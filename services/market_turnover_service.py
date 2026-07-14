from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import time

import pandas as pd
import requests


_TWSE_TURNOVER_CACHE = {
    "ts": 0.0,
    "data": {},
}

_CACHE_TTL_SECONDS = 60 * 60 * 6


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value or "").strip()
        text = text.replace(",", "")
        text = text.replace("，", "")
        text = text.replace("億", "")
        text = text.replace("元", "")
        text = text.replace("--", "")
        text = text.replace("-", "-")

        if text in {"", "None", "nan"}:
            return default

        return float(text)

    except Exception:
        return default


def _parse_date(value: Any) -> str:
    text = str(value or "").strip()

    if not text:
        return ""

    text = text.replace("/", "-")

    try:
        if len(text) >= 10 and "-" in text:
            return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")

        if len(text) >= 8 and text[:8].isdigit():
            return datetime.strptime(text[:8], "%Y%m%d").strftime("%Y-%m-%d")

    except Exception:
        return ""

    return ""


def _amount_to_yi(value: Any, key_name: str = "") -> float:
    """
    回傳單位固定為「億元」。

    TWSE 有些來源是元，有些欄位名稱可能已經是億元。
    這裡用數值大小與欄位名稱做防呆。
    """
    num = _to_float(value, 0.0)

    if num <= 0:
        return 0.0

    key = str(key_name or "")

    if "億" in key:
        return num

    # 如果是元，通常會是 1,000,000,000,000 這種等級
    if num >= 100_000_000:
        return num / 100_000_000

    # 如果已經是 11786.17 這種億元數字，就直接回傳
    return num


def _pick_date_from_row(row: dict) -> str:
    for key in row.keys():
        if "日期" in str(key) or str(key).lower() in {"date", "trade_date"}:
            parsed = _parse_date(row.get(key))
            if parsed:
                return parsed

    return ""


def _pick_turnover_from_row(row: dict) -> float:
    """
    優先抓上市成交金額。
    避免抓到上櫃、合計或交易量。
    """
    preferred_keys = []

    for key in row.keys():
        k = str(key)

        if "成交金額" not in k:
            continue

        if "上櫃" in k:
            continue

        if "合計" in k or "總計" in k:
            continue

        if "上市" in k or "集中" in k or "證交所" in k:
            preferred_keys.append(key)

    for key in preferred_keys:
        value = _amount_to_yi(row.get(key), str(key))
        if value > 0:
            return value

    # 如果沒有明確上市欄位，就退一步抓第一個成交金額欄位
    for key in row.keys():
        k = str(key)

        if "成交金額" not in k:
            continue

        if "上櫃" in k:
            continue

        value = _amount_to_yi(row.get(key), k)

        if value > 0:
            return value

    return 0.0


def _fetch_twse_openapi_mi_index4() -> dict[str, float]:
    """
    TWSE OpenAPI: /exchangeReport/MI_INDEX4
    官方 OpenAPI 清單中此項為每日上市上櫃跨市場成交資訊。
    回傳 dict:
        {
            "2026-07-14": 11786.17,
            ...
        }
    """
    url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX4"

    res = requests.get(url, timeout=20)
    res.raise_for_status()

    payload = res.json()

    if not isinstance(payload, list):
        return {}

    result: dict[str, float] = {}

    for row in payload:
        if not isinstance(row, dict):
            continue

        trade_date = _pick_date_from_row(row)
        turnover_yi = _pick_turnover_from_row(row)

        if trade_date and turnover_yi > 0:
            result[trade_date] = turnover_yi

    return result


