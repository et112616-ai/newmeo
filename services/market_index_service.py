from __future__ import annotations

# market_index_service_full_v3.py
# 完整版：Shioaji 即時大盤 + K線圖快取 + yfinance/Yahoo/TWSE fallback + stale snapshot fallback + get_api timing

import os
import time
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
from services.market_turnover_service import (
    apply_twse_turnover_to_market_df,
    get_twse_turnover_map,
)
from matplotlib import font_manager
from matplotlib.font_manager import FontProperties
import pandas as pd
import requests
from utils.chart_style import (
    AXIS_TICK_FONTSIZE,
    CHART_BACKGROUND,
    DEFAULT_CANDLE_WIDTH,
    FIGURE_SIZES,
    HIGH_LOW_FONTSIZE,
    annotate_visible_high_low,
    apply_axis_style,
    configure_chart_font,
    hide_chart_spines,
    set_price_axis_to_visible_high_low,
)

try:
    import yfinance as yf
except Exception:
    yf = None

try:
    from services.sinopac_quote_service import get_api
except Exception:
    def get_api():
        return None

try:
    from services.upload_service import publish_figure
except Exception:
    def publish_figure(fig, name: str) -> str:
        return ""


MARKET_INDEX_CONTRACT_FIX_VERSION = "2026-07-16-v2-IX0001-YAHOO-SNAPSHOT-FALLBACK"
MARKET_INDEX_SNAPSHOT_FIX_VERSION = "2026-07-16-v2-IND-ZERO-SNAPSHOT-FALLBACK"

# =========================
# Cache settings
# =========================
MARKET_INDEX_CACHE_TTL_SECONDS = int(os.getenv("MARKET_INDEX_CACHE_TTL_SECONDS", "5"))
MARKET_INDEX_CHART_CACHE_TTL_SECONDS = int(os.getenv("MARKET_INDEX_CHART_CACHE_TTL_SECONDS", "900"))

# 歷史 OHLC 與「已補成交金額」資料不需要跟 5 秒即時 snapshot 一起重抓。
# 預設快取 6 小時；盤中最新一根仍由 Shioaji snapshot 覆蓋。
MARKET_INDEX_HISTORY_CACHE_TTL_SECONDS = int(
    os.getenv("MARKET_INDEX_HISTORY_CACHE_TTL_SECONDS", "21600")
)
MARKET_INDEX_TURNOVER_CACHE_TTL_SECONDS = int(
    os.getenv("MARKET_INDEX_TURNOVER_CACHE_TTL_SECONDS", "21600")
)

MARKET_INDEX_YAHOO_TIMEOUT_SECONDS = float(
    os.getenv("MARKET_INDEX_YAHOO_TIMEOUT_SECONDS", "4")
)
MARKET_INDEX_YFINANCE_TIMEOUT_SECONDS = float(
    os.getenv("MARKET_INDEX_YFINANCE_TIMEOUT_SECONDS", "4")
)

# direct Yahoo 通常比 yfinance 少一層包裝，冷啟動時優先使用。
MARKET_INDEX_HISTORY_SOURCE_ORDER = [
    item.strip().lower()
    for item in os.getenv(
        "MARKET_INDEX_HISTORY_SOURCE_ORDER",
        "yahoo_direct,yfinance,twse",
    ).split(",")
    if item.strip()
]

_MARKET_INDEX_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MARKET_INDEX_CHART_CACHE: dict[str, tuple[float, str]] = {}
_MARKET_INDEX_HISTORY_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_MARKET_INDEX_TURNOVER_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}

# 避免 Gunicorn threads 同時產生同一張大盤圖。
_MARKET_INDEX_CHART_LOCK = threading.Lock()

_FONT_SETUP_DONE = False
_FONT_SETUP_LOCK = threading.Lock()
_HTTP_SESSION = requests.Session()


@dataclass
class MarketIndexSnapshot:
    available: bool
    message: str

    index_id: str = "TAIEX"
    index_name: str = "加權指數"

    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0

    change: float = 0.0
    change_pct: float = 0.0

    volume: int = 0
    total_volume: int = 0
    amount: int = 0
    total_amount: int = 0

    quote_time: str = ""
    quote_source: str = "永豐即時"

    chart_url: str = ""


def _debug(*args):
    print("DEBUG market_index |", *args, flush=True)

def _fmt_ma_value(value) -> str:
    try:
        num = float(value)

        if num == 0:
            return "--"

        return f"{num:,.2f}"

    except Exception:
        return "--"

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        if isinstance(value, Decimal):
            return float(value)

        text = str(value).replace(",", "").replace("%", "").strip()

        if not text or text in {"--", "-"}:
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

            if key in {"tick_type", "change_type"}:
                result[key] = str(value)
            else:
                result[key] = value

        except Exception:
            continue

    return result


def _normalize_ts(value: Any) -> str:
    if value is None or value == "":
        return ""

    try:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")

        ts = pd.to_datetime(value, errors="coerce")

        if pd.notna(ts):
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert("Asia/Taipei")

            return ts.strftime("%Y-%m-%d %H:%M:%S")

    except Exception:
        pass

    return str(value)


