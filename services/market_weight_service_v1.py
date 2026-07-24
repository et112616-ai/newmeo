from __future__ import annotations

import math
import os
import queue
import re
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from services.market_index_service import get_market_index_snapshot
from services.sinopac_quote_service import get_api
from services.supabase_service import (
    get_market_contribution_history,
    get_latest_market_weight_rows,
    upsert_market_contribution_row,
    upsert_market_weight_rows,
)


MARKET_WEIGHT_VERSION = "2026-07-24-v1.7-MAKE-40S-SAFE"
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
TWSE_MIS_QUOTE_URL = (
    "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
)
HTTP_TIMEOUT_SECONDS = float(
    os.getenv("MARKET_WEIGHT_HTTP_TIMEOUT_SECONDS", "8")
)
SNAPSHOT_SOFT_DEADLINE_SECONDS = max(
    15.0,
    min(
        float(
            os.getenv(
                "MARKET_CONTRIBUTION_SOFT_DEADLINE_SECONDS",
                "30",
            )
        ),
        34.0,
    ),
)
PROVIDER_CALL_TIMEOUT_SECONDS = max(
    3.0,
    min(
        float(
            os.getenv(
                "MARKET_CONTRIBUTION_PROVIDER_TIMEOUT_SECONDS",
                "10",
            )
        ),
        15.0,
    ),
)
TOP_WEIGHT_LIMIT = max(
    5,
    min(int(os.getenv("MARKET_WEIGHT_TOP_LIMIT", "20")), 30),
)
MAX_STALE_MINUTES = max(
    1.0,
    min(
        float(
            os.getenv(
                "MARKET_CONTRIBUTION_MAX_STALE_MINUTES",
                "5",
            )
        ),
        30.0,
    ),
)
_HTTP = requests.Session()
_HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 market-weight-service/1.0",
    "Accept": "application/json,text/plain,*/*",
})


def _debug(*args: Any) -> None:
    print("DEBUG market_weight |", *args, flush=True)


class SnapshotDeadlineExceeded(TimeoutError):
    """盤中快照無法在 Make 40 秒限制前安全完成。"""


def _remaining_seconds(
    deadline: float,
    stage: str,
    cap: float | None = None,
) -> float:
    remaining = deadline - time.perf_counter()
    if remaining <= 0.25:
        raise SnapshotDeadlineExceeded(
            f"snapshot soft deadline exceeded before {stage}"
        )
    if cap is not None:
        remaining = min(remaining, cap)
    return max(0.25, remaining)


def _call_with_timeout(
    function,
    timeout_seconds: float,
    stage: str,
):
    """
    為沒有 timeout 參數的 Shioaji／Supabase 呼叫加上路由級截止時間。

    背景執行緒設為 daemon；主請求會在截止時間內回覆 Make，
    不會因單一供應商卡住而超過 40 秒。
    """
    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            result_queue.put((True, function()))
        except Exception as exc:
            result_queue.put((False, exc))

    worker = threading.Thread(
        target=runner,
        name=f"market-weight-{stage}",
        daemon=True,
    )
    worker.start()
    try:
        success, value = result_queue.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise SnapshotDeadlineExceeded(
            f"{stage} timed out after {timeout_seconds:.1f}s"
        ) from exc
    if success:
        return value
    raise value


