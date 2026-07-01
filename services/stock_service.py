from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Optional
from zoneinfo import ZoneInfo

import time as time_module
import pandas as pd
import yfinance as yf

from utils.formatter import normalize_time_frame, signed_number, signed_percent


STOCK_SERVICE_VERSION = "stock_service_v5_quote_reconcile"

TW_SUFFIX = ".TW"
TWO_SUFFIX = ".TWO"

REFERENCE_PRICE_COL = "_reference_price"
DISPLAY_TIMESTAMP_COL = "_display_timestamp"
QUOTE_PRICE_COL = "_quote_price"
QUOTE_CACHE_TTL_SECONDS = 20
_QUOTE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
DWM_CACHE_TTL_SECONDS = 60
_DWM_CACHE: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}

@dataclass
class StockMeta:
    input_text: str
    stock_id: str
    yf_symbol: str
    stock_name: str


@dataclass
class PriceMeta:
    price_info: str
    change_info: str
    time_stamp: str
    price_change: float
    latest_price: float


def _twstock_lookup(query: str) -> Optional[tuple[str, str]]:
    try:
        import twstock

        q = query.strip()
        q_upper = q.upper().replace(TW_SUFFIX, "").replace(TWO_SUFFIX, "")

        if q_upper in twstock.codes:
            item = twstock.codes[q_upper]
            return q_upper, getattr(item, "name", q_upper) or q_upper

        exact = []
        partial = []

        for code, item in twstock.codes.items():
            name = getattr(item, "name", "") or ""

            if not name:
                continue

            if q == name:
                exact.append((code, name))
            elif q in name:
                partial.append((code, name))

        if exact:
            return exact[0]

        if partial:
            return partial[0]

    except Exception:
        return None

    return None


def normalize_stock_input(stock_input: str) -> StockMeta:
    raw = str(stock_input or "").strip()

    if not raw:
        raise ValueError("請輸入股票代號或名稱。")

    cleaned = raw.upper().replace(TW_SUFFIX, "").replace(TWO_SUFFIX, "").strip()

    lookup = _twstock_lookup(raw)

    if lookup:
        stock_id, stock_name = lookup
    elif cleaned.isdigit():
        stock_id, stock_name = cleaned, cleaned
    else:
        stock_id, stock_name = cleaned, raw

    yf_symbol = f"{stock_id}.TW" if stock_id.isdigit() else stock_id

    return StockMeta(
        input_text=raw,
        stock_id=stock_id,
        yf_symbol=yf_symbol,
        stock_name=stock_name,
    )


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


def _read_value(source: Any, keys: list[str]) -> Any:
    for key in keys:
        try:
            if isinstance(source, dict) and key in source:
                return source.get(key)

            if hasattr(source, "get"):
                value = source.get(key)
                if value not in (None, ""):
                    return value

            if hasattr(source, key):
                value = getattr(source, key)
                if value not in (None, ""):
                    return value

        except Exception:
            continue

    return None


def _download_history(symbol: str, period: str, interval: str) -> pd.DataFrame:
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=False)
    return df


