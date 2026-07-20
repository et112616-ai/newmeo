from __future__ import annotations

import os
import time
import threading
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd


SHIOAJI_API_KEY = os.getenv("SHIOAJI_API_KEY", "").strip()
SHIOAJI_SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY", "").strip()
SHIOAJI_SIMULATION = os.getenv("SHIOAJI_SIMULATION", "false").strip().lower() == "true"

_API = None
_LOGIN_TS = 0.0
_LOGIN_LOCK = threading.Lock()
LOGIN_TTL_SECONDS = int(os.getenv("SHIOAJI_LOGIN_TTL_SECONDS", str(60 * 60 * 12)) or 0)

# 登入年齡只保留給健康狀態與除錯顯示，不再因超過 12 小時就把仍可用的
# Shioaji session 判定為 cold_api。實際 snapshots() 失敗時才讓 session 失效，
# 再由背景監控重新登入。
_LAST_LOGIN_SUCCESS = ""
_LAST_LOGIN_ERROR = ""
_CONSECUTIVE_LOGIN_FAILURES = 0
_RECONNECT_MONITOR_STARTED = False
_RECONNECT_MONITOR_THREAD = None
_RECONNECT_MONITOR_LOCK = threading.Lock()
SHIOAJI_RECONNECT_CHECK_SECONDS = max(
    15,
    int(os.getenv("SHIOAJI_RECONNECT_CHECK_SECONDS", "60") or 60),
)
SHIOAJI_ALLOW_COLD_STOCK_LOGIN = (
    os.getenv("SHIOAJI_ALLOW_COLD_STOCK_LOGIN", "0").strip() == "1"
)
# 最後一道保險：即使 Render 還殘留舊環境變數 = 1，也不允許 LINE
# 使用者請求同步等待 Shioaji login。只有維運者明確開啟第三個開關才會阻塞。
SHIOAJI_REQUEST_BLOCKING_LOGIN = (
    os.getenv("SHIOAJI_REQUEST_BLOCKING_LOGIN", "0").strip() == "1"
)

_STOCK_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
STOCK_SNAPSHOT_CACHE_TTL_SECONDS = 3

INTRADAY_UNIFIED_FIX_VERSION = "2026-07-16-v2.1-UNIFIED-ALL-TF-LOGIN-HOTFIX"
SHIOAJI_LOGIN_FIX_VERSION = "2026-07-16-v1-COMPATIBLE-LOGIN"
QUOTE_SERVICE_VERSION = "2026-07-20-v2.1-REALTIME-NONBLOCKING-SAFE"
INTRADAY_TIME_FRAMES = {"1m", "5m", "15m", "30m", "60m"}
INTRADAY_RESAMPLE_RULES = {
    "1m": "",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "60m": "60min",
}
YAHOO_INTRADAY_1M_RANGE = os.getenv("YAHOO_INTRADAY_1M_RANGE", "5d").strip() or "5d"
_YAHOO_INTRADAY_1M_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_YAHOO_INTRADAY_1M_CACHE_LOCK = threading.Lock()
YAHOO_INTRADAY_1M_CACHE_TTL_SECONDS = float(
    os.getenv("YAHOO_INTRADAY_1M_CACHE_TTL_SECONDS", "30") or 30
)


def _debug(*args):
    print("DEBUG shioaji |", *args, flush=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        if isinstance(value, Decimal):
            return float(value)

        text = str(value).replace(",", "").replace("%", "").strip()

        if not text:
            return default

        return float(text)

    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, float(default))))
    except Exception:
        return default


def _to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}

    if isinstance(obj, dict):
        return obj

    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass

    result: dict[str, Any] = {}

    for key in dir(obj):
        if key.startswith("_"):
            continue

        try:
            value = getattr(obj, key)

            if callable(value):
                continue

            result[key] = value
        except Exception:
            continue

    return result


def _normalize_ts(value: Any) -> str:
    """
    Shioaji snapshot 的 ts 可能是：
    - datetime
    - pandas Timestamp
    - int / float timestamp
    - 字串
    """
    if value is None or value == "":
        return ""

    try:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")

        ts = pd.to_datetime(value, errors="coerce")

        if pd.notna(ts):
            # 若有時區，轉台北時間；若沒有，就直接顯示
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert("Asia/Taipei")

            return ts.strftime("%Y-%m-%d %H:%M:%S")

    except Exception:
        pass

    return str(value)


