from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import calendar
import os

import matplotlib
matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from matplotlib import font_manager

try:
    from config import FINMIND_TOKEN
except Exception:
    FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

from services.financial_service import (
    get_financial_rows,
    sync_stock_financial_quarterly,
)
from services.upload_service import publish_figure


FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
PE_RIVER_CHART_VERSION = "2026-07-24-v2-MOBILE-READABLE"


@dataclass
class PeRiverSnapshot:
    available: bool
    message: str
    stock_id: str
    stock_name: str
    chart_url: str = ""
    latest_date: str = ""
    latest_close: float = 0.0
    latest_ttm_eps: float = 0.0
    current_pe: float = 0.0
    pe_levels: list[float] = field(default_factory=list)
    zone_label: str = ""
    source: str = "FinMind"
    rows_count: int = 0


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
        print("DEBUG pe_river font setup failed", "| error =", repr(exc), flush=True)

    plt.rcParams["font.sans-serif"] = [
        "Noto Sans CJK TC",
        "Microsoft JhengHei",
        "Arial Unicode MS",
        "DejaVu Sans",
        "sans-serif",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    return {}


def _clean_stock_id(stock_id: str) -> str:
    return str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        text = str(value).replace(",", "").replace("%", "").strip()

        if text in {"", "--", "-", "nan", "None"}:
            return default

        return float(text)

    except Exception:
        return default


def _date_days_ago(days: int) -> str:
    return (datetime.utcnow().date() - timedelta(days=int(days))).strftime("%Y-%m-%d")


def _request_finmind_stock_price(stock_id: str, days: int = 2200) -> pd.DataFrame:
    sid = _clean_stock_id(stock_id)
    start_date = _date_days_ago(days)

    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": sid,
        "start_date": start_date,
    }

    token = str(FINMIND_TOKEN or os.getenv("FINMIND_TOKEN", "") or "").strip()

    if token:
        params["token"] = token

    timeout_seconds = int(os.getenv("PE_RIVER_FINMIND_TIMEOUT_SECONDS", "10"))

    try:
        res = requests.get(
            FINMIND_URL,
            params=params,
            timeout=timeout_seconds,
        )

        if res.status_code >= 400:
            print(
                "DEBUG pe_river finmind stock price failed",
                "| stock_id =",
                sid,
                "| status =",
                res.status_code,
                "| body =",
                res.text[:200],
                flush=True,
            )
            return pd.DataFrame()

        payload = res.json()
        rows = payload.get("data") or []

        if not isinstance(rows, list) or not rows:
            return pd.DataFrame()

        raw = pd.DataFrame(rows)

        required = ["date", "close"]

        for col in required:
            if col not in raw.columns:
                return pd.DataFrame()

        out = pd.DataFrame()
        out["Date"] = pd.to_datetime(raw["date"], errors="coerce")
        out["Close"] = pd.to_numeric(raw["close"], errors="coerce")

        out = out.dropna(subset=["Date", "Close"])
        out = out[out["Close"] > 0]

        if out.empty:
            return pd.DataFrame()

        out = out.sort_values("Date")
        out = out.drop_duplicates(subset=["Date"], keep="last")
        out = out.set_index("Date")
        out.index = pd.to_datetime(out.index).tz_localize(None)
        out.attrs["source"] = "FinMind_TaiwanStockPrice"

        return out

    except Exception as exc:
        print(
            "DEBUG pe_river finmind stock price exception",
            "| stock_id =",
            sid,
            "| error =",
            repr(exc),
            flush=True,
        )
        return pd.DataFrame()


