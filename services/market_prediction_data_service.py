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

from services.supabase_service import get_market_prediction_rows


MODEL_VERSION = "2026-07-21-v1-STRICT-15M-LOGISTIC"
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

    rows = get_market_prediction_rows(start, end, limit=50000)
    frame = _prepare_training_frame(rows)
    if len(frame) < MIN_TRAINING_ROWS:
        return {
            "ok": False,
            "message": f"可訓練資料不足：目前 {len(frame)} 筆，至少需要 {MIN_TRAINING_ROWS} 筆",
            "version": MODEL_VERSION,
            "database_rows": len(rows),
            "training_rows": len(frame),
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
        }

    train = split["train"]
    validation = split["validation"]
    test = split["test"]
    candidates: list[dict[str, Any]] = []
    best_model: Pipeline | None = None
    best_score: tuple[float, float] | None = None
    best_c = 0.3

    for c_value in (0.05, 0.1, 0.3, 1.0):
        model = _new_pipeline(c_value)
        model.fit(train[FEATURE_COLUMNS], train["target_direction"])
        metrics = _metrics(model, validation)
        candidates.append({"c": c_value, **metrics})
        score = (float(metrics["macro_f1"]), -float(metrics["log_loss"]))
        if best_score is None or score > best_score:
            best_score = score
            best_model = model
            best_c = c_value

    train_and_validation = pd.concat([train, validation]).sort_index()
    final_model = _new_pipeline(best_c)
    final_model.fit(
        train_and_validation[FEATURE_COLUMNS],
        train_and_validation["target_direction"],
    )
    test_metrics = _metrics(final_model, test)
    baseline = _majority_baseline(train_and_validation, test)

    test_macro_gain = float(test_metrics["macro_f1"]) - float(baseline["macro_f1"])
    deployment_ready = bool(
        len(split["test_days"]) >= 8
        and test_macro_gain >= 0.02
        and float(test_metrics["balanced_accuracy"]) >= 0.36
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
        "deployment_ready": deployment_ready,
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
        "| deployment_ready =", deployment_ready,
        "| sec =", report["seconds"],
    )
    return report