def _login_shioaji_compatible(api) -> None:
    """
    相容不同 Shioaji 版本的 login() 簽名。

    部分版本支援 contracts_timeout，部分版本不支援；遇到
    unexpected keyword argument 時，立即移除該參數重試。
    """
    base_kwargs = {
        "api_key": SHIOAJI_API_KEY,
        "secret_key": SHIOAJI_SECRET_KEY,
    }

    try:
        api.login(
            **base_kwargs,
            contracts_timeout=10000,
        )
        _debug(
            "login signature",
            "| version =", SHIOAJI_LOGIN_FIX_VERSION,
            "| mode = contracts_timeout_supported",
        )
        return

    except TypeError as exc:
        message = str(exc)

        if "contracts_timeout" not in message:
            raise

        _debug(
            "login retry",
            "| version =", SHIOAJI_LOGIN_FIX_VERSION,
            "| reason = contracts_timeout_unsupported",
        )

    # 舊版 Shioaji：不帶 contracts_timeout。
    api.login(**base_kwargs)
    _debug(
        "login signature",
        "| version =", SHIOAJI_LOGIN_FIX_VERSION,
        "| mode = basic_api_key_secret",
    )


def _api_has_contracts(api: Any) -> bool:
    if api is None:
        return False

    try:
        return hasattr(api, "Contracts")
    except Exception:
        return False


def _invalidate_api(reason: Any = "") -> None:
    """標記目前 session 失效，交由下一次查詢或背景監控重新登入。"""
    global _API, _LOGIN_TS, _LAST_LOGIN_ERROR

    _API = None
    _LOGIN_TS = 0.0
    if reason:
        _LAST_LOGIN_ERROR = str(reason)

    _debug("session invalidated", "| reason =", str(reason or "unknown"))


def get_api(force_reconnect: bool = False):
    """
    Lazy login。
    不在 import 時登入，避免 Render boot 時卡住。

    使用 lock 避免多個請求同時建立 Shioaji session。
    """
    global _API, _LOGIN_TS
    global _LAST_LOGIN_SUCCESS, _LAST_LOGIN_ERROR, _CONSECUTIVE_LOGIN_FAILURES

    if not SHIOAJI_API_KEY or not SHIOAJI_SECRET_KEY:
        _debug("missing api key or secret")
        return None

    # 不再因登入超過固定時數就把可用 session 判成 cold_api。
    # Shioaji / Solace 連線若真的失效，snapshot 例外會呼叫 _invalidate_api()。
    if not force_reconnect and _api_has_contracts(_API):
        return _API

    with _LOGIN_LOCK:
        # 取得 lock 後再次檢查，避免其他 thread 已完成登入。
        if not force_reconnect and _api_has_contracts(_API):
            return _API

        try:
            import shioaji as sj

            api = sj.Shioaji(simulation=SHIOAJI_SIMULATION)
            _login_shioaji_compatible(api)

            _API = api
            _LOGIN_TS = time.time()
            _LAST_LOGIN_SUCCESS = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _LAST_LOGIN_ERROR = ""
            _CONSECUTIVE_LOGIN_FAILURES = 0

            _debug(
                "login ok",
                "| version =", SHIOAJI_LOGIN_FIX_VERSION,
                "| simulation =", SHIOAJI_SIMULATION,
            )

            return _API

        except Exception as exc:
            _CONSECUTIVE_LOGIN_FAILURES += 1
            _LAST_LOGIN_ERROR = repr(exc)
            _debug(
                "login failed",
                "| version =", SHIOAJI_LOGIN_FIX_VERSION,
                "| error =", repr(exc),
            )
            _API = None
            _LOGIN_TS = 0.0
            return None


def get_shioaji_status() -> dict[str, Any]:
    ready = is_shioaji_api_ready()
    login_age = max(0.0, time.time() - _LOGIN_TS) if _LOGIN_TS > 0 else 0.0

    return {
        "ready": ready,
        "quote_service_version": QUOTE_SERVICE_VERSION,
        "last_login_success": _LAST_LOGIN_SUCCESS,
        "last_login_error": _LAST_LOGIN_ERROR,
        "consecutive_failures": _CONSECUTIVE_LOGIN_FAILURES,
        "login_age_seconds": round(login_age, 1),
        "login_ttl_seconds": LOGIN_TTL_SECONDS,
        "ttl_enforced": False,
        "reconnect_monitor_alive": bool(
            _RECONNECT_MONITOR_THREAD is not None
            and _RECONNECT_MONITOR_THREAD.is_alive()
        ),
    }


