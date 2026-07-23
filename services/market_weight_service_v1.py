from __future__ import annotations

import math
import os
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from services.market_index_service import get_market_index_snapshot
from services.sinopac_quote_service import get_api
from services.supabase_service import (
    get_latest_market_weight_rows,
    upsert_market_contribution_row,
    upsert_market_weight_rows,
)


MARKET_WEIGHT_VERSION = "2026-07-23-v1-TWSE-TOP20-CONTRIBUTION"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TWSE_COMPANY_URL = (
    "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
)
TWSE_DAILY_PRICE_URL = (
    "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
)
YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
)
HTTP_TIMEOUT_SECONDS = float(
    os.getenv("MARKET_WEIGHT_HTTP_TIMEOUT_SECONDS", "8")
)
TOP_WEIGHT_LIMIT = max(
    5,
    min(int(os.getenv("MARKET_WEIGHT_TOP_LIMIT", "20")), 30),
)
_HTTP = requests.Session()
_HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 market-weight-service/1.0",
    "Accept": "application/json,text/plain,*/*",
})


def _debug(*args: Any) -> None:
    print("DEBUG market_weight |", *args, flush=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value if value is not None else "")
        text = (
            text.replace(",", "")
            .replace("%", "")
            .replace("\u3000", "")
            .strip()
        )
        if not text or text in {"-", "--", "nan", "None"}:
            return default
        result = float(text)
        return result if math.isfinite(result) else default
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


def _resolve_trade_date(value: str | None) -> str:
    if value:
        parsed = pd.Timestamp(value)
        if pd.isna(parsed):
            raise ValueError("trade_date 格式必須是 YYYY-MM-DD")
        return parsed.strftime("%Y-%m-%d")
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")


