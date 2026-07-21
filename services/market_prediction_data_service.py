from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from services.market_future_kline_service import get_market_future_1m_history
from services.market_index_service import get_market_index_1m_history
from services.supabase_service import upsert_market_prediction_rows


MARKET_PREDICTION_DATA_VERSION = "2026-07-21-v1-TAIEX-TXF-15M-DATASET"
TAIPEI_TZ = "Asia/Taipei"
SESSION_START = "09:00"
SESSION_END = "13:30"
LAST_LABEL_TIME = "13:15"
MAX_REQUEST_DAYS = 30


def _debug(*args: Any) -> None:
    print("DEBUG market_prediction_data |", *args, flush=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else default
    except Exception:
        return default


def _validate_date_range(
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str]:
    today = datetime.now(ZoneInfo(TAIPEI_TZ)).date()

    if not end_date:
        end_date = today.strftime("%Y-%m-%d")
    if not start_date:
        start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")

    start_ts = pd.Timestamp(str(start_date)).normalize()
    end_ts = pd.Timestamp(str(end_date)).normalize()

    if pd.isna(start_ts) or pd.isna(end_ts):
        raise ValueError("start_date / end_date 格式必須是 YYYY-MM-DD")
    if end_ts < start_ts:
        raise ValueError("end_date 不可早於 start_date")
    if (end_ts - start_ts).days > MAX_REQUEST_DAYS:
        raise ValueError("單次同步最多 30 天，較長區間請分批執行")

    return start_ts.strftime("%Y-%m-%d"), end_ts.strftime("%Y-%m-%d")


def _normalize_1m_frame(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    work = df.copy()
    work.index = pd.to_datetime(work.index, errors="coerce")
    work = work[~work.index.isna()].copy()

    try:
        if getattr(work.index, "tz", None) is not None:
            work.index = work.index.tz_convert(TAIPEI_TZ).tz_localize(None)
    except Exception:
        pass

    work.index = pd.DatetimeIndex(work.index).floor("min")

    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in work.columns:
            if col == "Volume":
                work[col] = 0.0
            else:
                return pd.DataFrame()
        work[col] = pd.to_numeric(work[col], errors="coerce")

    work = work.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if work.empty:
        return pd.DataFrame()

    aggregations: dict[str, str] = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    if "Amount" in work.columns:
        work["Amount"] = pd.to_numeric(work["Amount"], errors="coerce").fillna(0.0)
        aggregations["Amount"] = "sum"

    work = work.groupby(level=0).agg(aggregations).sort_index()
    work = work.between_time(SESSION_START, SESSION_END, inclusive="both")

    rename = {
        "Open": f"{prefix}_open",
        "High": f"{prefix}_high",
        "Low": f"{prefix}_low",
        "Close": f"{prefix}_close",
        "Volume": f"{prefix}_volume",
        "Amount": f"{prefix}_amount",
    }
    return work.rename(columns=rename)


def _add_features(aligned: pd.DataFrame) -> pd.DataFrame:
    result = aligned.copy()
    day_key = result.index.normalize()

    result["basis"] = result["txf_close"] - result["taiex_close"]
    result["basis_pct"] = result["basis"] / result["taiex_close"] * 100.0

    for minutes in (1, 3, 5, 10, 15, 30):
        delta = pd.Timedelta(minutes=minutes)

        taiex_past = result["taiex_close"].copy()
        taiex_past.index = taiex_past.index + delta
        taiex_past = taiex_past.reindex(result.index)

        txf_past = result["txf_close"].copy()
        txf_past.index = txf_past.index + delta
        txf_past = txf_past.reindex(result.index)

        result[f"taiex_return_{minutes}m"] = (
            result["taiex_close"] / taiex_past - 1.0
        ) * 100.0
        result[f"txf_return_{minutes}m"] = (
            result["txf_close"] / txf_past - 1.0
        ) * 100.0

    basis_past_5m = result["basis"].copy()
    basis_past_5m.index = basis_past_5m.index + pd.Timedelta(minutes=5)
    result["basis_change_5m"] = result["basis"] - basis_past_5m.reindex(result.index)

    result["taiex_volatility_15m"] = result.groupby(day_key, sort=False)[
        "taiex_return_1m"
    ].transform(lambda values: values.rolling(15, min_periods=10).std(ddof=0))
    result["txf_volatility_15m"] = result.groupby(day_key, sort=False)[
        "txf_return_1m"
    ].transform(lambda values: values.rolling(15, min_periods=10).std(ddof=0))

    txf_volume_mean = result.groupby(day_key, sort=False)["txf_volume"].transform(
        lambda values: values.rolling(30, min_periods=10).mean()
    )
    result["txf_volume_ratio_30m"] = (
        result["txf_volume"] / txf_volume_mean.replace(0.0, np.nan)
    )

    day_group = result.groupby(day_key, sort=False)
    result["taiex_day_high_so_far"] = day_group["taiex_high"].cummax()
    result["taiex_day_low_so_far"] = day_group["taiex_low"].cummin()
    day_range = (
        result["taiex_day_high_so_far"] - result["taiex_day_low_so_far"]
    ).replace(0.0, np.nan)
    result["taiex_day_position"] = (
        (result["taiex_close"] - result["taiex_day_low_so_far"]) / day_range
    )

    result["minute_of_day"] = result.index.hour * 60 + result.index.minute
    result["weekday"] = result.index.dayofweek
    return result


def _add_15m_target(
    frame: pd.DataFrame,
    neutral_threshold_pct: float,
) -> pd.DataFrame:
    result = frame.copy()

    future = result[["taiex_close"]].rename(
        columns={"taiex_close": "target_close_15m"}
    )
    future.index = future.index - pd.Timedelta(minutes=15)
    result = result.join(future, how="left")

    same_trade_date = (
        result.index.normalize()
        == (result.index + pd.Timedelta(minutes=15)).normalize()
    )
    valid_base_time = result.index.strftime("%H:%M") <= LAST_LABEL_TIME
    valid_target = (
        same_trade_date
        & valid_base_time
        & result["target_close_15m"].notna()
    )

    result["target_return_15m"] = np.where(
        valid_target,
        (result["target_close_15m"] / result["taiex_close"] - 1.0) * 100.0,
        np.nan,
    )
    result["target_direction"] = np.select(
        [
            result["target_return_15m"] > neutral_threshold_pct,
            result["target_return_15m"] < -neutral_threshold_pct,
        ],
        [1, -1],
        default=0,
    )
    result.loc[~valid_target, "target_direction"] = np.nan
    return result


def build_market_prediction_frame(
    taiex_df: pd.DataFrame,
    txf_df: pd.DataFrame,
    neutral_threshold_pct: float | None = None,
) -> pd.DataFrame:
    """對齊 TAIEX/TXF 1 分資料並建立可回測的 15 分鐘標籤。"""
    taiex_contract_code = str(getattr(taiex_df, "attrs", {}).get("contract_code") or "IX0001")
    txf_contract_code = str(getattr(txf_df, "attrs", {}).get("contract_code") or "TXFR1")

    taiex = _normalize_1m_frame(taiex_df, "taiex")
    txf = _normalize_1m_frame(txf_df, "txf")

    if taiex.empty or txf.empty:
        return pd.DataFrame()

    aligned = taiex.join(txf, how="inner")
    aligned = aligned[~aligned.index.duplicated(keep="last")].sort_index()
    if aligned.empty:
        return pd.DataFrame()

    threshold = neutral_threshold_pct
    if threshold is None:
        threshold = _safe_float(
            os.getenv("MARKET_PREDICTION_NEUTRAL_PCT", "0.08"),
            0.08,
        )
    threshold = max(float(threshold), 0.0)

    result = _add_features(aligned)
    result = _add_15m_target(result, threshold)
    result.attrs["neutral_threshold_pct"] = threshold
    result.attrs["version"] = MARKET_PREDICTION_DATA_VERSION
    result.attrs["taiex_contract_code"] = taiex_contract_code
    result.attrs["txf_contract_code"] = txf_contract_code
    return result


def _quality_report(
    taiex_df: pd.DataFrame,
    txf_df: pd.DataFrame,
    frame: pd.DataFrame,
) -> dict[str, Any]:
    taiex = _normalize_1m_frame(taiex_df, "taiex")
    txf = _normalize_1m_frame(txf_df, "txf")

    union_rows = len(taiex.index.union(txf.index)) if not taiex.empty or not txf.empty else 0
    aligned_rows = len(frame)
    missing_ratio = (
        1.0 - aligned_rows / union_rows
        if union_rows > 0
        else 1.0
    )

    labeled = frame["target_return_15m"].notna() if not frame.empty else pd.Series(dtype=bool)
    trade_days = int(frame.index.normalize().nunique()) if not frame.empty else 0
    direction_counts = {"up": 0, "flat": 0, "down": 0}
    feature_ready_rows = 0

    if not frame.empty:
        directions = pd.to_numeric(frame.loc[labeled, "target_direction"], errors="coerce")
        direction_counts = {
            "up": int((directions == 1).sum()),
            "flat": int((directions == 0).sum()),
            "down": int((directions == -1).sum()),
        }
        required_features = [
            "taiex_return_1m",
            "taiex_return_5m",
            "taiex_return_15m",
            "taiex_return_30m",
            "txf_return_1m",
            "txf_return_5m",
            "txf_return_15m",
            "txf_return_30m",
            "basis_pct",
            "taiex_volatility_15m",
            "txf_volume_ratio_30m",
        ]
        feature_ready_rows = int(frame[required_features].notna().all(axis=1).sum())

    return {
        "taiex_rows": int(len(taiex)),
        "txf_rows": int(len(txf)),
        "aligned_rows": int(aligned_rows),
        "labeled_rows": int(labeled.sum()) if len(labeled) else 0,
        "trade_days": trade_days,
        "feature_ready_rows": feature_ready_rows,
        "direction_counts": direction_counts,
        "alignment_missing_ratio": round(float(missing_ratio), 6),
        "duplicate_timestamps": int(frame.index.duplicated().sum()) if not frame.empty else 0,
        "first_timestamp": str(frame.index.min()) if not frame.empty else "",
        "last_timestamp": str(frame.index.max()) if not frame.empty else "",
    }


def _records_for_supabase(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []

    records: list[dict[str, Any]] = []
    persisted_columns = [
        "taiex_open", "taiex_high", "taiex_low", "taiex_close", "taiex_volume",
        "txf_open", "txf_high", "txf_low", "txf_close", "txf_volume",
        "basis", "basis_pct",
        "taiex_return_1m", "taiex_return_3m", "taiex_return_5m",
        "taiex_return_10m", "taiex_return_15m", "taiex_return_30m",
        "txf_return_1m", "txf_return_3m", "txf_return_5m",
        "txf_return_10m", "txf_return_15m", "txf_return_30m",
        "basis_change_5m", "taiex_volatility_15m", "txf_volatility_15m",
        "txf_volume_ratio_30m", "taiex_day_high_so_far",
        "taiex_day_low_so_far", "taiex_day_position",
        "target_close_15m", "target_return_15m", "target_direction",
        "minute_of_day", "weekday",
    ]

    for ts, row in frame.iterrows():
        local_ts = pd.Timestamp(ts)
        if local_ts.tzinfo is None:
            local_ts = local_ts.tz_localize(TAIPEI_TZ)
        else:
            local_ts = local_ts.tz_convert(TAIPEI_TZ)

        item: dict[str, Any] = {
            "ts": local_ts.isoformat(),
            "trade_date": local_ts.strftime("%Y-%m-%d"),
            "taiex_contract_code": str(frame.attrs.get("taiex_contract_code") or "IX0001"),
            "txf_contract_code": str(frame.attrs.get("txf_contract_code") or "TXFR1"),
            "source": "Shioaji_IX0001_TXFR1",
            "dataset_version": MARKET_PREDICTION_DATA_VERSION,
            "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }

        for col in persisted_columns:
            if col not in row.index:
                continue
            value = row[col]
            if pd.isna(value):
                item[col] = None
            elif col in {"target_direction", "minute_of_day", "weekday"}:
                item[col] = int(value)
            else:
                item[col] = float(value)

        records.append(item)

    return records


def sync_market_prediction_data(
    start_date: str | None = None,
    end_date: str | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    """抓取、對齊並選擇性寫入大盤15分鐘模型資料。"""
    started = time.perf_counter()

    try:
        start, end = _validate_date_range(start_date, end_date)
    except Exception as exc:
        return {"ok": False, "message": str(exc), "version": MARKET_PREDICTION_DATA_VERSION}

    taiex_df = get_market_index_1m_history(start, end)
    txf_df = get_market_future_1m_history(start, end)

    if taiex_df is None or taiex_df.empty:
        return {
            "ok": False,
            "message": "Shioaji 未回傳加權指數 IX0001 分鐘資料",
            "start_date": start,
            "end_date": end,
            "version": MARKET_PREDICTION_DATA_VERSION,
        }

    if txf_df is None or txf_df.empty:
        return {
            "ok": False,
            "message": "Shioaji 未回傳台指期 TXFR1 分鐘資料",
            "start_date": start,
            "end_date": end,
            "version": MARKET_PREDICTION_DATA_VERSION,
        }

    frame = build_market_prediction_frame(taiex_df, txf_df)
    quality = _quality_report(taiex_df, txf_df, frame)

    persist_result: dict[str, Any] = {
        "requested": bool(persist),
        "success": False,
        "rows": 0,
    }

    if persist and not frame.empty:
        records = _records_for_supabase(frame)
        persist_result = upsert_market_prediction_rows(records)
        persist_result["requested"] = True

    result = {
        "ok": bool(not frame.empty),
        "message": "ok" if not frame.empty else "TAIEX 與 TXF 沒有可對齊的分鐘資料",
        "version": MARKET_PREDICTION_DATA_VERSION,
        "start_date": start,
        "end_date": end,
        "quality": quality,
        "neutral_threshold_pct": frame.attrs.get("neutral_threshold_pct") if not frame.empty else None,
        "persist": persist_result,
        "seconds": round(time.perf_counter() - started, 3),
    }

    _debug(
        "sync",
        "| start =", start,
        "| end =", end,
        "| aligned =", quality.get("aligned_rows"),
        "| labeled =", quality.get("labeled_rows"),
        "| days =", quality.get("trade_days"),
        "| persist =", persist_result.get("success"),
        "| sec =", result["seconds"],
    )
    return result
