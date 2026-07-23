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

from services.market_prediction_repository_v7 import (
    REPOSITORY_VERSION,
    load_market_prediction_rows_paginated,
)


MODEL_VERSION = "2026-07-23-v7-TWO-STAGE-SELECTIVE-100PT"
TAIPEI_TZ = "Asia/Taipei"
CLASS_LABELS = [-1, 0, 1]
CLASS_NAMES = {-1: "down", 0: "flat", 1: "up"}
MIN_TRADE_DAYS = 50
MIN_TRAINING_ROWS = 8000
NEUTRAL_THRESHOLD_POINTS = 100.0

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
_FORWARD_CACHE: dict[str, Any] = {}


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
    ]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = frame.dropna(subset=numeric).copy()
    frame["trade_date"] = frame.index.normalize()
    grouped = frame.groupby("trade_date", sort=False)

    # V6 不沿用資料表內舊的 0.08% 標籤，直接由大盤點數重算答案：
    # 上漲 > +100、盤整 -100~+100、下跌 < -100。
    frame["taiex_change_15m_points"] = (
        grouped["taiex_close"].shift(-15) - frame["taiex_close"]
    )
    target_change = frame["taiex_change_15m_points"]
    frame["target_direction"] = np.where(
        target_change.isna(),
        np.nan,
        np.where(
            target_change > NEUTRAL_THRESHOLD_POINTS,
            1,
            np.where(
                target_change < -NEUTRAL_THRESHOLD_POINTS,
                -1,
                0,
            ),
        ),
    )
    frame["target_event"] = np.where(
        frame["target_direction"].isna(),
        np.nan,
        (frame["target_direction"] != 0).astype(float),
    )

    for minutes in (1, 3, 5, 10, 15):
        frame[f"taiex_return_{minutes}m"] = grouped["taiex_close"].pct_change(
            periods=minutes,
            fill_method=None,
        ) * 100.0
        frame[f"txf_return_{minutes}m"] = grouped["txf_close"].pct_change(
            periods=minutes,
            fill_method=None,
        ) * 100.0

    frame["taiex_previous_change_15m_points"] = (
        frame["taiex_close"] - grouped["taiex_close"].shift(15)
    )

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
    previous_change = frame["taiex_previous_change_15m_points"].to_numpy(
        dtype=float
    )
    momentum = np.where(
        previous_change > NEUTRAL_THRESHOLD_POINTS,
        1,
        np.where(
            previous_change < -NEUTRAL_THRESHOLD_POINTS,
            -1,
            0,
        ),
    )
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
        "neutral_threshold_type": "fixed_points",
        "neutral_threshold_points": int(NEUTRAL_THRESHOLD_POINTS),
        "label_definition": {
            "change": "TAIEX(t+15m) - TAIEX(t)",
            "down": "< -100 points",
            "flat": "-100 to +100 points (inclusive)",
            "up": "> +100 points",
        },
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


def _class_recall_from_confusion(
    matrix: dict[str, dict[str, int]],
) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for class_name in ("down", "flat", "up"):
        row = matrix.get(class_name, {})
        total = sum(int(value or 0) for value in row.values())
        correct = int(row.get(class_name, 0) or 0)
        result[class_name] = (
            round(float(correct / total), 6) if total > 0 else None
        )
    return result


