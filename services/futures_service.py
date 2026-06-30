    from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from typing import Any

import matplotlib
matplotlib.use("Agg")

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import pandas as pd
import requests
import yfinance as yf

from config import FINMIND_TOKEN
from services.upload_service import publish_figure


try:
    from services.futures_map_service import get_stock_futures_mapping
except Exception:
    def get_stock_futures_mapping(stock_id: str):
        return None


FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FUTURES_SNAPSHOT_URL = "https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot"

FUTURES_SNAPSHOT_CACHE_TTL_SECONDS = 15
_FUTURES_SNAPSHOT_CACHE: dict[str, tuple[float, list[dict]]] = {}

SPOT_CACHE_TTL_SECONDS = 20
_SPOT_CACHE: dict[str, tuple[float, float]] = {}


@dataclass
class FuturesSnapshot:
    available: bool
    message: str

    stock_id: str
    stock_name: str

    futures_id: str = ""
    futures_name: str = ""
    contract_date: str = ""
    trade_date: str = ""
    trading_session: str = ""
    chart_url: str = ""

    future_price: float = 0.0
    future_change: float = 0.0
    future_change_pct: float = 0.0

    spot_price: float = 0.0
    basis: float = 0.0
    basis_pct: float = 0.0

    volume: int = 0
    open_interest: int = 0
    settlement_price: float = 0.0
    
    quote_source: str = "日成交"
    quote_time: str = ""

# fallback 用：如果 Supabase 還沒同步到，才吃這裡
STOCK_FUTURES_MAP: dict[str, dict[str, Any]] = {
    "2330": {
        "name": "台積電期貨",
        "candidates": ["CDF", "CD"],
    },
    "2337": {
        "name": "旺宏期貨",
        "candidates": ["DIF", "DI"],
    },
    "2344": {
        "name": "華邦電期貨",
        "candidates": ["FZF", "FZ"],
    },
}


def _debug(*args):
    print("DEBUG futures |", *args, flush=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").strip()

            if not value:
                return default

        return float(value)

    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, float(default))))
    except Exception:
        return default


def _clean_stock_id(stock_id: str) -> str:
    return str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()


def _start_date(days: int = 90) -> str:
    return (datetime.utcnow().date() - timedelta(days=days)).strftime("%Y-%m-%d")


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys([str(v).strip().upper() for v in values if str(v).strip()]))


def _build_candidates_from_futures_code(futures_code: str) -> list[str]:
    """
    期交所標的表通常會給基礎代碼，例如：
    2408 南亞科 = CY

    FinMind / 行情資料可能吃：
    CYF 或 CY

    所以自動產生候選：
    CY  -> CYF, CY
    CYF -> CYF, CY
    """
    code = str(futures_code or "").strip().upper()

    if not code:
        return []

    candidates: list[str] = []

    if code.endswith("F"):
        candidates.append(code)
        candidates.append(code[:-1])
    else:
        candidates.append(f"{code}F")
        candidates.append(code)

    return _unique(candidates)


def _resolve_stock_futures_candidates(stock_id: str, stock_name: str) -> tuple[str, list[str], str]:
    """
    優先查 Supabase stock_futures_map。
    查不到才 fallback 到 STOCK_FUTURES_MAP。

    回傳：
    futures_name, candidates, source
    """
    sid = _clean_stock_id(stock_id)

    # 1. Supabase mapping
    try:
        mapping = get_stock_futures_mapping(sid)
    except Exception as exc:
        _debug("supabase mapping failed:", sid, exc)
        mapping = None

    if mapping:
        futures_name = (
            mapping.get("futures_name")
            or mapping.get("name")
            or f"{mapping.get('stock_name') or stock_name}期貨"
        )

        candidates = list(mapping.get("candidates") or [])

        if not candidates:
            futures_code = (
                mapping.get("futures_code")
                or mapping.get("code")
                or mapping.get("futures_id")
                or ""
            )
            candidates = _build_candidates_from_futures_code(str(futures_code))

        candidates = _unique(candidates)

        if candidates:
            _debug("mapping source=supabase", sid, futures_name, candidates)
            return str(futures_name), candidates, "supabase"

    # 2. fallback manual map
    info = STOCK_FUTURES_MAP.get(sid)

    if info:
        futures_name = str(info.get("name") or f"{stock_name}期貨")
        candidates = _unique(list(info.get("candidates") or []))

        if candidates:
            _debug("mapping source=fallback", sid, futures_name, candidates)
            return futures_name, candidates, "fallback"

    _debug("mapping not found", sid)

    return "", [], "none"

