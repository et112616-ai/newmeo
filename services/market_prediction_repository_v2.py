from __future__ import annotations

from typing import Any

from services.supabase_service import get_supabase_client


REPOSITORY_VERSION = "2026-07-21-v2-PAGINATED-50000"


def get_market_prediction_rows_paginated(
    start_date: str,
    end_date: str,
    limit: int = 50000,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    """分頁讀取模型資料，避開 Supabase/PostgREST 單次1,000筆上限。"""
    client = get_supabase_client()
    if client is None:
        return []

    safe_limit = max(1, min(int(limit or 50000), 50000))
    safe_page_size = max(100, min(int(page_size or 1000), 1000))
    rows: list[dict[str, Any]] = []

    try:
        for offset in range(0, safe_limit, safe_page_size):
            current_size = min(safe_page_size, safe_limit - offset)
            response = (
                client.table("market_prediction_1m")
                .select("*")
                .gte("trade_date", str(start_date))
                .lte("trade_date", str(end_date))
                .order("ts", desc=False)
                .range(offset, offset + current_size - 1)
                .execute()
            )
            page = response.data or []
            if not isinstance(page, list) or not page:
                break
            rows.extend(row for row in page if isinstance(row, dict))
            if len(page) < current_size:
                break

        print(
            "DEBUG market_prediction_repository",
            "| version =", REPOSITORY_VERSION,
            "| start =", start_date,
            "| end =", end_date,
            "| rows =", len(rows),
            flush=True,
        )
        return rows[:safe_limit]
    except Exception as exc:
        print(
            "get_market_prediction_rows_paginated failed:",
            f" start={start_date}, end={end_date}, rows={len(rows)}, error={exc}",
            flush=True,
        )
        return []
