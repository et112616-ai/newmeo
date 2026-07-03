from __future__ import annotations

import os
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd


SHIOAJI_API_KEY = os.getenv("SHIOAJI_API_KEY", "").strip()
SHIOAJI_SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY", "").strip()
SHIOAJI_SIMULATION = os.getenv("SHIOAJI_SIMULATION", "false").strip().lower() == "true"

_API = None
_LOGIN_TS = 0.0
LOGIN_TTL_SECONDS = 60 * 60 * 12

_STOCK_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
STOCK_SNAPSHOT_CACHE_TTL_SECONDS = 3


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


def get_api():
    """
    Lazy login。
    不在 import 時登入，避免 Render boot 時卡住。
    """
    global _API, _LOGIN_TS

    if not SHIOAJI_API_KEY or not SHIOAJI_SECRET_KEY:
        _debug("missing api key or secret")
        return None

    now = time.time()

    if _API is not None and now - _LOGIN_TS < LOGIN_TTL_SECONDS:
        return _API

    try:
        import shioaji as sj

        api = sj.Shioaji(simulation=SHIOAJI_SIMULATION)

        api.login(
            api_key=SHIOAJI_API_KEY,
            secret_key=SHIOAJI_SECRET_KEY,
            contracts_timeout=10000,
        )

        _API = api
        _LOGIN_TS = now

        _debug("login ok", "simulation =", SHIOAJI_SIMULATION)

        return _API

    except Exception as exc:
        _debug("login failed", exc)
        _API = None
        return None


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
    - "5m": 由 1分K resample 成 5分K
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

    if tf not in {"1m", "5m"}:
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

        if tf == "5m":
            df = (
                df.resample("5min")
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

def get_stock_intraday_yahoo_direct(
    stock_id: str,
    yf_symbol: str = "",
    time_frame: str = "1m",
    timeout: int = 5,
):
    """
    使用 Yahoo chart API direct 抓個股盤中 1m / 5m 資料。

    這不是 yfinance library，因此不會走 yfinance 的 cookie / crumb 流程，
    可避開常見的 YFRateLimitError。

    回傳：
    - DataFrame
    - index = DatetimeIndex
    - columns = Open, High, Low, Close, Volume
    """
    import time
    from urllib.parse import quote

    import pandas as pd
    import requests

    t0 = time.perf_counter()

    stock_id = str(stock_id or "").strip()
    yf_symbol = str(yf_symbol or "").strip()
    tf = str(time_frame or "1m").strip()

    if tf not in {"1m", "5m"}:
        print(
            "DEBUG yahoo_direct intraday | unsupported tf =",
            tf,
            flush=True,
        )
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

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
    }

    last_error = ""

    for symbol in clean_symbols:
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(symbol, safe="")

            params = {
                "range": "1d",
                "interval": "1m",
                "includePrePost": "false",
                "events": "history",
            }

            resp = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            )

            if resp.status_code != 200:
                last_error = f"status={resp.status_code}"
                continue

            payload = resp.json()

            chart = payload.get("chart") or {}
            error = chart.get("error")

            if error:
                last_error = str(error)
                continue

            result = chart.get("result") or []

            if not result:
                last_error = "empty_result"
                continue

            item = result[0]
            timestamps = item.get("timestamp") or []
            indicators = item.get("indicators") or {}
            quote_data = (indicators.get("quote") or [{}])[0]

            if not timestamps or not quote_data:
                last_error = "empty_timestamp_or_quote"
                continue

            length = len(timestamps)

            def _same_len(values):
                values = values or []

                if len(values) != length:
                    return [None] * length

                return values

            df = pd.DataFrame(
                {
                    "Open": _same_len(quote_data.get("open")),
                    "High": _same_len(quote_data.get("high")),
                    "Low": _same_len(quote_data.get("low")),
                    "Close": _same_len(quote_data.get("close")),
                    "Volume": _same_len(quote_data.get("volume")),
                },
                index=pd.to_datetime(timestamps, unit="s", utc=True)
                .tz_convert("Asia/Taipei")
                .tz_localize(None),
            )

            df = df.sort_index()

            required = ["Open", "High", "Low", "Close", "Volume"]

            for col in required:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna(subset=["Close"])
            df = df[df["Close"] > 0]

            if df.empty:
                last_error = "empty_valid_close"
                continue

            for col in ["Open", "High", "Low"]:
                df[col] = df[col].fillna(df["Close"])
                df.loc[df[col] <= 0, col] = df.loc[df[col] <= 0, "Close"]

            df["Volume"] = df["Volume"].fillna(0)

            df["High"] = df[["High", "Open", "Close"]].max(axis=1)
            df["Low"] = df[["Low", "Open", "Close"]].min(axis=1)

            if tf == "5m":
                df = (
                    df.resample("5min")
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

            if df.empty:
                last_error = "empty_after_resample"
                continue

            print(
                "DEBUG yahoo_direct intraday",
                "| stock =",
                stock_id,
                "| symbol =",
                symbol,
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
            last_error = repr(exc)
            continue

    print(
        "DEBUG yahoo_direct intraday failed",
        "| stock =",
        stock_id,
        "| symbols =",
        clean_symbols,
        "| error =",
        last_error,
        "| sec =",
        round(time.perf_counter() - t0, 3),
        flush=True,
    )

    return None
