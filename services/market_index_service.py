from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.font_manager import FontProperties
import pandas as pd

try:
    import yfinance as yf
except Exception:
    yf = None

try:
    from services.sinopac_quote_service import get_api
except Exception:
    def get_api():
        return None

try:
    from services.upload_service import publish_figure
except Exception:
    def publish_figure(fig, name: str) -> str:
        return ""


MARKET_INDEX_CACHE_TTL_SECONDS = 3
MARKET_INDEX_CHART_CACHE_TTL_SECONDS = 300

_MARKET_INDEX_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MARKET_INDEX_CHART_CACHE: dict[str, tuple[float, str]] = {}


@dataclass
class MarketIndexSnapshot:
    available: bool
    message: str

    index_id: str = "TAIEX"
    index_name: str = "加權指數"

    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0

    change: float = 0.0
    change_pct: float = 0.0

    volume: int = 0
    total_volume: int = 0
    amount: int = 0
    total_amount: int = 0

    quote_time: str = ""
    quote_source: str = "永豐即時"

    chart_url: str = ""


def _debug(*args):
    print("DEBUG market_index |", *args, flush=True)


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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, float(default))))
    except Exception:
        return default


def _to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}

    if isinstance(obj, dict):
        return obj

    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass

    result: dict[str, Any] = {}

    for key in dir(obj):
        if key.startswith("_"):
            continue

        try:
            value = getattr(obj, key)

            if callable(value):
                continue

            if key in {"tick_type", "change_type"}:
                result[key] = str(value)
            else:
                result[key] = value

        except Exception:
            continue

    return result


def _normalize_ts(value: Any) -> str:
    if value is None or value == "":
        return ""

    try:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")

        ts = pd.to_datetime(value, errors="coerce")

        if pd.notna(ts):
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert("Asia/Taipei")

            return ts.strftime("%Y-%m-%d %H:%M:%S")

    except Exception:
        pass

    return str(value)


def _setup_chinese_font() -> None:
    try:
        candidates = [
            Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NotoSansTC-Regular.ttf",
            Path("/opt/render/project/src/assets/fonts/NotoSansTC-Regular.ttf"),
            Path("/mnt/data/NotoSansTC-Regular.ttf"),
        ]

        for font_path in candidates:
            if font_path.exists():
                font_manager.fontManager.addfont(str(font_path))
                font_name = FontProperties(fname=str(font_path)).get_name()
                plt.rcParams["font.family"] = font_name
                break

        plt.rcParams["axes.unicode_minus"] = False

    except Exception as exc:
        _debug("font setup failed", exc)


def _get_taiex_contract(api):
    """
    Shioaji 加權指數 contract 常見路徑：
    api.Contracts.Indexs.TSE["001"]
    或 api.Contracts.Indexs.TSE.TSE001
    """
    try:
        return api.Contracts.Indexs.TSE["001"]
    except Exception:
        pass

    try:
        return api.Contracts.Indexs.TSE.TSE001
    except Exception:
        pass

    try:
        return api.Contracts.Indexs["TSE"]["001"]
    except Exception:
        pass

    return None


def _snapshot_from_dict(data: dict[str, Any]) -> MarketIndexSnapshot:
    return MarketIndexSnapshot(
        available=bool(data.get("available")),
        message=str(data.get("message") or ""),

        index_id=str(data.get("index_id") or "TAIEX"),
        index_name=str(data.get("index_name") or "加權指數"),

        open_price=_safe_float(data.get("open_price")),
        high_price=_safe_float(data.get("high_price")),
        low_price=_safe_float(data.get("low_price")),
        close_price=_safe_float(data.get("close_price")),

        change=_safe_float(data.get("change")),
        change_pct=_safe_float(data.get("change_pct")),

        volume=_safe_int(data.get("volume")),
        total_volume=_safe_int(data.get("total_volume")),
        amount=_safe_int(data.get("amount")),
        total_amount=_safe_int(data.get("total_amount")),

        quote_time=str(data.get("quote_time") or ""),
        quote_source=str(data.get("quote_source") or "永豐即時"),

        chart_url=str(data.get("chart_url") or ""),
    )