def _safe_skip(
    message: str,
    reason: str,
    *,
    started: float,
    **extra: Any,
) -> dict[str, Any]:
    """合理略過仍以成功 HTTP 回覆，避免 Make 把它視為 DataError。"""
    return {
        "ok": True,
        "skipped": True,
        "skip_reason": reason,
        "message": message,
        "write_blocked": True,
        "version": MARKET_WEIGHT_VERSION,
        "seconds": round(time.perf_counter() - started, 3),
        **extra,
    }


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
    requested_date = (
        _resolve_trade_date(trade_date)
        if trade_date
        else ""
    )
    try:
        latest_index_quote = _mis_index_quote("tse_t00.tw")
    except Exception as exc:
        latest_index_quote = {}
        latest_index_error = repr(exc)
    else:
        latest_index_error = ""
    latest_trade_date = str(
        latest_index_quote.get("trade_date") or ""
    )
    if not latest_trade_date:
        return {
            "ok": False,
            "message": "無法辨識證交所最新交易日，本次不寫入",
            "requested_trade_date": requested_date,
            "error": latest_index_error,
            "version": MARKET_WEIGHT_VERSION,
        }
    if requested_date and requested_date != latest_trade_date:
        return {
            "ok": False,
            "message": "指定日期與證交所最新交易日不一致，本次不寫入",
            "requested_trade_date": requested_date,
            "latest_trade_date": latest_trade_date,
            "version": MARKET_WEIGHT_VERSION,
        }
    resolved_date = latest_trade_date
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
        "latest_trade_date": latest_trade_date,
        "requested_trade_date": requested_date or None,
        "trade_date_source": "TWSE_MIS_tse_t00.tw",
        "method": "close_price_x_issued_common_shares",
        "is_official_taiex_weight": False,
        "trade_date_note": (
            "交易日由證交所MIS自動辨識；未指定trade_date時直接採用"
            "最近交易日，指定日期不一致時拒絕寫入。"
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


def _yahoo_index_frame(
    symbol: str,
    timeout_seconds: float | None = None,
) -> pd.DataFrame:
    request_timeout = max(
        0.5,
        min(
            HTTP_TIMEOUT_SECONDS,
            float(timeout_seconds or HTTP_TIMEOUT_SECONDS),
        ),
    )
    response = _HTTP.get(
        YAHOO_CHART_URL.format(symbol=url_quote(symbol, safe="")),
        params={
            "range": "1d",
            "interval": "1m",
            "includePrePost": "false",
        },
        timeout=request_timeout,
    )
    response.raise_for_status()
    payload = response.json()
    result = (((payload.get("chart") or {}).get("result") or [None])[0])
    if not isinstance(result, dict):
        return pd.DataFrame()
    timestamps = result.get("timestamp") or []
    quote_data = (
        ((result.get("indicators") or {}).get("quote") or [{}])[0]
    )
    closes = quote_data.get("close") or []
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


def _mis_index_quote(
    ex_ch: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """取得交易所MIS最新指數報價；上櫃加權指數為 otc_o00.tw。"""
    request_timeout = max(
        0.5,
        min(
            HTTP_TIMEOUT_SECONDS,
            float(timeout_seconds or HTTP_TIMEOUT_SECONDS),
        ),
    )
    response = _HTTP.get(
        TWSE_MIS_QUOTE_URL,
        params={
            "ex_ch": ex_ch,
            "json": "1",
            "delay": "0",
        },
        timeout=request_timeout,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("msgArray") or []
    if not isinstance(rows, list) or not rows:
        return {}
    raw = rows[0] if isinstance(rows[0], dict) else {}
    close = _safe_float(raw.get("z"))
    if close <= 0:
        close = _safe_float(raw.get("y"))
    timestamp_ms = int(_safe_float(raw.get("tlong")))
    if close <= 0 or timestamp_ms <= 0:
        return {}
    ts = pd.Timestamp(timestamp_ms, unit="ms", tz="UTC").tz_convert(
        TAIPEI_TZ
    )
    raw_trade_date = str(raw.get("d") or "").strip()
    trade_date = ""
    if re.fullmatch(r"\d{8}", raw_trade_date):
        trade_date = pd.Timestamp(
            datetime.strptime(raw_trade_date, "%Y%m%d")
        ).strftime("%Y-%m-%d")
    if not trade_date:
        trade_date = ts.date().isoformat()
    return {
        "close": close,
        "previous_close": _safe_float(raw.get("y")),
        "ts": ts,
        "trade_date": trade_date,
        "name": str(raw.get("n") or ""),
        "source": "TWSE_MIS",
    }


def _session_status(now: datetime) -> dict[str, Any]:
    minute_of_day = now.hour * 60 + now.minute
    session_start = 9 * 60 + 5
    session_end = 13 * 60 + 30
    weekday_ok = now.weekday() < 5
    in_session = (
        weekday_ok
        and session_start <= minute_of_day <= session_end
    )
    return {
        "in_session": in_session,
        "weekday": now.weekday(),
        "session": "09:05-13:30 Asia/Taipei",
        "checked_at": now.isoformat(),
    }


def _as_taipei_timestamp(value: Any) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return None
        if ts.tzinfo is None:
            ts = ts.tz_localize(TAIPEI_TZ)
        return ts.tz_convert(TAIPEI_TZ)
    except Exception:
        return None


def _history_return_pct(
    current_close: float,
    current_ts: pd.Timestamp,
    history_rows: list[dict[str, Any]],
    minutes: int,
    value_key: str = "otc_close",
) -> float | None:
    """以目標分鐘前後2分鐘內、且早於目前時間的最近快照計算報酬。"""
    if current_close <= 0 or not history_rows:
        return None
    target_ts = current_ts - pd.Timedelta(minutes=minutes)
    candidates: list[tuple[pd.Timestamp, float]] = []
    for row in history_rows:
        try:
            ts = pd.Timestamp(row.get("ts"))
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            ts = ts.tz_convert(TAIPEI_TZ)
            close = _safe_float(row.get(value_key))
            if close > 0 and ts < current_ts:
                candidates.append((ts, close))
        except Exception:
            continue
    if not candidates:
        return None
    previous_ts, previous_close = min(
        candidates,
        key=lambda item: abs((item[0] - target_ts).total_seconds()),
    )
    lag_error_seconds = abs(
        (target_ts - previous_ts).total_seconds()
    )
    if lag_error_seconds > 2 * 60:
        return None
    return (current_close / previous_close - 1.0) * 100.0


def _build_market_contribution_snapshot(
    persist: bool = False,
    *,
    started: float,
    deadline: float,
) -> dict[str, Any]:
    """用前一交易日權重計算前20大即時貢獻，並加入櫃買強弱差。"""
    now = datetime.now(TAIPEI_TZ)
    trade_date = now.strftime("%Y-%m-%d")
    session = _session_status(now)
    if persist and not session["in_session"]:
        return _safe_skip(
            "目前不在盤中寫入時段，本次略過",
            "outside_session",
            started=started,
            session_status=session,
        )
    weights, weight_date = _call_with_timeout(
        lambda: get_latest_market_weight_rows(
            before_date=trade_date,
            limit=TOP_WEIGHT_LIMIT,
        ),
        _remaining_seconds(
            deadline,
            "market weights",
            PROVIDER_CALL_TIMEOUT_SECONDS,
        ),
        "market-weights",
    )
    if not weights:
        return {
            "ok": False,
            "message": "找不到早於今日的上市權重，請先執行盤後權重同步",
            "version": MARKET_WEIGHT_VERSION,
        }

    history_rows = _call_with_timeout(
        lambda: get_market_contribution_history(
            trade_date=trade_date,
            limit=120,
        ),
        _remaining_seconds(
            deadline,
            "contribution history",
            PROVIDER_CALL_TIMEOUT_SECONDS,
        ),
        "contribution-history",
    )
    if persist:
        current_minute = pd.Timestamp(now).floor("min")
        for history_row in history_rows:
            previous_ts = _as_taipei_timestamp(history_row.get("ts"))
            if previous_ts is None:
                continue
            if previous_ts.floor("min") == current_minute:
                return _safe_skip(
                    "本分鐘已有盤中特徵資料，本次略過重複寫入",
                    "duplicate_minute",
                    started=started,
                    session_status=session,
                    existing_ts=previous_ts.isoformat(),
                )

    stock_ids = [str(row.get("stock_id") or "") for row in weights]
    try:
        snapshots = _call_with_timeout(
            lambda: _batch_stock_snapshots(stock_ids),
            _remaining_seconds(
                deadline,
                "Shioaji stock snapshots",
                PROVIDER_CALL_TIMEOUT_SECONDS,
            ),
            "stock-snapshots",
        )
    except SnapshotDeadlineExceeded:
        raise
    except Exception as exc:
        return {
            "ok": False,
            "message": "Shioaji 前20大股票批次snapshot失敗",
            "error": repr(exc),
            "version": MARKET_WEIGHT_VERSION,
        }
    index_snapshot = _call_with_timeout(
        lambda: get_market_index_snapshot(with_chart=False),
        _remaining_seconds(
            deadline,
            "market index snapshot",
            PROVIDER_CALL_TIMEOUT_SECONDS,
        ),
        "market-index",
    )
    taiex_close = _safe_float(
        getattr(index_snapshot, "close_price", 0.0)
    )
    taiex_change = _safe_float(getattr(index_snapshot, "change", 0.0))
    taiex_reference = taiex_close - taiex_change
    if taiex_reference <= 0:
        taiex_reference = taiex_close
    taiex_quote: dict[str, Any] = {}
    try:
        taiex_quote = _mis_index_quote(
            "tse_t00.tw",
            timeout_seconds=_remaining_seconds(
                deadline,
                "TAIEX MIS",
                HTTP_TIMEOUT_SECONDS,
            ),
        )
    except SnapshotDeadlineExceeded:
        raise
    except Exception:
        taiex_quote = {}
    taiex_mis_close = _safe_float(taiex_quote.get("close"))
    taiex_mis_reference = _safe_float(
        taiex_quote.get("previous_close")
    )
    if taiex_mis_close > 0:
        taiex_close = taiex_mis_close
    if taiex_mis_reference > 0:
        taiex_reference = taiex_mis_reference

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
        twse_frame = _yahoo_index_frame(
            "^TWII",
            timeout_seconds=_remaining_seconds(
                deadline,
                "Yahoo TAIEX",
                HTTP_TIMEOUT_SECONDS,
            ),
        )
        otc_frame = _yahoo_index_frame(
            "^TWOII",
            timeout_seconds=_remaining_seconds(
                deadline,
                "Yahoo OTC",
                HTTP_TIMEOUT_SECONDS,
            ),
        )
    except SnapshotDeadlineExceeded:
        raise
    except Exception as exc:
        yahoo_error = repr(exc)
    if twse_frame.empty and not taiex_quote:
        return {
            "ok": False,
            "message": "上市指數分鐘資料不足，本次不寫入",
            "taiex_rows": len(twse_frame),
            "otc_rows": len(otc_frame),
            "error": yahoo_error,
            "version": MARKET_WEIGHT_VERSION,
        }

    taiex_source = (
        "TWSE_MIS_tse_t00.tw"
        if taiex_quote
        else "YAHOO_^TWII"
    )
    otc_source = "YAHOO_^TWOII"
    otc_quote: dict[str, Any] = {}
    if otc_frame.empty:
        try:
            otc_quote = _mis_index_quote(
                "otc_o00.tw",
                timeout_seconds=_remaining_seconds(
                    deadline,
                    "OTC MIS",
                    HTTP_TIMEOUT_SECONDS,
                ),
            )
        except SnapshotDeadlineExceeded:
            raise
        except Exception as exc:
            yahoo_error = " | ".join(
                value
                for value in (yahoo_error, f"MIS={repr(exc)}")
                if value
            )
        if not otc_quote:
            return {
                "ok": False,
                "message": "上櫃指數即時資料不足，本次不寫入",
                "taiex_rows": len(twse_frame),
                "otc_rows": 0,
                "error": yahoo_error,
                "version": MARKET_WEIGHT_VERSION,
            }
        otc_source = "TWSE_MIS_otc_o00.tw"
        otc_close = _safe_float(otc_quote.get("close"))
    else:
        otc_close = _safe_float(otc_frame["close"].iloc[-1])
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
    if taiex_quote.get("ts") is not None:
        latest_times.append(pd.Timestamp(taiex_quote["ts"]))
    if otc_quote.get("ts") is not None:
        latest_times.append(pd.Timestamp(otc_quote["ts"]))
    feature_ts = (
        max(latest_times).floor("min")
        if latest_times
        else pd.Timestamp(now).floor("min")
    )
    if feature_ts.tzinfo is None:
        feature_ts = feature_ts.tz_localize(TAIPEI_TZ)
    if persist:
        for history_row in history_rows:
            previous_ts = _as_taipei_timestamp(history_row.get("ts"))
            if previous_ts is None:
                continue
            if previous_ts.floor("min") == feature_ts.floor("min"):
                return _safe_skip(
                    "相同行情分鐘已有盤中特徵資料，本次略過重複寫入",
                    "duplicate_feature_timestamp",
                    started=started,
                    session_status=session,
                    existing_ts=previous_ts.isoformat(),
                    feature_ts=feature_ts.isoformat(),
                )
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
    taiex_latest_ts = _as_taipei_timestamp(
        taiex_quote.get("ts")
        if taiex_quote.get("ts") is not None
        else twse_frame.index[-1]
    )
    otc_latest_ts = _as_taipei_timestamp(
        otc_quote.get("ts")
        if otc_quote.get("ts") is not None
        else otc_frame.index[-1]
    )
    freshness_sources = {
        "taiex": taiex_latest_ts,
        "otc": otc_latest_ts,
    }
    stale_sources: list[str] = []
    freshness: dict[str, Any] = {
        "max_stale_minutes": MAX_STALE_MINUTES,
        "status": "fresh",
    }
    for source_name, source_ts in freshness_sources.items():
        if source_ts is None:
            stale_sources.append(source_name)
            freshness[f"{source_name}_ts"] = None
            freshness[f"{source_name}_age_minutes"] = None
            continue
        age_minutes = max(
            0.0,
            (
                pd.Timestamp(now) - source_ts
            ).total_seconds() / 60.0,
        )
        freshness[f"{source_name}_ts"] = source_ts.isoformat()
        freshness[f"{source_name}_age_minutes"] = round(
            age_minutes,
            3,
        )
        if (
            source_ts.date().isoformat() != trade_date
            or age_minutes > MAX_STALE_MINUTES
        ):
            stale_sources.append(source_name)
    if stale_sources:
        freshness["status"] = "stale"
        freshness["stale_sources"] = stale_sources
        if session["in_session"]:
            return _safe_skip(
                "上市或上櫃行情超過新鮮度門檻，本次略過",
                "stale_market_data",
                started=started,
                freshness=freshness,
                session_status=session,
            )
    if taiex_quote:
        taiex_return_5m = _history_return_pct(
            taiex_close,
            feature_ts,
            history_rows,
            5,
            value_key="taiex_close",
        )
        taiex_return_15m = _history_return_pct(
            taiex_close,
            feature_ts,
            history_rows,
            15,
            value_key="taiex_close",
        )
    else:
        taiex_return_5m = _return_pct(twse_frame, 5)
        taiex_return_15m = _return_pct(twse_frame, 15)
    if otc_frame.empty:
        otc_return_1m = _history_return_pct(
            otc_close, feature_ts, history_rows, 1
        )
        otc_return_5m = _history_return_pct(
            otc_close, feature_ts, history_rows, 5
        )
        otc_return_15m = _history_return_pct(
            otc_close, feature_ts, history_rows, 15
        )
    else:
        otc_return_1m = _return_pct(otc_frame, 1)
        otc_return_5m = _return_pct(otc_frame, 5)
        otc_return_15m = _return_pct(otc_frame, 15)

    def rounded_or_none(value: float | None) -> float | None:
        return round(float(value), 6) if value is not None else None

    divergence_5m = (
        taiex_return_5m - otc_return_5m
        if otc_return_5m is not None
        else None
    )
    divergence_15m = (
        taiex_return_15m - otc_return_15m
        if otc_return_15m is not None
        else None
    )
    otc_history_ready = otc_return_15m is not None
    otc_1m_ready = otc_return_1m is not None

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
        "otc_return_1m": rounded_or_none(otc_return_1m),
        "otc_return_5m": rounded_or_none(otc_return_5m),
        "otc_return_15m": rounded_or_none(otc_return_15m),
        "taiex_return_5m": rounded_or_none(taiex_return_5m),
        "taiex_return_15m": rounded_or_none(taiex_return_15m),
        "taiex_otc_divergence_5m": rounded_or_none(divergence_5m),
        "taiex_otc_divergence_15m": rounded_or_none(divergence_15m),
        "components": components,
        "source": f"SHIOAJI+{taiex_source}+{otc_source}",
    }
    persist_result = {
        "requested": persist,
        "success": False,
        "rows": 0,
    }
    if persist:
        write_result = _call_with_timeout(
            lambda: upsert_market_contribution_row(row),
            _remaining_seconds(
                deadline,
                "Supabase contribution write",
                PROVIDER_CALL_TIMEOUT_SECONDS,
            ),
            "contribution-write",
        )
        persist_result = {
            "requested": True,
            **write_result,
        }
    _debug(
        "contribution",
        "| weight_date =", weight_date,
        "| components =", len(components),
        "| contribution =", row["top20_contribution_points"],
        "| otc_15m =", row["otc_return_15m"],
        "| otc_source =", otc_source,
        "| otc_history_ready =", otc_history_ready,
        "| sec =", round(time.perf_counter() - started, 3),
    )
    return {
        "ok": True,
        "message": "ok",
        "version": MARKET_WEIGHT_VERSION,
        "feature": row,
        "component_rows": len(components),
        "otc_source": otc_source,
        "freshness": freshness,
        "session_status": session,
        "otc_history_ready": otc_history_ready,
        "otc_1m_ready": otc_1m_ready,
        "otc_history_rows": len(history_rows),
        "history_note": (
            "上櫃MIS僅提供最新值；第一次先保存價格，"
            "定期累積滿15分鐘後可取得15分鐘報酬；"
            "5分鐘報酬僅在約5分鐘前已有快照時提供；"
            "1分鐘報酬只有相隔約1分鐘的快照存在時才計算。"
            if not otc_history_ready
            else "上櫃15分鐘報酬已可由已存快照安全回推。"
        ),
        "yahoo_error": yahoo_error,
        "persist": persist_result,
        "seconds": round(time.perf_counter() - started, 3),
    }


def build_market_contribution_snapshot(
    persist: bool = False,
) -> dict[str, Any]:
    """
    Make 盤中收集入口。

    以 30 秒軟性截止保留 JSON 回覆時間；預期性略過一律回傳
    ok=true/skipped=true，真正未處理例外仍交由 app 回傳 500。
    """
    started = time.perf_counter()
    deadline = started + SNAPSHOT_SOFT_DEADLINE_SECONDS
    try:
        return _build_market_contribution_snapshot(
            persist=persist,
            started=started,
            deadline=deadline,
        )
    except SnapshotDeadlineExceeded as exc:
        _debug(
            "snapshot skipped by deadline",
            "| error =", repr(exc),
            "| sec =", round(time.perf_counter() - started, 3),
        )
        return _safe_skip(
            "行情來源處理接近 Make 40 秒上限，本次安全略過",
            "soft_deadline",
            started=started,
            error=str(exc),
            deadline_seconds=SNAPSHOT_SOFT_DEADLINE_SECONDS,
        )