def _first_positive(mapping: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = _safe_float(mapping.get(key), 0.0)
        if value > 0:
            return value
    return 0.0


def _epoch_seconds_to_taipei_text(value: Any) -> str:
    try:
        number = float(value)
        if number <= 0:
            return ""

        # Yahoo 使用秒；少數來源可能傳毫秒、微秒或奈秒。
        if number >= 1e18:
            unit = "ns"
        elif number >= 1e15:
            unit = "us"
        elif number >= 1e12:
            unit = "ms"
        else:
            unit = "s"

        ts = pd.to_datetime(number, unit=unit, utc=True, errors="coerce")
        if pd.isna(ts):
            return ""
        return ts.tz_convert("Asia/Taipei").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _fetch_taiex_yahoo_intraday_snapshot() -> dict[str, Any]:
    """
    Yahoo chart 1m fallback for TAIEX.

    Shioaji 的 Snapshot 官方文件主要列出股票、期貨與選擇權；
    某些版本對 IND contract 會回傳有時間但 OHLC 全為 0 的 placeholder。
    此函式只在 Shioaji 指數 snapshot 無有效 close 時使用。
    """
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
    params = {
        "range": "1d",
        "interval": "1m",
        "includePrePost": "false",
        "events": "history",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    t0 = time.perf_counter()
    resp = _HTTP_SESSION.get(
        url,
        params=params,
        headers=headers,
        timeout=MARKET_INDEX_YAHOO_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    payload = resp.json()

    result = ((payload.get("chart") or {}).get("result") or [])
    if not result:
        return {}

    item = result[0]
    meta = item.get("meta") or {}
    timestamps = item.get("timestamp") or []
    quote = (((item.get("indicators") or {}).get("quote") or [{}])[0])

    close_list = quote.get("close") or []
    open_list = quote.get("open") or []
    high_list = quote.get("high") or []
    low_list = quote.get("low") or []

    valid_positions = [
        i for i, value in enumerate(close_list)
        if _safe_float(value, 0.0) > 0
    ]
    if not valid_positions:
        return {}

    last_pos = valid_positions[-1]
    latest_close = _safe_float(close_list[last_pos], 0.0)

    regular_price = _safe_float(meta.get("regularMarketPrice"), 0.0)
    if regular_price > 0:
        latest_close = regular_price

    valid_open = [_safe_float(v, 0.0) for v in open_list if _safe_float(v, 0.0) > 0]
    valid_high = [_safe_float(v, 0.0) for v in high_list if _safe_float(v, 0.0) > 0]
    valid_low = [_safe_float(v, 0.0) for v in low_list if _safe_float(v, 0.0) > 0]

    open_price = _safe_float(meta.get("regularMarketOpen"), 0.0) or (valid_open[0] if valid_open else latest_close)
    high_price = _safe_float(meta.get("regularMarketDayHigh"), 0.0) or (max(valid_high) if valid_high else latest_close)
    low_price = _safe_float(meta.get("regularMarketDayLow"), 0.0) or (min(valid_low) if valid_low else latest_close)

    previous_close = (
        _safe_float(meta.get("previousClose"), 0.0)
        or _safe_float(meta.get("chartPreviousClose"), 0.0)
    )
    change = latest_close - previous_close if previous_close > 0 else 0.0
    change_pct = change / previous_close * 100 if previous_close > 0 else 0.0

    quote_time = _epoch_seconds_to_taipei_text(meta.get("regularMarketTime"))
    if not quote_time and last_pos < len(timestamps):
        quote_time = _epoch_seconds_to_taipei_text(timestamps[last_pos])

    _debug(
        "yahoo intraday snapshot",
        "| version =", MARKET_INDEX_SNAPSHOT_FIX_VERSION,
        "| close =", latest_close,
        "| previous_close =", previous_close,
        "| change =", change,
        "| change_pct =", change_pct,
        "| time =", quote_time,
        "| sec =", round(time.perf_counter() - t0, 3),
    )

    return {
        "open_price": open_price,
        "high_price": max(high_price, open_price, latest_close),
        "low_price": min(low_price, open_price, latest_close),
        "close_price": latest_close,
        "change": change,
        "change_pct": change_pct,
        "quote_time": quote_time,
        "quote_source": "Yahoo 指數行情",
    }


def _fetch_taiex_daily_snapshot_fallback() -> dict[str, Any]:
    """最後備援：由 Yahoo 日 K 最新兩筆組成圖卡數字。"""
    try:
        df = _fetch_taiex_history_yahoo_direct()
    except Exception as exc:
        _debug("daily snapshot fallback failed", repr(exc))
        return {}

    if df is None or df.empty:
        return {}

    work = df.dropna(subset=["Close"]).copy()
    if work.empty:
        return {}

    latest = work.iloc[-1]
    close_price = _safe_float(latest.get("Close"), 0.0)
    if close_price <= 0:
        return {}

    previous_close = 0.0
    if len(work) >= 2:
        previous_close = _safe_float(work.iloc[-2].get("Close"), 0.0)

    change = close_price - previous_close if previous_close > 0 else 0.0
    change_pct = change / previous_close * 100 if previous_close > 0 else 0.0
    latest_index = pd.to_datetime(work.index[-1], errors="coerce")
    quote_time = latest_index.strftime("%Y-%m-%d") if pd.notna(latest_index) else ""

    return {
        "open_price": _safe_float(latest.get("Open"), close_price),
        "high_price": _safe_float(latest.get("High"), close_price),
        "low_price": _safe_float(latest.get("Low"), close_price),
        "close_price": close_price,
        "change": change,
        "change_pct": change_pct,
        "quote_time": quote_time,
        "quote_source": "Yahoo 日線備援",
    }


def _resolve_latest_turnover_yi(quote_time: str, raw_amount: float = 0.0) -> float:
    if raw_amount > 0:
        return raw_amount / 100_000_000 if raw_amount >= 100_000_000 else raw_amount

    date_key = ""
    try:
        ts = pd.to_datetime(quote_time, errors="coerce")
        if pd.notna(ts):
            date_key = ts.strftime("%Y-%m-%d")
    except Exception:
        pass

    try:
        turnover_map = get_twse_turnover_map([date_key] if date_key else None)
        if turnover_map:
            if date_key and _safe_float(turnover_map.get(date_key), 0.0) > 0:
                return _safe_float(turnover_map.get(date_key), 0.0)
            return _safe_float(sorted(turnover_map.items())[-1][1], 0.0)
    except Exception as exc:
        _debug("turnover snapshot fallback failed", repr(exc))

    return 0.0


def _setup_chinese_font() -> None:
    """
    中文字型只初始化一次。

    若專案同時有 Regular / Bold，兩個都註冊，避免：
    findfont: Failed to find font weight bold
    """
    global _FONT_SETUP_DONE

    if _FONT_SETUP_DONE:
        return

    with _FONT_SETUP_LOCK:
        if _FONT_SETUP_DONE:
            return

        try:
            configure_chart_font(Path(__file__).resolve().parents[1])
        except Exception as exc:
            _debug("font setup failed", exc)

        _FONT_SETUP_DONE = True


DEFAULT_AXIS_TICK_FONTSIZE = AXIS_TICK_FONTSIZE


def _add_quarter_grid(ax, color: str = "#AEB6BF", alpha: float = 0.26) -> None:
    try:
        y_min, y_max = ax.get_ylim()

        if pd.isna(y_min) or pd.isna(y_max) or y_max <= y_min:
            return

        span = y_max - y_min

        if span <= 0:
            return

        for ratio in (0.25, 0.50, 0.75):
            level = y_min + span * ratio
            ax.axhline(level, color=color, linewidth=0.8, linestyle="--", alpha=alpha, zorder=0)

    except Exception:
        pass


def _apply_axis_style(ax, x_labelsize: int = DEFAULT_AXIS_TICK_FONTSIZE, y_labelsize: int = DEFAULT_AXIS_TICK_FONTSIZE) -> None:
    apply_axis_style(ax, x_labelsize=x_labelsize, y_labelsize=y_labelsize)


def _hide_top_right_spines(ax) -> None:
    hide_chart_spines(ax)


def _get_taiex_contract(api):
    """
    取得加權指數 contract，兼容 Shioaji 新舊版本。

    Shioaji 1.7+：
        api.contracts.get("IX0001")

    舊版：
        api.Contracts.Indexs.TSE["001"]
        api.Contracts.Indexs.TSE.TSE001
    """
    if api is None:
        return None

    attempts: list[str] = []

    # Shioaji 1.7+：指數改用交易所標準代碼 IX0001。
    try:
        contracts_api = getattr(api, "contracts", None)
        get_contract = getattr(contracts_api, "get", None)

        if callable(get_contract):
            for code in ("IX0001", "TSE001", "001"):
                attempts.append(f"api.contracts.get({code})")

                try:
                    contract = get_contract(code)
                except TypeError:
                    # 少數版本可能要求 security_type；先讓後續 legacy fallback 接手。
                    contract = None
                except Exception as exc:
                    _debug(
                        "contract lookup failed",
                        "| version =", MARKET_INDEX_CONTRACT_FIX_VERSION,
                        "| source = api.contracts.get",
                        "| code =", code,
                        "| error =", repr(exc),
                    )
                    contract = None

                if contract is not None:
                    _debug(
                        "contract resolved",
                        "| version =", MARKET_INDEX_CONTRACT_FIX_VERSION,
                        "| source = api.contracts.get",
                        "| requested_code =", code,
                        "| actual_code =", getattr(contract, "code", ""),
                    )
                    return contract

    except Exception as exc:
        _debug(
            "new contract api unavailable",
            "| version =", MARKET_INDEX_CONTRACT_FIX_VERSION,
            "| error =", repr(exc),
        )

    # 新版 legacy facade 也可能接受 IX0001。
    legacy_candidates = [
        ("api.Contracts.Indexs.TSE[IX0001]", lambda: api.Contracts.Indexs.TSE["IX0001"]),
        ("api.Contracts.Indexs.TSE.IX0001", lambda: api.Contracts.Indexs.TSE.IX0001),
        ("api.Contracts.Indexs[TSE][IX0001]", lambda: api.Contracts.Indexs["TSE"]["IX0001"]),
        # 舊 Shioaji 版本代碼。
        ("api.Contracts.Indexs.TSE[001]", lambda: api.Contracts.Indexs.TSE["001"]),
        ("api.Contracts.Indexs.TSE.TSE001", lambda: api.Contracts.Indexs.TSE.TSE001),
        ("api.Contracts.Indexs[TSE][001]", lambda: api.Contracts.Indexs["TSE"]["001"]),
    ]

    for source, getter in legacy_candidates:
        attempts.append(source)

        try:
            contract = getter()
        except Exception:
            contract = None

        if contract is not None:
            _debug(
                "contract resolved",
                "| version =", MARKET_INDEX_CONTRACT_FIX_VERSION,
                "| source =", source,
                "| actual_code =", getattr(contract, "code", ""),
            )
            return contract

    _debug(
        "contract unresolved",
        "| version =", MARKET_INDEX_CONTRACT_FIX_VERSION,
        "| attempts =", attempts,
    )
    return None


def _snapshot_from_dict(data: dict[str, Any]) -> MarketIndexSnapshot:
    return MarketIndexSnapshot(
        available=bool(data.get("available")),
        message=str(data.get("message") or ""),

        index_id=str(data.get("index_id") or "TAIEX"),
        index_name=str(data.get("index_name") or "加權指數"),

        open_price=_safe_float(data.get("open_price")),
        high_price=_safe_float(data.get("high_price")),
        low_price=_safe_float(data.get("low_price")),
        close_price=_safe_float(data.get("close_price")),

        change=_safe_float(data.get("change")),
        change_pct=_safe_float(data.get("change_pct")),

        volume=_safe_float(data.get("volume")),
        total_volume=_safe_float(data.get("total_volume")),
        amount=_safe_int(data.get("amount")),
        total_amount=_safe_int(data.get("total_amount")),

        quote_time=str(data.get("quote_time") or ""),
        quote_source=str(data.get("quote_source") or "永豐即時"),

        chart_url=str(data.get("chart_url") or ""),
    )


# =========================
# Public APIs
# =========================
def get_market_index_snapshot(with_chart: bool = True) -> MarketIndexSnapshot:
    """取得加權指數圖卡資料；Shioaji IND snapshot 為 0 時自動改用 Yahoo。"""
    route_t0 = time.perf_counter()
    cache_key = "TAIEX"
    now = time.time()

    cached = _MARKET_INDEX_CACHE.get(cache_key)
    if cached:
        ts, data = cached
        age = now - ts
        if age <= MARKET_INDEX_CACHE_TTL_SECONDS:
            snapshot = _snapshot_from_dict(data)
            if with_chart and not snapshot.chart_url:
                snapshot.chart_url = get_market_index_chart_url(snapshot)
                data["chart_url"] = snapshot.chart_url
                _MARKET_INDEX_CACHE[cache_key] = (ts, data)
            _debug(
                "snapshot cache hit",
                "| version =", MARKET_INDEX_SNAPSHOT_FIX_VERSION,
                "| age_sec =", round(age, 2),
                "| close =", snapshot.close_price,
                "| chart_url =", bool(snapshot.chart_url),
                "| total_sec =", round(time.perf_counter() - route_t0, 3),
            )
            return snapshot

    t_api0 = time.perf_counter()
    api = get_api()
    t_api1 = time.perf_counter()

    contract = None
    t_contract0 = time.perf_counter()
    if api is not None:
        contract = _get_taiex_contract(api)
    t_contract1 = time.perf_counter()

    raw: dict[str, Any] = {}
    shioaji_data: dict[str, Any] = {}
    t_snapshot0 = time.perf_counter()

    if api is not None and contract is not None:
        try:
            snapshots = api.snapshots([contract])
            if snapshots:
                raw = _to_dict(snapshots[0])
                shioaji_close = _first_positive(
                    raw,
                    "close", "last_price", "price", "index", "close_price",
                )
                shioaji_open = _first_positive(raw, "open", "open_price", "day_open")
                shioaji_high = _first_positive(raw, "high", "high_price", "day_high")
                shioaji_low = _first_positive(raw, "low", "low_price", "day_low")
                shioaji_change = _safe_float(
                    raw.get("change_price")
                    if raw.get("change_price") is not None
                    else raw.get("change"),
                    0.0,
                )
                shioaji_change_pct = _safe_float(
                    raw.get("change_rate")
                    if raw.get("change_rate") is not None
                    else raw.get("change_pct"),
                    0.0,
                )
                shioaji_time = _normalize_ts(raw.get("ts") or raw.get("timestamp") or raw.get("time"))

                if shioaji_close > 0:
                    shioaji_data = {
                        "open_price": shioaji_open or shioaji_close,
                        "high_price": shioaji_high or shioaji_close,
                        "low_price": shioaji_low or shioaji_close,
                        "close_price": shioaji_close,
                        "change": shioaji_change,
                        "change_pct": shioaji_change_pct,
                        "quote_time": shioaji_time,
                        "quote_source": "永豐即時",
                    }
                else:
                    _debug(
                        "shioaji IND snapshot zero",
                        "| version =", MARKET_INDEX_SNAPSHOT_FIX_VERSION,
                        "| contract_code =", getattr(contract, "code", ""),
                        "| raw_keys =", sorted(str(k) for k in raw.keys()),
                        "| raw_ts =", raw.get("ts"),
                        "| fallback = yahoo_intraday",
                    )
        except Exception as exc:
            _debug(
                "shioaji index snapshot failed",
                "| version =", MARKET_INDEX_SNAPSHOT_FIX_VERSION,
                "| error =", repr(exc),
                "| fallback = yahoo_intraday",
            )

    t_snapshot1 = time.perf_counter()

    selected = shioaji_data
    if not selected:
        try:
            selected = _fetch_taiex_yahoo_intraday_snapshot()
        except Exception as exc:
            _debug("yahoo intraday snapshot failed", repr(exc))
            selected = {}

    if not selected:
        selected = _fetch_taiex_daily_snapshot_fallback()

    close_price = _safe_float(selected.get("close_price"), 0.0)
    if close_price <= 0:
        stale_snapshot = _get_stale_market_index_snapshot()
        if stale_snapshot is not None and stale_snapshot.close_price > 0:
            return stale_snapshot
        return MarketIndexSnapshot(
            available=False,
            message="目前無法取得有效的加權指數點位。",
        )

    raw_amount = _first_positive(raw, "total_amount", "amount")
    quote_time = str(selected.get("quote_time") or "")
    market_turnover_yi = _resolve_latest_turnover_yi(quote_time, raw_amount)

    data = {
        "available": True,
        "message": "ok",
        "index_id": "TAIEX",
        "index_name": "加權指數",
        "open_price": _safe_float(selected.get("open_price"), close_price),
        "high_price": _safe_float(selected.get("high_price"), close_price),
        "low_price": _safe_float(selected.get("low_price"), close_price),
        "close_price": close_price,
        "change": _safe_float(selected.get("change"), 0.0),
        "change_pct": _safe_float(selected.get("change_pct"), 0.0),
        "volume": market_turnover_yi,
        "total_volume": market_turnover_yi,
        "amount": _safe_int(raw.get("amount")),
        "total_amount": _safe_int(raw.get("total_amount")),
        "quote_time": quote_time,
        "quote_source": str(selected.get("quote_source") or "Yahoo 指數行情"),
        "chart_url": "",
    }

    # 避免 OHLC 因資料源缺欄位而顯示 0。
    data["open_price"] = data["open_price"] or close_price
    data["high_price"] = max(data["high_price"] or close_price, data["open_price"], close_price)
    data["low_price"] = min(
        value for value in [data["low_price"] or close_price, data["open_price"], close_price]
        if value > 0
    )

    _MARKET_INDEX_CACHE[cache_key] = (now, dict(data))
    snapshot = _snapshot_from_dict(data)

    t_chart0 = time.perf_counter()
    if with_chart:
        data["chart_url"] = get_market_index_chart_url(snapshot)
        snapshot.chart_url = data["chart_url"]
        _MARKET_INDEX_CACHE[cache_key] = (now, dict(data))
    t_chart1 = time.perf_counter()

    _debug(
        "snapshot",
        "| version =", MARKET_INDEX_SNAPSHOT_FIX_VERSION,
        "| source =", data["quote_source"],
        "| close =", data["close_price"],
        "| change =", data["change"],
        "| change_pct =", data["change_pct"],
        "| turnover_yi =", data["total_volume"],
        "| time =", data["quote_time"],
        "| chart_url =", bool(data["chart_url"]),
        "| get_api_sec =", round(t_api1 - t_api0, 3),
        "| contract_sec =", round(t_contract1 - t_contract0, 3),
        "| shioaji_sec =", round(t_snapshot1 - t_snapshot0, 3),
        "| chart_sec =", round(t_chart1 - t_chart0, 3),
        "| total_sec =", round(time.perf_counter() - route_t0, 3),
    )

    return snapshot


def _get_stale_market_index_snapshot() -> MarketIndexSnapshot | None:
    """
    Shioaji 暫時失敗時，若記憶體內還有舊的大盤資料，就先回舊資料。
    這可以避免冷啟動或短暫 API 問題時整張卡片出不來。
    """
    cached = _MARKET_INDEX_CACHE.get("TAIEX")

    if not cached:
        return None

    _, data = cached

    if not data:
        return None

    snapshot = _snapshot_from_dict(data)

    if not snapshot.available:
        return None

    if not snapshot.chart_url:
        snapshot.chart_url = get_market_index_chart_url(snapshot)

    return snapshot


def get_market_index_chart_url(snapshot: MarketIndexSnapshot | None = None) -> str:
    """
    產生加權指數日 K 圖。

    效能重點：
    1. 圖片快取 15 分鐘。
    2. 歷史 OHLC 與成交金額資料各自快取 6 小時。
    3. Yahoo direct 優先，避免 yfinance 冷啟動等滿 timeout。
    4. 使用 lock，避免多個 LINE 查詢同時重畫同一張圖。
    """
    t0 = time.perf_counter()

    cache_key = "TAIEX:D:MA"
    now = time.time()

    cached = _MARKET_INDEX_CHART_CACHE.get(cache_key)
    stale_url = ""

    if cached:
        ts, url = cached
        age = now - ts
        stale_url = url or ""

        if url and age <= MARKET_INDEX_CHART_CACHE_TTL_SECONDS:
            print(
                "DEBUG market_index chart timing",
                "| chart_cache_hit = True",
                "| age_sec =",
                round(age, 1),
                "| ttl_sec =",
                MARKET_INDEX_CHART_CACHE_TTL_SECONDS,
                "| total_sec =",
                round(time.perf_counter() - t0, 3),
                flush=True,
            )
            return url

    lock_t0 = time.perf_counter()

    with _MARKET_INDEX_CHART_LOCK:
        lock_wait_sec = time.perf_counter() - lock_t0

        # 等 lock 時，另一個 thread 可能已經畫完，再檢查一次。
        cached = _MARKET_INDEX_CHART_CACHE.get(cache_key)

        if cached:
            ts, url = cached
            age = time.time() - ts
            stale_url = url or stale_url

            if url and age <= MARKET_INDEX_CHART_CACHE_TTL_SECONDS:
                print(
                    "DEBUG market_index chart timing",
                    "| chart_cache_hit_after_lock = True",
                    "| lock_wait_sec =",
                    round(lock_wait_sec, 3),
                    "| total_sec =",
                    round(time.perf_counter() - t0, 3),
                    flush=True,
                )
                return url

        try:
            t_fetch0 = time.perf_counter()
            df, history_cache_hit = _get_cached_taiex_history()
            t_fetch1 = time.perf_counter()

            print(
                "DEBUG market_index chart timing",
                "| fetch_history_sec =",
                round(t_fetch1 - t_fetch0, 3),
                "| history_cache_hit =",
                history_cache_hit,
                "| rows =",
                0 if df is None else len(df),
                "| lock_wait_sec =",
                round(lock_wait_sec, 3),
                flush=True,
            )

            if df is None or df.empty:
                print(
                    "DEBUG market_index chart timing",
                    "| failed = empty_history",
                    "| use_stale_chart =",
                    bool(stale_url),
                    "| total_sec =",
                    round(time.perf_counter() - t0, 3),
                    flush=True,
                )
                return stale_url

            t_turnover0 = time.perf_counter()
            df, turnover_cache_hit = _get_cached_turnover_history(df)
            t_turnover1 = time.perf_counter()

            # 最後才補當日 Shioaji snapshot，確保現價與今日成交金額最新。
            t_append0 = time.perf_counter()

            if snapshot is not None and getattr(snapshot, "available", False):
                df = _append_snapshot_to_history(df, snapshot)

            t_append1 = time.perf_counter()

            print(
                "DEBUG market_index chart timing",
                "| turnover_sec =",
                round(t_turnover1 - t_turnover0, 3),
                "| turnover_cache_hit =",
                turnover_cache_hit,
                "| append_snapshot_sec =",
                round(t_append1 - t_append0, 3),
                flush=True,
            )

            t_chart0 = time.perf_counter()
            chart_url = _generate_market_index_kline_chart(df)
            t_chart1 = time.perf_counter()

            print(
                "DEBUG market_index chart timing",
                "| generate_chart_sec =",
                round(t_chart1 - t_chart0, 3),
                "| chart_url =",
                bool(chart_url),
                "| total_sec =",
                round(t_chart1 - t0, 3),
                flush=True,
            )

            if chart_url:
                _MARKET_INDEX_CHART_CACHE[cache_key] = (time.time(), chart_url)
                return chart_url

            return stale_url

        except Exception as exc:
            print(
                "DEBUG market_index chart timing",
                "| failed_exception =",
                repr(exc),
                "| use_stale_chart =",
                bool(stale_url),
                "| total_sec =",
                round(time.perf_counter() - t0, 3),
                flush=True,
            )

            _debug("chart failed", exc)
            return stale_url


def _get_cached_taiex_history() -> tuple[pd.DataFrame, bool]:
    cache_key = "TAIEX:D:OHLC"
    now = time.time()
    cached = _MARKET_INDEX_HISTORY_CACHE.get(cache_key)

    if cached:
        ts, df = cached

        if (
            df is not None
            and not df.empty
            and now - ts <= MARKET_INDEX_HISTORY_CACHE_TTL_SECONDS
        ):
            return df.copy(), True

    df = _fetch_taiex_history()

    if df is not None and not df.empty:
        _MARKET_INDEX_HISTORY_CACHE[cache_key] = (time.time(), df.copy())

    return df, False


def _get_cached_turnover_history(
    history_df: pd.DataFrame,
) -> tuple[pd.DataFrame, bool]:
    """
    將 TWSE 成交金額補值與歷史 OHLC 分開快取。

    即使圖表每 15 分鐘更新，也不需要每次重打成交金額 API。
    """
    cache_key = "TAIEX:D:TURNOVER"
    now = time.time()
    cached = _MARKET_INDEX_TURNOVER_CACHE.get(cache_key)

    if cached:
        ts, df = cached

        if (
            df is not None
            and not df.empty
            and now - ts <= MARKET_INDEX_TURNOVER_CACHE_TTL_SECONDS
        ):
            return df.copy(), True

    result = history_df.copy()

    try:
        enriched = apply_twse_turnover_to_market_df(result)

        if enriched is not None and not enriched.empty:
            result = enriched

    except Exception as exc:
        print(
            "DEBUG market_index apply turnover failed",
            "| error =",
            repr(exc),
            flush=True,
        )

    if result is not None and not result.empty:
        _MARKET_INDEX_TURNOVER_CACHE[cache_key] = (
            time.time(),
            result.copy(),
        )

    return result, False


# =========================
# History sources
# =========================
def _fetch_taiex_history() -> pd.DataFrame:
    """
    抓加權指數日 K 歷史資料。

    預設順序：
    1. Yahoo chart API direct
    2. yfinance
    3. TWSE 月資料 fallback

    可用 MARKET_INDEX_HISTORY_SOURCE_ORDER 調整。
    """
    fetcher_map = {
        "yahoo_direct": _fetch_taiex_history_yahoo_direct,
        "yfinance": _fetch_taiex_history_yfinance,
        "twse": _fetch_taiex_history_twse,
    }

    source_order = [
        name
        for name in MARKET_INDEX_HISTORY_SOURCE_ORDER
        if name in fetcher_map
    ]

    if not source_order:
        source_order = ["yahoo_direct", "yfinance", "twse"]

    for source_name in source_order:
        fetcher = fetcher_map[source_name]
        t0 = time.perf_counter()

        try:
            df = fetcher()
            elapsed = time.perf_counter() - t0

            if df is not None and not df.empty:
                df = _normalize_history_df(df)

                _debug(
                    "history source",
                    source_name,
                    "| rows =",
                    len(df),
                    "| sec =",
                    round(elapsed, 3),
                )
                return df

            _debug(
                "history source empty",
                source_name,
                "| sec =",
                round(elapsed, 3),
            )

        except Exception as exc:
            _debug(
                "history source failed",
                source_name,
                "| error =",
                repr(exc),
                "| sec =",
                round(time.perf_counter() - t0, 3),
            )

    return pd.DataFrame()


def _fetch_taiex_history_yfinance() -> pd.DataFrame:
    if yf is None:
        return pd.DataFrame()

    try:
        raw = yf.download(
            "^TWII",
            period="10mo",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
            timeout=MARKET_INDEX_YFINANCE_TIMEOUT_SECONDS,
        )
    except TypeError as exc:
        # 不重試一個「沒有 timeout」的 yfinance 呼叫，
        # 避免 Yahoo direct 失敗後又無限等待。
        _debug("yfinance timeout parameter unsupported", repr(exc))
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return df


def _fetch_taiex_history_yahoo_direct() -> pd.DataFrame:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"

    params = {
        "range": "10mo",
        "interval": "1d",
        "includePrePost": "false",
        "events": "history",
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    resp = _HTTP_SESSION.get(
        url,
        params=params,
        headers=headers,
        timeout=MARKET_INDEX_YAHOO_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    payload = resp.json()

    chart = payload.get("chart") or {}
    result = chart.get("result") or []

    if not result:
        return pd.DataFrame()

    item = result[0]
    timestamps = item.get("timestamp") or []
    indicators = item.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0]

    if not timestamps or not quote:
        return pd.DataFrame()

    df = pd.DataFrame(
        {
            "Open": quote.get("open") or [],
            "High": quote.get("high") or [],
            "Low": quote.get("low") or [],
            "Close": quote.get("close") or [],
            "Volume": quote.get("volume") or [],
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("Asia/Taipei").tz_localize(None),
    )

    df.index = df.index.normalize()

    return df


def _fetch_taiex_history_twse() -> pd.DataFrame:
    """
    TWSE fallback：
    - MI_5MINS_HIST：加權指數每月 OHLC。
    - FMTQIK：大盤每月成交量。
    合併後產生日K資料。

    若 TWSE 回傳欄位名稱調整，解析失敗會回空表，不影響主流程。
    """
    today = pd.Timestamp.now(tz="Asia/Taipei").date()
    months = _latest_month_starts(today, months=12)

    frames: list[pd.DataFrame] = []

    for month_start in months:
        ohlc = _fetch_twse_monthly_ohlc(month_start)
        volume = _fetch_twse_monthly_volume(month_start)

        if ohlc.empty:
            continue

        if not volume.empty:
            merged = ohlc.merge(
                volume,
                left_index=True,
                right_index=True,
                how="left",
            )
        else:
            merged = ohlc.copy()
            merged["Volume"] = 0

        frames.append(merged)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, axis=0)
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()

    return df.tail(220)

def _latest_month_starts(today, months: int = 12) -> list[pd.Timestamp]:
    current = pd.Timestamp(today).replace(day=1)
    result = []

    for i in range(months):
        result.append(current - pd.DateOffset(months=i))

    # 舊到新
    return list(reversed(result))


def _fetch_twse_monthly_ohlc(month_start: pd.Timestamp) -> pd.DataFrame:
    date_text = month_start.strftime("%Y%m01")

    urls = [
        "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST",
        "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST",
    ]

    for url in urls:
        try:
            payload = _twse_get_json(
                url,
                params={
                    "response": "json",
                    "date": date_text,
                },
            )

            data = payload.get("data") or payload.get("tables", [{}])[0].get("data") or []
            fields = payload.get("fields") or payload.get("tables", [{}])[0].get("fields") or []

            df = _parse_twse_ohlc_table(data, fields)

            if not df.empty:
                return df

        except Exception as exc:
            _debug("twse ohlc failed", date_text, exc)

    return pd.DataFrame()


def _fetch_twse_monthly_volume(month_start: pd.Timestamp) -> pd.DataFrame:
    date_text = month_start.strftime("%Y%m01")

    urls = [
        "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK",
        "https://www.twse.com.tw/exchangeReport/FMTQIK",
    ]

    for url in urls:
        try:
            payload = _twse_get_json(
                url,
                params={
                    "response": "json",
                    "date": date_text,
                },
            )

            data = payload.get("data") or payload.get("tables", [{}])[0].get("data") or []
            fields = payload.get("fields") or payload.get("tables", [{}])[0].get("fields") or []

            df = _parse_twse_volume_table(data, fields)

            if not df.empty:
                return df

        except Exception as exc:
            _debug("twse volume failed", date_text, exc)

    return pd.DataFrame()


def _twse_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.twse.com.tw/",
    }

    resp = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=8,
    )
    resp.raise_for_status()

    return resp.json()


def _parse_twse_ohlc_table(data: list, fields: list | None = None) -> pd.DataFrame:
    rows = []

    for row in data or []:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue

        date = _parse_twse_date(row[0])
        open_price = _safe_float(row[1])
        high_price = _safe_float(row[2])
        low_price = _safe_float(row[3])
        close_price = _safe_float(row[4])

        if not date or close_price <= 0:
            continue

        rows.append(
            {
                "Date": date,
                "Open": open_price,
                "High": high_price,
                "Low": low_price,
                "Close": close_price,
            }
        )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df.set_index("Date")
    df.index = df.index.normalize()

    return df


def _parse_twse_volume_table(data: list, fields: list | None = None) -> pd.DataFrame:
    rows = []

    for row in data or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue

        date = _parse_twse_date(row[0])

        if not date:
            continue

        # FMTQIK 第一個數值通常是成交股數。
        volume = _safe_int(row[1])

        rows.append(
            {
                "Date": date,
                "Volume": volume,
            }
        )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df.set_index("Date")
    df.index = df.index.normalize()

    return df


def _parse_twse_date(value: Any) -> str:
    """
    支援：
    - 2026/07/01
    - 115/07/01
    """
    text = str(value or "").strip()

    if not text:
        return ""

    text = text.replace("-", "/")

    parts = text.split("/")

    if len(parts) != 3:
        return ""

    try:
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])

        if year < 1911:
            year += 1911

        return f"{year:04d}-{month:02d}-{day:02d}"

    except Exception:
        return ""


