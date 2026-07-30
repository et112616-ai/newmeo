from __future__ import annotations

from datetime import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import pandas as pd
import matplotlib.ticker as mticker
from matplotlib import font_manager

from services.upload_service import publish_figure
from utils.chart_style import (
    AXIS_TICK_FONTSIZE,
    CHART_BACKGROUND,
    DEFAULT_CANDLE_WIDTH,
    FIGURE_SIZES,
    HIGH_LOW_FONTSIZE,
    INTRADAY_VOLUME_ALPHA,
    INTRADAY_VOLUME_WIDTH_RATIO,
    UNIFIED_KLINE_STYLE_VERSION,
    add_moving_averages,
    annotate_visible_high_low,
    apply_axis_style,
    configure_chart_font,
    draw_candles,
    draw_moving_average_lines,
    draw_volume_bars,
    format_price,
    get_kline_display_rows,
    get_kline_preset,
    hide_chart_spines,
    set_price_axis_to_visible_high_low,
)
from utils.formatter import normalize_time_frame


plt.rcParams["axes.unicode_minus"] = False

INTRADAY_AXIS_FIX_VERSION = "2026-07-20-v3-ACTUAL-DAY-EXTREMA"
INTRADAY_VOLUME_FIX_VERSION = "2026-07-16-v1-COMPACT-5M-VOLUME"
KLINE_DISPLAY_FIX_VERSION = "2026-07-29-v6.1-STOCK-SIX-MA"
STOCK_KLINE_PRESET = get_kline_preset("stock")

# LINE 會把 960px 圖表縮到 Flex 卡片寬度；15pt 在手機上仍過小。
# 保持原本兩行配置與行距，只放大文字本身。
KLINE_INFO_MA_FONTSIZE = int(STOCK_KLINE_PRESET["info_ma_fontsize"])
KLINE_INFO_OHLC_FONTSIZE = int(STOCK_KLINE_PRESET["info_ohlc_fontsize"])

BASE_DIR = Path(__file__).resolve().parents[1]
FONT_PATH = BASE_DIR / "assets" / "fonts" / "NotoSansTC-Regular.ttf"

CHART_FONT_PROP = configure_chart_font(BASE_DIR)


def _font_kwargs() -> dict:
    if CHART_FONT_PROP is not None:
        return {"fontproperties": CHART_FONT_PROP}
    return {}

def _empty_chart(title: str, message: str) -> str:
    fig, ax = plt.subplots(figsize=(7, 5), dpi=120, facecolor="white")
    ax.axis("off")
    ax.text(0.5, 0.55, "No Data", ha="center", va="center", fontsize=16, fontweight="bold")
    ax.text(0.5, 0.45, "Data unavailable", ha="center", va="center", fontsize=11)
    return publish_figure(fig, "empty")

def _set_tw_stock_intraday_axis(ax, df: pd.DataFrame) -> None:
    """
    現貨盤中圖固定顯示 09:00 ~ 13:30。
    前提：df.index 已經是台北時間。
    """
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return

    trade_date = df.index[-1].date()

    ax.set_xlim(
        pd.Timestamp.combine(trade_date, time(9, 0)),
        pd.Timestamp.combine(trade_date, time(13, 30)),
    )
    ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

def _get_reference_price(df: pd.DataFrame) -> float:
    """
    取得即時圖參考價 / 昨收。

    優先順序：
    1. df.attrs["previous_close"]：Yahoo chart API meta 的真正昨收。
    2. df.attrs["chart_previous_close"]
    3. df.attrs["regular_market_previous_close"]
    4. df.attrs["prev_close"]
    5. df 欄位 previous_close / prev_close / Adj Close 等。
    6. 最後才 fallback 第一筆 Close。

    注意：
    不能優先用第一筆 Close，否則遇到跳空開盤會把開盤附近價格誤當昨收。
    """
    if df is None or df.empty:
        return 0.0

    # 1. 先讀 DataFrame attrs
    attr_keys = [
        "previous_close",
        "chart_previous_close",
        "regular_market_previous_close",
        "prev_close",
        "reference_price",
        "ref_price",
    ]

    for key in attr_keys:
        try:
            value = df.attrs.get(key)

            if value is None:
                continue

            value = float(value)

            if value > 0:
                return value

        except Exception:
            continue

    # 2. 再讀欄位
    column_keys = [
        "previous_close",
        "PreviousClose",
        "prev_close",
        "PrevClose",
        "reference_price",
        "ReferencePrice",
        "ref_price",
        "RefPrice",
    ]

    for col in column_keys:
        if col not in df.columns:
            continue

        try:
            values = pd.to_numeric(df[col], errors="coerce").dropna()

            if values.empty:
                continue

            value = float(values.iloc[-1])

            if value > 0:
                return value

        except Exception:
            continue

    # 3. 最後 fallback：第一筆 Close
    try:
        close = pd.to_numeric(df["Close"], errors="coerce").dropna()

        if not close.empty:
            value = float(close.iloc[0])

            if value > 0:
                return value

    except Exception:
        pass

    return 0.0

