from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests
import yfinance as yf

from config import FINMIND_TOKEN

try:
    from services.sinopac_quote_service import (
        get_futures_snapshot as get_shioaji_futures_snapshot,
        get_stock_snapshot as get_shioaji_stock_snapshot,
    )
except Exception:
    def get_shioaji_futures_snapshot(futures_id: str, contract_date: str = ""):
        return None

    def get_shioaji_stock_snapshot(stock_id: str):
        return None

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


@dataclass
class FuturesSnapshot:
    available: bool
    message: str

    stock_id: str
    stock_name: str

    futures_id: str = ""
    futures_name: str = ""
    contract_date: str = ""
    trade_date: str = ""
    trading_session: str = ""

    future_price: float = 0.0
    future_change: float = 0.0
    future_change_pct: float = 0.0

    spot_price: float = 0.0
    basis: float = 0.0
    basis_pct: float = 0.0

    volume: int = 0
    open_interest: int = 0
    settlement_price: float = 0.0


# 第一版只放標準股票期貨，不放小型股票期貨。
# candidates 同時放完整股票期貨代號與可能的短代號，避免 FinMind 版本差異。
STOCK_FUTURES_MAP: dict[str, dict[str, Any]] = {
    "2330": {
        "name": "台積電期貨",
        "candidates": ["CDF", "CD"],
    },
    "2337": {
        "name": "旺宏期貨",
        "candidates": ["DIF", "DI"],
    },
    "2344": {
        "name": "華邦電期貨",
        "candidates": ["FZF", "FZ"],
    },
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").strip()

            if not value:
                return default

        return float(value)

    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, float(default))))
    except Exception:
        return default


def _clean_stock_id(stock_id: str) -> str:
    return str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()


def _start_date(days: int = 60) -> str:
    return (datetime.utcnow().date() - timedelta(days=days)).strftime("%Y-%m-%d")


def _request_finmind_futures_daily(futures_id: str) -> list[dict]:
    """
    抓期貨日成交資訊。
    只印狀態，不印 token。
    """
    params = {
        "dataset": "TaiwanFuturesDaily",
        "data_id": futures_id,
        "start_date": _start_date(60),
    }

    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    try:
        res = requests.get(FINMIND_URL, params=params, timeout=15)

        if res.status_code >= 400:
            print(
                f"_request_finmind_futures_daily failed: "
                f"futures_id={futures_id}, status={res.status_code}, body={res.text[:200]}"
            )
            return []

        payload = res.json()
        rows = payload.get("data") or []

        return rows if isinstance(rows, list) else []

    except Exception as exc:
        print(f"_request_finmind_futures_daily failed: futures_id={futures_id}, error={exc}")
        return []


def _normalize_contract_date(value: Any) -> str:
    """
    支援：
    202607
    2026/07
    2026-07

    空白、全月份、所有契約都排除。
    """
    s = str(value or "").strip()

    if not s:
        return ""

    lower = s.lower()

    if "all" in lower or "全" in s or "所有" in s:
        return ""

    digits = "".join(ch for ch in s if ch.isdigit())

    if len(digits) >= 6:
        return digits[:6]

    return ""


def _format_contract_date(value: Any) -> str:
    s = _normalize_contract_date(value)

    if len(s) == 6:
        return f"{s[:4]}/{s[4:6]}"

    return str(value or "--")


def _normalize_trade_date(value: Any) -> str:
    s = str(value or "").strip()

    if not s:
        return ""

    try:
        if len(s) >= 10 and "-" in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")

        digits = "".join(ch for ch in s if ch.isdigit())

        if len(digits) >= 8:
            return datetime.strptime(digits[:8], "%Y%m%d").strftime("%Y-%m-%d")

    except Exception:
        return s

    return s


def _is_afterhours_session(value: Any) -> bool:
    text = str(value or "").strip().lower()

    return (
        "after" in text
        or "night" in text
        or "盤後" in text
        or "夜盤" in text
    )


def _is_regular_session(value: Any) -> bool:
    text = str(value or "").strip().lower()

    return (
        "regular" in text
        or "day" in text
        or "一般" in text
        or "日盤" in text
    )


def _display_session(value: Any) -> str:
    if _is_afterhours_session(value):
        return "盤後"

    if _is_regular_session(value):
        return "日盤"

    text = str(value or "").strip()

    return text or "--"


