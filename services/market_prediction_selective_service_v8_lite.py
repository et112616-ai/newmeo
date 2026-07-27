from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    log_loss,
    mean_absolute_error,
    mean_squared_error,
    median_absolute_error,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from services.market_prediction_features_v8_lite import (
    FEATURE_COLUMNS,
    FEATURE_SERVICE_VERSION,
    NEUTRAL_THRESHOLD_POINTS,
    prepare_v8_training_frame,
)
from services.market_prediction_repository_v8_lite import (
    REPOSITORY_VERSION,
    load_market_prediction_rows_paginated,
)


MODEL_VERSION = "2026-07-27-v8.0-LITE-TWO-STAGE-RIDGE"
TAIPEI_TZ = "Asia/Taipei"
CLASS_LABELS = [-1, 0, 1]
CLASS_NAMES = {-1: "down", 0: "flat", 1: "up"}
MIN_TRADE_DAYS = 50
MIN_TRAINING_ROWS = 8000

_TRAIN_CACHE: dict[str, dict[str, Any]] = {}
_FORWARD_CACHE: dict[str, dict[str, Any]] = {}


def _debug(*args: Any) -> None:
    print("DEBUG market_prediction_v8_lite |", *args, flush=True)


def _date_range(
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str]:
    today = datetime.now(ZoneInfo(TAIPEI_TZ)).date()
    end = pd.Timestamp(end_date or today).normalize()
    start = pd.Timestamp(start_date or (end - timedelta(days=120))).normalize()
    if pd.isna(start) or pd.isna(end):
        raise ValueError("start_date / end_date 格式必須是 YYYY-MM-DD")
    if end < start:
        raise ValueError("end_date 不可早於 start_date")
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _date_split(frame: pd.DataFrame) -> dict[str, Any]:
    days = sorted(pd.DatetimeIndex(frame["trade_date"].unique()))
    if len(days) < MIN_TRADE_DAYS:
        raise ValueError(
            f"完整交易日不足：目前 {len(days)} 天，"
            f"至少需要 {MIN_TRADE_DAYS} 天"
        )

    train_end = max(1, int(len(days) * 0.70))
    validation_end = max(train_end + 1, int(len(days) * 0.85))
    validation_end = min(validation_end, len(days) - 1)
    train_days = days[:train_end]
    validation_days = days[train_end:validation_end]
    test_days = days[validation_end:]
    if min(len(train_days), len(validation_days), len(test_days)) < 5:
        raise ValueError("訓練、驗證或測試區間少於 5 個交易日")

    return {
        "train": frame[frame["trade_date"].isin(train_days)].copy(),
        "validation": frame[
            frame["trade_date"].isin(validation_days)
        ].copy(),
        "test": frame[frame["trade_date"].isin(test_days)].copy(),
        "train_days": train_days,
        "validation_days": validation_days,
        "test_days": test_days,
    }


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


def _new_regressor(alpha: float) -> Pipeline:
    return Pipeline(
        steps=[
            ("scale", StandardScaler()),
            ("model", Ridge(alpha=float(alpha))),
        ]
    )


def _safe_ratio(
    numerator: int | float,
    denominator: int | float,
) -> float | None:
    if denominator <= 0:
        return None
    return round(float(numerator / denominator), 6)