def _get_daily_close(stock_id: str, stock_name: str, days: int = 2200) -> pd.DataFrame:
    """
    優先共用 services.stock_service 的 FinMind 快取。
    若匯入失敗，再自行打 FinMind。
    """
    sid = _clean_stock_id(stock_id)

    try:
        from services.stock_service import (
            _get_finmind_daily_history,
            normalize_stock_input,
        )

        meta = normalize_stock_input(sid)
        df = _get_finmind_daily_history(meta, days=days)

        if df is not None and not df.empty and "Close" in df.columns:
            out = df[["Close"]].copy()
            out["Close"] = pd.to_numeric(out["Close"], errors="coerce")
            out = out.dropna(subset=["Close"])
            out = out[out["Close"] > 0]

            if not out.empty:
                out.attrs["source"] = str(getattr(df, "attrs", {}).get("price_source") or "stock_service")
                return out

    except Exception as exc:
        print(
            "DEBUG pe_river stock_service daily fallback",
            "| stock_id =",
            sid,
            "| error =",
            repr(exc),
            flush=True,
        )

    return _request_finmind_stock_price(sid, days=days)


def _quarter_end_date(year: int, quarter: int) -> pd.Timestamp:
    quarter = int(quarter)
    month = quarter * 3
    day = calendar.monthrange(int(year), month)[1]
    return pd.Timestamp(year=int(year), month=month, day=day)


def _prepare_eps_ttm_rows(stock_id: str, stock_name: str) -> pd.DataFrame:
    sid = _clean_stock_id(stock_id)

    limit = int(os.getenv("PE_RIVER_EPS_LIMIT", "40"))
    rows = get_financial_rows(sid, limit=limit)

    # 若資料不足，嘗試同步一次。
    if len(rows or []) < 8:
        try:
            sync_stock_financial_quarterly(
                stock_id=sid,
                stock_name=stock_name,
            )
            rows = get_financial_rows(sid, limit=limit)
        except Exception as exc:
            print(
                "DEBUG pe_river financial auto sync failed",
                "| stock_id =",
                sid,
                "| error =",
                repr(exc),
                flush=True,
            )

    records: list[dict[str, Any]] = []

    eps_lag_days = int(os.getenv("PE_RIVER_EPS_LAG_DAYS", "0"))

    for row in rows or []:
        year = int(_safe_float(row.get("fiscal_year"), 0))
        quarter = int(_safe_float(row.get("fiscal_quarter"), 0))
        ttm_eps = _safe_float(row.get("ttm_eps"), 0.0)

        if year <= 0 or quarter <= 0 or ttm_eps <= 0:
            continue

        try:
            effective_date = _quarter_end_date(year, quarter) + pd.Timedelta(days=eps_lag_days)
        except Exception:
            continue

        label = str(row.get("quarter_label") or f"{year % 100:02d}Q{quarter}")

        records.append(
            {
                "effective_date": effective_date,
                "quarter_label": label,
                "ttm_eps": float(ttm_eps),
            }
        )

    if not records:
        return pd.DataFrame()

    eps_df = pd.DataFrame(records)
    eps_df = eps_df.sort_values("effective_date")
    eps_df = eps_df.drop_duplicates(subset=["effective_date"], keep="last")

    return eps_df


def _zone_label(current_pe: float, levels: list[float]) -> str:
    if current_pe <= 0 or len(levels) < 6:
        return "--"

    p10, p25, p40, p60, p75, p90 = levels[:6]

    if current_pe <= p10:
        return "低估區"

    if current_pe <= p25:
        return "偏低區"

    if current_pe <= p40:
        return "合理偏低"

    if current_pe <= p60:
        return "合理區"

    if current_pe <= p75:
        return "偏高區"

    if current_pe <= p90:
        return "高估區"

    return "極高區"


