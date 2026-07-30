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
from matplotlib.patches import FancyBboxPatch

from services.upload_service import publish_figure


POST_MARKET_ANALYSIS_VERSION = (
    "2026-07-28-v2.5-CARD-ALIGNMENT"
)
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


def _make_resistance_zone(
    center: float,
    half_width: float,
    close: float,
) -> tuple[float, float]:
    low, high = _make_zone(center, half_width, close)
    minimum = _round_up_to_tick(close + _tick_size(close), close)
    low = max(low, minimum)
    high = max(high, low)
    return low, high


def _make_support_zone(
    center: float,
    half_width: float,
    close: float,
) -> tuple[float, float]:
    low, high = _make_zone(center, half_width, close)
    maximum = _round_down_to_tick(close - _tick_size(close), close)
    high = min(high, maximum)
    low = min(low, high)
    return low, high


def _strength(score: float) -> str:
    if score >= 3.6:
        return "強"
    if score >= 2.1:
        return "中"
    return "弱"


def _bias_style(label: str) -> tuple[str, str]:
    styles = {
        "偏強": ("#D32F2F", "#FFF2F2"),
        "中性": ("#B77900", "#FFF9E6"),
        "偏弱": ("#009B4D", "#EFFAF4"),
    }
    return styles.get(label, styles["中性"])


def _short_term_bias(work: pd.DataFrame) -> dict[str, Any]:
    recent = work.tail(20).copy()
    close = float(recent["Close"].iloc[-1])
    ma5 = float(recent["Close"].tail(5).mean())
    ma20 = float(recent["Close"].mean())

    score = 0
    score += 1 if close >= ma5 else -1
    score += 1 if ma5 >= ma20 else -1
    if len(recent) >= 6:
        base = float(recent["Close"].iloc[-6])
        return_5d = ((close / base) - 1.0) * 100.0 if base > 0 else 0.0
        score += 1 if return_5d >= 0 else -1
    else:
        return_5d = 0.0

    if score >= 2:
        label = "偏強"
    elif score <= -2:
        label = "偏弱"
    else:
        label = "中性"

    color, background = _bias_style(label)
    relation = (
        "現價高於5日與20日均線"
        if close >= ma5 and close >= ma20
        else "現價低於5日與20日均線"
        if close < ma5 and close < ma20
        else "現價位於5日與20日均線之間"
    )
    return {
        "label": label,
        "color": color,
        "background": background,
        "ma5": _round_to_tick(ma5, close),
        "ma20": _round_to_tick(ma20, close),
        "return_5d": return_5d,
        "note": relation,
    }


def _source_label(source_weights: dict[str, float]) -> str:
    labels = {
        "turning": "轉折",
        "range": "高低",
        "average": "均價",
        "volume": "量價",
        "gap": "缺口",
        "atr": "ATR",
    }
    ranked = sorted(
        source_weights.items(),
        key=lambda item: (-float(item[1]), item[0]),
    )
    selected: list[str] = []
    for source, _ in ranked:
        label = labels.get(source, source)
        if label not in selected:
            selected.append(label)
        if len(selected) >= 2:
            break
    return "＋".join(selected) if selected else "綜合"


def _cluster_candidates(
    candidates: list[tuple[float, float, str]],
    tolerance: float,
) -> list[dict[str, Any]]:
    clean = sorted(
        [
            (float(price), float(weight), str(source))
            for price, weight, source in candidates
            if price > 0
        ],
        key=lambda item: item[0],
    )
    clusters: list[dict[str, Any]] = []
    for price, weight, source in clean:
        if not clusters or abs(price - clusters[-1]["center"]) > tolerance:
            clusters.append(
                {
                    "center": price,
                    "score": weight,
                    "weight_sum": weight,
                    "source_weights": {source: weight},
                }
            )
            continue
        group = clusters[-1]
        total = group["weight_sum"] + weight
        group["center"] = (group["center"] * group["weight_sum"] + price * weight) / total
        group["weight_sum"] = total
        group["score"] += weight
        group["source_weights"][source] = (
            float(group["source_weights"].get(source, 0.0)) + weight
        )
    for group in clusters:
        group["source"] = _source_label(group.get("source_weights") or {})
    return clusters