def _request_finmind_futures_daily(futures_id: str) -> list[dict]:
    """
    抓 FinMind 期貨日成交資訊。
    這個是 fallback 用：
    即時報價抓不到時，仍然可以用 TaiwanFuturesDaily 顯示資料。
    """
    fid = str(futures_id or "").strip().upper()

    if not fid:
        return []

    params = {
        "dataset": "TaiwanFuturesDaily",
        "data_id": fid,
        "start_date": _start_date(90),
    }

    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    try:
        res = requests.get(
            FINMIND_URL,
            params=params,
            timeout=15,
        )

        if res.status_code >= 400:
            print(
                "_request_finmind_futures_daily failed: "
                f"futures_id={fid}, status={res.status_code}, "
                f"body={res.text[:200]}",
                flush=True,
            )
            return []

        payload = res.json()
        rows = payload.get("data") or []

        if not isinstance(rows, list):
            return []

        return rows

    except Exception as exc:
        print(
            f"_request_finmind_futures_daily failed: futures_id={fid}, error={exc}",
            flush=True,
        )
        return []

def _request_finmind_futures_snapshot(futures_id: str) -> list[dict]:
    """
    抓 FinMind 台股期貨即時資訊。

    抓不到時回傳 []，讓系統 fallback 日成交。
    token 同時放 params 與 Authorization，增加相容性。
    """
    fid = str(futures_id or "").strip().upper()

    if not fid:
        return []

    now = time_module.time()
    cached = _FUTURES_SNAPSHOT_CACHE.get(fid)

    if cached:
        ts, rows = cached
        if now - ts <= FUTURES_SNAPSHOT_CACHE_TTL_SECONDS:
            return rows

    params = {
        "data_id": fid,
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
    }

    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"

    try:
        res = requests.get(
            FUTURES_SNAPSHOT_URL,
            headers=headers,
            params=params,
            timeout=8,
        )

        if res.status_code in {401, 403}:
            _debug(
                "realtime snapshot permission denied",
                "futures_id =",
                fid,
                "status =",
                res.status_code,
            )
            return []

        if res.status_code >= 400:
            _debug(
                "realtime snapshot failed",
                "futures_id =",
                fid,
                "status =",
                res.status_code,
                "body =",
                res.text[:200],
            )
            return []

        payload = res.json()
        rows = payload.get("data") or []

        if not isinstance(rows, list):
            rows = []

        _debug(
            "realtime snapshot rows",
            "futures_id =",
            fid,
            "count =",
            len(rows),
        )

        if rows:
            _debug("realtime snapshot sample", rows[-1])

        _FUTURES_SNAPSHOT_CACHE[fid] = (now, rows)

        return rows

    except Exception as exc:
        _debug("realtime snapshot exception", fid, exc)
        return []
        