def _binary_metrics(
    model: Pipeline,
    frame: pd.DataFrame,
    target_column: str,
) -> dict[str, Any]:
    y_true = frame[target_column].astype(int).to_numpy()
    y_pred = model.predict(frame[FEATURE_COLUMNS]).astype(int)
    probability = model.predict_proba(frame[FEATURE_COLUMNS])
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
    target_column: str,
) -> tuple[float, list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    best_score: tuple[float, float, float] | None = None
    best_c = 0.05
    for c_value in (0.03, 0.05, 0.1, 0.3, 1.0):
        model = _new_classifier(c_value)
        model.fit(
            train[FEATURE_COLUMNS],
            train[target_column].astype(int),
        )
        metrics = _binary_metrics(model, validation, target_column)
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


def _regression_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
) -> dict[str, Any]:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
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
    absolute_error = np.abs(y_true - y_pred)
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
) -> tuple[float, list[dict[str, Any]], dict[str, float]]:
    candidates: list[dict[str, Any]] = []
    best_alpha = 10.0
    best_mae: float | None = None
    best_residuals = np.asarray([], dtype=float)
    target = "taiex_change_15m_points"

    for alpha in (1.0, 3.0, 10.0, 30.0, 100.0):
        model = _new_regressor(alpha)
        model.fit(train[FEATURE_COLUMNS], train[target])
        prediction = model.predict(validation[FEATURE_COLUMNS])
        metrics = _regression_metrics(
            validation[target].to_numpy(dtype=float),
            prediction,
        )
        candidates.append({"alpha": alpha, **metrics})
        mae = float(metrics["mae_points"])
        if best_mae is None or mae < best_mae:
            best_mae = mae
            best_alpha = alpha
            best_residuals = (
                validation[target].to_numpy(dtype=float) - prediction
            )

    residual_quantiles = {
        "q10": round(float(np.quantile(best_residuals, 0.10)), 4),
        "q50": round(float(np.quantile(best_residuals, 0.50)), 4),
        "q90": round(float(np.quantile(best_residuals, 0.90)), 4),
    }
    return best_alpha, candidates, residual_quantiles


def _probability_for_label(
    model: Pipeline,
    frame: pd.DataFrame,
    label: int,
) -> np.ndarray:
    probability = model.predict_proba(frame[FEATURE_COLUMNS])
    classes = list(model.named_steps["model"].classes_.astype(int))
    if label not in classes:
        return np.zeros(len(frame), dtype=float)
    return probability[:, classes.index(label)]


def _selective_predictions(
    event_probability: np.ndarray,
    up_probability: np.ndarray,
    event_threshold: float,
    direction_threshold: float,
) -> np.ndarray:
    confidence = np.maximum(up_probability, 1.0 - up_probability)
    active = (
        (event_probability >= float(event_threshold))
        & (confidence >= float(direction_threshold))
    )
    direction = np.where(up_probability >= 0.5, 1, -1)
    return np.where(active, direction, 0).astype(int)