def _normalize_history_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    work = df.copy()

    if isinstance(work.columns, pd.MultiIndex):
        work.columns = work.columns.get_level_values(0)

    # 欄名保險處理
    rename = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }

    work = work.rename(columns={col: rename.get(str(col).lower(), col) for col in work.columns})

    required = ["Open", "High", "Low", "Close", "Volume"]

    for col in required:
        if col not in work.columns:
            work[col] = 0

    work = work[required].copy()

    work.index = pd.to_datetime(work.index, errors="coerce")

    if getattr(work.index, "tz", None) is not None:
        work.index = work.index.tz_convert("Asia/Taipei").tz_localize(None)

    work.index = work.index.normalize()

    for col in required:
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)

    # 若 fallback 無 Volume，保留 0；價格一定要有效。
    work = work[work["Close"] > 0]

    # 若 Open/High/Low 缺失，用 Close 補齊，避免畫圖失敗。
    for col in ["Open", "High", "Low"]:
        work.loc[work[col] <= 0, col] = work.loc[work[col] <= 0, "Close"]

    work["High"] = work[["High", "Open", "Close"]].max(axis=1)
    work["Low"] = work[["Low", "Open", "Close"]].min(axis=1)

    work = work.sort_index()
    work = work[~work.index.duplicated(keep="last")]

    return work.tail(220)