def _pick_realtime_snapshot_row(rows: list[dict], base_futures_id: str) -> dict | None:
    """
    從 taiwan_futures_snapshot 回傳資料中挑出近月即時報價。

    data_id=TXF / CDF / CYF 時，回傳 futures_id 可能像：
    TXFR1、TXFF3、CDFR1、CYFR1 ...

    優先順序：
    1. futures_id 完全等於 base
    2. futures_id 以 base 開頭且含 R1
    3. futures_id 以 base 開頭
    4. 其他有效最新資料
    """
    base = str(base_futures_id or "").strip().upper()

    valid = []

    for r in rows:
        price = _safe_float(r.get("close"))

        if price <= 0:
            continue

        item = dict(r)
        item["_snapshot_date"] = str(r.get("date") or "")
        item["_snapshot_futures_id"] = str(r.get("futures_id") or "").strip().upper()

        valid.append(item)

    if not valid:
        return None

    exact = [
        r for r in valid
        if r["_snapshot_futures_id"] == base
    ]

    if exact:
        return sorted(exact, key=lambda r: r["_snapshot_date"])[-1]

    front_month = [
        r for r in valid
        if r["_snapshot_futures_id"].startswith(base)
        and "R1" in r["_snapshot_futures_id"]
    ]

    if front_month:
        return sorted(front_month, key=lambda r: r["_snapshot_date"])[-1]

    prefix = [
        r for r in valid
        if r["_snapshot_futures_id"].startswith(base)
    ]

    if prefix:
        return sorted(
            prefix,
            key=lambda r: (
                r["_snapshot_date"],
                _safe_int(r.get("total_volume") or r.get("volume")),
            ),
        )[-1]

    return sorted(valid, key=lambda r: r["_snapshot_date"])[-1]

def _get_realtime_snapshot_for_candidates(
    candidates: list[str],
    preferred_id: str,
) -> tuple[str, dict | None]:
    """
    逐一嘗試即時報價。

    例如聯電：
    candidates = ["CCF", "CC"]

    會依序試：
    CCF
    CC
    """
    ids = _unique([preferred_id] + list(candidates or []))

    for fid in ids:
        rows = _request_finmind_futures_snapshot(fid)
        row = _pick_realtime_snapshot_row(rows, fid)

        if row:
            _debug(
                "use realtime candidate",
                "request_id =",
                fid,
                "snapshot_futures_id =",
                row.get("futures_id"),
            )
            return fid, row

    return "", None

def _normalize_contract_date(value: Any) -> str:
    """
    只接受單一契約月份。

    接受：
    202607
    2026/07
    2026-07

    排除：
    202608/202609
    全月份
    所有契約
    空白
    """
    s = str(value or "").strip()

    if not s:
        return ""

    lower = s.lower()

    if "all" in lower or "全" in s or "所有" in s:
        return ""

    digits = "".join(ch for ch in s if ch.isdigit())

    if "/" in s:
        parts = s.split("/")

        # 2026/07 可以接受
        if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
            if len(digits) >= 6:
                return digits[:6]

        # 202608/202609 是跨月價差，要排除
        return ""

    if "-" in s:
        parts = s.split("-")

        # 2026-07 可以接受
        if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
            if len(digits) >= 6:
                return digits[:6]

        return ""

    if len(digits) == 6:
        return digits

    return ""


def _format_contract_date(value: Any) -> str:
    s = _normalize_contract_date(value)

    if len(s) == 6:
        return f"{s[:4]}/{s[4:6]}"

    return str(value or "--")


def _normalize_trade_date(value: Any) -> str:
    s = str(value or "").strip()

    if not s:
        return ""

    try:
        if len(s) >= 10 and "-" in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")

        digits = "".join(ch for ch in s if ch.isdigit())

        if len(digits) >= 8:
            return datetime.strptime(digits[:8], "%Y%m%d").strftime("%Y-%m-%d")

    except Exception:
        return s

    return s


def _get_session_value(row: dict) -> str:
    for key in [
        "trading_session",
        "TradingSession",
        "session",
        "tradingSession",
        "交易時段",
    ]:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)

    return ""


def _is_afterhours_session(value: Any) -> bool:
    text = str(value or "").strip().lower()

    return (
        "after" in text
        or "night" in text
        or "盤後" in text
        or "夜盤" in text
    )


def _is_regular_session(value: Any) -> bool:
    text = str(value or "").strip().lower()

    return (
        "regular" in text
        or "day" in text
        or "一般" in text
        or "日盤" in text
        or "position" in text
    )

def _normalize_session_mode(session_mode: str) -> str:
    mode = str(session_mode or "day").strip().lower()

    if mode in {"all", "full", "all_session", "全盤"}:
        return "all"

    return "day"

