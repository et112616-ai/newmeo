from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests
import yfinance as yf
import matplotlib
matplotlib.use("Agg")

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import pandas as pd

from services.upload_service import publish_figure
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
    只接受單一契約月份。

    接受：
    202607
    2026/07
    2026-07

    排除：
    202608/202609  # 跨月價差
    全月份
    所有契約
    空白
    """
    s = str(value or "").strip()

    if not s:
        return ""

    lower = s.lower()

    if "all" in lower or "全" in s or "所有" in s:
        return ""

    # 很重要：202608/202609 是跨月價差，不是近月單一契約
    # 但 2026/07 是日期格式，要保留。
    digits = "".join(ch for ch in s if ch.isdigit())

    if "/" in s:
        parts = s.split("/")

        # 2026/07 這種可以接受
        if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
            if len(digits) >= 6:
                return digits[:6]

        # 202608/202609 這種跨月價差要排除
        return ""

    if "-" in s:
        parts = s.split("-")

        # 2026-07 這種可以接受
        if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
            if len(digits) >= 6:
                return digits[:6]

    if len(digits) == 6:
        return digits

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

def _is_position_session(value: Any) -> bool:
    text = str(value or "").strip().lower()

    return (
        "position" in text
        or "open_interest" in text
        or "未沖銷" in text
        or "部位" in text
    )


def _is_valid_trade_row(row: dict) -> bool:
    """
    排除 position / 跨月價差 / 無價格資料。
    只保留真的可以拿來當 K 線的單一近月期貨交易資料。
    """
    if _is_position_session(row.get("trading_session")):
        return False

    contract = _normalize_contract_date(row.get("contract_date"))

    if not contract:
        return False

    price = _row_price(row)

    if price <= 0:
        return False

    return True
    
def _prepare_futures_kline_rows(rows: list[dict], selected_row: dict) -> list[dict]:
    """
    依照選到的近月契約，準備 K 線資料。

    規則：
    - 只取同一個近月 contract_date
    - 如果選到的是盤後，優先畫盤後
    - 如果沒有盤後，就畫日盤
    - 排除 position 資料
    """
    contract = selected_row.get("_contract_norm") or _normalize_contract_date(
        selected_row.get("contract_date")
        or selected_row.get("delivery_month")
        or selected_row.get("due_month")
        or selected_row.get("settlement_month")
    )

    if not contract:
        return []

    prefer_afterhours = _is_afterhours_session(selected_row.get("trading_session"))

    same_contract_rows = []

    for r in rows:
        r_contract = _normalize_contract_date(
            r.get("contract_date")
            or r.get("delivery_month")
            or r.get("due_month")
            or r.get("settlement_month")
        )

        if r_contract != contract:
            continue

        if not _is_valid_trade_row(r):
            continue

        item = dict(r)
        item["_trade_date_norm"] = _normalize_trade_date(r.get("date"))
        item["_contract_norm"] = r_contract

        if not item["_trade_date_norm"]:
            continue

        same_contract_rows.append(item)

    if not same_contract_rows:
        return []

    if prefer_afterhours:
        preferred = [
            r for r in same_contract_rows
            if _is_afterhours_session(r.get("trading_session"))
        ]

        if preferred:
            same_contract_rows = preferred
    else:
        preferred = [
            r for r in same_contract_rows
            if _is_regular_session(r.get("trading_session"))
        ]

        if preferred:
            same_contract_rows = preferred

    same_contract_rows = sorted(
        same_contract_rows,
        key=lambda r: r["_trade_date_norm"]
    )

    # 同一天若有重複，保留最後一筆
    by_date = {}

    for r in same_contract_rows:
        by_date[r["_trade_date_norm"]] = r

    return list(by_date.values())[-30:]


def _generate_futures_kline_chart(
    rows: list[dict],
    futures_id: str,
    futures_name: str,
    contract_date: str,
    session: str,
) -> str:
    """
    產生股票期貨 K 線圖。
    """
    if not rows:
        return ""

    chart_rows = []

    for r in rows:
        date = _normalize_trade_date(r.get("date"))
        close = _row_price(r)

        if not date or close <= 0:
            continue

        open_price = _safe_float(r.get("open") or r.get("Open") or close)
        high_price = _safe_float(r.get("max") or r.get("high") or r.get("High") or close)
        low_price = _safe_float(r.get("min") or r.get("low") or r.get("Low") or close)
        volume = _safe_int(r.get("volume"))

        if open_price <= 0:
            open_price = close

        if high_price <= 0:
            high_price = max(open_price, close)

        if low_price <= 0:
            low_price = min(open_price, close)

        chart_rows.append(
            {
                "date": date,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close,
                "volume": volume,
            }
        )

    if not chart_rows:
        return ""

    df = pd.DataFrame(chart_rows)

    fig = plt.figure(figsize=(7, 5.5), dpi=120, facecolor="white")
    gs = gridspec.GridSpec(2, 1, height_ratios=[3, 1], hspace=0.05)

    ax_k = fig.add_subplot(gs[0])
    ax_v = fig.add_subplot(gs[1], sharex=ax_k)

    ax_k.set_facecolor("#F8F9FA")
    ax_v.set_facecolor("#F8F9FA")

    x = range(len(df))
    width = 0.58

    for i, row in df.iterrows():
        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])

        color = "#FF3B30" if c >= o else "#34C759"

        ax_k.vlines(i, l, h, linewidth=1, color=color)

        lower = min(o, c)
        height = abs(c - o) or 0.01

        ax_k.bar(
            i,
            height,
            bottom=lower,
            width=width,
            color=color,
            align="center",
        )

        ax_v.bar(
            i,
            int(row["volume"]),
            width=width,
            color=color,
        )

    ax_k.set_title(
        f"{futures_name} {futures_id} {contract_date} {session} K線",
        fontsize=13,
        fontweight="bold",
    )

    ax_k.grid(True, linestyle=":", alpha=0.45)
    ax_v.grid(True, linestyle=":", alpha=0.45)
    ax_v.set_ylabel("Volume", fontsize=8)

    labels = [str(d)[5:] for d in df["date"].tolist()]
    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels([labels[i] for i in ticks], rotation=0, fontsize=8)

    plt.setp(ax_k.get_xticklabels(), visible=False)

    fig.tight_layout()

    return publish_figure(fig, f"{futures_id}_futures_kline")

def _pick_near_month_prefer_afterhours(rows: list[dict]) -> dict | None:
    """
    選資料規則：

    1. 排除 position
    2. 排除跨月價差，例如 202608/202609
    3. 排除價格為 0 的資料
    4. 找最新有效交易日
    5. 最新交易日中，contract_date 最小者 = 近月
    6. 同一近月中，盤後優先
    7. 沒盤後，用日盤
    """
    valid_rows: list[dict] = []

    for r in rows:
        if not _is_valid_trade_row(r):
            continue

        trade_date = _normalize_trade_date(r.get("date"))
        contract = _normalize_contract_date(r.get("contract_date"))

        if not trade_date or not contract:
            continue

        item = dict(r)
        item["_trade_date_norm"] = trade_date
        item["_contract_norm"] = contract

        valid_rows.append(item)

    print("DEBUG futures valid_rows_count =", len(valid_rows))

    if valid_rows:
        print("DEBUG futures valid_rows_last =", valid_rows[-1])

    if not valid_rows:
        return None

    latest_date = max(r["_trade_date_norm"] for r in valid_rows)

    latest_rows = [
        r for r in valid_rows
        if r["_trade_date_norm"] == latest_date
    ]

    print("DEBUG futures latest_date =", latest_date)
    print("DEBUG futures latest_rows_count =", len(latest_rows))

    if not latest_rows:
        return None

    near_contract = min(r["_contract_norm"] for r in latest_rows)

    near_rows = [
        r for r in latest_rows
        if r["_contract_norm"] == near_contract
    ]

    print("DEBUG futures near_contract =", near_contract)
    print("DEBUG futures near_rows_count =", len(near_rows))

    if not near_rows:
        return None

    afterhours_rows = [
        r for r in near_rows
        if _is_afterhours_session(r.get("trading_session"))
    ]

    if afterhours_rows:
        print("DEBUG futures choose afterhours =", afterhours_rows[-1])
        return afterhours_rows[-1]

    regular_rows = [
        r for r in near_rows
        if _is_regular_session(r.get("trading_session"))
    ]

    if regular_rows:
        print("DEBUG futures choose regular =", regular_rows[-1])
        return regular_rows[-1]

    print("DEBUG futures choose fallback =", near_rows[-1])
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
        "close_price",
        "last_price",
        "成交價",
        "最後成交價",
        "收盤價",
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

    print("DEBUG futures start:", sid, stock_name, futures_name, candidates)

    if not candidates:
        return FuturesSnapshot(
            available=False,
            message="這檔股票尚未建立標準股票期貨代號對照。",
            stock_id=sid,
            stock_name=stock_name,
        )

    selected_row: dict | None = None
    selected_futures_id = ""
    selected_rows: list[dict] = []

    for futures_id in candidates:
        rows = _request_finmind_futures_daily(futures_id)

        print(f"DEBUG futures candidate={futures_id}, rows_count={len(rows)}")

        if rows:
            print("DEBUG futures sample row:", rows[-1])

        if not rows:
            continue

        row = _pick_near_month_prefer_afterhours(rows)

        print(f"DEBUG selected row for {futures_id}:", row)

        if row:
            selected_row = row
            selected_futures_id = futures_id
            selected_rows = rows
            break

    if not selected_row:
        print("DEBUG futures result: no selected_row")
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

    display_contract = _format_contract_date(selected_row.get("contract_date"))
    display_session = _display_session(selected_row.get("trading_session"))

    kline_rows = _prepare_futures_kline_rows(selected_rows, selected_row)

    print("DEBUG kline_rows_count =", len(kline_rows))
    if kline_rows:
        print("DEBUG kline_rows_last =", kline_rows[-1])

    chart_url = _generate_futures_kline_chart(
        rows=kline_rows,
        futures_id=selected_futures_id,
        futures_name=futures_name,
        contract_date=display_contract,
        session=display_session,
    )

    print("DEBUG chart_url =", chart_url)

    return FuturesSnapshot(
        available=True,
        message="ok",
        stock_id=sid,
        stock_name=stock_name,
        futures_id=str(selected_row.get("futures_id") or selected_futures_id),
        futures_name=futures_name,
        contract_date=display_contract,
        trade_date=_normalize_trade_date(selected_row.get("date")),
        trading_session=display_session,
        chart_url=chart_url,
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
