from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from services.market_prediction_features_v8_lite import (
    FEATURE_COLUMNS as V8_ALL_FEATURE_COLUMNS,
    FEATURE_SERVICE_VERSION as V8_FEATURE_SERVICE_VERSION,
    NEUTRAL_THRESHOLD_POINTS,
    prepare_v8_training_frame,
)


FEATURE_SERVICE_VERSION = "2026-07-27-v8.1-FAIR-WARMUP-ABLATION"
CORE_WARMUP_MINUTES = 15

# 與 V7 相同的 17 個基礎特徵。這些欄位必須真的具有 15 分鐘歷史，
# 不可把盤初缺值補成 0 後拿去訓練或評估。
BASE_FEATURE_COLUMNS = [
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

CANDLE_FEATURE_COLUMNS = [
    "taiex_range_pct",
    "taiex_body_pct",
    "taiex_upper_wick_pct",
    "taiex_lower_wick_pct",
    "taiex_close_position",
    "txf_range_pct",
    "txf_body_pct",
    "txf_close_position",
    "taiex_atr14_pct",
    "txf_atr14_pct",
]

TREND_FEATURE_COLUMNS = [
    "taiex_return_30m",
    "taiex_return_60m",
    "txf_return_30m",
    "txf_return_60m",
    "basis_change_1m",
    "basis_change_15m",
    "taiex_volatility_5m",
    "taiex_volatility_30m",
    "txf_volatility_5m",
    "txf_volatility_30m",
    "taiex_previous_change_15m_points",
    "history_30m_ready",
    "history_60m_ready",
]

VOLUME_FEATURE_COLUMNS = [
    "txf_volume_ratio_5m",
    "txf_volume_ratio_30m",
    "txf_volume_zscore_30m",
]

POSITION_FEATURE_COLUMNS = [
    "taiex_distance_day_high_pct",
    "taiex_distance_day_low_pct",
    "taiex_day_position",
    "minutes_since_open_ratio",
    "minutes_to_close_ratio",
]


def _unique(columns: list[str]) -> list[str]:
    return list(dict.fromkeys(columns))


FEATURE_GROUPS = {
    "base": BASE_FEATURE_COLUMNS,
    "base_candle": _unique(BASE_FEATURE_COLUMNS + CANDLE_FEATURE_COLUMNS),
    "base_trend": _unique(BASE_FEATURE_COLUMNS + TREND_FEATURE_COLUMNS),
    "base_volume": _unique(BASE_FEATURE_COLUMNS + VOLUME_FEATURE_COLUMNS),
    "base_position": _unique(BASE_FEATURE_COLUMNS + POSITION_FEATURE_COLUMNS),
    "all": _unique(
        BASE_FEATURE_COLUMNS
        + CANDLE_FEATURE_COLUMNS
        + TREND_FEATURE_COLUMNS
        + VOLUME_FEATURE_COLUMNS
        + POSITION_FEATURE_COLUMNS
    ),
}


def normalize_feature_group(value: str | None) -> str:
    key = str(value or "base").strip().lower()
    aliases = {
        "v7": "base",
        "candle": "base_candle",
        "trend": "base_trend",
        "volume": "base_volume",
        "position": "base_position",
        "full": "all",
    }
    key = aliases.get(key, key)
    if key not in FEATURE_GROUPS:
        allowed = ", ".join(FEATURE_GROUPS)
        raise ValueError(f"feature_group 必須是：{allowed}")
    return key


def feature_columns_for_group(value: str | None) -> list[str]:
    return list(FEATURE_GROUPS[normalize_feature_group(value)])


def prepare_v8_1_training_frame(
    rows: list[dict[str, Any]],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """建立 v8.1 公平比較資料。

    v8.0 為保留盤初預測能力，把全部特徵缺值補成 0；離線比較時卻因此
    把每天前 15 分鐘也當成完整樣本。v8.1 明確排除每個交易日前 15 筆，
    僅讓 30/60 分鐘延伸特徵使用 ready flag + 0 的設計。
    """
    frame = prepare_v8_training_frame(rows)
    if frame.empty:
        return frame, {
            "rows_before_warmup_filter": 0,
            "rows_after_warmup_filter": 0,
            "warmup_rows_removed": 0,
            "rows_per_day": {},
        }

    before = int(len(frame))
    # prepare_v8_training_frame 已按時間排序，且每個交易日尾端 15 筆因
    # 沒有 t+15 標籤而排除。此處再移除各日最前面的 15 筆。
    row_number = frame.groupby("trade_date", sort=False).cumcount()
    frame = frame[row_number >= CORE_WARMUP_MINUTES].copy()

    # 保護核心 15 分鐘欄位：即使上游版本改變，也不允許缺值樣本進模型。
    frame = frame.replace([np.inf, -np.inf], np.nan)
    frame = frame.dropna(
        subset=[
            *BASE_FEATURE_COLUMNS,
            "taiex_change_15m_points",
            "target_direction",
            "target_event",
        ]
    ).copy()

    # 30/60 分鐘延伸特徵可以在 ready=0 時補 0；其他新增特徵若資料源
    # 當下無法計算，也以 0 表示「沒有額外訊息」，但核心特徵不再補值。
    optional_columns = [
        column
        for column in V8_ALL_FEATURE_COLUMNS
        if column not in BASE_FEATURE_COLUMNS
    ]
    available_optional = [
        column for column in optional_columns if column in frame.columns
    ]
    frame[available_optional] = frame[available_optional].fillna(0.0)

    # 點數迴歸不直接猜絕對點數，先除以當下可得的波動尺度。
    close = pd.to_numeric(frame["taiex_close"], errors="coerce")
    volatility_pct = pd.to_numeric(
        frame["taiex_volatility_15m"],
        errors="coerce",
    ).abs()
    atr_pct = pd.to_numeric(
        frame["taiex_atr14_pct"],
        errors="coerce",
    ).abs()
    volatility_scale = close * volatility_pct / 100.0 * np.sqrt(15.0)
    atr_scale = close * atr_pct / 100.0 * np.sqrt(15.0)
    frame["target_scale_points"] = pd.concat(
        [volatility_scale, atr_scale],
        axis=1,
    ).max(axis=1).clip(lower=20.0, upper=1000.0)
    frame["target_normalized_15m"] = (
        frame["taiex_change_15m_points"]
        / frame["target_scale_points"].replace(0.0, np.nan)
    )
    frame = frame.dropna(
        subset=["target_scale_points", "target_normalized_15m"]
    ).copy()

    rows_per_day = {
        pd.Timestamp(day).strftime("%Y-%m-%d"): int(count)
        for day, count in frame.groupby("trade_date").size().items()
    }
    quality = {
        "base_feature_count": len(BASE_FEATURE_COLUMNS),
        "core_warmup_minutes": CORE_WARMUP_MINUTES,
        "rows_before_warmup_filter": before,
        "rows_after_warmup_filter": int(len(frame)),
        "warmup_rows_removed": before - int(len(frame)),
        "expected_complete_rows_per_day": 240,
        "rows_per_day": rows_per_day,
        "v8_source_feature_version": V8_FEATURE_SERVICE_VERSION,
    }
    return frame, quality