def _pick_near_month_prefer_afterhours(rows: list[dict]) -> dict | None:
    """
    選資料規則：

    1. 找最新資料日期
    2. 最新日期中，找 contract_date 最小者，也就是近月
    3. 同一近月內，盤後優先
    4. 沒盤後，用日盤
    5. session 欄位不明時，用近月最後一筆
    """
    valid_rows: list[dict] = []

    for r in rows:
        trade_date = _normalize_trade_date(r.get("date"))
        contract = _normalize_contract_date(r.get("contract_date"))

        if not trade_date or not contract:
            continue

        item = dict(r)
        item["_trade_date_norm"] = trade_date
        item["_contract_norm"] = contract

        valid_rows.append(item)

    if not valid_rows:
        return None

    latest_date = max(r["_trade_date_norm"] for r in valid_rows)

    latest_rows = [
        r for r in valid_rows
        if r["_trade_date_norm"] == latest_date
    ]

    if not latest_rows:
        return None

    near_contract = min(r["_contract_norm"] for r in latest_rows)

    near_rows = [
        r for r in latest_rows
        if r["_contract_norm"] == near_contract
    ]

    if not near_rows:
        return None

    afterhours_rows = [
        r for r in near_rows
        if _is_afterhours_session(r.get("trading_session"))
    ]

    if afterhours_rows:
        return afterhours_rows[-1]

    regular_rows = [
        r for r in near_rows
        if _is_regular_session(r.get("trading_session"))
    ]

    if regular_rows:
        return regular_rows[-1]

    return near_rows[-1]


def _get_spot_price(stock_id: str) -> float:
    """
    現貨價格用 Yahoo quote。
    """
    sid = _clean_stock_id(stock_id)

    for symbol in [f"{sid}.TW", f"{sid}.TWO"]:
        try:
            ticker = yf.Ticker(symbol)

            try:
                fast_info = ticker.fast_info

                for key in [
                    "last_price",
                    "lastPrice",
                    "regularMarketPrice",
                    "currentPrice",
                ]:
                    try:
                        value = fast_info.get(key)
                        price = _safe_float(value)

                        if price > 0:
                            return price

                    except Exception:
                        pass

            except Exception:
                pass

            try:
                info = ticker.info or {}

                for key in [
                    "regularMarketPrice",
                    "currentPrice",
                    "lastPrice",
                ]:
                    price = _safe_float(info.get(key))

                    if price > 0:
                        return price

            except Exception:
                pass

        except Exception:
            continue

    return 0.0


def _row_price(row: dict) -> float:
    for key in [
        "close",
        "Close",
        "settlement_price",
        "SettlementPrice",
    ]:
        price = _safe_float(row.get(key))

        if price > 0:
            return price

    return 0.0


def _row_change(row: dict) -> float:
    for key in [
        "spread",
        "change",
        "price_change",
    ]:
        value = _safe_float(row.get(key), default=0.0)

        if value != 0:
            return value

    return 0.0


def _row_change_pct(row: dict) -> float:
    for key in [
        "spread_per",
        "change_percent",
        "price_change_pct",
    ]:
        value = _safe_float(row.get(key), default=0.0)

        if value != 0:
            return value

    return 0.0


def _resolve_stock_futures_candidates(stock_id: str) -> tuple[str, list[str]]:
    sid = _clean_stock_id(stock_id)
    info = STOCK_FUTURES_MAP.get(sid)

    if not info:
        return "", []

    name = str(info.get("name") or "")
    candidates = list(info.get("candidates") or [])

    return name, candidates


