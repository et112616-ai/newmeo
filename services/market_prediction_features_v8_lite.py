from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


FEATURE_SERVICE_VERSION = "2026-07-27-v8.0-LITE-CAUSAL-OHLCV"
TAIPEI_TZ = "Asia/Taipei"
NEUTRAL_THRESHOLD_POINTS = 100.0

RAW_COLUMNS = [
    "taiex_open",
    "taiex_high",
    "taiex_low",
    "taiex_close",
    "taiex_volume",
    "txf_open",
    "txf_high",
    "txf_low",
    "txf_close",
    "txf_volume",
]

# V8 Lite 只使用當下或過去已完成分鐘可計算的欄位。
# 30/60 分鐘歷史不足時以 0 填補，並由 ready flag 明確告知模型。
FEATURE_COLUMNS = [
    "taiex_return_1m",
    "taiex_return_3m",
    "taiex_return_5m",
    "taiex_return_10m",
    "taiex_return_15m",
    "taiex_return_30m",
    "taiex_return_60m",
    "txf_return_1m",
    "txf_return_3m",
    "txf_return_5m",
    "txf_return_10m",
    "txf_return_15m",
    "txf_return_30m",
    "txf_return_60m",
    "basis_pct",
    "basis_change_1m",
    "basis_change_5m",
    "basis_change_15m",
    "taiex_volatility_5m",
    "taiex_volatility_15m",
    "taiex_volatility_30m",
    "txf_volatility_5m",
    "txf_volatility_15m",
    "txf_volatility_30m",
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
    "txf_volume_ratio_5m",
    "txf_volume_ratio_15m",
    "txf_volume_ratio_30m",
    "txf_volume_zscore_30m",
    "taiex_distance_day_high_pct",
    "taiex_distance_day_low_pct",
    "taiex_day_position",
    "taiex_previous_change_15m_points",
    "minute_sin",
    "minute_cos",
    "minutes_since_open_ratio",
    "minutes_to_close_ratio",
    "history_30m_ready",
    "history_60m_ready",
]


def _rolling_group(
    frame: pd.DataFrame,
    column: str,
    window: int,
    min_periods: int,
    operation: str,
) -> pd.Series:
    grouped = frame.groupby("trade_date", sort=False)[column]
    if operation == "mean":
        return grouped.transform(
            lambda values: values.rolling(
                window,
                min_periods=min_periods,
            ).mean()
        )
    if operation == "std":
        return grouped.transform(
            lambda values: values.rolling(
                window,
                min_periods=min_periods,
            ).std(ddof=0)
        )
    raise ValueError(f"unsupported rolling operation: {operation}")


def _add_candle_features(
    frame: pd.DataFrame,
    prefix: str,
) -> None:
    open_price = frame[f"{prefix}_open"]
    high = frame[f"{prefix}_high"]
    low = frame[f"{prefix}_low"]
    close = frame[f"{prefix}_close"]
    previous_close = frame.groupby("trade_date", sort=False)[
        f"{prefix}_close"
    ].shift(1)
    denominator = previous_close.where(
        previous_close > 0,
        open_price,
    ).replace(0.0, np.nan)

    frame[f"{prefix}_range_pct"] = (high - low) / denominator * 100.0
    frame[f"{prefix}_body_pct"] = (
        close - open_price
    ) / denominator * 100.0
    frame[f"{prefix}_upper_wick_pct"] = (
        high - pd.concat([open_price, close], axis=1).max(axis=1)
    ) / denominator * 100.0
    frame[f"{prefix}_lower_wick_pct"] = (
        pd.concat([open_price, close], axis=1).min(axis=1) - low
    ) / denominator * 100.0

    candle_range = (high - low).replace(0.0, np.nan)
    frame[f"{prefix}_close_position"] = (
        (close - low) / candle_range
    ).fillna(0.5)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame[f"_{prefix}_true_range"] = true_range
    atr = _rolling_group(
        frame,
        f"_{prefix}_true_range",
        14,
        5,
        "mean",
    )
    frame[f"{prefix}_atr14_pct"] = (
        atr / close.replace(0.0, np.nan) * 100.0
    )