def _set_centered_price_axis(ax, df: pd.DataFrame) -> float:
    """
    設定台股即時圖價格軸與右側漲跌幅軸。

    重要：
    - 下緣直接使用今日實際最低價，不再由參考價乘上漲跌幅推算，
      也不額外減去 padding，避免出現 190.8 這類非有效跳動價格。
    - 上緣至少包含今日實際最高價；若昨收／參考價高於今日最高，
      則保留參考價，讓參考線與右側漲跌幅軸仍可正確顯示。
    - 今日高低價取自 df 的 High / Low；Shioaji snapshot 已補入 df 時，
      會自然優先反映永豐當日高低價。
    """
    ref_price = _get_reference_price(df)

    try:
        ref_price = float(ref_price)
    except Exception:
        ref_price = 0.0

    debug_values: dict[str, float] = {}

    for col in ["Open", "High", "Low", "Close"]:
        if col not in df.columns:
            continue

        values = pd.to_numeric(df[col], errors="coerce").dropna()

        if values.empty:
            continue

        col_min = float(values.min())
        col_max = float(values.max())

        if col == "Open":
            debug_values["open"] = float(values.iloc[0])
        elif col == "High":
            debug_values["high"] = col_max
        elif col == "Low":
            debug_values["low"] = col_min
        elif col == "Close":
            debug_values["close_min"] = col_min
            debug_values["close_max"] = col_max

    day_low = debug_values.get("low")
    day_high = debug_values.get("high")

    if not day_low or not day_high:
        return ref_price

    ymin = float(day_low)
    ymax = max(float(day_high), float(ref_price or 0.0))

    if ymax <= ymin:
        # 極少數無波動資料仍需留一個合法繪圖範圍。
        ymax = ymin + max(abs(ymin) * 0.001, 0.01)

    ax.set_ylim(ymin, ymax)

    # 一般刻度之外，強制加入當日最低與上緣，確保邊界能對應左側 Y 軸數值。
    locator = mticker.MaxNLocator(nbins=6, min_n_ticks=5)
    regular_ticks = locator.tick_values(ymin, ymax)
    price_ticks = sorted({
        ymin,
        *[
            float(value)
            for value in regular_ticks
            if ymin < float(value) < ymax
        ],
        ymax,
    })
    ax.yaxis.set_major_locator(mticker.FixedLocator(price_ticks))
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda value, _pos: format_price(value))
    )

    ax.axhline(
        ref_price,
        linestyle="--",
        linewidth=1.1,
        color="#7F8C8D",
        alpha=0.85,
        zorder=1,
    )

    if ref_price > 0:
        def price_to_pct(price):
            return (price - ref_price) / ref_price * 100

        def pct_to_price(pct):
            return ref_price * (1 + pct / 100)

        secax = ax.secondary_yaxis(
            "right",
            functions=(price_to_pct, pct_to_price),
        )

        secax.yaxis.set_major_formatter(
            mticker.FuncFormatter(lambda value, pos: f"{value:+.1f}%")
        )
        secax.yaxis.set_major_locator(
            mticker.FixedLocator([price_to_pct(value) for value in price_ticks])
        )
        secax.tick_params(axis="y", labelsize=DEFAULT_AXIS_TICK_FONTSIZE)

    print(
        "DEBUG intraday axis bounds",
        "| version =", INTRADAY_AXIS_FIX_VERSION,
        "| ref =", ref_price,
        "| open =", debug_values.get("open"),
        "| high =", debug_values.get("high"),
        "| low =", debug_values.get("low"),
        "| close_min =", debug_values.get("close_min"),
        "| close_max =", debug_values.get("close_max"),
        "| ymin =", round(ymin, 4),
        "| ymax =", round(ymax, 4),
        flush=True,
    )

    return ref_price

def _fmt_high_low_price(value) -> str:
    return format_price(value)

DEFAULT_AXIS_TICK_FONTSIZE = AXIS_TICK_FONTSIZE


def _add_quarter_grid(ax, color: str = "#AEB6BF", alpha: float = 0.26) -> None:
    """在圖上加入 25% / 50% / 75% 的淡水平引導線。"""
    try:
        y_min, y_max = ax.get_ylim()

        if pd.isna(y_min) or pd.isna(y_max) or y_max <= y_min:
            return

        span = y_max - y_min

        if span <= 0:
            return

        for ratio in (0.25, 0.50, 0.75):
            level = y_min + span * ratio
            ax.axhline(
                level,
                color=color,
                linewidth=0.8,
                linestyle="--",
                alpha=alpha,
                zorder=0,
            )

    except Exception:
        pass


def _apply_axis_style(ax, x_labelsize: int = DEFAULT_AXIS_TICK_FONTSIZE, y_labelsize: int = DEFAULT_AXIS_TICK_FONTSIZE) -> None:
    apply_axis_style(ax, x_labelsize=x_labelsize, y_labelsize=y_labelsize)


def _hide_top_right_spines(ax) -> None:
    hide_chart_spines(ax)


def _annotate_high_low(
    ax,
    plot_df,
    x_values,
    fontsize: int = 12,
) -> None:
    """
    在目前圖表範圍內標出最高價與最低價。
    plot_df 需要有 High / Low 欄位。
    x_values 對應 plot_df 的 x 座標。
    """
    return annotate_visible_high_low(
        ax,
        plot_df,
        x_values,
        fontsize=fontsize,
    )


def _infer_intraday_interval_minutes(df: pd.DataFrame) -> float:
    """推估盤中資料的時間間隔，供成交量顯示密度判斷。"""
    try:
        if df is None or len(df) < 2 or not isinstance(df.index, pd.DatetimeIndex):
            return 0.0

        index = pd.DatetimeIndex(df.index).sort_values().unique()

        if len(index) < 2:
            return 0.0

        diffs = pd.Series(index[1:] - index[:-1]).dt.total_seconds() / 60.0
        diffs = diffs[(diffs > 0) & (diffs <= 120)]

        if diffs.empty:
            return 0.0

        return float(diffs.median())

    except Exception:
        return 0.0


