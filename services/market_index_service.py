from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.font_manager import FontProperties
import pandas as pd
import requests

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


# =========================
# Cache settings
# =========================
MARKET_INDEX_CACHE_TTL_SECONDS = int(os.getenv("MARKET_INDEX_CACHE_TTL_SECONDS", "3"))
MARKET_INDEX_CHART_CACHE_TTL_SECONDS = int(os.getenv("MARKET_INDEX_CHART_CACHE_TTL_SECONDS", "900"))

_MARKET_INDEX_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MARKET_INDEX_CHART_CACHE: dict[str, tuple[float, str]] = {}


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


def _setup_chinese_font() -> None:
    try:
        candidates = [
            Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSansTC-Regular.ttf",
            Path("/opt/render/project/src/assets/fonts/NotoSansTC-Regular.ttf"),
            Path("/mnt/data/NotoSansTC-Regular.ttf"),
        ]

        for font_path in candidates:
            if font_path.exists():
                font_manager.fontManager.addfont(str(font_path))
                font_name = FontProperties(fname=str(font_path)).get_name()
                plt.rcParams["font.family"] = font_name
                break

        plt.rcParams["axes.unicode_minus"] = False

    except Exception as exc:
        _debug("font setup failed", exc)


def _get_taiex_contract(api):
    """
    Shioaji 加權指數 contract 常見路徑：
    api.Contracts.Indexs.TSE["001"]
    或 api.Contracts.Indexs.TSE.TSE001
    """
    try:
        return api.Contracts.Indexs.TSE["001"]
    except Exception:
        pass

    try:
        return api.Contracts.Indexs.TSE.TSE001
    except Exception:
        pass

    try:
        return api.Contracts.Indexs["TSE"]["001"]
    except Exception:
        pass

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

        volume=_safe_int(data.get("volume")),
        total_volume=_safe_int(data.get("total_volume")),
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
    """
    取得加權指數即時 snapshot。
    with_chart=True 時會附 chart_url；圖表有 15 分鐘快取。
    """
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
                "| age_sec =",
                round(age, 2),
                "| chart_url =",
                bool(snapshot.chart_url),
                "| total_sec =",
                round(time.perf_counter() - route_t0, 3),
            )

            return snapshot

    api = get_api()

    if api is None:
        return MarketIndexSnapshot(
            available=False,
            message="Shioaji 尚未登入，無法取得加權指數即時資料。",
        )

    contract = _get_taiex_contract(api)

    if contract is None:
        return MarketIndexSnapshot(
            available=False,
            message="找不到加權指數 contract：TSE001。",
        )

    try:
        t_snapshot0 = time.perf_counter()

        snapshots = api.snapshots([contract])

        t_snapshot1 = time.perf_counter()

        if not snapshots:
            return MarketIndexSnapshot(
                available=False,
                message="Shioaji 沒有回傳加權指數 snapshot。",
            )

        raw = _to_dict(snapshots[0])

        data = {
            "available": True,
            "message": "ok",
            "index_id": "TAIEX",
            "index_name": "加權指數",

            "open_price": _safe_float(raw.get("open")),
            "high_price": _safe_float(raw.get("high")),
            "low_price": _safe_float(raw.get("low")),
            "close_price": _safe_float(raw.get("close")),

            "change": _safe_float(raw.get("change_price")),
            "change_pct": _safe_float(raw.get("change_rate")),

            "volume": _safe_int(raw.get("volume")),
            "total_volume": _safe_int(raw.get("total_volume")),
            "amount": _safe_int(raw.get("amount")),
            "total_amount": _safe_int(raw.get("total_amount")),

            "quote_time": _normalize_ts(raw.get("ts")),
            "quote_source": "永豐即時",

            "chart_url": "",
        }

        # 先快取即時數字，避免圖表失敗影響即時回覆。
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
            "| close =",
            data["close_price"],
            "| change =",
            data["change"],
            "| change_pct =",
            data["change_pct"],
            "| volume =",
            data["total_volume"],
            "| time =",
            data["quote_time"],
            "| chart_url =",
            bool(data["chart_url"]),
            "| shioaji_sec =",
            round(t_snapshot1 - t_snapshot0, 3),
            "| chart_sec =",
            round(t_chart1 - t_chart0, 3),
            "| total_sec =",
            round(time.perf_counter() - route_t0, 3),
        )

        return snapshot

    except Exception as exc:
        _debug("snapshot failed", exc)

        return MarketIndexSnapshot(
            available=False,
            message=f"取得加權指數即時資料失敗：{exc}",
        )


