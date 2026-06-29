from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from config import FINMIND_TOKEN


FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

TDCC_LATEST_CSV_URLS = [
    "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",
    "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5",
]


def _today() -> datetime.date:
    return datetime.utcnow().date()


def _start_date(days: int = 90) -> str:
    return (_today() - timedelta(days=days)).strftime("%Y-%m-%d")


def _recent_dates(n: int) -> list[str]:
    today = _today()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def _clean_stock_id(stock_id: str) -> str:
    return str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()


def _request_finmind(
    dataset: str,
    stock_id: str,
    start_date: str,
    end_date: Optional[str] = None,
) -> list[dict]:
    """
    FinMind v4 通用查詢。
    重要：不要在 log 印完整 URL，避免 token 外洩。
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

        if res.status_code >= 400:
            print(
                f"_request_finmind failed: "
                f"dataset={dataset}, stock_id={stock_id}, status={res.status_code}, "
                f"body={res.text[:200]}"
            )
            return []

        payload = res.json()

        if payload.get("status") not in (None, 200, "200", True):
            print(
                f"FinMind status warning: "
                f"dataset={dataset}, status={payload.get('status')}, msg={payload.get('msg')}"
            )

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
    20260626 -> 06/26
    """
    if not date_str:
        return "--"

    s = str(date_str).strip()

    try:
        if len(s) >= 10 and "-" in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%m/%d")

        if len(s) >= 8 and s[:8].isdigit():
            return datetime.strptime(s[:8], "%Y%m%d").strftime("%m/%d")

        return s

    except Exception:
        return s


# ============================================================
# 法人
# ============================================================

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

    result = {
        "foreign": [],
        "trust": [],
        "dealer": [],
    }

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

    temp = {
        "foreign": {},
        "trust": {},
        "dealer": {},
    }

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


# ============================================================
# 大戶：FinMind + TDCC OpenData
# ============================================================

def _is_large_holder_level(level_raw: Any) -> bool:
    """
    判斷是否為千張以上。

    TDCC 常見持股分級：
    15 = 1,000,001 股以上

    有些資料源可能用文字：
    1,000,001以上 / 1000001以上
    """
    text = str(level_raw or "").replace(",", "").replace(" ", "").strip()

    if not text:
        return False

    if "合計" in text or "total" in text.lower():
        return False

    if "1000001" in text and ("以上" in text or "up" in text.lower()):
        return True

    if "1000000" in text and ("以上" in text or "up" in text.lower()):
        return True

    try:
        level_num = int(float(text))

        # TDCC 最新 CSV 通常用 15 代表 1,000,001 股以上。
        return level_num >= 15

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


def _large_holder_from_finmind_rows(rows: list[dict]) -> list[dict]:
    by_date: dict[str, float] = {}

    for r in rows:
        date = str(r.get("date", ""))[:10]

        if not date:
            continue

        level_raw = (
            r.get("HoldingSharesLevel")
            or r.get("holding_shares_level")
            or r.get("level")
            or r.get("持股分級")
            or r.get("Securities Holding Range")
            or ""
        )

        if not _is_large_holder_level(level_raw):
            continue

        by_date[date] = by_date.get(date, 0.0) + _extract_holder_percent(r)

    items = sorted(by_date.items())

    if not items:
        return []

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


def _read_tdcc_csv(text: str) -> pd.DataFrame:
    """
    讀取 TDCC 集保 CSV。
    有些環境讀到的 CSV 可能沒有表頭，因此做兩段式相容。
    """
    text = text.replace("\ufeff", "").strip()

    if not text:
        return pd.DataFrame()

    try:
        df = pd.read_csv(StringIO(text), dtype=str)
        df.columns = [str(c).strip() for c in df.columns]

        if "資料日期" in df.columns and "證券代號" in df.columns:
            return df.fillna("")

    except Exception:
        pass

    try:
        df = pd.read_csv(
            StringIO(text),
            dtype=str,
            header=None,
            names=[
                "資料日期",
                "證券代號",
                "持股分級",
                "人數",
                "股數",
                "占集保庫存數比例%",
            ],
        )

        return df.fillna("")

    except Exception as exc:
        print(f"_read_tdcc_csv failed: {exc}")
        return pd.DataFrame()