def _append_snapshot_to_history(df: pd.DataFrame, snapshot: MarketIndexSnapshot) -> pd.DataFrame:
    result = df.copy()

    close_price = _safe_float(getattr(snapshot, "close_price", 0.0))

    if close_price <= 0:
        return result

    quote_time = str(getattr(snapshot, "quote_time", "") or "").strip()
    ts = pd.to_datetime(quote_time, errors="coerce")

    if pd.isna(ts):
        ts = pd.Timestamp.now(tz="Asia/Taipei")

    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert("Asia/Taipei").tz_localize(None)

    trade_date = pd.Timestamp(ts).normalize()

    open_price = _safe_float(getattr(snapshot, "open_price", 0.0)) or close_price
    high_price = _safe_float(getattr(snapshot, "high_price", 0.0)) or close_price
    low_price = _safe_float(getattr(snapshot, "low_price", 0.0)) or close_price
    volume = _safe_int(getattr(snapshot, "total_volume", 0)) or _safe_int(getattr(snapshot, "volume", 0))

    high_price = max(high_price, open_price, close_price)
    low_price = min(low_price, open_price, close_price)

    if trade_date in result.index:
        if open_price > 0:
            result.loc[trade_date, "Open"] = open_price

        result.loc[trade_date, "High"] = max(_safe_float(result.loc[trade_date, "High"]), high_price)
        result.loc[trade_date, "Low"] = min(
            _safe_float(result.loc[trade_date, "Low"]) or low_price,
            low_price,
        )
        result.loc[trade_date, "Close"] = close_price

        if volume > 0:
            result.loc[trade_date, "Volume"] = volume

    else:
        result.loc[trade_date, ["Open", "High", "Low", "Close", "Volume"]] = [
            open_price,
            high_price,
            low_price,
            close_price,
            volume,
        ]

    result = result.sort_index()

    return result.tail(220)


