from __future__ import annotations

import math
import time
from datetime import datetime, time as clock_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


SHADOW_SERVICE_VERSION = "2026-07-27-v1.1-COMPLETED-MINUTE-GUARD"
ARTIFACT_KEY = "taiex_15m_selective_v7_fixed_100pt"
TAIPEI_TZ = "Asia/Taipei"
NEUTRAL_THRESHOLD_POINTS = 100.0
MAX_STALE_MINUTES = 5.0
MODEL_CACHE_SECONDS = 1800
SESSION_FETCH_START = clock_time(9, 0)
SESSION_FETCH_END = clock_time(13, 32)
PREDICTION_START = clock_time(9, 15)
PREDICTION_END = clock_time(13, 15)

FEATURE_COLUMNS = [
    "taiex_return_1m",
    "taiex_return_3m",
    "taiex_return_5m",
    "taiex_return_10m",
    "taiex_return_15m",
    "txf_return_1m",
    "txf_return_3m",
    "txf_return_5m",
    "txf_return_10m",
    "txf_return_15m",
    "basis_pct",
    "basis_change_5m",
    "taiex_volatility_15m",
    "txf_volatility_15m",
    "txf_volume_ratio_15m",
    "minute_sin",
    "minute_cos",
]

_ARTIFACT_CACHE: dict[str, Any] = {
    "loaded_at": 0.0,
    "artifact": None,
}


def _debug(*args: Any) -> None:
    print("DEBUG market_prediction_shadow |", *args, flush=True)


def _now_taipei() -> datetime:
    return datetime.now(ZoneInfo(TAIPEI_TZ))


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if np.isfinite(number) else default
    except Exception:
        return default


def _json_number_list(values: Any) -> list[Any]:
    array = np.asarray(values)
    if array.ndim == 0:
        return [float(array)]
    return array.tolist()


def _export_pipeline(model: Any) -> dict[str, Any]:
    scaler = model.named_steps["scale"]
    estimator = model.named_steps["model"]
    return {
        "classes": [int(value) for value in estimator.classes_.tolist()],
        "coef": _json_number_list(estimator.coef_),
        "intercept": _json_number_list(estimator.intercept_),
        "scaler_mean": _json_number_list(scaler.mean_),
        "scaler_scale": _json_number_list(scaler.scale_),
        "c": float(estimator.C),
    }


def _manual_probability(
    state: dict[str, Any],
    feature_values: np.ndarray,
) -> dict[int, float]:
    classes = [int(value) for value in (state.get("classes") or [])]
    coef = np.asarray(state.get("coef") or [], dtype=float)
    intercept = np.asarray(state.get("intercept") or [], dtype=float)
    mean = np.asarray(state.get("scaler_mean") or [], dtype=float)
    scale = np.asarray(state.get("scaler_scale") or [], dtype=float)

    if (
        not classes
        or coef.size == 0
        or mean.size != feature_values.size
        or scale.size != feature_values.size
    ):
        raise ValueError("模型成品格式不完整")

    safe_scale = np.where(scale == 0.0, 1.0, scale)
    standardized = (feature_values - mean) / safe_scale
    scores = np.matmul(coef, standardized) + intercept

    if len(classes) == 2 and coef.shape[0] == 1:
        score = float(np.clip(scores[0], -50.0, 50.0))
        positive = 1.0 / (1.0 + math.exp(-score))
        return {
            classes[0]: float(1.0 - positive),
            classes[1]: float(positive),
        }

    stable = scores - np.max(scores)
    exponential = np.exp(stable)
    probability = exponential / exponential.sum()
    return {
        label: float(probability[index])
        for index, label in enumerate(classes)
    }