def _make_pe_river_chart(
    stock_id: str,
    stock_name: str,
    weekly: pd.DataFrame,
    levels: list[float],
    latest_close: float,
    current_pe: float,
    zone_label: str,
) -> str:
    font_kwargs = _setup_font()

    # LINE 圖卡會把圖片縮到約 300～360px 寬，因此保留原本長寬比，
    # 但移除卡片上已經出現的重複標題與圖例，讓真正的繪圖區放大。
    fig, ax = plt.subplots(figsize=(9.6, 6.2), dpi=100, facecolor="white")
    ax.set_facecolor("#FAFBFC")

    x = weekly.index
    ttm = weekly["ttm_eps"].astype(float)

    level_series = []

    for level in levels:
        level_series.append(ttm * float(level))

    band_colors = [
        "#DFF3E3",
        "#EAF6D8",
        "#FFF3C4",
        "#FFE2B8",
        "#FFD0C2",
    ]

    # 河流區間
    for i in range(len(level_series) - 1):
        ax.fill_between(
            x,
            level_series[i].values,
            level_series[i + 1].values,
            color=band_colors[i % len(band_colors)],
            alpha=0.88,
            linewidth=0,
        )

    # 分位線
    line_colors = ["#4CAF50", "#8BC34A", "#F9A825", "#FB8C00", "#E53935", "#8E24AA"]

    for idx, series in enumerate(level_series):
        ax.plot(
            x,
            series.values,
            linewidth=1.55,
            color=line_colors[idx % len(line_colors)],
            alpha=0.9,
        )

    ax.plot(
        x,
        weekly["Close"].astype(float).values,
        linewidth=3.1,
        color="#111111",
        zorder=5,
    )

    ax.scatter(
        [x[-1]],
        [latest_close],
        s=82,
        color="#111111",
        edgecolor="white",
        linewidth=1.5,
        zorder=6,
    )

    # 最新價直接標在最後一點，縮小後仍能辨識目前位置。
    ax.annotate(
        f"{latest_close:,.2f}",
        xy=(x[-1], latest_close),
        xytext=(-12, 14),
        textcoords="offset points",
        ha="right",
        va="bottom",
        fontsize=16,
        fontweight="bold",
        color="#111111",
        bbox={
            "boxstyle": "round,pad=0.25",
            "facecolor": "white",
            "edgecolor": "#9CA3AF",
            "alpha": 0.96,
        },
        zorder=7,
        **font_kwargs,
    )

    ax.set_ylabel("股價", fontsize=17, labelpad=10, **font_kwargs)
    ax.grid(
        True,
        axis="y",
        linestyle="--",
        linewidth=0.9,
        color="#9CA3AF",
        alpha=0.48,
    )

    # 近一年顯示，X 軸用月份，比年份更直觀。
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y/%m"))

    ax.set_xlabel("日期", fontsize=17, labelpad=8, **font_kwargs)
    ax.tick_params(axis="x", labelsize=15, pad=6)
    ax.tick_params(axis="y", labelsize=15, pad=5)
    ax.margins(x=0.025, y=0.075)

    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)

    for spine in ["left", "bottom"]:
        ax.spines[spine].set_color("#6B7280")
        ax.spines[spine].set_linewidth(1.1)

    fig.subplots_adjust(left=0.105, right=0.975, bottom=0.145, top=0.965)

    try:
        return publish_figure(fig, f"{stock_id}_pe_river")
    finally:
        plt.close(fig)