def build_v8_feature_frame(
    raw_frame: pd.DataFrame,
    include_targets: bool = True,
) -> pd.DataFrame:
    """由已對齊的 TAIEX/TXF OHLCV 建立因果特徵。

    index 必須是台北時間的一分鐘時間戳；函式不會讀取未來分鐘。
    """
    if raw_frame is None or raw_frame.empty:
        return pd.DataFrame()

    frame = raw_frame.copy()
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame[~frame.index.isna()].copy()
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_convert(TAIPEI_TZ).tz_localize(None)
    frame.index = pd.DatetimeIndex(frame.index).floor("min")
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()

    missing_raw = [column for column in RAW_COLUMNS if column not in frame]
    if missing_raw:
        return pd.DataFrame()

    for column in RAW_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(
        subset=[
            "taiex_open",
            "taiex_high",
            "taiex_low",
            "taiex_close",
            "txf_open",
            "txf_high",
            "txf_low",
            "txf_close",
        ]
    ).copy()
    if frame.empty:
        return frame

    frame["taiex_volume"] = frame["taiex_volume"].fillna(0.0).clip(lower=0.0)
    frame["txf_volume"] = frame["txf_volume"].fillna(0.0).clip(lower=0.0)
    frame["trade_date"] = frame.index.normalize()
    grouped = frame.groupby("trade_date", sort=False)

    for minutes in (1, 3, 5, 10, 15, 30, 60):
        frame[f"taiex_return_{minutes}m"] = grouped[
            "taiex_close"
        ].pct_change(periods=minutes, fill_method=None) * 100.0
        frame[f"txf_return_{minutes}m"] = grouped[
            "txf_close"
        ].pct_change(periods=minutes, fill_method=None) * 100.0

    frame["basis"] = frame["txf_close"] - frame["taiex_close"]
    frame["basis_pct"] = (
        frame["basis"] / frame["taiex_close"].replace(0.0, np.nan) * 100.0
    )
    frame["basis_change_1m"] = grouped["basis"].diff(1)
    frame["basis_change_5m"] = grouped["basis"].diff(5)
    frame["basis_change_15m"] = grouped["basis"].diff(15)

    for prefix in ("taiex", "txf"):
        for window, minimum in ((5, 3), (15, 10), (30, 10)):
            frame[f"{prefix}_volatility_{window}m"] = _rolling_group(
                frame,
                f"{prefix}_return_1m",
                window,
                minimum,
                "std",
            )
        _add_candle_features(frame, prefix)

    for window, minimum in ((5, 3), (15, 10), (30, 10)):
        volume_mean = _rolling_group(
            frame,
            "txf_volume",
            window,
            minimum,
            "mean",
        )
        frame[f"txf_volume_ratio_{window}m"] = (
            frame["txf_volume"] / volume_mean.replace(0.0, np.nan)
        )
    volume_mean_30 = _rolling_group(
        frame,
        "txf_volume",
        30,
        10,
        "mean",
    )
    volume_std_30 = _rolling_group(
        frame,
        "txf_volume",
        30,
        10,
        "std",
    )
    frame["txf_volume_zscore_30m"] = (
        (frame["txf_volume"] - volume_mean_30)
        / volume_std_30.replace(0.0, np.nan)
    )

    day_high = grouped["taiex_high"].cummax()
    day_low = grouped["taiex_low"].cummin()
    day_range = (day_high - day_low).replace(0.0, np.nan)
    frame["taiex_distance_day_high_pct"] = (
        (frame["taiex_close"] - day_high)
        / frame["taiex_close"].replace(0.0, np.nan)
        * 100.0
    )
    frame["taiex_distance_day_low_pct"] = (
        (frame["taiex_close"] - day_low)
        / frame["taiex_close"].replace(0.0, np.nan)
        * 100.0
    )
    frame["taiex_day_position"] = (
        (frame["taiex_close"] - day_low) / day_range
    ).fillna(0.5)
    frame["taiex_previous_change_15m_points"] = (
        frame["taiex_close"] - grouped["taiex_close"].shift(15)
    )

    session_minute = (frame.index.hour * 60 + frame.index.minute) - 540
    angle = 2.0 * math.pi * session_minute / 270.0
    frame["minute_sin"] = np.sin(angle)
    frame["minute_cos"] = np.cos(angle)
    frame["minutes_since_open_ratio"] = np.clip(
        session_minute / 270.0,
        0.0,
        1.0,
    )
    frame["minutes_to_close_ratio"] = np.clip(
        (270.0 - session_minute) / 270.0,
        0.0,
        1.0,
    )
    day_row_number = grouped.cumcount()
    frame["history_30m_ready"] = (day_row_number >= 30).astype(float)
    frame["history_60m_ready"] = (day_row_number >= 60).astype(float)

    if include_targets:
        frame["taiex_change_15m_points"] = (
            grouped["taiex_close"].shift(-15) - frame["taiex_close"]
        )
        target = frame["taiex_change_15m_points"]
        frame["target_direction"] = np.where(
            target.isna(),
            np.nan,
            np.where(
                target > NEUTRAL_THRESHOLD_POINTS,
                1,
                np.where(
                    target < -NEUTRAL_THRESHOLD_POINTS,
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

    frame = frame.replace([np.inf, -np.inf], np.nan)
    # 盤初尚未累積 30/60 分鐘是已知狀態，不應因此排除預測。
    frame[FEATURE_COLUMNS] = frame[FEATURE_COLUMNS].fillna(0.0)
    if include_targets:
        frame = frame.dropna(
            subset=["target_direction", "taiex_change_15m_points"]
        ).copy()
        frame["target_direction"] = frame["target_direction"].astype(int)
        frame["target_event"] = frame["target_event"].astype(int)
    return frame


def prepare_v8_training_frame(
    rows: list[dict[str, Any]],
) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    required = ["ts", *RAW_COLUMNS]
    if any(column not in frame.columns for column in required):
        return pd.DataFrame()

    frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce", utc=True)
    frame = frame.dropna(subset=["ts"]).copy()
    frame["ts"] = (
        frame["ts"].dt.tz_convert(TAIPEI_TZ).dt.tz_localize(None)
    )
    frame = frame.sort_values("ts").drop_duplicates("ts", keep="last")
    frame = frame.set_index("ts")
    return build_v8_feature_frame(frame, include_targets=True)
