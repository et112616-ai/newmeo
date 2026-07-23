from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests


MARKET_MARGIN_SERVICE_VERSION = "2026-07-23-v4-TWSE-TPEX-SWITCH"
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
TPEX_MARGIN_API_URL = (
    "https://www.tpex.org.tw/web/stock/margin_trading/"
    "margin_balance/margin_bal_result.php"
)

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
    market_scope: str = "tse"
    market_name: str = "上市"
    has_margin_money: bool = True


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
        market_scope=_normalize_market_scope(data.get("market_scope")),
        market_name=str(data.get("market_name") or "上市"),
        has_margin_money=bool(data.get("has_margin_money", True)),
    )


def _normalize_market_scope(value: Any) -> str:
    text = str(value or "").strip().lower()

    if text in {"otc", "tpex", "上櫃", "櫃買"}:
        return "otc"

    return "tse"


def _market_name(market_scope: str) -> str:
    return "上櫃" if _normalize_market_scope(market_scope) == "otc" else "上市"


def get_market_margin_snapshot(
    days: int = 45,
    market_scope: str = "tse",
) -> MarketMarginSnapshot:
    """
    取得上市或上櫃市場融資融券資料。

    上市：
        FinMind TaiwanStockTotalMarginPurchaseShortSale。
    上櫃：
        TPEx 上櫃股票融資融券餘額，逐日彙總市場數字。
    """
    scope = _normalize_market_scope(market_scope)
    market_name = _market_name(scope)
    cache_key = f"market_margin:{scope}:{days}"
    now = time.time()

    cached = _MARKET_MARGIN_CACHE.get(cache_key)

    if cached:
        ts, data = cached

        if now - ts <= MARKET_MARGIN_CACHE_TTL_SECONDS:
            return _snapshot_from_dict(data)

    try:
        if scope == "otc":
            parsed = _request_tpex_market_margin(days=days)
            source = "TPEx"
            has_margin_money = False
        else:
            rows = _request_finmind_market_margin(days=days)
            parsed = _parse_market_margin_rows(rows)
            source = "FinMind / TWSE"
            has_margin_money = True

        if not parsed:
            return MarketMarginSnapshot(
                available=False,
                message=f"查無{market_name}融資融券資料。",
                market_scope=scope,
                market_name=market_name,
                source=source,
                has_margin_money=has_margin_money,
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

            "source": source,
            "market_scope": scope,
            "market_name": market_name,
            "has_margin_money": has_margin_money,
        }

        _MARKET_MARGIN_CACHE[cache_key] = (now, data)

        _debug(
            "version =",
            MARKET_MARGIN_SERVICE_VERSION,
            "market =",
            scope,
            "source =",
            source,
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
            message=f"取得{market_name}融資融券資料失敗：{exc}",
            market_scope=scope,
            market_name=market_name,
            source="TPEx" if scope == "otc" else "FinMind / TWSE",
            has_margin_money=scope != "otc",
        )


def _request_finmind_market_margin(days: int = 45) -> list[dict[str, Any]]:
    end_date = datetime.now(ZoneInfo("Asia/Taipei")).date()
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


def _to_roc_date(target_date) -> str:
    return f"{target_date.year - 1911:03d}/{target_date.month:02d}/{target_date.day:02d}"


def _request_tpex_margin_day(target_date) -> dict[str, Any] | None:
    params = {
        "l": "zh-tw",
        "o": "json",
        "d": _to_roc_date(target_date),
    }

    try:
        resp = requests.get(
            TPEX_MARGIN_API_URL,
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 market-margin-service/1.0",
            },
            timeout=8,
        )
        resp.raise_for_status()
        payload = resp.json()

        tables = payload.get("tables") or []

        for table in tables:
            rows = table.get("data") or []
            fields = table.get("fields") or []

            if not rows or not fields:
                continue

            parsed = _aggregate_tpex_margin_rows(
                rows=rows,
                fields=fields,
                date_text=str(payload.get("date") or ""),
                fallback_date=target_date.strftime("%Y-%m-%d"),
            )

            if parsed:
                return parsed

    except Exception as exc:
        _debug(
            "tpex day failed",
            "| date =",
            target_date,
            "| error =",
            repr(exc),
        )

    return None


