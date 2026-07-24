from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo


MARKET_AFTERHOURS_VERSION = "2026-07-24-v1-MARKET-CLOSE-DIGEST"
TAIPEI_TZ = "Asia/Taipei"


def _debug(*args: Any) -> None:
    print("DEBUG market_afterhours_digest |", *args, flush=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else default
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, float(default))))
    except Exception:
        return default


def _snapshot_dict(snapshot: Any) -> dict[str, Any]:
    if snapshot is None:
        return {}
    if isinstance(snapshot, dict):
        return dict(snapshot)
    result: dict[str, Any] = {}
    for key in dir(snapshot):
        if key.startswith("_"):
            continue
        try:
            value = getattr(snapshot, key)
            if not callable(value):
                result[key] = value
        except Exception:
            continue
    return result


def _safe_call(
    label: str,
    callback: Callable[[], Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        value = callback()
        result = _snapshot_dict(value)
        result["_call_ok"] = True
        result["_call_seconds"] = round(time.perf_counter() - started, 3)
        return result
    except Exception as exc:
        _debug(label, "failed", "| error =", repr(exc))
        return {
            "available": False,
            "message": repr(exc),
            "_call_ok": False,
            "_call_seconds": round(time.perf_counter() - started, 3),
        }


def _latest_contribution_row() -> dict[str, Any]:
    try:
        from services.supabase_service import get_supabase_client

        client = get_supabase_client()
        if client is None:
            return {}
        response = (
            client.table("market_contribution_1m")
            .select(
                "ts,trade_date,weight_trade_date,taiex_close,"
                "taiex_reference,top20_market_weight_pct,"
                "top20_contribution_points,"
                "top20_positive_weight_ratio_pct,"
                "top20_negative_weight_ratio_pct,"
                "largest_stock_id,largest_contribution_points,"
                "otc_close,otc_return_5m,otc_return_15m,"
                "taiex_return_5m,taiex_return_15m,"
                "taiex_otc_divergence_5m,"
                "taiex_otc_divergence_15m,components,source"
            )
            .order("ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
    except Exception as exc:
        _debug("latest contribution failed", "| error =", repr(exc))
    return {}


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(TAIPEI_TZ))
        return parsed.astimezone(ZoneInfo(TAIPEI_TZ))
    except Exception:
        return None


def _future_phase(quote_time: Any) -> str:
    parsed = _parse_time(quote_time)
    if parsed is None:
        return "期貨資料"
    minute = parsed.hour * 60 + parsed.minute
    if minute >= 15 * 60 or minute < 5 * 60:
        return "夜盤即時"
    if minute >= 13 * 60 + 30:
        return "日盤收盤"
    return "日盤資料"


def _market_levels(
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
) -> dict[str, Any]:
    valid = (
        close_price > 0
        and high_price > 0
        and low_price > 0
        and high_price >= low_price
    )
    if not valid:
        return {
            "range_points": 0.0,
            "range_pct": 0.0,
            "close_position_pct": 0.0,
            "close_position_label": "區間資料不足",
            "pivot": 0.0,
            "resistance_1": 0.0,
            "support_1": 0.0,
        }

    range_points = high_price - low_price
    range_pct = (
        range_points / open_price * 100.0
        if open_price > 0
        else 0.0
    )
    close_position = (
        (close_price - low_price) / range_points * 100.0
        if range_points > 0
        else 50.0
    )
    if close_position >= 70:
        position_label = "收在日內高檔"
    elif close_position <= 30:
        position_label = "收在日內低檔"
    else:
        position_label = "收在區間中段"

    pivot = (high_price + low_price + close_price) / 3.0
    resistance_1 = 2.0 * pivot - low_price
    support_1 = 2.0 * pivot - high_price
    return {
        "range_points": round(range_points, 2),
        "range_pct": round(range_pct, 3),
        "close_position_pct": round(close_position, 1),
        "close_position_label": position_label,
        "pivot": round(pivot, 2),
        "resistance_1": round(resistance_1, 2),
        "support_1": round(support_1, 2),
    }


def _largest_component(
    contribution: dict[str, Any],
) -> dict[str, Any]:
    components = contribution.get("components") or []
    if isinstance(components, list):
        stock_id = str(contribution.get("largest_stock_id") or "")
        for item in components:
            if (
                isinstance(item, dict)
                and str(item.get("stock_id") or "") == stock_id
            ):
                return {
                    "stock_id": stock_id,
                    "stock_name": str(item.get("stock_name") or stock_id),
                    "contribution_points": _safe_float(
                        item.get("contribution_points")
                    ),
                }
    return {
        "stock_id": str(contribution.get("largest_stock_id") or ""),
        "stock_name": str(contribution.get("largest_stock_id") or "--"),
        "contribution_points": _safe_float(
            contribution.get("largest_contribution_points")
        ),
    }


def _data_item(
    label: str,
    snapshot: dict[str, Any],
    date_key: str,
) -> dict[str, Any]:
    available = bool(snapshot.get("available", snapshot.get("_call_ok")))
    return {
        "label": label,
        "available": available,
        "date": str(snapshot.get(date_key) or ""),
        "source": str(snapshot.get("source") or snapshot.get("quote_source") or ""),
        "message": str(snapshot.get("message") or ""),
    }


def build_market_afterhours_digest() -> dict[str, Any]:
    """整合收盤行情、籌碼、融資券、期貨與權值貢獻。"""
    started = time.perf_counter()
    now = datetime.now(ZoneInfo(TAIPEI_TZ))
    now_minute = now.hour * 60 + now.minute
    is_intraday_preview = (
        now.weekday() < 5
        and 9 * 60 <= now_minute <= 13 * 60 + 35
    )
    data_mode = "盤中暫估" if is_intraday_preview else "收盤資料"

    from services.market_chip_service import get_market_chip_snapshot
    from services.market_future_service import get_market_future_snapshot
    from services.market_index_service import get_market_index_snapshot
    from services.market_margin_service import get_market_margin_snapshot

    # FinMind、TWSE/TPEx 與 Supabase 可並行；永豐 index/future 則依序呼叫，
    # 避免同一個 Shioaji session 被兩個 thread 同時存取。
    with ThreadPoolExecutor(max_workers=4) as executor:
        chip_future = executor.submit(
            _safe_call,
            "market_chip",
            lambda: get_market_chip_snapshot(days=45),
        )
        margin_tse_future = executor.submit(
            _safe_call,
            "margin_tse",
            lambda: get_market_margin_snapshot(
                days=45,
                market_scope="tse",
            ),
        )
        margin_otc_future = executor.submit(
            _safe_call,
            "margin_otc",
            lambda: get_market_margin_snapshot(
                days=45,
                market_scope="otc",
            ),
        )
        contribution_future = executor.submit(
            _safe_call,
            "contribution",
            _latest_contribution_row,
        )

        market = _safe_call(
            "market_index",
            lambda: get_market_index_snapshot(with_chart=False),
        )
        future = _safe_call(
            "market_future",
            lambda: get_market_future_snapshot(session_mode="all"),
        )

        chip = chip_future.result()
        margin_tse = margin_tse_future.result()
        margin_otc = margin_otc_future.result()
        contribution = contribution_future.result()

    open_price = _safe_float(market.get("open_price"))
    high_price = _safe_float(market.get("high_price"))
    low_price = _safe_float(market.get("low_price"))
    close_price = _safe_float(market.get("close_price"))
    change = _safe_float(market.get("change"))
    change_pct = _safe_float(market.get("change_pct"))
    levels = _market_levels(
        open_price,
        high_price,
        low_price,
        close_price,
    )

    future_price = _safe_float(future.get("future_price"))
    basis_points = (
        future_price - close_price
        if future_price > 0 and close_price > 0
        else 0.0
    )
    future_phase = _future_phase(future.get("quote_time"))

    trade_date = str(contribution.get("trade_date") or "")
    if not trade_date:
        quote_time = str(market.get("quote_time") or "")
        trade_date = quote_time[:10] if len(quote_time) >= 10 else ""
    if not trade_date:
        trade_date = now.strftime("%Y-%m-%d")

    largest = _largest_component(contribution)
    data_status = [
        _data_item("大盤", market, "quote_time"),
        _data_item("法人", chip, "latest_date"),
        _data_item("上市融資券", margin_tse, "latest_date"),
        _data_item("上櫃融資券", margin_otc, "latest_date"),
        {
            "label": "權值貢獻",
            "available": bool(contribution.get("trade_date")),
            "date": str(contribution.get("trade_date") or ""),
            "source": str(contribution.get("source") or ""),
            "message": "",
        },
        _data_item("台指期", future, "quote_time"),
    ]

    result = {
        "ok": bool(market.get("available") and close_price > 0),
        "message": (
            "ok"
            if market.get("available") and close_price > 0
            else str(market.get("message") or "大盤收盤資料不足")
        ),
        "version": MARKET_AFTERHOURS_VERSION,
        "trade_date": trade_date,
        "generated_at": now.isoformat(),
        "data_mode": data_mode,
        "market": {
            "available": bool(market.get("available")),
            "open": round(open_price, 2),
            "high": round(high_price, 2),
            "low": round(low_price, 2),
            "close": round(close_price, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 3),
            "turnover_yi": round(
                _safe_float(
                    market.get("total_volume")
                    or market.get("volume")
                ),
                2,
            ),
            "quote_time": str(market.get("quote_time") or ""),
            "source": str(market.get("quote_source") or ""),
            **levels,
        },
        "future": {
            "available": bool(future.get("available")),
            "contract_code": str(future.get("contract_code") or ""),
            "price": round(future_price, 2),
            "change": round(_safe_float(future.get("future_change")), 2),
            "change_pct": round(
                _safe_float(future.get("future_change_pct")),
                3,
            ),
            "basis_points": round(basis_points, 2),
            "phase": future_phase,
            "quote_time": str(future.get("quote_time") or ""),
            "source": str(future.get("quote_source") or ""),
        },
        "chip": {
            "available": bool(chip.get("available")),
            "date": str(chip.get("latest_date") or ""),
            "foreign_yi": round(_safe_float(chip.get("foreign")), 2),
            "trust_yi": round(
                _safe_float(chip.get("investment_trust")),
                2,
            ),
            "dealer_yi": round(_safe_float(chip.get("dealer")), 2),
            "total_yi": round(_safe_float(chip.get("total")), 2),
            "source": str(chip.get("source") or ""),
        },
        "margin": {
            "tse": {
                "available": bool(margin_tse.get("available")),
                "date": str(margin_tse.get("latest_date") or ""),
                "money_change_yi": round(
                    _safe_float(margin_tse.get("margin_money_change"))
                    / 100_000_000,
                    2,
                ),
                "short_change": _safe_int(
                    margin_tse.get("short_change")
                ),
                "ratio": round(
                    _safe_float(
                        margin_tse.get("margin_short_ratio")
                    ),
                    2,
                ),
            },
            "otc": {
                "available": bool(margin_otc.get("available")),
                "date": str(margin_otc.get("latest_date") or ""),
                "money_change_yi": round(
                    _safe_float(margin_otc.get("margin_money_change"))
                    / 100_000_000,
                    2,
                ),
                "short_change": _safe_int(
                    margin_otc.get("short_change")
                ),
                "ratio": round(
                    _safe_float(
                        margin_otc.get("margin_short_ratio")
                    ),
                    2,
                ),
            },
        },
        "weight": {
            "available": bool(contribution.get("trade_date")),
            "date": str(contribution.get("trade_date") or ""),
            "weight_date": str(
                contribution.get("weight_trade_date") or ""
            ),
            "top20_contribution_points": round(
                _safe_float(
                    contribution.get("top20_contribution_points")
                ),
                2,
            ),
            "top20_weight_pct": round(
                _safe_float(
                    contribution.get("top20_market_weight_pct")
                ),
                2,
            ),
            "positive_ratio_pct": round(
                _safe_float(
                    contribution.get(
                        "top20_positive_weight_ratio_pct"
                    )
                ),
                2,
            ),
            "negative_ratio_pct": round(
                _safe_float(
                    contribution.get(
                        "top20_negative_weight_ratio_pct"
                    )
                ),
                2,
            ),
            "largest": largest,
            "otc_return_15m": (
                None
                if contribution.get("otc_return_15m") is None
                else round(
                    _safe_float(contribution.get("otc_return_15m")),
                    4,
                )
            ),
            "taiex_otc_divergence_15m": (
                None
                if contribution.get(
                    "taiex_otc_divergence_15m"
                ) is None
                else round(
                    _safe_float(
                        contribution.get(
                            "taiex_otc_divergence_15m"
                        )
                    ),
                    4,
                )
            ),
            "source": str(contribution.get("source") or ""),
        },
        "data_status": data_status,
        "note": (
            "盤中資料尚未定案；收盤後再查可取得完整盤後總覽。"
            if is_intraday_preview
            else "盤後資料整理與區間參考，非次日漲跌預測。"
        ),
        "seconds": round(time.perf_counter() - started, 3),
    }
    _debug(
        "built",
        "| date =", trade_date,
        "| close =", close_price,
        "| chip_date =", result["chip"]["date"],
        "| margin_tse_date =", result["margin"]["tse"]["date"],
        "| contribution_date =", result["weight"]["date"],
        "| sec =", result["seconds"],
    )
    return result