def _display_session(value: Any) -> str:
    text = str(value or "").strip()

    if text.lower() in {"all", "full", "all_session"} or text == "全盤":
        return "全盤"

    if _is_afterhours_session(value):
        return "盤後"

    if _is_regular_session(value):
        return "日盤"

    return text or "--"
    
def _current_tw_futures_session_preference() -> str:
    """
    依台灣時間決定目前應該優先顯示日盤或盤後。

    - 08:45 ~ 13:45：優先日盤
    - 15:00 之後：優先盤後
    - 其他時間：優先日盤
    """
    now_tpe = datetime.now(ZoneInfo("Asia/Taipei"))
    t = now_tpe.time()

    if time(8, 45) <= t <= time(13, 45):
        return "regular"

    if t >= time(15, 0):
        return "afterhours"

    return "regular"


def _row_price(row: dict) -> float:
    for key in [
        "close",
        "Close",
        "close_price",
        "last_price",
        "成交價",
        "最後成交價",
        "收盤價",
        "settlement_price",
        "SettlementPrice",
    ]:
        price = _safe_float(row.get(key))

        if price > 0:
            return price

    return 0.0


def _row_change(row: dict) -> float:
    for key in [
        "spread",
        "change",
        "price_change",
    ]:
        value = _safe_float(row.get(key), default=0.0)

        if value != 0:
            return value

    return 0.0


def _row_change_pct(row: dict) -> float:
    for key in [
        "spread_per",
        "change_percent",
        "price_change_pct",
    ]:
        value = _safe_float(row.get(key), default=0.0)

        if value != 0:
            return value

    return 0.0


def _is_valid_trade_row(row: dict) -> bool:
    """
    只排除：
    - 跨月價差，例如 202608/202609
    - contract_date 空白 / 全月份
    - 價格為 0

    不排除 position。
    因為 FinMind 的 position 通常會帶 close / settlement_price / open_interest。
    """
    contract = _normalize_contract_date(row.get("contract_date"))

    if not contract:
        return False

    price = _row_price(row)

    if price <= 0:
        return False

    return True

def _get_open_price(row: dict) -> float:
    return _safe_float(
        row.get("open")
        or row.get("Open")
        or row.get("open_price")
        or row.get("開盤價")
        or _row_price(row)
    )


def _get_high_price(row: dict) -> float:
    return _safe_float(
        row.get("max")
        or row.get("high")
        or row.get("High")
        or row.get("最高價")
        or _row_price(row)
    )


def _get_low_price(row: dict) -> float:
    return _safe_float(
        row.get("min")
        or row.get("low")
        or row.get("Low")
        or row.get("最低價")
        or _row_price(row)
    )


def _combine_all_session_rows(rows: list[dict]) -> dict | None:
    """
    將同一交易日、同一契約的日盤 + 盤後合併成「全盤」。

    全盤定義：
    - 15:00 ~ 05:00
    - 08:45 ~ 13:45

    合併邏輯：
    - open：優先盤後 open，沒有盤後才用日盤 open
    - high：日盤 / 盤後最高價取 max
    - low：日盤 / 盤後最低價取 min
    - close：優先日盤 close，沒有日盤才用盤後 close
    - volume：日盤 + 盤後
    - open_interest：取最後一筆可用資料
    """
    valid = [dict(r) for r in rows if _is_valid_trade_row(r)]

    if not valid:
        return None

    after_rows = [
        r for r in valid
        if _is_afterhours_session(_get_session_value(r))
    ]

    regular_rows = [
        r for r in valid
        if _is_regular_session(_get_session_value(r))
    ]

    # open：全盤優先用盤後，沒有盤後才用日盤
    open_source = after_rows[0] if after_rows else valid[0]

    # close：完整全盤通常日盤比較新；若只有盤後，就用盤後
    close_source = regular_rows[-1] if regular_rows else valid[-1]

    prices_high = [_get_high_price(r) for r in valid if _get_high_price(r) > 0]
    prices_low = [_get_low_price(r) for r in valid if _get_low_price(r) > 0]

    combined = dict(close_source)

    combined["open"] = _get_open_price(open_source)
    combined["max"] = max(prices_high) if prices_high else _row_price(close_source)
    combined["min"] = min(prices_low) if prices_low else _row_price(close_source)
    combined["close"] = _row_price(close_source)
    combined["volume"] = sum(_safe_int(r.get("volume")) for r in valid)

    combined["trading_session"] = "all"
    combined["_trade_date_norm"] = valid[0].get("_trade_date_norm") or _normalize_trade_date(valid[0].get("date"))
    combined["_contract_norm"] = valid[0].get("_contract_norm") or _normalize_contract_date(valid[0].get("contract_date"))

    # open_interest / settlement_price 取最後一筆可用
    for key in ["open_interest", "settlement_price"]:
        for r in reversed(valid):
            if r.get(key) not in (None, "", 0, "0"):
                combined[key] = r.get(key)
                break

    return combined

