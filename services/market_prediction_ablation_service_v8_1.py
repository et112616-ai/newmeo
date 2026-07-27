from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.metrics import (
    accuracy_score,
    mean_absolute_error,
    mean_squared_error,
    median_absolute_error,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from services import market_prediction_selective_service_v8_lite as _v8
from services.market_prediction_features_v8_1 import (
    FEATURE_GROUPS,
    FEATURE_SERVICE_VERSION,
    NEUTRAL_THRESHOLD_POINTS,
    feature_columns_for_group,
    normalize_feature_group,
    prepare_v8_1_training_frame,
)
from services.market_prediction_repository_v8_1 import (
    REPOSITORY_VERSION,
    load_market_prediction_rows_paginated,
)


MODEL_VERSION = "2026-07-27-v8.1-FAIR-ABLATION-NORMALIZED-RIDGE"
MIN_TRAINING_ROWS = 8000

# v8.0 的共用函式以模組級 FEATURE_COLUMNS 取欄位。離線比較一次只允許
# 一組執行，並在 finally 還原，避免不同請求互相污染。
_MODEL_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}


def _safe_ratio(
    numerator: int | float,
    denominator: int | float,
) -> float | None:
    if denominator <= 0:
        return None
    return round(float(numerator / denominator), 6)


def _new_normalized_regressor(alpha: float) -> Pipeline:
    return Pipeline(
        steps=[
            ("scale", StandardScaler()),
            ("model", Ridge(alpha=float(alpha))),
        ]
    )


def _point_regression_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
) -> dict[str, Any]:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    absolute_error = np.abs(y_true - y_pred)
    true_direction = np.where(
        y_true > NEUTRAL_THRESHOLD_POINTS,
        1,
        np.where(y_true < -NEUTRAL_THRESHOLD_POINTS, -1, 0),
    )
    predicted_direction = np.where(
        y_pred > NEUTRAL_THRESHOLD_POINTS,
        1,
        np.where(y_pred < -NEUTRAL_THRESHOLD_POINTS, -1, 0),
    )
    return {
        "rows": int(len(y_true)),
        "mae_points": round(float(mean_absolute_error(y_true, y_pred)), 4),
        "rmse_points": round(
            float(np.sqrt(mean_squared_error(y_true, y_pred))),
            4,
        ),
        "median_ae_points": round(
            float(median_absolute_error(y_true, y_pred)),
            4,
        ),
        "within_50_points": _safe_ratio(
            int((absolute_error <= 50.0).sum()),
            len(y_true),
        ),
        "within_100_points": _safe_ratio(
            int((absolute_error <= 100.0).sum()),
            len(y_true),
        ),
        "fixed_100pt_direction_accuracy": round(
            float(accuracy_score(true_direction, predicted_direction)),
            6,
        ),
        "sign_accuracy": round(
            float(
                accuracy_score(
                    np.sign(y_true).astype(int),
                    np.sign(y_pred).astype(int),
                )
            ),
            6,
        ),
        "prediction_mean_points": round(float(np.mean(y_pred)), 4),
        "actual_mean_points": round(float(np.mean(y_true)), 4),
    }


def _select_regression_alpha(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    columns: list[str],
) -> tuple[float, list[dict[str, Any]], dict[str, float]]:
    candidates: list[dict[str, Any]] = []
    best_alpha = 10.0
    best_mae: float | None = None
    best_normalized_residual = np.asarray([], dtype=float)

    for alpha in (1.0, 3.0, 10.0, 30.0, 100.0, 300.0):
        model = _new_normalized_regressor(alpha)
        model.fit(train[columns], train["target_normalized_15m"])
        normalized_prediction = model.predict(validation[columns])
        point_prediction = (
            normalized_prediction
            * validation["target_scale_points"].to_numpy(dtype=float)
        )
        actual_points = validation[
            "taiex_change_15m_points"
        ].to_numpy(dtype=float)
        metrics = _point_regression_metrics(actual_points, point_prediction)
        candidates.append({"alpha": alpha, **metrics})
        mae = float(metrics["mae_points"])
        if best_mae is None or mae < best_mae:
            best_mae = mae
            best_alpha = alpha
            best_normalized_residual = (
                validation["target_normalized_15m"].to_numpy(dtype=float)
                - normalized_prediction
            )

    quantiles = {
        "q10": round(
            float(np.quantile(best_normalized_residual, 0.10)),
            6,
        ),
        "q50": round(
            float(np.quantile(best_normalized_residual, 0.50)),
            6,
        ),
        "q90": round(
            float(np.quantile(best_normalized_residual, 0.90)),
            6,
        ),
    }
    return best_alpha, candidates, quantiles