def get_market_index_chart_url(snapshot: MarketIndexSnapshot | None = None) -> str:
    """
    產生加權指數日K圖：
    1. 優先吃 15 分鐘 chart 快取。
    2. 資料源優先順序：
       - yfinance ^TWII
       - Yahoo chart API direct
       - TWSE MI_5MINS_HIST + FMTQIK
    3. 如果新資料失敗，回舊圖 stale url。
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

        print(
            "DEBUG market_index chart timing",
            "| chart_cache_hit = False",
            "| cache_expired =",
            bool(url),
            "| age_sec =",
            round(age, 1),
            "| ttl_sec =",
            MARKET_INDEX_CHART_CACHE_TTL_SECONDS,
            flush=True,
        )
    else:
        print(
            "DEBUG market_index chart timing",
            "| chart_cache_hit = False",
            "| no_cache = True",
            flush=True,
        )

    try:
        t_fetch0 = time.perf_counter()

        df = _fetch_taiex_history()

        t_fetch1 = time.perf_counter()

        print(
            "DEBUG market_index chart timing",
            "| fetch_history_sec =",
            round(t_fetch1 - t_fetch0, 3),
            "| rows =",
            0 if df is None else len(df),
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

        t_append0 = time.perf_counter()

        if snapshot is not None and getattr(snapshot, "available", False):
            df = _append_snapshot_to_history(df, snapshot)

        t_append1 = time.perf_counter()

        print(
            "DEBUG market_index chart timing",
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
            _MARKET_INDEX_CHART_CACHE[cache_key] = (now, chart_url)
            return chart_url

        return stale_url

    except Exception as exc:
        print(
            "DEBUG market_index chart timing",
            "| failed_exception =",
            exc,
            "| use_stale_chart =",
            bool(stale_url),
            "| total_sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )

        _debug("chart failed", exc)
        return stale_url


# =========================
# History sources
# =========================
def _fetch_taiex_history() -> pd.DataFrame:
    """
    抓加權指數日K歷史資料。
    依序嘗試：
    1. yfinance ^TWII
    2. Yahoo chart API direct
    3. TWSE MI_5MINS_HIST + FMTQIK
    """
    sources = [
        ("yfinance", _fetch_taiex_history_yfinance),
        ("yahoo_direct", _fetch_taiex_history_yahoo_direct),
        ("twse", _fetch_taiex_history_twse),
    ]

    for source_name, fetcher in sources:
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
                exc,
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
            timeout=10,
        )
    except TypeError:
        raw = yf.download(
            "^TWII",
            period="10mo",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )

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

    resp = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=10,
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
def _generate_market_index_kline_chart(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""

    _setup_chinese_font()

    work_df = df.copy()

    for ma in [5, 20, 60, 120]:
        work_df[f"MA{ma}"] = work_df["Close"].rolling(ma).mean()

    plot_df = work_df.tail(130).copy()

    if plot_df.empty:
        return ""

    fig = plt.figure(figsize=(7.2, 5.8), dpi=130, facecolor="white")
    gs = gridspec.GridSpec(2, 1, height_ratios=[3, 1], hspace=0.06)

    ax_k = fig.add_subplot(gs[0])
    ax_v = fig.add_subplot(gs[1], sharex=ax_k)

    ax_k.set_facecolor("#F8F9FA")
    ax_v.set_facecolor("#F8F9FA")

    x_values = list(range(len(plot_df)))
    candle_width = 0.58

    for i in range(len(plot_df)):
        row = plot_df.iloc[i]

        open_price = float(row["Open"])
        high_price = float(row["High"])
        low_price = float(row["Low"])
        close_price = float(row["Close"])
        volume = int(row["Volume"])

        color = "#FF2D2D" if close_price >= open_price else "#00B050"

        ax_k.vlines(i, low_price, high_price, linewidth=1.0, color=color)

        lower = min(open_price, close_price)
        height = abs(close_price - open_price)

        if height <= 0:
            height = 0.01

        ax_k.bar(
            i,
            height,
            bottom=lower,
            width=candle_width,
            color=color,
            align="center",
        )

        ax_v.bar(
            i,
            volume,
            width=candle_width,
            color=color,
            align="center",
        )

    ma_styles = {
        "MA5": ("MA5", "#111111", 1.1),
        "MA20": ("MA20", "#1F77B4", 1.1),
        "MA60": ("MA60", "#FF7F0E", 1.1),
        "MA120": ("MA120", "#9467BD", 1.1),
    }

    for col, (label, color, linewidth) in ma_styles.items():
        if col in plot_df.columns:
            ax_k.plot(
                x_values,
                plot_df[col].values,
                label=label,
                linewidth=linewidth,
                color=color,
            )

    latest_close = float(plot_df["Close"].iloc[-1])
    latest_date = plot_df.index[-1].strftime("%Y-%m-%d")

    ax_k.set_title(
        f"加權指數日K｜{latest_date} 收 {latest_close:,.2f}",
        fontsize=13,
        fontweight="bold",
    )

    ax_k.grid(True, linestyle=":", alpha=0.4)
    ax_v.grid(True, linestyle=":", alpha=0.35)

    ax_k.legend(loc="upper left", fontsize=8, ncol=4, frameon=False)

    labels = [idx.strftime("%m/%d") for idx in plot_df.index]
    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels(
        [labels[i] for i in ticks],
        rotation=0,
        fontsize=8,
    )

    plt.setp(ax_k.get_xticklabels(), visible=False)

    ax_v.set_ylabel("成交量", fontsize=9)

    ax_k.tick_params(axis="y", labelsize=8)
    ax_v.tick_params(axis="y", labelsize=8)

    ax_k.spines["top"].set_visible(False)
    ax_k.spines["right"].set_visible(False)
    ax_v.spines["top"].set_visible(False)
    ax_v.spines["right"].set_visible(False)

    fig.tight_layout()

    try:
        return publish_figure(fig, "taiex_market_index_kline")
    finally:
        plt.close(fig)
