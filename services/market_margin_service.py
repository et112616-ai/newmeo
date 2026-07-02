from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import requests


FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"

MARKET_MARGIN_CACHE_TTL_SECONDS = 30 * 60
_MARKET_MARGIN_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class MarketMarginSnapshot:
    available: bool
    message: str

    latest_date: str = ""

    margin_balance: int = 0
    margin_change: int = 0
    margin_buy: int = 0
    margin_sell: int = 0
    margin_return: int = 0

    margin_money_balance: int = 0
    margin_money_change: int = 0

    short_balance: int = 0
    short_change: int = 0
    short_buy: int = 0
    short_sell: int = 0
    short_return: int = 0

    margin_short_ratio: float = 0.0

    recent_rows: list[dict[str, Any]] = field(default_factory=list)

    source: str = "FinMind"


def _debug(*args):
    print("DEBUG market_margin |", *args, flush=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        text = str(value).replace(",", "").strip()

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


def _snapshot_from_dict(data: dict[str, Any]) -> MarketMarginSnapshot:
    return MarketMarginSnapshot(
        available=bool(data.get("available")),
        message=str(data.get("message") or ""),

        latest_date=str(data.get("latest_date") or ""),

        margin_balance=_safe_int(data.get("margin_balance")),
        margin_change=_safe_int(data.get("margin_change")),
        margin_buy=_safe_int(data.get("margin_buy")),
        margin_sell=_safe_int(data.get("margin_sell")),
        margin_return=_safe_int(data.get("margin_return")),

        margin_money_balance=_safe_int(data.get("margin_money_balance")),
        margin_money_change=_safe_int(data.get("margin_money_change")),

        short_balance=_safe_int(data.get("short_balance")),
        short_change=_safe_int(data.get("short_change")),
        short_buy=_safe_int(data.get("short_buy")),
        short_sell=_safe_int(data.get("short_sell")),
        short_return=_safe_int(data.get("short_return")),

        margin_short_ratio=_safe_float(data.get("margin_short_ratio")),

        recent_rows=list(data.get("recent_rows") or []),

        source=str(data.get("source") or "FinMind"),
    )


def get_market_margin_snapshot(days: int = 45) -> MarketMarginSnapshot:
    """
    取得台灣整體市場融資融券資料。
    資料來源：FinMind TaiwanStockTotalMarginPurchaseShortSale。
    """
    cache_key = f"market_margin:{days}"
    now = time.time()

    cached = _MARKET_MARGIN_CACHE.get(cache_key)

    if cached:
        ts, data = cached

        if now - ts <= MARKET_MARGIN_CACHE_TTL_SECONDS:
            return _snapshot_from_dict(data)

    try:
        rows = _request_finmind_market_margin(days=days)

        if not rows:
            return MarketMarginSnapshot(
                available=False,
                message="查無大盤融資融券資料。",
            )

        parsed = _parse_market_margin_rows(rows)

        if not parsed:
            return MarketMarginSnapshot(
                available=False,
                message="大盤融資融券資料格式解析失敗。",
            )

        latest = parsed[-1]
        recent_rows = parsed[-5:]

        data = {
            "available": True,
            "message": "ok",

            "latest_date": latest["date"],

            "margin_balance": latest["margin_balance"],
            "margin_change": latest["margin_change"],
            "margin_buy": latest["margin_buy"],
            "margin_sell": latest["margin_sell"],
            "margin_return": latest["margin_return"],

            "margin_money_balance": latest["margin_money_balance"],
            "margin_money_change": latest["margin_money_change"],

            "short_balance": latest["short_balance"],
            "short_change": latest["short_change"],
            "short_buy": latest["short_buy"],
            "short_sell": latest["short_sell"],
            "short_return": latest["short_return"],

            "margin_short_ratio": latest["margin_short_ratio"],

            "recent_rows": recent_rows,

            "source": "FinMind",
        }

        _MARKET_MARGIN_CACHE[cache_key] = (now, data)

        _debug(
            "latest =",
            data["latest_date"],
            "margin_balance =",
            data["margin_balance"],
            "margin_change =",
            data["margin_change"],
            "short_balance =",
            data["short_balance"],
            "short_change =",
            data["short_change"],
            "ratio =",
            data["margin_short_ratio"],
        )

        return _snapshot_from_dict(data)

    except Exception as exc:
        _debug("failed", exc)

        return MarketMarginSnapshot(
            available=False,
            message=f"取得大盤融資融券資料失敗：{exc}",
        )


def _request_finmind_market_margin(days: int = 45) -> list[dict[str, Any]]:
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=max(days, 15))

    params = {
        "dataset": "TaiwanStockTotalMarginPurchaseShortSale",
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
    }

    headers = {}

    if FINMIND_TOKEN:
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"
        params["token"] = FINMIND_TOKEN

    try:
        resp = requests.get(
            FINMIND_API_URL,
            params=params,
            headers=headers,
            timeout=12,
        )
        resp.raise_for_status()

        payload = resp.json()

        if isinstance(payload, dict):
            data = payload.get("data") or []

            if data:
                _debug("rows =", len(data))
                return list(data)

            _debug("no data", payload.get("msg") or payload.get("message") or "")

    except Exception as exc:
        _debug("request failed", exc)

    return []


def _parse_market_margin_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}

    for row in rows:
        date = str(row.get("date") or "").strip()
        name = str(row.get("name") or "").strip()

        if not date or not name:
            continue

        grouped.setdefault(date, {})[name] = dict(row)

    parsed: list[dict[str, Any]] = []

    for date in sorted(grouped):
        item = grouped[date]

        margin = item.get("MarginPurchase") or {}
        margin_money = item.get("MarginPurchaseMoney") or {}
        short = item.get("ShortSale") or {}

        margin_balance = _safe_int(margin.get("TodayBalance"))
        margin_yes = _safe_int(margin.get("YesBalance"))
        margin_change = margin_balance - margin_yes

        margin_money_balance = _safe_int(margin_money.get("TodayBalance"))
        margin_money_yes = _safe_int(margin_money.get("YesBalance"))
        margin_money_change = margin_money_balance - margin_money_yes

        short_balance = _safe_int(short.get("TodayBalance"))
        short_yes = _safe_int(short.get("YesBalance"))
        short_change = short_balance - short_yes

        ratio = 0.0

        if margin_balance > 0:
            ratio = short_balance / margin_balance * 100

        parsed.append(
            {
                "date": date,

                "margin_balance": margin_balance,
                "margin_change": margin_change,
                "margin_buy": _safe_int(margin.get("buy")),
                "margin_sell": _safe_int(margin.get("sell")),
                "margin_return": _safe_int(margin.get("Return")),

                "margin_money_balance": margin_money_balance,
                "margin_money_change": margin_money_change,

                "short_balance": short_balance,
                "short_change": short_change,
                "short_buy": _safe_int(short.get("buy")),
                "short_sell": _safe_int(short.get("sell")),
                "short_return": _safe_int(short.get("Return")),

                "margin_short_ratio": round(ratio, 2),
            }
        )

    return parsed
