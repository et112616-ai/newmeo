from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from utils.formatter import normalize_time_frame, signed_number, signed_percent


STOCK_SERVICE_VERSION = "stock_service_v4_intraday_reference_price"

TW_SUFFIX = ".TW"
TWO_SUFFIX = ".TWO"

REFERENCE_PRICE_COL = "_reference_price"
DISPLAY_TIMESTAMP_COL = "_display_timestamp"


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


def _download_history(symbol: str, period: str, interval: str) -> pd.DataFrame:
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=False)
    return df


def _normalize_to_taipei_time(df: pd.DataFrame) -> pd.DataFrame:
    """
    將 yfinance 回傳時間統一轉成台北時間，並移除 timezone。
    """
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
    """
    盤中資料只保留最新交易日，避免 5d / 5m 把多天混在同一張圖。
    """
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df

    last_date = df.index[-1].date()
    return df[df.index.date == last_date].copy()


def _filter_tw_stock_session(df: pd.DataFrame, time_frame: str) -> pd.DataFrame:
    """
    台股現貨交易時間：09:00 ~ 13:30
    """
    if df.empty or time_frame not in {"1m", "5m"}:
        return df

    try:
        return df.between_time("09:00", "13:30").copy()
    except Exception as exc:
        print(f"_filter_tw_stock_session failed: {exc}")
        return df


def _set_reference_price(df: pd.DataFrame, reference_price: float) -> pd.DataFrame:
    """
    同時把平盤價存到 attrs 和欄位。
    欄位是為了避免某些 pandas 操作後 attrs 遺失。
    """
    df = df.copy()

    ref = float(reference_price)

    df.attrs["reference_price"] = ref
    df[REFERENCE_PRICE_COL] = ref

    return df


def _get_reference_price_from_df(df: pd.DataFrame) -> float:
    """
    從 df 取平盤價。

    優先順序：
    1. df.attrs["reference_price"]
    2. df["_reference_price"]
    3. 今日第一筆 Open
    4. 今日第一筆 Close
    """
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


def _get_previous_close(meta: StockMeta, trade_date) -> float:
    """
    取得前一交易日收盤價，作為平盤價。
    """
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
    平盤價 = 前一交易日收盤價，不是今日開盤價。
    """
    if df.empty or time_frame not in {"1m", "5m"}:
        return df

    if not isinstance(df.index, pd.DatetimeIndex):
        return df

    df = df.copy()

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
    收盤後補一筆 13:30，價格沿用最後一筆，讓畫面顯示完整收盤時間。
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


def get_history(meta: StockMeta, time_frame: str = "D") -> tuple[pd.DataFrame, str]:
    tf = normalize_time_frame(time_frame)

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

    return df, tf


def get_stock_name(meta: StockMeta) -> str:
    if meta.stock_name and meta.stock_name != meta.stock_id:
        return meta.stock_name

    try:
        ticker = yf.Ticker(meta.yf_symbol)
        info = ticker.info or {}

        return (
            info.get("shortName")
            or info.get("longName")
            or meta.stock_id
        )

    except Exception:
        return meta.stock_id


def build_price_meta(df: pd.DataFrame, time_frame: str) -> PriceMeta:
    """
    價格資訊。

    重要：
    - 1m / 5m：漲跌幅 = 最新價 vs 平盤價
    - D / W / M：漲跌幅 = 最新一根 vs 前一根
    """
    if df.empty:
        return PriceMeta("--", "--", "--", 0.0, 0.0)

    tf = normalize_time_frame(time_frame)

    latest = float(df["Close"].iloc[-1])

    if tf in {"1m", "5m"}:
        prev = _get_reference_price_from_df(df)

        if not prev:
            prev = latest

    else:
        prev = float(df["Close"].iloc[-2]) if len(df) > 1 else latest

    change = latest - prev
    pct = (change / prev * 100) if prev else 0.0

    if tf in {"1m", "5m"}:
        display_stamp = df.attrs.get("display_timestamp")

        if not display_stamp and DISPLAY_TIMESTAMP_COL in df.columns:
            try:
                s = df[DISPLAY_TIMESTAMP_COL].dropna()
                if not s.empty:
                    display_stamp = str(s.iloc[-1])
            except Exception:
                display_stamp = ""

        if display_stamp:
            stamp = str(display_stamp)
        else:
            last_ts = df.index[-1]

            try:
                now_tpe = datetime.now(ZoneInfo("Asia/Taipei"))
                trade_date = last_ts.date()

                if (
                    last_ts.time() >= time(13, 20)
                    and (
                        trade_date < now_tpe.date()
                        or (
                            trade_date == now_tpe.date()
                            and now_tpe.time() >= time(13, 35)
                        )
                    )
                ):
                    stamp = f"{trade_date.strftime('%Y-%m-%d')} 13:30"
                else:
                    stamp = last_ts.strftime("%Y-%m-%d %H:%M")

            except Exception:
                stamp = df.index[-1].strftime("%Y-%m-%d %H:%M")

    else:
        stamp = df.index[-1].strftime("%Y-%m-%d")

    print(
        STOCK_SERVICE_VERSION,
        "| build_price_meta",
        "| tf=", tf,
        "| latest=", latest,
        "| reference/prev=", prev,
        "| change=", change,
        "| pct=", pct,
        "| stamp=", stamp,
        "| attrs=", df.attrs,
    )

    return PriceMeta(
        price_info=f"{latest:.2f}",
        change_info=f"{signed_number(change)} ({signed_percent(pct)})",
        time_stamp=stamp,
        price_change=change,
        latest_price=latest,
    )