# =========================
# Chart
# =========================
def _fmt_index_ma_value(value) -> str:
    try:
        num = float(value)

        if num == 0:
            return "--"

        return f"{num:,.2f}"

    except Exception:
        return "--"

def _generate_market_index_kline_chart(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""

    _setup_chinese_font()

    work_df = df.copy()

    # 用完整資料先算均線
    for period in [5, 20, 60, 120]:
        work_df[f"MA{period}"] = (
            work_df["Close"]
            .astype(float)
            .rolling(period, min_periods=1)
            .mean()
        )

    # 顯示最近約 3 個月
    plot_df = work_df.tail(60).copy()

    if plot_df.empty:
        return ""

    latest = work_df.iloc[-1]
    latest_close = float(latest["Close"])
    latest_date = work_df.index[-1].strftime("%Y-%m-%d")

    ma5 = _fmt_index_ma_value(latest.get("MA5"))
    ma20 = _fmt_index_ma_value(latest.get("MA20"))
    ma60 = _fmt_index_ma_value(latest.get("MA60"))
    ma120 = _fmt_index_ma_value(latest.get("MA120"))

    fig = plt.figure(figsize=FIGURE_SIZES["market_index"], dpi=130, facecolor="white")

    # 上方資訊區 + K線 + 成交量
    gs = gridspec.GridSpec(
        3,
        1,
        height_ratios=[1.0, 3.5, 1.15],
        hspace=0.05,
    )

    ax_info = fig.add_subplot(gs[0])
    ax_k = fig.add_subplot(gs[1])
    ax_v = fig.add_subplot(gs[2], sharex=ax_k)

    # ========= 上方資訊區 =========
    ax_info.set_facecolor("white")
    ax_info.axis("off")

    # 第一排 MA
    ax_info.text(
        0.00,
        0.46,
        f"5MA {ma5}",
        fontsize=15,
        fontweight="bold",
        color="#111111",
        ha="left",
        va="center",
        transform=ax_info.transAxes,
    )
    ax_info.text(
        0.28,
        0.46,
        f"20MA {ma20}",
        fontsize=15,
        fontweight="bold",
        color="#1F77B4",
        ha="left",
        va="center",
        transform=ax_info.transAxes,
    )

    # 第二排 MA
    ax_info.text(
        0.00,
        0.12,
        f"60MA {ma60}",
        fontsize=15,
        fontweight="bold",
        color="#FF7F0E",
        ha="left",
        va="center",
        transform=ax_info.transAxes,
    )
    ax_info.text(
        0.28,
        0.12,
        f"120MA {ma120}",
        fontsize=15,
        fontweight="bold",
        color="#9467BD",
        ha="left",
        va="center",
        transform=ax_info.transAxes,
    )

    # ========= K線區 =========
    ax_k.set_facecolor(CHART_BACKGROUND)
    ax_v.set_facecolor(CHART_BACKGROUND)

    x_values = list(range(len(plot_df)))
    candle_width = DEFAULT_CANDLE_WIDTH

    open_values = pd.to_numeric(plot_df["Open"], errors="coerce").astype(float).values
    high_values = pd.to_numeric(plot_df["High"], errors="coerce").astype(float).values
    low_values = pd.to_numeric(plot_df["Low"], errors="coerce").astype(float).values
    close_values = pd.to_numeric(plot_df["Close"], errors="coerce").astype(float).values
    volume_values = (
        pd.to_numeric(plot_df["Volume"], errors="coerce")
        .fillna(0)
        .astype(float)
        .values
    )

    candle_colors = [
        "#FF2D2D" if close_price >= open_price else "#00B050"
        for open_price, close_price in zip(open_values, close_values)
    ]

    body_bottom = [
        min(open_price, close_price)
        for open_price, close_price in zip(open_values, close_values)
    ]
    body_height = [
        max(abs(close_price - open_price), 0.01)
        for open_price, close_price in zip(open_values, close_values)
    ]

    # 一次建立整組 artists，避免 60 根 K 棒逐根呼叫造成額外開銷。
    ax_k.vlines(
        x_values,
        low_values,
        high_values,
        linewidth=1.0,
        colors=candle_colors,
    )

    ax_k.bar(
        x_values,
        body_height,
        bottom=body_bottom,
        width=candle_width,
        color=candle_colors,
        align="center",
    )

    ax_v.bar(
        x_values,
        volume_values,
        width=candle_width,
        color=candle_colors,
        align="center",
    )

    # 均線
    ma_styles = {
        "MA5": ("#111111", 1.2),
        "MA20": ("#1F77B4", 1.2),
        "MA60": ("#FF7F0E", 1.2),
        "MA120": ("#9467BD", 1.2),
    }

    for col, (line_color, linewidth) in ma_styles.items():
        if col in plot_df.columns:
            ax_k.plot(
                x_values,
                plot_df[col].values,
                linewidth=linewidth,
                color=line_color,
            )

    # 大盤 K 線同樣以畫面可見最高／最低作為價格軸上下緣，並顯示精確軸值。
    set_price_axis_to_visible_high_low(
        ax_k,
        plot_df["High"],
        plot_df["Low"],
        tick_fontsize=AXIS_TICK_FONTSIZE,
    )
    annotate_visible_high_low(
        ax_k,
        plot_df,
        x_values,
        fontsize=HIGH_LOW_FONTSIZE,
    )

    _apply_axis_style(ax_k)
    _apply_axis_style(ax_v)

    labels = [idx.strftime("%m/%d") for idx in plot_df.index]
    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels(
        [labels[i] for i in ticks],
        rotation=0,
        fontsize=11,
    )

    plt.setp(ax_k.get_xticklabels(), visible=False)

    ax_v.set_ylabel("成交量", fontsize=11)

    ax_k.tick_params(axis="y", labelsize=11)
    ax_v.tick_params(axis="y", labelsize=11)

    _hide_top_right_spines(ax_k)
    _hide_top_right_spines(ax_v)

    # 固定留白比 tight_layout 更穩定，也少一次 layout 計算。
    fig.subplots_adjust(
        left=0.08,
        right=0.97,
        top=0.97,
        bottom=0.075,
        hspace=0.05,
    )

    try:
        return publish_figure(fig, "taiex_market_index_kline")
    finally:
        plt.close(fig)
