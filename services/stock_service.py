from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf

from utils.formatter import normalize_time_frame, signed_number, signed_percent


@dataclass
class StockMeta:
    input_text: str
    stock_id: str          # 2330
    yf_symbol: str         # 2330.TW
    stock_name: str        # 台積電 / shortName / fallback


@dataclass
class PriceMeta:
    price_info: str
    change_info: str
    time_stamp: str
    price_change: float
    latest_price: float


TW_SUFFIX = ".TW"
TWO_SUFFIX = ".TWO"


def _twstock_lookup(query: str) -> Optional[tuple[str, str]]:
    """
    用 twstock 的 codes 資料做台股名稱/代號查詢，不在專案內硬編股票清單。
    找不到時回傳 None。
    """
    try:
        import twstock

        q = query.strip()
        q_upper = q.upper().replace(TW_SUFFIX, "").replace(TWO_SUFFIX, "")

        # 代號精準查詢
        if q_upper in twstock.codes:
            item = twstock.codes[q_upper]
            return q_upper, getattr(item, "name", q_upper) or q_upper

        # 名稱精準 / 包含查詢
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
    將 yfinance 回傳的時間統一轉成台北時間，並移除 timezone。

    如果 yfinance 回來是 UTC：
    01:00 -> 09:00

    如果 yfinance 回來已經是 Asia/Taipei：
    09:00 -> 09:00
    """
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df

    df = df.copy()

    try:
        if df.index.tz is None:
            # 你的圖出現 01:00，代表高機率是 UTC naive。
            df.index = df.index.tz_localize("UTC").tz_convert("Asia/Taipei")
        else:
            df.index = df.index.tz_convert("Asia/Taipei")

        # Matplotlib 比較穩定：轉完台北時間後拿掉 timezone。
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
    return df[df.index.date == last_date]


def _filter_tw_stock_session(df: pd.DataFrame, time_frame: str) -> pd.DataFrame:
    """
    台股現貨交易時間：
    09:00 ~ 13:30

    只對 1m / 5m 盤中資料做過濾。
    """
    if df.empty or time_frame not in {"1m", "5m"}:
        return df

    try:
        return df.between_time("09:00", "13:30")
    except Exception as exc:
        print(f"_filter_tw_stock_session failed: {exc}")
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

    # 上櫃股票 fallback .TWO
    if df.empty and meta.yf_symbol.endswith(TW_SUFFIX):
        two_symbol = meta.yf_symbol.replace(TW_SUFFIX, TWO_SUFFIX)
        df = _download_history(two_symbol, period, interval)

        if not df.empty:
            meta.yf_symbol = two_symbol

    # yfinance 盤中 1m 在盤後 / 假日可能 period=1d 空掉，改抓 5d 再取最後交易日。
    if df.empty and tf == "1m":
        df = _download_history(meta.yf_symbol, "5d", "1m")

    if not df.empty:
        df = df.dropna(subset=["Close"])

        # 關鍵：先轉台北時間
        df = _normalize_to_taipei_time(df)

        # 1m / 5m 只取最新交易日
        if tf in {"1m", "5m"}:
            df = _keep_latest_trading_day(df)

        # 1m / 5m 過濾台股現貨盤中時間
        df = _filter_tw_stock_session(df, tf)

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
    if df.empty:
        return PriceMeta("--", "--", "--", 0.0, 0.0)

    latest = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-2]) if len(df) > 1 else latest

    change = latest - prev
    pct = (change / prev * 100) if prev else 0.0

    if time_frame in {"1m", "5m"}:
        stamp = df.index[-1].strftime("%Y-%m-%d %H:%M")
    else:
        stamp = df.index[-1].strftime("%Y-%m-%d")

    return PriceMeta(
        price_info=f"{latest:.2f}",
        change_info=f"{signed_number(change)} ({signed_percent(pct)})",
        time_stamp=stamp,
        price_change=change,
        latest_price=latest,
    )