def _prepare_intraday_volume_display(df: pd.DataFrame) -> tuple[pd.DataFrame, float, str]:
    """
    準備即時圖的成交量顯示資料。

    - 原始為 1 分資料時，價格折線仍保留全部 1 分資料，成交量則彙總成 5 分鐘，
      避免約 270 根柱子擠成一片。
    - 原始已是 5 分或更大週期時，直接使用原資料。
    - 回傳：顯示用 DataFrame、顯示間隔分鐘數、顯示模式。
    """
    if df is None or df.empty:
        return pd.DataFrame(), 0.0, "empty"

    attrs_backup = dict(getattr(df, "attrs", {}) or {})
    work = df.copy()

    if not isinstance(work.index, pd.DatetimeIndex):
        try:
            work.index = pd.to_datetime(work.index, errors="coerce")
            work = work[~work.index.isna()].copy()
        except Exception:
            return pd.DataFrame(), 0.0, "invalid_index"

    if work.empty:
        return work, 0.0, "empty"

    try:
        work = work.sort_index()
        work = work[~work.index.duplicated(keep="last")].copy()
    except Exception:
        pass

    for col in ["Open", "Close", "Volume"]:
        if col not in work.columns:
            if col == "Volume":
                work[col] = 0.0
            else:
                return pd.DataFrame(), 0.0, "missing_ohlc"

        work[col] = pd.to_numeric(work[col], errors="coerce")

    work["Volume"] = work["Volume"].fillna(0.0)
    input_interval = _infer_intraday_interval_minutes(work)

    # 1 分資料一律把成交量彙總成 5 分鐘；價格折線本身仍使用原 df，不受影響。
    if 0 < input_interval <= 1.5:
        volume_df = work.resample(
            "5min",
            label="left",
            closed="left",
        ).agg(
            {
                "Open": "first",
                "Close": "last",
                "Volume": "sum",
            }
        )
        volume_df = volume_df.dropna(subset=["Open", "Close"]).copy()
        display_interval = 5.0
        mode = "1m_to_5m"
    else:
        volume_df = work[["Open", "Close", "Volume"]].copy()
        display_interval = input_interval if input_interval > 0 else 5.0
        mode = "native"

    try:
        volume_df.attrs.update(attrs_backup)
    except Exception:
        pass

    return volume_df, display_interval, mode

def generate_instant_chart(df: pd.DataFrame, stock_id: str, stock_name: str) -> str:
    if df is None or df.empty:
        return _empty_chart(f"{stock_id}", "No intraday data")

    df = df.copy()

    # ===== 取價格欄位 =====
    close = df["Close"].astype(float)
    latest = float(close.iloc[-1])

    ref_price = _get_reference_price(df)

    try:
        ref_price = float(ref_price)
    except Exception:
        ref_price = latest

    try:
        prev_close = float(df.attrs.get("previous_close") or ref_price or latest)
    except Exception:
        prev_close = latest

    try:
        open_price = float(df["Open"].astype(float).iloc[0])
    except Exception:
        open_price = latest

    try:
        high_price = float(df["High"].astype(float).max())
    except Exception:
        high_price = latest

    try:
        low_price = float(df["Low"].astype(float).min())
    except Exception:
        low_price = latest

    try:
        total_volume = float(df["Volume"].fillna(0).astype(float).sum())
    except Exception:
        total_volume = 0.0

    # 即時分K這邊目前預設 Volume 是「股」，統一換算成「張」。
    volume_lots = total_volume / 1000.0

    def _fmt_price(value) -> str:
        try:
            return f"{float(value):,.2f}"
        except Exception:
            return "--"

    def _fmt_volume_lots(value) -> str:
        try:
            return f"{float(value):,.0f} 張"
        except Exception:
            return "--"

    # ===== 畫布：資訊列 + 主圖 + 成交量 =====
    fig = plt.figure(figsize=FIGURE_SIZES["stock_instant"], dpi=140, facecolor="white")
    gs = gridspec.GridSpec(
        3,
        1,
        height_ratios=[0.95, 5.35, 0.72],
        hspace=0.07,
    )

    ax_info = fig.add_subplot(gs[0])
    ax = fig.add_subplot(gs[1])
    ax_v = fig.add_subplot(gs[2], sharex=ax)

    ax_info.axis("off")
    ax.set_facecolor(CHART_BACKGROUND)
    ax_v.set_facecolor(CHART_BACKGROUND)

    # ===== 上方資訊列：2 排 x 3 欄 =====
    info_items = [
        ("昨收", _fmt_price(prev_close)),
        ("開盤", _fmt_price(open_price)),
        ("最高", _fmt_price(high_price)),
        ("最低", _fmt_price(low_price)),
        ("參考", _fmt_price(ref_price)),
        ("成交量", _fmt_volume_lots(volume_lots)),
    ]

    positions = [
        (0.00, 0.68),
        (0.34, 0.68),
        (0.68, 0.68),
        (0.00, 0.25),
        (0.34, 0.25),
        (0.68, 0.25),
    ]

    for (label, value), (x, y) in zip(info_items, positions):
        ax_info.text(
            x,
            y,
            f"{label} {value}",
            ha="left",
            va="center",
            fontsize=18,
            fontweight="bold",
            color="#333333",
            transform=ax_info.transAxes,
            **_get_font_kwargs_safe(),
        )

    # ===== 主圖：即時折線 =====
    line_color = "#E74C3C" if latest >= ref_price else "#27AE60"

    # 分鐘折線原本只畫 Close，第一點可能不是今日開盤價。
    # 在最前面補一個 09:00 的真正開盤價，讓走勢從 Open 出發。
    line_index = pd.DatetimeIndex(df.index)
    line_values = close.astype(float).tolist()

    try:
        first_ts = pd.Timestamp(line_index[0])
        session_open_ts = first_ts.normalize() + pd.Timedelta(hours=9)

        if first_ts < session_open_ts:
            session_open_ts = first_ts

        line_index = pd.DatetimeIndex([session_open_ts]).append(line_index)
        line_values = [float(open_price)] + line_values
    except Exception:
        line_values = close.astype(float).tolist()

    line_series = pd.Series(line_values, index=line_index, dtype="float64")
    line_array = line_series.to_numpy(dtype="float64")

    ax.plot(
        line_series.index,
        line_array,
        linewidth=2.6,
        color=line_color,
        zorder=3,
    )

    ax.fill_between(
        line_series.index,
        line_array,
        ref_price,
        where=line_array >= ref_price,
        alpha=0.12,
        color="#E74C3C",
        interpolate=True,
        zorder=2,
    )

    ax.fill_between(
        line_series.index,
        line_array,
        ref_price,
        where=line_array < ref_price,
        alpha=0.10,
        color="#27AE60",
        interpolate=True,
        zorder=2,
    )

    print(
        "DEBUG intraday opening point",
        "| version =", INTRADAY_AXIS_FIX_VERSION,
        "| stock_id =", stock_id,
        "| opening_time =", str(line_series.index[0]),
        "| opening_price =", float(line_array[0]),
        "| first_close_time =", str(df.index[0]),
        "| first_close =", float(close.iloc[0]),
        flush=True,
    )

    # Y 軸採今日 OHLC + 昨收範圍，右側顯示相對昨收漲跌幅。
    _set_tw_stock_intraday_axis(ax, df)
    _set_centered_price_axis(ax, df)

    _apply_axis_style(ax, x_labelsize=12, y_labelsize=12)

    # ===== 成交量圖 =====
    # 價格線保留完整分鐘資料；若原始是 1 分資料，只把成交量彙總成 5 分鐘顯示。
    volume_df, display_interval_minutes, volume_mode = _prepare_intraday_volume_display(df)

    if volume_df is None or volume_df.empty:
        volume_df = df[["Open", "Close", "Volume"]].copy()
        display_interval_minutes = max(_infer_intraday_interval_minutes(volume_df), 1.0)
        volume_mode = "fallback"

    vol_colors = []

    for _, row in volume_df.iterrows():
        try:
            o = float(row["Open"])
            c = float(row["Close"])
            # 使用較柔和的漲跌色，並搭配 alpha，避免成交量搶過價格線。
            vol_colors.append("#D95F59" if c >= o else "#49A978")
        except Exception:
            vol_colors.append("#D95F59")

    volume_lot_series = (
        pd.to_numeric(volume_df["Volume"], errors="coerce")
        .fillna(0.0)
        .astype(float)
        / 1000.0
    )

    # Matplotlib 日期座標的寬度單位是「天」。成交量柱刻意留縫並降低透明度。
    bar_width = max(display_interval_minutes * INTRADAY_VOLUME_WIDTH_RATIO / 1440.0, 1.0 / 1440.0)
    bar_width = min(bar_width, 12.0 / 1440.0)

    ax_v.bar(
        volume_df.index,
        volume_lot_series,
        width=bar_width,
        color=vol_colors,
        alpha=INTRADAY_VOLUME_ALPHA,
        edgecolor="none",
        align="center",
        zorder=2,
    )

    ax_v.set_ylabel("成交量(張)", fontsize=12, **_get_font_kwargs_safe())
    _apply_axis_style(ax_v, x_labelsize=12, y_labelsize=12)
    ax_v.yaxis.set_major_locator(mticker.MaxNLocator(nbins=4, min_n_ticks=3))
    ax_v.margins(y=0.18)

    ax_v.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
    ax_v.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    print(
        "DEBUG intraday volume display",
        "| version =", INTRADAY_VOLUME_FIX_VERSION,
        "| stock_id =", stock_id,
        "| mode =", volume_mode,
        "| source_rows =", len(df),
        "| display_rows =", len(volume_df),
        "| display_interval_min =", round(display_interval_minutes, 3),
        "| bar_width_days =", round(bar_width, 7),
        flush=True,
    )

    plt.setp(ax.get_xticklabels(), visible=False)

    _hide_top_right_spines(ax)
    _hide_top_right_spines(ax_v)

    fig.subplots_adjust(**STOCK_KLINE_PRESET["subplots_adjust"])

    try:
        image_url = publish_figure(fig, f"{stock_id}_instant")

        print(
            "DEBUG publish instant figure",
            "| stock_id =",
            stock_id,
            "| image_url =",
            image_url,
            flush=True,
        )

        return image_url or ""

    except Exception:
        import traceback

        print(
            "DEBUG publish instant figure failed",
            "| stock_id =",
            stock_id,
            flush=True,
        )
        print(traceback.format_exc(), flush=True)

        return ""

    finally:
        plt.close(fig)

