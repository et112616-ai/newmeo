from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.supabase_service import get_supabase_client


QUALITY_VERSION = "2026-07-24-v1.1-WEIGHT-FEATURE-15M-QUOTA-SAFE"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
EXPECTED_ROWS_PER_DAY = 18
MINIMUM_ROWS_PER_DAY = 16
MINIMUM_CORE_COMPLETE_RATIO = 0.90
MINIMUM_AB_TEST_DAYS = 20
MAX_ACCEPTABLE_GAP_MINUTES = 30.0
SELECT_COLUMNS = (
    "ts,trade_date,weight_trade_date,top20_market_weight_pct,"
    "top20_contribution_points,top20_positive_weight_ratio_pct,"
    "top20_negative_weight_ratio_pct,largest_stock_id,"
    "largest_contribution_points,otc_close,otc_return_5m,"
    "otc_return_15m,taiex_return_5m,taiex_return_15m,"
    "taiex_otc_divergence_5m,taiex_otc_divergence_15m,source"
)
CORE_FEATURE_COLUMNS = [
    "top20_contribution_points",
    "top20_positive_weight_ratio_pct",
    "top20_negative_weight_ratio_pct",
    "largest_contribution_points",
    "otc_return_15m",
    "taiex_return_15m",
    "taiex_otc_divergence_15m",
]


def _date_range(
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str]:
    today = datetime.now(TAIPEI_TZ).date()
    end = date.fromisoformat(end_date) if end_date else today
    start = (
        date.fromisoformat(start_date)
        if start_date
        else end - timedelta(days=45)
    )
    if end < start:
        raise ValueError("end_date 不可早於 start_date")
    return start.isoformat(), end.isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(TAIPEI_TZ)
    except Exception:
        return None


def _is_present(value: Any) -> bool:
    return value is not None and str(value).strip() not in {"", "nan", "None"}


