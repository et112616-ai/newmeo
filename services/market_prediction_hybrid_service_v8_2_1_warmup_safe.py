from __future__ import annotations

import math
import time
from datetime import datetime, time as clock_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    f1_score,
    log_loss,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from services.market_prediction_features_v8_1 import (
    BASE_FEATURE_COLUMNS,
    FEATURE_GROUPS,
    FEATURE_SERVICE_VERSION,
    prepare_v8_1_training_frame,
)
from services.market_prediction_features_v8_lite import (
    build_v8_feature_frame,
)
from services.market_prediction_repository_v8_1 import (
    load_market_prediction_rows_paginated,
)
from services.market_prediction_repository_v8_2 import (
    REPOSITORY_VERSION,
    get_latest_shadow_prediction,
    get_shadow_history,
    get_unsettled_shadow_predictions,
    update_shadow_result,
    upsert_shadow_prediction,
)


SERVICE_VERSION = "2026-07-28-v8.2.1-HYBRID-WARMUP-SAFE"
MODEL_VERSION = "2026-07-27-v8.2-ALL48-EVENT-BASE17-DIRECTION"
ARTIFACT_KEY = "taiex_15m_hybrid_v8_2_all48_event_base17_direction"
TAIPEI_TZ = "Asia/Taipei"
NEUTRAL_THRESHOLD_POINTS = 100.0
MAX_STALE_MINUTES = 5.0
MODEL_CACHE_SECONDS = 1800
MIN_TRAINING_ROWS = 8000
SESSION_FETCH_START = clock_time(9, 0)
SESSION_FETCH_END = clock_time(13, 32)
PREDICTION_START = clock_time(9, 15)
PREDICTION_END = clock_time(13, 15)
FEATURE_SELECTION_CUTOFF = "2026-07-23"
UNBIASED_FORWARD_START = "2026-07-27"

EVENT_FEATURE_COLUMNS = list(FEATURE_GROUPS["all"])
DIRECTION_FEATURE_COLUMNS = list(BASE_FEATURE_COLUMNS)

_ARTIFACT_CACHE: dict[str, Any] = {
    "loaded_at": 0.0,
    "artifact": None,
}


def _debug(*args: Any) -> None:
    print("DEBUG market_prediction_v8_2 |", *args, flush=True)


def _now_taipei() -> datetime:
    return datetime.now(ZoneInfo(TAIPEI_TZ))


def _finite_float(
    value: Any,
    default: float | None = None,
) -> float | None:
    try:
        number = float(value)
        return number if np.isfinite(number) else default
    except Exception:
        return default


def _safe_ratio(
    numerator: int | float,
    denominator: int | float,
) -> float | None:
    if denominator <= 0:
        return None
    return round(float(numerator / denominator), 6)


def _new_classifier(c_value: float) -> Pipeline:
    return Pipeline(
        steps=[
            ("scale", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=float(c_value),
                    class_weight="balanced",
                    solver="lbfgs",
                    max_iter=1500,
                    random_state=42,
                ),
            ),
        ]
    )


def _probability_for_label(
    model: Pipeline,
    frame: pd.DataFrame,
    columns: list[str],
    label: int,
) -> np.ndarray:
    classes = list(model.named_steps["model"].classes_.astype(int))
    position = classes.index(int(label))
    return model.predict_proba(frame[columns])[:, position]


def _binary_metrics(
    model: Pipeline,
    frame: pd.DataFrame,
    columns: list[str],
    target_column: str,
) -> dict[str, Any]:
    y_true = frame[target_column].astype(int).to_numpy()
    y_pred = model.predict(frame[columns]).astype(int)
    probability = model.predict_proba(frame[columns])
    labels = list(model.named_steps["model"].classes_.astype(int))
    return {
        "rows": int(len(frame)),
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 6),
        "balanced_accuracy": round(
            float(balanced_accuracy_score(y_true, y_pred)),
            6,
        ),
        "macro_f1": round(
            float(
                f1_score(
                    y_true,
                    y_pred,
                    average="macro",
                    zero_division=0,
                )
            ),
            6,
        ),
        "log_loss": round(
            float(log_loss(y_true, probability, labels=labels)),
            6,
        ),
    }