def get_pe_river_snapshot(stock_id: str, stock_name: str = "") -> PeRiverSnapshot:
    sid = _clean_stock_id(stock_id)
    name = str(stock_name or "").strip()

    if not sid:
        return PeRiverSnapshot(
            available=False,
            message="股票代碼空白，無法產生本益比河流圖。",
            stock_id=sid,
            stock_name=name,
        )

    days = int(os.getenv("PE_RIVER_DAYS", "2200"))

    daily = _get_daily_close(sid, name, days=days)

    if daily is None or daily.empty:
        return PeRiverSnapshot(
            available=False,
            message="目前抓不到股價歷史資料，無法產生本益比河流圖。",
            stock_id=sid,
            stock_name=name,
        )

    eps_df = _prepare_eps_ttm_rows(sid, name)

    if eps_df is None or eps_df.empty:
        return PeRiverSnapshot(
            available=False,
            message="目前查無近四季 EPS，無法產生本益比河流圖。",
            stock_id=sid,
            stock_name=name,
        )

    weekly = (
        daily[["Close"]]
        .resample("W-FRI")
        .last()
        .dropna(subset=["Close"])
        .copy()
    )

    # 最後一筆 index 盡量改成實際最新交易日，避免週五未到時顯示未來日期。
    try:
        idx = list(weekly.index)
        idx[-1] = daily.index[-1]
        weekly.index = pd.DatetimeIndex(idx)
        weekly = weekly[~weekly.index.duplicated(keep="last")]
    except Exception:
        pass

    if len(weekly) < 80:
        return PeRiverSnapshot(
            available=False,
            message="股價歷史資料不足，暫時無法產生本益比河流圖。",
            stock_id=sid,
            stock_name=name,
        )

    # reset_index() 後，如果原本 index 沒有名稱，欄位會叫 "index"；
    # 如果 index 名稱是 Date，欄位才會叫 "Date"。
    # Render 上這次錯誤就是 left_on="date" 找不到欄位，所以這裡統一改名。
    weekly_for_merge = weekly.reset_index()
    weekly_date_col = weekly_for_merge.columns[0]
    weekly_for_merge = weekly_for_merge.rename(columns={weekly_date_col: "date"})
    weekly_for_merge["date"] = pd.to_datetime(weekly_for_merge["date"], errors="coerce")
    weekly_for_merge = weekly_for_merge.dropna(subset=["date"]).sort_values("date")

    eps_for_merge = eps_df.rename(columns={"effective_date": "eps_date"}).copy()
    eps_for_merge["eps_date"] = pd.to_datetime(eps_for_merge["eps_date"], errors="coerce")
    eps_for_merge = eps_for_merge.dropna(subset=["eps_date"]).sort_values("eps_date")

    merged = pd.merge_asof(
        weekly_for_merge,
        eps_for_merge,
        left_on="date",
        right_on="eps_date",
        direction="backward",
    )

    merged = merged.dropna(subset=["Close", "ttm_eps"])
    merged = merged[merged["ttm_eps"] > 0]
    merged["pe"] = merged["Close"].astype(float) / merged["ttm_eps"].astype(float)

    # 過度極端值會讓河流失真，先做基本濾除。
    pe_max = float(os.getenv("PE_RIVER_MAX_PE", "200"))
    merged = merged[(merged["pe"] > 0) & (merged["pe"] <= pe_max)]

    if len(merged) < 60:
        return PeRiverSnapshot(
            available=False,
            message="有效 PE 歷史資料不足，暫時無法產生本益比河流圖。",
            stock_id=sid,
            stock_name=name,
        )

    percentiles = [10, 25, 40, 60, 75, 90]
    levels = [round(float(np.percentile(merged["pe"].values, p)), 2) for p in percentiles]

    chart_df = merged.set_index("date")[["Close", "ttm_eps", "pe"]].copy()
    chart_df = chart_df.tail(int(os.getenv("PE_RIVER_MAX_WEEKS", "52")))

    latest = chart_df.iloc[-1]
    latest_close = float(latest["Close"])
    latest_ttm_eps = float(latest["ttm_eps"])
    current_pe = float(latest["pe"])
    zone = _zone_label(current_pe, levels)

    latest_date = ""

    try:
        latest_date = chart_df.index[-1].strftime("%Y-%m-%d")
    except Exception:
        latest_date = str(chart_df.index[-1])

    chart_url = _make_pe_river_chart(
        sid,
        name,
        chart_df,
        levels,
        latest_close,
        current_pe,
        zone,
    )

    print(
        "DEBUG pe_river snapshot",
        "| stock_id =",
        sid,
        "| rows =",
        len(chart_df),
        "| display_weeks =",
        int(os.getenv("PE_RIVER_MAX_WEEKS", "52")),
        "| latest_date =",
        latest_date,
        "| close =",
        latest_close,
        "| ttm_eps =",
        latest_ttm_eps,
        "| current_pe =",
        current_pe,
        "| levels =",
        levels,
        "| chart_url =",
        bool(chart_url),
        "| chart_version =",
        PE_RIVER_CHART_VERSION,
        flush=True,
    )

    return PeRiverSnapshot(
        available=bool(chart_url),
        message="ok" if chart_url else "本益比河流圖產生失敗。",
        stock_id=sid,
        stock_name=name,
        chart_url=chart_url or "",
        latest_date=latest_date,
        latest_close=latest_close,
        latest_ttm_eps=latest_ttm_eps,
        current_pe=current_pe,
        pe_levels=levels,
        zone_label=zone,
        rows_count=len(chart_df),
    )
