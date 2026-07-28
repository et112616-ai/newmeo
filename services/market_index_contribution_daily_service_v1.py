from __future__ import annotations

import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from services.supabase_service import upsert_market_index_contribution_daily_rows


MARKET_INDEX_CONTRIBUTION_VERSION = (
    "2026-07-28-v1-DAILY-TSE-OTC-SIGNED-CONTRIBUTION"
)
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
HTTP_TIMEOUT_SECONDS = max(
    3.0,
    min(
        float(
            os.getenv(
                "MARKET_INDEX_CONTRIBUTION_HTTP_TIMEOUT_SECONDS",
                "10",
            )
        ),
        20.0,
    ),
)
TOP_CONTRIBUTOR_LIMIT = max(
    3,
    min(
        int(
            os.getenv(
                "MARKET_INDEX_CONTRIBUTION_TOP_LIMIT",
                "10",
            )
        ),
        20,
    ),
)

TWSE_COMPANY_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TWSE_DAILY_PRICE_URL = (
    "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
)
TWSE_DAILY_INDEX_URL = (
    "https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK"
)
TPEX_DAILY_QUOTES_URL = (
    "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
)
TPEX_DAILY_MARKET_VALUE_URL = (
    "https://www.tpex.org.tw/openapi/v1/tpex_daily_market_value"
)
TPEX_DAILY_INDEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_index"

_HTTP = requests.Session()
_HTTP.headers.update(
    {
        "User-Agent": "Mozilla/5.0 market-index-contribution/1.0",
        "Accept": "application/json,text/plain,*/*",
    }
)


