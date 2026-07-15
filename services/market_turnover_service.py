from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import pandas as pd
import requests


# 成交金額歷史資料屬於盤後資料，成功資料可快取 6 小時。
_CACHE_TTL_SECONDS = int(os.getenv("TWSE_TURNOVER_CACHE_TTL_SECONDS", str(60 * 60 * 6)))

# 若 TWSE 暫時失敗，也要快取「空結果」，避免每一位使用者都重新等 timeout。
_NEGATIVE_CACHE_TTL_SECONDS = int(
    os.getenv("TWSE_TURNOVER_NEGATIVE_CACHE_TTL_SECONDS", str(60 * 10))
)

# requests 支援 (connect timeout, read timeout)。
_CONNECT_TIMEOUT_SECONDS = float(
    os.getenv("TWSE_TURNOVER_CONNECT_TIMEOUT_SECONDS", "1.5")
)
_READ_TIMEOUT_SECONDS = float(
    os.getenv("TWSE_TURNOVER_READ_TIMEOUT_SECONDS", "3.0")
)

# 大盤圖通常只顯示最近 60 根；80 個交易日約落在 4 個月內。
_MAX_MONTHS = max(1, int(os.getenv("TWSE_TURNOVER_MAX_MONTHS", "4")))
_MAX_WORKERS = max(1, min(4, int(os.getenv("TWSE_TURNOVER_MAX_WORKERS", "4"))))

_TWSE_TURNOVER_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "data": {},
    "months": [],
    "initialized": False,
}

_TWSE_TURNOVER_LOCK = threading.Lock()

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; LINE-Stock-Bot/1.0; "
        "+https://www.twse.com.tw/)"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def _debug(*args: Any) -> None:
    print("DEBUG TWSE turnover |", *args, flush=True)


def _request_timeout() -> tuple[float, float]:
    return (
        max(0.2, _CONNECT_TIMEOUT_SECONDS),
        max(0.5, _READ_TIMEOUT_SECONDS),
    )


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value or "").strip()
        text = text.replace(",", "")
        text = text.replace("，", "")
        text = text.replace("億", "")
        text = text.replace("元", "")
        text = text.replace("--", "")

        if text in {"", "None", "nan"}:
            return default

        return float(text)

    except Exception:
        return default


def _parse_date(value: Any) -> str:
    """
    支援：
    - 2026-07-15
    - 2026/07/15
    - 20260715
    - 115/07/15（民國年）
    """
    text = str(value or "").strip()

    if not text:
        return ""

    text = text.replace(".", "/").replace("-", "/")

    try:
        if "/" in text:
            parts = text.split("/")

            if len(parts) >= 3:
                year = int(parts[0])
                month = int(parts[1])
                day = int(parts[2][:2])

                if year < 1911:
                    year += 1911

                return f"{year:04d}-{month:02d}-{day:02d}"

        digits = "".join(ch for ch in text if ch.isdigit())

        if len(digits) >= 8:
            year = int(digits[:4])
            month = int(digits[4:6])
            day = int(digits[6:8])
            return f"{year:04d}-{month:02d}-{day:02d}"

        if len(digits) == 7:
            year = int(digits[:3]) + 1911
            month = int(digits[3:5])
            day = int(digits[5:7])
            return f"{year:04d}-{month:02d}-{day:02d}"

    except Exception:
        return ""

    return ""


def _amount_to_yi(value: Any, key_name: str = "") -> float:
    """將成交金額統一換算成億元。"""
    num = _to_float(value, 0.0)

    if num <= 0:
        return 0.0

    if "億" in str(key_name or ""):
        return num

    if num >= 100_000_000:
        return num / 100_000_000

    return num