def _fit_bundle(
    frame: pd.DataFrame,
    columns: list[str],
) -> dict[str, Any]:
    split = _v8._date_split(frame)
    train = split["train"]
    validation = split["validation"]
    train_direction = train[train["target_event"] == 1].copy()
    validation_direction = validation[
        validation["target_event"] == 1
    ].copy()
    if min(len(train_direction), len(validation_direction)) < 100:
        raise ValueError("突破100點的方向樣本不足")

    event_c, event_candidates = _v8._select_classifier_c(
        train,
        validation,
        "target_event",
    )
    direction_c, direction_candidates = _v8._select_classifier_c(
        train_direction,
        validation_direction,
        "target_direction",
    )

    tuning_event = _v8._new_classifier(event_c)
    tuning_event.fit(train[columns], train["target_event"].astype(int))
    tuning_direction = _v8._new_classifier(direction_c)
    tuning_direction.fit(
        train_direction[columns],
        train_direction["target_direction"].astype(int),
    )
    thresholds, threshold_candidates = _v8._select_thresholds(
        tuning_event,
        tuning_direction,
        validation,
    )
    regression_alpha, regression_candidates, residual_quantiles = (
        _select_regression_alpha(train, validation, columns)
    )
    return {
        "split": split,
        "event_c": event_c,
        "direction_c": direction_c,
        "thresholds": thresholds,
        "event_candidates": event_candidates,
        "direction_candidates": direction_candidates,
        "threshold_candidates": threshold_candidates,
        "regression_alpha": regression_alpha,
        "regression_candidates": regression_candidates,
        "residual_quantiles": residual_quantiles,
    }


def _fit_final_models(
    frame: pd.DataFrame,
    bundle: dict[str, Any],
    columns: list[str],
) -> dict[str, Pipeline]:
    event_model = _v8._new_classifier(bundle["event_c"])
    event_model.fit(frame[columns], frame["target_event"].astype(int))

    direction_frame = frame[frame["target_event"] == 1].copy()
    direction_model = _v8._new_classifier(bundle["direction_c"])
    direction_model.fit(
        direction_frame[columns],
        direction_frame["target_direction"].astype(int),
    )

    regression_model = _new_normalized_regressor(
        bundle["regression_alpha"]
    )
    regression_model.fit(
        frame[columns],
        frame["target_normalized_15m"],
    )
    return {
        "event": event_model,
        "direction": direction_model,
        "regression": regression_model,
    }


def _classification_predictions(
    models: dict[str, Pipeline],
    frame: pd.DataFrame,
    thresholds: dict[str, Any],
) -> np.ndarray:
    event_probability = _v8._probability_for_label(
        models["event"],
        frame,
        1,
    )
    up_probability = _v8._probability_for_label(
        models["direction"],
        frame,
        1,
    )
    return _v8._selective_predictions(
        event_probability,
        up_probability,
        float(thresholds["event_probability_threshold"]),
        float(thresholds["direction_confidence_threshold"]),
    )


