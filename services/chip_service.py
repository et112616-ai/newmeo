from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

from config import FINMIND_TOKEN


FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


def _today() -> datetime.date:
    return datetime.utcnow().date()


def _start_date(days: int = 90) -> str:
    return (_today() - timedelta(days=days)).strftime("%Y-%m-%d")


def _recent_dates(n: int) -> list[str]:
    today = _today()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def _clean_stock_id(stock_id: str) -> str:
    return str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()


def _request_finmind(dataset: str, stock_id: str, start_date: str, end_date: Optional[str] = None) -> list[dict]:
    """
    FinMind v4 通用查詢。
    有 FINMIND_TOKEN 就帶 token；沒有 token 也先嘗試查詢，失敗才 fallback。
    """
    params = {
        "dataset": dataset,
        "data_id": _clean_stock_id(stock_id),
        "start_date": start_date,
    }

    if end_date:
        params["end_date"] = end_date

    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    try:
        res = requests.get(FINMIND_URL, params=params, timeout=15)
        res.raise_for_status()
        payload = res.json()

        if payload.get("status") not in (None, 200, "200", True):
            print(f"FinMind status warning: {dataset} {payload.get('status')} {payload.get('msg')}")

        rows = payload.get("data") or []
        return rows if isinstance(rows, list) else []

    except Exception as exc:
        print(f"_request_finmind failed: dataset={dataset}, stock_id={stock_id}, error={exc}")
        return []


def _to_float(value: Any, default: float = 0.0) -> float:
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


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_to_float(value, float(default))))
    except Exception:
        return default


def _fmt_md(date_str: str) -> str:
    """
    2026-06-26 -> 06/26
    6/26 -> 6/26
    """
    if not date_str:
        return "--"

    try:
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d").strftime("%m/%d")
    except Exception:
        return str(date_str)


def _mock_institutional() -> Dict[str, List[dict]]:
    dates = _recent_dates(10)
    base = [1200, -850, 430, 2100, -1500, 600, -300, 900, -450, 2300]
    return {
        "foreign": [{"date": d, "buy_sell": v} for d, v in zip(dates, base)],
        "trust": [{"date": d, "buy_sell": int(v * 0.25)} for d, v in zip(dates, base)],
        "dealer": [{"date": d, "buy_sell": int(v * -0.15)} for d, v in zip(dates, base)],
    }


def get_institutional_chips(stock_id: str) -> Dict[str, List[dict]]:
    """
    三大法人買賣超。
    優先嘗試 FinMind，失敗才 mock fallback。
    """
    start_date = _start_date(45)

    dataset_candidates = [
        "TaiwanStockInstitutionalInvestorsBuySell",
        "InstitutionalInvestorsBuySell",
    ]

    rows: list[dict] = []
    for dataset in dataset_candidates:
        rows = _request_finmind(dataset, stock_id, start_date)
        if rows:
            break

    if not rows:
        return _mock_institutional()

    result = {"foreign": [], "trust": [], "dealer": []}

    # 不同版本欄位可能略有不同，所以用多組名稱相容。
    key_map = {
        "Foreign_Investor": "foreign",
        "Foreign_Investor_Self": "foreign",
        "Foreign_Dealer_Self": "foreign",
        "外資": "foreign",
        "外資及陸資": "foreign",
        "Investment_Trust": "trust",
        "投信": "trust",
        "Dealer_self": "dealer",
        "Dealer_Hedging": "dealer",
        "Dealer": "dealer",
        "自營商": "dealer",
        "自營商自行買賣": "dealer",
        "自營商避險": "dealer",
    }

    temp = {"foreign": {}, "trust": {}, "dealer": {}}

    for r in rows:
        name = (
            r.get("name")
            or r.get("institutional_investors")
            or r.get("investor")
            or r.get("type")
            or ""
        )

        section = key_map.get(str(name).strip())

        if not section:
            continue

        date = str(r.get("date", ""))[:10]

        if not date:
            continue

        if "buy" in r and "sell" in r:
            value = _to_float(r.get("buy")) - _to_float(r.get("sell"))
        else:
            value = (
                _to_float(r.get("buy_sell"))
                or _to_float(r.get("buy_sell_amount"))
                or _to_float(r.get("net_buy_sell"))
            )

        temp[section][date] = temp[section].get(date, 0.0) + value

    for section in result:
        items = sorted(temp[section].items())[-10:]
        result[section] = [{"date": d, "buy_sell": v} for d, v in items]

    if all(not result[k] for k in result):
        return _mock_institutional()

    return result


def _mock_large_holder_table() -> list[dict]:
    return [
        {"date": "06/18", "ratio": "65.42%", "diff": "+0.23%"},
        {"date": "06/12", "ratio": "65.19%", "diff": "-0.05%"},
        {"date": "06/05", "ratio": "65.24%", "diff": "+0.11%"},
        {"date": "05/29", "ratio": "65.13%", "diff": "+0.02%"},
        {"date": "05/22", "ratio": "65.11%", "diff": "-0.45%"},
        {"date": "05/15", "ratio": "65.56%", "diff": "+0.08%"},
    ]


