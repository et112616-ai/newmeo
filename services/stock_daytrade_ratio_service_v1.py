from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo
import os

import pandas as pd
import requests

try:
    from config import FINMIND_TOKEN
except Exception:
    FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")


FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
DAYTRADE_RATIO_VERSION = "2026-07-24-v1-FINMIND-OFFICIAL-DAYTRADE"
TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass
class DayTradingRatioSnapshot:
    available: bool
    message: str
    stock_id: str
    latest_date: str = ""
    daytrade_volume: int = 0
    total_volume: int = 0
    ratio_pct: float = 0.0
    buy_amount: int = 0
    sell_amount: int = 0
    publication_status: str = ""
    source: str = "FinMind / TWSE / TPEx"
    version: str = DAYTRADE_RATIO_VERSION


def _clean_stock_id(stock_id: str) -> str:
    return (
        str(stock_id or "")
        .replace(".TW", "")
        .replace(".TWO", "")
        .strip()
    )


def _safe_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        text = str(value).replace(",", "").strip()
        if text in {"", "--", "-", "nan", "None"}:
            return 0
        return int(round(float(text)))
    except Exception:
        return 0


def _finmind_rows(
    dataset: str,
    stock_id: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start_date,
        "end_date": end_date,
    }

    token = str(
        FINMIND_TOKEN or os.getenv("FINMIND_TOKEN", "") or ""
    ).strip()
    if token:
        params["token"] = token

    timeout_seconds = float(
        os.getenv("DAYTRADE_FINMIND_TIMEOUT_SECONDS", "8")
    )
    response = requests.get(
        FINMIND_URL,
        params=params,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        return []

    rows = payload.get("data") or []
    return [row for row in rows if isinstance(row, dict)]


def _volume_from_daily_df(
    daily_df: pd.DataFrame | None,
    target_date: str,
) -> int:
    if daily_df is None or daily_df.empty or "Volume" not in daily_df.columns:
        return 0

    work = daily_df.copy()
    if not isinstance(work.index, pd.DatetimeIndex):
        work.index = pd.to_datetime(work.index, errors="coerce")
        work = work[~work.index.isna()]

    if work.empty:
        return 0

    target = pd.Timestamp(target_date).date()
    matched = work[work.index.date == target]
    if matched.empty:
        return 0

    return _safe_int(matched["Volume"].iloc[-1])


def _volume_from_finmind_price(stock_id: str, target_date: str) -> int:
    try:
        rows = _finmind_rows(
            "TaiwanStockPrice",
            stock_id,
            target_date,
            target_date,
        )
    except Exception:
        return 0

    for row in reversed(rows):
        if str(row.get("date") or "")[:10] != target_date:
            continue
        return _safe_int(
            row.get("Trading_Volume")
            or row.get("trading_volume")
            or row.get("Volume")
        )

    return 0


def get_stock_daytrade_ratio(
    stock_id: str,
    daily_df: pd.DataFrame | None = None,
    now: datetime | None = None,
) -> DayTradingRatioSnapshot:
    sid = _clean_stock_id(stock_id)
    if not sid:
        return DayTradingRatioSnapshot(
            available=False,
            message="股票代碼空白，無法取得當沖占比。",
            stock_id=sid,
        )

    checked_at = now or datetime.now(TAIPEI)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=TAIPEI)
    else:
        checked_at = checked_at.astimezone(TAIPEI)

    end = checked_at.date()
    start = end - timedelta(days=14)

    try:
        rows = _finmind_rows(
            "TaiwanStockDayTrading",
            sid,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        print(
            "DEBUG stock daytrade ratio fetch failed",
            "| stock_id =",
            sid,
            "| error =",
            repr(exc),
            flush=True,
        )
        return DayTradingRatioSnapshot(
            available=False,
            message="官方當沖資料暫時無法取得。",
            stock_id=sid,
        )

    normalized: list[dict[str, Any]] = []
    for row in rows:
        row_date = str(row.get("date") or "")[:10]
        try:
            parsed_date = pd.Timestamp(row_date).date()
        except Exception:
            continue

        # 當日成交量值約在晚間 21:30 才更新；在此之前不把同日的
        # 0 視為真正的 0%，而是退回前一個已公布交易日。
        if (
            parsed_date == checked_at.date()
            and checked_at.time() < dt_time(21, 30)
        ):
            continue

        item = dict(row)
        item["_date"] = parsed_date
        normalized.append(item)

    if not normalized:
        return DayTradingRatioSnapshot(
            available=False,
            message="最新當沖成交量值尚未公布。",
            stock_id=sid,
        )

    normalized.sort(key=lambda item: item["_date"])
    latest = normalized[-1]
    latest_date = latest["_date"].strftime("%Y-%m-%d")
    daytrade_volume = _safe_int(
        latest.get("Volume")
        or latest.get("volume")
        or latest.get("Trading_Volume")
    )
    total_volume = _volume_from_daily_df(daily_df, latest_date)
    if total_volume <= 0:
        total_volume = _volume_from_finmind_price(sid, latest_date)

    # 少數備援行情來源可能以「張」回傳；若剛好差約1000倍，自動校正。
    if (
        total_volume > 0
        and daytrade_volume > total_volume
        and total_volume * 1000 >= daytrade_volume
    ):
        total_volume *= 1000

    if total_volume <= 0:
        return DayTradingRatioSnapshot(
            available=False,
            message="找不到同日總成交量，暫時無法計算當沖占比。",
            stock_id=sid,
            latest_date=latest_date,
            daytrade_volume=daytrade_volume,
        )

    ratio = daytrade_volume / total_volume * 100.0
    age_days = max((checked_at.date() - latest["_date"]).days, 0)
    publication_status = "初步資料" if age_days <= 2 else "已公布"

    print(
        "DEBUG stock daytrade ratio",
        "| version =",
        DAYTRADE_RATIO_VERSION,
        "| stock_id =",
        sid,
        "| date =",
        latest_date,
        "| daytrade_volume =",
        daytrade_volume,
        "| total_volume =",
        total_volume,
        "| ratio_pct =",
        round(ratio, 3),
        "| publication_status =",
        publication_status,
        flush=True,
    )

    return DayTradingRatioSnapshot(
        available=True,
        message="ok",
        stock_id=sid,
        latest_date=latest_date,
        daytrade_volume=daytrade_volume,
        total_volume=total_volume,
        ratio_pct=round(ratio, 2),
        buy_amount=_safe_int(latest.get("BuyAmount")),
        sell_amount=_safe_int(latest.get("SellAmount")),
        publication_status=publication_status,
    )