def _pick_near_month_prefer_afterhours(
    rows: list[dict],
    session_mode: str = "day",
) -> dict | None:
    """
    選資料規則：

    day：
    - 只選日盤 / position

    all：
    - 同一交易日、同一近月契約，把日盤 + 盤後合併成全盤
    """
    session_mode = _normalize_session_mode(session_mode)

    valid_rows: list[dict] = []

    for r in rows:
        if not _is_valid_trade_row(r):
            continue

        trade_date = _normalize_trade_date(r.get("date"))
        contract = _normalize_contract_date(r.get("contract_date"))

        if not trade_date or not contract:
            continue

        item = dict(r)
        item["_trade_date_norm"] = trade_date
        item["_contract_norm"] = contract

        valid_rows.append(item)

    _debug("valid_rows_count =", len(valid_rows), "session_mode =", session_mode)

    if not valid_rows:
        return None

    latest_date = max(r["_trade_date_norm"] for r in valid_rows)

    latest_rows = [
        r for r in valid_rows
        if r["_trade_date_norm"] == latest_date
    ]

    if not latest_rows:
        return None

    near_contract = min(r["_contract_norm"] for r in latest_rows)

    near_rows = [
        r for r in latest_rows
        if r["_contract_norm"] == near_contract
    ]

    if not near_rows:
        return None

    if session_mode == "all":
        combined = _combine_all_session_rows(near_rows)

        _debug(
            "choose all-session",
            "latest_date =",
            latest_date,
            "near_contract =",
            near_contract,
            "rows =",
            len(near_rows),
            "combined =",
            combined,
        )

        return combined

    # day mode：只取日盤 / position
    regular_rows = [
        r for r in near_rows
        if _is_regular_session(_get_session_value(r))
    ]

    if regular_rows:
        _debug(
            "choose day-session",
            "latest_date =",
            latest_date,
            "near_contract =",
            near_contract,
            "regular_rows =",
            len(regular_rows),
        )
        return regular_rows[-1]

    # fallback：如果真的沒有日盤，才用近月最後一筆
    _debug(
        "day-session fallback",
        "latest_date =",
        latest_date,
        "near_contract =",
        near_contract,
        "near_rows =",
        len(near_rows),
    )

    return near_rows[-1]

