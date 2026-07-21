from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    log_loss,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from services.market_prediction_repository_v4 import (
    REPOSITORY_VERSION,
    load_market_prediction_rows_paginated,
)


MODEL_VERSION = "2026-07-21-v4-WALK-FORWARD-NONOVERLAP"
TAIPEI_TZ = "Asia/Taipei"
CLASS_LABELS = [-1, 0, 1]
CLASS_NAMES = {-1: "down", 0: "flat", 1: "up"}
MIN_TRADE_DAYS = 50
MIN_TRAINING_ROWS = 8000

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

_MODEL_CACHE: dict[str, Any] = {}


def _debug(*args: Any) -> None:
    print("DEBUG market_prediction_model |", *args, flush=True)


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


def _prepare_training_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    required_raw = [
        "ts",
        "trade_date",
        "taiex_close",
        "txf_close",
        "txf_volume",
        "target_direction",
    ]
    if any(column not in frame.columns for column in required_raw):
        return pd.DataFrame()

    frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce", utc=True)
    frame = frame.dropna(subset=["ts"]).copy()
    frame["ts"] = frame["ts"].dt.tz_convert(TAIPEI_TZ).dt.tz_localize(None)
    frame = frame.sort_values("ts").drop_duplicates("ts", keep="last")
    frame = frame.set_index("ts")

    numeric = [
        "taiex_close",
        "txf_close",
        "txf_volume",
        "target_direction",
    ]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = frame.dropna(subset=numeric).copy()
    frame["trade_date"] = frame.index.normalize()
    grouped = frame.groupby("trade_date", sort=False)

    for minutes in (1, 3, 5, 10, 15):
        frame[f"taiex_return_{minutes}m"] = grouped["taiex_close"].pct_change(
            periods=minutes,
            fill_method=None,
        ) * 100.0
        frame[f"txf_return_{minutes}m"] = grouped["txf_close"].pct_change(
            periods=minutes,
            fill_method=None,
        ) * 100.0

    frame["basis"] = frame["txf_close"] - frame["taiex_close"]
    frame["basis_pct"] = frame["basis"] / frame["taiex_close"] * 100.0
    frame["basis_change_5m"] = grouped["basis"].diff(5)
    frame["taiex_volatility_15m"] = grouped["taiex_return_1m"].transform(
        lambda values: values.rolling(15, min_periods=10).std(ddof=0)
    )
    frame["txf_volatility_15m"] = grouped["txf_return_1m"].transform(
        lambda values: values.rolling(15, min_periods=10).std(ddof=0)
    )
    volume_mean_15m = grouped["txf_volume"].transform(
        lambda values: values.rolling(15, min_periods=10).mean()
    )
    frame["txf_volume_ratio_15m"] = (
        frame["txf_volume"] / volume_mean_15m.replace(0.0, np.nan)
    )

    session_minute = (frame.index.hour * 60 + frame.index.minute) - 540
    angle = 2.0 * math.pi * session_minute / 270.0
    frame["minute_sin"] = np.sin(angle)
    frame["minute_cos"] = np.cos(angle)

    frame = frame.replace([np.inf, -np.inf], np.nan)
    frame = frame.dropna(subset=FEATURE_COLUMNS + ["target_direction"]).copy()
    frame["target_direction"] = frame["target_direction"].astype(int)
    frame = frame[frame["target_direction"].isin(CLASS_LABELS)].copy()
    return frame


def _date_split(frame: pd.DataFrame) -> dict[str, Any]:
    days = sorted(pd.DatetimeIndex(frame["trade_date"].unique()))
    if len(days) < MIN_TRADE_DAYS:
        raise ValueError(
            f"完整交易日不足：目前 {len(days)} 天，至少需要 {MIN_TRADE_DAYS} 天"
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
        "validation": frame[frame["trade_date"].isin(validation_days)].copy(),
        "test": frame[frame["trade_date"].isin(test_days)].copy(),
        "train_days": train_days,
        "validation_days": validation_days,
        "test_days": test_days,
    }


def _new_pipeline(c_value: float) -> Pipeline:
    return Pipeline(
        steps=[
            ("scale", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=float(c_value),
                    class_weight="balanced",
                    solver="lbfgs",
                    max_iter=1000,
                    random_state=42,
                ),
            ),
        ]
    )