def _load_rows(
    start_date: str,
    end_date: str,
    limit: int = 10000,
    page_size: int = 1000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    client = get_supabase_client()
    if client is None:
        return [], {
            "ok": False,
            "complete": False,
            "rows": 0,
            "pages": 0,
            "error": "Supabase client unavailable",
        }

    safe_limit = max(1, min(int(limit or 10000), 20000))
    safe_page_size = max(100, min(int(page_size or 1000), 1000))
    rows: list[dict[str, Any]] = []
    pages = 0
    last_ts = ""

    try:
        while len(rows) < safe_limit:
            current_size = min(safe_page_size, safe_limit - len(rows))
            query = (
                client.table("market_contribution_1m")
                .select(SELECT_COLUMNS)
                .gte("trade_date", start_date)
                .lte("trade_date", end_date)
                .order("ts", desc=False)
            )
            if last_ts:
                query = query.gt("ts", last_ts)
            response = query.limit(current_size).execute()
            page = response.data or []
            if not isinstance(page, list) or not page:
                break
            clean_page = [
                row
                for row in page
                if isinstance(row, dict) and row.get("ts")
            ]
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

        return rows[:safe_limit], {
            "ok": True,
            "complete": len(rows) < safe_limit,
            "rows": min(len(rows), safe_limit),
            "pages": pages,
            "last_ts": last_ts,
            "limit_reached": len(rows) >= safe_limit,
            "error": "",
        }
    except Exception as exc:
        return [], {
            "ok": False,
            "complete": False,
            "rows_before_error": len(rows),
            "pages_before_error": pages,
            "last_ts": last_ts,
            "error": repr(exc),
        }


def _day_report(
    trade_date: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(
        (
            (parsed, row)
            for row in rows
            if (parsed := _parse_timestamp(row.get("ts"))) is not None
        ),
        key=lambda item: item[0],
    )
    timestamps = [item[0] for item in ordered]
    gaps = [
        (right - left).total_seconds() / 60.0
        for left, right in zip(timestamps, timestamps[1:])
    ]
    core_complete_rows = sum(
        all(_is_present(row.get(column)) for column in CORE_FEATURE_COLUMNS)
        for _, row in ordered
    )
    row_count = len(ordered)
    core_complete_ratio = (
        core_complete_rows / row_count if row_count else 0.0
    )
    first_time = timestamps[0].strftime("%H:%M") if timestamps else None
    last_time = timestamps[-1].strftime("%H:%M") if timestamps else None
    starts_on_time = bool(first_time and first_time <= "09:20")
    ends_on_time = bool(last_time and last_time >= "13:25")
    max_gap = max(gaps) if gaps else None
    cadence_gaps = sum(12.0 <= gap <= 18.0 for gap in gaps)
    cadence_ratio = cadence_gaps / len(gaps) if gaps else 0.0
    complete = (
        row_count >= MINIMUM_ROWS_PER_DAY
        and core_complete_ratio >= MINIMUM_CORE_COMPLETE_RATIO
        and starts_on_time
        and ends_on_time
        and max_gap is not None
        and max_gap <= MAX_ACCEPTABLE_GAP_MINUTES
    )
    return {
        "trade_date": trade_date,
        "rows": row_count,
        "expected_rows": EXPECTED_ROWS_PER_DAY,
        "coverage_pct": round(
            min(row_count / EXPECTED_ROWS_PER_DAY, 1.0) * 100.0,
            2,
        ),
        "first_time": first_time,
        "last_time": last_time,
        "core_complete_rows": core_complete_rows,
        "core_complete_ratio": round(core_complete_ratio, 6),
        "median_gap_minutes": round(
            sorted(gaps)[len(gaps) // 2],
            3,
        ) if gaps else None,
        "max_gap_minutes": round(max_gap, 3) if max_gap is not None else None,
        "fifteen_minute_cadence_ratio": round(cadence_ratio, 6),
        "complete": complete,
    }


def evaluate_market_weight_feature_quality(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    try:
        start_text, end_text = _date_range(start_date, end_date)
    except Exception as exc:
        return {
            "ok": False,
            "message": "日期格式錯誤",
            "error": repr(exc),
            "version": QUALITY_VERSION,
        }

    rows, repository = _load_rows(start_text, end_text)
    if not repository.get("ok"):
        return {
            "ok": False,
            "message": "權值特徵資料讀取失敗",
            "repository_status": repository,
            "version": QUALITY_VERSION,
        }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    timestamp_counts: Counter[str] = Counter()
    for row in rows:
        trade_date = str(row.get("trade_date") or "")
        timestamp = str(row.get("ts") or "")
        if trade_date and timestamp:
            grouped[trade_date].append(row)
            timestamp_counts[timestamp] += 1

    daily = [
        _day_report(trade_date, grouped[trade_date])
        for trade_date in sorted(grouped)
    ]
    complete_days = sum(bool(item["complete"]) for item in daily)
    total_rows = sum(int(item["rows"]) for item in daily)
    core_complete_rows = sum(
        int(item["core_complete_rows"]) for item in daily
    )
    ready_for_ab_test = complete_days >= MINIMUM_AB_TEST_DAYS
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    return {
        "ok": True,
        "message": "ok",
        "version": QUALITY_VERSION,
        "start_date": start_text,
        "end_date": end_text,
        "status": (
            "ready_for_ab_test"
            if ready_for_ab_test
            else "collecting"
        ),
        "ready_for_ab_test": ready_for_ab_test,
        "minimum_ab_test_days": MINIMUM_AB_TEST_DAYS,
        "remaining_complete_days": max(
            0,
            MINIMUM_AB_TEST_DAYS - complete_days,
        ),
        "trade_days": len(daily),
        "complete_trade_days": complete_days,
        "incomplete_trade_days": len(daily) - complete_days,
        "rows": total_rows,
        "duplicate_timestamps": sum(
            count - 1 for count in timestamp_counts.values() if count > 1
        ),
        "overall_core_complete_ratio": round(
            core_complete_rows / total_rows if total_rows else 0.0,
            6,
        ),
        "quality_definition": {
            "schedule": "每15分鐘一筆，09:15-13:30",
            "expected_rows_per_day": EXPECTED_ROWS_PER_DAY,
            "minimum_rows_per_day": MINIMUM_ROWS_PER_DAY,
            "minimum_core_complete_ratio": MINIMUM_CORE_COMPLETE_RATIO,
            "maximum_gap_minutes": MAX_ACCEPTABLE_GAP_MINUTES,
            "core_features": CORE_FEATURE_COLUMNS,
        },
        "daily": daily,
        "repository_status": repository,
        "next_step": (
            "資料已達門檻，可執行原v7與權值增強模型A/B測試。"
            if ready_for_ab_test
            else "繼續盤中每5分鐘收集；尚不改動v7模型或LINE公開訊號。"
        ),
        "seconds": round(elapsed, 3),
    }