def _normalize_to_taipei_time(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df

    df = df.copy()

    try:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("Asia/Taipei")
        else:
            df.index = df.index.tz_convert("Asia/Taipei")

        df.index = df.index.tz_localize(None)

    except Exception as exc:
        print(f"_normalize_to_taipei_time failed: {exc}")

    return df


def _keep_latest_trading_day(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df

    last_date = df.index[-1].date()
    return df[df.index.date == last_date].copy()


def _filter_tw_stock_session(df: pd.DataFrame, time_frame: str) -> pd.DataFrame:
    if df.empty or time_frame not in {"1m", "5m"}:
        return df

    try:
        return df.between_time("09:00", "13:30").copy()
    except Exception as exc:
        print(f"_filter_tw_stock_session failed: {exc}")
        return df


def _set_reference_price(df: pd.DataFrame, reference_price: float) -> pd.DataFrame:
    df = df.copy()

    ref = float(reference_price)

    df.attrs["reference_price"] = ref
    df[REFERENCE_PRICE_COL] = ref

    return df


def _get_reference_price_from_df(df: pd.DataFrame) -> float:
    try:
        ref = df.attrs.get("reference_price")

        if ref not in (None, ""):
            ref_float = float(ref)

            if ref_float > 0:
                return ref_float

    except Exception:
        pass

    try:
        if REFERENCE_PRICE_COL in df.columns:
            s = df[REFERENCE_PRICE_COL].dropna()

            if not s.empty:
                ref_float = float(s.iloc[-1])

                if ref_float > 0:
                    return ref_float

    except Exception:
        pass

    try:
        if "Open" in df.columns and not df["Open"].empty:
            ref_float = float(df["Open"].iloc[0])

            if ref_float > 0:
                return ref_float

    except Exception:
        pass

    try:
        return float(df["Close"].iloc[0])
    except Exception:
        return 0.0


def _get_latest_price_from_df(df: pd.DataFrame) -> float:
    """
    最新價優先順序：
    1. quote 覆蓋價
    2. quote 欄位
    3. history 最後一筆 Close
    """
    try:
        quote_price = df.attrs.get("quote_price")

        if quote_price not in (None, ""):
            price = float(quote_price)

            if price > 0:
                return price

    except Exception:
        pass

    try:
        if QUOTE_PRICE_COL in df.columns:
            s = df[QUOTE_PRICE_COL].dropna()

            if not s.empty:
                price = float(s.iloc[-1])

                if price > 0:
                    return price

    except Exception:
        pass

    return float(df["Close"].iloc[-1])


def _get_previous_close(meta: StockMeta, trade_date) -> float:
    try:
        daily = _download_history(meta.yf_symbol, "15d", "1d")

        if daily.empty and meta.yf_symbol.endswith(TW_SUFFIX):
            two_symbol = meta.yf_symbol.replace(TW_SUFFIX, TWO_SUFFIX)
            daily = _download_history(two_symbol, "15d", "1d")

        if daily.empty:
            return 0.0

        daily = daily.dropna(subset=["Close"])
        daily = _normalize_to_taipei_time(daily)

        if daily.empty:
            return 0.0

        prev_daily = daily[daily.index.date < trade_date]

        if prev_daily.empty:
            return 0.0

        return float(prev_daily["Close"].iloc[-1])

    except Exception as exc:
        print(f"_get_previous_close failed: {exc}")
        return 0.0


def _attach_intraday_reference_price(
    meta: StockMeta,
    df: pd.DataFrame,
    time_frame: str,
) -> pd.DataFrame:
    """
    幫 1m / 5m 盤中資料加上平盤價。
    平盤價 = 前一交易日收盤價。

    優先使用 fast_info.previous_close，避免多打一個日 K 造成 timeout。
    """
    if df.empty or time_frame not in {"1m", "5m"}:
        return df

    if not isinstance(df.index, pd.DatetimeIndex):
        return df

    df = df.copy()

    try:
        quote = _get_yahoo_quote_snapshot(meta)
        ref_price = _safe_float(quote.get("previous_close"))

        if ref_price > 0:
            return _set_reference_price(df, ref_price)

    except Exception as exc:
        print(f"_attach_intraday_reference_price quote failed: {exc}")

    trade_date = df.index[-1].date()

    ref_price = _get_previous_close(meta, trade_date)

    if ref_price > 0:
        return _set_reference_price(df, ref_price)

    try:
        fallback_ref = float(df["Open"].iloc[0])
        return _set_reference_price(df, fallback_ref)
    except Exception:
        return df
def _append_intraday_close_point(df: pd.DataFrame, time_frame: str) -> pd.DataFrame:
    """
    yfinance 有時最後一筆停在 13:24、13:25。
    收盤後補一筆 13:30，價格先沿用最後一筆。
    後面會再用 quote 最新價覆蓋。
    """
    if df.empty or time_frame not in {"1m", "5m"}:
        return df

    if not isinstance(df.index, pd.DatetimeIndex):
        return df

    try:
        df = df.copy()
        attrs = dict(df.attrs)

        last_ts = df.index[-1]
        trade_date = last_ts.date()
        close_ts = pd.Timestamp.combine(trade_date, time(13, 30))

        if last_ts >= close_ts:
            return df

        if last_ts.time() < time(13, 20):
            return df

        now_tpe = datetime.now(ZoneInfo("Asia/Taipei"))

        is_old_trade_day = trade_date < now_tpe.date()
        is_after_close_today = (
            trade_date == now_tpe.date()
            and now_tpe.time() >= time(13, 35)
        )

        if not is_old_trade_day and not is_after_close_today:
            return df

        last_row = df.iloc[-1].copy()

        append_df = pd.DataFrame([last_row], index=[close_ts])
        append_df.columns = df.columns

        df = pd.concat([df, append_df])
        df = df[~df.index.duplicated(keep="last")].sort_index()

        df.attrs.update(attrs)

        display_stamp = f"{trade_date.strftime('%Y-%m-%d')} 13:30"
        df.attrs["display_timestamp"] = display_stamp
        df[DISPLAY_TIMESTAMP_COL] = display_stamp

        return df

    except Exception as exc:
        print(f"_append_intraday_close_point failed: {exc}")
        return df


def _get_yahoo_quote_snapshot(meta: StockMeta) -> dict[str, Any]:
    """
    用 yfinance fast_info 抓最新報價。

    重要：
    - 不使用 ticker.info，因為它常常很慢，容易造成 Make HTTP timeout。
    - 加 20 秒快取，避免同一請求流程重複打 Yahoo。
    """
    cache_key = meta.yf_symbol
    now = time_module.time()

    cached = _QUOTE_CACHE.get(cache_key)

    if cached:
        ts, data = cached
        if now - ts <= QUOTE_CACHE_TTL_SECONDS:
            return dict(data)

    try:
        ticker = yf.Ticker(meta.yf_symbol)
        fast_info = ticker.fast_info

        latest = _safe_float(
            _read_value(
                fast_info,
                [
                    "last_price",
                    "lastPrice",
                    "regularMarketPrice",
                    "currentPrice",
                ],
            )
        )

        previous_close = _safe_float(
            _read_value(
                fast_info,
                [
                    "previous_close",
                    "previousClose",
                    "regularMarketPreviousClose",
                    "regular_market_previous_close",
                ],
            )
        )

        result: dict[str, Any] = {}

        if latest > 0:
            result["latest_price"] = latest

        if previous_close > 0:
            result["previous_close"] = previous_close

        if result:
            _QUOTE_CACHE[cache_key] = (now, result)

        return result

    except Exception as exc:
        print(f"_get_yahoo_quote_snapshot fast_info failed: {exc}")
        return {}
        
def _reconcile_intraday_with_quote(
    meta: StockMeta,
    df: pd.DataFrame,
    time_frame: str,
) -> pd.DataFrame:
    """
    用 quote 最新價覆蓋 intraday history 最後一筆。

    這是修正：
    - yfinance history 最後 Close 可能是 204
    - Yahoo 報價頁 13:30 可能是 203
    的問題。
    """
    if df.empty or time_frame not in {"1m", "5m"}:
        return df

    if not isinstance(df.index, pd.DatetimeIndex):
        return df

    quote = _get_yahoo_quote_snapshot(meta)

    latest_price = _safe_float(quote.get("latest_price"))

    if latest_price <= 0:
        return df

    df = df.copy()

    display_stamp = df.attrs.get("display_timestamp")

    previous_close = _safe_float(quote.get("previous_close"))

    if previous_close > 0:
        df = _set_reference_price(df, previous_close)

    if display_stamp:
        df.attrs["display_timestamp"] = display_stamp
        df[DISPLAY_TIMESTAMP_COL] = display_stamp

    last_idx = df.index[-1]

    df.at[last_idx, "Close"] = latest_price

    if "High" in df.columns:
        df.at[last_idx, "High"] = max(_safe_float(df.at[last_idx, "High"]), latest_price)

    if "Low" in df.columns:
        old_low = _safe_float(df.at[last_idx, "Low"])

        if old_low > 0:
            df.at[last_idx, "Low"] = min(old_low, latest_price)
        else:
            df.at[last_idx, "Low"] = latest_price

    if "Open" in df.columns:
        old_open = _safe_float(df.at[last_idx, "Open"])

        if old_open <= 0:
            df.at[last_idx, "Open"] = latest_price

    df.attrs["quote_price"] = latest_price
    df.attrs["quote_source"] = "yfinance.fast_info"
    df[QUOTE_PRICE_COL] = latest_price

    print(
        STOCK_SERVICE_VERSION,
        "| reconcile_quote",
        "| stock=", meta.stock_id,
        "| yf_symbol=", meta.yf_symbol,
        "| quote_latest=", latest_price,
        "| quote_prev_close=", previous_close,
        "| last_idx=", last_idx,
    )

    return df

def _normalize_yf_daily_df(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.copy()

    # yfinance 有時會回 MultiIndex 欄位
    if isinstance(df.columns, pd.MultiIndex):
        for level in range(df.columns.nlevels):
            level_values = set(str(x) for x in df.columns.get_level_values(level))
            if {"Open", "High", "Low", "Close"}.issubset(level_values):
                df.columns = df.columns.get_level_values(level)
                break

    df = df.loc[:, ~df.columns.duplicated()]

    required = ["Open", "High", "Low", "Close"]

    for col in required:
        if col not in df.columns:
            return pd.DataFrame()

    if "Volume" not in df.columns:
        df["Volume"] = 0

    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])

    if df.empty:
        return pd.DataFrame()

    df.index = pd.to_datetime(df.index).tz_localize(None)

    return df


def _resample_ohlcv_keep_latest_date(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df

    latest_trade_ts = df.index[-1]

    out = df.resample(rule).agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )

    out = out.dropna(subset=["Open", "High", "Low", "Close"])

    if out.empty:
        return out

    # 重點：
    # 週線、月線最後一根 K 的 index 改成實際最新交易日
    # 避免月線顯示 2026-06-01 或週線顯示未來週五
    idx = list(out.index)
    idx[-1] = latest_trade_ts
    out.index = pd.DatetimeIndex(idx)

    out = out[~out.index.duplicated(keep="last")]

    return out


def _get_dwm_history_from_daily(meta, time_frame: str) -> pd.DataFrame:
    tf = normalize_time_frame(time_frame)
    cache_key = (meta.yf_symbol, tf)
    now = time_module.time()

    cached = _DWM_CACHE.get(cache_key)

    if cached:
        ts, cached_df = cached
        if now - ts <= DWM_CACHE_TTL_SECONDS:
            return cached_df.copy()

    # 為了計算 120T：
    # D 至少要 120 日
    # W 至少要 120 週
    # M 至少要 120 月，所以月線抓 15 年日 K 再轉月 K
    period_map = {
        "D": "2y",
        "W": "5y",
        "M": "15y",
    }

    period = period_map.get(tf, "2y")

    try:
        raw = yf.download(
            meta.yf_symbol,
            period=period,
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception as exc:
        print(f"_get_dwm_history_from_daily download failed: {meta.yf_symbol}, tf={tf}, error={exc}")
        return pd.DataFrame()

    daily = _normalize_yf_daily_df(raw)

    if daily.empty:
        print(f"_get_dwm_history_from_daily empty daily: {meta.yf_symbol}, tf={tf}")
        return pd.DataFrame()

    if tf == "D":
        out = daily.copy()
    elif tf == "W":
        out = _resample_ohlcv_keep_latest_date(daily, "W-FRI")
    elif tf == "M":
        try:
            out = _resample_ohlcv_keep_latest_date(daily, "ME")
        except Exception:
            out = _resample_ohlcv_keep_latest_date(daily, "M")
    else:
        out = daily.copy()

    if out.empty:
        return pd.DataFrame()

    # 保留足夠資料給 120T，但不要讓圖太重
    tail_map = {
        "D": 180,
        "W": 180,
        "M": 140,
    }

    out = out.tail(tail_map.get(tf, 180)).copy()

    display_stamp = daily.index[-1].strftime("%Y-%m-%d")
    out.attrs["display_timestamp"] = display_stamp

    try:
        out[DISPLAY_TIMESTAMP_COL] = display_stamp
    except Exception:
        pass

    _DWM_CACHE[cache_key] = (now, out.copy())

    print(
        "stock_service_v5_quote_reconcile | dwm_history | "
        f"stock={meta.stock_id} | tf={tf} | rows={len(out)} | latest={out.index[-1]}"
    )

    return out

def get_history(meta: StockMeta, time_frame: str = "D") -> tuple[pd.DataFrame, str]:
    tf = normalize_time_frame(time_frame)
    
    if tf in {"D", "W", "M"}:
        dwm_df = _get_dwm_history_from_daily(meta, tf)

        if not dwm_df.empty:
            return dwm_df

    mapping = {
        "1m": ("1d", "1m"),
        "5m": ("5d", "5m"),
        "D": ("6mo", "1d"),
        "W": ("2y", "1wk"),
        "M": ("5y", "1mo"),
    }

    period, interval = mapping.get(tf, mapping["D"])

    df = _download_history(meta.yf_symbol, period, interval)

    if df.empty and meta.yf_symbol.endswith(TW_SUFFIX):
        two_symbol = meta.yf_symbol.replace(TW_SUFFIX, TWO_SUFFIX)
        df = _download_history(two_symbol, period, interval)

        if not df.empty:
            meta.yf_symbol = two_symbol

    if df.empty and tf == "1m":
        df = _download_history(meta.yf_symbol, "5d", "1m")

    if not df.empty:
        df = df.dropna(subset=["Close"])

        df = _normalize_to_taipei_time(df)

        if tf in {"1m", "5m"}:
            df = _keep_latest_trading_day(df)

        if tf in {"1m", "5m"}:
            df = _filter_tw_stock_session(df, tf)

        if tf in {"1m", "5m"}:
            df = _attach_intraday_reference_price(meta, df, tf)

        if tf in {"1m", "5m"}:
            df = _append_intraday_close_point(df, tf)

        if tf in {"1m", "5m"}:
            df = _reconcile_intraday_with_quote(meta, df, tf)

    return df, tf


def get_stock_name(meta: StockMeta) -> str:
    """
    股票名稱優先使用 twstock 查到的名稱。
    不再呼叫 yfinance ticker.info，避免查詢 timeout。
    """
    if meta.stock_name and meta.stock_name != meta.stock_id:
        return meta.stock_name

    return meta.stock_id
    
def build_price_meta(df: pd.DataFrame, time_frame: str) -> PriceMeta:
    """
    價格資訊。

    規則：
    - 1m / 5m：最新價、時間 一律以 intraday df 最後一筆為準
    - 漲跌幅 = 最新價 vs 平盤價(reference_price)
    - D / W / M：最新一根 vs 前一根
    """
    if df is None or df.empty:
        return PriceMeta("--", "--", "--", 0.0, 0.0)

    tf = normalize_time_frame(time_frame)

    latest = float(df["Close"].iloc[-1])

    if tf in {"1m", "5m"}:
        ref_price = df.attrs.get("reference_price")

        try:
            prev = float(ref_price)
        except Exception:
            prev = 0.0

        if not prev:
            try:
                prev = float(df["Open"].iloc[0])
            except Exception:
                prev = latest

        change = latest - prev
        pct = (change / prev * 100) if prev else 0.0

        stamp = df.index[-1].strftime("%Y-%m-%d %H:%M")

        return PriceMeta(
            price_info=f"{latest:.2f}",
            change_info=f"{signed_number(change)} ({signed_percent(pct)})",
            time_stamp=stamp,
            price_change=change,
            latest_price=latest,
        )

    # D / W / M
    prev = float(df["Close"].iloc[-2]) if len(df) > 1 else latest
    change = latest - prev
    pct = (change / prev * 100) if prev else 0.0
    stamp = df.index[-1].strftime("%Y-%m-%d")

    return PriceMeta(
        price_info=f"{latest:.2f}",
        change_info=f"{signed_number(change)} ({signed_percent(pct)})",
        time_stamp=stamp,
        price_change=change,
        latest_price=latest,
    )
    