def _debug(*args: Any) -> None:
    print("DEBUG market_index_contribution_daily |", *args, flush=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = (
            str(value if value is not None else "")
            .replace(",", "")
            .replace("%", "")
            .replace("\u3000", "")
            .strip()
        )
        if not text or text in {"-", "--", "nan", "None"}:
            return default
        number = float(text)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _pick(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    normalized = {
        re.sub(r"\s+", "", str(key)).lower(): value
        for key, value in row.items()
    }
    for key in keys:
        value = normalized.get(re.sub(r"\s+", "", key).lower())
        if value not in (None, ""):
            return value
    return None


def _iso_trade_date(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    try:
        if len(digits) == 7:
            year = int(digits[:3]) + 1911
            return f"{year:04d}-{digits[3:5]}-{digits[5:7]}"
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    except Exception:
        return ""
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _resolve_requested_date(value: str | None) -> str:
    if not value:
        return ""
    resolved = _iso_trade_date(value)
    if not resolved:
        raise ValueError("trade_date 格式必須是 YYYY-MM-DD")
    return resolved


def _request_rows(url: str) -> list[dict[str, Any]]:
    response = _HTTP.get(url, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "result", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _fetch_all_sources(
    scopes: set[str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    urls: dict[str, str] = {}
    if "tse" in scopes:
        urls.update(
            {
                "tse_company": TWSE_COMPANY_URL,
                "tse_price": TWSE_DAILY_PRICE_URL,
                "tse_index": TWSE_DAILY_INDEX_URL,
            }
        )
    if "otc" in scopes:
        urls.update(
            {
                "otc_quote": TPEX_DAILY_QUOTES_URL,
                "otc_market_value": TPEX_DAILY_MARKET_VALUE_URL,
                "otc_index": TPEX_DAILY_INDEX_URL,
            }
        )

    rows_by_key: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(6, len(urls))) as executor:
        futures = {
            executor.submit(_request_rows, url): key
            for key, url in urls.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                rows_by_key[key] = future.result()
            except Exception as exc:
                rows_by_key[key] = []
                errors[key] = repr(exc)
    return rows_by_key, errors


def _latest_row(
    rows: list[dict[str, Any]],
    date_keys: tuple[str, ...],
) -> tuple[dict[str, Any], str]:
    best_row: dict[str, Any] = {}
    best_date = ""
    for row in rows:
        value = _pick(row, *date_keys)
        row_date = _iso_trade_date(value)
        if row_date and row_date >= best_date:
            best_date = row_date
            best_row = row
    return best_row, best_date


def _clean_contributor(
    row: dict[str, Any],
    rank: int,
) -> dict[str, Any]:
    return {
        "rank": rank,
        "stock_id": str(row.get("stock_id") or ""),
        "stock_name": str(row.get("stock_name") or ""),
        "close_price": round(_safe_float(row.get("close_price")), 4),
        "previous_close": round(_safe_float(row.get("previous_close")), 4),
        "change": round(_safe_float(row.get("change")), 4),
        "return_pct": round(_safe_float(row.get("return_pct")), 6),
        "previous_weight_pct": round(
            _safe_float(row.get("previous_weight_ratio")) * 100.0,
            6,
        ),
        "contribution_points": round(
            _safe_float(row.get("contribution_points")),
            4,
        ),
    }


def _finalize_market(
    *,
    trade_date: str,
    market_scope: str,
    index_name: str,
    index_close: float,
    index_change: float,
    candidates: list[dict[str, Any]],
    source: str,
) -> dict[str, Any]:
    index_reference = index_close - index_change
    total_previous_market_cap = sum(
        _safe_float(row.get("previous_market_cap"))
        for row in candidates
    )
    if (
        index_reference <= 0
        or total_previous_market_cap <= 0
        or len(candidates) < 100
    ):
        return {
            "ok": False,
            "market_scope": market_scope,
            "trade_date": trade_date,
            "message": "有效成分股或指數資料不足",
            "universe_rows": len(candidates),
        }

    raw_total = 0.0
    for row in candidates:
        previous_weight_ratio = (
            _safe_float(row.get("previous_market_cap"))
            / total_previous_market_cap
        )
        raw_points = (
            index_reference
            * previous_weight_ratio
            * _safe_float(row.get("return_pct"))
            / 100.0
        )
        row["previous_weight_ratio"] = previous_weight_ratio
        row["raw_contribution_points"] = raw_points
        raw_total += raw_points

    scale_factor = 1.0
    reconciled = False
    if (
        abs(index_change) >= 0.01
        and abs(raw_total) >= 0.01
        and index_change * raw_total > 0
    ):
        candidate_scale = index_change / raw_total
        if 0.25 <= candidate_scale <= 4.0:
            scale_factor = candidate_scale
            reconciled = True

    for row in candidates:
        row["contribution_points"] = (
            _safe_float(row.get("raw_contribution_points"))
            * scale_factor
        )

    positive = sorted(
        (
            row
            for row in candidates
            if _safe_float(row.get("contribution_points")) > 0
        ),
        key=lambda row: _safe_float(row.get("contribution_points")),
        reverse=True,
    )
    negative = sorted(
        (
            row
            for row in candidates
            if _safe_float(row.get("contribution_points")) < 0
        ),
        key=lambda row: _safe_float(row.get("contribution_points")),
    )
    flat_count = len(candidates) - len(positive) - len(negative)
    positive_points = sum(
        _safe_float(row.get("contribution_points"))
        for row in positive
    )
    negative_points = sum(
        _safe_float(row.get("contribution_points"))
        for row in negative
    )
    net_points = positive_points + negative_points
    top_positive = [
        _clean_contributor(row, rank)
        for rank, row in enumerate(
            positive[:TOP_CONTRIBUTOR_LIMIT],
            start=1,
        )
    ]
    top_negative = [
        _clean_contributor(row, rank)
        for rank, row in enumerate(
            negative[:TOP_CONTRIBUTOR_LIMIT],
            start=1,
        )
    ]

    return {
        "ok": True,
        "trade_date": trade_date,
        "market_scope": market_scope,
        "index_name": index_name,
        "index_close": round(index_close, 4),
        "index_reference": round(index_reference, 4),
        "index_change_points": round(index_change, 4),
        "index_change_pct": round(
            (index_change / index_reference * 100.0)
            if index_reference > 0
            else 0.0,
            6,
        ),
        "positive_contribution_points": round(positive_points, 4),
        "negative_contribution_points": round(negative_points, 4),
        "net_contribution_points": round(net_points, 4),
        "raw_net_contribution_points": round(raw_total, 4),
        "reconciliation_scale": round(scale_factor, 10),
        "reconciled_to_index_change": reconciled,
        "positive_count": len(positive),
        "negative_count": len(negative),
        "flat_count": flat_count,
        "universe_rows": len(candidates),
        "contributors": {
            "positive": top_positive,
            "negative": top_negative,
        },
        "largest_positive": top_positive[0] if top_positive else {},
        "largest_negative": top_negative[0] if top_negative else {},
        "method": (
            "previous_close_market_cap_weight_"
            "reconciled_to_official_index_change"
            if reconciled
            else "previous_close_market_cap_weight_estimate"
        ),
        "is_official_component_contribution": False,
        "source": source,
        "note": (
            "依前一交易日市值權重估算個股點數，並以官方指數"
            "當日實際漲跌校準總和；個股貢獻為估算值。"
            if reconciled
            else "依前一交易日市值權重估算；個股貢獻為估算值。"
        ),
    }


def _build_tse(
    rows_by_key: dict[str, list[dict[str, Any]]],
    requested_date: str,
) -> dict[str, Any]:
    index_row, index_date = _latest_row(
        rows_by_key.get("tse_index", []),
        ("Date", "日期"),
    )
    price_row, price_date = _latest_row(
        rows_by_key.get("tse_price", []),
        ("Date", "日期"),
    )
    trade_date = min(
        value for value in (index_date, price_date) if value
    ) if index_date and price_date else ""
    if not trade_date:
        return {
            "ok": False,
            "market_scope": "tse",
            "message": "證交所每日行情或指數日期不足",
        }
    if requested_date and requested_date != trade_date:
        return {
            "ok": False,
            "market_scope": "tse",
            "trade_date": trade_date,
            "requested_trade_date": requested_date,
            "message": "指定日期與證交所最新完整交易日不一致",
        }

    shares_map: dict[str, dict[str, Any]] = {}
    for row in rows_by_key.get("tse_company", []):
        stock_id = str(
            _pick(row, "公司代號", "Code", "公司代碼") or ""
        ).strip()
        if not re.fullmatch(r"\d{4}", stock_id):
            continue
        shares = _safe_float(
            _pick(
                row,
                "已發行普通股數或TDR原股發行股數",
                "已發行普通股數",
                "IssuedShares",
            )
        )
        if shares <= 0:
            continue
        shares_map[stock_id] = {
            "shares": shares,
            "stock_name": str(
                _pick(row, "公司簡稱", "公司名稱", "Name") or ""
            ).strip(),
        }

    candidates: list[dict[str, Any]] = []
    for row in rows_by_key.get("tse_price", []):
        if _iso_trade_date(_pick(row, "Date", "日期")) != trade_date:
            continue
        stock_id = str(
            _pick(row, "Code", "證券代號", "公司代號") or ""
        ).strip()
        company = shares_map.get(stock_id)
        if company is None:
            continue
        close = _safe_float(
            _pick(row, "ClosingPrice", "收盤價", "Close")
        )
        change = _safe_float(_pick(row, "Change", "漲跌價差"))
        previous_close = close - change
        if close <= 0 or previous_close <= 0:
            continue
        shares = _safe_float(company.get("shares"))
        candidates.append(
            {
                "stock_id": stock_id,
                "stock_name": str(
                    _pick(row, "Name", "證券名稱")
                    or company.get("stock_name")
                    or stock_id
                ).strip(),
                "close_price": close,
                "previous_close": previous_close,
                "change": change,
                "return_pct": change / previous_close * 100.0,
                "previous_market_cap": previous_close * shares,
            }
        )

    index_close = _safe_float(_pick(index_row, "TAIEX", "收盤指數"))
    index_change = _safe_float(_pick(index_row, "Change", "漲跌點數"))
    return _finalize_market(
        trade_date=trade_date,
        market_scope="tse",
        index_name="發行量加權股價指數",
        index_close=index_close,
        index_change=index_change,
        candidates=candidates,
        source="TWSE_OPENAPI_STOCK_DAY_ALL+t187ap03_L+FMTQIK",
    )


def _build_otc(
    rows_by_key: dict[str, list[dict[str, Any]]],
    requested_date: str,
) -> dict[str, Any]:
    index_row, index_date = _latest_row(
        rows_by_key.get("otc_index", []),
        ("Date", "日期"),
    )
    _, quote_date = _latest_row(
        rows_by_key.get("otc_quote", []),
        ("Date", "日期"),
    )
    _, market_value_date = _latest_row(
        rows_by_key.get("otc_market_value", []),
        ("Date", "日期"),
    )
    dates = [value for value in (index_date, quote_date, market_value_date) if value]
    trade_date = min(dates) if len(dates) == 3 else ""
    if not trade_date:
        return {
            "ok": False,
            "market_scope": "otc",
            "message": "櫃買每日行情、市值或指數日期不足",
        }
    if requested_date and requested_date != trade_date:
        return {
            "ok": False,
            "market_scope": "otc",
            "trade_date": trade_date,
            "requested_trade_date": requested_date,
            "message": "指定日期與櫃買最新完整交易日不一致",
        }

    quote_map: dict[str, dict[str, Any]] = {}
    for row in rows_by_key.get("otc_quote", []):
        if _iso_trade_date(_pick(row, "Date", "日期")) != trade_date:
            continue
        stock_id = str(
            _pick(
                row,
                "SecuritiesCompanyCode",
                "SecuritiesCompanyCode",
                "證券代號",
            )
            or ""
        ).strip()
        if re.fullmatch(r"\d{4}", stock_id):
            quote_map[stock_id] = row

    candidates: list[dict[str, Any]] = []
    for row in rows_by_key.get("otc_market_value", []):
        if _iso_trade_date(_pick(row, "Date", "日期")) != trade_date:
            continue
        stock_id = str(
            _pick(row, "SecuritiesCompanyCode", "證券代號") or ""
        ).strip()
        quote = quote_map.get(stock_id)
        if quote is None:
            continue
        close = _safe_float(
            _pick(quote, "Close", "ClosingPrice", "收盤價")
        )
        change = _safe_float(_pick(quote, "Change", "漲跌價差"))
        previous_close = close - change
        shares = _safe_float(
            _pick(
                row,
                "Capitals",
                "發行股數",
                "IssuedShares",
            )
        )
        if close <= 0 or previous_close <= 0 or shares <= 0:
            continue
        candidates.append(
            {
                "stock_id": stock_id,
                "stock_name": str(
                    _pick(
                        row,
                        "CompanyName",
                        "公司名稱",
                    )
                    or _pick(quote, "CompanyName", "公司名稱")
                    or stock_id
                ).strip(),
                "close_price": close,
                "previous_close": previous_close,
                "change": change,
                "return_pct": change / previous_close * 100.0,
                "previous_market_cap": previous_close * shares,
            }
        )

    index_close = _safe_float(_pick(index_row, "Close", "TPExIndex"))
    index_change = _safe_float(_pick(index_row, "Change", "IndexChange"))
    return _finalize_market(
        trade_date=trade_date,
        market_scope="otc",
        index_name="櫃買指數",
        index_close=index_close,
        index_change=index_change,
        candidates=candidates,
        source=(
            "TPEX_OPENAPI_mainboard_quotes+"
            "daily_market_value+tpex_index"
        ),
    )


def _db_row(result: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "trade_date",
        "market_scope",
        "index_name",
        "index_close",
        "index_reference",
        "index_change_points",
        "index_change_pct",
        "positive_contribution_points",
        "negative_contribution_points",
        "net_contribution_points",
        "raw_net_contribution_points",
        "reconciliation_scale",
        "reconciled_to_index_change",
        "positive_count",
        "negative_count",
        "flat_count",
        "universe_rows",
        "contributors",
        "method",
        "is_official_component_contribution",
        "source",
        "note",
    }
    return {key: result.get(key) for key in keys}


def build_daily_market_index_contributions(
    trade_date: str | None = None,
    market_scope: str = "all",
    persist: bool = False,
) -> dict[str, Any]:
    """
    建立每日盤後「上市／上櫃」正負貢獻排行。

    這是盤後日資料，與 market_contribution_1m 的盤中模型特徵完全分離。
    """
    started = time.perf_counter()
    requested_date = _resolve_requested_date(trade_date)
    normalized_scope = str(market_scope or "all").strip().lower()
    if normalized_scope not in {"all", "tse", "otc"}:
        return {
            "ok": False,
            "message": "market_scope 僅支援 all、tse、otc",
            "version": MARKET_INDEX_CONTRIBUTION_VERSION,
        }
    scopes = {"tse", "otc"} if normalized_scope == "all" else {normalized_scope}
    rows_by_key, fetch_errors = _fetch_all_sources(scopes)

    results: dict[str, dict[str, Any]] = {}
    if "tse" in scopes:
        results["tse"] = _build_tse(rows_by_key, requested_date)
    if "otc" in scopes:
        results["otc"] = _build_otc(rows_by_key, requested_date)

    success_rows = [
        _db_row(result)
        for result in results.values()
        if bool(result.get("ok"))
    ]
    persist_result: dict[str, Any] = {
        "requested": persist,
        "success": False,
        "rows": 0,
    }
    if persist and success_rows:
        persist_result = {
            "requested": True,
            **upsert_market_index_contribution_daily_rows(success_rows),
        }

    ok = bool(success_rows) and all(
        bool(result.get("ok")) for result in results.values()
    )
    partial = bool(success_rows) and not ok
    response = {
        "ok": ok,
        "partial": partial,
        "message": "ok" if ok else ("部分市場完成" if partial else "盤後貢獻計算失敗"),
        "version": MARKET_INDEX_CONTRIBUTION_VERSION,
        "requested_trade_date": requested_date or None,
        "market_scope": normalized_scope,
        "generated_at": datetime.now(TAIPEI_TZ).isoformat(),
        "markets": results,
        "fetch_errors": fetch_errors,
        "persist": persist_result,
        "seconds": round(time.perf_counter() - started, 3),
    }
    _debug(
        "built",
        "| scope =", normalized_scope,
        "| ok =", ok,
        "| partial =", partial,
        "| persisted =", persist_result.get("success"),
        "| dates =",
        {
            key: value.get("trade_date")
            for key, value in results.items()
        },
        "| sec =", response["seconds"],
    )
    return response
