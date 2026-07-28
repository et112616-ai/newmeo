from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
from matplotlib import font_manager


# 所有個股、大盤、期貨圖表的共用視覺規格。
UNIFIED_KLINE_STYLE_VERSION = "2026-07-28-v2-UNIFIED-KLINE-PRESETS"

AXIS_TICK_FONTSIZE = 11
HIGH_LOW_FONTSIZE = 12
INFO_FONTSIZE = 14

UP_COLOR = "#FF2D2D"
DOWN_COLOR = "#00B050"
FLAT_COLOR = "#666666"
CHART_BACKGROUND = "#F8F9FA"
GRID_COLOR = "#AEB6BF"

DEFAULT_CANDLE_WIDTH = 0.58
CANDLE_WICK_LINEWIDTH = 1.0
BASE_GRID_ALPHA = 0.18
QUARTER_GRID_ALPHA = 0.34
GRID_LINEWIDTH = 0.85
INTRADAY_VOLUME_ALPHA = 0.38
INTRADAY_VOLUME_WIDTH_RATIO = 0.62

# LINE Flex 圖片以「顯示面積」換取可讀性，不用堆高像素。
# 官方上限為 1024 x 1024；保留安全餘裕可降低不同裝置載入失敗率。
LINE_IMAGE_EXPORT_DPI = 100
LINE_IMAGE_MAX_EDGE = 960
LINE_IMAGE_TARGET_BYTES = 900 * 1024
LINE_STOCK_CHART_ASPECT_RATIO = "6:5"

FIGURE_SIZES = {
    "stock_instant": (9.2, 8.0),
    "stock_kline": (9.6, 8.0),
    "market_index": (9.4, 7.6),
    "market_future": (8.6, 5.6),
    "stock_future": (7.6, 5.6),
}

# 共用外觀由本檔管理；商品差異只留在 preset。
# 所有 MA 都以完整 period 才開始顯示，避免資料不足時產生假均線。
KLINE_PRESETS: dict[str, dict[str, Any]] = {
    "stock": {
        "figure_size": FIGURE_SIZES["stock_kline"],
        "dpi": 132,
        "height_ratios": (1.02, 3.32, 1.08),
        "subplots_adjust": {
            "left": 0.10,
            "right": 0.97,
            "top": 0.98,
            "bottom": 0.085,
            "hspace": 0.09,
        },
        "ma_periods": (5, 20, 60, 120),
        "ma_styles": {
            "MA5": ("#111111", 1.2),
            "MA20": ("#1F77B4", 1.2),
            "MA60": ("#FF7F0E", 1.2),
            "MA120": ("#9467BD", 1.2),
        },
        "display_rows": {
            "1m": 60,
            "5m": 60,
            "15m": 60,
            "30m": 60,
            "60m": 60,
            "D": 60,
            "W": 80,
            "M": 80,
            "default": 60,
        },
        "info_ma_fontsize": 16,
        "info_ohlc_fontsize": 16,
    },
    "market_index": {
        "figure_size": FIGURE_SIZES["market_index"],
        "dpi": 130,
        "height_ratios": (1.0, 3.5, 1.15),
        "subplots_adjust": {
            "left": 0.10,
            "right": 0.97,
            "top": 0.97,
            "bottom": 0.075,
            "hspace": 0.05,
        },
        "ma_periods": (5, 12, 22, 30, 66, 120),
        "ma_styles": {
            "MA5": ("#111111", 1.2),
            "MA12": ("#1F77B4", 1.2),
            "MA22": ("#2CA02C", 1.2),
            "MA30": ("#D62728", 1.2),
            "MA66": ("#FF7F0E", 1.2),
            "MA120": ("#9467BD", 1.2),
        },
        "display_rows": {"D": 60, "default": 60},
        "info_ma_fontsize": 15,
    },
    "market_future": {
        "figure_size": FIGURE_SIZES["market_future"],
        "dpi": 118,
        "axes_rect": (0.10, 0.14, 0.86, 0.72),
        "header_position": (0.10, 0.94),
        "display_rows": {
            "1m": 60,
            "5m": 60,
            "15m": 60,
            "30m": 60,
            "60m": 60,
            "default": 60,
        },
        "bollinger_period": 20,
        "bollinger_std": 2.0,
        "bollinger_styles": {
            "BB_UPPER": ("#D32F2F", 1.25),
            "BB_MID": ("#333333", 1.15),
            "BB_LOWER": ("#00A84F", 1.25),
        },
    },
}