def _metrics(model: Pipeline, frame: pd.DataFrame) -> dict[str, Any]:
    x_values = frame[FEATURE_COLUMNS]
    y_true = frame["target_direction"].astype(int).to_numpy()
    y_pred = model.predict(x_values).astype(int)
    raw_probability = model.predict_proba(x_values)
    model_classes = list(model.named_steps["model"].classes_.astype(int))
    probability = np.zeros((len(frame), len(CLASS_LABELS)), dtype=float)
    for source_index, label in enumerate(model_classes):
        probability[:, CLASS_LABELS.index(label)] = raw_probability[:, source_index]

    matrix = confusion_matrix(y_true, y_pred, labels=CLASS_LABELS)
    return {
        "rows": int(len(frame)),
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 6),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, y_pred)), 6),
        "macro_f1": round(float(f1_score(y_true, y_pred, labels=CLASS_LABELS, average="macro", zero_division=0)), 6),
        "log_loss": round(float(log_loss(y_true, probability, labels=CLASS_LABELS)), 6),
        "prediction_counts": {
            CLASS_NAMES[label]: int((y_pred == label).sum()) for label in CLASS_LABELS
        },
        "confusion_matrix": {
            CLASS_NAMES[actual]: {
                CLASS_NAMES[predicted]: int(matrix[row_index, column_index])
                for column_index, predicted in enumerate(CLASS_LABELS)
            }
            for row_index, actual in enumerate(CLASS_LABELS)
        },
    }


def _label_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, Any]:
    matrix = confusion_matrix(y_true, y_pred, labels=CLASS_LABELS)
    return {
        "rows": int(len(y_true)),
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 6),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, y_pred)), 6),
        "macro_f1": round(float(f1_score(y_true, y_pred, labels=CLASS_LABELS, average="macro", zero_division=0)), 6),
        "confusion_matrix": {
            CLASS_NAMES[actual]: {
                CLASS_NAMES[predicted]: int(matrix[row_index, column_index])
                for column_index, predicted in enumerate(CLASS_LABELS)
            }
            for row_index, actual in enumerate(CLASS_LABELS)
        },
    }


def _non_overlapping(frame: pd.DataFrame, offset: int) -> pd.DataFrame:
    session_minute = (frame.index.hour * 60 + frame.index.minute) - 540
    return frame[(session_minute % 15) == int(offset)].copy()


def _aggregate_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"groups": len(reports)}
    for key in ("accuracy", "balanced_accuracy", "macro_f1"):
        values = [float(report[key]) for report in reports if key in report]
        result[f"{key}_mean"] = round(float(np.mean(values)), 6) if values else None
        result[f"{key}_std"] = round(float(np.std(values)), 6) if values else None
        result[f"{key}_min"] = round(float(np.min(values)), 6) if values else None
        result[f"{key}_max"] = round(float(np.max(values)), 6) if values else None
    result["rows_total"] = int(sum(int(report.get("rows", 0)) for report in reports))
    return result


def _rule_baseline(frame: pd.DataFrame, mode: str) -> dict[str, Any]:
    y_true = frame["target_direction"].astype(int).to_numpy()
    returns = frame["taiex_return_15m"].to_numpy(dtype=float)
    threshold = 0.08
    momentum = np.where(returns > threshold, 1, np.where(returns < -threshold, -1, 0))
    y_pred = -momentum if mode == "mean_reversion" else momentum
    return _label_metrics(y_true, y_pred.astype(int))