def _forward_day_reports(
    model: Pipeline,
    frame: pd.DataFrame,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for trade_date, day_frame in frame.groupby("trade_date", sort=True):
        metrics = _metrics(model, day_frame)
        reports.append(
            {
                "trade_date": pd.Timestamp(trade_date).strftime("%Y-%m-%d"),
                "rows": metrics["rows"],
                "accuracy": metrics["accuracy"],
                "balanced_accuracy": metrics["balanced_accuracy"],
                "macro_f1": metrics["macro_f1"],
                "prediction_counts": metrics["prediction_counts"],
            }
        )
    return reports


def evaluate_market_prediction_forward_one_stage(
    training_start_date: str,
    training_cutoff: str,
    evaluation_start_date: str,
    evaluation_end_date: str,
    force: bool = False,
) -> dict[str, Any]:
    """以 cutoff 前資料定型模型，之後區間只評估，禁止資料洩漏。"""
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

    if any(pd.isna(value) for value in (
        training_start,
        cutoff,
        evaluation_start,
        evaluation_end,
    )):
        return {
            "ok": False,
            "message": "日期格式必須是 YYYY-MM-DD",
            "version": MODEL_VERSION,
        }
    if cutoff < training_start:
        return {
            "ok": False,
            "message": "training_cutoff 不可早於 training_start_date",
            "version": MODEL_VERSION,
        }
    if evaluation_end < evaluation_start:
        return {
            "ok": False,
            "message": "evaluation_end_date 不可早於 evaluation_start_date",
            "version": MODEL_VERSION,
        }
    if evaluation_start <= cutoff:
        return {
            "ok": False,
            "message": "前瞻區間必須晚於 training_cutoff，避免資料洩漏",
            "version": MODEL_VERSION,
        }

    training_start_text = training_start.strftime("%Y-%m-%d")
    cutoff_text = cutoff.strftime("%Y-%m-%d")
    evaluation_start_text = evaluation_start.strftime("%Y-%m-%d")
    evaluation_end_text = evaluation_end.strftime("%Y-%m-%d")
    cache_key = ":".join(
        (
            training_start_text,
            cutoff_text,
            evaluation_start_text,
            evaluation_end_text,
        )
    )
    if not force and cache_key in _FORWARD_CACHE:
        cached = dict(_FORWARD_CACHE[cache_key])
        cached["cached"] = True
        return cached

    training_rows, training_repository_status = (
        load_market_prediction_rows_paginated(
            training_start_text,
            cutoff_text,
            limit=50000,
        )
    )
    if not training_repository_status.get("ok"):
        return {
            "ok": False,
            "message": "Supabase 歷史訓練資料讀取失敗",
            "version": MODEL_VERSION,
            "repository_version": REPOSITORY_VERSION,
            "training_repository_status": training_repository_status,
        }

    evaluation_rows, evaluation_repository_status = (
        load_market_prediction_rows_paginated(
            evaluation_start_text,
            evaluation_end_text,
            limit=50000,
        )
    )
    if not evaluation_repository_status.get("ok"):
        return {
            "ok": False,
            "message": "Supabase 前瞻評估資料讀取失敗",
            "version": MODEL_VERSION,
            "repository_version": REPOSITORY_VERSION,
            "evaluation_repository_status": evaluation_repository_status,
        }

    training_frame = _prepare_training_frame(training_rows)
    evaluation_frame = _prepare_training_frame(evaluation_rows)
    if len(training_frame) < MIN_TRAINING_ROWS:
        return {
            "ok": False,
            "message": (
                f"歷史可訓練資料不足：目前 {len(training_frame)} 筆，"
                f"至少需要 {MIN_TRAINING_ROWS} 筆"
            ),
            "version": MODEL_VERSION,
            "training_database_rows": len(training_rows),
            "training_rows": len(training_frame),
        }
    if evaluation_frame.empty:
        return {
            "ok": False,
            "message": "前瞻區間沒有可評估資料",
            "version": MODEL_VERSION,
            "evaluation_database_rows": len(evaluation_rows),
            "evaluation_rows": 0,
        }

    try:
        historical_split = _date_split(training_frame)
    except Exception as exc:
        return {
            "ok": False,
            "message": str(exc),
            "version": MODEL_VERSION,
            "training_database_rows": len(training_rows),
            "training_rows": len(training_frame),
        }

    best_c, candidates = _select_c(
        historical_split["train"],
        historical_split["validation"],
    )
    frozen_model = _new_pipeline(best_c)
    frozen_model.fit(
        training_frame[FEATURE_COLUMNS],
        training_frame["target_direction"],
    )
    majority_class = int(
        training_frame["target_direction"].value_counts().idxmax()
    )

    forward_metrics = _metrics(frozen_model, evaluation_frame)
    majority = _majority_baseline(training_frame, evaluation_frame)
    momentum = _rule_baseline(evaluation_frame, "momentum")
    mean_reversion = _rule_baseline(evaluation_frame, "mean_reversion")
    non_overlapping = _non_overlap_evaluation(
        frozen_model,
        evaluation_frame,
        majority_class,
    )
    calibration_frame = _non_overlapping(evaluation_frame, 0)
    calibration = (
        _confidence_calibration(frozen_model, calibration_frame)
        if not calibration_frame.empty
        else {"rows": 0, "expected_calibration_error": None, "bins": []}
    )
    class_recall = _class_recall_from_confusion(
        forward_metrics["confusion_matrix"]
    )

    strongest_rule_f1 = max(
        float(non_overlapping["majority"].get("macro_f1_mean") or 0.0),
        float(non_overlapping["momentum"].get("macro_f1_mean") or 0.0),
        float(non_overlapping["mean_reversion"].get("macro_f1_mean") or 0.0),
    )
    model_f1 = float(
        non_overlapping["model"].get("macro_f1_mean") or 0.0
    )
    forward_days = int(evaluation_frame["trade_date"].nunique())
    preliminary = forward_days < 20

    report = {
        "ok": True,
        "message": "ok",
        "version": MODEL_VERSION,
        "cached": False,
        "mode": "frozen_forward_holdout",
        "feature_window_minutes": 15,
        "prediction_horizon_minutes": 15,
        "neutral_threshold_type": "fixed_points",
        "neutral_threshold_points": int(NEUTRAL_THRESHOLD_POINTS),
        "label_definition": {
            "change": "TAIEX(t+15m) - TAIEX(t)",
            "down": "< -100 points",
            "flat": "-100 to +100 points (inclusive)",
            "up": "> +100 points",
        },
        "repository_version": REPOSITORY_VERSION,
        "leakage_guard": {
            "passed": True,
            "training_start_date": training_start_text,
            "model_fitted_through": cutoff_text,
            "evaluation_start_date": evaluation_start_text,
            "evaluation_end_date": evaluation_end_text,
            "evaluation_rows_used_for_fit": 0,
        },
        "training": {
            "database_rows": int(len(training_rows)),
            "rows": int(len(training_frame)),
            "trade_days": int(training_frame["trade_date"].nunique()),
            "selected_c": best_c,
            "repository_status": training_repository_status,
        },
        "evaluation": {
            "database_rows": int(len(evaluation_rows)),
            "rows": int(len(evaluation_frame)),
            "trade_days": forward_days,
            "repository_status": evaluation_repository_status,
            **forward_metrics,
            "class_recall": class_recall,
            "by_day": _forward_day_reports(frozen_model, evaluation_frame),
        },
        "baselines": {
            "majority": majority,
            "momentum": momentum,
            "mean_reversion": mean_reversion,
        },
        "non_overlapping_15m": non_overlapping,
        "forward_macro_f1_gain_vs_strongest_rule": round(
            model_f1 - strongest_rule_f1,
            6,
        ),
        "calibration": calibration,
        "validation_candidates": candidates,
        "preliminary": preliminary,
        "minimum_recommended_forward_days": 20,
        "readiness_note": (
            "前瞻樣本未滿20個交易日，目前僅供觀察"
            if preliminary
            else "前瞻樣本已滿20個交易日，可評估是否進入LINE影子測試"
        ),
        "seconds": round(time.perf_counter() - started, 3),
    }

    _FORWARD_CACHE.clear()
    _FORWARD_CACHE[cache_key] = report
    _debug(
        "forward_evaluated",
        "| fitted_through =", cutoff_text,
        "| evaluation =", f"{evaluation_start_text}:{evaluation_end_text}",
        "| days =", forward_days,
        "| rows =", len(evaluation_frame),
        "| balanced_accuracy =", forward_metrics["balanced_accuracy"],
        "| macro_f1 =", forward_metrics["macro_f1"],
        "| sec =", report["seconds"],
    )
    return report


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
            float(f1_score(y_true, y_pred, average="macro", zero_division=0)),
            6,
        ),
        "log_loss": round(
            float(log_loss(y_true, probability, labels=labels)),
            6,
        ),
    }