def get_kline_preset(kind: str) -> dict[str, Any]:
    """回傳商品 K 線 preset 的獨立副本，避免呼叫端誤改全域設定。"""
    key = str(kind or "").strip().lower()
    if key not in KLINE_PRESETS:
        raise KeyError(f"unknown kline preset: {kind}")
    return deepcopy(KLINE_PRESETS[key])


def get_kline_display_rows(kind: str, time_frame: str) -> int:
    preset = get_kline_preset(kind)
    row_map = preset.get("display_rows") or {}
    tf = str(time_frame or "").strip()
    return int(row_map.get(tf, row_map.get("default", 60)))


def add_moving_averages(
    frame: pd.DataFrame,
    periods: Sequence[int],
    close_column: str = "Close",
) -> pd.DataFrame:
    """以完整 period 計算均線，統一個股與大盤的 min_periods 規則。"""
    work = frame.copy()
    close = pd.to_numeric(work[close_column], errors="coerce")
    for raw_period in periods:
        period = int(raw_period)
        work[f"MA{period}"] = close.rolling(
            period,
            min_periods=period,
        ).mean()
    return work


def draw_candles(
    ax,
    frame: pd.DataFrame,
    x_values: Sequence[float] | None = None,
    candle_width: float = DEFAULT_CANDLE_WIDTH,
    wick_linewidth: float = CANDLE_WICK_LINEWIDTH,
) -> tuple[list[float], list[str]]:
    """用相同寬度、影線與台股紅綠色繪製所有 K 棒。"""
    if frame is None or frame.empty:
        return [], []

    x = list(x_values) if x_values is not None else list(range(len(frame)))
    open_values = pd.to_numeric(frame["Open"], errors="coerce").astype(float).values
    high_values = pd.to_numeric(frame["High"], errors="coerce").astype(float).values
    low_values = pd.to_numeric(frame["Low"], errors="coerce").astype(float).values
    close_values = pd.to_numeric(frame["Close"], errors="coerce").astype(float).values

    colors = [
        UP_COLOR if close_price >= open_price else DOWN_COLOR
        for open_price, close_price in zip(open_values, close_values)
    ]
    body_bottom = [
        min(open_price, close_price)
        for open_price, close_price in zip(open_values, close_values)
    ]
    body_height = [
        max(abs(close_price - open_price), 0.01)
        for open_price, close_price in zip(open_values, close_values)
    ]

    ax.vlines(
        x,
        low_values,
        high_values,
        linewidth=wick_linewidth,
        colors=colors,
        zorder=2,
    )
    ax.bar(
        x,
        body_height,
        bottom=body_bottom,
        width=candle_width,
        color=colors,
        edgecolor=colors,
        linewidth=0,
        align="center",
        zorder=3,
    )
    return x, colors


def draw_volume_bars(
    ax,
    volume_values,
    colors: Sequence[str],
    x_values: Sequence[float] | None = None,
    candle_width: float = DEFAULT_CANDLE_WIDTH,
) -> None:
    volumes = pd.to_numeric(pd.Series(volume_values), errors="coerce").fillna(0.0)
    x = list(x_values) if x_values is not None else list(range(len(volumes)))
    ax.bar(
        x,
        volumes.to_numpy(dtype=float),
        width=candle_width,
        color=list(colors),
        edgecolor="none",
        align="center",
        zorder=2,
    )


