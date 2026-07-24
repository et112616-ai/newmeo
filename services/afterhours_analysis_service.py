from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

from services.upload_service import publish_figure


POST_MARKET_ANALYSIS_VERSION = "2026-07-24-v2-DUAL-MODE-SUPPORT-ZONES"
BASE_DIR = Path(__file__).resolve().parents[1]
FONT_PATH = BASE_DIR / "assets" / "fonts" / "NotoSansTC-Regular.ttf"
_FONT_PROP = None


def _setup_font() -> dict[str, Any]:
    global _FONT_PROP

    try:
        if FONT_PATH.exists():
            font_manager.fontManager.addfont(str(FONT_PATH))
            _FONT_PROP = font_manager.FontProperties(fname=str(FONT_PATH))
            plt.rcParams["font.family"] = _FONT_PROP.get_name()
            plt.rcParams["axes.unicode_minus"] = False
            return {"fontproperties": _FONT_PROP}
    except Exception as exc:
        print("DEBUG post_market font setup failed", "| error =", repr(exc), flush=True)

    plt.rcParams["font.sans-serif"] = [
        "Noto Sans CJK TC",
        "Microsoft JhengHei",
        "Arial Unicode MS",
        "DejaVu Sans",
        "sans-serif",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    return {}


def _prepare_daily_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    work = df.copy()
    if not isinstance(work.index, pd.DatetimeIndex):
        work.index = pd.to_datetime(work.index, errors="coerce")
        work = work[~work.index.isna()].copy()
    if work.empty:
        return pd.DataFrame()

    work = work.sort_index()
    try:
        date_keys = pd.Index(work.index.normalize())
        work = work.loc[~date_keys.duplicated(keep="last")].copy()
    except Exception:
        pass

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col not in work.columns:
            if col == "Volume":
                work[col] = 0.0
            else:
                return pd.DataFrame()
        work[col] = pd.to_numeric(work[col], errors="coerce")

    work = work.dropna(subset=["Open", "High", "Low", "Close"])
    work = work[
        (work["Open"] > 0)
        & (work["High"] > 0)
        & (work["Low"] > 0)
        & (work["Close"] > 0)
    ].copy()
    return work


def _tick_size(price: float) -> float:
    price = float(price)
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def _round_to_tick(value: float, ref_price: float) -> float:
    tick = _tick_size(ref_price)
    return round(round(float(value) / tick) * tick, 2)


def _round_down_to_tick(value: float, ref_price: float) -> float:
    tick = _tick_size(ref_price)
    return round(math.floor(float(value) / tick + 1e-9) * tick, 2)


def _round_up_to_tick(value: float, ref_price: float) -> float:
    tick = _tick_size(ref_price)
    return round(math.ceil(float(value) / tick - 1e-9) * tick, 2)


def _fmt_price(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return "--"


def _fmt_zone(zone: tuple[float, float]) -> str:
    low, high = sorted(zone)
    if abs(high - low) < 1e-9:
        return _fmt_price(low)
    return f"{_fmt_price(low)}–{_fmt_price(high)}"


def _latest_date(work: pd.DataFrame) -> str:
    try:
        return work.index[-1].strftime("%Y-%m-%d")
    except Exception:
        return str(work.index[-1])


def _atr(work: pd.DataFrame, period: int = 14) -> float:
    recent = work.tail(max(period + 1, 3)).copy()
    prev_close = recent["Close"].shift(1)
    true_range = pd.concat(
        [
            recent["High"] - recent["Low"],
            (recent["High"] - prev_close).abs(),
            (recent["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    value = float(true_range.tail(period).median())
    close = float(work["Close"].iloc[-1])
    if not np.isfinite(value) or value <= 0:
        value = max(float((recent["High"] - recent["Low"]).median()), close * 0.02)
    return max(value, _tick_size(close) * 4)


def _make_zone(center: float, half_width: float, close: float) -> tuple[float, float]:
    low = _round_down_to_tick(max(center - half_width, _tick_size(close)), close)
    high = _round_up_to_tick(center + half_width, close)
    return low, max(high, low)


def _strength(score: float) -> str:
    if score >= 3.6:
        return "強"
    if score >= 2.1:
        return "中"
    return "弱"


def _cluster_candidates(
    candidates: list[tuple[float, float]],
    tolerance: float,
) -> list[dict[str, float]]:
    clean = sorted(
        [(float(price), float(weight)) for price, weight in candidates if price > 0],
        key=lambda item: item[0],
    )
    clusters: list[dict[str, float]] = []
    for price, weight in clean:
        if not clusters or abs(price - clusters[-1]["center"]) > tolerance:
            clusters.append({"center": price, "score": weight, "weight_sum": weight})
            continue
        group = clusters[-1]
        total = group["weight_sum"] + weight
        group["center"] = (group["center"] * group["weight_sum"] + price * weight) / total
        group["weight_sum"] = total
        group["score"] += weight
    return clusters


def _short_term_levels(work: pd.DataFrame) -> dict[str, Any]:
    recent = work.tail(60).copy()
    close = float(recent["Close"].iloc[-1])
    atr14 = _atr(recent, 14)
    tick = _tick_size(close)
    candidates: list[tuple[float, float]] = []

    # 轉折點使用較長歷史辨識，但呈現用途仍是未來 1–5 個交易日。
    highs = recent["High"].to_numpy(dtype=float)
    lows = recent["Low"].to_numpy(dtype=float)
    for idx in range(2, len(recent) - 2):
        if highs[idx] >= max(highs[idx - 2:idx + 3]):
            candidates.append((highs[idx], 1.5))
        if lows[idx] <= min(lows[idx - 2:idx + 3]):
            candidates.append((lows[idx], 1.5))

    for days, weight in [(5, 2.6), (10, 2.0), (20, 1.5), (40, 1.0)]:
        frame = recent.tail(days)
        if not frame.empty:
            candidates.extend(
                [
                    (float(frame["High"].max()), weight),
                    (float(frame["Low"].min()), weight),
                    (float(frame["Close"].mean()), weight * 0.65),
                ]
            )

    volume = pd.to_numeric(recent["Volume"], errors="coerce").fillna(0)
    if float(volume.max()) > 0:
        top_volume = recent.loc[volume.nlargest(min(6, len(recent))).index]
        for _, row in top_volume.iterrows():
            typical = (float(row["High"]) + float(row["Low"]) + float(row["Close"])) / 3
            candidates.append((typical, 1.0))

    tolerance = max(atr14 * 0.35, close * 0.006, tick * 4)
    clusters = _cluster_candidates(candidates, tolerance)
    supports = sorted(
        [item for item in clusters if item["center"] < close - tick],
        key=lambda item: (close - item["center"], -item["score"]),
    )
    resistances = sorted(
        [item for item in clusters if item["center"] > close + tick],
        key=lambda item: (item["center"] - close, -item["score"]),
    )

    fallback_supports = [
        {"center": close - atr14, "score": 1.0},
        {"center": close - atr14 * 2, "score": 0.8},
    ]
    fallback_resistances = [
        {"center": close + atr14, "score": 1.0},
        {"center": close + atr14 * 2, "score": 0.8},
    ]
    while len(supports) < 2:
        supports.append(fallback_supports[len(supports)])
    while len(resistances) < 2:
        resistances.append(fallback_resistances[len(resistances)])

    supports = sorted(supports[:2], key=lambda item: item["center"], reverse=True)
    resistances = sorted(resistances[:2], key=lambda item: item["center"])
    zone_half = max(atr14 * 0.16, tick * 2)

    return {
        "date": _latest_date(work),
        "close": _round_to_tick(close, close),
        "atr": atr14,
        "r1": _make_zone(resistances[0]["center"], zone_half, close),
        "r1_strength": _strength(resistances[0]["score"]),
        "r2": _make_zone(resistances[1]["center"], zone_half, close),
        "r2_strength": _strength(resistances[1]["score"]),
        "s1": _make_zone(supports[0]["center"], zone_half, close),
        "s1_strength": _strength(supports[0]["score"]),
        "s2": _make_zone(supports[1]["center"], zone_half, close),
        "s2_strength": _strength(supports[1]["score"]),
    }


def _next_day_levels(work: pd.DataFrame) -> dict[str, Any]:
    latest = work.iloc[-1]
    high = float(latest["High"])
    low = float(latest["Low"])
    close = float(latest["Close"])
    pivot = (high + low + close) / 3.0
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    atr5 = _atr(work, 5)
    zone_half = max(atr5 * 0.09, _tick_size(close) * 2)

    return {
        "date": _latest_date(work),
        "close": _round_to_tick(close, close),
        "pivot": _round_to_tick(pivot, close),
        "r1": _make_zone(r1, zone_half, close),
        "r2": _make_zone(r2, zone_half, close),
        "s1": _make_zone(s1, zone_half, close),
        "s2": _make_zone(s2, zone_half, close),
    }


def generate_post_market_analysis_chart(
    df: pd.DataFrame,
    stock_id: str,
    stock_name: str,
    branch_buy_rows: list[Any] | None = None,
    branch_sell_rows: list[Any] | None = None,
    analysis_mode: str = "short",
    daytrade_ratio: float | None = None,
    daytrade_date: str = "",
    daytrade_status: str = "",
) -> str:
    del branch_buy_rows, branch_sell_rows
    work = _prepare_daily_df(df)
    if work.empty:
        return ""

    mode = "daytrade" if str(analysis_mode).lower() == "daytrade" else "short"
    levels = _next_day_levels(work) if mode == "daytrade" else _short_term_levels(work)
    font_kwargs = _setup_font()

    fig = plt.figure(figsize=(5.8, 5.8), dpi=120, facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.set_facecolor("white")

    if mode == "daytrade":
        title = "隔日沖觀察｜次一交易日"
        rows = [
            ("壓 2", _fmt_zone(levels["r2"]), "#C62828", ""),
            ("壓 1", _fmt_zone(levels["r1"]), "#E53935", ""),
            ("中 軸", _fmt_price(levels["pivot"]), "#C69200", "P"),
            ("撐 1", _fmt_zone(levels["s1"]), "#00A84F", ""),
            ("撐 2", _fmt_zone(levels["s2"]), "#008C3A", ""),
        ]
    else:
        title = "短線支撐壓力｜未來 1–5 日"
        rows = [
            ("壓 2", _fmt_zone(levels["r2"]), "#C62828", levels["r2_strength"]),
            ("壓 1", _fmt_zone(levels["r1"]), "#E53935", levels["r1_strength"]),
            ("現 價", _fmt_price(levels["close"]), "#C69200", ""),
            ("撐 1", _fmt_zone(levels["s1"]), "#00A84F", levels["s1_strength"]),
            ("撐 2", _fmt_zone(levels["s2"]), "#008C3A", levels["s2_strength"]),
        ]

    ax.text(
        0.5, 0.945, title, fontsize=22, fontweight="bold",
        color="#222222", va="top", ha="center", **font_kwargs,
    )
    ax.text(
        0.5, 0.895, f"資料日 {levels['date']}", fontsize=12,
        color="#777777", va="top", ha="center", **font_kwargs,
    )

    y = 0.805
    for label, value, color, badge in rows:
        ax.text(
            0.31, y, f"{label}：", fontsize=25, fontweight="bold",
            color=color, va="top", ha="right", **font_kwargs,
        )
        ax.text(
            0.34, y, value, fontsize=25, fontweight="bold",
            color="#111111", va="top", ha="left", **font_kwargs,
        )
        if badge:
            ax.text(
                0.89, y + 0.004, badge, fontsize=11, fontweight="bold",
                color=color, va="top", ha="center", **font_kwargs,
            )
        y -= 0.125

    if mode == "daytrade":
        if daytrade_ratio is not None:
            ratio_note = (
                f"現股當沖占比 {float(daytrade_ratio):.1f}%"
                f"｜{daytrade_date[5:].replace('-', '/')} {daytrade_status or '已公布'}"
            )
        else:
            ratio_note = "當沖占比：最新官方資料尚未公布"
        ax.text(
            0.5, 0.155, ratio_note, fontsize=12, color="#555555",
            va="center", ha="center", **font_kwargs,
        )
        footnote = "Pivot 區間供隔日波動觀察，不代表買賣訊號。"
    else:
        ax.text(
            0.5, 0.155, "強弱依近 60 日轉折、均價與量價密集度估算",
            fontsize=12, color="#555555", va="center", ha="center", **font_kwargs,
        )
        footnote = "5日為觀察期間；支撐壓力為區間，非目標價。"

    ax.text(
        0.5, 0.105, footnote, fontsize=11, color="#888888",
        va="center", ha="center", **font_kwargs,
    )

    print(
        "DEBUG post_market dual mode",
        "| version =", POST_MARKET_ANALYSIS_VERSION,
        "| stock_id =", stock_id,
        "| stock_name =", stock_name,
        "| mode =", mode,
        "| date =", levels["date"],
        "| r1 =", levels["r1"],
        "| s1 =", levels["s1"],
        "| daytrade_ratio =", daytrade_ratio,
        flush=True,
    )

    try:
        return publish_figure(fig, f"{stock_id}_post_market_{mode}")
    finally:
        plt.close(fig)
