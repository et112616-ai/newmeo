from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests
import yfinance as yf

from config import FINMIND_TOKEN

try:
    from services.futures_map_service import get_stock_futures_mapping
except Exception:
    def get_stock_futures_mapping(stock_id: str):
        return None


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
    chart_url: str = ""

    future_price: float = 0.0
    future_change: float = 0.0
    future_change_pct: float = 0.0

    spot_price: float = 0.0
    basis: float = 0.0
    basis_pct: float = 0.0

    volume: int = 0
    open_interest: int = 0
    settlement_price: float = 0.0

    quote_source: str = "日成交"
    quote_time: str = ""


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

def _unique(values: list[str]) -> list[str]:
    """
    去除空白、轉大寫、去重，並保留原本順序。
    """
    result = []

    for value in values or []:
        text = str(value or "").strip().upper()

        if not text:
            continue

        if text not in result:
            result.append(text)

    return result


def _build_candidates_from_futures_code(futures_code: str) -> list[str]:
    """
    期交所資料通常給基礎代碼，例如：
    2303 聯電 = CC

    FinMind / Shioaji 可能使用：
    CCF 或 CC

    所以自動產生：
    CC  -> CCF, CC
    CCF -> CCF, CC
    """
    code = str(futures_code or "").strip().upper()

    if not code:
        return []

    candidates = []

    if code.endswith("F"):
        candidates.append(code)
        candidates.append(code[:-1])
    else:
        candidates.append(f"{code}F")
        candidates.append(code)

    return _unique(candidates)

def _resolve_stock_futures_candidates(
    stock_id: str,
    stock_name: str = "",
) -> tuple[str, list[str], str]:
    """
    股票代號 -> 股票期貨代號。

    優先順序：
    1. Supabase stock_futures_map
    2. STOCK_FUTURES_MAP fallback

    回傳：
    futures_name, candidates, source
    """
    sid = _clean_stock_id(stock_id)

    # =========================
    # 1. Supabase 對照表
    # =========================
    try:
        mapping = get_stock_futures_mapping(sid)
    except Exception as exc:
        print(
            "DEBUG futures mapping supabase failed",
            sid,
            exc,
            flush=True,
        )
        mapping = None

    if mapping:
        futures_name = (
            mapping.get("futures_name")
            or mapping.get("name")
            or f"{mapping.get('stock_name') or stock_name or sid}期貨"
        )

        candidates = list(mapping.get("candidates") or [])

        if not candidates:
            futures_code = (
                mapping.get("futures_code")
                or mapping.get("code")
                or mapping.get("futures_id")
                or ""
            )

            candidates = _build_candidates_from_futures_code(str(futures_code))

        candidates = _unique(candidates)

        if candidates:
            print(
                "DEBUG futures mapping source=supabase",
                sid,
                futures_name,
                candidates,
                flush=True,
            )
            return str(futures_name), candidates, "supabase"

    # =========================
    # 2. 手動 fallback
    # =========================
    info = STOCK_FUTURES_MAP.get(sid)

    if info:
        futures_name = str(info.get("name") or f"{stock_name or sid}期貨")
        candidates = _unique(list(info.get("candidates") or []))

        if candidates:
            print(
                "DEBUG futures mapping source=fallback",
                sid,
                futures_name,
                candidates,
                flush=True,
            )
            return futures_name, candidates, "fallback"

    print(
        "DEBUG futures mapping not found",
        sid,
        flush=True,
    )

    return "", [], "none"

def _pick_chart_anchor_row(rows: list[dict]) -> dict | None:
    """
    給 K 線圖用的 fallback row。

    用途：
    - 即時價格已經由 Shioaji 取得
    - 但 FinMind 的日盤 / 全盤挑選沒有選到 selected_row
    - 仍然用 FinMind 有效資料產生 K 線圖
    """
    valid_rows: list[dict] = []

    for r in rows or []:
        try:
            if not _is_valid_trade_row(r):
                continue
        except Exception:
            if _row_price(r) <= 0:
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

    latest_date = max(r["_trade_date_norm"] for r in valid_rows)

    latest_rows = [
        r for r in valid_rows
        if r["_trade_date_norm"] == latest_date
    ]

    if not latest_rows:
        return valid_rows[-1]

    near_contract = min(r["_contract_norm"] for r in latest_rows)

    near_rows = [
        r for r in latest_rows
        if r["_contract_norm"] == near_contract
    ]

    if near_rows:
        return near_rows[-1]

    return latest_rows[-1]