def get_market_index_snapshot(with_chart: bool = True) -> MarketIndexSnapshot:
    """
    取得加權指數即時 snapshot。
    with_chart=True 時會附上 chart_url，但圖表會使用 5 分鐘快取。
    """
    cache_key = "TAIEX"
    now = time.time()

    cached = _MARKET_INDEX_CACHE.get(cache_key)

    if cached:
        ts, data = cached

        if now - ts <= MARKET_INDEX_CACHE_TTL_SECONDS:
            snapshot = _snapshot_from_dict(data)

            if with_chart and not snapshot.chart_url:
                snapshot.chart_url = get_market_index_chart_url(snapshot)

            return snapshot

    api = get_api()

    if api is None:
        return MarketIndexSnapshot(
            available=False,
            message="Shioaji 尚未登入，無法取得加權指數即時資料。",
        )

    contract = _get_taiex_contract(api)

    if contract is None:
        return MarketIndexSnapshot(
            available=False,
            message="找不到加權指數 contract：TSE001。",
        )

    try:
        snapshots = api.snapshots([contract])

        if not snapshots:
            return MarketIndexSnapshot(
                available=False,
                message="Shioaji 沒有回傳加權指數 snapshot。",
            )

        raw = _to_dict(snapshots[0])

        data = {
            "available": True,
            "message": "ok",
            "index_id": "TAIEX",
            "index_name": "加權指數",

            "open_price": _safe_float(raw.get("open")),
            "high_price": _safe_float(raw.get("high")),
            "low_price": _safe_float(raw.get("low")),
            "close_price": _safe_float(raw.get("close")),

            "change": _safe_float(raw.get("change_price")),
            "change_pct": _safe_float(raw.get("change_rate")),

            "volume": _safe_int(raw.get("volume")),
            "total_volume": _safe_int(raw.get("total_volume")),
            "amount": _safe_int(raw.get("amount")),
            "total_amount": _safe_int(raw.get("total_amount")),

            "quote_time": _normalize_ts(raw.get("ts")),
            "quote_source": "永豐即時",

            "chart_url": "",
        }

        # 先快取即時數字，再產圖，避免圖表失敗影響即時資料。
        _MARKET_INDEX_CACHE[cache_key] = (now, dict(data))

        snapshot = _snapshot_from_dict(data)

        if with_chart:
            data["chart_url"] = get_market_index_chart_url(snapshot)
            snapshot.chart_url = data["chart_url"]
            _MARKET_INDEX_CACHE[cache_key] = (now, dict(data))

        _debug(
            "snapshot",
            "close =",
            data["close_price"],
            "change =",
            data["change"],
            "change_pct =",
            data["change_pct"],
            "volume =",
            data["total_volume"],
            "time =",
            data["quote_time"],
            "chart_url =",
            bool(data["chart_url"]),
        )

        return snapshot

    except Exception as exc:
        _debug("snapshot failed", exc)

        return MarketIndexSnapshot(
            available=False,
            message=f"取得加權指數即時資料失敗：{exc}",
        )


def get_market_index_chart_url(snapshot: MarketIndexSnapshot | None = None) -> str:
    """
    產生加權指數日K圖：
    - K線
    - 5MA / 20MA / 60MA / 120MA
    - 成交量
    - 5 分鐘快取
    """
    cache_key = "TAIEX:D:MA"
    now = time.time()

    cached = _MARKET_INDEX_CHART_CACHE.get(cache_key)

    if cached:
        ts, url = cached

        if url and now - ts <= MARKET_INDEX_CHART_CACHE_TTL_SECONDS:
            return url

    try:
        df = _fetch_taiex_history()

        if df.empty:
            return ""

        if snapshot is not None and getattr(snapshot, "available", False):
            df = _append_snapshot_to_history(df, snapshot)

        chart_url = _generate_market_index_kline_chart(df)

        if chart_url:
            _MARKET_INDEX_CHART_CACHE[cache_key] = (now, chart_url)

        return chart_url

    except Exception as exc:
        _debug("chart failed", exc)
        return ""


def _fetch_taiex_history() -> pd.DataFrame:
    """
    抓加權指數日K歷史資料。
    第一版使用 yfinance ^TWII；失敗則回傳空表。
    """
    if yf is None:
        _debug("yfinance not available")
        return pd.DataFrame()

    try:
        raw = yf.download(
            "^TWII",
            period="10mo",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )

        if raw is None or raw.empty:
            _debug("yfinance ^TWII empty")
            return pd.DataFrame()

        df = raw.copy()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        required = ["Open", "High", "Low", "Close", "Volume"]

        for col in required:
            if col not in df.columns:
                df[col] = 0

        df = df[required].copy()

        df.index = pd.to_datetime(df.index, errors="coerce")

        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_convert("Asia/Taipei").tz_localize(None)

        df.index = df.index.normalize()

        for col in required:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        df = df[df["Close"] > 0]
        df = df.sort_index()

        return df.tail(180)

    except Exception as exc:
        _debug("fetch history failed", exc)
        return pd.DataFrame()