def _unfilled_gap_zones(
    work: pd.DataFrame,
    lookback: int = 20,
) -> list[dict[str, Any]]:
    """
    回傳近 N 個交易日仍未完全回補的跳空區間。

    向上跳空：前高至當日低之間，位於現價下方時視為支撐。
    向下跳空：當日高至前低之間，位於現價上方時視為壓力。
    部分回補會縮小剩餘區間；完全回補則移除。
    """
    if work is None or len(work) < 2:
        return []

    recent = work.tail(max(int(lookback) + 1, 3)).copy()
    close = float(recent["Close"].iloc[-1])
    tick = _tick_size(close)
    minimum_width = max(tick * 2, close * 0.0025)
    zones: list[dict[str, Any]] = []

    for idx in range(1, len(recent)):
        previous = recent.iloc[idx - 1]
        current = recent.iloc[idx]
        previous_high = float(previous["High"])
        previous_low = float(previous["Low"])
        previous_close = float(previous["Close"])
        current_high = float(current["High"])
        current_low = float(current["Low"])
        future = recent.iloc[idx + 1:]
        age_days = len(recent) - 1 - idx
        recency_score = max(0.0, 1.0 - age_days / max(float(lookback), 1.0))
        score = 3.2 + recency_score * 0.8

        # 向上跳空：若之後最低價跌回前高以下，代表缺口已完全回補。
        up_gap_width = current_low - previous_high
        maximum_gap_width = max(previous_close * 0.12, minimum_width)
        if minimum_width <= up_gap_width <= maximum_gap_width:
            zone_low = previous_high
            zone_high = current_low
            if not future.empty:
                later_low = float(future["Low"].min())
                if later_low <= zone_low + tick * 0.25:
                    continue
                zone_high = min(zone_high, later_low)
            zone_low = _round_down_to_tick(zone_low, close)
            zone_high = _round_up_to_tick(zone_high, close)
            if (
                zone_high - zone_low >= tick
                and zone_high < close - tick
            ):
                zones.append(
                    {
                        "center": (zone_low + zone_high) / 2.0,
                        "score": score,
                        "source": "缺口",
                        "source_weights": {"gap": score},
                        "zone": (zone_low, zone_high),
                        "side": "support",
                        "gap_type": "up",
                    }
                )

        # 向下跳空：若之後最高價漲回前低以上，代表缺口已完全回補。
        down_gap_width = previous_low - current_high
        if minimum_width <= down_gap_width <= maximum_gap_width:
            zone_low = current_high
            zone_high = previous_low
            if not future.empty:
                later_high = float(future["High"].max())
                if later_high >= zone_high - tick * 0.25:
                    continue
                zone_low = max(zone_low, later_high)
            zone_low = _round_down_to_tick(zone_low, close)
            zone_high = _round_up_to_tick(zone_high, close)
            if (
                zone_high - zone_low >= tick
                and zone_low > close + tick
            ):
                zones.append(
                    {
                        "center": (zone_low + zone_high) / 2.0,
                        "score": score,
                        "source": "缺口",
                        "source_weights": {"gap": score},
                        "zone": (zone_low, zone_high),
                        "side": "resistance",
                        "gap_type": "down",
                    }
                )

    return zones


def _merge_gap_zones(
    clusters: list[dict[str, Any]],
    gap_zones: list[dict[str, Any]],
    tolerance: float,
) -> list[dict[str, Any]]:
    merged = list(clusters)
    for gap in gap_zones:
        gap_low, gap_high = gap["zone"]
        matching = [
            item
            for item in merged
            if (
                gap_low - tolerance
                <= float(item.get("center") or 0.0)
                <= gap_high + tolerance
            )
        ]
        if not matching:
            merged.append(gap)
            continue

        group = min(
            matching,
            key=lambda item: abs(
                float(item.get("center") or 0.0) - float(gap["center"])
            ),
        )
        gap_score = float(gap.get("score") or 0.0)
        group["score"] = float(group.get("score") or 0.0) + gap_score
        group["weight_sum"] = float(group.get("weight_sum") or 0.0) + gap_score
        source_weights = dict(group.get("source_weights") or {})
        source_weights["gap"] = float(source_weights.get("gap", 0.0)) + gap_score
        group["source_weights"] = source_weights
        group["source"] = _source_label(source_weights)
        group["zone"] = gap["zone"]
        group["side"] = gap["side"]
        group["gap_type"] = gap["gap_type"]
    return merged


