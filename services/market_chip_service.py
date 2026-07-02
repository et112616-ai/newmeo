from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import requests


FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"

MARKET_CHIP_CACHE_TTL_SECONDS = 30 * 60
_MARKET_CHIP_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class MarketChipSnapshot:
    available: bool
    message: str

    latest_date: str = ""
    foreign: float = 0.0
    investment_trust: float = 0.0
    dealer: float = 0.0
    total: float = 0.0

    recent_rows: list[dict[str, Any]] = field(default_factory=list)
    source: str = "FinMind"
    unit: str = "億元"


def _debug(*args):
    print("DEBUG market_chip |", *args, flush=True)


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


def _to_yi(value: float) -> float:
    """
    FinMind 整體市場三大法人 buy / sell 為金額。
    這裡換算成億元顯示。
    """
    return float(value) / 100_000_000


def _snapshot_from_dict(data: dict[str, Any]) -> MarketChipSnapshot:
    return MarketChipSnapshot(
        available=bool(data.get("available")),
        message=str(data.get("message") or ""),

        latest_date=str(data.get("latest_date") or ""),
        foreign=_safe_float(data.get("foreign")),
        investment_trust=_safe_float(data.get("investment_trust")),
        dealer=_safe_float(data.get("dealer")),
        total=_safe_float(data.get("total")),

        recent_rows=list(data.get("recent_rows") or []),
        source=str(data.get("source") or "FinMind"),
        unit=str(data.get("unit") or "億元"),
    )


def get_market_chip_snapshot(days: int = 45) -> MarketChipSnapshot:
    """
    取得整體市場三大法人買賣超。
    回傳單位：億元。
    """
    cache_key = f"market_chip:{days}"
    now = time.time()

    cached = _MARKET_CHIP_CACHE.get(cache_key)

    if cached:
        ts, data = cached

        if now - ts <= MARKET_CHIP_CACHE_TTL_SECONDS:
            return _snapshot_from_dict(data)

    try:
        rows = _request_finmind_market_chip(days=days)

        if not rows:
            return MarketChipSnapshot(
                available=False,
                message="查無整體市場三大法人資料。",
            )

        parsed = _parse_market_chip_rows(rows)

        if not parsed:
            return MarketChipSnapshot(
                available=False,
                message="整體市場三大法人資料格式解析失敗。",
            )

        latest = parsed[-1]
        recent_rows = parsed[-8:]

        data = {
            "available": True,
            "message": "ok",
            "latest_date": latest["date"],
            "foreign": latest["foreign"],
            "investment_trust": latest["investment_trust"],
            "dealer": latest["dealer"],
            "total": latest["total"],
            "recent_rows": recent_rows,
            "source": "FinMind",
            "unit": "億元",
        }

        _MARKET_CHIP_CACHE[cache_key] = (now, data)

        _debug(
            "latest =",
            data["latest_date"],
            "foreign =",
            data["foreign"],
            "investment_trust =",
            data["investment_trust"],
            "dealer =",
            data["dealer"],
            "total =",
            data["total"],
        )

        return _snapshot_from_dict(data)

    except Exception as exc:
        _debug("failed", exc)

        return MarketChipSnapshot(
            available=False,
            message=f"取得整體市場三大法人資料失敗：{exc}",
        )


def _request_finmind_market_chip(days: int = 45) -> list[dict[str, Any]]:
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=max(days, 15))

    datasets = [
        "TaiwanStockTotalInstitutionalInvestors",
        "InstitutionalInvestors",
    ]

    last_error = ""

    for dataset in datasets:
        params = {
            "dataset": dataset,
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
                    _debug("dataset =", dataset, "rows =", len(data))
                    return list(data)

                last_error = str(payload.get("msg") or payload.get("message") or "")

        except Exception as exc:
            last_error = str(exc)
            _debug("request failed", dataset, exc)

    if last_error:
        _debug("no data", last_error)

    return []


def _parse_market_chip_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, float]] = {}

    for row in rows:
        date = str(row.get("date") or "").strip()
        name = str(row.get("name") or "").strip()

        if not date or not name:
            continue

        buy = _safe_float(row.get("buy"))
        sell = _safe_float(row.get("sell"))
        net_yi = _to_yi(buy - sell)

        item = grouped.setdefault(
            date,
            {
                "foreign": 0.0,
                "investment_trust": 0.0,
                "dealer": 0.0,
                "total": 0.0,
            },
        )

        # 外資：外資 + 外資自營商
        if name in {"Foreign_Investor", "Foreign_Dealer_Self"}:
            item["foreign"] += net_yi

        # 投信
        elif name == "Investment_Trust":
            item["investment_trust"] += net_yi

        # 自營商：舊制 Dealer + 新制自行買賣 + 避險
        elif name in {"Dealer", "Dealer_self", "Dealer_Hedging"}:
            item["dealer"] += net_yi

        else:
            # 未知分類不顯示，但仍保守計入合計
            pass

    parsed: list[dict[str, Any]] = []

    for date in sorted(grouped):
        item = grouped[date]
        total = (
            item["foreign"]
            + item["investment_trust"]
            + item["dealer"]
        )

        parsed.append(
            {
                "date": date,
                "foreign": round(item["foreign"], 2),
                "investment_trust": round(item["investment_trust"], 2),
                "dealer": round(item["dealer"], 2),
                "total": round(total, 2),
            }
        )

    return parsed