def _request_tpex_market_margin(days: int = 45) -> list[dict[str, Any]]:
    """
    TPEx 舊版查詢端點可指定日期，這裡只抓最近 10 個平日，
    並行請求後保留最近 5 個有資料的交易日，避免逐檔再查行情。
    """
    end_date = datetime.now(ZoneInfo("Asia/Taipei")).date()
    target_dates = []
    cursor = end_date
    max_calendar_days = max(16, min(int(days or 45), 45))

    for _ in range(max_calendar_days):
        if cursor.weekday() < 5:
            target_dates.append(cursor)

        if len(target_dates) >= 10:
            break

        cursor -= timedelta(days=1)

    parsed_rows: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_request_tpex_margin_day, target_date): target_date
            for target_date in target_dates
        }

        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                _debug(
                    "tpex future failed",
                    "| date =",
                    futures[future],
                    "| error =",
                    repr(exc),
                )
                continue

            if row:
                parsed_rows.append(row)

    parsed_rows.sort(key=lambda item: str(item.get("date") or ""))
    result = parsed_rows[-5:]

    _debug(
        "tpex rows =",
        len(result),
        "requested_days =",
        len(target_dates),
        "first =",
        result[0]["date"] if result else "",
        "latest =",
        result[-1]["date"] if result else "",
    )

    return result


def _aggregate_tpex_margin_rows(
    rows: list[list[Any]],
    fields: list[str],
    date_text: str,
    fallback_date: str,
) -> dict[str, Any] | None:
    field_index = {
        str(field).replace("\n", "").replace(" ", "").strip(): index
        for index, field in enumerate(fields)
    }

    def _index(*names: str) -> int | None:
        for name in names:
            key = str(name).replace("\n", "").replace(" ", "").strip()

            if key in field_index:
                return field_index[key]

        return None

    indices = {
        "margin_yes": _index("前資餘額(張)", "前資餘額"),
        "margin_buy": _index("資買"),
        "margin_sell": _index("資賣"),
        "margin_return": _index("現償"),
        "margin_balance": _index("資餘額"),
        "short_yes": _index("前券餘額(張)", "前券餘額"),
        "short_sell": _index("券賣"),
        "short_buy": _index("券買"),
        "short_return": _index("券償"),
        "short_balance": _index("券餘額"),
    }

    required = {
        "margin_yes",
        "margin_balance",
        "short_yes",
        "short_balance",
    }

    if any(indices[name] is None for name in required):
        _debug("tpex schema mismatch", fields)
        return None

    totals = {name: 0 for name in indices}

    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue

        for name, index in indices.items():
            if index is not None and index < len(row):
                totals[name] += _safe_int(row[index])

    margin_balance = totals["margin_balance"]
    short_balance = totals["short_balance"]
    ratio = short_balance / margin_balance * 100 if margin_balance > 0 else 0.0

    iso_date = fallback_date
    compact_date = "".join(ch for ch in str(date_text) if ch.isdigit())

    if len(compact_date) == 8:
        iso_date = (
            f"{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:8]}"
        )

    return {
        "date": iso_date,
        "margin_balance": margin_balance,
        "margin_change": margin_balance - totals["margin_yes"],
        "margin_buy": totals["margin_buy"],
        "margin_sell": totals["margin_sell"],
        "margin_return": totals["margin_return"],
        "margin_money_balance": 0,
        "margin_money_change": 0,
        "short_balance": short_balance,
        "short_change": short_balance - totals["short_yes"],
        "short_buy": totals["short_buy"],
        "short_sell": totals["short_sell"],
        "short_return": totals["short_return"],
        "margin_short_ratio": round(ratio, 2),
    }


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
