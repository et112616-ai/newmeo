from __future__ import annotations

from typing import Any

import pandas as pd
import requests

from services.supabase_service import get_supabase_client


TAIFEX_STOCK_LIST_URL = "https://www.taifex.com.tw/enl/eng2/stockLists"


def _norm_col(col) -> str:
    if isinstance(col, tuple):
        return " ".join(str(x).strip() for x in col if str(x).strip())
    return str(col).strip()


def _find_col(columns: list[str], candidates: list[str]) -> str | None:
    for c in candidates:
        for col in columns:
            if c.lower() == col.lower():
                return col

    for c in candidates:
        for col in columns:
            if c.lower() in col.lower():
                return col

    return None


def _to_int(value) -> int | None:
    try:
        text = str(value).replace(",", "").strip()
        if not text or text.lower() == "nan":
            return None
        return int(float(text))
    except Exception:
        return None


def _build_candidate_ids(futures_code: str) -> list[str]:
    """
    期交所標的表通常是基礎代碼，例如：
    2408 南亞科 = CY

    FinMind / 行情資料可能用：
    CYF 或 CY

    所以自動產生兩種候選。
    """
    code = str(futures_code or "").strip().upper()

    if not code:
        return []

    candidates = []

    if not code.endswith("F"):
        candidates.append(f"{code}F")

    candidates.append(code)

    # 去重
    return list(dict.fromkeys(candidates))


def sync_stock_futures_map_from_taifex() -> dict[str, Any]:
    """
    從期交所股票期貨/股票選擇權標的頁面同步標準股票期貨對照表到 Supabase。

    目前只保留：
    - Stock Futures
    - 普通股票期貨
    - 契約乘數 2,000 股

    排除：
    - 小型股票期貨 100 股
    - ETF 期貨
    """
    resp = requests.get(TAIFEX_STOCK_LIST_URL, timeout=20)
    resp.raise_for_status()

    tables = pd.read_html(resp.text)

    if not tables:
        return {
            "ok": False,
            "message": "no table found",
            "count": 0,
        }

    df = tables[0].copy()
    df.columns = [_norm_col(c) for c in df.columns]

    columns = list(df.columns)

    col_futures_code = _find_col(columns, ["Ticker Symbol", "股票期貨商品代碼", "商品代碼"])
    col_stock_name = _find_col(columns, ["Underlying Stock", "標的證券", "標的證券簡稱"])
    col_stock_id = _find_col(columns, ["Stock Code", "證券代號"])
    col_stock_futures = _find_col(columns, ["Stock Futures", "股票期貨"])
    col_contract_size = _find_col(
        columns,
        [
            "Shares or beneficial units of underlying security",
            "標的證券股數",
            "契約乘數",
        ],
    )

    required = {
        "futures_code": col_futures_code,
        "stock_name": col_stock_name,
        "stock_id": col_stock_id,
        "stock_futures": col_stock_futures,
        "contract_size": col_contract_size,
    }

    missing = [k for k, v in required.items() if not v]

    if missing:
        return {
            "ok": False,
            "message": f"missing columns: {missing}",
            "columns": columns,
            "count": 0,
        }

    records: list[dict[str, Any]] = []

    for _, row in df.iterrows():
        stock_id = str(row.get(col_stock_id, "")).strip()
        futures_code = str(row.get(col_futures_code, "")).strip().upper()
        stock_name = str(row.get(col_stock_name, "")).strip()
        stock_futures_flag = str(row.get(col_stock_futures, "")).strip()
        contract_size = _to_int(row.get(col_contract_size))

        if not stock_id or not futures_code:
            continue

        # 只抓有股票期貨的項目
        if "Stock Futures" not in stock_futures_flag and "●" not in stock_futures_flag:
            continue

        # 只抓標準股票期貨：2,000 股
        if contract_size != 2000:
            continue

        records.append(
            {
                "stock_id": stock_id,
                "stock_name": stock_name,
                "futures_code": futures_code,
                "futures_name": f"{stock_name}期貨",
                "contract_size": contract_size,
                "source": "TAIFEX",
            }
        )

    if not records:
        return {
            "ok": False,
            "message": "no records parsed",
            "columns": columns,
            "count": 0,
        }

    supabase = get_supabase_client()

    supabase.table("stock_futures_map").upsert(
        records,
        on_conflict="stock_id",
    ).execute()

    return {
        "ok": True,
        "message": "synced",
        "count": len(records),
    }


def get_stock_futures_mapping(stock_id: str) -> dict[str, Any] | None:
    stock_id = str(stock_id or "").strip()

    if not stock_id:
        return None

    supabase = get_supabase_client()

    resp = (
        supabase.table("stock_futures_map")
        .select("*")
        .eq("stock_id", stock_id)
        .limit(1)
        .execute()
    )

    data = resp.data or []

    if not data:
        return None

    row = data[0]

    futures_code = str(row.get("futures_code", "")).strip().upper()

    row["candidates"] = _build_candidate_ids(futures_code)

    return row