def _load_artifact(force: bool = False) -> dict[str, Any] | None:
    now = time.time()
    cached = _ARTIFACT_CACHE.get("artifact")
    loaded_at = float(_ARTIFACT_CACHE.get("loaded_at") or 0.0)
    if cached and not force and now - loaded_at <= MODEL_CACHE_SECONDS:
        return cached

    from services.supabase_service import (
        get_latest_market_prediction_model_artifact,
    )

    artifact = get_latest_market_prediction_model_artifact(ARTIFACT_KEY)
    if artifact:
        _ARTIFACT_CACHE["artifact"] = artifact
        _ARTIFACT_CACHE["loaded_at"] = now
        return artifact
    return None


def prepare_shadow_model(
    training_start_date: str = "2026-04-14",
    training_cutoff: str = "2026-07-13",
    persist: bool = True,
) -> dict[str, Any]:
    """離線建立一次模型成品；LINE 查詢不會呼叫此函式。"""
    started = time.perf_counter()
    try:
        from services.market_prediction_repository_v7 import (
            REPOSITORY_VERSION,
            load_market_prediction_rows_paginated,
        )
        from services.market_prediction_selective_service_v7 import (
            MODEL_VERSION,
            _date_split,
            _new_pipeline,
            _prepare_training_frame,
            _select_binary_c,
            _threshold_candidates,
        )

        rows, repository_status = load_market_prediction_rows_paginated(
            str(training_start_date),
            str(training_cutoff),
            limit=50000,
        )
        if not repository_status.get("complete"):
            return {
                "ok": False,
                "message": "訓練資料讀取不完整",
                "repository_status": repository_status,
                "version": SHADOW_SERVICE_VERSION,
            }

        frame = _prepare_training_frame(rows)
        if frame.empty:
            return {
                "ok": False,
                "message": "沒有可用的模型訓練資料",
                "database_rows": len(rows),
                "version": SHADOW_SERVICE_VERSION,
            }

        split = _date_split(frame)
        train = split["train"]
        validation = split["validation"]
        train_direction = train[train["target_event"] == 1].copy()
        validation_direction = validation[
            validation["target_event"] == 1
        ].copy()
        if min(len(train_direction), len(validation_direction)) < 100:
            return {
                "ok": False,
                "message": "突破100點的方向樣本不足",
                "version": SHADOW_SERVICE_VERSION,
            }

        event_c, event_candidates = _select_binary_c(
            train,
            validation,
            "target_event",
        )
        direction_c, direction_candidates = _select_binary_c(
            train_direction,
            validation_direction,
            "target_direction",
        )

        tuning_event = _new_pipeline(event_c)
        tuning_event.fit(
            train[FEATURE_COLUMNS],
            train["target_event"].astype(int),
        )
        tuning_direction = _new_pipeline(direction_c)
        tuning_direction.fit(
            train_direction[FEATURE_COLUMNS],
            train_direction["target_direction"].astype(int),
        )
        thresholds, threshold_candidates = _threshold_candidates(
            tuning_event,
            tuning_direction,
            validation,
        )

        final_event = _new_pipeline(event_c)
        final_event.fit(
            frame[FEATURE_COLUMNS],
            frame["target_event"].astype(int),
        )
        all_direction = frame[frame["target_event"] == 1].copy()
        final_direction = _new_pipeline(direction_c)
        final_direction.fit(
            all_direction[FEATURE_COLUMNS],
            all_direction["target_direction"].astype(int),
        )

        artifact = {
            "artifact_key": ARTIFACT_KEY,
            "model_version": str(MODEL_VERSION),
            "service_version": SHADOW_SERVICE_VERSION,
            "repository_version": str(REPOSITORY_VERSION),
            "training_start_date": str(training_start_date),
            "training_cutoff": str(training_cutoff),
            "feature_columns": FEATURE_COLUMNS,
            "event_model": _export_pipeline(final_event),
            "direction_model": _export_pipeline(final_direction),
            "thresholds": {
                "event_probability_threshold": float(
                    thresholds["event_probability_threshold"]
                ),
                "direction_confidence_threshold": float(
                    thresholds["direction_confidence_threshold"]
                ),
                "validation_directional_precision": _finite_float(
                    thresholds.get("directional_precision"),
                    0.0,
                ),
                "validation_signal_coverage": _finite_float(
                    thresholds.get("signal_coverage"),
                    0.0,
                ),
            },
            "training_rows": int(len(frame)),
            "trade_days": int(frame["trade_date"].nunique()),
            "release_status": "shadow_only",
            "metadata": {
                "neutral_threshold_points": 100,
                "feature_window_minutes": 15,
                "prediction_horizon_minutes": 15,
                "event_model_candidates": event_candidates,
                "direction_model_candidates": direction_candidates,
                "top_threshold_candidates": threshold_candidates,
            },
        }

        persist_result = {
            "requested": bool(persist),
            "success": False,
            "rows": 0,
        }
        if persist:
            from services.supabase_service import (
                upsert_market_prediction_model_artifact,
            )

            persist_result = {
                "requested": True,
                **upsert_market_prediction_model_artifact(artifact),
            }
            if persist_result.get("success"):
                _ARTIFACT_CACHE["artifact"] = artifact
                _ARTIFACT_CACHE["loaded_at"] = time.time()

        return {
            "ok": True,
            "message": "ok",
            "version": SHADOW_SERVICE_VERSION,
            "artifact_key": ARTIFACT_KEY,
            "model_version": MODEL_VERSION,
            "training_start_date": str(training_start_date),
            "training_cutoff": str(training_cutoff),
            "database_rows": len(rows),
            "training_rows": len(frame),
            "trade_days": int(frame["trade_date"].nunique()),
            "features": FEATURE_COLUMNS,
            "selected_c": {
                "event": event_c,
                "direction": direction_c,
            },
            "thresholds": artifact["thresholds"],
            "release_status": "shadow_only",
            "persist": persist_result,
            "repository_status": repository_status,
            "seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        _debug("prepare failed", "| error =", repr(exc))
        return {
            "ok": False,
            "message": "影子模型成品建立失敗",
            "error": repr(exc),
            "version": SHADOW_SERVICE_VERSION,
            "seconds": round(time.perf_counter() - started, 3),
        }


def _build_live_feature_frame(
    taiex_df: pd.DataFrame,
    txf_df: pd.DataFrame,
) -> pd.DataFrame:
    from services.market_prediction_data_service_v2 import _normalize_1m_frame

    taiex = _normalize_1m_frame(taiex_df, "taiex")
    txf = _normalize_1m_frame(txf_df, "txf")
    if taiex.empty or txf.empty:
        return pd.DataFrame()

    frame = taiex.join(txf, how="inner")
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    if frame.empty:
        return frame

    grouped = frame.groupby(frame.index.normalize(), sort=False)
    for minutes in (1, 3, 5, 10, 15):
        frame[f"taiex_return_{minutes}m"] = grouped[
            "taiex_close"
        ].pct_change(periods=minutes, fill_method=None) * 100.0
        frame[f"txf_return_{minutes}m"] = grouped[
            "txf_close"
        ].pct_change(periods=minutes, fill_method=None) * 100.0

    frame["basis"] = frame["txf_close"] - frame["taiex_close"]
    frame["basis_pct"] = frame["basis"] / frame["taiex_close"] * 100.0
    frame["basis_change_5m"] = grouped["basis"].diff(5)
    frame["taiex_volatility_15m"] = grouped[
        "taiex_return_1m"
    ].transform(lambda values: values.rolling(15, min_periods=10).std(ddof=0))
    frame["txf_volatility_15m"] = grouped[
        "txf_return_1m"
    ].transform(lambda values: values.rolling(15, min_periods=10).std(ddof=0))
    volume_mean = grouped["txf_volume"].transform(
        lambda values: values.rolling(15, min_periods=10).mean()
    )
    frame["txf_volume_ratio_15m"] = (
        frame["txf_volume"] / volume_mean.replace(0.0, np.nan)
    )

    session_minute = (frame.index.hour * 60 + frame.index.minute) - 540
    angle = 2.0 * math.pi * session_minute / 270.0
    frame["minute_sin"] = np.sin(angle)
    frame["minute_cos"] = np.cos(angle)
    return frame.replace([np.inf, -np.inf], np.nan)


def _completed_minute_cutoff(now: datetime) -> pd.Timestamp:
    """回傳目前已結束之分鐘 K 的最晚時間標籤（台北時間、無時區）。

    Shioaji 盤中 Kbars 使用分鐘結束時間標示，例如 11:05:33 可能已出現
    11:06 這根正在形成中的 K。此時 11:05 才是可安全採用的最後標籤。
    """
    local_now = pd.Timestamp(now)
    if local_now.tzinfo is not None:
        local_now = local_now.tz_convert(TAIPEI_TZ).tz_localize(None)
    return local_now.floor("min")


def _timestamp_text(value: Any) -> str:
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return ""
        return stamp.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _filter_completed_minute_rows(
    frame: pd.DataFrame,
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """排除截止分鐘之後的未完成或時間超前 K 棒，並保留 DataFrame attrs。"""
    if frame is None or frame.empty:
        return pd.DataFrame(), {
            "raw_rows": 0,
            "used_rows": 0,
            "dropped_incomplete_rows": 0,
            "raw_last": "",
            "used_last": "",
        }

    attrs = dict(getattr(frame, "attrs", {}) or {})
    work = frame.copy()
    parsed_index = pd.to_datetime(work.index, errors="coerce")
    valid_mask = ~pd.isna(parsed_index)
    work = work.loc[valid_mask].copy()
    parsed_index = pd.DatetimeIndex(parsed_index[valid_mask])

    if parsed_index.tz is not None:
        parsed_index = parsed_index.tz_convert(TAIPEI_TZ).tz_localize(None)
    work.index = parsed_index.floor("min")
    work = work.sort_index()

    raw_rows = int(len(work))
    raw_last = work.index[-1] if raw_rows else None
    work = work.loc[work.index <= cutoff].copy()
    work.attrs.update(attrs)
    used_rows = int(len(work))
    used_last = work.index[-1] if used_rows else None
    return work, {
        "raw_rows": raw_rows,
        "used_rows": used_rows,
        "dropped_incomplete_rows": max(0, raw_rows - used_rows),
        "raw_last": _timestamp_text(raw_last),
        "used_last": _timestamp_text(used_last),
    }


def _fetch_live_frame(now: datetime) -> tuple[pd.DataFrame, dict[str, Any]]:
    from services.market_future_kline_service import (
        get_market_future_1m_history,
    )
    from services.market_index_service import get_market_index_1m_history

    trade_date = now.strftime("%Y-%m-%d")
    taiex_df = get_market_index_1m_history(trade_date, trade_date)
    txf_df = get_market_future_1m_history(trade_date, trade_date)
    cutoff = _completed_minute_cutoff(now)
    taiex_df, taiex_guard = _filter_completed_minute_rows(
        taiex_df,
        cutoff,
    )
    txf_df, txf_guard = _filter_completed_minute_rows(
        txf_df,
        cutoff,
    )
    frame = _build_live_feature_frame(taiex_df, txf_df)

    source_status = {
        "taiex_rows": int(len(taiex_df)) if taiex_df is not None else 0,
        "txf_rows": int(len(txf_df)) if txf_df is not None else 0,
        "aligned_rows": int(len(frame)),
        "taiex_source": str(
            getattr(taiex_df, "attrs", {}).get("source") or "unknown"
        ),
        "txf_source": str(
            getattr(txf_df, "attrs", {}).get("source") or "unknown"
        ),
        "server_time": now.isoformat(timespec="seconds"),
        "completed_minute_cutoff": _timestamp_text(cutoff),
        "taiex_raw_rows": taiex_guard["raw_rows"],
        "taiex_raw_last": taiex_guard["raw_last"],
        "taiex_used_last": taiex_guard["used_last"],
        "taiex_dropped_incomplete_rows": taiex_guard[
            "dropped_incomplete_rows"
        ],
        "txf_raw_rows": txf_guard["raw_rows"],
        "txf_raw_last": txf_guard["raw_last"],
        "txf_used_last": txf_guard["used_last"],
        "txf_dropped_incomplete_rows": txf_guard[
            "dropped_incomplete_rows"
        ],
    }
    _debug(
        "completed minute guard",
        "| server_now =", source_status["server_time"],
        "| cutoff =", source_status["completed_minute_cutoff"],
        "| taiex_raw_last =", source_status["taiex_raw_last"],
        "| taiex_used_last =", source_status["taiex_used_last"],
        "| taiex_dropped =",
        source_status["taiex_dropped_incomplete_rows"],
        "| txf_raw_last =", source_status["txf_raw_last"],
        "| txf_used_last =", source_status["txf_used_last"],
        "| txf_dropped =",
        source_status["txf_dropped_incomplete_rows"],
    )
    return frame, source_status


def _direction_from_points(change_points: float) -> str:
    if change_points > NEUTRAL_THRESHOLD_POINTS:
        return "up"
    if change_points < -NEUTRAL_THRESHOLD_POINTS:
        return "down"
    return "flat"


def _settle_pending_predictions(
    live_frame: pd.DataFrame,
    latest_ts: pd.Timestamp,
) -> dict[str, Any]:
    from services.supabase_service import (
        get_unsettled_market_prediction_shadow_predictions,
        update_market_prediction_shadow_result,
    )

    latest_local = latest_ts.tz_localize(TAIPEI_TZ)
    pending = get_unsettled_market_prediction_shadow_predictions(
        trade_date=latest_ts.strftime("%Y-%m-%d"),
        horizon_before=latest_local.isoformat(),
        limit=100,
    )
    settled = 0
    for row in pending:
        horizon = pd.to_datetime(row.get("horizon_ts"), errors="coerce", utc=True)
        if pd.isna(horizon):
            continue
        horizon_local = horizon.tz_convert(TAIPEI_TZ).tz_localize(None)
        if horizon_local > latest_ts:
            continue

        position = live_frame.index.get_indexer(
            [horizon_local],
            method="nearest",
            tolerance=pd.Timedelta(minutes=2),
        )[0]
        if position < 0:
            continue
        actual_close = _finite_float(
            live_frame.iloc[position].get("taiex_close")
        )
        base_close = _finite_float(row.get("base_taiex_close"))
        if actual_close is None or base_close is None:
            continue

        change_points = actual_close - base_close
        actual_direction = _direction_from_points(change_points)
        signal = str(row.get("signal") or "observe")
        is_correct = (
            signal == actual_direction
            if signal in {"up", "down"}
            else None
        )
        success = update_market_prediction_shadow_result(
            str(row.get("prediction_ts") or ""),
            {
                "actual_close": round(actual_close, 4),
                "actual_change_points": round(change_points, 4),
                "actual_direction": actual_direction,
                "is_correct": is_correct,
                "status": "settled",
                "settled_at": latest_local.isoformat(),
            },
        )
        settled += int(bool(success))
    return {
        "pending_rows": len(pending),
        "settled_rows": settled,
    }


def _stored_result(now: datetime) -> dict[str, Any]:
    from services.supabase_service import (
        get_latest_market_prediction_shadow_prediction,
    )

    row = get_latest_market_prediction_shadow_prediction()
    if not row:
        return {
            "ok": False,
            "message": "目前沒有已保存的影子預測",
            "version": SHADOW_SERVICE_VERSION,
            "session_status": "closed",
        }

    prediction_ts = pd.to_datetime(
        row.get("prediction_ts"),
        errors="coerce",
        utc=True,
    )
    display_time = ""
    age_minutes = None
    if not pd.isna(prediction_ts):
        local_ts = prediction_ts.tz_convert(TAIPEI_TZ)
        display_time = local_ts.strftime("%Y-%m-%d %H:%M")
        age_minutes = max(
            0.0,
            (now - local_ts.to_pydatetime()).total_seconds() / 60.0,
        )

    return {
        "ok": True,
        "message": "目前非盤中，顯示最近一次影子預測",
        "version": SHADOW_SERVICE_VERSION,
        "source": "stored_shadow_prediction",
        "session_status": "closed",
        "is_live": False,
        "signal": str(row.get("signal") or "observe"),
        "event_probability": _finite_float(row.get("event_probability"), 0.0),
        "up_probability": _finite_float(row.get("up_probability"), 0.0),
        "direction_confidence": _finite_float(
            row.get("direction_confidence"),
            0.0,
        ),
        "taiex_close": _finite_float(row.get("base_taiex_close")),
        "prediction_ts": str(row.get("prediction_ts") or ""),
        "display_time": display_time,
        "age_minutes": round(age_minutes, 2) if age_minutes is not None else None,
        "freshness_status": "收盤後",
        "thresholds": {
            "event_probability_threshold": _finite_float(
                row.get("event_probability_threshold"),
                0.0,
            ),
            "direction_confidence_threshold": _finite_float(
                row.get("direction_confidence_threshold"),
                0.0,
            ),
        },
        "label_definition": {
            "up": "> +100點",
            "flat": "-100至+100點",
            "down": "< -100點",
        },
        "release_status": "shadow_only",
        "model_note": "模型測試中，非交易建議",
    }


def predict_market_shadow(
    persist: bool = False,
    force_live: bool = False,
) -> dict[str, Any]:
    """盤中快速推論；persist=True 僅供排程保存與15分鐘後驗證。"""
    started = time.perf_counter()
    now = _now_taipei()
    in_fetch_session = (
        now.weekday() < 5
        and SESSION_FETCH_START <= now.time() <= SESSION_FETCH_END
    )
    if not in_fetch_session and not force_live:
        result = _stored_result(now)
        result["seconds"] = round(time.perf_counter() - started, 3)
        return result

    artifact = _load_artifact()
    if not artifact:
        return {
            "ok": False,
            "message": "尚未建立影子模型成品，請先執行模型準備端點",
            "version": SHADOW_SERVICE_VERSION,
            "artifact_key": ARTIFACT_KEY,
            "seconds": round(time.perf_counter() - started, 3),
        }

    try:
        live_frame, source_status = _fetch_live_frame(now)
        ready = live_frame.dropna(subset=FEATURE_COLUMNS).copy()
        if ready.empty:
            return {
                "ok": False,
                "message": "上市或台指期分鐘資料不足，暫不預測",
                "version": SHADOW_SERVICE_VERSION,
                "source_status": source_status,
                "seconds": round(time.perf_counter() - started, 3),
            }

        latest_ts = pd.Timestamp(ready.index[-1])
        latest = ready.iloc[-1]
        latest_local = latest_ts.tz_localize(TAIPEI_TZ)
        age_minutes = max(
            0.0,
            (now - latest_local.to_pydatetime()).total_seconds() / 60.0,
        )
        freshness_status = (
            "即時"
            if age_minutes <= 2.0
            else "稍有延遲"
            if age_minutes <= MAX_STALE_MINUTES
            else "延遲行情"
        )
        if age_minutes > MAX_STALE_MINUTES and not force_live:
            stored = _stored_result(now)
            stored.update({
                "message": "即時來源已超過5分鐘，顯示最近一次已保存預測",
                "freshness_status": freshness_status,
                "live_source_age_minutes": round(age_minutes, 2),
                "source_status": source_status,
                "seconds": round(time.perf_counter() - started, 3),
            })
            return stored

        feature_values = latest[FEATURE_COLUMNS].to_numpy(dtype=float)
        event_probability = _manual_probability(
            artifact.get("event_model") or {},
            feature_values,
        ).get(1, 0.0)
        up_probability = _manual_probability(
            artifact.get("direction_model") or {},
            feature_values,
        ).get(1, 0.0)
        direction_confidence = max(up_probability, 1.0 - up_probability)
        thresholds = artifact.get("thresholds") or {}
        event_threshold = float(
            thresholds.get("event_probability_threshold") or 0.45
        )
        direction_threshold = float(
            thresholds.get("direction_confidence_threshold") or 0.60
        )
        has_signal = (
            event_probability >= event_threshold
            and direction_confidence >= direction_threshold
        )
        signal = (
            "up"
            if has_signal and up_probability >= 0.5
            else "down"
            if has_signal
            else "observe"
        )

        prediction_allowed = (
            PREDICTION_START <= latest_ts.time() <= PREDICTION_END
        )
        horizon_ts = latest_local + timedelta(minutes=15)
        prediction_row = {
            "prediction_ts": latest_local.isoformat(),
            "horizon_ts": horizon_ts.isoformat(),
            "trade_date": latest_ts.strftime("%Y-%m-%d"),
            "base_taiex_close": round(float(latest["taiex_close"]), 4),
            "signal": signal,
            "event_probability": round(float(event_probability), 8),
            "up_probability": round(float(up_probability), 8),
            "direction_confidence": round(float(direction_confidence), 8),
            "event_probability_threshold": round(event_threshold, 6),
            "direction_confidence_threshold": round(direction_threshold, 6),
            "model_version": str(artifact.get("model_version") or ""),
            "artifact_key": str(artifact.get("artifact_key") or ARTIFACT_KEY),
            "source": (
                f"{source_status.get('taiex_source')}+"
                f"{source_status.get('txf_source')}"
            ),
            "status": "pending",
        }

        settle_result = {
            "requested": bool(persist),
            "pending_rows": 0,
            "settled_rows": 0,
        }
        persist_result = {
            "requested": bool(persist),
            "success": False,
            "rows": 0,
            "message": (
                "LINE preview only"
                if not persist
                else "13:15後不建立新的15分鐘預測"
            ),
        }
        if persist:
            settle_result = {
                "requested": True,
                **_settle_pending_predictions(ready, latest_ts),
            }
            if prediction_allowed:
                from services.supabase_service import (
                    upsert_market_prediction_shadow_prediction,
                )

                persist_result = {
                    "requested": True,
                    **upsert_market_prediction_shadow_prediction(
                        prediction_row
                    ),
                }

        result = {
            "ok": True,
            "message": "ok",
            "version": SHADOW_SERVICE_VERSION,
            "source": "live_1m_features",
            "source_status": source_status,
            "is_live": True,
            "session_status": "in_session",
            "prediction_allowed": prediction_allowed,
            "signal": signal,
            "event_probability": round(float(event_probability), 6),
            "up_probability": round(float(up_probability), 6),
            "down_probability": round(float(1.0 - up_probability), 6),
            "direction_confidence": round(float(direction_confidence), 6),
            "taiex_close": round(float(latest["taiex_close"]), 2),
            "prediction_ts": latest_local.isoformat(),
            "horizon_ts": horizon_ts.isoformat(),
            "display_time": latest_ts.strftime("%Y-%m-%d %H:%M"),
            "age_minutes": round(age_minutes, 2),
            "freshness_status": freshness_status,
            "thresholds": {
                "event_probability_threshold": event_threshold,
                "direction_confidence_threshold": direction_threshold,
            },
            "label_definition": {
                "up": "> +100點",
                "flat": "-100至+100點",
                "down": "< -100點",
            },
            "artifact": {
                "artifact_key": artifact.get("artifact_key"),
                "model_version": artifact.get("model_version"),
                "training_cutoff": artifact.get("training_cutoff"),
                "release_status": artifact.get("release_status"),
            },
            "release_status": "shadow_only",
            "model_note": "模型測試中，非交易建議",
            "persist": persist_result,
            "settlement": settle_result,
            "seconds": round(time.perf_counter() - started, 3),
        }
        _debug(
            "predict",
            "| ts =", result["prediction_ts"],
            "| signal =", signal,
            "| event =", result["event_probability"],
            "| up =", result["up_probability"],
            "| persisted =", persist_result.get("success"),
            "| settled =", settle_result.get("settled_rows"),
            "| sec =", result["seconds"],
        )
        return result
    except Exception as exc:
        _debug("predict failed", "| error =", repr(exc))
        return {
            "ok": False,
            "message": "大盤15分鐘影子預測失敗",
            "error": repr(exc),
            "version": SHADOW_SERVICE_VERSION,
            "seconds": round(time.perf_counter() - started, 3),
        }


def evaluate_shadow_history(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """僅評估已落地且已結算的影子紀錄，不重新訓練模型。"""
    started = time.perf_counter()
    today = _now_taipei().date()
    end_text = str(end_date or today.strftime("%Y-%m-%d"))
    start_text = str(
        start_date
        or (today - timedelta(days=45)).strftime("%Y-%m-%d")
    )
    try:
        from services.supabase_service import (
            get_market_prediction_shadow_history,
        )

        rows = get_market_prediction_shadow_history(
            start_text,
            end_text,
            limit=10000,
        )
        settled = [
            row
            for row in rows
            if str(row.get("status") or "") == "settled"
            and str(row.get("actual_direction") or "") in {
                "up",
                "down",
                "flat",
            }
        ]
        trade_days = sorted({
            str(row.get("trade_date") or "")
            for row in settled
            if row.get("trade_date")
        })
        signal_rows = [
            row
            for row in settled
            if str(row.get("signal") or "") in {"up", "down"}
        ]
        correct_rows = [
            row
            for row in signal_rows
            if bool(row.get("is_correct"))
        ]
        actual_move_signals = [
            row
            for row in signal_rows
            if str(row.get("actual_direction") or "") in {"up", "down"}
        ]

        def ratio(numerator: int, denominator: int) -> float | None:
            if denominator <= 0:
                return None
            return round(numerator / denominator, 6)

        by_day: list[dict[str, Any]] = []
        for trade_date in trade_days:
            day_rows = [
                row
                for row in settled
                if str(row.get("trade_date") or "") == trade_date
            ]
            day_signals = [
                row
                for row in day_rows
                if str(row.get("signal") or "") in {"up", "down"}
            ]
            day_correct = [
                row for row in day_signals if bool(row.get("is_correct"))
            ]
            by_day.append({
                "trade_date": trade_date,
                "rows": len(day_rows),
                "signal_rows": len(day_signals),
                "signal_coverage": ratio(len(day_signals), len(day_rows)),
                "directional_precision": ratio(
                    len(day_correct),
                    len(day_signals),
                ),
            })

        directional_precision = ratio(len(correct_rows), len(signal_rows))
        signal_coverage = ratio(len(signal_rows), len(settled))
        ready = bool(
            len(trade_days) >= 20
            and directional_precision is not None
            and directional_precision >= 0.50
            and signal_coverage is not None
            and signal_coverage >= 0.05
        )
        return {
            "ok": True,
            "message": "ok",
            "version": SHADOW_SERVICE_VERSION,
            "start_date": start_text,
            "end_date": end_text,
            "database_rows": len(rows),
            "settled_rows": len(settled),
            "pending_rows": len(rows) - len(settled),
            "trade_days": len(trade_days),
            "signal_rows": len(signal_rows),
            "observe_rows": len(settled) - len(signal_rows),
            "correct_direction_rows": len(correct_rows),
            "directional_precision": directional_precision,
            "signal_coverage": signal_coverage,
            "move_detection_precision": ratio(
                len(actual_move_signals),
                len(signal_rows),
            ),
            "minimum_shadow_days": 20,
            "remaining_shadow_days": max(0, 20 - len(trade_days)),
            "ready_for_public_signal": ready,
            "readiness_note": (
                "達到20個交易日與最低精準度/覆蓋率門檻"
                if ready
                else "維持影子測試，不對外宣稱正式訊號"
            ),
            "by_day": by_day,
            "seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": "影子預測成效讀取失敗",
            "error": repr(exc),
            "version": SHADOW_SERVICE_VERSION,
            "seconds": round(time.perf_counter() - started, 3),
        }