def _zone_from_candidate(
    item: dict[str, Any],
    zone_half: float,
    close: float,
    side: str,
) -> tuple[float, float]:
    direct_zone = item.get("zone")
    if isinstance(direct_zone, (tuple, list)) and len(direct_zone) == 2:
        low = _round_down_to_tick(float(direct_zone[0]), close)
        high = _round_up_to_tick(float(direct_zone[1]), close)
        if side == "resistance":
            minimum = _round_up_to_tick(close + _tick_size(close), close)
            return max(low, minimum), max(high, minimum)
        maximum = _round_down_to_tick(close - _tick_size(close), close)
        return min(low, maximum), min(high, maximum)

    if side == "resistance":
        return _make_resistance_zone(item["center"], zone_half, close)
    return _make_support_zone(item["center"], zone_half, close)


def _candidate_badge(item: dict[str, Any]) -> str:
    source = _candidate_display_source(item)
    strength = _strength(float(item.get("score") or 0.0))
    # LINE 圖卡寬度有限，主來源只保留前一項；完整方法另列於說明卡。
    primary_source = "缺口" if "缺口" in source else source.split("＋", 1)[0]
    return f"{primary_source}・{strength}"


def _candidate_display_source(item: dict[str, Any]) -> str:
    source = str(item.get("source") or "綜合")
    source_weights = item.get("source_weights") or {}
    if float(source_weights.get("gap") or 0.0) <= 0:
        return source
    non_gap = [part for part in source.split("＋") if part != "缺口"]
    return "缺口" + (f"＋{non_gap[0]}" if non_gap else "")


