from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import font_manager

from services.upload_service import publish_figure

try:
    from services.sinopac_quote_service import get_api
except Exception:
    def get_api():
        return None


MARKET_FUTURE_KLINE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

TTL_MAP = {
    "1m": 30,
    "5m": 45,
    "15m": 90,
    "30m": 120,
    "60m": 180,
}

LOOKBACK_DAYS_MAP = {
    "1m": 2,
    "5m": 3,
    "15m": 7,
    "30m": 14,
    "60m": 30,
}

RESAMPLE_RULE_MAP = {
    "1m": "",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "60m": "60min",
}


BASE_DIR = Path(__file__).resolve().parents[1]
FONT_PATH = BASE_DIR / "assets" / "fonts" / "NotoSansTC-Regular.ttf"

CHART_FONT_PROP = None

if FONT_PATH.exists():
    font_manager.fontManager.addfont(str(FONT_PATH))
    CHART_FONT_PROP = font_manager.FontProperties(fname=str(FONT_PATH))
    plt.rcParams["font.family"] = CHART_FONT_PROP.get_name()
else:
    plt.rcParams["font.sans-serif"] = [
        "Noto Sans CJK TC",
        "Microsoft JhengHei",
        "Arial Unicode MS",
        "DejaVu Sans",
        "sans-serif",
    ]

plt.rcParams["axes.unicode_minus"] = False


@dataclass
class MarketFutureKlineSnapshot:
    available: bool
    message: str
    image_url: str = ""
    time_frame: str = "1m"
    label: str = "1分"
    contract_code: str = "TXFR1"
    latest_time: str = ""
    latest_close: float = 0.0
    bb_upper: float = 0.0
    bb_mid: float = 0.0
    bb_lower: float = 0.0
    rows: int = 0


def _debug(*args):
    print("DEBUG market_future_kline |", *args, flush=True)


def _font_kwargs() -> dict:
    if CHART_FONT_PROP is not None:
        return {"fontproperties": CHART_FONT_PROP}
    return {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        if isinstance(value, Decimal):
            return float(value)

        text = str(value).replace(",", "").replace("%", "").strip()

        if not text:
            return default

        return float(text)

    except Exception:
        return default


def _try_get_contract(container, code: str):
    if container is None:
        return None

    try:
        return container[code]
    except Exception:
        pass

    try:
        return getattr(container, code)
    except Exception:
        pass

    return None


def _get_txf_contract(api):
    futures_root = getattr(api.Contracts, "Futures", None)

    if futures_root is None:
        return None

    txf_group = _try_get_contract(futures_root, "TXF")

    candidates = [
        "TXFR1",
        "TXF",
    ]

    for code in candidates:
        contract = _try_get_contract(txf_group, code)

        if contract is not None:
            return contract

    for code in candidates:
        contract = _try_get_contract(futures_root, code)

        if contract is not None:
            return contract

    return None


def _contract_code(contract) -> str:
    try:
        return str(getattr(contract, "code", "") or "TXFR1")
    except Exception:
        return "TXFR1"


def _tf_label(tf: str) -> str:
    return {
        "1m": "1分",
        "5m": "5分",
        "15m": "15分",
        "30m": "30分",
        "60m": "60分",
    }.get(tf, tf)


def _normalize_tf(value: str) -> str:
    tf = str(value or "1m").strip()

    if tf not in {"1m", "5m", "15m", "30m", "60m"}:
        return "1m"

    return tf

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
            f"高 {_fmt_price(high_y)}",
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
            f"低 {_fmt_price(low_y)}",
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

def _kbars_to_df(kbars: Any) -> pd.DataFrame:
    try:
        if isinstance(kbars, dict):
            raw = kbars
        elif hasattr(kbars, "__dict__"):
            raw = dict(kbars.__dict__)
        else:
            raw = dict(kbars)

        df = pd.DataFrame(raw)
    except Exception:
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    if "ts" not in df.columns:
        return pd.DataFrame()

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts"]).copy()

    if df.empty:
        return pd.DataFrame()

    try:
        if getattr(df["ts"].dt, "tz", None) is not None:
            df["ts"] = df["ts"].dt.tz_convert("Asia/Taipei").dt.tz_localize(None)
    except Exception:
        pass

    rename_map = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }

    df = df.rename(columns=rename_map)

    needed = ["Open", "High", "Low", "Close", "Volume"]

    for col in needed:
        if col not in df.columns:
            if col == "Volume":
                df[col] = 0
            else:
                return pd.DataFrame()

        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()

    if df.empty:
        return pd.DataFrame()

    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")].copy()

    return df[needed]