def _selective_metrics(
    frame: pd.DataFrame,
    prediction: np.ndarray,
) -> dict[str, Any]:
    y_true = frame["target_direction"].astype(int).to_numpy()
    labels = CLASS_LABELS
    matrix = confusion_matrix(y_true, prediction, labels=labels)
    signal_mask = prediction != 0
    actual_move = y_true != 0
    correct = signal_mask & (prediction == y_true)
    up_mask = prediction == 1
    down_mask = prediction == -1
    matrix_dict = {
        CLASS_NAMES[actual]: {
            CLASS_NAMES[predicted]: int(matrix[row_index, column_index])
            for column_index, predicted in enumerate(labels)
        }
        for row_index, actual in enumerate(labels)
    }
    return {
        "rows": int(len(frame)),
        "accuracy": round(
            float(accuracy_score(y_true, prediction)),
            6,
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
                    average="macro",
                    zero_division=0,
                )
            ),
            6,
        ),
        "confusion_matrix": matrix_dict,
        "prediction_counts": {
            "down": int(down_mask.sum()),
            "observe": int((prediction == 0).sum()),
            "up": int(up_mask.sum()),
        },
        "signal_rows": int(signal_mask.sum()),
        "signal_coverage": _safe_ratio(int(signal_mask.sum()), len(frame)),
        "directional_precision": _safe_ratio(
            int(correct.sum()),
            int(signal_mask.sum()),
        ),
        "move_detection_precision": _safe_ratio(
            int((signal_mask & actual_move).sum()),
            int(signal_mask.sum()),
        ),
        "move_detection_recall": _safe_ratio(
            int((signal_mask & actual_move).sum()),
            int(actual_move.sum()),
        ),
        "up_precision": _safe_ratio(
            int((up_mask & (y_true == 1)).sum()),
            int(up_mask.sum()),
        ),
        "down_precision": _safe_ratio(
            int((down_mask & (y_true == -1)).sum()),
            int(down_mask.sum()),
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
        1,
    )
    up_probability = _probability_for_label(
        direction_model,
        validation,
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
        if float(item.get("signal_coverage") or 0.0) >= 0.10
        and int((item.get("prediction_counts") or {}).get("up", 0)) >= 25
        and int((item.get("prediction_counts") or {}).get("down", 0)) >= 25
    ]
    selected = max(
        eligible or candidates,
        key=lambda item: (
            float(item.get("directional_precision") or 0.0),
            float(item.get("macro_f1") or 0.0),
            float(item.get("balanced_accuracy") or 0.0),
            float(item.get("signal_coverage") or 0.0),
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


def _fit_bundle(
    training_frame: pd.DataFrame,
) -> dict[str, Any]:
    split = _date_split(training_frame)
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
        "target_event",
    )
    direction_c, direction_candidates = _select_classifier_c(
        train_direction,
        validation_direction,
        "target_direction",
    )
    regression_alpha, regression_candidates, residual_quantiles = (
        _select_regression_alpha(train, validation)
    )

    tuning_event = _new_classifier(event_c)
    tuning_event.fit(
        train[FEATURE_COLUMNS],
        train["target_event"].astype(int),
    )
    tuning_direction = _new_classifier(direction_c)
    tuning_direction.fit(
        train_direction[FEATURE_COLUMNS],
        train_direction["target_direction"].astype(int),
    )
    thresholds, threshold_candidates = _select_thresholds(
        tuning_event,
        tuning_direction,
        validation,
    )

    return {
        "split": split,
        "event_c": event_c,
        "direction_c": direction_c,
        "regression_alpha": regression_alpha,
        "event_candidates": event_candidates,
        "direction_candidates": direction_candidates,
        "regression_candidates": regression_candidates,
        "residual_quantiles": residual_quantiles,
        "thresholds": thresholds,
        "threshold_candidates": threshold_candidates,
    }


def _fit_final_models(
    frame: pd.DataFrame,
    bundle: dict[str, Any],
) -> dict[str, Pipeline]:
    event_model = _new_classifier(bundle["event_c"])
    event_model.fit(
        frame[FEATURE_COLUMNS],
        frame["target_event"].astype(int),
    )
    direction_frame = frame[frame["target_event"] == 1].copy()
    direction_model = _new_classifier(bundle["direction_c"])
    direction_model.fit(
        direction_frame[FEATURE_COLUMNS],
        direction_frame["target_direction"].astype(int),
    )
    regression_model = _new_regressor(bundle["regression_alpha"])
    regression_model.fit(
        frame[FEATURE_COLUMNS],
        frame["taiex_change_15m_points"],
    )
    return {
        "event": event_model,
        "direction": direction_model,
        "regression": regression_model,
    }


def _evaluate_models(
    models: dict[str, Pipeline],
    frame: pd.DataFrame,
    thresholds: dict[str, Any],
    residual_quantiles: dict[str, float],
) -> dict[str, Any]:
    event_probability = _probability_for_label(
        models["event"],
        frame,
        1,
    )
    up_probability = _probability_for_label(
        models["direction"],
        frame,
        1,
    )
    prediction = _selective_predictions(
        event_probability,
        up_probability,
        float(thresholds["event_probability_threshold"]),
        float(thresholds["direction_confidence_threshold"]),
    )
    point_prediction = models["regression"].predict(
        frame[FEATURE_COLUMNS]
    )
    actual_points = frame["taiex_change_15m_points"].to_numpy(dtype=float)
    regression = _regression_metrics(actual_points, point_prediction)
    zero_baseline_mae = float(np.mean(np.abs(actual_points)))
    previous_move = frame[
        "taiex_previous_change_15m_points"
    ].to_numpy(dtype=float)
    momentum_baseline_mae = float(
        mean_absolute_error(actual_points, previous_move)
    )
    regression["zero_change_baseline_mae_points"] = round(
        zero_baseline_mae,
        4,
    )
    regression["momentum_baseline_mae_points"] = round(
        momentum_baseline_mae,
        4,
    )
    regression["mae_gain_vs_zero_baseline_points"] = round(
        zero_baseline_mae - float(regression["mae_points"]),
        4,
    )
    lower = point_prediction + float(residual_quantiles["q10"])
    upper = point_prediction + float(residual_quantiles["q90"])
    regression["p10_p90_interval_coverage"] = _safe_ratio(
        int(((actual_points >= lower) & (actual_points <= upper)).sum()),
        len(actual_points),
    )
    regression["residual_quantiles_from_validation"] = residual_quantiles

    by_day: list[dict[str, Any]] = []
    for trade_date, day in frame.groupby("trade_date", sort=True):
        positions = frame.index.get_indexer(day.index)
        day_prediction = prediction[positions]
        day_points = point_prediction[positions]
        day_actual = day["taiex_change_15m_points"].to_numpy(dtype=float)
        by_day.append({
            "trade_date": pd.Timestamp(trade_date).strftime("%Y-%m-%d"),
            "rows": int(len(day)),
            "selective": _selective_metrics(day, day_prediction),
            "regression": _regression_metrics(day_actual, day_points),
        })

    return {
        "selective": _selective_metrics(frame, prediction),
        "regression": regression,
        "by_day": by_day,
    }


def train_market_prediction_model(
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """以相同 70/15/15 日期切分比較 V8 Lite，不影響 V7。"""
    started = time.perf_counter()
    try:
        start, end = _date_range(start_date, end_date)
    except Exception as exc:
        return {"ok": False, "message": str(exc), "version": MODEL_VERSION}

    cache_key = f"{start}:{end}"
    if not force and cache_key in _TRAIN_CACHE:
        result = dict(_TRAIN_CACHE[cache_key])
        result["cached"] = True
        return result

    rows, repository_status = load_market_prediction_rows_paginated(
        start,
        end,
        limit=50000,
    )
    if not repository_status.get("complete"):
        return {
            "ok": False,
            "message": "訓練資料讀取不完整",
            "version": MODEL_VERSION,
            "repository_status": repository_status,
        }
    frame = prepare_v8_training_frame(rows)
    if len(frame) < MIN_TRAINING_ROWS:
        return {
            "ok": False,
            "message": (
                f"可訓練資料不足：目前 {len(frame)} 筆，"
                f"至少需要 {MIN_TRAINING_ROWS} 筆"
            ),
            "version": MODEL_VERSION,
            "database_rows": len(rows),
        }

    try:
        bundle = _fit_bundle(frame)
    except Exception as exc:
        return {"ok": False, "message": str(exc), "version": MODEL_VERSION}

    train_plus_validation = pd.concat(
        [
            bundle["split"]["train"],
            bundle["split"]["validation"],
        ]
    ).sort_index()
    models = _fit_final_models(train_plus_validation, bundle)
    evaluation = _evaluate_models(
        models,
        bundle["split"]["test"],
        bundle["thresholds"],
        bundle["residual_quantiles"],
    )
    split = bundle["split"]
    result = {
        "ok": True,
        "message": "ok",
        "version": MODEL_VERSION,
        "feature_service_version": FEATURE_SERVICE_VERSION,
        "repository_version": REPOSITORY_VERSION,
        "mode": "v8_lite_two_stage_plus_point_regression",
        "cached": False,
        "start_date": start,
        "end_date": end,
        "database_rows": len(rows),
        "training_rows": len(frame),
        "trade_days": int(frame["trade_date"].nunique()),
        "feature_count": len(FEATURE_COLUMNS),
        "features": FEATURE_COLUMNS,
        "split": {
            "train_days": len(split["train_days"]),
            "validation_days": len(split["validation_days"]),
            "test_days": len(split["test_days"]),
            "train_first": split["train_days"][0].strftime("%Y-%m-%d"),
            "train_last": split["train_days"][-1].strftime("%Y-%m-%d"),
            "validation_first": split["validation_days"][0].strftime(
                "%Y-%m-%d"
            ),
            "validation_last": split["validation_days"][-1].strftime(
                "%Y-%m-%d"
            ),
            "test_first": split["test_days"][0].strftime("%Y-%m-%d"),
            "test_last": split["test_days"][-1].strftime("%Y-%m-%d"),
        },
        "selected": {
            "event_c": bundle["event_c"],
            "direction_c": bundle["direction_c"],
            "regression_alpha": bundle["regression_alpha"],
            "event_probability_threshold": bundle["thresholds"][
                "event_probability_threshold"
            ],
            "direction_confidence_threshold": bundle["thresholds"][
                "direction_confidence_threshold"
            ],
        },
        "evaluation": evaluation,
        "validation_candidates": {
            "event": bundle["event_candidates"],
            "direction": bundle["direction_candidates"],
            "regression": bundle["regression_candidates"],
            "thresholds": bundle["threshold_candidates"],
        },
        "deployment_ready": False,
        "shadow_mode_ready": False,
        "readiness_note": (
            "僅供與V7離線比較；尚未接管LINE或既有影子紀錄"
        ),
        "repository_status": repository_status,
        "seconds": round(time.perf_counter() - started, 3),
    }
    _TRAIN_CACHE[cache_key] = dict(result)
    return result


def evaluate_market_prediction_forward(
    training_start_date: str,
    training_cutoff: str,
    evaluation_start_date: str,
    evaluation_end_date: str,
    force: bool = False,
) -> dict[str, Any]:
    """凍結 cutoff 後評估分類與15分鐘點數，不使用前瞻資料調參。"""
    started = time.perf_counter()
    try:
        training_start = pd.Timestamp(training_start_date).normalize()
        cutoff = pd.Timestamp(training_cutoff).normalize()
        evaluation_start = pd.Timestamp(evaluation_start_date).normalize()
        evaluation_end = pd.Timestamp(evaluation_end_date).normalize()
    except Exception:
        return {
            "ok": False,
            "message": "日期格式必須是 YYYY-MM-DD",
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

    texts = [
        value.strftime("%Y-%m-%d")
        for value in (
            training_start,
            cutoff,
            evaluation_start,
            evaluation_end,
        )
    ]
    cache_key = ":".join(texts)
    if not force and cache_key in _FORWARD_CACHE:
        result = dict(_FORWARD_CACHE[cache_key])
        result["cached"] = True
        return result

    training_rows, training_status = load_market_prediction_rows_paginated(
        texts[0],
        texts[1],
        limit=50000,
    )
    evaluation_rows, evaluation_status = load_market_prediction_rows_paginated(
        texts[2],
        texts[3],
        limit=50000,
    )
    if not training_status.get("complete") or not evaluation_status.get(
        "complete"
    ):
        return {
            "ok": False,
            "message": "Supabase資料讀取不完整",
            "version": MODEL_VERSION,
            "training_repository_status": training_status,
            "evaluation_repository_status": evaluation_status,
        }

    training_frame = prepare_v8_training_frame(training_rows)
    evaluation_frame = prepare_v8_training_frame(evaluation_rows)
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

    try:
        bundle = _fit_bundle(training_frame)
    except Exception as exc:
        return {"ok": False, "message": str(exc), "version": MODEL_VERSION}
    models = _fit_final_models(training_frame, bundle)
    evaluation = _evaluate_models(
        models,
        evaluation_frame,
        bundle["thresholds"],
        bundle["residual_quantiles"],
    )

    result = {
        "ok": True,
        "message": "ok",
        "version": MODEL_VERSION,
        "feature_service_version": FEATURE_SERVICE_VERSION,
        "repository_version": REPOSITORY_VERSION,
        "mode": "v8_lite_frozen_forward_holdout",
        "cached": False,
        "feature_count": len(FEATURE_COLUMNS),
        "feature_window_minutes": 60,
        "prediction_horizon_minutes": 15,
        "neutral_threshold_points": int(NEUTRAL_THRESHOLD_POINTS),
        "label_definition": {
            "change": "TAIEX(t+15m) - TAIEX(t)",
            "down": "< -100 points",
            "observe": "confidence below selected thresholds",
            "flat_actual": "-100 to +100 points (inclusive)",
            "up": "> +100 points",
        },
        "leakage_guard": {
            "passed": True,
            "training_start_date": texts[0],
            "model_fitted_through": texts[1],
            "evaluation_start_date": texts[2],
            "evaluation_end_date": texts[3],
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
            "regression_alpha": bundle["regression_alpha"],
            "event_probability_threshold": bundle["thresholds"][
                "event_probability_threshold"
            ],
            "direction_confidence_threshold": bundle["thresholds"][
                "direction_confidence_threshold"
            ],
        },
        "release_status": "offline_comparison_only",
        "readiness_note": "先與同期間V7比較，不寫入既有影子預測表",
        "repository_status": {
            "training": training_status,
            "evaluation": evaluation_status,
        },
        "seconds": round(time.perf_counter() - started, 3),
    }
    _FORWARD_CACHE[cache_key] = dict(result)
    _debug(
        "forward",
        "| train_rows =", len(training_frame),
        "| eval_rows =", len(evaluation_frame),
        "| direction_precision =",
        (evaluation["selective"] or {}).get("directional_precision"),
        "| mae =",
        (evaluation["regression"] or {}).get("mae_points"),
        "| sec =", result["seconds"],
    )
    return result