def _parse_fmtqik_payload(payload: Any) -> dict[str, float]:
    """
    FMTQIK 欄位通常為：
    日期、成交股數、成交金額、成交筆數、發行量加權股價指數、漲跌點數。
    """
    rows: list[Any] = []
    fields: list[Any] = []

    if isinstance(payload, dict):
        raw_rows = payload.get("data")
        raw_fields = payload.get("fields")

        if isinstance(raw_rows, list):
            rows = raw_rows
        if isinstance(raw_fields, list):
            fields = raw_fields

        # 新版 TWSE rwd API 有時放在 tables 內。
        if not rows:
            tables = payload.get("tables")

            if isinstance(tables, list):
                for table in tables:
                    if not isinstance(table, dict):
                        continue

                    table_rows = table.get("data")
                    table_fields = table.get("fields")

                    if isinstance(table_rows, list) and table_rows:
                        rows = table_rows
                        fields = table_fields if isinstance(table_fields, list) else []
                        break

    elif isinstance(payload, list):
        rows = payload

    result: dict[str, float] = {}

    amount_index = 2

    if fields:
        for idx, field in enumerate(fields):
            if "成交金額" in str(field):
                amount_index = idx
                break

    for row in rows:
        trade_date = ""
        amount = 0.0

        if isinstance(row, (list, tuple)):
            if not row:
                continue

            trade_date = _parse_date(row[0])

            if len(row) > amount_index:
                amount = _amount_to_yi(row[amount_index], "成交金額")

        elif isinstance(row, dict):
            for key, value in row.items():
                key_text = str(key)

                if not trade_date and (
                    "日期" in key_text
                    or key_text.lower() in {"date", "trade_date"}
                ):
                    trade_date = _parse_date(value)

                if amount <= 0 and "成交金額" in key_text:
                    amount = _amount_to_yi(value, key_text)

        if trade_date and amount > 0:
            result[trade_date] = amount

    return result


def _month_keys_from_dates(needed_dates: list[str] | None) -> list[str]:
    parsed_dates: list[datetime] = []

    for value in needed_dates or []:
        parsed = _parse_date(value)

        if not parsed:
            continue

        try:
            parsed_dates.append(datetime.strptime(parsed, "%Y-%m-%d"))
        except Exception:
            continue

    if not parsed_dates:
        parsed_dates = [datetime.now()]

    month_keys = sorted({dt.strftime("%Y%m01") for dt in parsed_dates})

    return month_keys[-_MAX_MONTHS:]


def _fetch_twse_fmtqik_month(month_key: str) -> dict[str, float]:
    """一次抓一個月的集中市場每日成交金額。"""
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
    params = {
        "response": "json",
        "date": str(month_key),
    }

    t0 = time.perf_counter()

    try:
        response = requests.get(
            url,
            params=params,
            headers=_REQUEST_HEADERS,
            timeout=_request_timeout(),
        )
        response.raise_for_status()

        result = _parse_fmtqik_payload(response.json())

        _debug(
            "month =",
            month_key[:6],
            "| rows =",
            len(result),
            "| sec =",
            round(time.perf_counter() - t0, 3),
        )

        return result

    except Exception as exc:
        _debug(
            "month failed =",
            month_key[:6],
            "| error =",
            repr(exc),
            "| sec =",
            round(time.perf_counter() - t0, 3),
        )
        return {}


def _fetch_needed_months(needed_dates: list[str] | None) -> dict[str, float]:
    month_keys = _month_keys_from_dates(needed_dates)

    if not month_keys:
        return {}

    result: dict[str, float] = {}
    worker_count = min(_MAX_WORKERS, len(month_keys))

    # 平行抓月份，最壞等待時間接近一次 request timeout，
    # 不會像原本逐日 fallback 一樣累積成數十秒。
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_fetch_twse_fmtqik_month, month_key): month_key
            for month_key in month_keys
        }

        for future in as_completed(futures):
            try:
                month_result = future.result()

                if month_result:
                    result.update(month_result)
            except Exception:
                continue

    return result