def _prepare_futures_kline_rows(
    rows: list[dict],
    selected_row: dict,
    session_mode: str = "day",
) -> list[dict]:
    """
    準備股票期貨 K 線資料。

    day：
    - 只畫日盤 / position

    all：
    - 每個交易日把日盤 + 盤後合併成一根全盤 K
    """
    session_mode = _normalize_session_mode(session_mode)

    contract = selected_row.get("_contract_norm") or _normalize_contract_date(
        selected_row.get("contract_date")
    )

    if not contract:
        return []

    same_contract_rows: list[dict] = []

    for r in rows:
        r_contract = _normalize_contract_date(r.get("contract_date"))

        if r_contract != contract:
            continue

        if not _is_valid_trade_row(r):
            continue

        item = dict(r)
        item["_trade_date_norm"] = _normalize_trade_date(r.get("date"))
        item["_contract_norm"] = r_contract

        if not item["_trade_date_norm"]:
            continue

        same_contract_rows.append(item)

    if not same_contract_rows:
        return []

    # 全盤：依交易日合併日盤 + 盤後
    if session_mode == "all":
        grouped: dict[str, list[dict]] = {}

        for r in same_contract_rows:
            grouped.setdefault(r["_trade_date_norm"], []).append(r)

        combined_rows: list[dict] = []

        for trade_date in sorted(grouped):
            combined = _combine_all_session_rows(grouped[trade_date])

            if combined:
                combined_rows.append(combined)

        return combined_rows[-30:]

    # 日盤：只取 regular / position
    regular_rows = [
        r for r in same_contract_rows
        if _is_regular_session(_get_session_value(r))
    ]

    if regular_rows:
        same_contract_rows = regular_rows

    same_contract_rows = sorted(
        same_contract_rows,
        key=lambda r: r["_trade_date_norm"],
    )

    by_date: dict[str, dict] = {}

    for r in same_contract_rows:
        by_date[r["_trade_date_norm"]] = r

    return list(by_date.values())[-30:]
    
def _generate_futures_kline_chart(
    rows: list[dict],
    futures_id: str,
    futures_name: str,
    contract_date: str,
    session: str,
    current_price: float = 0.0,
    quote_time: str = "",
) -> str:
    """
    產生股票期貨 K 線圖。
    """
    if not rows:
        return ""

    chart_rows = []

    for r in rows:
        date = _normalize_trade_date(r.get("date"))
        close = _row_price(r)

        if not date or close <= 0:
            continue

        open_price = _safe_float(r.get("open") or r.get("Open") or close)
        high_price = _safe_float(r.get("max") or r.get("high") or r.get("High") or close)
        low_price = _safe_float(r.get("min") or r.get("low") or r.get("Low") or close)
        volume = _safe_int(r.get("volume"))

        if open_price <= 0:
            open_price = close

        if high_price <= 0:
            high_price = max(open_price, close)

        if low_price <= 0:
            low_price = min(open_price, close)

        chart_rows.append(
            {
                "date": date,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close,
                "volume": volume,
            }
        )

    if not chart_rows:
        return ""

    df = pd.DataFrame(chart_rows)

    fig = plt.figure(figsize=(7, 5.5), dpi=120, facecolor="white")
    gs = gridspec.GridSpec(2, 1, height_ratios=[3, 1], hspace=0.05)

    ax_k = fig.add_subplot(gs[0])
    ax_v = fig.add_subplot(gs[1], sharex=ax_k)

    ax_k.set_facecolor("#F8F9FA")
    ax_v.set_facecolor("#F8F9FA")

    x = list(range(len(df)))
    width = 0.58

    for i in range(len(df)):
        row = df.iloc[i]

        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])

        color = "#FF3B30" if c >= o else "#34C759"

        ax_k.vlines(i, l, h, linewidth=1, color=color)

        lower = min(o, c)
        height = abs(c - o)

        if height <= 0:
            height = 0.01

        ax_k.bar(
            i,
            height,
            bottom=lower,
            width=width,
            color=color,
            align="center",
        )

        ax_v.bar(
            i,
            int(row["volume"]),
            width=width,
            color=color,
        )

    if session == "全盤":
        session_label = "All-session"
    elif session == "盤後":
        session_label = "After-hours"
    else:
        session_label = "Day"

    ax_k.set_title(
        f"{futures_id} {contract_date} {session_label} Futures K",
        fontsize=13,
        fontweight="bold",
    )

    ax_k.grid(True, linestyle=":", alpha=0.45)
    ax_v.grid(True, linestyle=":", alpha=0.45)
    ax_v.set_ylabel("Volume", fontsize=8)
    
    # 即時現價線
    if current_price and current_price > 0:
        ax_k.axhline(
            current_price,
            linestyle="--",
            linewidth=1.2,
            alpha=0.8,
        )

        ax_k.text(
            0.99,
            current_price,
            f" 現價 {current_price:g}",
            transform=ax_k.get_yaxis_transform(),
            ha="right",
            va="bottom",
            fontsize=9,
            fontweight="bold",
        )
    
    labels = [str(d)[5:] for d in df["date"].tolist()]
    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels([labels[i] for i in ticks], rotation=0, fontsize=8)

    plt.setp(ax_k.get_xticklabels(), visible=False)

    fig.tight_layout()

    return publish_figure(fig, f"{futures_id}_futures_kline")