def get_stock_futures_snapshot(
    stock_id: str,
    stock_name: str,
    session_mode: str = "day",
) -> FuturesSnapshot:
    """
    股票期貨：

    - 支援 controller 傳入 session_mode
    - day：日盤
    - all：全盤
    - 價格優先使用 Shioaji 期貨 snapshot
    - fallback 使用 FinMind TaiwanFuturesDaily
    """
    from dataclasses import fields as dataclass_fields

    sid = _clean_stock_id(stock_id)

    session_mode = str(session_mode or "day").strip().lower()

    if session_mode not in {"day", "all"}:
        session_mode = "day"

    resolved = _resolve_stock_futures_candidates(sid)

    if isinstance(resolved, tuple) and len(resolved) >= 3:
        futures_name = resolved[0]
        candidates = resolved[1]
    else:
        futures_name, candidates = resolved

    if not candidates:
        payload = {
            "available": False,
            "message": "這檔股票尚未建立標準股票期貨代號對照。",
            "stock_id": sid,
            "stock_name": stock_name,
        }

        allowed = {f.name for f in dataclass_fields(FuturesSnapshot)}
        return FuturesSnapshot(**{k: v for k, v in payload.items() if k in allowed})

    def _session_text(row: dict) -> str:
        for key in [
            "trading_session",
            "TradingSession",
            "session",
            "tradingSession",
            "交易時段",
        ]:
            value = row.get(key)
            if value not in (None, ""):
                return str(value)

        return ""

    def _pick_row_by_mode(rows: list[dict], mode: str) -> dict | None:
        valid_rows: list[dict] = []

        for r in rows:
            try:
                if not _is_valid_trade_row(r):
                    continue
            except Exception:
                contract = _normalize_contract_date(r.get("contract_date"))
                price = _row_price(r)

                if not contract or price <= 0:
                    continue

            trade_date = _normalize_trade_date(r.get("date"))
            contract = _normalize_contract_date(r.get("contract_date"))

            if not trade_date or not contract:
                continue

            item = dict(r)
            item["_trade_date_norm"] = trade_date
            item["_contract_norm"] = contract
            valid_rows.append(item)

        if not valid_rows:
            return None

        if mode == "day":
            day_rows = [
                r for r in valid_rows
                if _is_regular_session(_session_text(r))
            ]

            if not day_rows:
                return None

            latest_date = max(r["_trade_date_norm"] for r in day_rows)

            latest_rows = [
                r for r in day_rows
                if r["_trade_date_norm"] == latest_date
            ]

            near_contract = min(r["_contract_norm"] for r in latest_rows)

            near_rows = [
                r for r in latest_rows
                if r["_contract_norm"] == near_contract
            ]

            return near_rows[-1] if near_rows else None

        latest_date = max(r["_trade_date_norm"] for r in valid_rows)

        latest_rows = [
            r for r in valid_rows
            if r["_trade_date_norm"] == latest_date
        ]

        near_contract = min(r["_contract_norm"] for r in latest_rows)

        near_rows = [
            r for r in latest_rows
            if r["_contract_norm"] == near_contract
        ]

        if not near_rows:
            return None

        regular_rows = [
            r for r in near_rows
            if _is_regular_session(_session_text(r))
        ]

        # 全盤：如果同一交易日已經有日盤資料，收盤價以日盤最後一筆為主；
        # 如果只有盤後，才用盤後。
        chosen = regular_rows[-1] if regular_rows else near_rows[-1]
        chosen = dict(chosen)
        chosen["trading_session"] = "all"

        return chosen

    selected_row: dict | None = None
    selected_futures_id = ""
    selected_rows: list[dict] = []

    for futures_id in candidates:
        rows = _request_finmind_futures_daily(futures_id)

        print(
            "DEBUG futures candidate",
            "| futures_id=", futures_id,
            "| rows_count=", len(rows),
            flush=True,
        )

        if not rows:
            continue

        row = _pick_row_by_mode(rows, session_mode)

        if row:
            selected_row = row
            selected_futures_id = futures_id
            selected_rows = rows
            break

    if not selected_row:
        payload = {
            "available": False,
            "message": "查無近月股票期貨資料，可能是 FinMind 尚未更新或期貨代號需調整。",
            "stock_id": sid,
            "stock_name": stock_name,
            "futures_name": futures_name,
        }

        allowed = {f.name for f in dataclass_fields(FuturesSnapshot)}
        return FuturesSnapshot(**{k: v for k, v in payload.items() if k in allowed})

    display_contract = _format_contract_date(selected_row.get("contract_date"))

    if session_mode == "all":
        display_session = "全盤"
    else:
        display_session = _display_session(_session_text(selected_row))

    future_price = _row_price(selected_row)
    future_change = _row_change(selected_row)
    future_change_pct = _row_change_pct(selected_row)
    future_volume = _safe_int(selected_row.get("volume"))

    quote_source = "日成交"
    quote_time = ""

    # =========================
    # 第一順位：Shioaji 期貨即時價
    # =========================
    try:
        shioaji_quote = get_shioaji_futures_snapshot(
            selected_futures_id,
            selected_row.get("contract_date"),
        )
    except Exception as exc:
        print(
            "DEBUG futures shioaji futures snapshot failed",
            selected_futures_id,
            exc,
            flush=True,
        )
        shioaji_quote = None

    if shioaji_quote:
        sj_price = _safe_float(shioaji_quote.get("close"))

        if sj_price > 0:
            future_price = sj_price
            future_change = _safe_float(
                shioaji_quote.get("change"),
                future_change,
            )
            future_change_pct = _safe_float(
                shioaji_quote.get("change_pct"),
                future_change_pct,
            )

            future_volume = _safe_int(
                shioaji_quote.get("total_volume")
                or shioaji_quote.get("volume"),
                future_volume,
            )

            quote_source = "永豐即時"
            quote_time = str(shioaji_quote.get("ts") or "")

            print(
                "DEBUG futures use shioaji futures snapshot",
                "| futures_id=", selected_futures_id,
                "| snapshot_futures_id=", shioaji_quote.get("futures_id"),
                "| price=", future_price,
                "| change=", future_change,
                "| change_pct=", future_change_pct,
                "| volume=", future_volume,
                "| time=", quote_time,
                flush=True,
            )

    # =========================
    # 現貨價格：優先 Shioaji，fallback 原本 _get_spot_price
    # =========================
    spot_price = 0.0

    try:
        stock_snapshot = get_shioaji_stock_snapshot(sid)

        if stock_snapshot:
            spot_price = _safe_float(stock_snapshot.get("close"))
    except Exception as exc:
        print(
            "DEBUG futures shioaji stock snapshot failed",
            sid,
            exc,
            flush=True,
        )

    if spot_price <= 0:
        spot_price = _get_spot_price(sid)

    basis = future_price - spot_price if future_price and spot_price else 0.0
    basis_pct = (basis / spot_price * 100) if spot_price else 0.0

    trade_date = _normalize_trade_date(selected_row.get("date"))

    if quote_time:
        trade_date = quote_time[:10]

    chart_url = ""

    try:
        if "_prepare_futures_kline_rows" in globals() and "_generate_futures_kline_chart" in globals():
            kline_rows = _prepare_futures_kline_rows(
                selected_rows,
                selected_row,
                session_mode=session_mode,
            )

            chart_url = _generate_futures_kline_chart(
                rows=kline_rows,
                futures_id=selected_futures_id,
                futures_name=futures_name,
                contract_date=display_contract,
                session=display_session,
                current_price=future_price if quote_source != "日成交" else 0.0,
                quote_time=quote_time,
            )

    except TypeError:
        try:
            kline_rows = _prepare_futures_kline_rows(
                selected_rows,
                selected_row,
            )

            chart_url = _generate_futures_kline_chart(
                rows=kline_rows,
                futures_id=selected_futures_id,
                futures_name=futures_name,
                contract_date=display_contract,
                session=display_session,
            )

        except Exception as exc:
            print("DEBUG futures chart fallback failed", exc, flush=True)

    except Exception as exc:
        print("DEBUG futures chart failed", exc, flush=True)

    print(
        "DEBUG futures final",
        "| stock=", sid,
        "| futures_id=", selected_futures_id,
        "| contract=", selected_row.get("contract_date"),
        "| session=", display_session,
        "| date=", trade_date,
        "| quote_source=", quote_source,
        "| quote_time=", quote_time,
        "| future_price=", future_price,
        "| spot_price=", spot_price,
        "| basis=", basis,
        "| chart_url=", chart_url,
        flush=True,
    )

    payload = {
        "available": True,
        "message": "ok",
        "stock_id": sid,
        "stock_name": stock_name,
        "futures_id": str(selected_row.get("futures_id") or selected_futures_id),
        "futures_name": futures_name,
        "contract_date": display_contract,
        "trade_date": trade_date,
        "trading_session": display_session,
        "chart_url": chart_url,
        "future_price": future_price,
        "future_change": future_change,
        "future_change_pct": future_change_pct,
        "spot_price": spot_price,
        "basis": basis,
        "basis_pct": basis_pct,
        "volume": future_volume,
        "open_interest": _safe_int(selected_row.get("open_interest")),
        "settlement_price": _safe_float(selected_row.get("settlement_price")),
        "quote_source": quote_source,
        "quote_time": quote_time,
    }

    allowed = {f.name for f in dataclass_fields(FuturesSnapshot)}

    return FuturesSnapshot(
        **{
            k: v
            for k, v in payload.items()
            if k in allowed
        }
    )