def _short_term_levels(work: pd.DataFrame) -> dict[str, Any]:
    recent = work.tail(60).copy()
    close = float(recent["Close"].iloc[-1])
    atr14 = _atr(recent, 14)
    tick = _tick_size(close)
    candidates: list[tuple[float, float, str]] = []

    # 轉折點使用較長歷史辨識，但呈現用途仍是未來 1–5 個交易日。
    highs = recent["High"].to_numpy(dtype=float)
    lows = recent["Low"].to_numpy(dtype=float)
    for idx in range(2, len(recent) - 2):
        if highs[idx] >= max(highs[idx - 2:idx + 3]):
            candidates.append((highs[idx], 1.5, "turning"))
        if lows[idx] <= min(lows[idx - 2:idx + 3]):
            candidates.append((lows[idx], 1.5, "turning"))

    for days, weight in [(5, 2.6), (10, 2.0), (20, 1.5), (40, 1.0)]:
        frame = recent.tail(days)
        if not frame.empty:
            candidates.extend(
                [
                    (float(frame["High"].max()), weight, "range"),
                    (float(frame["Low"].min()), weight, "range"),
                    (float(frame["Close"].mean()), weight * 0.65, "average"),
                ]
            )

    volume = pd.to_numeric(recent["Volume"], errors="coerce").fillna(0)
    if float(volume.max()) > 0:
        top_volume = recent.loc[volume.nlargest(min(6, len(recent))).index]
        for _, row in top_volume.iterrows():
            typical = (float(row["High"]) + float(row["Low"]) + float(row["Close"])) / 3
            candidates.append((typical, 1.0, "volume"))

    tolerance = max(atr14 * 0.35, close * 0.006, tick * 4)
    clusters = _cluster_candidates(candidates, tolerance)
    gap_zones = _unfilled_gap_zones(recent, lookback=20)
    clusters = _merge_gap_zones(clusters, gap_zones, tolerance)
    supports = sorted(
        [
            item
            for item in clusters
            if item["center"] < close - tick
            and item.get("side") != "resistance"
        ],
        key=lambda item: (close - item["center"], -item["score"]),
    )
    resistances = sorted(
        [
            item
            for item in clusters
            if item["center"] > close + tick
            and item.get("side") != "support"
        ],
        key=lambda item: (item["center"] - close, -item["score"]),
    )

    fallback_supports = [
        {"center": close - atr14, "score": 1.0, "source": "ATR"},
        {"center": close - atr14 * 2, "score": 0.8, "source": "ATR"},
    ]
    fallback_resistances = [
        {"center": close + atr14, "score": 1.0, "source": "ATR"},
        {"center": close + atr14 * 2, "score": 0.8, "source": "ATR"},
    ]
    while len(supports) < 2:
        supports.append(fallback_supports[len(supports)])
    while len(resistances) < 2:
        resistances.append(fallback_resistances[len(resistances)])

    supports = sorted(supports[:2], key=lambda item: item["center"], reverse=True)
    resistances = sorted(resistances[:2], key=lambda item: item["center"])
    zone_half = max(atr14 * 0.16, tick * 2)
    bias = _short_term_bias(recent)

    return {
        "date": _latest_date(work),
        "close": _round_to_tick(close, close),
        "atr": atr14,
        "r1": _zone_from_candidate(
            resistances[0], zone_half, close, "resistance"
        ),
        "r1_strength": _strength(resistances[0]["score"]),
        "r1_source": _candidate_display_source(resistances[0]),
        "r1_badge": _candidate_badge(resistances[0]),
        "r2": _zone_from_candidate(
            resistances[1], zone_half, close, "resistance"
        ),
        "r2_strength": _strength(resistances[1]["score"]),
        "r2_source": _candidate_display_source(resistances[1]),
        "r2_badge": _candidate_badge(resistances[1]),
        "s1": _zone_from_candidate(supports[0], zone_half, close, "support"),
        "s1_strength": _strength(supports[0]["score"]),
        "s1_source": _candidate_display_source(supports[0]),
        "s1_badge": _candidate_badge(supports[0]),
        "s2": _zone_from_candidate(supports[1], zone_half, close, "support"),
        "s2_strength": _strength(supports[1]["score"]),
        "s2_source": _candidate_display_source(supports[1]),
        "s2_badge": _candidate_badge(supports[1]),
        "gap_count": len(gap_zones),
        "bias": bias,
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
    pivot_gap = close - pivot
    bias_tolerance = max(atr5 * 0.08, _tick_size(close) * 2)
    if pivot_gap > bias_tolerance:
        bias_label = "偏強"
        bias_note = "現價位於 Pivot 上方"
    elif pivot_gap < -bias_tolerance:
        bias_label = "偏弱"
        bias_note = "現價位於 Pivot 下方"
    else:
        bias_label = "中性"
        bias_note = "現價貼近 Pivot"
    bias_color, bias_background = _bias_style(bias_label)

    return {
        "date": _latest_date(work),
        "close": _round_to_tick(close, close),
        "pivot": _round_to_tick(pivot, close),
        "r1": _make_resistance_zone(r1, zone_half, close),
        "r2": _make_resistance_zone(r2, zone_half, close),
        "s1": _make_support_zone(s1, zone_half, close),
        "s2": _make_support_zone(s2, zone_half, close),
        "bias": {
            "label": bias_label,
            "color": bias_color,
            "background": bias_background,
            "pivot_gap": _round_to_tick(pivot_gap, close),
            "note": bias_note,
        },
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
            ("壓 2", _fmt_zone(levels["r2"]), "#C62828", "Pivot"),
            ("壓 1", _fmt_zone(levels["r1"]), "#E53935", "Pivot"),
            (
                "現 價",
                _fmt_price(levels["close"]),
                "#C69200",
                levels["bias"]["label"],
            ),
            ("撐 1", _fmt_zone(levels["s1"]), "#00A84F", "Pivot"),
            ("撐 2", _fmt_zone(levels["s2"]), "#008C3A", "Pivot"),
        ]
    else:
        title = "短線支撐壓力｜未來 1–5 日"
        rows = [
            ("壓 2", _fmt_zone(levels["r2"]), "#C62828", levels["r2_badge"]),
            ("壓 1", _fmt_zone(levels["r1"]), "#E53935", levels["r1_badge"]),
            (
                "現 價",
                _fmt_price(levels["close"]),
                "#C69200",
                levels["bias"]["label"],
            ),
            ("撐 1", _fmt_zone(levels["s1"]), "#00A84F", levels["s1_badge"]),
            ("撐 2", _fmt_zone(levels["s2"]), "#008C3A", levels["s2_badge"]),
        ]

    header_color = "#FFF4F1" if mode == "daytrade" else "#F1F7FF"
    ax.add_patch(
        FancyBboxPatch(
            (0.075, 0.875),
            0.85,
            0.085,
            boxstyle="round,pad=0.012,rounding_size=0.025",
            linewidth=0,
            facecolor=header_color,
            transform=ax.transAxes,
        )
    )
    ax.text(
        0.5, 0.932, title, fontsize=22, fontweight="bold",
        color="#222222", va="center", ha="center", **font_kwargs,
    )

    row_backgrounds = ["#FFF3F3", "#FFF8F7", "#FFFBEA", "#F2FBF6", "#ECF8F1"]
    row_y_positions = [0.79, 0.675, 0.56, 0.445, 0.33]
    for row_index, (label, value, color, badge) in enumerate(rows):
        y = row_y_positions[row_index]
        ax.add_patch(
            FancyBboxPatch(
                (0.075, y - 0.068),
                0.85,
                0.096,
                boxstyle="round,pad=0.008,rounding_size=0.018",
                linewidth=0.8,
                edgecolor="#E7E9ED",
                facecolor=row_backgrounds[row_index],
                transform=ax.transAxes,
            )
        )
        ax.add_patch(
            FancyBboxPatch(
                (0.075, y - 0.068),
                0.013,
                0.096,
                boxstyle="round,pad=0,rounding_size=0.006",
                linewidth=0,
                facecolor=color,
                transform=ax.transAxes,
            )
        )
        ax.text(
            0.29, y - 0.018, label, fontsize=20, fontweight="bold",
            color=color, va="center", ha="right", **font_kwargs,
        )
        ax.text(
            0.34, y - 0.018, value, fontsize=22, fontweight="bold",
            color="#111111", va="center", ha="left", **font_kwargs,
        )
        if badge:
            badge_color = (
                levels["bias"]["color"]
                if row_index == 2 and badge in {"偏強", "中性", "偏弱"}
                else color
            )
            badge_facecolor = "#FFFFFF" if badge != "Pivot" else "#F8FAFC"
            ax.add_patch(
                FancyBboxPatch(
                    (0.805, y - 0.044),
                    0.12,
                    0.052,
                    boxstyle="round,pad=0.004,rounding_size=0.012",
                    linewidth=0.8,
                    edgecolor=badge_color,
                    facecolor=badge_facecolor,
                    transform=ax.transAxes,
                )
            )
            ax.text(
                0.865,
                y - 0.018,
                badge,
                fontsize=9.6 if len(str(badge)) >= 4 else 10.8,
                fontweight="bold",
                color=badge_color, va="center", ha="center", **font_kwargs,
            )

    if mode == "daytrade":
        if daytrade_ratio is not None:
            ratio_note = (
                f"現股當沖占比 {float(daytrade_ratio):.1f}%"
                f"｜{daytrade_date[5:].replace('-', '/')} {daytrade_status or '已公布'}"
            )
        else:
            ratio_note = "當沖占比：最新官方資料尚未公布"
        ax.text(
            0.5,
            0.175,
            ratio_note,
            fontsize=12.2,
            fontweight="bold",
            color="#4B5563",
            va="center", ha="center", **font_kwargs,
        )
        pivot_gap = float(levels["bias"]["pivot_gap"])
        gap_text = f"{pivot_gap:+,.2f}"
        comparison_note = (
            f"隔日判讀 {levels['bias']['label']}｜"
            f"Pivot {_fmt_price(levels['pivot'])}｜現價差 {gap_text}"
        )
        footnote = "支撐壓力依 Pivot 推估；中間列統一顯示現價。"
    else:
        ax.text(
            0.5,
            0.175,
            "觀察期 1–5 日｜以資料日收盤價作為現價",
            fontsize=12.2,
            fontweight="bold",
            color="#4B5563",
            va="center", ha="center", **font_kwargs,
        )
        comparison_note = (
            f"短線判讀 {levels['bias']['label']}｜"
            f"MA5 {_fmt_price(levels['bias']['ma5'])}｜"
            f"MA20 {_fmt_price(levels['bias']['ma20'])}"
        )
        footnote = (
            "來源：轉折／高低／均價／量價／未回補缺口；"
            "ATR14定區寬。"
        )

    ax.text(
        0.5, 0.13, comparison_note, fontsize=12.2, fontweight="bold",
        color=levels["bias"]["color"], va="center", ha="center", **font_kwargs,
    )
    ax.text(
        0.5, 0.085, footnote, fontsize=10.8, color="#5F6368",
        va="center", ha="center", **font_kwargs,
    )
    ax.text(
        0.91, 0.04, f"資料日 {levels['date']}", fontsize=10.8,
        color="#737373", va="center", ha="right", **font_kwargs,
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
        "| anchor = current_close",
        "| bias =", levels["bias"]["label"],
        "| pivot =", levels.get("pivot"),
        "| r1_source =", levels.get("r1_source"),
        "| s1_source =", levels.get("s1_source"),
        "| gap_count =", levels.get("gap_count"),
        "| daytrade_ratio =", daytrade_ratio,
        flush=True,
    )

    try:
        return publish_figure(fig, f"{stock_id}_post_market_{mode}")
    finally:
        plt.close(fig)

def generate_post_market_fibonacci_chart(
    df: pd.DataFrame,
    stock_id: str,
    stock_name: str,
) -> str:
    """
    產生第 3 張圖卡：60 根日 K 純 K 棒黃金切割線。
    風格比照標準日 K 線圖（暗色底、右側預留 20% 數據標籤區）。
    """
    work = _prepare_daily_df(df)
    if work.empty:
        return ""

    recent = work.tail(60).reset_index(drop=True)
    total_bars = len(recent)
    font_kwargs = _setup_font()

    high_val = float(recent["High"].max())
    low_val = float(recent["Low"].min())
    diff = high_val - low_val
    latest_close = float(recent["Close"].iloc[-1])

    # 指定黃金切割位階與對應顏色
    fibo_levels = [
        ("100.0%", high_val, "#555555"),               # 深灰
        ("78.6%",  low_val + diff * 0.786, "#888888"), # 淺灰
        ("61.8%",  low_val + diff * 0.618, "#EF5350"), # 紅色 (關鍵位)
        ("50.0%",  low_val + diff * 0.500, "#FFA726"), # 橘色
        ("38.2%",  low_val + diff * 0.382, "#26A69A"), # 綠色
        ("23.6%",  low_val + diff * 0.236, "#888888"), # 淺灰
        ("0.0%",   low_val, "#555555"),                # 深灰
    ]

    # 建立 TradingView 風格暗色畫布 (與常見日K視圖一致)
    fig, ax = plt.subplots(figsize=(8, 5.5), dpi=130)
    fig.patch.set_facecolor("#131722")
    ax.set_facecolor("#131722")

    # 右側預留 22% X 軸空間，專門放價格標籤
    right_margin_x = total_bars * 1.22
    ax.set_xlim(-1, right_margin_x)

    y_padding = diff * 0.05 if diff > 0 else 1.0
    ax.set_ylim(low_val - y_padding, high_val + y_padding)

    # 繪製純 K 棒 (台股習慣：漲紅跌綠)
    for i, row in recent.iterrows():
        open_p, close_p = float(row["Open"]), float(row["Close"])
        high_p, low_p = float(row["High"]), float(row["Low"])
        
        is_up = close_p >= open_p
        color = "#EF5350" if is_up else "#26A69A"

        # 上下影線
        ax.plot([i, i], [low_p, high_p], color=color, linewidth=1.1, alpha=0.9)
        
        # 實體
        body_bottom = min(open_p, close_p)
        body_height = max(abs(close_p - open_p), diff * 0.004)
        
        rect = patches.Rectangle(
            (i - 0.32, body_bottom),
            0.64,
            body_height,
            facecolor=color,
            edgecolor=color,
            linewidth=0.5
        )
        ax.add_patch(rect)

    # 繪製黃金切割線與右側標籤 (價格 │ 比例)
    label_x_pos = total_bars + 1.2
    for pct_label, val, color in fibo_levels:
        ax.hlines(
            y=val,
            xmin=0,
            xmax=total_bars - 0.5,
            colors=color,
            linestyle="--",
            linewidth=0.8,
            alpha=0.6
        )
        
        text_str = f"{val:,.2f}  │  {pct_label}"
        ax.text(
            label_x_pos,
            val,
            text_str,
            color=color,
            fontsize=9.5,
            fontweight="bold",
            va="center",
            ha="left",
            **font_kwargs
        )

    # 繪製最新價亮色虛線與右側顯眼標籤
    ax.hlines(
        y=latest_close,
        xmin=0,
        xmax=total_bars + 0.5,
        colors="#FFD700",
        linestyle="-",
        linewidth=1.2,
        alpha=0.9
    )
    latest_text = f"{latest_close:,.2f}  │  最新價"
    ax.text(
        label_x_pos,
        latest_close,
        latest_text,
        color="#131722",
        fontsize=9.5,
        fontweight="bold",
        va="center",
        ha="left",
        bbox=dict(
            boxstyle="round,pad=0.35,rounding_size=0.2",
            facecolor="#FFD700",
            edgecolor="none"
        ),
        **font_kwargs
    )

    # 頂部標題
    title_str = f"{stock_id} {stock_name} (日K 60根黃金切割分析)"
    ax.text(
        0.0,
        high_val + y_padding * 0.4,
        title_str,
        color="#FFFFFF",
        fontsize=13,
        fontweight="bold",
        va="bottom",
        ha="left",
        **font_kwargs
    )

    ax.axis("off")
    plt.subplots_adjust(left=0.03, right=0.97, top=0.90, bottom=0.03)

    try:
        return publish_figure(fig, f"{stock_id}_post_market_fibonacci")
    finally:
        plt.close(fig)