def _get_spot_price(stock_id: str) -> float:
    """
    現貨價格用 Yahoo fast_info。
    不使用 ticker.info，避免 timeout。
    """
    sid = _clean_stock_id(stock_id)
    now = time_module.time()

    cached = _SPOT_CACHE.get(sid)

    if cached:
        ts, price = cached
        if now - ts <= SPOT_CACHE_TTL_SECONDS:
            return price

    for symbol in [f"{sid}.TW", f"{sid}.TWO"]:
        try:
            ticker = yf.Ticker(symbol)

            try:
                fast_info = ticker.fast_info

                for key in [
                    "last_price",
                    "lastPrice",
                    "regularMarketPrice",
                    "currentPrice",
                ]:
                    try:
                        value = fast_info.get(key)
                    except Exception:
                        value = None

                    price = _safe_float(value)

                    if price > 0:
                        _SPOT_CACHE[sid] = (now, price)
                        return price

            except Exception:
                pass

            try:
                hist = ticker.history(period="5d", interval="1d")

                if hist is not None and not hist.empty:
                    price = _safe_float(hist["Close"].dropna().iloc[-1])

                    if price > 0:
                        _SPOT_CACHE[sid] = (now, price)
                        return price

            except Exception:
                pass

        except Exception:
            continue

    return 0.0


def _calc_change_from_kline_rows(kline_rows: list[dict], future_price: float) -> tuple[float, float]:
    """
    如果 FinMind row 沒有 spread / spread_per，
    就用同契約同時段前一筆 close 算漲跌。
    """
    if not kline_rows or len(kline_rows) < 2 or future_price <= 0:
        return 0.0, 0.0

    prev_price = _row_price(kline_rows[-2])

    if prev_price <= 0:
        return 0.0, 0.0

    change = future_price - prev_price
    change_pct = change / prev_price * 100

    return change, change_pct