def _non_overlap_evaluation(
    model: Pipeline,
    frame: pd.DataFrame,
    majority_class: int,
) -> dict[str, Any]:
    model_reports: list[dict[str, Any]] = []
    majority_reports: list[dict[str, Any]] = []
    momentum_reports: list[dict[str, Any]] = []
    reversal_reports: list[dict[str, Any]] = []
    by_offset: list[dict[str, Any]] = []

    for offset in range(15):
        sample = _non_overlapping(frame, offset)
        if sample.empty:
            continue
        model_report = _metrics(model, sample)
        y_true = sample["target_direction"].astype(int).to_numpy()
        majority_pred = np.full(len(sample), int(majority_class), dtype=int)
        majority_report = _label_metrics(y_true, majority_pred)
        momentum_report = _rule_baseline(sample, "momentum")
        reversal_report = _rule_baseline(sample, "mean_reversion")
        model_reports.append(model_report)
        majority_reports.append(majority_report)
        momentum_reports.append(momentum_report)
        reversal_reports.append(reversal_report)
        by_offset.append({
            "offset": offset,
            "rows": len(sample),
            "accuracy": model_report["accuracy"],
            "balanced_accuracy": model_report["balanced_accuracy"],
            "macro_f1": model_report["macro_f1"],
        })

    return {
        "model": _aggregate_reports(model_reports),
        "majority": _aggregate_reports(majority_reports),
        "momentum": _aggregate_reports(momentum_reports),
        "mean_reversion": _aggregate_reports(reversal_reports),
        "by_offset": by_offset,
    }


def _select_c(train: pd.DataFrame, validation: pd.DataFrame) -> tuple[float, list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    best_score: tuple[float, float] | None = None
    best_c = 0.05
    for c_value in (0.05, 0.1, 0.3, 1.0):
        model = _new_pipeline(c_value)
        model.fit(train[FEATURE_COLUMNS], train["target_direction"])
        metrics = _metrics(model, validation)
        candidates.append({"c": c_value, **metrics})
        score = (float(metrics["macro_f1"]), -float(metrics["log_loss"]))
        if best_score is None or score > best_score:
            best_score = score
            best_c = c_value
    return best_c, candidates


def _walk_forward(frame: pd.DataFrame) -> dict[str, Any]:
    days = sorted(pd.DatetimeIndex(frame["trade_date"].unique()))
    initial_history_days = 30
    validation_days = 5
    test_block_days = 8
    folds: list[dict[str, Any]] = []
    model_aggregates: list[dict[str, Any]] = []
    majority_aggregates: list[dict[str, Any]] = []
    momentum_aggregates: list[dict[str, Any]] = []
    reversal_aggregates: list[dict[str, Any]] = []

    for test_start in range(initial_history_days, len(days), test_block_days):
        test_days = days[test_start : test_start + test_block_days]
        if len(test_days) < 5:
            continue
        history_days = days[:test_start]
        fit_days = history_days[:-validation_days]
        tune_days = history_days[-validation_days:]
        train = frame[frame["trade_date"].isin(fit_days)]
        validation = frame[frame["trade_date"].isin(tune_days)]
        test = frame[frame["trade_date"].isin(test_days)]
        selected_c, _ = _select_c(train, validation)
        history = pd.concat([train, validation]).sort_index()
        model = _new_pipeline(selected_c)
        model.fit(history[FEATURE_COLUMNS], history["target_direction"])
        majority_class = int(history["target_direction"].value_counts().idxmax())
        evaluation = _non_overlap_evaluation(model, test, majority_class)
        model_aggregates.append(evaluation["model"])
        majority_aggregates.append(evaluation["majority"])
        momentum_aggregates.append(evaluation["momentum"])
        reversal_aggregates.append(evaluation["mean_reversion"])
        folds.append({
            "history_days": len(history_days),
            "test_days": len(test_days),
            "test_first": test_days[0].strftime("%Y-%m-%d"),
            "test_last": test_days[-1].strftime("%Y-%m-%d"),
            "selected_c": selected_c,
            "model": evaluation["model"],
            "majority": evaluation["majority"],
            "momentum": evaluation["momentum"],
            "mean_reversion": evaluation["mean_reversion"],
        })

    def fold_mean(items: list[dict[str, Any]], key: str) -> float:
        values = [float(item[key]) for item in items if item.get(key) is not None]
        return round(float(np.mean(values)), 6) if values else 0.0

    return {
        "folds": folds,
        "fold_count": len(folds),
        "model_macro_f1_mean": fold_mean(model_aggregates, "macro_f1_mean"),
        "model_balanced_accuracy_mean": fold_mean(model_aggregates, "balanced_accuracy_mean"),
        "model_accuracy_mean": fold_mean(model_aggregates, "accuracy_mean"),
        "majority_macro_f1_mean": fold_mean(majority_aggregates, "macro_f1_mean"),
        "momentum_macro_f1_mean": fold_mean(momentum_aggregates, "macro_f1_mean"),
        "mean_reversion_macro_f1_mean": fold_mean(reversal_aggregates, "macro_f1_mean"),
    }


def _confidence_calibration(model: Pipeline, frame: pd.DataFrame) -> dict[str, Any]:
    y_true = frame["target_direction"].astype(int).to_numpy()
    y_pred = model.predict(frame[FEATURE_COLUMNS]).astype(int)
    probability = model.predict_proba(frame[FEATURE_COLUMNS])
    confidence = probability.max(axis=1)
    correct = (y_true == y_pred).astype(float)
    bins = [(0.0, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 0.60), (0.60, 1.01)]
    output: list[dict[str, Any]] = []
    ece = 0.0
    for low, high in bins:
        mask = (confidence >= low) & (confidence < high)
        count = int(mask.sum())
        if count == 0:
            continue
        avg_confidence = float(confidence[mask].mean())
        accuracy = float(correct[mask].mean())
        ece += count / len(frame) * abs(avg_confidence - accuracy)
        output.append({
            "range": f"{low:.2f}-{min(high, 1.0):.2f}",
            "rows": count,
            "average_confidence": round(avg_confidence, 6),
            "accuracy": round(accuracy, 6),
        })
    return {"rows": len(frame), "expected_calibration_error": round(ece, 6), "bins": output}


def _majority_baseline(train: pd.DataFrame, test: pd.DataFrame) -> dict[str, Any]:
    majority = int(train["target_direction"].value_counts().idxmax())
    y_true = test["target_direction"].astype(int).to_numpy()
    y_pred = np.full(len(test), majority, dtype=int)
    return {
        "class": CLASS_NAMES[majority],
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 6),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, y_pred)), 6),
        "macro_f1": round(float(f1_score(y_true, y_pred, labels=CLASS_LABELS, average="macro", zero_division=0)), 6),
    }