def _is_large_holder_row(row: dict) -> bool:
    """
    判斷是否為「千張以上」級距。
    FinMind / 集保欄位可能是：
    - HoldingSharesLevel
    - level
    - 持股分級
    - Securities Holding Range

    千張 = 1,000,000 股以上。
    """
    level_raw = (
        row.get("HoldingSharesLevel")
        or row.get("holding_shares_level")
        or row.get("level")
        or row.get("持股分級")
        or row.get("Securities Holding Range")
        or ""
    )

    text = str(level_raw).replace(",", "").replace(" ", "")

    if "合計" in text or "total" in text.lower():
        return False

    # 常見文字級距：1,000,001以上 / 1000001以上
    if "1000001" in text and ("以上" in text or "up" in text.lower()):
        return True

    if "1000000" in text and ("以上" in text or "up" in text.lower()):
        return True

    # 常見數字級距：16 或以上多半是千張以上；保留 17 也納入，避免不同版本定義差異。
    try:
        level_num = int(float(text))
        return level_num >= 16
    except Exception:
        return False


def _extract_holder_percent(row: dict) -> float:
    candidates = [
        "percentage",
        "percent",
        "rate",
        "ratio",
        "占集保庫存數比例%",
        "占集保庫存數比例",
        "占集保庫存比例%",
        "占集保庫存比例",
    ]

    for key in candidates:
        if key in row:
            return _to_float(row.get(key))

    return 0.0


def get_large_holder_table(stock_id: str) -> list[dict]:
    """
    千張大戶持股比率。
    優先使用 FinMind 的 TaiwanStockHoldingSharesPer。
    若 API 沒資料或欄位不符，才 fallback mock。

    注意：
    - 此資料來自集保股權分散表邏輯，通常是週資料。
    - 最新週資料是否已更新，要看資料源更新時間。
    """
    start_date = _start_date(120)

    rows = _request_finmind(
        dataset="TaiwanStockHoldingSharesPer",
        stock_id=stock_id,
        start_date=start_date,
    )

    if not rows:
        return _mock_large_holder_table()

    # 依日期彙總千張以上級距比例
    by_date: dict[str, float] = {}

    for r in rows:
        date = str(r.get("date", ""))[:10]

        if not date:
            continue

        if not _is_large_holder_row(r):
            continue

        by_date[date] = by_date.get(date, 0.0) + _extract_holder_percent(r)

    items = sorted(by_date.items())

    if not items:
        return _mock_large_holder_table()

    # 取最近 6 週，由新到舊
    last_items = items[-6:]
    output: list[dict] = []

    for idx in range(len(last_items) - 1, -1, -1):
        date, ratio = last_items[idx]

        if idx > 0:
            prev_ratio = last_items[idx - 1][1]
            diff = ratio - prev_ratio
        else:
            diff = 0.0

        output.append(
            {
                "date": _fmt_md(date),
                "ratio": f"{ratio:.2f}%",
                "diff": f"{diff:+.2f}%",
            }
        )

    return output


def _mock_margin_table() -> list[dict]:
    rows = [
        {"date": "6/23", "margin": 12450, "short": 1200},
        {"date": "6/22", "margin": 12100, "short": 1250},
        {"date": "6/19", "margin": 11950, "short": 1100},
        {"date": "6/18", "margin": 12000, "short": 1050},
        {"date": "6/17", "margin": 12200, "short": 980},
        {"date": "6/16", "margin": 12150, "short": 1020},
        {"date": "6/15", "margin": 11800, "short": 950},
        {"date": "6/12", "margin": 11900, "short": 900},
        {"date": "6/11", "margin": 11750, "short": 880},
        {"date": "6/10", "margin": 11600, "short": 850},
    ]

    for r in rows:
        r["ratio"] = f"{(r['short'] / r['margin'] * 100):.2f}%" if r["margin"] else "--"

    return rows


def _pick_first_number(row: dict, keys: list[str]) -> float:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return _to_float(row.get(key))
    return 0.0


def get_margin_table(stock_id: str) -> list[dict]:
    """
    融資券 10 日動態。
    優先使用 FinMind TaiwanStockMarginPurchaseShortSale。
    融券比 = 融券餘額 / 融資餘額 * 100%
    """
    start_date = _start_date(45)

    rows = _request_finmind(
        dataset="TaiwanStockMarginPurchaseShortSale",
        stock_id=stock_id,
        start_date=start_date,
    )

    if not rows:
        return _mock_margin_table()

    rows = sorted(rows, key=lambda r: str(r.get("date", "")))

    output: list[dict] = []

    for r in rows[-10:][::-1]:
        date = str(r.get("date", ""))[:10]

        margin_balance = _pick_first_number(
            r,
            [
                "MarginPurchaseTodayBalance",
                "MarginPurchaseTodayBalanceAmount",
                "margin_purchase_today_balance",
                "融資餘額",
                "MarginPurchaseBalance",
            ],
        )

        short_balance = _pick_first_number(
            r,
            [
                "ShortSaleTodayBalance",
                "ShortSaleTodayBalanceAmount",
                "short_sale_today_balance",
                "融券餘額",
                "ShortSaleBalance",
            ],
        )

        ratio = (short_balance / margin_balance * 100) if margin_balance else 0.0

        output.append(
            {
                "date": _fmt_md(date),
                "margin": _to_int(margin_balance),
                "short": _to_int(short_balance),
                "ratio": f"{ratio:.2f}%" if margin_balance else "--",
            }
        )

    return output or _mock_margin_table()
