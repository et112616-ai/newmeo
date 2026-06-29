from __future__ import annotations

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
) -> bool:
    """
    寫入或更新千張大戶持股比率。

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

        client.table("tdcc_large_holder_history").upsert(
            data,
            on_conflict="stock_id,trade_date",
        ).execute()

        return True

    except Exception as exc:
        print(f"upsert_large_holder_history failed: stock_id={stock_id}, error={exc}")
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
            .select("stock_id,trade_date,large_holder_ratio,source")
            .eq("stock_id", str(stock_id).strip())
            .order("trade_date", desc=True)
            .limit(limit)
            .execute()
        )

        rows = res.data or []

        return rows if isinstance(rows, list) else []

    except Exception as exc:
        print(f"get_large_holder_history_rows failed: stock_id={stock_id}, error={exc}")
        return []