def _to_lots(volume_value, volume_unit: str = "shares") -> float:
    """
    成交量統一轉成「張」。

    volume_unit:
    - shares / share / 股：代表原始資料是股，除以 1000
    - lots / lot / 張：代表原始資料已經是張，不除
    """
    try:
        value = float(volume_value)
    except Exception:
        return 0.0

    unit = str(volume_unit or "shares").lower()

    if unit in {"lots", "lot", "張"}:
        return value

    return value / 1000.0
    
def _fmt_lots(value) -> str:
    try:
        return f"{float(value):,.0f} 張"
    except Exception:
        return "--"
    

def _fmt_ma_value(value) -> str:
    try:
        if value is None or pd.isna(value):
            return "--"
        return f"{float(value):.2f}"
    except Exception:
        return "--"


def _get_font_kwargs_safe() -> dict:
    try:
        return _font_kwargs()
    except Exception:
        return {}

def _setup_chinese_font():
    """
    設定中文字型，避免圖表中文字變方塊。
    """
    try:
        from pathlib import Path

        import matplotlib.font_manager as fm
        import matplotlib.pyplot as plt

        font_path = Path("assets/fonts/NotoSansTC-Regular.ttf")

        if font_path.exists():
            fm.fontManager.addfont(str(font_path))
            font_prop = fm.FontProperties(fname=str(font_path))
            plt.rcParams["font.family"] = font_prop.get_name()

        plt.rcParams["axes.unicode_minus"] = False

    except Exception as exc:
        print(
            "DEBUG chart font setup failed",
            "| error =",
            repr(exc),
            flush=True,
        )