def _find_turnover_in_any_json(obj: Any) -> float:
    """
    備援：掃 TWSE MI_INDEX JSON。
    只要找到「成交金額」附近的數字，就轉成億元。
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = str(key)

            if "成交金額" in k:
                amount = _amount_to_yi(value, k)
                if amount > 0:
                    return amount

            found = _find_turnover_in_any_json(value)
            if found > 0:
                return found

    if isinstance(obj, list):
        # 常見格式：["成交金額", "1,178,617,xxxxxx"]
        for i, value in enumerate(obj):
            if "成交金額" in str(value):
                for j in range(i + 1, min(i + 4, len(obj))):
                    amount = _amount_to_yi(obj[j], "成交金額")
                    if amount > 0:
                        return amount

        for value in obj:
            found = _find_turnover_in_any_json(value)
            if found > 0:
                return found

    return 0.0


def _fetch_twse_daily_turnover(trade_date: str) -> float:
    """
    備援：抓單日 TWSE MI_INDEX。
    trade_date: YYYY-MM-DD
    """
    try:
        dt = datetime.strptime(trade_date, "%Y-%m-%d")
    except Exception:
        return 0.0

    date_str = dt.strftime("%Y%m%d")

    urls = [
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
        "https://www.twse.com.tw/exchangeReport/MI_INDEX",
    ]

    params = {
        "response": "json",
        "date": date_str,
        "type": "MS",
    }

    for url in urls:
        try:
            res = requests.get(url, params=params, timeout=15)
            if res.status_code >= 400:
                continue

            payload = res.json()
            amount = _find_turnover_in_any_json(payload)

            if amount > 0:
                return amount

        except Exception:
            continue

    return 0.0


def get_twse_turnover_map(
    needed_dates: list[str] | None = None,
) -> dict[str, float]:
    """
    回傳成交金額 map，單位固定為億元。
    """
    now = time.time()

    if (
        _TWSE_TURNOVER_CACHE["data"]
        and now - float(_TWSE_TURNOVER_CACHE["ts"]) < _CACHE_TTL_SECONDS
    ):
        return dict(_TWSE_TURNOVER_CACHE["data"])

    result: dict[str, float] = {}

    try:
        result.update(_fetch_twse_openapi_mi_index4())
    except Exception as exc:
        print(
            "DEBUG TWSE MI_INDEX4 fetch failed",
            "| error =",
            repr(exc),
            flush=True,
        )

    # 如果 OpenAPI 沒有涵蓋到圖表需要的日期，就用單日 API 補最近幾天。
    if needed_dates:
        missing_dates = [
            d for d in needed_dates
            if d and d not in result
        ]

        # 避免一次打太多，只補最近 10 個缺失日期。
        for d in missing_dates[:10]:
            amount = _fetch_twse_daily_turnover(d)

            if amount > 0:
                result[d] = amount

    _TWSE_TURNOVER_CACHE["ts"] = now
    _TWSE_TURNOVER_CACHE["data"] = dict(result)

    print(
        "DEBUG TWSE turnover map",
        "| rows =",
        len(result),
        "| latest =",
        sorted(result.items())[-3:] if result else [],
        flush=True,
    )

    return result


def apply_twse_turnover_to_market_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    大盤專用：
    將 df["Volume"] 改成 TWSE 成交金額，單位億元。

    注意：
    - 個股不能用這個。
    - 大盤 K 線副圖要畫成交金額，不是 yfinance Volume。
    """
    if df is None or df.empty:
        return df

    work = df.copy()

    if not isinstance(work.index, pd.DatetimeIndex):
        try:
            work.index = pd.to_datetime(work.index)
        except Exception:
            return work

    date_keys = [
        d.strftime("%Y-%m-%d")
        for d in work.index[-80:]
        if pd.notna(d)
    ]

    turnover_map = get_twse_turnover_map(date_keys)

    if not turnover_map:
        return work

    mapped_values = []

    for idx in work.index:
        key = idx.strftime("%Y-%m-%d")
        mapped_values.append(turnover_map.get(key, None))

    series = pd.Series(mapped_values, index=work.index, dtype="float64")

    # 只覆蓋有抓到的日期，避免歷史全部變 NaN。
    if "Volume" not in work.columns:
        work["Volume"] = series
    else:
        old_volume = pd.to_numeric(work["Volume"], errors="coerce")
        work["Volume"] = series.where(series.notna(), old_volume)

    work["MarketTurnoverYI"] = series

    return work


def get_latest_market_turnover_yi(df: pd.DataFrame) -> float:
    if df is None or df.empty:
        return 0.0

    if "MarketTurnoverYI" in df.columns:
        s = pd.to_numeric(df["MarketTurnoverYI"], errors="coerce").dropna()

        if not s.empty:
            return float(s.iloc[-1])

    if "Volume" in df.columns:
        s = pd.to_numeric(df["Volume"], errors="coerce").dropna()

        if not s.empty:
            value = float(s.iloc[-1])

            # 大盤成交金額如果已經是億元，通常是幾千～幾萬。
            if 100 <= value <= 100000:
                return value

    return 0.0