def _select_classifier_c(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    columns: list[str],
    target_column: str,
) -> tuple[float, list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    best_score: tuple[float, float, float] | None = None
    best_c = 0.05
    for c_value in (0.03, 0.05, 0.1, 0.3, 1.0):
        model = _new_classifier(c_value)
        model.fit(train[columns], train[target_column].astype(int))
        metrics = _binary_metrics(
            model,
            validation,
            columns,
            target_column,
        )
        candidates.append({"c": c_value, **metrics})
        score = (
            float(metrics["balanced_accuracy"]),
            float(metrics["macro_f1"]),
            -float(metrics["log_loss"]),
        )
        if best_score is None or score > best_score:
            best_score = score
            best_c = c_value
    return best_c, candidates


def _selective_predictions(
    event_probability: np.ndarray,
    up_probability: np.ndarray,
    event_threshold: float,
    direction_threshold: float,
) -> np.ndarray:
    confidence = np.maximum(up_probability, 1.0 - up_probability)
    has_signal = (
        (event_probability >= float(event_threshold))
        & (confidence >= float(direction_threshold))
    )
    return np.where(
        has_signal & (up_probability >= 0.5),
        1,
        np.where(has_signal, -1, 0),
    )


def _selective_metrics(
    frame: pd.DataFrame,
    prediction: np.ndarray,
) -> dict[str, Any]:
    y_true = frame["target_direction"].astype(int).to_numpy()
    prediction = np.asarray(prediction, dtype=int)
    signal = prediction != 0
    actual_move = y_true != 0
    correct_direction = signal & (prediction == y_true)
    up = prediction == 1
    down = prediction == -1
    return {
        "rows": int(len(frame)),
        "signal_rows": int(signal.sum()),
        "signal_coverage": _safe_ratio(int(signal.sum()), len(frame)),
        "directional_precision": _safe_ratio(
            int(correct_direction.sum()),
            int(signal.sum()),
        ),
        "move_detection_precision": _safe_ratio(
            int((signal & actual_move).sum()),
            int(signal.sum()),
        ),
        "move_detection_recall": _safe_ratio(
            int((signal & actual_move).sum()),
            int(actual_move.sum()),
        ),
        "up_signal_rows": int(up.sum()),
        "down_signal_rows": int(down.sum()),
        "up_precision": _safe_ratio(
            int((up & (y_true == 1)).sum()),
            int(up.sum()),
        ),
        "down_precision": _safe_ratio(
            int((down & (y_true == -1)).sum()),
            int(down.sum()),
        ),
        "balanced_accuracy": round(
            float(balanced_accuracy_score(y_true, prediction)),
            6,
        ),
        "macro_f1": round(
            float(
                f1_score(
                    y_true,
                    prediction,
                    labels=[-1, 0, 1],
                    average="macro",
                    zero_division=0,
                )
            ),
            6,
        ),
    }


def _select_thresholds(
    event_model: Pipeline,
    direction_model: Pipeline,
    validation: pd.DataFrame,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    event_probability = _probability_for_label(
        event_model,
        validation,
        EVENT_FEATURE_COLUMNS,
        1,
    )
    up_probability = _probability_for_label(
        direction_model,
        validation,
        DIRECTION_FEATURE_COLUMNS,
        1,
    )
    candidates: list[dict[str, Any]] = []
    for event_threshold in (0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        for direction_threshold in (0.50, 0.55, 0.60, 0.65, 0.70):
            prediction = _selective_predictions(
                event_probability,
                up_probability,
                event_threshold,
                direction_threshold,
            )
            candidates.append({
                "event_probability_threshold": event_threshold,
                "direction_confidence_threshold": direction_threshold,
                **_selective_metrics(validation, prediction),
            })

    eligible = [
        item
        for item in candidates
        if 0.10 <= float(item.get("signal_coverage") or 0.0) <= 0.40
        and int(item.get("up_signal_rows") or 0) >= 25
        and int(item.get("down_signal_rows") or 0) >= 25
    ]
    selected = max(
        eligible or candidates,
        key=lambda item: (
            float(item.get("directional_precision") or 0.0),
            float(item.get("macro_f1") or 0.0),
            float(item.get("move_detection_precision") or 0.0),
            -abs(float(item.get("signal_coverage") or 0.0) - 0.20),
        ),
    )
    ranked = sorted(
        candidates,
        key=lambda item: (
            float(item.get("directional_precision") or 0.0),
            float(item.get("macro_f1") or 0.0),
        ),
        reverse=True,
    )
    return selected, ranked[:10]


def _date_split(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    days = sorted(pd.DatetimeIndex(frame["trade_date"].unique()))
    if len(days) < 50:
        raise ValueError(f"完整交易日不足：目前 {len(days)} 天，至少需要50天")
    train_end = max(1, int(len(days) * 0.70))
    validation_end = max(train_end + 1, int(len(days) * 0.85))
    validation_end = min(validation_end, len(days) - 1)
    train_days = days[:train_end]
    validation_days = days[train_end:validation_end]
    if len(validation_days) < 5:
        raise ValueError("驗證區間少於5個交易日")
    return {
        "train": frame[frame["trade_date"].isin(train_days)].copy(),
        "validation": frame[
            frame["trade_date"].isin(validation_days)
        ].copy(),
    }


def _json_number_list(values: Any) -> list[Any]:
    array = np.asarray(values)
    if array.ndim == 0:
        return [float(array)]
    return array.tolist()


def _export_pipeline(model: Pipeline) -> dict[str, Any]:
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
        raise ValueError("模型成品格式或特徵數不一致")
    standardized = (
        feature_values - mean
    ) / np.where(scale == 0.0, 1.0, scale)
    scores = np.matmul(coef, standardized) + intercept
    if len(classes) == 2 and coef.shape[0] == 1:
        score = float(np.clip(scores[0], -50.0, 50.0))
        positive = 1.0 / (1.0 + math.exp(-score))
        return {
            classes[0]: float(1.0 - positive),
            classes[1]: float(positive),
        }
    stable = scores - np.max(scores)
    probability = np.exp(stable)
    probability = probability / probability.sum()
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


def prepare_hybrid_model(
    training_start_date: str = "2026-04-14",
    training_cutoff: str = "2026-07-13",
    persist: bool = True,
) -> dict[str, Any]:
    """建立 v8.2：All48 事件模型 + Base17 方向模型。"""
    started = time.perf_counter()
    try:
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
                "version": SERVICE_VERSION,
            }
        frame, frame_quality = prepare_v8_1_training_frame(rows)
        if len(frame) < MIN_TRAINING_ROWS:
            return {
                "ok": False,
                "message": (
                    f"可訓練資料不足：目前{len(frame)}筆，"
                    f"至少需要{MIN_TRAINING_ROWS}筆"
                ),
                "version": SERVICE_VERSION,
            }
        missing = [
            column
            for column in set(
                EVENT_FEATURE_COLUMNS + DIRECTION_FEATURE_COLUMNS
            )
            if column not in frame.columns
        ]
        if missing:
            return {
                "ok": False,
                "message": f"缺少模型特徵：{', '.join(sorted(missing))}",
                "version": SERVICE_VERSION,
            }

        split = _date_split(frame)
        train = split["train"]
        validation = split["validation"]
        train_direction = train[train["target_event"] == 1].copy()
        validation_direction = validation[
            validation["target_event"] == 1
        ].copy()
        if min(len(train_direction), len(validation_direction)) < 100:
            raise ValueError("突破100點的方向樣本不足")

        event_c, event_candidates = _select_classifier_c(
            train,
            validation,
            EVENT_FEATURE_COLUMNS,
            "target_event",
        )
        direction_c, direction_candidates = _select_classifier_c(
            train_direction,
            validation_direction,
            DIRECTION_FEATURE_COLUMNS,
            "target_direction",
        )
        tuning_event = _new_classifier(event_c)
        tuning_event.fit(
            train[EVENT_FEATURE_COLUMNS],
            train["target_event"].astype(int),
        )
        tuning_direction = _new_classifier(direction_c)
        tuning_direction.fit(
            train_direction[DIRECTION_FEATURE_COLUMNS],
            train_direction["target_direction"].astype(int),
        )
        thresholds, threshold_candidates = _select_thresholds(
            tuning_event,
            tuning_direction,
            validation,
        )

        final_event = _new_classifier(event_c)
        final_event.fit(
            frame[EVENT_FEATURE_COLUMNS],
            frame["target_event"].astype(int),
        )
        all_direction = frame[frame["target_event"] == 1].copy()
        final_direction = _new_classifier(direction_c)
        final_direction.fit(
            all_direction[DIRECTION_FEATURE_COLUMNS],
            all_direction["target_direction"].astype(int),
        )

        artifact = {
            "artifact_key": ARTIFACT_KEY,
            "model_version": MODEL_VERSION,
            "service_version": SERVICE_VERSION,
            "repository_version": REPOSITORY_VERSION,
            "training_start_date": str(training_start_date),
            "training_cutoff": str(training_cutoff),
            # 舊 artifact schema 只有一個 feature_columns，保存事件模型欄位；
            # 方向模型欄位明確存入 metadata。
            "feature_columns": EVENT_FEATURE_COLUMNS,
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
                "architecture": "all48_event_plus_base17_direction",
                "event_feature_columns": EVENT_FEATURE_COLUMNS,
                "direction_feature_columns": DIRECTION_FEATURE_COLUMNS,
                "event_feature_count": len(EVENT_FEATURE_COLUMNS),
                "direction_feature_count": len(DIRECTION_FEATURE_COLUMNS),
                "neutral_threshold_points": 100,
                "feature_window_minutes": 60,
                "prediction_horizon_minutes": 15,
                "feature_selection_cutoff": FEATURE_SELECTION_CUTOFF,
                "unbiased_forward_start_date": UNBIASED_FORWARD_START,
                "event_model_candidates": event_candidates,
                "direction_model_candidates": direction_candidates,
                "top_threshold_candidates": threshold_candidates,
                "frame_quality": frame_quality,
            },
        }
        persist_result = {
            "requested": bool(persist),
            "success": False,
            "rows": 0,
            "message": "preview only",
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
            "version": SERVICE_VERSION,
            "model_version": MODEL_VERSION,
            "artifact_key": ARTIFACT_KEY,
            "architecture": {
                "event": {
                    "feature_group": "all",
                    "feature_count": len(EVENT_FEATURE_COLUMNS),
                    "selected_c": event_c,
                },
                "direction": {
                    "feature_group": "base",
                    "feature_count": len(DIRECTION_FEATURE_COLUMNS),
                    "selected_c": direction_c,
                },
            },
            "thresholds": artifact["thresholds"],
            "training_start_date": str(training_start_date),
            "training_cutoff": str(training_cutoff),
            "feature_selection_cutoff": FEATURE_SELECTION_CUTOFF,
            "unbiased_forward_start_date": UNBIASED_FORWARD_START,
            "training_rows": int(len(frame)),
            "trade_days": int(frame["trade_date"].nunique()),
            "release_status": "shadow_only",
            "line_enabled": False,
            "persist": persist_result,
            "repository_status": repository_status,
            "seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        _debug("prepare failed", "| error =", repr(exc))
        return {
            "ok": False,
            "message": "v8.2 Hybrid模型成品建立失敗",
            "error": repr(exc),
            "version": SERVICE_VERSION,
            "seconds": round(time.perf_counter() - started, 3),
        }


def _timestamp_text(value: Any) -> str:
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return ""
        return stamp.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _filter_completed_rows(
    frame: pd.DataFrame,
    cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
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
    index = pd.to_datetime(work.index, errors="coerce")
    valid = ~pd.isna(index)
    work = work.loc[valid].copy()
    index = pd.DatetimeIndex(index[valid])
    if index.tz is not None:
        index = index.tz_convert(TAIPEI_TZ).tz_localize(None)
    work.index = index.floor("min")
    work = work.sort_index()
    raw_rows = int(len(work))
    raw_last = work.index[-1] if raw_rows else None
    work = work.loc[work.index <= cutoff].copy()
    work.attrs.update(attrs)
    return work, {
        "raw_rows": raw_rows,
        "used_rows": int(len(work)),
        "dropped_incomplete_rows": raw_rows - int(len(work)),
        "raw_last": _timestamp_text(raw_last),
        "used_last": _timestamp_text(
            work.index[-1] if not work.empty else None
        ),
    }


def _build_live_frame(
    taiex_df: pd.DataFrame,
    txf_df: pd.DataFrame,
) -> pd.DataFrame:
    from services.market_prediction_data_service_v2 import _normalize_1m_frame

    taiex = _normalize_1m_frame(taiex_df, "taiex")
    txf = _normalize_1m_frame(txf_df, "txf")
    if taiex.empty or txf.empty:
        return pd.DataFrame()
    raw = taiex.join(txf, how="inner")
    raw = raw[~raw.index.duplicated(keep="last")].sort_index()
    return build_v8_feature_frame(raw, include_targets=False)


def _fetch_live_frame(now: datetime) -> tuple[pd.DataFrame, dict[str, Any]]:
    from services.market_future_kline_service import (
        get_market_future_1m_history,
    )
    from services.market_index_service import get_market_index_1m_history

    trade_date = now.strftime("%Y-%m-%d")
    taiex_df = get_market_index_1m_history(trade_date, trade_date)
    txf_df = get_market_future_1m_history(trade_date, trade_date)
    cutoff = pd.Timestamp(now).tz_localize(None).floor("min")
    taiex_df, taiex_guard = _filter_completed_rows(taiex_df, cutoff)
    txf_df, txf_guard = _filter_completed_rows(txf_df, cutoff)
    frame = _build_live_frame(taiex_df, txf_df)
    source_status = {
        "taiex_rows": int(len(taiex_df)),
        "txf_rows": int(len(txf_df)),
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
    return frame, source_status


def _direction_from_points(change_points: float) -> str:
    if change_points > NEUTRAL_THRESHOLD_POINTS:
        return "up"
    if change_points < -NEUTRAL_THRESHOLD_POINTS:
        return "down"
    return "flat"


def _settle_pending(
    live_frame: pd.DataFrame,
    latest_ts: pd.Timestamp,
) -> dict[str, Any]:
    latest_local = latest_ts.tz_localize(TAIPEI_TZ)
    pending = get_unsettled_shadow_predictions(
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
        success = update_shadow_result(
            str(row.get("prediction_ts") or ""),
            {
                "actual_close": round(actual_close, 4),
                "actual_change_points": round(change_points, 4),
                "actual_direction": actual_direction,
                "is_correct": (
                    signal == actual_direction
                    if signal in {"up", "down"}
                    else None
                ),
                "status": "settled",
                "settled_at": latest_local.isoformat(),
            },
        )
        settled += int(bool(success))
    return {"pending_rows": len(pending), "settled_rows": settled}


def _stored_result(now: datetime) -> dict[str, Any]:
    row = get_latest_shadow_prediction()
    if not row:
        return {
            "ok": False,
            "message": "目前沒有v8.2已保存影子預測",
            "version": SERVICE_VERSION,
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
        "message": "目前非盤中，顯示最近一次v8.2影子預測",
        "version": SERVICE_VERSION,
        "source": "stored_v8_2_shadow_prediction",
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
        "release_status": "shadow_only",
        "line_enabled": False,
        "unbiased_forward_start_date": UNBIASED_FORWARD_START,
        "model_note": "v8.2獨立影子測試，非交易建議",
    }


def predict_hybrid_shadow(
    persist: bool = False,
    force_live: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    now = _now_taipei()
    in_session = (
        now.weekday() < 5
        and SESSION_FETCH_START <= now.time() <= SESSION_FETCH_END
    )
    if not in_session and not force_live:
        result = _stored_result(now)
        result["seconds"] = round(time.perf_counter() - started, 3)
        return result

    if now.time() < PREDICTION_START and not force_live:
        return {
            "ok": True,
            "skipped": True,
            "skip_reason": "market_warmup",
            "message": "盤初資料累積中，09:15後才開始預測",
            "version": SERVICE_VERSION,
            "session_status": "warming_up",
            "is_live": False,
            "prediction_allowed": False,
            "release_status": "shadow_only",
            "persist": {
                "requested": bool(persist),
                "success": False,
                "rows": 0,
                "message": "safe skip: market_warmup",
            },
            "settlement": {
                "requested": bool(persist),
                "pending_rows": 0,
                "settled_rows": 0,
            },
            "seconds": round(time.perf_counter() - started, 3),
        }

    artifact = _load_artifact()
    if not artifact:
        return {
            "ok": False,
            "message": "尚未建立v8.2模型成品",
            "version": SERVICE_VERSION,
            "artifact_key": ARTIFACT_KEY,
            "seconds": round(time.perf_counter() - started, 3),
        }
    try:
        live_frame, source_status = _fetch_live_frame(now)
        if live_frame.empty:
            return {
                "ok": True,
                "skipped": True,
                "skip_reason": "source_not_ready",
                "message": "上市或台指期分鐘資料不足，暫不預測",
                "version": SERVICE_VERSION,
                "prediction_allowed": False,
                "persist": {
                    "requested": bool(persist),
                    "success": False,
                    "rows": 0,
                    "message": "safe skip: source_not_ready",
                },
                "settlement": {
                    "requested": bool(persist),
                    "pending_rows": 0,
                    "settled_rows": 0,
                },
                "source_status": source_status,
                "seconds": round(time.perf_counter() - started, 3),
            }
        ready = live_frame.replace([np.inf, -np.inf], np.nan).dropna(
            subset=list(
                dict.fromkeys(
                    EVENT_FEATURE_COLUMNS + DIRECTION_FEATURE_COLUMNS
                )
            )
        )
        if ready.empty:
            return {
                "ok": True,
                "skipped": True,
                "skip_reason": "features_not_ready",
                "message": "v8.2特徵尚未完整",
                "version": SERVICE_VERSION,
                "prediction_allowed": False,
                "persist": {
                    "requested": bool(persist),
                    "success": False,
                    "rows": 0,
                    "message": "safe skip: features_not_ready",
                },
                "settlement": {
                    "requested": bool(persist),
                    "pending_rows": 0,
                    "settled_rows": 0,
                },
                "source_status": source_status,
                "seconds": round(time.perf_counter() - started, 3),
            }
        latest_ts = pd.Timestamp(ready.index[-1])
        day_count = int(
            (ready.index.normalize() == latest_ts.normalize()).sum()
        )
        if day_count < 16:
            return {
                "ok": True,
                "skipped": True,
                "skip_reason": "market_warmup",
                "message": "開盤尚未累積完整15分鐘歷史",
                "version": SERVICE_VERSION,
                "prediction_allowed": False,
                "persist": {
                    "requested": bool(persist),
                    "success": False,
                    "rows": 0,
                    "message": "safe skip: market_warmup",
                },
                "settlement": {
                    "requested": bool(persist),
                    "pending_rows": 0,
                    "settled_rows": 0,
                },
                "source_status": source_status,
                "day_feature_rows": day_count,
                "seconds": round(time.perf_counter() - started, 3),
            }

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
                "message": "即時來源超過5分鐘，顯示最近一次v8.2預測",
                "freshness_status": freshness_status,
                "live_source_age_minutes": round(age_minutes, 2),
                "source_status": source_status,
                "seconds": round(time.perf_counter() - started, 3),
            })
            return stored

        metadata = artifact.get("metadata") or {}
        event_columns = list(
            metadata.get("event_feature_columns")
            or artifact.get("feature_columns")
            or EVENT_FEATURE_COLUMNS
        )
        direction_columns = list(
            metadata.get("direction_feature_columns")
            or DIRECTION_FEATURE_COLUMNS
        )
        event_probability = _manual_probability(
            artifact.get("event_model") or {},
            latest[event_columns].to_numpy(dtype=float),
        ).get(1, 0.0)
        up_probability = _manual_probability(
            artifact.get("direction_model") or {},
            latest[direction_columns].to_numpy(dtype=float),
        ).get(1, 0.0)
        direction_confidence = max(up_probability, 1.0 - up_probability)
        thresholds = artifact.get("thresholds") or {}
        event_threshold = float(
            thresholds.get("event_probability_threshold") or 0.70
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
        row = {
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
        settlement = {
            "requested": bool(persist),
            "pending_rows": 0,
            "settled_rows": 0,
        }
        persist_result = {
            "requested": bool(persist),
            "success": False,
            "rows": 0,
            "message": (
                "preview only"
                if not persist
                else "13:15後不建立新的15分鐘預測"
            ),
        }
        if persist:
            settlement = {
                "requested": True,
                **_settle_pending(ready, latest_ts),
            }
            if prediction_allowed:
                persist_result = {
                    "requested": True,
                    **upsert_shadow_prediction(row),
                }

        result = {
            "ok": True,
            "message": "ok",
            "version": SERVICE_VERSION,
            "model_version": MODEL_VERSION,
            "architecture": "all48_event_plus_base17_direction",
            "source": "live_1m_v8_2_features",
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
                "feature_selection_cutoff": metadata.get(
                    "feature_selection_cutoff"
                ),
                "unbiased_forward_start_date": metadata.get(
                    "unbiased_forward_start_date"
                ),
            },
            "release_status": "shadow_only",
            "line_enabled": False,
            "model_note": "v8.2獨立影子測試，非交易建議",
            "persist": persist_result,
            "settlement": settlement,
            "seconds": round(time.perf_counter() - started, 3),
        }
        _debug(
            "predict",
            "| ts =", result["prediction_ts"],
            "| signal =", signal,
            "| event =", result["event_probability"],
            "| up =", result["up_probability"],
            "| persisted =", persist_result.get("success"),
            "| settled =", settlement.get("settled_rows"),
            "| sec =", result["seconds"],
        )
        return result
    except Exception as exc:
        _debug("predict failed", "| error =", repr(exc))
        return {
            "ok": False,
            "message": "v8.2 Hybrid影子預測失敗",
            "error": repr(exc),
            "version": SERVICE_VERSION,
            "seconds": round(time.perf_counter() - started, 3),
        }


def evaluate_hybrid_shadow(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    today = _now_taipei().date()
    start_text = str(start_date or UNBIASED_FORWARD_START)
    end_text = str(end_date or today.strftime("%Y-%m-%d"))
    rows = get_shadow_history(start_text, end_text, limit=10000)
    settled = [
        row
        for row in rows
        if str(row.get("status") or "") == "settled"
        and str(row.get("actual_direction") or "") in {"up", "down", "flat"}
    ]
    signals = [
        row
        for row in settled
        if str(row.get("signal") or "") in {"up", "down"}
    ]
    correct = [row for row in signals if bool(row.get("is_correct"))]
    up = [row for row in signals if str(row.get("signal")) == "up"]
    down = [row for row in signals if str(row.get("signal")) == "down"]
    correct_up = [row for row in up if bool(row.get("is_correct"))]
    correct_down = [row for row in down if bool(row.get("is_correct"))]
    trade_days = sorted({
        str(row.get("trade_date"))
        for row in settled
        if row.get("trade_date")
    })
    return {
        "ok": True,
        "message": "ok",
        "version": SERVICE_VERSION,
        "model_version": MODEL_VERSION,
        "architecture": "all48_event_plus_base17_direction",
        "start_date": start_text,
        "end_date": end_text,
        "unbiased_forward_start_date": UNBIASED_FORWARD_START,
        "trade_days": len(trade_days),
        "rows": len(rows),
        "settled_rows": len(settled),
        "signal_rows": len(signals),
        "up_signal_rows": len(up),
        "down_signal_rows": len(down),
        "settlement_ratio": _safe_ratio(len(settled), len(rows)),
        "signal_coverage": _safe_ratio(len(signals), len(settled)),
        "directional_precision": _safe_ratio(len(correct), len(signals)),
        "up_precision": _safe_ratio(len(correct_up), len(up)),
        "down_precision": _safe_ratio(len(correct_down), len(down)),
        "minimum_shadow_days": 20,
        "remaining_shadow_days": max(0, 20 - len(trade_days)),
        "ready_for_comparison": len(trade_days) >= 20,
        "release_status": "shadow_only",
        "line_enabled": False,
        "seconds": round(time.perf_counter() - started, 3),
    }


def compare_v7_and_v8_2(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """只比較兩版同一分鐘且皆已結算的影子紀錄。"""
    started = time.perf_counter()
    today = _now_taipei().date()
    start_text = str(start_date or UNBIASED_FORWARD_START)
    end_text = str(end_date or today.strftime("%Y-%m-%d"))
    from services.supabase_service import (
        get_market_prediction_shadow_history,
    )

    v7_rows = get_market_prediction_shadow_history(
        start_text,
        end_text,
        limit=10000,
    )
    v7_rows = [
        row
        for row in v7_rows
        if str(row.get("artifact_key") or "")
        == "taiex_15m_selective_v7_fixed_100pt"
        and str(row.get("status") or "") == "settled"
    ]
    v82_rows = [
        row
        for row in get_shadow_history(start_text, end_text, limit=10000)
        if str(row.get("status") or "") == "settled"
    ]

    def minute_key(row: dict[str, Any]) -> str:
        stamp = pd.to_datetime(
            row.get("prediction_ts"),
            errors="coerce",
            utc=True,
        )
        return "" if pd.isna(stamp) else stamp.floor("min").isoformat()

    v7_map = {minute_key(row): row for row in v7_rows if minute_key(row)}
    v82_map = {minute_key(row): row for row in v82_rows if minute_key(row)}
    common = sorted(set(v7_map) & set(v82_map))

    def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
        signals = [
            row
            for row in rows
            if str(row.get("signal") or "") in {"up", "down"}
        ]
        correct = [row for row in signals if bool(row.get("is_correct"))]
        up = [row for row in signals if str(row.get("signal")) == "up"]
        down = [row for row in signals if str(row.get("signal")) == "down"]
        return {
            "rows": len(rows),
            "signal_rows": len(signals),
            "signal_coverage": _safe_ratio(len(signals), len(rows)),
            "directional_precision": _safe_ratio(len(correct), len(signals)),
            "up_signal_rows": len(up),
            "down_signal_rows": len(down),
            "up_precision": _safe_ratio(
                sum(bool(row.get("is_correct")) for row in up),
                len(up),
            ),
            "down_precision": _safe_ratio(
                sum(bool(row.get("is_correct")) for row in down),
                len(down),
            ),
        }

    v7_common = [v7_map[key] for key in common]
    v82_common = [v82_map[key] for key in common]
    trade_days = sorted({
        str(row.get("trade_date") or "")
        for row in v82_common
        if row.get("trade_date")
    })
    v7_metrics = metrics(v7_common)
    v82_metrics = metrics(v82_common)
    ready = len(trade_days) >= 20
    v7_precision = v7_metrics.get("directional_precision")
    v82_precision = v82_metrics.get("directional_precision")
    winner = "collecting"
    if ready and v7_precision is not None and v82_precision is not None:
        winner = (
            "v8.2"
            if float(v82_precision) > float(v7_precision)
            else "v7"
            if float(v7_precision) > float(v82_precision)
            else "tie"
        )
    return {
        "ok": True,
        "message": "ok",
        "version": SERVICE_VERSION,
        "start_date": start_text,
        "end_date": end_text,
        "unbiased_forward_start_date": UNBIASED_FORWARD_START,
        "same_minute_rows": len(common),
        "trade_days": len(trade_days),
        "minimum_comparison_days": 20,
        "remaining_days": max(0, 20 - len(trade_days)),
        "ready_for_decision": ready,
        "winner": winner,
        "v7": v7_metrics,
        "v8_2": v82_metrics,
        "comparison_note": (
            "僅比較兩版同一分鐘且皆已結算的預測，"
            "避免不同排程時間造成不公平。"
        ),
        "seconds": round(time.perf_counter() - started, 3),
    }