def get_twse_turnover_map(
    needed_dates: list[str] | None = None,
) -> dict[str, float]:
    """
    回傳集中市場每日成交金額 map，單位固定為億元。

    設計重點：
    - 成功與失敗都快取，避免 TWSE 暫時異常時每次查詢都等待。
    - 依需要日期按月份平行抓取，不再逐日補查。
    """
    required_months = _month_keys_from_dates(needed_dates)
    now = time.time()
    initialized = bool(_TWSE_TURNOVER_CACHE.get("initialized"))
    cached_data = dict(_TWSE_TURNOVER_CACHE.get("data") or {})
    cached_months = set(_TWSE_TURNOVER_CACHE.get("months") or [])
    cache_age = now - float(_TWSE_TURNOVER_CACHE.get("ts") or 0.0)
    cache_ttl = _CACHE_TTL_SECONDS if cached_data else _NEGATIVE_CACHE_TTL_SECONDS
    cache_covers_request = set(required_months).issubset(cached_months)

    if initialized and cache_covers_request and cache_age < cache_ttl:
        _debug(
            "cache hit = True",
            "| rows =",
            len(cached_data),
            "| age_sec =",
            round(cache_age, 1),
        )
        return cached_data

    wait_t0 = time.perf_counter()

    with _TWSE_TURNOVER_LOCK:
        lock_wait = time.perf_counter() - wait_t0

        # 其他 thread 可能已經完成刷新，再檢查一次。
        now = time.time()
        initialized = bool(_TWSE_TURNOVER_CACHE.get("initialized"))
        cached_data = dict(_TWSE_TURNOVER_CACHE.get("data") or {})
        cached_months = set(_TWSE_TURNOVER_CACHE.get("months") or [])
        cache_age = now - float(_TWSE_TURNOVER_CACHE.get("ts") or 0.0)
        cache_ttl = _CACHE_TTL_SECONDS if cached_data else _NEGATIVE_CACHE_TTL_SECONDS
        cache_covers_request = set(required_months).issubset(cached_months)

        if initialized and cache_covers_request and cache_age < cache_ttl:
            _debug(
                "cache hit after lock = True",
                "| rows =",
                len(cached_data),
                "| lock_wait_sec =",
                round(lock_wait, 3),
            )
            return cached_data

        fetch_t0 = time.perf_counter()
        result = _fetch_needed_months(needed_dates)
        fetch_sec = time.perf_counter() - fetch_t0

        _TWSE_TURNOVER_CACHE["ts"] = time.time()
        _TWSE_TURNOVER_CACHE["data"] = dict(result)
        _TWSE_TURNOVER_CACHE["months"] = list(required_months)
        _TWSE_TURNOVER_CACHE["initialized"] = True

        _debug(
            "map refreshed",
            "| rows =",
            len(result),
            "| latest =",
            sorted(result.items())[-3:] if result else [],
            "| fetch_sec =",
            round(fetch_sec, 3),
            "| lock_wait_sec =",
            round(lock_wait, 3),
            "| negative_cache =",
            not bool(result),
        )

        return dict(result)


def apply_twse_turnover_to_market_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    大盤專用：將 df["Volume"] 改成 TWSE 成交金額，單位億元。

    只覆蓋成功取得的日期；若 TWSE 暫時失敗，保留原始資料，
    並由 market_index_service 最後補上當日 Shioaji 成交金額。
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

    mapped_values = [
        turnover_map.get(idx.strftime("%Y-%m-%d"), None)
        for idx in work.index
    ]

    series = pd.Series(mapped_values, index=work.index, dtype="float64")

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
        values = pd.to_numeric(
            df["MarketTurnoverYI"],
            errors="coerce",
        ).dropna()

        if not values.empty:
            return float(values.iloc[-1])

    if "Volume" in df.columns:
        values = pd.to_numeric(df["Volume"], errors="coerce").dropna()

        if not values.empty:
            value = float(values.iloc[-1])

            if 100 <= value <= 100_000:
                return value

    return 0.0