def _append_snapshot_to_history(df: pd.DataFrame, snapshot: MarketIndexSnapshot) -> pd.DataFrame:
    result = df.copy()

    close_price = _safe_float(getattr(snapshot, "close_price", 0.0))

    if close_price <= 0:
        return result

    quote_time = str(getattr(snapshot, "quote_time", "") or "").strip()
    ts = pd.to_datetime(quote_time, errors="coerce")

    if pd.isna(ts):
        ts = pd.Timestamp.now(tz="Asia/Taipei")

    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.tz_convert("Asia/Taipei").tz_localize(None)

    trade_date = pd.Timestamp(ts).normalize()

    open_price = _safe_float(getattr(snapshot, "open_price", 0.0)) or close_price
    high_price = _safe_float(getattr(snapshot, "high_price", 0.0)) or close_price
    low_price = _safe_float(getattr(snapshot, "low_price", 0.0)) or close_price
    volume = _safe_int(getattr(snapshot, "total_volume", 0)) or _safe_int(getattr(snapshot, "volume", 0))

    high_price = max(high_price, open_price, close_price)
    low_price = min(low_price, open_price, close_price)

    if trade_date in result.index:
        if open_price > 0:
            result.loc[trade_date, "Open"] = open_price

        result.loc[trade_date, "High"] = max(_safe_float(result.loc[trade_date, "High"]), high_price)
        result.loc[trade_date, "Low"] = min(
            _safe_float(result.loc[trade_date, "Low"]) or low_price,
            low_price,
        )
        result.loc[trade_date, "Close"] = close_price

        if volume > 0:
            result.loc[trade_date, "Volume"] = volume

    else:
        result.loc[trade_date, ["Open", "High", "Low", "Close", "Volume"]] = [
            open_price,
            high_price,
            low_price,
            close_price,
            volume,
        ]

    result = result.sort_index()

    return result.tail(180)


def _generate_market_index_kline_chart(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""

    _setup_chinese_font()

    work_df = df.copy()

    for ma in [5, 20, 60, 120]:
        work_df[f"MA{ma}"] = work_df["Close"].rolling(ma).mean()

    plot_df = work_df.tail(130).copy()

    if plot_df.empty:
        return ""

    fig = plt.figure(figsize=(7.2, 5.8), dpi=130, facecolor="white")
    gs = gridspec.GridSpec(2, 1, height_ratios=[3, 1], hspace=0.06)

    ax_k = fig.add_subplot(gs[0])
    ax_v = fig.add_subplot(gs[1], sharex=ax_k)

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
        volume = int(row["Volume"])

        color = "#FF2D2D" if close_price >= open_price else "#00B050"

        ax_k.vlines(i, low_price, high_price, linewidth=1.0, color=color)

        lower = min(open_price, close_price)
        height = abs(close_price - open_price)

        if height <= 0:
            height = 0.01

        ax_k.bar(
            i,
            height,
            bottom=lower,
            width=candle_width,
            color=color,
            align="center",
        )

        ax_v.bar(
            i,
            volume,
            width=candle_width,
            color=color,
            align="center",
        )

    ma_styles = {
        "MA5": ("MA5", "#111111", 1.1),
        "MA20": ("MA20", "#1F77B4", 1.1),
        "MA60": ("MA60", "#FF7F0E", 1.1),
        "MA120": ("MA120", "#9467BD", 1.1),
    }

    for col, (label, color, linewidth) in ma_styles.items():
        if col in plot_df.columns:
            ax_k.plot(
                x_values,
                plot_df[col].values,
                label=label,
                linewidth=linewidth,
                color=color,
            )

    latest_close = float(plot_df["Close"].iloc[-1])
    latest_date = plot_df.index[-1].strftime("%Y-%m-%d")

    ax_k.set_title(
        f"加權指數日K｜{latest_date} 收 {latest_close:,.2f}",
        fontsize=13,
        fontweight="bold",
    )

    ax_k.grid(True, linestyle=":", alpha=0.4)
    ax_v.grid(True, linestyle=":", alpha=0.35)

    ax_k.legend(loc="upper left", fontsize=8, ncol=4, frameon=False)

    labels = [idx.strftime("%m/%d") for idx in plot_df.index]
    step = max(1, len(labels) // 6)
    ticks = list(range(0, len(labels), step))

    ax_v.set_xticks(ticks)
    ax_v.set_xticklabels(
        [labels[i] for i in ticks],
        rotation=0,
        fontsize=8,
    )

    plt.setp(ax_k.get_xticklabels(), visible=False)

    ax_v.set_ylabel("成交量", fontsize=9)

    ax_k.tick_params(axis="y", labelsize=8)
    ax_v.tick_params(axis="y", labelsize=8)

    ax_k.spines["top"].set_visible(False)
    ax_k.spines["right"].set_visible(False)
    ax_v.spines["top"].set_visible(False)
    ax_v.spines["right"].set_visible(False)

    fig.tight_layout()

    try:
        return publish_figure(fig, "taiex_market_index_kline")
    finally:
        plt.close(fig)
