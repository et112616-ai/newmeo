

from datetime import datetime
from typing import Any, Optional

from supabase import create_client

from config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

_client = None

def get_supabase_client():
    global _client

    if _client is not None:
        return _client

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        print("Supabase env missing: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        return None

    _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _client


def upsert_large_holder_history(
    stock_id: str,
    trade_date: str,
    large_holder_ratio: float,
    source: str = "TDCC",
    large_holder_people: int | None = None,
) -> bool:
    """
    寫入或更新千張大戶持股比率與千張大戶人數。

    trade_date 格式：
    2026-06-26
    """
    client = get_supabase_client()

    if client is None:
        return False

    try:
        data = {
            "stock_id": str(stock_id).strip(),
            "trade_date": trade_date,
            "large_holder_ratio": float(large_holder_ratio),
            "source": source,
            "updated_at": datetime.utcnow().isoformat(),
        }

        if large_holder_people is not None:
            data["large_holder_people"] = int(large_holder_people)

        client.table("tdcc_large_holder_history").upsert(
            data,
            on_conflict="stock_id,trade_date",
        ).execute()

        return True

    except Exception as exc:
        print(
            "upsert_large_holder_history failed:"
            f" stock_id={stock_id},"
            f" trade_date={trade_date},"
            f" large_holder_people={large_holder_people},"
            f" error={exc}",
            flush=True,
        )
        return False

def get_large_holder_history_rows(stock_id: str, limit: int = 7) -> list[dict[str, Any]]:
    """
    取得最近 N 筆大戶歷史資料。
    limit 預設 7，是為了顯示 6 筆時仍可計算最舊一筆的週變化。
    """
    client = get_supabase_client()

    if client is None:
        return []

    try:
        res = (
            client.table("tdcc_large_holder_history")
            .select("stock_id,trade_date,large_holder_ratio,large_holder_people,source")
            .eq("stock_id", str(stock_id).strip())
            .order("trade_date", desc=True)
            .limit(limit)
            .execute()
        )

        rows = res.data or []

        return rows if isinstance(rows, list) else []

    except Exception as exc:
        print(f"get_large_holder_history_rows failed: stock_id={stock_id}, error={exc}", flush=True)
        return []


def upsert_market_prediction_rows(
    rows: list[dict[str, Any]],
    batch_size: int = 500,
) -> dict[str, Any]:
    """分批寫入 TAIEX/TXF 對齊後的 1 分模型資料。"""
    client = get_supabase_client()

    if client is None:
        return {
            "success": False,
            "rows": 0,
            "message": "Supabase client unavailable",
        }

    clean_rows = [row for row in rows if isinstance(row, dict) and row.get("ts")]
    if not clean_rows:
        return {"success": True, "rows": 0, "batches": 0, "message": "no rows"}

    batch_size = max(50, min(int(batch_size or 500), 1000))
    written = 0
    batches = 0

    try:
        for offset in range(0, len(clean_rows), batch_size):
            batch = clean_rows[offset : offset + batch_size]
            client.table("market_prediction_1m").upsert(
                batch,
                on_conflict="ts",
            ).execute()
            written += len(batch)
            batches += 1

        return {
            "success": True,
            "rows": written,
            "batches": batches,
            "message": "ok",
        }

    except Exception as exc:
        print(
            "upsert_market_prediction_rows failed:"
            f" written={written}, total={len(clean_rows)}, error={exc}",
            flush=True,
        )
        return {
            "success": False,
            "rows": written,
            "batches": batches,
            "message": repr(exc),
        }


def get_market_prediction_rows(
    start_date: str,
    end_date: str,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    """讀取指定日期區間的大盤模型資料，供後續離線訓練使用。"""
    client = get_supabase_client()

    if client is None:
        return []

    try:
        safe_limit = max(1, min(int(limit or 10000), 50000))
        response = (
            client.table("market_prediction_1m")
            .select("*")
            .gte("trade_date", str(start_date))
            .lte("trade_date", str(end_date))
            .order("ts", desc=False)
            .limit(safe_limit)
            .execute()
        )
        rows = response.data or []
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        print(
            "get_market_prediction_rows failed:"
            f" start={start_date}, end={end_date}, error={exc}",
            flush=True,
        )
        return []


def upsert_market_weight_rows(
    rows: list[dict[str, Any]],
    batch_size: int = 100,
) -> dict[str, Any]:
    """寫入每日上市權重；預設只會有前20名。"""
    client = get_supabase_client()
    if client is None:
        return {
            "success": False,
            "rows": 0,
            "message": "Supabase client unavailable",
        }
    clean_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("trade_date")
        and row.get("stock_id")
    ]
    if not clean_rows:
        return {
            "success": True,
            "rows": 0,
            "batches": 0,
            "message": "no rows",
        }
    safe_batch_size = max(20, min(int(batch_size or 100), 500))
    written = 0
    batches = 0
    try:
        for offset in range(0, len(clean_rows), safe_batch_size):
            batch = clean_rows[offset : offset + safe_batch_size]
            client.table("market_weight_daily").upsert(
                batch,
                on_conflict="trade_date,stock_id",
            ).execute()
            written += len(batch)
            batches += 1
        return {
            "success": True,
            "rows": written,
            "batches": batches,
            "message": "ok",
        }
    except Exception as exc:
        print(
            "upsert_market_weight_rows failed:"
            f" written={written}, total={len(clean_rows)}, error={exc}",
            flush=True,
        )
        return {
            "success": False,
            "rows": written,
            "batches": batches,
            "message": repr(exc),
        }


def get_latest_market_weight_rows(
    before_date: str | None = None,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], str]:
    """取得指定日期以前最近一個權重交易日，防止盤中使用未來資料。"""
    client = get_supabase_client()
    if client is None:
        return [], ""
    try:
        date_query = (
            client.table("market_weight_daily")
            .select("trade_date")
            .order("trade_date", desc=True)
        )
        if before_date:
            date_query = date_query.lt("trade_date", str(before_date))
        date_response = date_query.limit(1).execute()
        date_rows = date_response.data or []
        if not isinstance(date_rows, list) or not date_rows:
            return [], ""
        weight_date = str(date_rows[0].get("trade_date") or "")
        if not weight_date:
            return [], ""
        safe_limit = max(1, min(int(limit or 20), 100))
        response = (
            client.table("market_weight_daily")
            .select(
                "trade_date,stock_id,stock_name,close_price,"
                "issued_shares,market_cap,weight_ratio,weight_rank,source"
            )
            .eq("trade_date", weight_date)
            .order("weight_rank", desc=False)
            .limit(safe_limit)
            .execute()
        )
        rows = response.data or []
        return (rows if isinstance(rows, list) else []), weight_date
    except Exception as exc:
        print(
            "get_latest_market_weight_rows failed:"
            f" before_date={before_date}, error={exc}",
            flush=True,
        )
        return [], ""


def upsert_market_contribution_row(
    row: dict[str, Any],
) -> dict[str, Any]:
    """寫入一筆盤中聚合特徵。"""
    client = get_supabase_client()
    if client is None:
        return {
            "success": False,
            "rows": 0,
            "message": "Supabase client unavailable",
        }
    if not isinstance(row, dict) or not row.get("ts"):
        return {
            "success": False,
            "rows": 0,
            "message": "invalid row",
        }
    try:
        client.table("market_contribution_1m").upsert(
            row,
            on_conflict="ts",
        ).execute()
        return {"success": True, "rows": 1, "message": "ok"}
    except Exception as exc:
        print(
            "upsert_market_contribution_row failed:",
            repr(exc),
            flush=True,
        )
        return {
            "success": False,
            "rows": 0,
            "message": repr(exc),
        }
