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
from utils.formatter import normalize_time_frame


plt.rcParams["axes.unicode_minus"] = False

BASE_DIR = Path(__file__).resolve().parents[1]
FONT_PATH = BASE_DIR / "assets" / "fonts" / "NotoSansTC-Regular.ttf"

CHART_FONT_PROP = None

if FONT_PATH.exists():
    font_manager.fontManager.addfont(str(FONT_PATH))
    CHART_FONT_PROP = font_manager.FontProperties(fname=str(FONT_PATH))
    plt.rcParams["font.family"] = CHART_FONT_PROP.get_name()
else:
    print(f"Chart font not found: {FONT_PATH}")
    plt.rcParams["font.sans-serif"] = [
        "Noto Sans CJK TC",
        "Microsoft JhengHei",
        "Arial Unicode MS",
        "DejaVu Sans",
        "sans-serif",
    ]


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
    讓平盤價置於 Y 軸中間，並在右側顯示漲跌幅百分比。
    """
    ref_price = _get_reference_price(df)

    close = df["Close"].astype(float).dropna()

    if close.empty:
        return ref_price

    max_delta = max(
        abs(float(close.max()) - ref_price),
        abs(float(close.min()) - ref_price),
    )

    if max_delta <= 0:
        max_delta = max(ref_price * 0.005, 0.5)

    max_delta *= 1.2

    ymin = ref_price - max_delta
    ymax = ref_price + max_delta

    ax.set_ylim(ymin, ymax)

    ax.axhline(
        ref_price,
        linestyle="--",
        linewidth=1.0,
        alpha=0.8,
        label="Prev Close",
    )

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

    return ref_price

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
    try:
        if plot_df is None or plot_df.empty:
            return

        if "High" not in plot_df.columns or "Low" not in plot_df.columns:
            return

        high_series = pd.to_numeric(plot_df["High"], errors="coerce")
        low_series = pd.to_numeric(plot_df["Low"], errors="coerce")

        if high_series.dropna().empty or low_series.dropna().empty:
            return

        high_idx = high_series.idxmax()
        low_idx = low_series.idxmin()

        high_pos = list(plot_df.index).index(high_idx)
        low_pos = list(plot_df.index).index(low_idx)

        high_x = x_values[high_pos]
        low_x = x_values[low_pos]

        high_y = float(high_series.loc[high_idx])
        low_y = float(low_series.loc[low_idx])

        y_min = float(low_series.min())
        y_max = float(high_series.max())
        y_pad = max((y_max - y_min) * 0.035, y_max * 0.001)

        ax.text(
            high_x,
            high_y + y_pad,
            f"高 {high_y:,.2f}",
            ha="center",
            va="bottom",
            fontsize=fontsize,
            fontweight="bold",
            color="#D32F2F",
            zorder=10,
        )

        ax.text(
            low_x,
            low_y - y_pad,
            f"低 {low_y:,.2f}",
            ha="center",
            va="top",
            fontsize=fontsize,
            fontweight="bold",
            color="#00A84F",
            zorder=10,
        )

    except Exception as exc:
        print(
            "DEBUG annotate high low failed",
            "| error =",
            repr(exc),
            flush=True,
        )

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
    fig = plt.figure(figsize=(8.4, 7.6), dpi=140, facecolor="white")
    gs = gridspec.GridSpec(
        3,
        1,
        height_ratios=[0.95, 4.6, 1.45],
        hspace=0.05,
    )

    ax_info = fig.add_subplot(gs[0])
    ax = fig.add_subplot(gs[1])
    ax_v = fig.add_subplot(gs[2], sharex=ax)

    ax_info.axis("off")
    ax.set_facecolor("#F8F9FA")
    ax_v.set_facecolor("#F8F9FA")

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
            fontsize=16,
            fontweight="bold",
            color="#333333",
            transform=ax_info.transAxes,
            **_get_font_kwargs_safe(),
        )

    # ===== 主圖：即時折線 =====
    line_color = "#E74C3C" if latest >= ref_price else "#27AE60"

    ax.plot(
        df.index,
        close,
        linewidth=2.6,
        color=line_color,
        zorder=3,
    )

    ax.fill_between(
        df.index,
        close,
        ref_price,
        where=close >= ref_price,
        alpha=0.12,
        color="#E74C3C",
        interpolate=True,
        zorder=2,
    )

    ax.fill_between(
        df.index,
        close,
        ref_price,
        where=close < ref_price,
        alpha=0.10,
        color="#27AE60",
        interpolate=True,
        zorder=2,
    )

    # 參考線 / 昨收線
    ax.axhline(
        ref_price,
        linestyle="--",
        linewidth=1.3,
        color="#7F8C8D",
        alpha=0.85,
        zorder=1,
    )

    # 自動置中價格軸 + 右側漲跌幅
    _set_tw_stock_intraday_axis(ax, df)
    _set_centered_price_axis(ax, df)

    ax.grid(True, linestyle=":", alpha=0.35)
    ax.tick_params(axis="x", labelsize=11)
    ax.tick_params(axis="y", labelsize=11)

    # ===== 成交量圖 =====
    vol_colors = []

    for _, row in df.iterrows():
        try:
            o = float(row["Open"])
            c = float(row["Close"])
            vol_colors.append("#E74C3C" if c >= o else "#27AE60")
        except Exception:
            vol_colors.append("#E74C3C")

    volume_lot_series = df["Volume"].fillna(0).astype(float) / 1000.0

    bar_width = 0.0025 if len(df) > 100 else 0.0045

    ax_v.bar(
        df.index,
        volume_lot_series,
        width=bar_width,
        color=vol_colors,
        edgecolor="none",
    )

    ax_v.set_ylabel("成交量(張)", fontsize=12, **_get_font_kwargs_safe())
    ax_v.grid(True, linestyle=":", alpha=0.30)
    ax_v.tick_params(axis="x", labelsize=10)
    ax_v.tick_params(axis="y", labelsize=10)

    ax_v.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
    ax_v.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    plt.setp(ax.get_xticklabels(), visible=False)

    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
        ax_v.spines[spine].set_visible(False)

    fig.tight_layout()

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
    fig = plt.figure(figsize=(8.4, 7.6), dpi=140, facecolor="white")
    gs = gridspec.GridSpec(
        3,
        1,
        height_ratios=[0.95, 4.6, 1.45],
        hspace=0.05,
    )

    ax_info = fig.add_subplot(gs[0])
    ax = fig.add_subplot(gs[1])
    ax_v = fig.add_subplot(gs[2], sharex=ax)

    ax_info.axis("off")
    ax.set_facecolor("#F8F9FA")
    ax_v.set_facecolor("#F8F9FA")

    # ===== 上方資訊列：改成 2 排 x 3 欄 =====
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
            fontsize=16,
            fontweight="bold",
            color="#333333",
            transform=ax_info.transAxes,
            **_get_font_kwargs_safe(),
        )

    # ===== 主圖：即時折線 =====
    line_color = "#E74C3C" if latest >= ref_price else "#27AE60"

    ax.plot(
        df.index,
        close,
        linewidth=2.6,
        color=line_color,
        zorder=3,
    )

    ax.fill_between(
        df.index,
        close,
        ref_price,
        where=close >= ref_price,
        alpha=0.12,
        color="#E74C3C",
        interpolate=True,
        zorder=2,
    )

    ax.fill_between(
        df.index,
        close,
        ref_price,
        where=close < ref_price,
        alpha=0.10,
        color="#27AE60",
        interpolate=True,
        zorder=2,
    )

    # 參考線 / 昨收線
    ax.axhline(
        ref_price,
        linestyle="--",
        linewidth=1.3,
        color="#7F8C8D",
        alpha=0.85,
        zorder=1,
    )

    # 自動置中價格軸 + 右側漲跌幅
    _set_tw_stock_intraday_axis(ax, df)
    _set_centered_price_axis(ax, df)

    ax.grid(True, linestyle=":", alpha=0.35)
    ax.tick_params(axis="x", labelsize=11)
    ax.tick_params(axis="y", labelsize=11)

    # ===== 成交量圖 =====
    vol_colors = []

    for _, row in df.iterrows():
        try:
            o = float(row["Open"])
            c = float(row["Close"])
            vol_colors.append("#E74C3C" if c >= o else "#27AE60")
        except Exception:
            vol_colors.append("#E74C3C")

    volume_lot_series = df["Volume"].fillna(0).astype(float) / 1000.0

    # Matplotlib 日期座標的 width 單位是「天」
    # 0.0025 約等於 3.6 分鐘，視覺上比較飽滿。
    bar_width = 0.0025 if len(df) > 100 else 0.0045

    ax_v.bar(
        df.index,
        volume_lot_series,
        width=bar_width,
        color=vol_colors,
        edgecolor="none",
    )

    ax_v.set_ylabel("成交量(張)", fontsize=12, **_get_font_kwargs_safe())
    ax_v.grid(True, linestyle=":", alpha=0.30)
    ax_v.tick_params(axis="x", labelsize=10)
    ax_v.tick_params(axis="y", labelsize=10)

    # X 軸格式
    ax_v.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
    ax_v.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    plt.setp(ax.get_xticklabels(), visible=False)

    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
        ax_v.spines[spine].set_visible(False)

    fig.tight_layout()

    try:
        return publish_figure(fig, f"{stock_id}_instant")
    finally:
        plt.close(fig)
    
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

    work_df = work_df.dropna(subset=["Open", "High", "Low", "Close"])

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

    if work_df.empty:
        return ""

    volume_unit = str(getattr(work_df, "attrs", {}).get("volume_unit") or "shares").lower()

    # MA 必須先用完整資料算，再裁切顯示範圍。
    # min_periods 用完整 period，避免資料不足時硬算出失真的 MA。
    close_for_ma = pd.to_numeric(work_df["Close"], errors="coerce")

    for period in [5, 10, 20, 60, 120, 240]:
        work_df[f"MA{period}"] = close_for_ma.rolling(
            period,
            min_periods=period,
        ).mean()

    if tf == "D":
        plot_df = work_df.tail(60).copy()
    elif tf == "W":
        plot_df = work_df.tail(80).copy()
    elif tf == "M":
        plot_df = work_df.tail(80).copy()
    else:
        plot_df = work_df.tail(60).copy()

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

    ma5 = latest.get("MA5")
    ma20 = latest.get("MA20")
    ma60 = latest.get("MA60")
    ma120 = latest.get("MA120")

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
                "| MA5 =", ma5,
                "| MA20 =", ma20,
                "| MA60 =", ma60,
                "| MA120 =", ma120,
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

    fig = plt.figure(figsize=(9, 7), dpi=130, facecolor="white")
    gs = gridspec.GridSpec(
        3,
        1,
        height_ratios=[0.78, 3.35, 1.05],
        hspace=0.05,
    )

    ax_info = fig.add_subplot(gs[0])
    ax_k = fig.add_subplot(gs[1])
    ax_v = fig.add_subplot(gs[2], sharex=ax_k)

    ax_info.set_facecolor("white")
    ax_info.axis("off")

    font_kwargs = _get_font_kwargs_safe()

    ax_info.text(0.00, 0.34, f"5MA {_fmt_ma(ma5)}", fontsize=15, fontweight="bold", color="#111111", ha="left", va="center", transform=ax_info.transAxes, **font_kwargs)
    ax_info.text(0.28, 0.34, f"20MA {_fmt_ma(ma20)}", fontsize=15, fontweight="bold", color="#1F77B4", ha="left", va="center", transform=ax_info.transAxes, **font_kwargs)
    ax_info.text(0.56, 0.34, f"60MA {_fmt_ma(ma60)}", fontsize=15, fontweight="bold", color="#FF7F0E", ha="left", va="center", transform=ax_info.transAxes, **font_kwargs)
    ax_info.text(0.00, 0.08, f"120MA {_fmt_ma(ma120)}", fontsize=15, fontweight="bold", color="#9467BD", ha="left", va="center", transform=ax_info.transAxes, **font_kwargs)

    ax_info.text(
        0.35,
        0.08,
        f"開 {_fmt_price(latest_open)}  高 {_fmt_price(latest_high)}  低 {_fmt_price(latest_low)}  量 {_fmt_lots(latest_volume)}",
        fontsize=13,
        color="#444444",
        ha="left",
        va="center",
        transform=ax_info.transAxes,
        **font_kwargs,
    )

    ax_k.set_facecolor("#F8F9FA")
    ax_v.set_facecolor("#F8F9FA")

    x_values = list(range(len(plot_df)))
    candle_width = 0.58

    for i in range(len(plot_df)):
        row = plot_df.iloc[i]

        open_price = float(row["Open"])
        high_price = float(row["High"])
        low_price = float(row["Low"])
        close_price = float(row["Close"])
        volume = _to_lots(row["Volume"], volume_unit)

        color = "#FF2D2D" if close_price >= open_price else "#00B050"

        ax_k.vlines(i, low_price, high_price, linewidth=1.0, color=color)

        lower = min(open_price, close_price)
        height = abs(close_price - open_price)

        if height <= 0:
            height = 0.01

        ax_k.bar(i, height, bottom=lower, width=candle_width, color=color, align="center")
        ax_v.bar(i, volume, width=candle_width, color=color, align="center")

    ma_styles = {
        "MA5": ("#111111", 1.2),
        "MA20": ("#1F77B4", 1.2),
        "MA60": ("#FF7F0E", 1.2),
        "MA120": ("#9467BD", 1.2),
    }

    for col, (line_color, linewidth) in ma_styles.items():
        if col in plot_df.columns:
            ax_k.plot(x_values, plot_df[col].values, linewidth=linewidth, color=line_color)

    ax_k.grid(True, linestyle=":", alpha=0.35)
    ax_v.grid(True, linestyle=":", alpha=0.30)

    if tf == "D":
        labels = [idx.strftime("%m/%d") for idx in plot_df.index]
    elif tf == "W":
        labels = [idx.strftime("%Y/%m") for idx in plot_df.index]
    else:
        labels = [idx.strftime("%Y/%m") for idx in plot_df.index]

    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels([labels[i] for i in ticks], rotation=0, fontsize=9, **font_kwargs)

    plt.setp(ax_k.get_xticklabels(), visible=False)

    ax_v.set_ylabel("成交量", fontsize=10, **font_kwargs)

    ax_k.tick_params(axis="y", labelsize=9)
    ax_v.tick_params(axis="y", labelsize=9)

    ax_k.spines["top"].set_visible(False)
    ax_k.spines["right"].set_visible(False)
    ax_v.spines["top"].set_visible(False)
    ax_v.spines["right"].set_visible(False)

    fig.tight_layout()

    try:
        return publish_figure(fig, f"{stock_id}_{tf}_kline")
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

        ax_bar.tick_params(axis="y", labelsize=12)
        ax_bar.grid(True, axis="y", linestyle=":", alpha=0.35)

        ax_bar.spines["top"].set_visible(False)
        ax_bar.spines["right"].set_visible(False)

        ax_bar.margins(y=0.22)

    fig.tight_layout(rect=[0.03, 0.02, 0.98, 0.965])

    _annotate_high_low(
        ax_k,
        plot_df,
        x_values,
        fontsize=12,
    )
    
    return publish_figure(fig, f"{stock_id}_chip")
