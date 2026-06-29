from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests
import yfinance as yf

from config import FINMIND_TOKEN


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


def get_stock_futures_snapshot(stock_id: str, stock_name: str) -> FuturesSnapshot:
    """
    股票期貨第一版：

    - 只抓標準股票期貨
    - 只抓近月
    - 同近月盤後優先
    - 沒盤後才用日盤
    - 不抓小型期貨
    """
    sid = _clean_stock_id(stock_id)

    futures_name, candidates = _resolve_stock_futures_candidates(sid)

    if not candidates:
        return FuturesSnapshot(
            available=False,
            message="這檔股票尚未建立標準股票期貨代號對照。",
            stock_id=sid,
            stock_name=stock_name,
        )

    selected_row: dict | None = None
    selected_futures_id = ""

    for futures_id in candidates:
        rows = _request_finmind_futures_daily(futures_id)

        if not rows:
            continue

        row = _pick_near_month_prefer_afterhours(rows)

        if row:
            selected_row = row
            selected_futures_id = futures_id
            break

    if not selected_row:
        return FuturesSnapshot(
            available=False,
            message="查無近月股票期貨資料，可能是 FinMind 尚未更新或期貨代號需調整。",
            stock_id=sid,
            stock_name=stock_name,
            futures_name=futures_name,
        )

    future_price = _row_price(selected_row)
    future_change = _row_change(selected_row)
    future_change_pct = _row_change_pct(selected_row)

    spot_price = _get_spot_price(sid)

    basis = future_price - spot_price if future_price and spot_price else 0.0
    basis_pct = (basis / spot_price * 100) if spot_price else 0.0

    print(
        "futures_service_v1",
        "| stock=", sid,
        "| futures_id=", selected_futures_id,
        "| contract=", selected_row.get("contract_date"),
        "| session=", selected_row.get("trading_session"),
        "| date=", selected_row.get("date"),
        "| future_price=", future_price,
        "| spot_price=", spot_price,
    )

    return FuturesSnapshot(
        available=True,
        message="ok",
        stock_id=sid,
        stock_name=stock_name,
        futures_id=str(selected_row.get("futures_id") or selected_futures_id),
        futures_name=futures_name,
        contract_date=_format_contract_date(selected_row.get("contract_date")),
        trade_date=_normalize_trade_date(selected_row.get("date")),
        trading_session=_display_session(selected_row.get("trading_session")),
        future_price=future_price,
        future_change=future_change,
        future_change_pct=future_change_pct,
        spot_price=spot_price,
        basis=basis,
        basis_pct=basis_pct,
        volume=_safe_int(selected_row.get("volume")),
        open_interest=_safe_int(selected_row.get("open_interest")),
        settlement_price=_safe_float(selected_row.get("settlement_price")),
    )
