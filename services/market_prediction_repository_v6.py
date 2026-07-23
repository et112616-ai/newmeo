from __future__ import annotations

from typing import Any

from services.supabase_service import get_supabase_client


REPOSITORY_VERSION = "2026-07-23-v6-FIXED-100PT-KEYSET"
MODEL_SELECT_COLUMNS = (
    "ts,trade_date,taiex_close,txf_close,txf_volume,target_direction"
)


def load_market_prediction_rows_paginated(
    start_date: str,
    end_date: str,
    limit: int = 50000,
    page_size: int = 1000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """以 ts 主鍵游標分頁，避開單次1,000筆與 range 相容性問題。"""
    client = get_supabase_client()
    if client is None:
        return [], {
            "ok": False,
            "complete": False,
            "rows": 0,
            "pages": 0,
            "error": "Supabase client unavailable",
        }

    safe_limit = max(1, min(int(limit or 50000), 50000))
    safe_page_size = max(100, min(int(page_size or 1000), 1000))
    rows: list[dict[str, Any]] = []
    pages = 0
    last_ts = ""

    try:
        while len(rows) < safe_limit:
            current_size = min(safe_page_size, safe_limit - len(rows))
            query = (
                client.table("market_prediction_1m")
                .select(MODEL_SELECT_COLUMNS)
                .gte("trade_date", str(start_date))
                .lte("trade_date", str(end_date))
                .order("ts", desc=False)
            )
            if last_ts:
                query = query.gt("ts", last_ts)
            response = query.limit(current_size).execute()
            page = response.data or []
            if not isinstance(page, list) or not page:
                break
            clean_page = [row for row in page if isinstance(row, dict) and row.get("ts")]
            if not clean_page:
                break
            next_last_ts = str(clean_page[-1]["ts"])
            if last_ts and next_last_ts <= last_ts:
                raise RuntimeError("Supabase ts pagination cursor did not advance")
            rows.extend(clean_page)
            pages += 1
            last_ts = next_last_ts
            if len(page) < current_size:
                break

        print(
            "DEBUG market_prediction_repository",
            "| version =", REPOSITORY_VERSION,
            "| start =", start_date,
            "| end =", end_date,
            "| rows =", len(rows),
            "| pages =", pages,
            flush=True,
        )
        result = rows[:safe_limit]
        return result, {
            "ok": True,
            "complete": True,
            "rows": len(result),
            "pages": pages,
            "last_ts": last_ts,
            "error": "",
        }
    except Exception as exc:
        print(
            "get_market_prediction_rows_paginated failed:",
            f" start={start_date}, end={end_date}, rows={len(rows)}, error={exc}",
            flush=True,
        )
        return [], {
            "ok": False,
            "complete": False,
            "rows_before_error": len(rows),
            "pages_before_error": pages,
            "last_ts": last_ts,
            "error": repr(exc),
        }