def _evaluate(
    models: dict[str, Pipeline],
    frame: pd.DataFrame,
    bundle: dict[str, Any],
    columns: list[str],
) -> dict[str, Any]:
    prediction = _classification_predictions(
        models,
        frame,
        bundle["thresholds"],
    )
    scale = frame["target_scale_points"].to_numpy(dtype=float)
    normalized_prediction = models["regression"].predict(frame[columns])
    point_prediction = normalized_prediction * scale
    actual_points = frame[
        "taiex_change_15m_points"
    ].to_numpy(dtype=float)

    regression = _point_regression_metrics(actual_points, point_prediction)
    zero_baseline_mae = float(np.mean(np.abs(actual_points)))
    previous_move = frame[
        "taiex_previous_change_15m_points"
    ].to_numpy(dtype=float)
    regression["zero_change_baseline_mae_points"] = round(
        zero_baseline_mae,
        4,
    )
    regression["momentum_baseline_mae_points"] = round(
        float(mean_absolute_error(actual_points, previous_move)),
        4,
    )
    regression["mae_gain_vs_zero_baseline_points"] = round(
        zero_baseline_mae - float(regression["mae_points"]),
        4,
    )
    residuals = bundle["residual_quantiles"]
    lower = (
        normalized_prediction + float(residuals["q10"])
    ) * scale
    upper = (
        normalized_prediction + float(residuals["q90"])
    ) * scale
    regression["p10_p90_interval_coverage"] = _safe_ratio(
        int(((actual_points >= lower) & (actual_points <= upper)).sum()),
        len(actual_points),
    )
    regression["normalized_residual_quantiles_from_validation"] = residuals

    by_day: list[dict[str, Any]] = []
    for trade_date, positions in frame.groupby(
        "trade_date",
        sort=True,
    ).indices.items():
        positions_array = np.asarray(positions, dtype=int)
        day = frame.iloc[positions_array]
        by_day.append({
            "trade_date": pd.Timestamp(trade_date).strftime("%Y-%m-%d"),
            "rows": int(len(day)),
            "selective": _v8._selective_metrics(
                day,
                prediction[positions_array],
            ),
            "regression": _point_regression_metrics(
                actual_points[positions_array],
                point_prediction[positions_array],
            ),
        })

    non_overlapping: list[dict[str, Any]] = []
    for offset in range(15):
        positions = np.arange(offset, len(frame), 15, dtype=int)
        subset = frame.iloc[positions]
        metrics = _v8._selective_metrics(
            subset,
            prediction[positions],
        )
        non_overlapping.append({
            "offset": offset,
            **metrics,
        })

    return {
        "selective": _v8._selective_metrics(frame, prediction),
        "regression": regression,
        "by_day": by_day,
        "non_overlapping_15m": {
            "groups": 15,
            "rows_total": int(
                sum(item["rows"] for item in non_overlapping)
            ),
            "directional_precision_mean": round(
                float(
                    np.mean([
                        float(item.get("directional_precision") or 0.0)
                        for item in non_overlapping
                    ])
                ),
                6,
            ),
            "signal_coverage_mean": round(
                float(
                    np.mean([
                        float(item.get("signal_coverage") or 0.0)
                        for item in non_overlapping
                    ])
                ),
                6,
            ),
            "macro_f1_mean": round(
                float(
                    np.mean([
                        float(item.get("macro_f1") or 0.0)
                        for item in non_overlapping
                    ])
                ),
                6,
            ),
            "by_offset": non_overlapping,
        },
    }


