from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List

import requests

from config import FINMIND_TOKEN


def _recent_dates(n: int) -> list[str]:
    today = datetime.utcnow().date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


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
    第一版：FinMind Token 有設定時嘗試抓資料；失敗則 mock fallback，避免 LINE 流程中斷。
    """
    if not FINMIND_TOKEN:
        return _mock_institutional()

    try:
        # FinMind dataset 名稱可能依版本異動；因此保留 fallback。
        start_date = (datetime.utcnow().date() - timedelta(days=30)).strftime("%Y-%m-%d")
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": stock_id.replace(".TW", ""),
            "start_date": start_date,
            "token": FINMIND_TOKEN,
        }
        res = requests.get(url, params=params, timeout=12)
        res.raise_for_status()
        payload = res.json()
        rows = payload.get("data") or []
        if not rows:
            return _mock_institutional()

        result = {"foreign": [], "trust": [], "dealer": []}
        key_map = {
            "Foreign_Investor": "foreign",
            "Foreign_Dealer_Self": "foreign",
            "Investment_Trust": "trust",
            "Dealer_self": "dealer",
            "Dealer_Hedging": "dealer",
        }
        # 依日期彙總，避免同一類別多筆分散。
        temp = {"foreign": {}, "trust": {}, "dealer": {}}
        for r in rows:
            name = r.get("name") or r.get("institutional_investors") or ""
            section = key_map.get(name)
            if not section:
                continue
            date = r.get("date", "--")
            value = r.get("buy", 0) - r.get("sell", 0) if "buy" in r and "sell" in r else r.get("buy_sell", 0)
            temp[section][date] = temp[section].get(date, 0) + float(value or 0)
        for section in result:
            items = sorted(temp[section].items())[-10:]
            result[section] = [{"date": d, "buy_sell": v} for d, v in items]
        if all(not result[k] for k in result):
            return _mock_institutional()
        return result
    except Exception:
        return _mock_institutional()


def get_large_holder_table(stock_id: str) -> list[dict]:
    # 第一版使用 mock fallback；後續可在此接集保。
    return [
        {"date": "06/18", "ratio": "65.42%", "diff": "+0.23%"},
        {"date": "06/12", "ratio": "65.19%", "diff": "-0.05%"},
        {"date": "06/05", "ratio": "65.24%", "diff": "+0.11%"},
        {"date": "05/29", "ratio": "65.13%", "diff": "+0.02%"},
        {"date": "05/22", "ratio": "65.11%", "diff": "-0.45%"},
        {"date": "05/15", "ratio": "65.56%", "diff": "+0.08%"},
    ]


def get_margin_table(stock_id: str) -> list[dict]:
    # 第一版使用 mock fallback；後續可接 FinMind TaiwanStockMarginPurchaseShortSale。
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
