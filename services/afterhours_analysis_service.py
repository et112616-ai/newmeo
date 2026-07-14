from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import font_manager

from services.upload_service import publish_figure


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
        keep_mask = ~date_keys.duplicated(keep="last")
        work = work.loc[keep_mask].copy()
    except Exception:
        pass

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col not in work.columns:
            if col == "Volume":
                work[col] = 0
            else:
                return pd.DataFrame()

        work[col] = pd.to_numeric(work[col], errors="coerce")

    work = work.dropna(subset=["Open", "High", "Low", "Close"])

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

    try:
        return round(round(float(value) / tick) * tick, 2)
    except Exception:
        return float(value)


def _fmt_price(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return "--"


def _fmt_lots(value: Any) -> str:
    try:
        return f"{int(round(float(value))):,} 張"
    except Exception:
        return "--"


def _calc_levels(work: pd.DataFrame) -> dict[str, Any]:
    latest = work.iloc[-1]

    high = float(latest["High"])
    low = float(latest["Low"])
    close = float(latest["Close"])

    pivot = (high + low + close) / 3.0
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)

    try:
        recent = work.tail(5).copy()
        recent_range = float((recent["High"] - recent["Low"]).median())
    except Exception:
        recent_range = 0.0

    if not recent_range or recent_range <= 0:
        recent_range = max(high - low, close * 0.02, 1.0)

    if r1 <= close:
        r1 = close + recent_range * 0.5
    if r2 <= r1:
        r2 = close + recent_range
    if s1 >= close:
        s1 = close - recent_range * 0.5
    if s2 >= s1:
        s2 = close - recent_range

    avg3 = float(work["Close"].tail(3).mean())

    latest_date = "--"

    try:
        latest_date = work.index[-1].strftime("%Y-%m-%d")
    except Exception:
        latest_date = str(work.index[-1])

    return {
        "date": latest_date,
        "r2": _round_to_tick(r2, close),
        "r1": _round_to_tick(r1, close),
        "flat": _round_to_tick(close, close),
        "s1": _round_to_tick(s1, close),
        "s2": _round_to_tick(s2, close),
        "avg3": _round_to_tick(avg3, close),
    }

def generate_post_market_analysis_chart(
    df: pd.DataFrame,
    stock_id: str,
    stock_name: str,
    branch_name: str = "資料建置中",
    branch_net_lots: Any = "--",
    branch_avg_price: Any = "--",
) -> str:
    work = _prepare_daily_df(df)

    if work.empty:
        return ""

    font_kwargs = _setup_font()
    levels = _calc_levels(work)

    # 白底，不再使用綠色卡片底色
    fig = plt.figure(figsize=(7.0, 7.0), dpi=150, facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.set_facecolor("white")

    x_label = 0.13
    x_value = 0.46
    y = 0.88

    # 壓力 / 支撐區塊放大
    rows = [
        ("壓 2", levels["r2"], "#D32F2F"),
        ("壓 1", levels["r1"], "#F44336"),
        ("平盤", levels["flat"], "#E0A800"),
        ("撐 1", levels["s1"], "#00A84F"),
        ("撐 2", levels["s2"], "#008C3A"),
    ]

    for label, price, color in rows:
        ax.text(
            x_label,
            y,
            f"{label}：",
            fontsize=34,
            fontweight="bold",
            color=color,
            va="top",
            **font_kwargs,
        )
        ax.text(
            x_value,
            y,
            _fmt_price(price),
            fontsize=34,
            fontweight="bold",
            color="#111111",
            va="top",
            **font_kwargs,
        )
        y -= 0.125

    # 分隔線
    y -= 0.015
    ax.text(
        0.11,
        y,
        "────────────",
        fontsize=24,
        color="#666666",
        va="top",
        **font_kwargs,
    )
    y -= 0.105

    # 關鍵分點區塊
    ax.text(
        x_label,
        y,
        "關鍵分點：",
        fontsize=25,
        fontweight="bold",
        color="#111111",
        va="top",
        **font_kwargs,
    )
    ax.text(
        x_label + 0.36,
        y,
        str(branch_name or "資料建置中"),
        fontsize=25,
        fontweight="bold",
        color="#111111",
        va="top",
        **font_kwargs,
    )
    y -= 0.10

    if isinstance(branch_net_lots, (int, float)):
        branch_net_text = _fmt_lots(branch_net_lots)
    else:
        branch_net_text = str(branch_net_lots or "--")

    ax.text(
        x_label,
        y,
        f"買賣超：{branch_net_text}",
        fontsize=25,
        fontweight="bold",
        color="#111111",
        va="top",
        **font_kwargs,
    )
    y -= 0.10

    avg_text = "--"

    try:
        if isinstance(branch_avg_price, (int, float)) and float(branch_avg_price) > 0:
            avg_text = _fmt_price(branch_avg_price)
        elif str(branch_avg_price or "").strip() not in {"", "--"}:
            avg_text = str(branch_avg_price)
    except Exception:
        avg_text = "--"

    ax.text(
        x_label,
        y,
        f"3日均價：{avg_text}",
        fontsize=25,
        fontweight="bold",
        color="#111111",
        va="top",
        **font_kwargs,
    )
    
    # 底部備註縮小，避免干擾主資訊
    ax.text(
        0.08,
        0.045,
        "支撐壓力為推估，僅供盤後觀察。",
        fontsize=12,
        color="#777777",
        va="bottom",
        **font_kwargs,
    )

    print(
        "DEBUG post_market analysis",
        "| stock_id =",
        stock_id,
        "| date =",
        levels["date"],
        "| r2 =",
        levels["r2"],
        "| r1 =",
        levels["r1"],
        "| flat =",
        levels["flat"],
        "| s1 =",
        levels["s1"],
        "| s2 =",
        levels["s2"],
        "| avg3 =",
        levels["avg3"],
        flush=True,
    )

    try:
        return publish_figure(fig, f"{stock_id}_post_market")
    finally:
        plt.close(fig)
