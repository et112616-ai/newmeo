from __future__ import annotations

from datetime import datetime
from typing import Any


REPOSITORY_VERSION = "2026-07-27-v8.2-HYBRID-ISOLATED-SHADOW"
SHADOW_TABLE = "market_prediction_shadow_predictions_v8_2"


def _client():
    from services.supabase_service import get_supabase_client

    return get_supabase_client()


def upsert_shadow_prediction(row: dict[str, Any]) -> dict[str, Any]:
    client = _client()
    if client is None:
        return {
            "success": False,
            "rows": 0,
            "message": "Supabase client unavailable",
        }
    if not isinstance(row, dict) or not row.get("prediction_ts"):
        return {
            "success": False,
            "rows": 0,
            "message": "invalid prediction row",
        }
    try:
        payload = dict(row)
        payload["updated_at"] = datetime.utcnow().isoformat()
        client.table(SHADOW_TABLE).upsert(
            payload,
            on_conflict="prediction_ts",
        ).execute()
        return {"success": True, "rows": 1, "message": "ok"}
    except Exception as exc:
        print(
            "v8.2 upsert_shadow_prediction failed:",
            repr(exc),
            flush=True,
        )
        return {"success": False, "rows": 0, "message": repr(exc)}


def get_latest_shadow_prediction() -> dict[str, Any] | None:
    client = _client()
    if client is None:
        return None
    try:
        response = (
            client.table(SHADOW_TABLE)
            .select("*")
            .order("prediction_ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
    except Exception as exc:
        print(
            "v8.2 get_latest_shadow_prediction failed:",
            repr(exc),
            flush=True,
        )
    return None


def get_unsettled_shadow_predictions(
    trade_date: str,
    horizon_before: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    client = _client()
    if client is None or not trade_date or not horizon_before:
        return []
    safe_limit = max(1, min(int(limit or 100), 500))
    try:
        response = (
            client.table(SHADOW_TABLE)
            .select(
                "prediction_ts,horizon_ts,trade_date,base_taiex_close,"
                "signal,status"
            )
            .eq("trade_date", str(trade_date))
            .eq("status", "pending")
            .lte("horizon_ts", str(horizon_before))
            .order("horizon_ts", desc=False)
            .limit(safe_limit)
            .execute()
        )
        rows = response.data or []
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        print(
            "v8.2 get_unsettled_shadow_predictions failed:",
            repr(exc),
            flush=True,
        )
        return []


def update_shadow_result(
    prediction_ts: str,
    values: dict[str, Any],
) -> bool:
    client = _client()
    if (
        client is None
        or not str(prediction_ts or "").strip()
        or not isinstance(values, dict)
    ):
        return False
    try:
        payload = dict(values)
        payload["updated_at"] = datetime.utcnow().isoformat()
        (
            client.table(SHADOW_TABLE)
            .update(payload)
            .eq("prediction_ts", str(prediction_ts).strip())
            .execute()
        )
        return True
    except Exception as exc:
        print("v8.2 update_shadow_result failed:", repr(exc), flush=True)
        return False


def get_shadow_history(
    start_date: str,
    end_date: str,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    client = _client()
    if client is None:
        return []
    safe_limit = max(1, min(int(limit or 10000), 10000))
    try:
        response = (
            client.table(SHADOW_TABLE)
            .select(
                "prediction_ts,horizon_ts,trade_date,base_taiex_close,"
                "signal,event_probability,up_probability,"
                "direction_confidence,status,actual_close,"
                "actual_change_points,actual_direction,is_correct,"
                "model_version,artifact_key"
            )
            .gte("trade_date", str(start_date))
            .lte("trade_date", str(end_date))
            .order("prediction_ts", desc=False)
            .limit(safe_limit)
            .execute()
        )
        rows = response.data or []
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        print("v8.2 get_shadow_history failed:", repr(exc), flush=True)
        return []