def _top_coefficients(model: Pipeline, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    estimator = model.named_steps["model"]
    result: dict[str, list[dict[str, Any]]] = {}
    for row_index, label in enumerate(estimator.classes_.astype(int)):
        weights = estimator.coef_[row_index]
        order = np.argsort(np.abs(weights))[::-1][:limit]
        result[CLASS_NAMES[label]] = [
            {
                "feature": FEATURE_COLUMNS[index],
                "coefficient": round(float(weights[index]), 6),
            }
            for index in order
        ]
    return result


def train_market_prediction_model(
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        start, end = _date_range(start_date, end_date)
    except Exception as exc:
        return {"ok": False, "message": str(exc), "version": MODEL_VERSION}

    cache_key = f"{start}:{end}"
    if not force and cache_key in _MODEL_CACHE:
        cached = dict(_MODEL_CACHE[cache_key]["report"])
        cached["cached"] = True
        return cached

    rows, repository_status = load_market_prediction_rows_paginated(
        start,
        end,
        limit=50000,
    )
    if not repository_status.get("ok"):
        return {
            "ok": False,
            "message": "Supabase 模型資料分頁讀取失敗",
            "version": MODEL_VERSION,
            "repository_version": REPOSITORY_VERSION,
            "repository_status": repository_status,
            "database_rows": 0,
            "training_rows": 0,
        }
    frame = _prepare_training_frame(rows)
    if len(frame) < MIN_TRAINING_ROWS:
        return {
            "ok": False,
            "message": f"可訓練資料不足：目前 {len(frame)} 筆，至少需要 {MIN_TRAINING_ROWS} 筆",
            "version": MODEL_VERSION,
            "database_rows": len(rows),
            "training_rows": len(frame),
            "repository_status": repository_status,
        }

    try:
        split = _date_split(frame)
    except Exception as exc:
        return {
            "ok": False,
            "message": str(exc),
            "version": MODEL_VERSION,
            "database_rows": len(rows),
            "training_rows": len(frame),
            "repository_status": repository_status,
        }

    train = split["train"]
    validation = split["validation"]
    test = split["test"]
    best_c, candidates = _select_c(train, validation)

    train_and_validation = pd.concat([train, validation]).sort_index()
    final_model = _new_pipeline(best_c)
    final_model.fit(
        train_and_validation[FEATURE_COLUMNS],
        train_and_validation["target_direction"],
    )
    test_metrics = _metrics(final_model, test)
    baseline = _majority_baseline(train_and_validation, test)
    majority_class = int(
        train_and_validation["target_direction"].value_counts().idxmax()
    )
    non_overlapping_test = _non_overlap_evaluation(
        final_model,
        test,
        majority_class,
    )
    walk_forward = _walk_forward(frame)
    calibration_sample = _non_overlapping(test, 0)
    calibration = _confidence_calibration(final_model, calibration_sample)

    test_macro_gain = float(test_metrics["macro_f1"]) - float(baseline["macro_f1"])
    strongest_rule_f1 = max(
        float(walk_forward.get("majority_macro_f1_mean", 0.0)),
        float(walk_forward.get("momentum_macro_f1_mean", 0.0)),
        float(walk_forward.get("mean_reversion_macro_f1_mean", 0.0)),
    )
    walk_forward_gain = (
        float(walk_forward.get("model_macro_f1_mean", 0.0)) - strongest_rule_f1
    )
    shadow_mode_ready = bool(
        int(walk_forward.get("fold_count", 0)) >= 3
        and walk_forward_gain >= 0.015
        and float(walk_forward.get("model_balanced_accuracy_mean", 0.0)) >= 0.36
    )
    production_ready = bool(
        shadow_mode_ready
        and int(frame["trade_date"].nunique()) >= 120
        and float(calibration.get("expected_calibration_error", 1.0)) <= 0.08
    )

    report = {
        "ok": True,
        "message": "ok",
        "version": MODEL_VERSION,
        "cached": False,
        "start_date": start,
        "end_date": end,
        "feature_window_minutes": 15,
        "prediction_horizon_minutes": 15,
        "repository_version": REPOSITORY_VERSION,
        "repository_status": repository_status,
        "features": FEATURE_COLUMNS,
        "database_rows": int(len(rows)),
        "training_rows": int(len(frame)),
        "trade_days": int(frame["trade_date"].nunique()),
        "split": {
            "train_days": len(split["train_days"]),
            "validation_days": len(split["validation_days"]),
            "test_days": len(split["test_days"]),
            "train_first": split["train_days"][0].strftime("%Y-%m-%d"),
            "train_last": split["train_days"][-1].strftime("%Y-%m-%d"),
            "validation_first": split["validation_days"][0].strftime("%Y-%m-%d"),
            "validation_last": split["validation_days"][-1].strftime("%Y-%m-%d"),
            "test_first": split["test_days"][0].strftime("%Y-%m-%d"),
            "test_last": split["test_days"][-1].strftime("%Y-%m-%d"),
        },
        "selected_c": best_c,
        "validation_candidates": candidates,
        "test": test_metrics,
        "baseline": baseline,
        "test_macro_f1_gain": round(test_macro_gain, 6),
        "non_overlapping_test": non_overlapping_test,
        "walk_forward": walk_forward,
        "walk_forward_macro_f1_gain_vs_strongest_rule": round(walk_forward_gain, 6),
        "calibration": calibration,
        "shadow_mode_ready": shadow_mode_ready,
        "production_ready": production_ready,
        "deployment_ready": production_ready,
        "readiness_note": (
            "可進行LINE影子測試，尚未達正式對外門檻"
            if shadow_mode_ready and not production_ready
            else "已達正式對外最低門檻"
            if production_ready
            else "尚未通過嚴格回測門檻"
        ),
        "top_coefficients": _top_coefficients(final_model),
        "seconds": round(time.perf_counter() - started, 3),
    }

    _MODEL_CACHE.clear()
    _MODEL_CACHE[cache_key] = {"model": final_model, "report": report}
    _debug(
        "trained",
        "| days =", report["trade_days"],
        "| rows =", report["training_rows"],
        "| test_macro_f1 =", test_metrics["macro_f1"],
        "| baseline_macro_f1 =", baseline["macro_f1"],
        "| shadow_ready =", shadow_mode_ready,
        "| production_ready =", production_ready,
        "| sec =", report["seconds"],
    )
    return report
