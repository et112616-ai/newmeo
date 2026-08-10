from __future__ import annotations

from typing import Any


def get_etf_registry(client, etf_code: str) -> dict[str, Any] | None:
    etf_code = str(etf_code or "").strip().upper()

    if not etf_code:
        return None

    response = (
        client.table("etf_registry")
        .select("*")
        .eq("etf_code", etf_code)
        .eq("active", True)
        .limit(1)
        .execute()
    )

    rows = response.data or []

    return rows[0] if rows else None