def _request_tdcc_latest_rows(stock_id: str) -> list[dict]:
    """
    抓 TDCC 最新一週全市場集保戶股權分散表 CSV。
    這個來源不用 FinMind token。
    """
    sid = _clean_stock_id(stock_id)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    }

    for url in TDCC_LATEST_CSV_URLS:
        try:
            res = requests.get(url, headers=headers, timeout=25)

            if res.status_code >= 400:
                print(f"_request_tdcc_latest_rows failed: status={res.status_code}, url={url}")
                continue

            text = res.content.decode("utf-8-sig", errors="ignore")
            df = _read_tdcc_csv(text)

            if df.empty:
                print(f"_request_tdcc_latest_rows empty csv: url={url}")
                continue

            df.columns = [str(c).strip() for c in df.columns]

            required = [
                "資料日期",
                "證券代號",
                "持股分級",
                "占集保庫存數比例%",
            ]

            if not all(c in df.columns for c in required):
                print(f"_request_tdcc_latest_rows missing columns: {list(df.columns)}")
                continue

            df["證券代號"] = df["證券代號"].astype(str).str.strip()
            target = df[df["證券代號"] == sid]

            if target.empty:
                print(f"_request_tdcc_latest_rows no stock: stock_id={sid}, url={url}")
                continue

            return target.to_dict("records")

        except Exception as exc:
            print(f"_request_tdcc_latest_rows failed: stock_id={sid}, error={exc}")

    return []


def _large_holder_from_tdcc_latest_rows(rows: list[dict]) -> list[dict]:
    """
    TDCC OpenData CSV 通常只有最新一週，
    因此這裡只能回最新日期一筆。
    """
    if not rows:
        return []

    by_date: dict[str, float] = {}

    for r in rows:
        date = str(r.get("資料日期", "")).strip()
        level = r.get("持股分級", "")

        if not date:
            continue

        if not _is_large_holder_level(level):
            continue

        ratio = _to_float(r.get("占集保庫存數比例%"))

        by_date[date] = by_date.get(date, 0.0) + ratio

    items = sorted(by_date.items())

    if not items:
        return []

    date, ratio = items[-1]

    return [
        {
            "date": _fmt_md(date),
            "ratio": f"{ratio:.2f}%",
            "diff": "--",
        }
    ]


def _large_holder_unavailable(reason: str = "資料未取得") -> list[dict]:
    return [
        {
            "date": "--",
            "ratio": "資料未取得",
            "diff": reason,
        }
    ]


def get_large_holder_table(stock_id: str) -> list[dict]:
    """
    千張大戶持股比率。

    優先順序：
    1. FinMind TaiwanStockHoldingSharesPer：若帳號有權限，可取得歷史多週。
    2. TDCC OpenData CSV：免費官方資料，通常只有最新一週。
    3. 都失敗則顯示資料未取得，不再回傳假日期。
    """
    start_date = _start_date(120)

    finmind_rows = _request_finmind(
        dataset="TaiwanStockHoldingSharesPer",
        stock_id=stock_id,
        start_date=start_date,
    )

    if finmind_rows:
        output = _large_holder_from_finmind_rows(finmind_rows)

        if output:
            return output

    tdcc_rows = _request_tdcc_latest_rows(stock_id)

    if tdcc_rows:
        output = _large_holder_from_tdcc_latest_rows(tdcc_rows)

        if output:
            return output

    return _large_holder_unavailable("TDCC/FinMind皆無資料")


# ============================================================
# 融資券
# ============================================================

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