def draw_moving_average_lines(
    ax,
    frame: pd.DataFrame,
    styles: Mapping[str, tuple[str, float]],
    x_values: Sequence[float] | None = None,
) -> None:
    x = list(x_values) if x_values is not None else list(range(len(frame)))
    for column, (color, linewidth) in styles.items():
        if column in frame.columns:
            ax.plot(
                x,
                pd.to_numeric(frame[column], errors="coerce").values,
                linewidth=float(linewidth),
                color=color,
                zorder=4,
            )


def configure_chart_font(base_dir: str | Path | None = None):
    """統一註冊繁中字型，並回傳可供 text() 使用的 FontProperties。"""
    font_prop = None

    font_dirs = []
    if base_dir is not None:
        font_dirs.append(Path(base_dir) / "assets" / "fonts")
    font_dirs.extend(
        [
            Path("/opt/render/project/src/assets/fonts"),
            Path("/mnt/data"),
        ]
    )

    for font_dir in font_dirs:
        regular_candidates = [
            font_dir / "NotoSansTC-Regular.ttf",
            font_dir / "NotoSansCJKtc-Regular.otf",
        ]
        bold_candidates = [
            font_dir / "NotoSansTC-Bold.ttf",
            font_dir / "NotoSansCJKtc-Bold.otf",
        ]

        for font_path in [*regular_candidates, *bold_candidates]:
            if font_path.exists():
                font_manager.fontManager.addfont(str(font_path))

        if font_prop is None:
            regular_path = next((path for path in regular_candidates if path.exists()), None)
            if regular_path is not None:
                font_prop = font_manager.FontProperties(fname=str(regular_path))

    if font_prop is not None:
        plt.rcParams["font.family"] = font_prop.get_name()

    if font_prop is None:
        plt.rcParams["font.sans-serif"] = [
            "Noto Sans CJK TC",
            "Microsoft JhengHei",
            "Arial Unicode MS",
            "DejaVu Sans",
            "sans-serif",
        ]

    plt.rcParams["axes.unicode_minus"] = False
    return font_prop


def font_kwargs(font_prop=None) -> dict[str, Any]:
    return {"fontproperties": font_prop} if font_prop is not None else {}


def format_price(value: Any) -> str:
    """價格最多保留兩位小數，並移除尾端 0。"""
    try:
        number = float(value)
        if pd.isna(number):
            return "--"
        return f"{number:,.2f}".rstrip("0").rstrip(".")
    except Exception:
        return "--"


def add_quarter_grid(
    ax,
    color: str = GRID_COLOR,
    alpha: float = QUARTER_GRID_ALPHA,
) -> None:
    """在目前 Y 軸範圍的 25%／50%／75% 加入淡水平線。"""
    try:
        y_min, y_max = ax.get_ylim()
        if pd.isna(y_min) or pd.isna(y_max) or y_max <= y_min:
            return

        span = y_max - y_min
        for ratio in (0.25, 0.50, 0.75):
            ax.axhline(
                y_min + span * ratio,
                color=color,
                linewidth=GRID_LINEWIDTH,
                linestyle="--",
                alpha=alpha,
                zorder=0,
            )
    except Exception:
        pass


def apply_axis_style(
    ax,
    x_labelsize: int = AXIS_TICK_FONTSIZE,
    y_labelsize: int = AXIS_TICK_FONTSIZE,
    quarter_grid: bool = True,
) -> None:
    ax.set_axisbelow(True)
    ax.grid(
        True,
        axis="y",
        color=GRID_COLOR,
        linestyle=":",
        alpha=BASE_GRID_ALPHA,
        linewidth=GRID_LINEWIDTH,
    )
    if quarter_grid:
        add_quarter_grid(ax)
    ax.tick_params(axis="x", labelsize=x_labelsize)
    ax.tick_params(axis="y", labelsize=y_labelsize)


def hide_chart_spines(ax, hide: Iterable[str] = ("top", "right")) -> None:
    for spine in hide:
        if spine in ax.spines:
            ax.spines[spine].set_visible(False)