def build_daily_market_weights(
    trade_date: str | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    """用上市公司發行股數與最新盤後收盤價估算TAIEX市值權重。"""
    started = time.perf_counter()
    resolved_date = _resolve_trade_date(trade_date)
    try:
        company_rows = _request_rows(TWSE_COMPANY_URL)
        price_rows = _request_rows(TWSE_DAILY_PRICE_URL)
    except Exception as exc:
        return {
            "ok": False,
            "message": "TWSE OpenAPI 讀取失敗",
            "error": repr(exc),
            "version": MARKET_WEIGHT_VERSION,
        }

    shares_map: dict[str, dict[str, Any]] = {}
    for row in company_rows:
        stock_id = str(
            _pick(row, "公司代號", "Code", "公司代碼") or ""
        ).strip()
        if not re.fullmatch(r"\d{4}", stock_id):
            continue
        issued_shares = _safe_float(
            _pick(
                row,
                "已發行普通股數或TDR原股發行股數",
                "已發行普通股數",
                "IssuedShares",
            )
        )
        if issued_shares <= 0:
            continue
        shares_map[stock_id] = {
            "stock_name": str(
                _pick(row, "公司簡稱", "公司名稱", "Name") or ""
            ).strip(),
            "issued_shares": issued_shares,
        }

    candidates: list[dict[str, Any]] = []
    for row in price_rows:
        stock_id = str(
            _pick(row, "Code", "證券代號", "公司代號") or ""
        ).strip()
        company = shares_map.get(stock_id)
        if company is None:
            continue
        close_price = _safe_float(
            _pick(row, "ClosingPrice", "收盤價", "Close")
        )
        if close_price <= 0:
            continue
        market_cap = close_price * float(company["issued_shares"])
        candidates.append({
            "trade_date": resolved_date,
            "stock_id": stock_id,
            "stock_name": str(
                _pick(row, "Name", "證券名稱") or company["stock_name"]
            ).strip(),
            "close_price": round(close_price, 4),
            "issued_shares": int(round(float(company["issued_shares"]))),
            "market_cap": int(round(market_cap)),
            "_market_cap": market_cap,
        })

    total_market_cap = sum(row["_market_cap"] for row in candidates)
    if total_market_cap <= 0 or len(candidates) < 100:
        return {
            "ok": False,
            "message": "有效上市普通股資料不足",
            "company_rows": len(company_rows),
            "price_rows": len(price_rows),
            "matched_rows": len(candidates),
            "version": MARKET_WEIGHT_VERSION,
        }

    candidates.sort(key=lambda row: row["_market_cap"], reverse=True)
    top_rows: list[dict[str, Any]] = []
    for rank, row in enumerate(candidates[:TOP_WEIGHT_LIMIT], start=1):
        clean = {key: value for key, value in row.items() if key != "_market_cap"}
        clean["weight_ratio"] = round(
            float(row["_market_cap"] / total_market_cap),
            10,
        )
        clean["weight_rank"] = rank
        clean["source"] = "TWSE_OPENAPI_APPROX"
        top_rows.append(clean)

    persist_result = {
        "requested": persist,
        "success": False,
        "rows": 0,
    }
    if persist:
        persist_result = {
            "requested": True,
            **upsert_market_weight_rows(top_rows),
        }

    return {
        "ok": True,
        "message": "ok",
        "version": MARKET_WEIGHT_VERSION,
        "trade_date": resolved_date,
        "method": "close_price_x_issued_common_shares",
        "is_official_taiex_weight": False,
        "trade_date_note": (
            "STOCK_DAY_ALL未附交易日；請只在盤後以實際交易日呼叫，"
            "不可預填未來日期。"
        ),
        "weight_note": (
            "以證交所公開收盤價與已發行普通股數估算；"
            "供模型特徵使用，不宣稱為官方即時TAIEX權重。"
        ),
        "universe_rows": len(candidates),
        "top_rows": len(top_rows),
        "top_weight_coverage_pct": round(
            sum(float(row["weight_ratio"]) for row in top_rows) * 100.0,
            4,
        ),
        "weights": top_rows,
        "persist": persist_result,
        "seconds": round(time.perf_counter() - started, 3),
    }


def _stock_contract(api: Any, stock_id: str) -> Any:
    try:
        return api.Contracts.Stocks[stock_id]
    except Exception:
        pass
    try:
        return api.Contracts.Stocks.TSE[stock_id]
    except Exception:
        return None


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        return dict(value)
    except Exception:
        pass
    try:
        return dict(value.__dict__)
    except Exception:
        return {}


def _batch_stock_snapshots(
    stock_ids: list[str],
) -> dict[str, dict[str, Any]]:
    api = get_api()
    if api is None:
        return {}
    contracts = []
    contract_ids: list[str] = []
    for stock_id in stock_ids:
        contract = _stock_contract(api, stock_id)
        if contract is not None:
            contracts.append(contract)
            contract_ids.append(stock_id)
    if not contracts:
        return {}
    snapshots = api.snapshots(contracts) or []
    result: dict[str, dict[str, Any]] = {}
    for fallback_id, snapshot in zip(contract_ids, snapshots):
        raw = _to_dict(snapshot)
        stock_id = str(
            raw.get("code")
            or raw.get("stock_id")
            or fallback_id
        ).strip()
        close = _safe_float(
            raw.get("close")
            or raw.get("last_price")
            or raw.get("price")
        )
        if close <= 0:
            continue
        result[stock_id] = {
            "close": close,
            "ts": str(
                raw.get("ts")
                or raw.get("timestamp")
                or ""
            ),
        }
    return result


def _yahoo_index_frame(symbol: str) -> pd.DataFrame:
    response = _HTTP.get(
        YAHOO_CHART_URL.format(symbol=quote(symbol, safe="")),
        params={
            "range": "1d",
            "interval": "1m",
            "includePrePost": "false",
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    result = (((payload.get("chart") or {}).get("result") or [None])[0])
    if not isinstance(result, dict):
        return pd.DataFrame()
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0])
    closes = quote.get("close") or []
    size = min(len(timestamps), len(closes))
    if size <= 0:
        return pd.DataFrame()
    frame = pd.DataFrame({
        "ts": pd.to_datetime(timestamps[:size], unit="s", utc=True),
        "close": pd.to_numeric(closes[:size], errors="coerce"),
    }).dropna()
    if frame.empty:
        return frame
    frame["ts"] = frame["ts"].dt.tz_convert(TAIPEI_TZ)
    return frame.set_index("ts").sort_index()


def _return_pct(frame: pd.DataFrame, minutes: int) -> float:
    if frame.empty or len(frame) <= minutes:
        return 0.0
    latest = _safe_float(frame["close"].iloc[-1])
    previous = _safe_float(frame["close"].iloc[-1 - minutes])
    if latest <= 0 or previous <= 0:
        return 0.0
    return (latest / previous - 1.0) * 100.0


def build_market_contribution_snapshot(
    persist: bool = False,
) -> dict[str, Any]:
    """用前一交易日權重計算前20大即時貢獻，並加入櫃買強弱差。"""
    started = time.perf_counter()
    now = datetime.now(TAIPEI_TZ)
    trade_date = now.strftime("%Y-%m-%d")
    weights, weight_date = get_latest_market_weight_rows(
        before_date=trade_date,
        limit=TOP_WEIGHT_LIMIT,
    )
    if not weights:
        return {
            "ok": False,
            "message": "找不到早於今日的上市權重，請先執行盤後權重同步",
            "version": MARKET_WEIGHT_VERSION,
        }

    stock_ids = [str(row.get("stock_id") or "") for row in weights]
    try:
        snapshots = _batch_stock_snapshots(stock_ids)
    except Exception as exc:
        return {
            "ok": False,
            "message": "Shioaji 前20大股票批次snapshot失敗",
            "error": repr(exc),
            "version": MARKET_WEIGHT_VERSION,
        }
    index_snapshot = get_market_index_snapshot(with_chart=False)
    taiex_close = _safe_float(
        getattr(index_snapshot, "close_price", 0.0)
    )
    taiex_change = _safe_float(getattr(index_snapshot, "change", 0.0))
    taiex_reference = taiex_close - taiex_change
    if taiex_reference <= 0:
        taiex_reference = taiex_close

    components: list[dict[str, Any]] = []
    for row in weights:
        stock_id = str(row.get("stock_id") or "")
        snapshot = snapshots.get(stock_id)
        reference_close = _safe_float(row.get("close_price"))
        current_close = _safe_float(
            snapshot.get("close") if snapshot else 0.0
        )
        if reference_close <= 0 or current_close <= 0:
            continue
        weight_ratio = _safe_float(row.get("weight_ratio"))
        return_pct = (current_close / reference_close - 1.0) * 100.0
        contribution = (
            taiex_reference * weight_ratio * return_pct / 100.0
        )
        components.append({
            "stock_id": stock_id,
            "stock_name": str(row.get("stock_name") or ""),
            "weight_rank": int(_safe_float(row.get("weight_rank"))),
            "weight_pct": round(weight_ratio * 100.0, 4),
            "reference_close": round(reference_close, 4),
            "current_close": round(current_close, 4),
            "return_pct": round(return_pct, 6),
            "contribution_points": round(contribution, 4),
        })
    if len(components) < max(5, len(weights) // 2):
        return {
            "ok": False,
            "message": "有效權值股即時報價不足",
            "weight_rows": len(weights),
            "snapshot_rows": len(snapshots),
            "component_rows": len(components),
            "version": MARKET_WEIGHT_VERSION,
        }

    twse_frame = pd.DataFrame()
    otc_frame = pd.DataFrame()
    yahoo_error = ""
    try:
        twse_frame = _yahoo_index_frame("^TWII")
        otc_frame = _yahoo_index_frame("^TWOII")
    except Exception as exc:
        yahoo_error = repr(exc)
    if twse_frame.empty or otc_frame.empty:
        return {
            "ok": False,
            "message": "上市或上櫃指數分鐘資料不足，本次不寫入",
            "taiex_rows": len(twse_frame),
            "otc_rows": len(otc_frame),
            "error": yahoo_error,
            "version": MARKET_WEIGHT_VERSION,
        }

    taiex_return_5m = _return_pct(twse_frame, 5)
    taiex_return_15m = _return_pct(twse_frame, 15)
    otc_return_1m = _return_pct(otc_frame, 1)
    otc_return_5m = _return_pct(otc_frame, 5)
    otc_return_15m = _return_pct(otc_frame, 15)
    otc_close = (
        _safe_float(otc_frame["close"].iloc[-1])
        if not otc_frame.empty
        else 0.0
    )
    total_weight = sum(float(item["weight_pct"]) for item in components)
    positive_weight = sum(
        float(item["weight_pct"])
        for item in components
        if float(item["return_pct"]) > 0
    )
    negative_weight = sum(
        float(item["weight_pct"])
        for item in components
        if float(item["return_pct"]) < 0
    )
    largest = max(
        components,
        key=lambda item: abs(float(item["contribution_points"])),
    )
    latest_times = [
        frame.index[-1]
        for frame in (twse_frame, otc_frame)
        if not frame.empty
    ]
    feature_ts = (
        max(latest_times).floor("min")
        if latest_times
        else pd.Timestamp(now).floor("min")
    )
    if feature_ts.tzinfo is None:
        feature_ts = feature_ts.tz_localize(TAIPEI_TZ)
    feature_trade_date = feature_ts.tz_convert(TAIPEI_TZ).date().isoformat()
    if feature_trade_date != trade_date:
        return {
            "ok": False,
            "message": "目前取得的指數分鐘資料不是今天，休市或行情尚未更新時不寫入",
            "expected_trade_date": trade_date,
            "latest_feature_trade_date": feature_trade_date,
            "latest_feature_ts": feature_ts.isoformat(),
            "version": MARKET_WEIGHT_VERSION,
        }

    row = {
        "ts": feature_ts.isoformat(),
        "trade_date": trade_date,
        "weight_trade_date": weight_date,
        "taiex_close": round(taiex_close, 4),
        "taiex_reference": round(taiex_reference, 4),
        "top20_market_weight_pct": round(total_weight, 4),
        "top20_contribution_points": round(
            sum(float(item["contribution_points"]) for item in components),
            4,
        ),
        "top20_positive_weight_ratio_pct": round(
            positive_weight / total_weight * 100.0
            if total_weight > 0
            else 0.0,
            4,
        ),
        "top20_negative_weight_ratio_pct": round(
            negative_weight / total_weight * 100.0
            if total_weight > 0
            else 0.0,
            4,
        ),
        "largest_stock_id": str(largest["stock_id"]),
        "largest_contribution_points": round(
            float(largest["contribution_points"]),
            4,
        ),
        "otc_close": round(otc_close, 4),
        "otc_return_1m": round(otc_return_1m, 6),
        "otc_return_5m": round(otc_return_5m, 6),
        "otc_return_15m": round(otc_return_15m, 6),
        "taiex_return_5m": round(taiex_return_5m, 6),
        "taiex_return_15m": round(taiex_return_15m, 6),
        "taiex_otc_divergence_5m": round(
            taiex_return_5m - otc_return_5m,
            6,
        ),
        "taiex_otc_divergence_15m": round(
            taiex_return_15m - otc_return_15m,
            6,
        ),
        "components": components,
        "source": "SHIOAJI+YAHOO",
    }
    persist_result = {
        "requested": persist,
        "success": False,
        "rows": 0,
    }
    if persist:
        persist_result = {
            "requested": True,
            **upsert_market_contribution_row(row),
        }
    _debug(
        "contribution",
        "| weight_date =", weight_date,
        "| components =", len(components),
        "| contribution =", row["top20_contribution_points"],
        "| otc_15m =", row["otc_return_15m"],
        "| sec =", round(time.perf_counter() - started, 3),
    )
    return {
        "ok": True,
        "message": "ok",
        "version": MARKET_WEIGHT_VERSION,
        "feature": row,
        "component_rows": len(components),
        "yahoo_error": yahoo_error,
        "persist": persist_result,
        "seconds": round(time.perf_counter() - started, 3),
    }
