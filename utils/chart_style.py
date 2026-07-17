from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
from matplotlib import font_manager


# 所有個股、大盤、期貨圖表的共用視覺規格。
AXIS_TICK_FONTSIZE = 11
HIGH_LOW_FONTSIZE = 12
INFO_FONTSIZE = 14

UP_COLOR = "#FF2D2D"
DOWN_COLOR = "#00B050"
FLAT_COLOR = "#666666"
CHART_BACKGROUND = "#F8F9FA"
GRID_COLOR = "#AEB6BF"

DEFAULT_CANDLE_WIDTH = 0.58
INTRADAY_VOLUME_ALPHA = 0.38
INTRADAY_VOLUME_WIDTH_RATIO = 0.62

FIGURE_SIZES = {
    "stock_instant": (9.2, 8.0),
    "stock_kline": (9.6, 8.0),
    "market_index": (9.4, 7.6),
    "market_future": (8.6, 5.6),
    "stock_future": (7.6, 5.6),
}


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
    alpha: float = 0.26,
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
                linewidth=0.8,
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
    ax.grid(True, axis="y", linestyle=":", alpha=0.12, linewidth=0.8)
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