def evaluate_market_prediction_v8_1(
    training_start_date: str,
    training_cutoff: str,
    evaluation_start_date: str,
    evaluation_end_date: str,
    feature_group: str = "base",
    force: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        group = normalize_feature_group(feature_group)
        columns = feature_columns_for_group(group)
        training_start = pd.Timestamp(training_start_date).normalize()
        cutoff = pd.Timestamp(training_cutoff).normalize()
        evaluation_start = pd.Timestamp(evaluation_start_date).normalize()
        evaluation_end = pd.Timestamp(evaluation_end_date).normalize()
    except Exception as exc:
        return {
            "ok": False,
            "message": str(exc),
            "version": MODEL_VERSION,
        }

    if evaluation_start <= cutoff:
        return {
            "ok": False,
            "message": "前瞻區間必須晚於 training_cutoff",
            "version": MODEL_VERSION,
        }
    if cutoff < training_start or evaluation_end < evaluation_start:
        return {
            "ok": False,
            "message": "日期區間錯誤",
            "version": MODEL_VERSION,
        }

    dates = [
        value.strftime("%Y-%m-%d")
        for value in (
            training_start,
            cutoff,
            evaluation_start,
            evaluation_end,
        )
    ]
    cache_key = ":".join([*dates, group])
    if not force and cache_key in _CACHE:
        result = dict(_CACHE[cache_key])
        result["cached"] = True
        return result

    training_rows, training_status = load_market_prediction_rows_paginated(
        dates[0],
        dates[1],
        limit=50000,
    )
    evaluation_rows, evaluation_status = load_market_prediction_rows_paginated(
        dates[2],
        dates[3],
        limit=50000,
    )
    if not training_status.get("complete") or not evaluation_status.get(
        "complete"
    ):
        return {
            "ok": False,
            "message": "Supabase資料讀取不完整",
            "version": MODEL_VERSION,
            "repository_status": {
                "training": training_status,
                "evaluation": evaluation_status,
            },
        }

    training_frame, training_quality = prepare_v8_1_training_frame(
        training_rows
    )
    evaluation_frame, evaluation_quality = prepare_v8_1_training_frame(
        evaluation_rows
    )
    if len(training_frame) < MIN_TRAINING_ROWS:
        return {
            "ok": False,
            "message": (
                f"歷史可訓練資料不足：目前 {len(training_frame)} 筆，"
                f"至少需要 {MIN_TRAINING_ROWS} 筆"
            ),
            "version": MODEL_VERSION,
        }
    if evaluation_frame.empty:
        return {
            "ok": False,
            "message": "前瞻區間沒有可評估資料",
            "version": MODEL_VERSION,
        }
    missing = [
        column
        for column in columns
        if column not in training_frame.columns
        or column not in evaluation_frame.columns
    ]
    if missing:
        return {
            "ok": False,
            "message": f"缺少模型特徵：{', '.join(missing)}",
            "version": MODEL_VERSION,
        }

    if not _MODEL_LOCK.acquire(blocking=False):
        return {
            "ok": True,
            "message": "已有一組v8.1離線比較正在執行，本次安全略過",
            "version": MODEL_VERSION,
            "skipped": True,
            "skip_reason": "v8_1_ablation_already_running",
        }

    original_columns = list(_v8.FEATURE_COLUMNS)
    try:
        _v8.FEATURE_COLUMNS = list(columns)
        bundle = _fit_bundle(training_frame, columns)
        models = _fit_final_models(training_frame, bundle, columns)
        evaluation = _evaluate(
            models,
            evaluation_frame,
            bundle,
            columns,
        )
    except Exception as exc:
        return {
            "ok": False,
            "message": repr(exc),
            "version": MODEL_VERSION,
        }
    finally:
        _v8.FEATURE_COLUMNS = original_columns
        _MODEL_LOCK.release()

    result = {
        "ok": True,
        "message": "ok",
        "version": MODEL_VERSION,
        "mode": "v8_1_fair_single_group_frozen_forward_holdout",
        "cached": False,
        "feature_group": group,
        "feature_count": len(columns),
        "features": columns,
        "available_feature_groups": {
            key: len(value) for key, value in FEATURE_GROUPS.items()
        },
        "feature_service_version": FEATURE_SERVICE_VERSION,
        "repository_version": REPOSITORY_VERSION,
        "prediction_horizon_minutes": 15,
        "neutral_threshold_points": int(NEUTRAL_THRESHOLD_POINTS),
        "fair_comparison_guard": {
            "passed": True,
            "core_warmup_minutes_removed": 15,
            "expected_complete_rows_per_day": 240,
            "training": training_quality,
            "evaluation": evaluation_quality,
        },
        "leakage_guard": {
            "passed": True,
            "training_start_date": dates[0],
            "model_fitted_through": dates[1],
            "evaluation_start_date": dates[2],
            "evaluation_end_date": dates[3],
            "evaluation_rows_used_for_fit": 0,
            "evaluation_rows_used_for_threshold_selection": 0,
        },
        "training": {
            "database_rows": len(training_rows),
            "rows": len(training_frame),
            "trade_days": int(training_frame["trade_date"].nunique()),
        },
        "evaluation": {
            "database_rows": len(evaluation_rows),
            "rows": len(evaluation_frame),
            "trade_days": int(evaluation_frame["trade_date"].nunique()),
            **evaluation,
        },
        "selected": {
            "event_c": bundle["event_c"],
            "direction_c": bundle["direction_c"],
            "event_probability_threshold": bundle["thresholds"][
                "event_probability_threshold"
            ],
            "direction_confidence_threshold": bundle["thresholds"][
                "direction_confidence_threshold"
            ],
            "normalized_regression_alpha": bundle["regression_alpha"],
        },
        "validation_candidates": {
            "event": bundle["event_candidates"],
            "direction": bundle["direction_candidates"],
            "thresholds": bundle["threshold_candidates"],
            "normalized_regression": bundle["regression_candidates"],
        },
        "release_status": "offline_comparison_only",
        "deployment_ready": False,
        "shadow_mode_ready": False,
        "readiness_note": (
            "一次只測一組；先完成base，再測其他組。"
            "不得依單一前瞻區間直接替換V7。"
        ),
        "repository_status": {
            "training": training_status,
            "evaluation": evaluation_status,
        },
        "seconds": round(time.perf_counter() - started, 3),
    }
    _CACHE[cache_key] = dict(result)
    return result
