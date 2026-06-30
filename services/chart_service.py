from __future__ import annotations

from datetime import time

import matplotlib
matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import pandas as pd
import matplotlib.ticker as mticker

from services.upload_service import publish_figure
from utils.formatter import normalize_time_frame

plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["font.sans-serif"] = [
    "Noto Sans CJK TC",
    "Microsoft JhengHei",
    "Arial Unicode MS",
    "DejaVu Sans",
    "sans-serif",
]

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
    
def generate_kline_chart(df: pd.DataFrame, stock_id: str, stock_name: str, time_frame: str) -> str:
    tf = normalize_time_frame(time_frame)

    if df.empty:
        return _empty_chart(f"{stock_id} {stock_name}", "暫無 K 線資料")

    df = df.copy()
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA20"] = df["Close"].rolling(20).mean()

    fig = plt.figure(figsize=(7, 5.5), dpi=120, facecolor="white")
    gs = gridspec.GridSpec(2, 1, height_ratios=[3, 1], hspace=0.05)

    ax_k = fig.add_subplot(gs[0])
    ax_v = fig.add_subplot(gs[1], sharex=ax_k)

    ax_k.set_facecolor("#F8F9FA")
    ax_v.set_facecolor("#F8F9FA")

    x = range(len(df))
    width = 0.58

    for i, (_, row) in enumerate(df.iterrows()):
        o = float(row["Open"])
        h = float(row["High"])
        l = float(row["Low"])
        c = float(row["Close"])

        color = "#FF3B30" if c >= o else "#34C759"

        ax_k.vlines(i, l, h, linewidth=1, color=color)

        lower = min(o, c)
        height = abs(c - o) or 0.01
        ax_k.bar(i, height, bottom=lower, width=width, color=color, align="center")

        vol = float(row.get("Volume", 0) or 0)
        ax_v.bar(i, vol, width=width, color=color)

    ax_k.plot(list(x), df["MA5"], linewidth=1.1, label="MA5")
    ax_k.plot(list(x), df["MA20"], linewidth=1.1, label="MA20")

    ax_k.set_title(f"{stock_id} {tf} K-Line", fontsize=13, fontweight="bold")
    ax_k.grid(True, linestyle=":", alpha=0.45)
    ax_v.grid(True, linestyle=":", alpha=0.45)
    ax_k.legend(loc="best", fontsize=8)
    ax_v.set_ylabel("Volume", fontsize=8)

    labels = []
    for idx in df.index:
        if tf in {"1m", "5m"}:
            labels.append(idx.strftime("%H:%M"))
        else:
            labels.append(idx.strftime("%m/%d"))

    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels([labels[i] for i in ticks], rotation=0, fontsize=8)

    plt.setp(ax_k.get_xticklabels(), visible=False)

    fig.tight_layout()

    return publish_figure(fig, f"{stock_id}_{tf}_kline")


def _fmt_chip_ratio(value) -> str:
    try:
        if value in (None, "", "--"):
            return "--"
        return f"{float(value):.2f}%"
    except Exception:
        return str(value)


def generate_chip_chart(stock_id: str, stock_name: str, chip_rows: dict[str, list[dict]]) -> str:
    """
    法人籌碼圖（手機可讀版）
    規格：
    - 三大區塊：外資 / 投信 / 自營商
    - 每區塊第一排：日期 │ 持股比 │ 當日買賣超
    - 第二排：10日買賣超柱狀圖
    - 文字放大，避免 LINE 手機版看不清楚
    """
    fig, axes = plt.subplots(3, 1, figsize=(8.2, 9.6), dpi=150, facecolor="white")
    fig.suptitle(
        f"{stock_id} {stock_name} 三大法人籌碼",
        fontsize=18,
        fontweight="bold",
        y=0.985,
    )

    sections = [
        ("外資", chip_rows.get("foreign", [])),
        ("投信", chip_rows.get("trust", [])),
        ("自營商", chip_rows.get("dealer", [])),
    ]

    for ax, (title, rows) in zip(axes, sections):
        ax.set_facecolor("#F8F9FA")

        # 最近 10 筆
        rows = rows[-10:] if rows else []

        dates = [str(r.get("date", ""))[5:].replace("-", "/") for r in rows]
        values = [float(r.get("buy_sell", 0) or 0) for r in rows]

        # ratio 若資料沒有，就顯示 --
        latest_ratio = "--"
        if rows:
            ratio_raw = rows[-1].get("ratio")
            if ratio_raw not in (None, "", "--"):
                try:
                    latest_ratio = f"{float(ratio_raw):.2f}%"
                except Exception:
                    latest_ratio = str(ratio_raw)

        latest_date = dates[-1] if dates else "--"
        latest_buy_sell = values[-1] if values else 0.0

        # 第一排資訊文字
        info_text = f"{latest_date} │ {title}持股比 {latest_ratio} │ 買賣超 {latest_buy_sell:,.0f} 張"
        ax.text(
            0.01,
            1.10,
            info_text,
            transform=ax.transAxes,
            fontsize=12.5,
            fontweight="bold",
            color="#333333",
            va="bottom",
        )

        # 第二排：10日柱狀圖
        if values:
            colors = ["#FF3B30" if v > 0 else "#34C759" for v in values]
            x = list(range(len(values)))
            ax.bar(x, values, color=colors, width=0.58, edgecolor="none")
            ax.axhline(0, color="#666666", linewidth=1.0)

            ax.set_xticks(x)
            ax.set_xticklabels(dates, fontsize=10)
        else:
            ax.text(
                0.5,
                0.5,
                "暫無資料",
                transform=ax.transAxes,
                ha="center",
                va="center",
                fontsize=12,
                color="#888888",
            )
            ax.set_xticks([])

        # 標題
        ax.set_title(title, loc="left", fontsize=14, fontweight="bold", pad=18)

        # Y 軸字放大
        ax.tick_params(axis="y", labelsize=10)
        ax.grid(True, axis="y", linestyle=":", alpha=0.35)

        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    fig.tight_layout(rect=[0.02, 0.02, 0.98, 0.965], h_pad=2.0)

    return publish_figure(fig, f"{stock_id}_chip")