def get_stock_futures_snapshot(
    stock_id: str,
    stock_name: str,
    session_mode: str = "day",
) -> FuturesSnapshot:
    """
    股票期貨：

    - 股票期貨代號對照：Supabase 為主
    - 期貨即時價格：永豐 Shioaji 為主
    - K 線 / fallback：FinMind TaiwanFuturesDaily
    - 不再因為 FinMind 找不到就直接失敗
    """
    from dataclasses import fields as dataclass_fields

    sid = _clean_stock_id(stock_id)

    session_mode = str(session_mode or "day").strip().lower()

    if session_mode not in {"day", "all"}:
        session_mode = "day"

    # =========================
    # 1. 取得股票期貨代號
    # =========================
    try:
        resolved = _resolve_stock_futures_candidates(sid, stock_name)
    except TypeError:
        resolved = _resolve_stock_futures_candidates(sid)

    if isinstance(resolved, tuple) and len(resolved) >= 3:
        futures_name = resolved[0]
        candidates = resolved[1]
        source = resolved[2]
    else:
        futures_name, candidates = resolved
        source = "unknown"

    candidates = list(candidates or [])

    if not candidates:
        payload = {
            "available": False,
            "message": "這檔股票尚未建立標準股票期貨代號對照。",
            "stock_id": sid,
            "stock_name": stock_name,
            "futures_name": futures_name or f"{stock_name}期貨",
        }

        allowed = {f.name for f in dataclass_fields(FuturesSnapshot)}

        return FuturesSnapshot(
            **{
                k: v
                for k, v in payload.items()
                if k in allowed
            }
        )

    print(
        "DEBUG futures start",
        "| stock=", sid,
        "| name=", stock_name,
        "| futures_name=", futures_name,
        "| candidates=", candidates,
        "| source=", source,
        "| session_mode=", session_mode,
        flush=True,
    )

    # =========================
    # 2. 第一順位：永豐 Shioaji 期貨 snapshot
    # =========================
    shioaji_quote = None
    selected_futures_id = ""

    shioaji_futures_func = globals().get("get_shioaji_futures_snapshot")

    if callable(shioaji_futures_func):
        for fid in candidates:
            try:
                q = shioaji_futures_func(fid, "")

                print(
                    "DEBUG futures try shioaji",
                    "| fid=", fid,
                    "| quote=", q,
                    flush=True,
                )

                if q and _safe_float(q.get("close")) > 0:
                    shioaji_quote = q
                    selected_futures_id = fid
                    break

            except Exception as exc:
                print(
                    "DEBUG futures shioaji futures snapshot failed",
                    "| fid=", fid,
                    "| error=", exc,
                    flush=True,
                )
    else:
        print(
            "DEBUG futures shioaji futures function not available",
            flush=True,
        )

    # =========================
    # 3. 第二順位：FinMind 日成交資料
    #    主要用於 K 線與 fallback
    # =========================
    selected_row: dict | None = None
    selected_rows: list[dict] = []

    chart_source_rows: list[dict] = []
    chart_source_futures_id = ""

    for fid in candidates:
        rows = _request_finmind_futures_daily(fid)

        print(
            "DEBUG futures finmind daily",
            "| fid=", fid,
            "| rows_count=", len(rows),
            flush=True,
        )

        if not rows:
            continue
                   
        # 即使沒有選到日盤 row，也先保留一份給 K 線圖使用
        if rows and not chart_source_rows:
            chart_source_rows = rows
            chart_source_futures_id = fid

        try:
            row = _pick_near_month_prefer_afterhours(
                rows,
                session_mode=session_mode,
            )
        except TypeError:
            row = _pick_near_month_prefer_afterhours(rows)
        except Exception as exc:
            print(
                "DEBUG futures pick finmind row failed",
                "| fid=", fid,
                "| error=", exc,
                flush=True,
            )
            row = None

        if row:
            if not selected_futures_id:
                selected_futures_id = fid

            selected_row = row
            selected_rows = rows
            break

    # =========================
    # 4. 如果 Shioaji 與 FinMind 都沒有，才失敗
    # =========================
        # 如果有 FinMind rows 但沒有成功選到 selected_row，
    # 仍然保留給 K 線圖使用。
    if not selected_rows and chart_source_rows:
        selected_rows = chart_source_rows

    if not selected_futures_id and chart_source_futures_id:
        selected_futures_id = chart_source_futures_id

    if not selected_row and selected_rows:
        selected_row = _pick_chart_anchor_row(selected_rows)
    
    if not shioaji_quote and not selected_row:
        payload = {
            "available": False,
            "message": "查無股票期貨即時資料，且 FinMind 尚未提供近月資料。",
            "stock_id": sid,
            "stock_name": stock_name,
            "futures_name": futures_name or f"{stock_name}期貨",
        }

        allowed = {f.name for f in dataclass_fields(FuturesSnapshot)}

        return FuturesSnapshot(
            **{
                k: v
                for k, v in payload.items()
                if k in allowed
            }
        )

    # =========================
    # 5. 決定商品、契約、時段
    # =========================
    if not selected_futures_id:
        selected_futures_id = candidates[0]

    display_session = "全盤" if session_mode == "all" else "日盤"

    display_contract = "--"
    trade_date = ""
    open_interest = 0
    settlement_price = 0.0

    if selected_row:
        display_contract = _format_contract_date(
            selected_row.get("contract_date")
        )
        trade_date = _normalize_trade_date(
            selected_row.get("date")
        )
        open_interest = _safe_int(
            selected_row.get("open_interest")
        )
        settlement_price = _safe_float(
            selected_row.get("settlement_price")
        )

    # =========================
    # 6. 價格：Shioaji 優先，FinMind fallback
    # =========================
    quote_source = "日成交"
    quote_time = ""

    if shioaji_quote:
        future_price = _safe_float(
            shioaji_quote.get("close")
        )
        future_change = _safe_float(
            shioaji_quote.get("change")
        )
        future_change_pct = _safe_float(
            shioaji_quote.get("change_pct")
        )
        future_volume = _safe_int(
            shioaji_quote.get("total_volume")
            or shioaji_quote.get("volume")
        )

        quote_source = "永豐即時"
        quote_time = str(
            shioaji_quote.get("ts") or ""
        )

        if quote_time:
            trade_date = quote_time[:10]

        selected_display_futures_id = str(
            shioaji_quote.get("futures_id")
            or selected_futures_id
        )

    else:
        row_for_price = selected_row or {}

        future_price = _row_price(row_for_price)
        future_change = _row_change(row_for_price)
        future_change_pct = _row_change_pct(row_for_price)
        future_volume = _safe_int(row_for_price.get("volume"))

        selected_display_futures_id = str(
            row_for_price.get("futures_id")
            or selected_futures_id
        )

    # =========================
    # 7. 現貨：優先 Shioaji
    # =========================
    spot_price = 0.0

    shioaji_stock_func = globals().get("get_shioaji_stock_snapshot")

    if callable(shioaji_stock_func):
        try:
            stock_snapshot = shioaji_stock_func(sid)

            if stock_snapshot:
                spot_price = _safe_float(
                    stock_snapshot.get("close")
                )

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

    # =========================
    # 8. K 線：有 FinMind rows 才畫
    # =========================
    chart_url = ""

    chart_anchor_row = selected_row

    if not chart_anchor_row and selected_rows:
        chart_anchor_row = _pick_chart_anchor_row(selected_rows)

    if chart_anchor_row and selected_rows:
        try:
            kline_rows = _prepare_futures_kline_rows(
                selected_rows,
                chart_anchor_row,
                session_mode=session_mode,
            )

            print(
                "DEBUG futures chart rows",
                "| count=", len(kline_rows),
                "| selected_futures_id=", selected_futures_id,
                flush=True,
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
                    chart_anchor_row,
                )

                chart_url = _generate_futures_kline_chart(
                    rows=kline_rows,
                    futures_id=selected_futures_id,
                    futures_name=futures_name,
                    contract_date=display_contract,
                    session=display_session,
                )

            except Exception as exc:
                print(
                    "DEBUG futures chart fallback failed",
                    exc,
                    flush=True,
                )

        except Exception as exc:
            print(
                "DEBUG futures chart failed",
                exc,
                flush=True,
            )


    # =========================
    # 9. 相容舊版 FuturesSnapshot dataclass
    # =========================
    payload = {
        "available": True,
        "message": "ok",
        "stock_id": sid,
        "stock_name": stock_name,
        "futures_id": selected_display_futures_id,
        "futures_name": futures_name or f"{stock_name}期貨",
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
        "open_interest": open_interest,
        "settlement_price": settlement_price,
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