def warmup_shioaji_once(force_reconnect: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    api = get_api(force_reconnect=force_reconnect)
    status = get_shioaji_status()
    status["ready"] = bool(api is not None and is_shioaji_api_ready())
    status["warmup_seconds"] = round(time.perf_counter() - started, 3)
    return status


def _shioaji_reconnect_monitor_loop() -> None:
    while True:
        try:
            if not is_shioaji_api_ready():
                status = warmup_shioaji_once(force_reconnect=False)
                _debug(
                    "reconnect monitor",
                    "| ready =", status.get("ready"),
                    "| sec =", status.get("warmup_seconds"),
                    "| error =", status.get("last_login_error"),
                )
        except Exception as exc:
            _debug("reconnect monitor failed", "| error =", repr(exc))

        time.sleep(SHIOAJI_RECONNECT_CHECK_SECONDS)


def start_shioaji_reconnect_monitor() -> bool:
    """啟動每個 Gunicorn worker 各自的 Shioaji 背景復線監控。"""
    global _RECONNECT_MONITOR_STARTED, _RECONNECT_MONITOR_THREAD

    with _RECONNECT_MONITOR_LOCK:
        if _RECONNECT_MONITOR_STARTED:
            return bool(
                _RECONNECT_MONITOR_THREAD is not None
                and _RECONNECT_MONITOR_THREAD.is_alive()
            )

        _RECONNECT_MONITOR_STARTED = True
        _RECONNECT_MONITOR_THREAD = threading.Thread(
            target=_shioaji_reconnect_monitor_loop,
            name="shioaji-reconnect-monitor",
            daemon=True,
        )
        _RECONNECT_MONITOR_THREAD.start()
        return True


def _get_stock_contract(api, stock_id: str):
    sid = str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()

    if not sid:
        return None

    try:
        return api.Contracts.Stocks[sid]
    except Exception:
        pass

    try:
        return api.Contracts.Stocks.TSE[sid]
    except Exception:
        pass

    try:
        return api.Contracts.Stocks.OTC[sid]
    except Exception:
        pass

    return None


def get_stock_snapshot(stock_id: str) -> dict[str, Any] | None:
    """
    查現股即時 snapshot。

    回傳：
    {
      stock_id,
      close,
      change,
      change_pct,
      open,
      high,
      low,
      volume,
      total_volume,
      ts,
      source
    }
    """
    sid = str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()

    if not sid:
        return None

    now = time.time()
    cached = _STOCK_SNAPSHOT_CACHE.get(sid)

    if cached:
        ts, data = cached
        if now - ts <= STOCK_SNAPSHOT_CACHE_TTL_SECONDS:
            return data

    api = get_api()

    if api is None:
        return None

    contract = _get_stock_contract(api, sid)

    if contract is None:
        _debug("stock contract not found", sid)
        return None

    try:
        snapshots = api.snapshots([contract])

        if not snapshots:
            return None

        raw = _to_dict(snapshots[0])

        close = _safe_float(raw.get("close"))
        change = _safe_float(raw.get("change_price"))
        change_pct = _safe_float(raw.get("change_rate"))

        data = {
            "stock_id": sid,
            "close": close,
            "change": change,
            "change_pct": change_pct,
            "open": _safe_float(raw.get("open")),
            "high": _safe_float(raw.get("high")),
            "low": _safe_float(raw.get("low")),
            "volume": _safe_int(raw.get("volume")),
            "total_volume": _safe_int(raw.get("total_volume")),
            "buy_price": _safe_float(raw.get("buy_price")),
            "sell_price": _safe_float(raw.get("sell_price")),
            "ts": _normalize_ts(raw.get("ts")),
            "source": "Shioaji",
        }

        _STOCK_SNAPSHOT_CACHE[sid] = (now, data)

        _debug(
            "stock snapshot",
            sid,
            "close =",
            data["close"],
            "change =",
            data["change"],
            "change_pct =",
            data["change_pct"],
            "ts =",
            data["ts"],
        )

        return data

    except Exception as exc:
        _debug("stock snapshot failed", sid, exc)
        _invalidate_api(repr(exc))
        return None


def append_stock_snapshot_to_intraday_df(df: pd.DataFrame, stock_id: str) -> pd.DataFrame:
    """
    把 Shioaji 即時 snapshot 補到 1m / 5m 圖表最後一點。

    注意：
    不要把 Shioaji 原始物件放進 df.attrs，
    否則 pandas deepcopy 時可能出現：
    TypeError: cannot pickle 'builtins.TickType' object
    """
    if df is None or df.empty:
        return df

    snapshot = get_stock_snapshot(stock_id)

    if not snapshot:
        return df

    price = _safe_float(snapshot.get("close"))

    if price <= 0:
        return df

    ts_text = str(snapshot.get("ts") or "").strip()

    if not ts_text:
        return df

    try:
        snap_ts = pd.to_datetime(ts_text)

        if pd.isna(snap_ts):
            return df

        result = df.copy()

        # 只存純文字 / 純數字，避免 TickType 之類物件造成 deepcopy 錯誤
        result.attrs["shioaji_snapshot"] = {
            "stock_id": str(snapshot.get("stock_id") or ""),
            "close": _safe_float(snapshot.get("close")),
            "change": _safe_float(snapshot.get("change")),
            "change_pct": _safe_float(snapshot.get("change_pct")),
            "volume": _safe_int(snapshot.get("volume")),
            "total_volume": _safe_int(snapshot.get("total_volume")),
            "ts": str(snapshot.get("ts") or ""),
            "source": "Shioaji",
        }

        # 對齊 index 時區
        if getattr(result.index, "tz", None) is not None:
            if snap_ts.tzinfo is None:
                snap_ts = snap_ts.tz_localize("Asia/Taipei")

            snap_ts = snap_ts.tz_convert(result.index.tz)

        else:
            if snap_ts.tzinfo is not None:
                snap_ts = snap_ts.tz_convert("Asia/Taipei").tz_localize(None)

        last_idx = result.index[-1]

        # 如果 Shioaji 時間沒有比 K 線新，就不硬塞點，但保留 attrs 給價格區使用
        if snap_ts <= last_idx:
            return result

        last_row = result.iloc[-1].copy()

        last_row["Open"] = price
        last_row["High"] = max(price, _safe_float(last_row.get("High"), price))
        last_row["Low"] = min(price, _safe_float(last_row.get("Low"), price))
        last_row["Close"] = price

        if "Volume" in result.columns:
            last_row["Volume"] = _safe_int(
                snapshot.get("volume")
                or snapshot.get("total_volume")
            )

        result.loc[snap_ts] = last_row
        result = result.sort_index()

        return result

    except Exception as exc:
        _debug("append snapshot failed", stock_id, exc)
        return df


# =========================
# Shioaji 期貨即時報價
# =========================

SHIOAJI_FUTURES_SNAPSHOT_CACHE_TTL_SECONDS = 3
_SHIOAJI_FUTURES_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _normalize_futures_yyyymm(value: Any) -> str:
    text = str(value or "").strip()

    if not text:
        return ""

    digits = "".join(ch for ch in text if ch.isdigit())

    if len(digits) >= 6:
        return digits[:6]

    return ""


def _try_get_contract(container, code: str):
    if container is None:
        return None

    code = str(code or "").strip()

    if not code:
        return None

    try:
        return container[code]
    except Exception:
        pass

    try:
        return getattr(container, code)
    except Exception:
        pass

    return None


def _get_futures_contract(api, futures_id: str, contract_date: str = ""):
    """
    取得股票期貨 contract。

    優先：
    1. 指定月份，例如 CCF202607
    2. 連續近月，例如 CCFR1
    3. base code，例如 CCF
    """
    base = str(futures_id or "").strip().upper()
    yyyymm = _normalize_futures_yyyymm(contract_date)

    if not base:
        return None

    futures_root = getattr(api.Contracts, "Futures", None)

    if futures_root is None:
        return None

    group = _try_get_contract(futures_root, base)

    candidates: list[str] = []

    if yyyymm:
        candidates.append(f"{base}{yyyymm}")

    candidates.extend(
        [
            f"{base}R1",
            f"{base}R2",
            base,
        ]
    )

    for code in candidates:
        contract = _try_get_contract(group, code)

        if contract is not None:
            return contract

        contract = _try_get_contract(futures_root, code)

        if contract is not None:
            return contract

    return None


def get_futures_snapshot(
    futures_id: str,
    contract_date: str = "",
) -> dict[str, Any] | None:
    """
    查期貨即時 snapshot。

    回傳純 dict，不能放 Shioaji 原始物件，避免 TickType deepcopy 錯誤。
    """
    fid = str(futures_id or "").strip().upper()
    yyyymm = _normalize_futures_yyyymm(contract_date)

    if not fid:
        return None

    cache_key = f"{fid}:{yyyymm or 'R1'}"
    now = time.time()

    cached = _SHIOAJI_FUTURES_SNAPSHOT_CACHE.get(cache_key)

    if cached:
        ts, data = cached

        if now - ts <= SHIOAJI_FUTURES_SNAPSHOT_CACHE_TTL_SECONDS:
            return data

    api = get_api()

    if api is None:
        return None

    contract = _get_futures_contract(
        api,
        futures_id=fid,
        contract_date=yyyymm,
    )

    if contract is None:
        _debug("futures contract not found", fid, yyyymm)
        return None

    try:
        snapshots = api.snapshots([contract])

        if not snapshots:
            return None

        raw = _to_dict(snapshots[0])

        close = _safe_float(raw.get("close"))

        if close <= 0:
            return None

        data = {
            "futures_id": str(raw.get("code") or fid),
            "close": close,
            "change": _safe_float(raw.get("change_price")),
            "change_pct": _safe_float(raw.get("change_rate")),
            "open": _safe_float(raw.get("open")),
            "high": _safe_float(raw.get("high")),
            "low": _safe_float(raw.get("low")),
            "volume": _safe_int(raw.get("volume")),
            "total_volume": _safe_int(raw.get("total_volume")),
            "buy_price": _safe_float(raw.get("buy_price")),
            "sell_price": _safe_float(raw.get("sell_price")),
            "ts": _normalize_ts(raw.get("ts")),
            "source": "Shioaji",
        }

        _SHIOAJI_FUTURES_SNAPSHOT_CACHE[cache_key] = (now, data)

        _debug(
            "futures snapshot",
            fid,
            yyyymm,
            "contract =",
            data["futures_id"],
            "close =",
            data["close"],
            "change =",
            data["change"],
            "change_pct =",
            data["change_pct"],
            "total_volume =",
            data["total_volume"],
            "ts =",
            data["ts"],
        )

        return data
    
    except Exception as exc:
        _debug("futures snapshot failed", fid, yyyymm, exc)
        return None

def get_stock_intraday_kbars(stock_id: str, time_frame: str = "1m", days: int = 1):
    """
    使用 Shioaji kbars 取得個股盤中 1m / 5m K線。
    回傳欄位格式會整理成 chart_service / stock_service 常用格式：
    Open, High, Low, Close, Volume
    index = DatetimeIndex

    time_frame:
    - "1m": 回傳 1分K
    - "5m" / "15m" / "30m" / "60m": 由同一份 1分K resample
    """
    import time
    from datetime import timedelta

    t0 = time.perf_counter()

    stock_id = str(stock_id or "").strip()
    tf = str(time_frame or "1m").strip()

    if not stock_id:
        print(
            "DEBUG shioaji kbars | empty stock_id",
            flush=True,
        )
        return None

    if tf not in INTRADAY_TIME_FRAMES:
        print(
            "DEBUG shioaji kbars | unsupported tf =",
            tf,
            flush=True,
        )
        return None

    try:
        api = get_api()

        if api is None:
            print(
                "DEBUG shioaji kbars | api none",
                "| stock =",
                stock_id,
                flush=True,
            )
            return None

        try:
            contract = api.Contracts.Stocks[stock_id]
        except Exception:
            try:
                contract = getattr(api.Contracts.Stocks, stock_id)
            except Exception:
                contract = None

        if contract is None:
            print(
                "DEBUG shioaji kbars | contract none",
                "| stock =",
                stock_id,
                flush=True,
            )
            return None

        now = pd.Timestamp.now(tz="Asia/Taipei")
        end_date = now.date()
        start_date = end_date - timedelta(days=max(0, int(days) - 1))

        kbars = api.kbars(
            contract=contract,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
        )

        raw_df = pd.DataFrame({**kbars})

        if raw_df is None or raw_df.empty:
            print(
                "DEBUG shioaji kbars | empty",
                "| stock =",
                stock_id,
                "| tf =",
                tf,
                "| sec =",
                round(time.perf_counter() - t0, 3),
                flush=True,
            )
            return None

        # Shioaji kbars 常見欄位：
        # ts, Open, High, Low, Close, Volume, Amount
        # 保險處理大小寫。
        rename_map = {}

        for col in raw_df.columns:
            lower = str(col).lower()

            if lower == "ts":
                rename_map[col] = "ts"
            elif lower == "open":
                rename_map[col] = "Open"
            elif lower == "high":
                rename_map[col] = "High"
            elif lower == "low":
                rename_map[col] = "Low"
            elif lower == "close":
                rename_map[col] = "Close"
            elif lower == "volume":
                rename_map[col] = "Volume"
            elif lower == "amount":
                rename_map[col] = "Amount"

        df = raw_df.rename(columns=rename_map).copy()

        if "ts" not in df.columns:
            print(
                "DEBUG shioaji kbars | missing ts",
                "| stock =",
                stock_id,
                "| cols =",
                list(df.columns),
                flush=True,
            )
            return None

        df["ts"] = pd.to_datetime(df["ts"], errors="coerce")

        if getattr(df["ts"].dt, "tz", None) is not None:
            try:
                df["ts"] = df["ts"].dt.tz_convert("Asia/Taipei").dt.tz_localize(None)
            except Exception:
                pass

        df = df.dropna(subset=["ts"])
        df = df.set_index("ts")
        df = df.sort_index()

        required = ["Open", "High", "Low", "Close", "Volume"]

        for col in required:
            if col not in df.columns:
                df[col] = 0

        df = df[required].copy()

        for col in required:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        df = df[df["Close"] > 0]

        if df.empty:
            print(
                "DEBUG shioaji kbars | no valid close",
                "| stock =",
                stock_id,
                "| tf =",
                tf,
                "| sec =",
                round(time.perf_counter() - t0, 3),
                flush=True,
            )
            return None

        for col in ["Open", "High", "Low"]:
            df.loc[df[col] <= 0, col] = df.loc[df[col] <= 0, "Close"]

        df["High"] = df[["High", "Open", "Close"]].max(axis=1)
        df["Low"] = df[["Low", "Open", "Close"]].min(axis=1)

        rule = INTRADAY_RESAMPLE_RULES.get(tf, "")
        if rule:
            df = (
                df.resample(rule, label="left", closed="left")
                .agg(
                    {
                        "Open": "first",
                        "High": "max",
                        "Low": "min",
                        "Close": "last",
                        "Volume": "sum",
                    }
                )
                .dropna(subset=["Open", "High", "Low", "Close"])
            )
            df = df[df["Close"] > 0]

        print(
            "DEBUG shioaji kbars",
            "| stock =",
            stock_id,
            "| tf =",
            tf,
            "| rows =",
            len(df),
            "| first =",
            df.index[0] if len(df) else "",
            "| last =",
            df.index[-1] if len(df) else "",
            "| sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )

        return df

    except Exception as exc:
        print(
            "DEBUG shioaji kbars failed",
            "| stock =",
            stock_id,
            "| tf =",
            tf,
            "| error =",
            repr(exc),
            "| sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )
        return None

def _copy_intraday_df(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    try:
        result.attrs.update(dict(getattr(df, "attrs", {}) or {}))
    except Exception:
        pass
    return result


def _format_yahoo_intraday_time_frame(raw_df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """從同一份多日 1 分底稿輸出 1/5/15/30/60 分，並保留原始最新報價時間。"""
    tf = str(tf or "1m").strip()
    work = _copy_intraday_df(raw_df)
    attrs = dict(getattr(work, "attrs", {}) or {})

    if work.empty or tf not in INTRADAY_TIME_FRAMES:
        return work

    rule = INTRADAY_RESAMPLE_RULES.get(tf, "")

    if not rule:
        result = work.copy()
    else:
        result = (
            work.resample(rule, label="left", closed="left")
            .agg(
                {
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum",
                }
            )
            .dropna(subset=["Open", "High", "Low", "Close"])
        )
        result = result[result["Close"] > 0]

    result.attrs.update(attrs)
    result.attrs["intraday_base_tf"] = "1m"
    result.attrs["display_tf"] = tf
    result.attrs["intraday_unified_version"] = INTRADAY_UNIFIED_FIX_VERSION

    if len(work):
        result.attrs["latest_quote_time"] = str(work.index[-1])
        try:
            result.attrs["latest_quote_price"] = float(work["Close"].iloc[-1])
        except Exception:
            pass

    return result

def get_stock_intraday_yahoo_direct(
    stock_id: str,
    yf_symbol: str = "",
    time_frame: str = "1m",
    timeout: int = 5,
):
    """
    使用 Yahoo chart API direct 抓個股盤中資料。

    修正版重點：
    - Yahoo 永遠只抓一份多日 1 分原始資料。
    - 1/5/15/30/60 分全部由同一份 1 分底稿輸出。
    - 1 分底稿短暫快取，連續切換不同分 K 時會使用同一批資料。
    - 聚合後仍保留原始 1 分最新時間於 attrs["latest_quote_time"]。
    """
    import time
    from urllib.parse import quote

    import pandas as pd
    import requests

    t0 = time.perf_counter()

    stock_id = str(stock_id or "").strip()
    yf_symbol = str(yf_symbol or "").strip()
    tf = str(time_frame or "1m").strip()

    if tf not in INTRADAY_TIME_FRAMES:
        print("DEBUG yahoo_direct intraday | unsupported tf =", tf, flush=True)
        return None

    symbols = []
    if yf_symbol:
        symbols.append(yf_symbol)
    if stock_id:
        symbols.extend([f"{stock_id}.TW", f"{stock_id}.TWO"])

    clean_symbols = []
    for symbol in symbols:
        symbol = str(symbol or "").strip().upper()
        if symbol and symbol not in clean_symbols:
            clean_symbols.append(symbol)

    if not clean_symbols:
        return None

    # 先找 1 分底稿快取。連續按 1m / 5m 時，兩張圖使用同一份資料。
    now = time.time()
    with _YAHOO_INTRADAY_1M_CACHE_LOCK:
        for symbol in clean_symbols:
            cached = _YAHOO_INTRADAY_1M_CACHE.get(symbol)
            if not cached:
                continue
            cache_ts, cached_df = cached
            if now - cache_ts <= YAHOO_INTRADAY_1M_CACHE_TTL_SECONDS:
                result = _format_yahoo_intraday_time_frame(cached_df, tf)
                print(
                    "DEBUG yahoo_direct intraday cache hit",
                    "| version =", INTRADAY_UNIFIED_FIX_VERSION,
                    "| stock =", stock_id,
                    "| symbol =", symbol,
                    "| tf =", tf,
                    "| raw_rows =", len(cached_df),
                    "| rows =", len(result),
                    "| raw_last =", cached_df.index[-1] if len(cached_df) else "",
                    "| display_last =", result.index[-1] if len(result) else "",
                    flush=True,
                )
                return result

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
    }
    last_error = ""

    for symbol in clean_symbols:
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(symbol, safe="")
            params = {
                "range": YAHOO_INTRADAY_1M_RANGE,
                "interval": "1m",
                "includePrePost": "false",
                "events": "history",
            }
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                last_error = f"status={resp.status_code}"
                continue

            payload = resp.json()
            chart = payload.get("chart") or {}
            error = chart.get("error")
            if error:
                last_error = str(error)
                continue

            result_list = chart.get("result") or []
            if not result_list:
                last_error = "empty_result"
                continue

            item = result_list[0]
            meta_data = item.get("meta") or {}
            yahoo_previous_close = (
                meta_data.get("previousClose")
                or meta_data.get("chartPreviousClose")
                or meta_data.get("regularMarketPreviousClose")
            )
            yahoo_regular_price = meta_data.get("regularMarketPrice")
            yahoo_regular_time = meta_data.get("regularMarketTime")

            timestamps = item.get("timestamp") or []
            indicators = item.get("indicators") or {}
            quote_data = (indicators.get("quote") or [{}])[0]
            if not timestamps or not quote_data:
                last_error = "empty_timestamp_or_quote"
                continue

            length = len(timestamps)

            def _same_len(values):
                values = values or []
                return values if len(values) == length else [None] * length

            raw_df = pd.DataFrame(
                {
                    "Open": _same_len(quote_data.get("open")),
                    "High": _same_len(quote_data.get("high")),
                    "Low": _same_len(quote_data.get("low")),
                    "Close": _same_len(quote_data.get("close")),
                    "Volume": _same_len(quote_data.get("volume")),
                },
                index=(
                    pd.to_datetime(timestamps, unit="s", utc=True)
                    .tz_convert("Asia/Taipei")
                    .tz_localize(None)
                ),
            ).sort_index()

            required = ["Open", "High", "Low", "Close", "Volume"]
            for col in required:
                raw_df[col] = pd.to_numeric(raw_df[col], errors="coerce")

            raw_df = raw_df.dropna(subset=["Close"])
            raw_df = raw_df[raw_df["Close"] > 0]
            if raw_df.empty:
                last_error = "empty_valid_close"
                continue

            for col in ["Open", "High", "Low"]:
                raw_df[col] = raw_df[col].fillna(raw_df["Close"])
                raw_df.loc[raw_df[col] <= 0, col] = raw_df.loc[raw_df[col] <= 0, "Close"]

            raw_df["Volume"] = raw_df["Volume"].fillna(0)
            raw_df["High"] = raw_df[["High", "Open", "Close"]].max(axis=1)
            raw_df["Low"] = raw_df[["Low", "Open", "Close"]].min(axis=1)
            raw_df = raw_df[~raw_df.index.duplicated(keep="last")]

            if yahoo_previous_close is not None:
                raw_df.attrs["previous_close"] = float(yahoo_previous_close)
            if yahoo_regular_price is not None:
                raw_df.attrs["regular_market_price"] = float(yahoo_regular_price)
            if yahoo_regular_time:
                try:
                    regular_ts = (
                        pd.to_datetime(yahoo_regular_time, unit="s", utc=True)
                        .tz_convert("Asia/Taipei")
                        .tz_localize(None)
                    )
                    raw_df.attrs["regular_market_time"] = str(regular_ts)
                except Exception:
                    pass

            raw_df.attrs["latest_quote_time"] = str(raw_df.index[-1])
            raw_df.attrs["latest_quote_price"] = float(raw_df["Close"].iloc[-1])
            raw_df.attrs["symbol"] = symbol
            raw_df.attrs["source"] = "yahoo_direct_1m_base"
            raw_df.attrs["intraday_base_tf"] = "1m"
            raw_df.attrs["version"] = INTRADAY_UNIFIED_FIX_VERSION

            with _YAHOO_INTRADAY_1M_CACHE_LOCK:
                _YAHOO_INTRADAY_1M_CACHE[symbol] = (time.time(), _copy_intraday_df(raw_df))

            formatted = _format_yahoo_intraday_time_frame(raw_df, tf)

            print(
                "DEBUG yahoo_direct intraday",
                "| version =", INTRADAY_UNIFIED_FIX_VERSION,
                "| stock =", stock_id,
                "| symbol =", symbol,
                "| tf =", tf,
                "| raw_rows =", len(raw_df),
                "| rows =", len(formatted),
                "| previous_close =", formatted.attrs.get("previous_close"),
                "| raw_first =", raw_df.index[0] if len(raw_df) else "",
                "| raw_last =", raw_df.index[-1] if len(raw_df) else "",
                "| display_last =", formatted.index[-1] if len(formatted) else "",
                "| sec =", round(time.perf_counter() - t0, 3),
                flush=True,
            )
            return formatted

        except Exception as exc:
            last_error = repr(exc)
            continue

    print(
        "DEBUG yahoo_direct intraday failed",
        "| version =", INTRADAY_UNIFIED_FIX_VERSION,
        "| stock =", stock_id,
        "| symbols =", clean_symbols,
        "| error =", last_error,
        "| sec =", round(time.perf_counter() - t0, 3),
        flush=True,
    )
    return None


def is_shioaji_api_ready() -> bool:
    """
    只檢查目前 process 的 Shioaji session，不觸發冷登入。

    舊版漏掉真正使用的全域變數 `_API`，導致已登入仍被判定 cold_api。
    """
    return _api_has_contracts(_API)


def append_stock_snapshot_to_intraday_df_fast(
    df,
    stock_id: str,
    allow_cold_login: bool = False,
):
    """
    快速版 append snapshot。

    allow_cold_login=False：
    - 若 Shioaji 還沒登入，直接跳過，不觸發 get_api()。
    - 避免使用者查第一檔股票時等 10~20 秒。

    allow_cold_login=True：
    - 行為接近原本 append_stock_snapshot_to_intraday_df()。
    - 可用於 warmup 或你真的想強制拿永豐最新 snapshot 的情境。
    """
    import time

    t0 = time.perf_counter()

    stock_id = str(stock_id or "").strip()

    effective_allow_cold_login = bool(
        allow_cold_login
        and SHIOAJI_ALLOW_COLD_STOCK_LOGIN
        and SHIOAJI_REQUEST_BLOCKING_LOGIN
    )

    if not effective_allow_cold_login and not is_shioaji_api_ready():
        # 只要求背景監控登入；目前 LINE request 立即沿用 Yahoo，不等待 login lock。
        try:
            start_shioaji_reconnect_monitor()
        except Exception:
            pass

        print(
            "DEBUG shioaji fast append skip",
            "| stock =",
            stock_id,
            "| reason = cold_api",
            "| background_requested = True",
            "| sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )
        return df

    if not is_shioaji_api_ready():
        print(
            "DEBUG shioaji fast append cold login",
            "| stock =", stock_id,
            "| enabled =", effective_allow_cold_login,
            flush=True,
        )

    try:
        result = append_stock_snapshot_to_intraday_df(df, stock_id)

        print(
            "DEBUG shioaji fast append done",
            "| stock =",
            stock_id,
            "| allow_cold_login =",
            effective_allow_cold_login,
            "| sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )

        return result

    except Exception as exc:
        print(
            "DEBUG shioaji fast append failed",
            "| stock =",
            stock_id,
            "| error =",
            repr(exc),
            "| sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )
        return df