def _select_binary_c(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    target_column: str,
) -> tuple[float, list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    best_score: tuple[float, float, float] | None = None
    best_c = 0.05
    for c_value in (0.05, 0.1, 0.3, 1.0):
        model = _new_pipeline(c_value)
        model.fit(train[FEATURE_COLUMNS], train[target_column].astype(int))
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
    direction_confidence_threshold: float,
) -> np.ndarray:
    direction_confidence = np.maximum(
        up_probability,
        1.0 - up_probability,
    )
    signal = (
        (event_probability >= float(event_threshold))
        & (
            direction_confidence
            >= float(direction_confidence_threshold)
        )
    )
    direction = np.where(up_probability >= 0.5, 1, -1)
    return np.where(signal, direction, 0).astype(int)


def _selective_metrics(
    frame: pd.DataFrame,
    y_pred: np.ndarray,
) -> dict[str, Any]:
    y_true = frame["target_direction"].astype(int).to_numpy()
    core = _label_metrics(y_true, y_pred)
    signal_mask = y_pred != 0
    actual_move_mask = y_true != 0
    correct_direction_mask = signal_mask & (y_pred == y_true)
    up_mask = y_pred == 1
    down_mask = y_pred == -1

    def safe_ratio(numerator: int, denominator: int) -> float | None:
        if denominator <= 0:
            return None
        return round(float(numerator / denominator), 6)

    signal_rows = int(signal_mask.sum())
    actual_move_rows = int(actual_move_mask.sum())
    up_signals = int(up_mask.sum())
    down_signals = int(down_mask.sum())
    result = {
        **core,
        "prediction_counts": {
            "down": down_signals,
            "observe": int((y_pred == 0).sum()),
            "up": up_signals,
        },
        "signal_rows": signal_rows,
        "observe_rows": int((y_pred == 0).sum()),
        "signal_coverage": safe_ratio(signal_rows, len(frame)),
        "directional_precision": safe_ratio(
            int(correct_direction_mask.sum()),
            signal_rows,
        ),
        "move_detection_precision": safe_ratio(
            int((signal_mask & actual_move_mask).sum()),
            signal_rows,
        ),
        "move_detection_recall": safe_ratio(
            int((signal_mask & actual_move_mask).sum()),
            actual_move_rows,
        ),
        "exact_direction_recall": safe_ratio(
            int(correct_direction_mask.sum()),
            actual_move_rows,
        ),
        "up_precision": safe_ratio(
            int((up_mask & (y_true == 1)).sum()),
            up_signals,
        ),
        "down_precision": safe_ratio(
            int((down_mask & (y_true == -1)).sum()),
            down_signals,
        ),
        "class_recall": _class_recall_from_confusion(
            core["confusion_matrix"]
        ),
    }
    return result