def _fetch_1m_kbars(contract, tf: str) -> pd.DataFrame:
    api = get_api()

    if api is None:
        return pd.DataFrame()

    days = LOOKBACK_DAYS_MAP.get(tf, 7)
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    try:
        kbars = api.kbars(
            contract=contract,
            start=start,
            end=end,
        )

        df = _kbars_to_df(kbars)

        _debug(
            "fetch kbars",
            "| contract =",
            _contract_code(contract),
            "| tf =",
            tf,
            "| start =",
            start,
            "| end =",
            end,
            "| rows =",
            0 if df is None else len(df),
        )

        return df

    except Exception as exc:
        _debug(
            "fetch kbars failed",
            "| contract =",
            _contract_code(contract),
            "| tf =",
            tf,
            "| error =",
            repr(exc),
        )
        return pd.DataFrame()


def _resample_df(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    tf = _normalize_tf(tf)

    if tf == "1m":
        return df.copy()

    rule = RESAMPLE_RULE_MAP.get(tf)

    if not rule:
        return df.copy()

    work = df.copy()

    result = work.resample(
        rule,
        label="right",
        closed="right",
    ).agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )

    result = result.dropna(subset=["Open", "High", "Low", "Close"]).copy()

    return result


def _add_bollinger(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()

    close = pd.to_numeric(work["Close"], errors="coerce")

    mid = close.rolling(20, min_periods=20).mean()
    std = close.rolling(20, min_periods=20).std(ddof=0)

    work["BB_MID"] = mid
    work["BB_UPPER"] = mid + 2 * std
    work["BB_LOWER"] = mid - 2 * std

    return work


def _fmt_price(value: Any) -> str:
    """價格最多保留 2 位小數，並移除尾端多餘的 0。"""
    try:
        number = float(value)

        if pd.isna(number):
            return "--"

        return f"{number:,.2f}".rstrip("0").rstrip(".")
    except Exception:
        return "--"


def _draw_market_future_bollinger_chart(
    df: pd.DataFrame,
    tf: str,
    contract_code: str,
    rows: int = 60,
) -> tuple[str, dict[str, Any]]:
    if df is None or df.empty:
        return "", {}

    tf = _normalize_tf(tf)
    label = _tf_label(tf)

    work = _add_bollinger(df)
    plot_df = work.tail(max(30, int(rows))).copy()

    if plot_df.empty:
        return "", {}

    latest = plot_df.iloc[-1]

    latest_close = _safe_float(latest.get("Close"))
    bb_upper = _safe_float(latest.get("BB_UPPER"))
    bb_mid = _safe_float(latest.get("BB_MID"))
    bb_lower = _safe_float(latest.get("BB_LOWER"))

    font_kwargs = _font_kwargs()

    # 單圖：刪除成交量副圖，降低高度，讓 K 線更清楚。
    fig = plt.figure(figsize=(7.0, 4.2), dpi=110, facecolor="white")

    # 上方預留空間放「現價 / BB」
    ax_k = fig.add_axes([0.10, 0.12, 0.86, 0.72])
    ax_k.set_facecolor("#F8F9FA")

    x_values = list(range(len(plot_df)))
    candle_width = 0.58

    for i, (_, row) in enumerate(plot_df.iterrows()):
        o = _safe_float(row.get("Open"))
        h = _safe_float(row.get("High"))
        l = _safe_float(row.get("Low"))
        c = _safe_float(row.get("Close"))

        color = "#FF2D2D" if c >= o else "#00B050"

        ax_k.vlines(i, l, h, linewidth=0.9, color=color, zorder=2)

        lower = min(o, c)
        height = abs(c - o)

        if height <= 0:
            height = 0.01

        ax_k.bar(
            i,
            height,
            bottom=lower,
            width=candle_width,
            color=color,
            edgecolor=color,
            align="center",
            zorder=3,
        )

    # 布林通道
    ax_k.plot(
        x_values,
        plot_df["BB_UPPER"].values,
        linewidth=1.25,
        color="#D32F2F",
        zorder=4,
    )

    ax_k.plot(
        x_values,
        plot_df["BB_MID"].values,
        linewidth=1.15,
        color="#333333",
        zorder=4,
    )

    ax_k.plot(
        x_values,
        plot_df["BB_LOWER"].values,
        linewidth=1.25,
        color="#00A84F",
        zorder=4,
    )

    _annotate_high_low(
        ax_k,
        plot_df,
        x_values,
        fontsize=12,
    )

    # 移到原本標題的位置，不跟 K 線重疊。
    bb_text = (
        f"現價 {_fmt_price(latest_close)}   "
        f"BB上 {_fmt_price(bb_upper)}   "
        f"BB中 {_fmt_price(bb_mid)}   "
        f"BB下 {_fmt_price(bb_lower)}"
    )

    fig.text(
        0.10,
        0.925,
        bb_text,
        ha="left",
        va="top",
        fontsize=13,
        fontweight="bold",
        color="#111111",
        **font_kwargs,
    )

    # 刪除圖內標題，避免和 LINE 圖卡標題重複。
    # ax_k.set_title(...) 不再使用。

    ax_k.grid(True, linestyle=":", alpha=0.35)
    ax_k.tick_params(axis="y", labelsize=9)

    labels = []

    index_dates = [idx.date() for idx in plot_df.index]
    multi_day = bool(index_dates and min(index_dates) != max(index_dates))

    for idx in plot_df.index:
        if multi_day:
            labels.append(idx.strftime("%m/%d\n%H:%M"))
        else:
            labels.append(idx.strftime("%H:%M"))

    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    if len(labels) - 1 not in ticks:
        ticks.append(len(labels) - 1)

    ax_k.set_xticks(ticks)
    ax_k.set_xticklabels(
        [labels[i] for i in ticks],
        rotation=0,
        fontsize=8,
        **font_kwargs,
    )

    for spine in ["top", "right"]:
        ax_k.spines[spine].set_visible(False)

    image_key = f"TXF_bollinger_{tf}_{rows}_{int(time.time() // TTL_MAP.get(tf, 60))}"

    try:
        image_url = publish_figure(fig, image_key) or ""
    finally:
        plt.close(fig)

    meta = {
        "latest_close": latest_close,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
        "latest_time": str(plot_df.index[-1])[:19],
        "rows": len(plot_df),
    }

    return image_url, meta

def get_market_future_kline_snapshot(
    time_frame: str = "1m",
    rows: int = 60,
) -> MarketFutureKlineSnapshot:
    tf = _normalize_tf(time_frame)
    label = _tf_label(tf)

    cache_key = f"TXF:{tf}:{rows}"
    now = time.time()
    ttl = TTL_MAP.get(tf, 60)

    cached = MARKET_FUTURE_KLINE_CACHE.get(cache_key)

    if cached:
        ts, payload = cached

        if now - ts <= ttl:
            return MarketFutureKlineSnapshot(**payload)

    api = get_api()

    if api is None:
        return MarketFutureKlineSnapshot(
            available=False,
            message="Shioaji 尚未登入，無法取得台指期 K 線。",
            time_frame=tf,
            label=label,
        )

    contract = _get_txf_contract(api)

    if contract is None:
        return MarketFutureKlineSnapshot(
            available=False,
            message="找不到台指期 TXF 近月 contract。",
            time_frame=tf,
            label=label,
        )

    contract_code = _contract_code(contract)

    df_1m = _fetch_1m_kbars(contract, tf)

    if df_1m is None or df_1m.empty:
        return MarketFutureKlineSnapshot(
            available=False,
            message="Shioaji 沒有回傳台指期 K 線資料。",
            time_frame=tf,
            label=label,
            contract_code=contract_code,
        )

    df_tf = _resample_df(df_1m, tf)

    if df_tf is None or df_tf.empty:
        return MarketFutureKlineSnapshot(
            available=False,
            message=f"台指期 {label}K 資料整理失敗。",
            time_frame=tf,
            label=label,
            contract_code=contract_code,
        )

    image_url, meta = _draw_market_future_bollinger_chart(
        df_tf,
        tf=tf,
        contract_code=contract_code,
        rows=rows,
    )

    if not image_url:
        return MarketFutureKlineSnapshot(
            available=False,
            message=f"台指期 {label}K 圖片產生失敗。",
            time_frame=tf,
            label=label,
            contract_code=contract_code,
        )

    payload = {
        "available": True,
        "message": "ok",
        "image_url": image_url,
        "time_frame": tf,
        "label": label,
        "contract_code": contract_code,
        "latest_time": str(meta.get("latest_time") or ""),
        "latest_close": _safe_float(meta.get("latest_close")),
        "bb_upper": _safe_float(meta.get("bb_upper")),
        "bb_mid": _safe_float(meta.get("bb_mid")),
        "bb_lower": _safe_float(meta.get("bb_lower")),
        "rows": int(meta.get("rows") or 0),
    }

    MARKET_FUTURE_KLINE_CACHE[cache_key] = (now, dict(payload))

    _debug(
        "snapshot",
        "| tf =",
        tf,
        "| contract =",
        contract_code,
        "| rows =",
        payload["rows"],
        "| image =",
        bool(image_url),
    )

    return MarketFutureKlineSnapshot(**payload)
