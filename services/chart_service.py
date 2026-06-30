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
    取得平盤價。
    優先使用 stock_service.py 放進 df.attrs 的 reference_price。
    """
    ref = df.attrs.get("reference_price")

    try:
        if ref:
            return float(ref)
    except Exception:
        pass

    try:
        if "Open" in df and not df["Open"].empty:
            return float(df["Open"].iloc[0])
    except Exception:
        pass

    return float(df["Close"].iloc[0])


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

def generate_instant_chart(df: pd.DataFrame, stock_id: str, stock_name: str) -> str:
    if df.empty:
        return _empty_chart(f"{stock_id}", "No intraday data")

    fig, ax = plt.subplots(figsize=(7, 5), dpi=120, facecolor="white")
    ax.set_facecolor("#F8F9FA")

    close = df["Close"].astype(float)
    ref_price = _get_reference_price(df)

    ax.plot(df.index, close, linewidth=2.2, label="Price")

    ax.fill_between(
        df.index,
        close,
        ref_price,
        where=close >= ref_price,
        alpha=0.18,
        interpolate=True,
    )

    ax.fill_between(
        df.index,
        close,
        ref_price,
        where=close < ref_price,
        alpha=0.12,
        interpolate=True,
    )

    ax.set_title(f"{stock_id} Intraday", fontsize=13, fontweight="bold")

    _set_tw_stock_intraday_axis(ax, df)
    _set_centered_price_axis(ax, df)

    ax.grid(True, linestyle=":", alpha=0.55)
    ax.legend(loc="best", fontsize=8)

    fig.autofmt_xdate()
    fig.tight_layout()

    return publish_figure(fig, f"{stock_id}_instant")
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

def generate_kline_chart(df: pd.DataFrame, stock_id: str, stock_name: str, time_frame: str) -> str:
    tf = normalize_time_frame(time_frame)
    font_kwargs = _get_font_kwargs_safe()

    if df.empty:
        return _empty_chart(f"{stock_id} {stock_name}", "暫無 K 線資料")

    df = df.copy()

    # =========================
    # 基本欄位整理
    # =========================
    required_cols = ["Open", "High", "Low", "Close"]

    for col in required_cols:
        if col not in df.columns:
            return _empty_chart(f"{stock_id} {stock_name}", f"Missing column: {col}")

    if "Volume" not in df.columns:
        df["Volume"] = 0

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])

    if df.empty:
        return _empty_chart(f"{stock_id} {stock_name}", "K 線資料為空")

    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
        except Exception:
            pass

    # =========================
    # D / W / M 顯示 5T、12T、22T、60T、120T
    # =========================
    show_ma_summary = tf in {"D", "W", "M"}

    if show_ma_summary:
        ma_periods = [5, 12, 22, 60, 120]
    else:
        ma_periods = [5, 20]

    # 先用完整資料計算均線，再裁切顯示範圍
    close_series = df["Close"].astype(float)

    for p in ma_periods:
        df[f"MA{p}"] = close_series.rolling(p, min_periods=1).mean()

    latest = df.iloc[-1]

    # =========================
    # 建圖
    # =========================
    if show_ma_summary:
        ma_text_1 = (
            f"5T {_fmt_ma_value(latest.get('MA5'))}    "
            f"12T {_fmt_ma_value(latest.get('MA12'))}    "
            f"22T {_fmt_ma_value(latest.get('MA22'))}"
        )

        ma_text_2 = (
            f"60T {_fmt_ma_value(latest.get('MA60'))}    "
            f"120T {_fmt_ma_value(latest.get('MA120'))}"
        )

        # 顯示最近 120 根
        plot_df = df.tail(120).copy()

        fig = plt.figure(figsize=(8.8, 7.2), dpi=140, facecolor="white")

        gs = gridspec.GridSpec(
            3,
            1,
            height_ratios=[0.90, 3.2, 1],
            hspace=0.06,
        )

        ax_info = fig.add_subplot(gs[0])
        ax_k = fig.add_subplot(gs[1])
        ax_v = fig.add_subplot(gs[2], sharex=ax_k)

        ax_info.axis("off")

        ax_info.text(
            0.01,
            0.78,
            f"{stock_id} {stock_name} {tf} K線",
            fontsize=17,
            fontweight="bold",
            color="#111111",
            ha="left",
            va="center",
            **font_kwargs,
        )

        ax_info.text(
            0.01,
            0.38,
            ma_text_1,
            fontsize=15,
            fontweight="bold",
            color="#222222",
            ha="left",
            va="center",
            **font_kwargs,
        )

        ax_info.text(
            0.01,
            0.08,
            ma_text_2,
            fontsize=15,
            fontweight="bold",
            color="#222222",
            ha="left",
            va="center",
            **font_kwargs,
        )

    else:
        plot_df = df.copy()

        fig = plt.figure(figsize=(7, 5.5), dpi=120, facecolor="white")
        gs = gridspec.GridSpec(2, 1, height_ratios=[3, 1], hspace=0.05)

        ax_k = fig.add_subplot(gs[0])
        ax_v = fig.add_subplot(gs[1], sharex=ax_k)

        ax_k.set_title(
            f"{stock_id} {tf} K-Line",
            fontsize=13,
            fontweight="bold",
        )

    ax_k.set_facecolor("#F8F9FA")
    ax_v.set_facecolor("#F8F9FA")

    x = list(range(len(plot_df)))
    width = 0.58

    # =========================
    # 畫 K 棒與成交量
    # 重點：不用 iterrows 拆包，避免 too many values to unpack
    # =========================
    for i in range(len(plot_df)):
        row = plot_df.iloc[i]

        o = float(row["Open"])
        h = float(row["High"])
        l = float(row["Low"])
        c = float(row["Close"])

        color = "#FF3B30" if c >= o else "#34C759"

        ax_k.vlines(i, l, h, linewidth=1, color=color)

        lower = min(o, c)
        height = abs(c - o)

        if height <= 0:
            height = 0.01

        ax_k.bar(
            i,
            height,
            bottom=lower,
            width=width,
            color=color,
            align="center",
        )

        vol = float(row.get("Volume", 0) or 0)

        ax_v.bar(
            i,
            vol,
            width=width,
            color=color,
        )

    # =========================
    # 畫均線
    # =========================
    for p in ma_periods:
        col = f"MA{p}"

        if col not in plot_df.columns:
            continue

        label = f"{p}T" if show_ma_summary else f"MA{p}"

        ax_k.plot(
            x,
            plot_df[col].astype(float).values,
            linewidth=1.25,
            label=label,
        )

    ax_k.grid(True, linestyle=":", alpha=0.45)
    ax_v.grid(True, linestyle=":", alpha=0.45)

    ax_k.legend(loc="best", fontsize=9)
    ax_v.set_ylabel("Volume", fontsize=9)

    # =========================
    # X 軸日期
    # =========================
    labels = []

    for idx in plot_df.index:
        try:
            ts = pd.to_datetime(idx)

            if tf in {"1m", "5m"}:
                labels.append(ts.strftime("%H:%M"))
            elif tf == "M":
                labels.append(ts.strftime("%Y/%m"))
            else:
                labels.append(ts.strftime("%m/%d"))

        except Exception:
            labels.append(str(idx))

    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels(
        [labels[i] for i in ticks],
        rotation=0,
        fontsize=9,
    )

    plt.setp(ax_k.get_xticklabels(), visible=False)

    fig.tight_layout()

    return publish_figure(fig, f"{stock_id}_{tf}_kline")
    
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

    return publish_figure(fig, f"{stock_id}_chip")