def _prepare_kline_work_df(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """
    K 線計算前整理資料。

    修正重點：
    1. D / W / M 若同一天有兩筆資料，保留最後一筆。
       這會處理「日 K 歷史列 + Shioaji 即時 snapshot 列」重複造成 MA 失真的問題。
    2. MA 一律用未還原 Close 計算。
    3. Open / High / Low / Close / Volume 轉成數值。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    attrs_backup = dict(getattr(df, "attrs", {}) or {})
    work_df = df.copy()

    if not isinstance(work_df.index, pd.DatetimeIndex):
        try:
            work_df.index = pd.to_datetime(work_df.index, errors="coerce")
            work_df = work_df[~work_df.index.isna()].copy()
        except Exception:
            pass

    if work_df.empty:
        return pd.DataFrame()

    try:
        work_df = work_df.sort_index()
    except Exception:
        pass

    normalized_tf = normalize_time_frame(tf)

    if normalized_tf in {"D", "W", "M"} and isinstance(work_df.index, pd.DatetimeIndex):
        try:
            before_rows = len(work_df)

            # 同一交易日若同時存在 00:00 日K與 13:xx 即時快照，
            # 保留最後一筆，避免 MA 把同一天算兩次。
            date_keys = pd.Index(work_df.index.normalize())
            keep_mask = ~date_keys.duplicated(keep="last")
            work_df = work_df.loc[keep_mask].copy()

            after_rows = len(work_df)

            if before_rows != after_rows:
                print(
                    "DEBUG kline dedupe same_date",
                    "| before_rows =",
                    before_rows,
                    "| after_rows =",
                    after_rows,
                    "| removed =",
                    before_rows - after_rows,
                    flush=True,
                )

        except Exception as exc:
            print(
                "DEBUG kline dedupe same_date failed",
                "| error =",
                repr(exc),
                flush=True,
            )

    required_cols = ["Open", "High", "Low", "Close", "Volume"]

    for col in required_cols:
        if col not in work_df.columns:
            if col == "Volume":
                work_df[col] = 0
            else:
                return pd.DataFrame()

        work_df[col] = pd.to_numeric(work_df[col], errors="coerce")

    ohlc_cols = ["Open", "High", "Low", "Close"]
    before_sanitize_rows = len(work_df)

    # 0 或負數不可能是有效台股 OHLC。也排除高低價關係顛倒的資料列，
    # 避免週／月重採樣來源中的異常值被畫成「低 0」或扭曲 Y 軸。
    # 不使用前值、開收盤價或漲跌幅補值，確保高低點仍只來自真實行情。
    valid_ohlc_mask = work_df[ohlc_cols].notna().all(axis=1)
    valid_ohlc_mask &= work_df[ohlc_cols].gt(0).all(axis=1)
    valid_ohlc_mask &= work_df["High"].ge(
        work_df[["Open", "Low", "Close"]].max(axis=1)
    )
    valid_ohlc_mask &= work_df["Low"].le(
        work_df[["Open", "High", "Close"]].min(axis=1)
    )
    work_df = work_df.loc[valid_ohlc_mask].copy()

    invalid_rows = before_sanitize_rows - len(work_df)
    if invalid_rows:
        print(
            "DEBUG kline sanitize",
            "| version =", KLINE_DISPLAY_FIX_VERSION,
            "| tf =", normalized_tf,
            "| before_rows =", before_sanitize_rows,
            "| after_rows =", len(work_df),
            "| invalid_rows =", invalid_rows,
            flush=True,
        )

    try:
        work_df.attrs.update(attrs_backup)
    except Exception:
        pass

    return work_df


def generate_kline_chart(df: pd.DataFrame, stock_id: str, stock_name: str, tf: str) -> str:
    if df is None or df.empty:
        return ""

    _setup_chinese_font()

    tf = normalize_time_frame(tf)
    work_df = _prepare_kline_work_df(df, tf)

    print(
        "DEBUG kline display active",
        "| version =", KLINE_DISPLAY_FIX_VERSION,
        "| style_version =", UNIFIED_KLINE_STYLE_VERSION,
        "| stock_id =", stock_id,
        "| tf =", tf,
        "| source_rows =", len(df),
        "| valid_rows =", len(work_df),
        "| info_font =", f"{KLINE_INFO_MA_FONTSIZE}/{KLINE_INFO_OHLC_FONTSIZE}",
        flush=True,
    )

    if work_df.empty:
        return ""

    volume_unit = str(getattr(work_df, "attrs", {}).get("volume_unit") or "shares").lower()

    # MA 必須先用完整資料算，再裁切顯示範圍。
    # min_periods 用完整 period，避免資料不足時硬算出失真的 MA。
    close_for_ma = pd.to_numeric(work_df["Close"], errors="coerce")

    work_df = add_moving_averages(
        work_df,
        STOCK_KLINE_PRESET["ma_periods"],
    )
    display_rows = get_kline_display_rows("stock", tf)
    plot_df = work_df.tail(display_rows).copy()

    if plot_df.empty:
        return ""

    latest = work_df.iloc[-1]

    latest_open = float(latest["Open"])
    latest_high = float(latest["High"])
    latest_low = float(latest["Low"])
    latest_close = float(latest["Close"])
    latest_volume = _to_lots(latest["Volume"], volume_unit)

    try:
        latest_date = work_df.index[-1].strftime("%Y-%m-%d")
    except Exception:
        latest_date = str(work_df.index[-1])

    prev_close = None

    if len(work_df) >= 2:
        prev_close = float(work_df["Close"].iloc[-2])

    change = 0.0
    pct = 0.0

    if prev_close and prev_close != 0:
        change = latest_close - prev_close
        pct = change / prev_close * 100

    ma_values = {
        f"MA{period}": latest.get(f"MA{period}")
        for period in STOCK_KLINE_PRESET["ma_periods"]
    }

    if str(stock_id) == "5274" and tf == "D":
        try:
            debug_tail = [float(x) for x in close_for_ma.dropna().tail(25).tolist()]

            print(
                "DEBUG kline MA check",
                "| stock_id =", stock_id,
                "| tf =", tf,
                "| rows =", len(work_df),
                "| latest_date =", latest_date,
                "| latest_close =", latest_close,
                "| volume =", latest_volume,
                "| ma_values =", ma_values,
                "| close_tail25 =", debug_tail,
                flush=True,
            )

        except Exception as exc:
            print(
                "DEBUG kline MA check failed",
                "| stock_id =", stock_id,
                "| error =", repr(exc),
                flush=True,
            )

    def _fmt_price(v):
        try:
            return f"{float(v):,.2f}"
        except Exception:
            return "--"

    def _fmt_ma(v):
        try:
            if v is None or pd.isna(v):
                return "--"
            return f"{float(v):.2f}"
        except Exception:
            return "--"

    fig = plt.figure(
        figsize=STOCK_KLINE_PRESET["figure_size"],
        dpi=int(STOCK_KLINE_PRESET["dpi"]),
        facecolor="white",
    )
    gs = gridspec.GridSpec(
        3,
        1,
        height_ratios=STOCK_KLINE_PRESET["height_ratios"],
        hspace=0.09,
    )

    ax_info = fig.add_subplot(gs[0])
    ax_k = fig.add_subplot(gs[1])
    ax_v = fig.add_subplot(gs[2], sharex=ax_k)

    ax_info.set_facecolor("white")
    ax_info.axis("off")

    font_kwargs = _get_font_kwargs_safe()

    # 與大盤一致：六組 MA 固定為 3 欄 × 2 排。
    # 個股另保留第三排的開高低量，避免六組數字被壓成過小字體。
    ma_positions = [
        ("MA5", 0.16, 0.77),
        ("MA12", 0.50, 0.77),
        ("MA22", 0.84, 0.77),
        ("MA30", 0.16, 0.45),
        ("MA66", 0.50, 0.45),
        ("MA120", 0.84, 0.45),
    ]
    for ma_name, x_pos, y_pos in ma_positions:
        ma_color = STOCK_KLINE_PRESET["ma_styles"][ma_name][0]
        ax_info.text(
            x_pos,
            y_pos,
            f"{ma_name} {_fmt_ma(ma_values.get(ma_name))}",
            fontsize=KLINE_INFO_MA_FONTSIZE,
            fontweight="bold",
            color=ma_color,
            ha="center",
            va="center",
            transform=ax_info.transAxes,
            **font_kwargs,
        )

    ax_info.text(
        0.00,
        0.12,
        f"開 {_fmt_price(latest_open)}  高 {_fmt_price(latest_high)}  低 {_fmt_price(latest_low)}  量 {_fmt_lots(latest_volume)}",
        fontsize=KLINE_INFO_OHLC_FONTSIZE,
        color="#444444",
        ha="left",
        va="center",
        transform=ax_info.transAxes,
        **font_kwargs,
    )

    ax_k.set_facecolor(CHART_BACKGROUND)
    ax_v.set_facecolor(CHART_BACKGROUND)

    x_values, candle_colors = draw_candles(
        ax_k,
        plot_df,
        candle_width=DEFAULT_CANDLE_WIDTH,
    )
    plot_volume_lots = [
        _to_lots(value, volume_unit)
        for value in plot_df["Volume"].tolist()
    ]
    draw_volume_bars(
        ax_v,
        plot_volume_lots,
        candle_colors,
        x_values=x_values,
        candle_width=DEFAULT_CANDLE_WIDTH,
    )
    draw_moving_average_lines(
        ax_k,
        plot_df,
        STOCK_KLINE_PRESET["ma_styles"],
        x_values=x_values,
    )

    # K 線價格軸上下緣固定為畫面可見 K 棒的最高／最低，並強制加入 Y 軸刻度。
    ax_k.margins(x=0.025, y=0)
    set_price_axis_to_visible_high_low(
        ax_k,
        plot_df["High"],
        plot_df["Low"],
        tick_fontsize=AXIS_TICK_FONTSIZE,
    )

    # 個股分 K 與日／週／月 K：標示目前顯示範圍內的最高、最低價。
    if tf in {"1m", "5m", "15m", "30m", "60m", "D", "W", "M"}:
        _annotate_high_low(
            ax_k,
            plot_df,
            x_values,
            fontsize=HIGH_LOW_FONTSIZE,
        )

    _apply_axis_style(ax_k)
    _apply_axis_style(ax_v)

    if tf in {"1m", "5m", "15m", "30m", "60m"}:
        index_dates = [idx.date() for idx in plot_df.index]
        multi_day = bool(index_dates and min(index_dates) != max(index_dates))

        if multi_day:
            labels = [idx.strftime("%m/%d\n%H:%M") for idx in plot_df.index]
        else:
            labels = [idx.strftime("%H:%M") for idx in plot_df.index]
    elif tf == "D":
        labels = [idx.strftime("%m/%d") for idx in plot_df.index]
    elif tf in {"W", "M"}:
        labels = [idx.strftime("%Y/%m") for idx in plot_df.index]
    else:
        labels = [str(idx) for idx in plot_df.index]

    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    if labels and len(labels) - 1 not in ticks:
        ticks.append(len(labels) - 1)

    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels([labels[i] for i in ticks], rotation=0, fontsize=12, **font_kwargs)

    plt.setp(ax_k.get_xticklabels(), visible=False)

    ax_k.tick_params(axis="y", labelsize=12)
    ax_v.tick_params(axis="y", labelsize=12)

    _hide_top_right_spines(ax_k)
    _hide_top_right_spines(ax_v)

    fig.subplots_adjust(left=0.10, right=0.97, top=0.98, bottom=0.085, hspace=0.09)

    try:
        return publish_figure(fig, f"{stock_id}_{tf}_kline")
    finally:
        plt.close(fig)

def generate_fibonacci_chart(
    df: pd.DataFrame,
    stock_id: str,
    stock_name: str,
    tf: str = "D",
) -> str:
    if df is None or df.empty:
        return ""

    _setup_chinese_font()

    tf = normalize_time_frame(tf)
    work_df = _prepare_kline_work_df(df, tf)

    print(
        "DEBUG kline display active",
        "| version =", KLINE_DISPLAY_FIX_VERSION,
        "| style_version =", UNIFIED_KLINE_STYLE_VERSION,
        "| stock_id =", stock_id,
        "| tf =", tf,
        "| source_rows =", len(df),
        "| valid_rows =", len(work_df),
        "| info_font =", f"{KLINE_INFO_MA_FONTSIZE}/{KLINE_INFO_OHLC_FONTSIZE}",
        flush=True,
    )

    if work_df.empty:
        return ""

    volume_unit = str(getattr(work_df, "attrs", {}).get("volume_unit") or "shares").lower()

    # MA 必須先用完整資料算，再裁切顯示範圍。
    # min_periods 用完整 period，避免資料不足時硬算出失真的 MA。
    close_for_ma = pd.to_numeric(work_df["Close"], errors="coerce")

    work_df = add_moving_averages(
        work_df,
        STOCK_KLINE_PRESET["ma_periods"],
    )
    display_rows = get_kline_display_rows("stock", tf)
    plot_df = work_df.tail(display_rows).copy()
    fib_df = work_df.tail(120)

    highest = fib_df["High"].max()
    lowest = fib_df["Low"].min()

    diff = highest-lowest

    fib_levels={
        "100%":highest,
        "78.6%":highest-diff*0.214,
        "61.8%":highest-diff*0.382,
        "50%":highest-diff*0.5,
        "38.2%":highest-diff*0.618,
        "23.6%":highest-diff*0.764,
        "0%":lowest,
    }

    if plot_df.empty:
        return ""

    latest = work_df.iloc[-1]

    latest_open = float(latest["Open"])
    latest_high = float(latest["High"])
    latest_low = float(latest["Low"])
    latest_close = float(latest["Close"])
    latest_volume = _to_lots(latest["Volume"], volume_unit)

    try:
        latest_date = work_df.index[-1].strftime("%Y-%m-%d")
    except Exception:
        latest_date = str(work_df.index[-1])

    prev_close = None

    if len(work_df) >= 2:
        prev_close = float(work_df["Close"].iloc[-2])

    change = 0.0
    pct = 0.0

    if prev_close and prev_close != 0:
        change = latest_close - prev_close
        pct = change / prev_close * 100

    ma_values = {
        f"MA{period}": latest.get(f"MA{period}")
        for period in STOCK_KLINE_PRESET["ma_periods"]
    }

    if str(stock_id) == "5274" and tf == "D":
        try:
            debug_tail = [float(x) for x in close_for_ma.dropna().tail(25).tolist()]

            print(
                "DEBUG kline MA check",
                "| stock_id =", stock_id,
                "| tf =", tf,
                "| rows =", len(work_df),
                "| latest_date =", latest_date,
                "| latest_close =", latest_close,
                "| volume =", latest_volume,
                "| ma_values =", ma_values,
                "| close_tail25 =", debug_tail,
                flush=True,
            )

        except Exception as exc:
            print(
                "DEBUG kline MA check failed",
                "| stock_id =", stock_id,
                "| error =", repr(exc),
                flush=True,
            )

    def _fmt_price(v):
        try:
            return f"{float(v):,.2f}"
        except Exception:
            return "--"

    def _fmt_ma(v):
        try:
            if v is None or pd.isna(v):
                return "--"
            return f"{float(v):.2f}"
        except Exception:
            return "--"

    fig = plt.figure(
        figsize=STOCK_KLINE_PRESET["figure_size"],
        dpi=int(STOCK_KLINE_PRESET["dpi"]),
        facecolor="white",
    )
    gs = gridspec.GridSpec(
        2,
        1,
        height_ratios=[1.0,5.4],
        hspace=0.09,
    )

    ax_info = fig.add_subplot(gs[0])
    ax_k = fig.add_subplot(gs[1])

    ax_info.set_facecolor("white")
    ax_info.axis("off")

    font_kwargs = _get_font_kwargs_safe()

    # 與大盤一致：六組 MA 固定為 3 欄 × 2 排。
    # 個股另保留第三排的開高低量，避免六組數字被壓成過小字體。
    fib_positions = [
        ("100%",0.16,0.77),
        ("78.6%",0.50,0.77),
        ("61.8%",0.84,0.77),
        ("50%",0.16,0.45),
        ("38.2%",0.50,0.45),
        ("23.6%",0.84,0.45),
    ]
    for label, x_pos, y_pos in fib_positions:
        ma_color = STOCK_KLINE_PRESET["ma_styles"][ma_name][0]
        ax_info.text(
            x_pos,
            y_pos,
            price = fib_levels.get(label)
            
            ax_info.text(
                x_pos,
                y_pos,
                f"{label} {price:.2f}",
                fontsize=KLINE_INFO_MA_FONTSIZE,
                fontweight="bold",
                color=ma_color,
                ha="center",
                va="center",
                transform=ax_info.transAxes,
                **font_kwargs,
            )
        )

    ax_info.text(
        0.00,
        0.12,
        f"開 {_fmt_price(latest_open)}  高 {_fmt_price(latest_high)}  低 {_fmt_price(latest_low)}  量 {_fmt_lots(latest_volume)}",
        fontsize=KLINE_INFO_OHLC_FONTSIZE,
        color="#444444",
        ha="left",
        va="center",
        transform=ax_info.transAxes,
        **font_kwargs,
    )

    ax_k.set_facecolor(CHART_BACKGROUND)

    x_values, candle_colors = draw_candles(
        ax_k,
        plot_df,
        candle_width=DEFAULT_CANDLE_WIDTH,
    )
    plot_volume_lots = [
        _to_lots(value, volume_unit)
        for value in plot_df["Volume"].tolist()
    ]
    
    # K 線價格軸上下緣固定為畫面可見 K 棒的最高／最低，並強制加入 Y 軸刻度。
    ax_k.margins(x=0.025, y=0)
    set_price_axis_to_visible_high_low(
        ax_k,
        plot_df["High"],
        plot_df["Low"],
        tick_fontsize=AXIS_TICK_FONTSIZE,
    )
    for label,price in fib_levels.items():

        ax_k.axhline(
            price,
            linestyle="--",
            linewidth=1,
            color="#1976D2",
            alpha=0.8,
        )

        ax_k.text(
            len(plot_df)-0.3,
            price,
            f"{label} {price:.2f}",
            fontsize=10,
            ha="left",
            va="center",
            color="#1976D2",
            **font_kwargs,
        )

    # 個股分 K 與日／週／月 K：標示目前顯示範圍內的最高、最低價。
    if tf in {"1m", "5m", "15m", "30m", "60m", "D", "W", "M"}:
        _annotate_high_low(
            ax_k,
            plot_df,
            x_values,
            fontsize=HIGH_LOW_FONTSIZE,
        )


    if tf in {"1m", "5m", "15m", "30m", "60m"}:
        index_dates = [idx.date() for idx in plot_df.index]
        multi_day = bool(index_dates and min(index_dates) != max(index_dates))

        if multi_day:
            labels = [idx.strftime("%m/%d\n%H:%M") for idx in plot_df.index]
        else:
            labels = [idx.strftime("%H:%M") for idx in plot_df.index]
    elif tf == "D":
        labels = [idx.strftime("%m/%d") for idx in plot_df.index]
    elif tf in {"W", "M"}:
        labels = [idx.strftime("%Y/%m") for idx in plot_df.index]
    else:
        labels = [str(idx) for idx in plot_df.index]

    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    if labels and len(labels) - 1 not in ticks:
        ticks.append(len(labels) - 1)

    plt.setp(ax_k.get_xticklabels(), visible=False)

    ax_k.tick_params(axis="y", labelsize=12)

    fig.subplots_adjust(left=0.10, right=0.97, top=0.98, bottom=0.085, hspace=0.09)

    try:
        return publish_figure(fig, f"{stock_id}_{tf}_fib")
    finally:
        plt.close(fig)

def _fmt_chip_ratio(value) -> str:
    try:
        if value in (None, "", "--"):
            return "--"
        if isinstance(value, str) and value.endswith("%"):
            return value
        return f"{float(value):.2f}%"
    except Exception:
        return str(value)

def _fmt_chip_date(value) -> str:
    s = str(value or "--").strip()

    if len(s) >= 10 and "-" in s:
        return s[5:10].replace("-", "/")

    return s.replace("-", "/")

def generate_chip_chart(stock_id: str, stock_name: str, chip_rows: dict[str, list[dict]]) -> str:
    """
    三大法人籌碼圖：中文大字版

    每區分成兩塊：
    1. 文字資訊列：法人名稱、日期、持股比、買賣超張數
    2. 10日買賣超柱狀圖
    """
    font_kwargs = _font_kwargs()

    fig = plt.figure(figsize=(8.8, 12.2), dpi=150, facecolor="white")

    gs = gridspec.GridSpec(
        6,
        1,
        height_ratios=[0.50, 1.55, 0.50, 1.55, 0.50, 1.55],
        hspace=0.36,
    )

    fig.suptitle(
        f"{stock_id} {stock_name} 三大法人籌碼",
        fontsize=21,
        fontweight="bold",
        y=0.992,
        **font_kwargs,
    )

    sections = [
        ("外資", chip_rows.get("foreign", [])),
        ("投信", chip_rows.get("trust", [])),
        ("自營商", chip_rows.get("dealer", [])),
    ]

    for idx, (section_name, rows) in enumerate(sections):
        rows = rows[-10:] if rows else []

        ax_text = fig.add_subplot(gs[idx * 2])
        ax_bar = fig.add_subplot(gs[idx * 2 + 1])

        # =========================
        # 文字資訊區
        # =========================
        ax_text.axis("off")

        latest = rows[-1] if rows else {}
        latest_date = _fmt_chip_date(latest.get("date", "--"))
        latest_ratio = _fmt_chip_ratio(latest.get("ratio", "--"))
        latest_value = float(latest.get("buy_sell", 0) or 0)

        latest_lots = abs(int(round(latest_value)))
        action_text = "買超" if latest_value >= 0 else "賣超"

        ax_text.text(
            0.01,
            0.72,
            section_name,
            fontsize=18,
            fontweight="bold",
            color="#111111",
            ha="left",
            va="center",
            **font_kwargs,
        )

        if latest_ratio in {"--", "", "None", "nan"}:
            info_text = f"{latest_date} │ {action_text} {latest_lots:,} 張"
        else:
            info_text = f"{latest_date} │ 持股比 {latest_ratio} │ {action_text} {latest_lots:,} 張"
        
        ax_text.text(
            0.01,
            0.24,
            info_text,
            fontsize=15,
            fontweight="bold",
            color="#333333",
            ha="left",
            va="center",
            **font_kwargs,
        )

        # =========================
        # 10日柱狀圖
        # =========================
        ax_bar.set_facecolor("#F8F9FA")

        values = [float(r.get("buy_sell", 0) or 0) for r in rows]
        dates = [_fmt_chip_date(r.get("date", "--")) for r in rows]

        if values:
            colors = ["#FF3B30" if v >= 0 else "#34C759" for v in values]
            x = list(range(len(values)))

            ax_bar.bar(
                x,
                values,
                color=colors,
                width=0.60,
                edgecolor="none",
            )

            ax_bar.axhline(
                0,
                linewidth=1.2,
                color="#666666",
            )

            ax_bar.set_xticks(x)
            ax_bar.set_xticklabels(
                dates,
                fontsize=12,
                rotation=0,
            )
        else:
            ax_bar.text(
                0.5,
                0.5,
                "暫無資料",
                transform=ax_bar.transAxes,
                ha="center",
                va="center",
                fontsize=15,
                color="#888888",
                **font_kwargs,
            )
            ax_bar.set_xticks([])

        ax_bar.tick_params(axis="x", labelsize=12)
        ax_bar.tick_params(axis="y", labelsize=12)
        ax_bar.grid(True, axis="y", linestyle=":", alpha=0.12)
        _add_quarter_grid(ax_bar, alpha=0.22)

        ax_bar.spines["top"].set_visible(False)
        ax_bar.spines["right"].set_visible(False)

        ax_bar.margins(y=0.22)

    fig.tight_layout(rect=[0.03, 0.02, 0.98, 0.965])

    try:
        return publish_figure(fig, f"{stock_id}_chip")
    finally:
        plt.close(fig)