def get_stock_futures_snapshot(
    stock_id: str,
    stock_name: str,
    session_mode: str = "day",
) -> FuturesSnapshot:
    """
    股票期貨：

    - Supabase stock_futures_map 為主
    - STOCK_FUTURES_MAP 為 fallback
    - 只抓標準股票期貨
    - 只抓近月
    - 日盤時間優先日盤
    - 盤後時間優先盤後
    """
    sid = _clean_stock_id(stock_id)
    session_mode = _normalize_session_mode(session_mode)
    futures_name, candidates, source = _resolve_stock_futures_candidates(sid, stock_name)

    _debug(
        "start",
        sid,
        stock_name,
        "source =",
        source,
        "name =",
        futures_name,
        "candidates =",
        candidates,
    )

    if not candidates:
        return FuturesSnapshot(
            available=False,
            message="這檔股票目前查不到標準股票期貨代號對照，請先同步期交所股票期貨對照表。",
            stock_id=sid,
            stock_name=stock_name,
        )

    selected_row: dict | None = None
    selected_futures_id = ""
    selected_rows: list[dict] = []

    for futures_id in candidates:
        rows = _request_finmind_futures_daily(futures_id)

        _debug("candidate =", futures_id, "rows_count =", len(rows))

        if rows:
            _debug("candidate sample last =", rows[-1])

        if not rows:
            continue

        row = _pick_near_month_prefer_afterhours(
            rows,
            session_mode=session_mode,
        )

        _debug("selected row for", futures_id, "=", row)

        if row:
            selected_row = row
            selected_futures_id = futures_id
            selected_rows = rows
            break

    if not selected_row:
        return FuturesSnapshot(
            available=False,
            message="查無近月股票期貨資料，可能是 FinMind 尚未更新或期貨代號需調整。",
            stock_id=sid,
            stock_name=stock_name,
            futures_name=futures_name,
        )

    display_contract = _format_contract_date(selected_row.get("contract_date"))
    
    if session_mode == "all":
        display_session = "全盤"
    else:
        display_session = _display_session(_get_session_value(selected_row))
    
    kline_rows = _prepare_futures_kline_rows(
        selected_rows,
        selected_row,
        session_mode=session_mode,
    )
    
    future_price = _row_price(selected_row)
    future_change = _row_change(selected_row)
    future_change_pct = _row_change_pct(selected_row)

    future_volume = _safe_int(selected_row.get("volume"))
    quote_source = "日成交"
    quote_time = ""
    
    # =========================
    # 優先使用即時期貨報價
    # =========================
    snapshot_request_id, snapshot_row = _get_realtime_snapshot_for_candidates(
        candidates=candidates,
        preferred_id=selected_futures_id,
    )

    if snapshot_row:
        rt_price = _safe_float(snapshot_row.get("close"))

        if rt_price > 0:
            future_price = rt_price
            future_change = _safe_float(snapshot_row.get("change_price"), future_change)
            future_change_pct = _safe_float(snapshot_row.get("change_rate"), future_change_pct)

            future_volume = _safe_int(
                snapshot_row.get("total_volume")
                or snapshot_row.get("volume"),
                future_volume,
            )

            quote_source = "即時報價"
            quote_time = str(snapshot_row.get("date") or "")

            _debug(
                "use realtime snapshot",
                "selected_futures_id =",
                selected_futures_id,
                "snapshot_futures_id =",
                snapshot_row.get("futures_id"),
                "price =",
                future_price,
                "change =",
                future_change,
                "change_pct =",
                future_change_pct,
                "time =",
                quote_time,
            )

            if quote_source != "即時報價" and future_change == 0 and future_change_pct == 0:
                calc_change, calc_change_pct = _calc_change_from_kline_rows(
                    kline_rows,
                    future_price,
                )

                if calc_change != 0:
                    future_change = calc_change
                    future_change_pct = calc_change_pct

            chart_url = _generate_futures_kline_chart(
                rows=kline_rows,
                futures_id=selected_futures_id,
                futures_name=futures_name,
                contract_date=display_contract,
                session=display_session,
                current_price=future_price if quote_source == "即時報價" else 0.0,
                quote_time=quote_time,
            )

            spot_price = _get_spot_price(sid)

            basis = future_price - spot_price if future_price and spot_price else 0.0
            basis_pct = (basis / spot_price * 100) if spot_price else 0.0

            _debug(
                "result",
                "selected_futures_id =",
                selected_futures_id,
                "contract =",
                display_contract,
                "session =",
                display_session,
                "future_price =",
                future_price,
                "spot_price =",
                spot_price,
                "basis =",
                basis,
                "quote_source =",
                quote_source,
                "quote_time =",
                quote_time,
                "chart_url =",
                chart_url,
            )

            return FuturesSnapshot(
                available=True,
                message="ok",
                stock_id=sid,
                stock_name=stock_name,
                futures_id=str(selected_row.get("futures_id") or selected_futures_id),
                futures_name=futures_name or f"{stock_name}期貨",
                contract_date=display_contract,
                trade_date=_normalize_trade_date(selected_row.get("date")),
                trading_session=display_session,
                chart_url=chart_url,
                future_price=future_price,
                future_change=future_change,
                future_change_pct=future_change_pct,
                spot_price=spot_price,
                basis=basis,
                basis_pct=basis_pct,
                volume=future_volume,
                open_interest=_safe_int(selected_row.get("open_interest")),
                settlement_price=_safe_float(selected_row.get("settlement_price")),
                quote_source=quote_source,
                quote_time=quote_time,
            )