def _numeric_extrema(high_values, low_values) -> tuple[float | None, float | None]:
    high = pd.to_numeric(pd.Series(high_values), errors="coerce").dropna()
    low = pd.to_numeric(pd.Series(low_values), errors="coerce").dropna()
    high = high[high > 0]
    low = low[low > 0]

    if high.empty or low.empty:
        return None, None

    high_value = float(high.max())
    low_value = float(low.min())

    if not pd.notna(high_value) or not pd.notna(low_value):
        return None, None

    return high_value, low_value


def set_price_axis_to_visible_high_low(
    ax,
    high_values,
    low_values,
    tick_fontsize: int = AXIS_TICK_FONTSIZE,
    max_regular_ticks: int = 5,
) -> tuple[float | None, float | None]:
    """
    將 K 線價格軸上下緣鎖定為可見 K 棒最高／最低。

    同時強制把最高、最低放進 Y 軸刻度，讓 K 棒邊界能精確對應軸值。
    """
    high_value, low_value = _numeric_extrema(high_values, low_values)
    if high_value is None or low_value is None:
        return None, None

    if high_value <= low_value:
        # 無波動資料無法用同值建立 Matplotlib 軸；保留極小可視範圍。
        delta = max(abs(high_value) * 0.001, 0.01)
        ax.set_ylim(low_value - delta, high_value + delta)
        return high_value, low_value

    ax.set_ylim(low_value, high_value)
    ax.margins(y=0)

    locator = mticker.MaxNLocator(nbins=max_regular_ticks, min_n_ticks=3)
    regular_ticks = locator.tick_values(low_value, high_value)
    ticks = [
        float(value)
        for value in regular_ticks
        if low_value < float(value) < high_value
    ]
    ticks = sorted({low_value, *ticks, high_value})

    ax.yaxis.set_major_locator(mticker.FixedLocator(ticks))
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda value, _pos: format_price(value))
    )
    ax.tick_params(axis="y", labelsize=tick_fontsize)

    return high_value, low_value


def annotate_visible_high_low(
    ax,
    plot_df: pd.DataFrame,
    x_values,
    fontsize: int = HIGH_LOW_FONTSIZE,
    high_prefix: str = "高",
    low_prefix: str = "低",
) -> tuple[float | None, float | None]:
    """在軸內側標示可見範圍最高／最低，避免文字超出精確價格邊界。"""
    try:
        if plot_df is None or plot_df.empty:
            return None, None
        if "High" not in plot_df.columns or "Low" not in plot_df.columns:
            return None, None

        high_series = pd.to_numeric(plot_df["High"], errors="coerce")
        low_series = pd.to_numeric(plot_df["Low"], errors="coerce")
        high_series = high_series.where(high_series > 0)
        low_series = low_series.where(low_series > 0)
        if high_series.dropna().empty or low_series.dropna().empty:
            return None, None

        high_idx = high_series.idxmax()
        low_idx = low_series.idxmin()
        index_list = list(plot_df.index)
        high_pos = index_list.index(high_idx)
        low_pos = index_list.index(low_idx)
        high_y = float(high_series.loc[high_idx])
        low_y = float(low_series.loc[low_idx])

        total = max(len(index_list), 1)
        high_ha = "left" if high_pos <= 2 else ("right" if high_pos >= total - 3 else "center")
        low_ha = "left" if low_pos <= 2 else ("right" if low_pos >= total - 3 else "center")

        ax.annotate(
            f"{high_prefix} {format_price(high_y)}",
            xy=(x_values[high_pos], high_y),
            xytext=(0, -8),
            textcoords="offset points",
            ha=high_ha,
            va="top",
            fontsize=fontsize,
            fontweight="bold",
            color="#D32F2F",
            zorder=10,
            clip_on=True,
        )
        ax.annotate(
            f"{low_prefix} {format_price(low_y)}",
            xy=(x_values[low_pos], low_y),
            xytext=(0, 8),
            textcoords="offset points",
            ha=low_ha,
            va="bottom",
            fontsize=fontsize,
            fontweight="bold",
            color="#00A84F",
            zorder=10,
            clip_on=True,
        )
        return high_y, low_y
    except Exception as exc:
        print("DEBUG shared high low annotation failed", repr(exc), flush=True)
        return None, None