def _threshold_candidates(
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
            metrics = _selective_metrics(validation, prediction)
            candidates.append({
                "event_probability_threshold": event_threshold,
                "direction_confidence_threshold": direction_threshold,
                **metrics,
            })

    eligible = [
        item
        for item in candidates
        if float(item.get("signal_coverage") or 0.0) >= 0.10
        and int((item.get("prediction_counts") or {}).get("up", 0)) >= 25
        and int((item.get("prediction_counts") or {}).get("down", 0)) >= 25
    ]
    pool = eligible or candidates
    selected = max(
        pool,
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


def _event_calibration(
    event_probability: np.ndarray,
    y_event: np.ndarray,
) -> dict[str, Any]:
    bins = (
        (0.0, 0.30),
        (0.30, 0.40),
        (0.40, 0.50),
        (0.50, 0.60),
        (0.60, 0.70),
        (0.70, 1.01),
    )
    output: list[dict[str, Any]] = []
    ece = 0.0
    total = len(y_event)
    for low, high in bins:
        mask = (
            (event_probability >= low)
            & (event_probability < high)
        )
        count = int(mask.sum())
        if count == 0:
            continue
        average_probability = float(event_probability[mask].mean())
        actual_rate = float(y_event[mask].mean())
        ece += count / total * abs(average_probability - actual_rate)
        output.append({
            "range": f"{low:.2f}-{min(high, 1.0):.2f}",
            "rows": count,
            "average_event_probability": round(
                average_probability,
                6,
            ),
            "actual_event_rate": round(actual_rate, 6),
        })
    return {
        "rows": total,
        "expected_calibration_error": round(float(ece), 6),
        "bins": output,
    }


def _selective_day_reports(
    frame: pd.DataFrame,
    prediction: np.ndarray,
) -> list[dict[str, Any]]:
    work = frame.copy()
    work["_selective_prediction"] = prediction
    reports: list[dict[str, Any]] = []
    for trade_date, day_frame in work.groupby("trade_date", sort=True):
        metrics = _selective_metrics(
            day_frame,
            day_frame["_selective_prediction"].to_numpy(dtype=int),
        )
        reports.append({
            "trade_date": pd.Timestamp(trade_date).strftime("%Y-%m-%d"),
            "rows": metrics["rows"],
            "accuracy": metrics["accuracy"],
            "balanced_accuracy": metrics["balanced_accuracy"],
            "macro_f1": metrics["macro_f1"],
            "signal_rows": metrics["signal_rows"],
            "signal_coverage": metrics["signal_coverage"],
            "directional_precision": metrics["directional_precision"],
            "up_precision": metrics["up_precision"],
            "down_precision": metrics["down_precision"],
        })
    return reports


def _selective_non_overlap(
    frame: pd.DataFrame,
    prediction: np.ndarray,
) -> dict[str, Any]:
    work = frame.copy()
    work["_selective_prediction"] = prediction
    reports: list[dict[str, Any]] = []
    by_offset: list[dict[str, Any]] = []
    for offset in range(15):
        sample = _non_overlapping(work, offset)
        if sample.empty:
            continue
        metrics = _selective_metrics(
            sample,
            sample["_selective_prediction"].to_numpy(dtype=int),
        )
        reports.append(metrics)
        by_offset.append({
            "offset": offset,
            "rows": metrics["rows"],
            "accuracy": metrics["accuracy"],
            "balanced_accuracy": metrics["balanced_accuracy"],
            "macro_f1": metrics["macro_f1"],
            "signal_coverage": metrics["signal_coverage"],
            "directional_precision": metrics["directional_precision"],
        })

    aggregate = _aggregate_reports(reports)
    for key in (
        "signal_coverage",
        "directional_precision",
        "move_detection_precision",
        "move_detection_recall",
        "exact_direction_recall",
    ):
        values = [
            float(report[key])
            for report in reports
            if report.get(key) is not None
        ]
        aggregate[f"{key}_mean"] = (
            round(float(np.mean(values)), 6) if values else None
        )
    return {"model": aggregate, "by_offset": by_offset}


def evaluate_market_prediction_forward(
    training_start_date: str,
    training_cutoff: str,
    evaluation_start_date: str,
    evaluation_end_date: str,
    force: bool = False,
) -> dict[str, Any]:
    """V7兩階段：突破100點機率不足時輸出觀望，不強迫猜方向。"""
    started = time.perf_counter()
    try:
        training_start = pd.Timestamp(training_start_date).normalize()
        cutoff = pd.Timestamp(training_cutoff).normalize()
        evaluation_start = pd.Timestamp(
            evaluation_start_date
        ).normalize()
        evaluation_end = pd.Timestamp(evaluation_end_date).normalize()
    except Exception:
        return {
            "ok": False,
            "message": "日期格式必須是 YYYY-MM-DD",
            "version": MODEL_VERSION,
        }
    if any(pd.isna(value) for value in (
        training_start,
        cutoff,
        evaluation_start,
        evaluation_end,
    )):
        return {
            "ok": False,
            "message": "日期格式必須是 YYYY-MM-DD",
            "version": MODEL_VERSION,
        }
    if cutoff < training_start:
        return {
            "ok": False,
            "message": "training_cutoff 不可早於 training_start_date",
            "version": MODEL_VERSION,
        }
    if evaluation_end < evaluation_start:
        return {
            "ok": False,
            "message": "evaluation_end_date 不可早於 evaluation_start_date",
            "version": MODEL_VERSION,
        }
    if evaluation_start <= cutoff:
        return {
            "ok": False,
            "message": "前瞻區間必須晚於 training_cutoff，避免資料洩漏",
            "version": MODEL_VERSION,
        }

    training_start_text = training_start.strftime("%Y-%m-%d")
    cutoff_text = cutoff.strftime("%Y-%m-%d")
    evaluation_start_text = evaluation_start.strftime("%Y-%m-%d")
    evaluation_end_text = evaluation_end.strftime("%Y-%m-%d")
    cache_key = "v7:" + ":".join((
        training_start_text,
        cutoff_text,
        evaluation_start_text,
        evaluation_end_text,
    ))
    if not force and cache_key in _FORWARD_CACHE:
        cached = dict(_FORWARD_CACHE[cache_key])
        cached["cached"] = True
        return cached

    training_rows, training_repository_status = (
        load_market_prediction_rows_paginated(
            training_start_text,
            cutoff_text,
            limit=50000,
        )
    )
    evaluation_rows, evaluation_repository_status = (
        load_market_prediction_rows_paginated(
            evaluation_start_text,
            evaluation_end_text,
            limit=50000,
        )
    )
    if not training_repository_status.get("ok"):
        return {
            "ok": False,
            "message": "Supabase 歷史訓練資料讀取失敗",
            "version": MODEL_VERSION,
            "repository_version": REPOSITORY_VERSION,
            "training_repository_status": training_repository_status,
        }
    if not evaluation_repository_status.get("ok"):
        return {
            "ok": False,
            "message": "Supabase 前瞻評估資料讀取失敗",
            "version": MODEL_VERSION,
            "repository_version": REPOSITORY_VERSION,
            "evaluation_repository_status": evaluation_repository_status,
        }

    training_frame = _prepare_training_frame(training_rows)
    evaluation_frame = _prepare_training_frame(evaluation_rows)
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
        historical_split = _date_split(training_frame)
    except Exception as exc:
        return {
            "ok": False,
            "message": str(exc),
            "version": MODEL_VERSION,
        }
    train = historical_split["train"]
    validation = historical_split["validation"]
    train_event = train.copy()
    validation_event = validation.copy()
    train_direction = train[train["target_event"] == 1].copy()
    validation_direction = validation[
        validation["target_event"] == 1
    ].copy()
    if min(len(train_direction), len(validation_direction)) < 100:
        return {
            "ok": False,
            "message": "突破100點的方向樣本不足",
            "version": MODEL_VERSION,
            "train_direction_rows": len(train_direction),
            "validation_direction_rows": len(validation_direction),
        }

    event_c, event_candidates = _select_binary_c(
        train_event,
        validation_event,
        "target_event",
    )
    direction_c, direction_candidates = _select_binary_c(
        train_direction,
        validation_direction,
        "target_direction",
    )
    tuning_event_model = _new_pipeline(event_c)
    tuning_event_model.fit(
        train_event[FEATURE_COLUMNS],
        train_event["target_event"].astype(int),
    )
    tuning_direction_model = _new_pipeline(direction_c)
    tuning_direction_model.fit(
        train_direction[FEATURE_COLUMNS],
        train_direction["target_direction"].astype(int),
    )
    selected_thresholds, threshold_candidates = _threshold_candidates(
        tuning_event_model,
        tuning_direction_model,
        validation,
    )

    final_event_model = _new_pipeline(event_c)
    final_event_model.fit(
        training_frame[FEATURE_COLUMNS],
        training_frame["target_event"].astype(int),
    )
    historical_direction = training_frame[
        training_frame["target_event"] == 1
    ].copy()
    final_direction_model = _new_pipeline(direction_c)
    final_direction_model.fit(
        historical_direction[FEATURE_COLUMNS],
        historical_direction["target_direction"].astype(int),
    )

    event_probability = _probability_for_label(
        final_event_model,
        evaluation_frame,
        1,
    )
    up_probability = _probability_for_label(
        final_direction_model,
        evaluation_frame,
        1,
    )
    prediction = _selective_predictions(
        event_probability,
        up_probability,
        float(selected_thresholds["event_probability_threshold"]),
        float(selected_thresholds["direction_confidence_threshold"]),
    )
    evaluation_metrics = _selective_metrics(
        evaluation_frame,
        prediction,
    )
    non_overlapping = _selective_non_overlap(
        evaluation_frame,
        prediction,
    )
    majority = _majority_baseline(training_frame, evaluation_frame)
    momentum = _rule_baseline(evaluation_frame, "momentum")
    mean_reversion = _rule_baseline(
        evaluation_frame,
        "mean_reversion",
    )
    strongest_rule_f1 = max(
        float(majority["macro_f1"]),
        float(momentum["macro_f1"]),
        float(mean_reversion["macro_f1"]),
    )
    forward_days = int(evaluation_frame["trade_date"].nunique())
    signal_ready = bool(
        forward_days >= 20
        and float(evaluation_metrics.get("directional_precision") or 0.0)
        >= 0.50
        and float(evaluation_metrics.get("signal_coverage") or 0.0)
        >= 0.05
    )

    report = {
        "ok": True,
        "message": "ok",
        "version": MODEL_VERSION,
        "cached": False,
        "mode": "two_stage_selective_forward_holdout",
        "feature_window_minutes": 15,
        "prediction_horizon_minutes": 15,
        "neutral_threshold_type": "fixed_points",
        "neutral_threshold_points": int(NEUTRAL_THRESHOLD_POINTS),
        "label_definition": {
            "change": "TAIEX(t+15m) - TAIEX(t)",
            "down": "< -100 points",
            "observe": "signal confidence below selected thresholds",
            "flat_actual": "-100 to +100 points (inclusive)",
            "up": "> +100 points",
        },
        "repository_version": REPOSITORY_VERSION,
        "leakage_guard": {
            "passed": True,
            "training_start_date": training_start_text,
            "model_fitted_through": cutoff_text,
            "thresholds_tuned_before": evaluation_start_text,
            "evaluation_start_date": evaluation_start_text,
            "evaluation_end_date": evaluation_end_text,
            "evaluation_rows_used_for_fit": 0,
            "evaluation_rows_used_for_threshold_selection": 0,
        },
        "training": {
            "database_rows": len(training_rows),
            "rows": len(training_frame),
            "trade_days": int(training_frame["trade_date"].nunique()),
            "event_rows": int(training_frame["target_event"].sum()),
            "event_model_c": event_c,
            "direction_model_c": direction_c,
            "repository_status": training_repository_status,
        },
        "selected_signal_thresholds": {
            "event_probability_threshold": selected_thresholds[
                "event_probability_threshold"
            ],
            "direction_confidence_threshold": selected_thresholds[
                "direction_confidence_threshold"
            ],
            "selection_objective": (
                "maximize validation directional precision with at least "
                "10% coverage and at least 25 up/down signals"
            ),
            "validation_signal_coverage": selected_thresholds[
                "signal_coverage"
            ],
            "validation_directional_precision": selected_thresholds[
                "directional_precision"
            ],
        },
        "evaluation": {
            "database_rows": len(evaluation_rows),
            "trade_days": forward_days,
            "repository_status": evaluation_repository_status,
            **evaluation_metrics,
            "by_day": _selective_day_reports(
                evaluation_frame,
                prediction,
            ),
        },
        "baselines": {
            "majority": majority,
            "momentum": momentum,
            "mean_reversion": mean_reversion,
        },
        "non_overlapping_15m": non_overlapping,
        "forward_macro_f1_gain_vs_strongest_rule": round(
            float(evaluation_metrics["macro_f1"]) - strongest_rule_f1,
            6,
        ),
        "event_calibration": _event_calibration(
            event_probability,
            evaluation_frame["target_event"].astype(int).to_numpy(),
        ),
        "validation": {
            "event_model_candidates": event_candidates,
            "direction_model_candidates": direction_candidates,
            "top_signal_threshold_candidates": threshold_candidates,
        },
        "preliminary": forward_days < 20,
        "minimum_recommended_forward_days": 20,
        "signal_ready": signal_ready,
        "readiness_note": (
            "前瞻樣本未滿20個交易日，目前僅供觀察"
            if forward_days < 20
            else "方向訊號精準率或覆蓋率尚未達LINE影子測試門檻"
            if not signal_ready
            else "可進行LINE影子測試，仍不對一般使用者公開"
        ),
        "seconds": round(time.perf_counter() - started, 3),
    }
    _FORWARD_CACHE.clear()
    _FORWARD_CACHE[cache_key] = report
    _debug(
        "selective_forward_evaluated",
        "| fitted_through =", cutoff_text,
        "| days =", forward_days,
        "| rows =", len(evaluation_frame),
        "| coverage =", evaluation_metrics["signal_coverage"],
        "| directional_precision =",
        evaluation_metrics["directional_precision"],
        "| sec =", report["seconds"],
    )
    return report
