from __future__ import annotations

from services.sinopac_quote_service import (
    append_stock_snapshot_to_intraday_df_fast,
    get_stock_intraday_kbars,
    get_stock_intraday_yahoo_direct,
    get_stock_snapshot as get_shioaji_stock_snapshot,
    is_shioaji_api_ready,
)
from services.market_margin_service import get_market_margin_snapshot

from services.financial_service import get_financial_snapshot

from typing import Any

from services.chart_service import (
    generate_chip_chart,
    generate_instant_chart,
    generate_kline_chart,
)
from services.chip_service import (
    get_institutional_chips,
    get_large_holder_table,
    get_margin_table,
)
from services.market_chip_service import get_market_chip_snapshot
from services.futures_service import get_stock_futures_snapshot
from services.market_future_service import get_market_future_snapshot
from services.market_index_service import get_market_index_snapshot
from services.stock_service import (
    build_price_meta,
    get_history,
    get_stock_name,
    normalize_stock_input,
)
from utils.formatter import normalize_time_frame
from utils.parser import BotRequest
UP_COLOR = "#FF2D2D"
DOWN_COLOR = "#00B050"
FLAT_COLOR = "#666666"
ACTIVE_COLOR = "#16C957"
INACTIVE_COLOR = "#D9DDE3"
import traceback

from datetime import datetime, time
from zoneinfo import ZoneInfo
from services.financial_service import get_financial_snapshot

def _is_tw_stock_live_session() -> bool:
    """
    台股一般盤時間：
    09:00 ~ 13:30
    預留一點緩衝可抓到收盤最後資料。
    """
    now_tpe = datetime.now(ZoneInfo("Asia/Taipei")).time()
    return time(9, 0) <= now_tpe <= time(13, 35)

def _normalize_action(action: str | None) -> str:
    action = str(action or "").strip().lower()

    aliases = {
        "": "instant",

        # 加權指數 / 大盤
        "market_index": "market_index",
        "index": "market_index",
        "taiex": "market_index",
        "大盤": "market_index",
        "指數": "market_index",
        "加權": "market_index",
        "加權指數": "market_index",

        "market_k": "market_k",
        "index_k": "market_k",
        "大盤k": "market_k",
        "加權k": "market_k",
        "加權k線": "market_k",

        "market_chip": "market_chip",
        "index_chip": "market_chip",
        "大盤法人": "market_chip",
        "加權法人": "market_chip",

        "market_margin": "market_margin",
        "index_margin": "market_margin",
        "大盤融資券": "market_margin",
        "加權融資券": "market_margin",

        # 大盤 / 加權指數期貨：台指期 TXF
        "market_future": "market_future_day",
        "market_future_day": "market_future_day",
        "market_future_all": "market_future_all",
        "index_future": "market_future_day",
        "taiex_future": "market_future_day",
        "txf": "market_future_day",
        "台指期": "market_future_day",
        "大盤期貨": "market_future_day",
        "加權期貨": "market_future_day",
        "台指期日盤": "market_future_day",
        "台指期全盤": "market_future_all",
        "大盤期貨日盤": "market_future_day",
        "大盤期貨全盤": "market_future_all",
        
        # 即時
        "realtime": "instant",
        "real_time": "instant",
        "instant": "instant",
        "即時": "instant",

        # K 線
        "k": "k_line",
        "kline": "k_line",
        "k_line": "k_line",
        "k線": "k_line",
        "k線圖": "k_line",

        # 法人
        "chip": "chip",
        "chips": "chip",
        "institutional": "chip",
        "institution": "chip",
        "legal": "chip",
        "legal_person": "chip",
        "legalperson": "chip",
        "legal-person": "chip",
        "法人": "chip",

        # 大戶
        "large": "large_holder",
        "large_holder": "large_holder",
        "largeholder": "large_holder",
        "big": "large_holder",
        "big_holder": "large_holder",
        "major_holder": "large_holder",
        "holder": "large_holder",
        "大戶": "large_holder",

        # EPS
        "financial": "financial",
        "finance": "financial",
        "fundamental": "financial",
        "財務": "financial",
        "eps": "financial",

        # 融資券
        "margin": "margin",
        "margin_short": "margin",
        "margin-short": "margin",
        "short": "margin",
        "credit": "margin",
        "融資券": "margin",

        # 期貨
        "futures": "futures",
        "future": "futures",
        "期貨": "futures",
        
        "futures_day": "futures_day",
        "期貨日盤": "futures_day",
        
        "futures_all": "futures_all",
        "期貨全盤": "futures_all",
    }

    return aliases.get(action, action)

def _fmt_margin_int(value, signed: bool = False) -> str:
    try:
        num = int(float(value))

        if signed:
            sign = "+" if num > 0 else ""
            return f"{sign}{num:,}"

        return f"{num:,}"

    except Exception:
        return "--"


def _fmt_margin_money_yi(value, signed: bool = False) -> str:
    try:
        num = float(value) / 100_000_000

        if signed:
            sign = "+" if num > 0 else ""
            return f"{sign}{num:,.2f} 億"

        return f"{num:,.2f} 億"

    except Exception:
        return "--"


def _fmt_margin_ratio(value) -> str:
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return "--"


def _margin_change_color(value) -> str:
    try:
        num = float(value)

        if num > 0:
            return "#FF2D2D"

        if num < 0:
            return "#00B050"

    except Exception:
        pass

    return "#666666"


def _fmt_margin_mmdd(date_text: str) -> str:
    text = str(date_text or "").strip()

    if len(text) >= 10 and "-" in text:
        return text[5:10].replace("-", "/")

    return text.replace("-", "/")


def _build_market_margin_flex(snapshot) -> dict[str, Any]:
    """
    大盤融資券卡片。
    """

    def _summary_row(label: str, value: str, color: str = "#222222") -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "sm",
                    "color": "#666666",
                    "flex": 4,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": str(value),
                    "size": "sm",
                    "color": color,
                    "weight": "bold",
                    "flex": 6,
                    "align": "end",
                    "wrap": True,
                },
            ],
        }

    def _cell(
        text: str,
        flex: int,
        color: str = "#333333",
        weight: str = "regular",
        align: str = "end",
    ) -> dict[str, Any]:
        return {
            "type": "text",
            "text": str(text),
            "size": "xs",
            "color": color,
            "weight": weight,
            "flex": flex,
            "align": align,
            "wrap": True,
        }

    def _table_header() -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "6px",
            "backgroundColor": "#EEF1F4",
            "cornerRadius": "sm",
            "contents": [
                _cell("日期", 2, "#555555", "bold", "start"),
                _cell("融資增減", 3, "#555555", "bold", "end"),
                _cell("融券增減", 3, "#555555", "bold", "end"),
                _cell("資券比", 2, "#555555", "bold", "end"),
            ],
        }

    def _table_row(item: dict) -> dict[str, Any]:
        margin_change = int(item.get("margin_change") or 0)
        short_change = int(item.get("short_change") or 0)
        ratio = float(item.get("margin_short_ratio") or 0)

        return {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "6px",
            "contents": [
                _cell(_fmt_margin_mmdd(item.get("date", "--")), 2, "#333333", "regular", "start"),
                _cell(_fmt_margin_int(margin_change, signed=True), 3, _margin_change_color(margin_change)),
                _cell(_fmt_margin_int(short_change, signed=True), 3, _margin_change_color(short_change)),
                _cell(_fmt_margin_ratio(ratio), 2, "#333333"),
            ],
        }

    if not getattr(snapshot, "available", False):
        contents: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "大盤融資券",
                "size": "xxl",
                "weight": "bold",
                "color": "#111111",
                "wrap": True,
            },
            {
                "type": "separator",
                "margin": "md",
            },
            {
                "type": "text",
                "text": getattr(snapshot, "message", "查無大盤融資券資料。"),
                "size": "sm",
                "color": "#666666",
                "wrap": True,
                "margin": "md",
            },
            {
                "type": "separator",
                "margin": "md",
            },
        ]

        contents.extend(_market_index_buttons("market_margin"))

        return {
            "type": "flex",
            "altText": "大盤融資券",
            "contents": {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": contents,
                },
            },
        }

    latest_date = str(getattr(snapshot, "latest_date", "") or "--")

    margin_balance = int(getattr(snapshot, "margin_balance", 0) or 0)
    margin_change = int(getattr(snapshot, "margin_change", 0) or 0)
    margin_money_balance = int(getattr(snapshot, "margin_money_balance", 0) or 0)
    margin_money_change = int(getattr(snapshot, "margin_money_change", 0) or 0)

    short_balance = int(getattr(snapshot, "short_balance", 0) or 0)
    short_change = int(getattr(snapshot, "short_change", 0) or 0)

    ratio = float(getattr(snapshot, "margin_short_ratio", 0.0) or 0.0)

    recent_rows = list(getattr(snapshot, "recent_rows", []) or [])[-10:]
    recent_rows = list(reversed(recent_rows))

    table_contents: list[dict[str, Any]] = [_table_header()]

    if recent_rows:
        for item in recent_rows:
            table_contents.append(_table_row(item))
    else:
        table_contents.append(
            {
                "type": "text",
                "text": "暫無近10日資料",
                "size": "sm",
                "color": "#999999",
                "margin": "sm",
            }
        )

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "大盤融資券",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"最新日期：{latest_date}",
            "size": "sm",
            "color": "#666666",
            "margin": "xs",
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "margin": "md",
            "contents": [
                _summary_row("融資餘額", _fmt_margin_int(margin_balance), "#222222"),
                _summary_row("融資增減", _fmt_margin_int(margin_change, signed=True), _margin_change_color(margin_change)),
                _summary_row("融券餘額", _fmt_margin_int(short_balance), "#222222"),
                _summary_row("融券增減", _fmt_margin_int(short_change, signed=True), _margin_change_color(short_change)),
                _summary_row("資券比", _fmt_margin_ratio(ratio), "#222222"),
                _summary_row("融資金額", _fmt_margin_money_yi(margin_money_balance), "#222222"),
                _summary_row("融資金額增減", _fmt_margin_money_yi(margin_money_change, signed=True), _margin_change_color(margin_money_change)),
            ],
        },
        {
            "type": "text",
            "text": "近5日融資融券變化",
            "size": "md",
            "weight": "bold",
            "color": "#222222",
            "margin": "md",
        },
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "margin": "sm",
            "paddingAll": "6px",
            "backgroundColor": "#F8F9FA",
            "cornerRadius": "md",
            "contents": table_contents,
        },
        {
            "type": "text",
            "text": "融資/融券單位：張；融資金額單位：元換算億元；盤後資料。",
            "size": "xs",
            "color": "#888888",
            "wrap": True,
            "margin": "md",
        },
        {
            "type": "separator",
            "margin": "md",
        },
    ]

    contents.extend(_market_index_buttons("market_margin"))

    return {
        "type": "flex",
        "altText": "大盤融資券",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": contents,
            },
        },
    }

def _fmt_market_chip_yi(value) -> str:
    try:
        num = float(value)
        sign = "+" if num > 0 else ""
        return f"{sign}{num:,.2f} 億"
    except Exception:
        return "--"

def _market_chip_color(value) -> str:
    try:
        num = float(value)

        if num > 0:
            return "#FF2D2D"

        if num < 0:
            return "#00B050"

    except Exception:
        pass

    return "#666666"


def _market_chip_label(value) -> str:
    try:
        num = float(value)

        if num > 0:
            return "買超"

        if num < 0:
            return "賣超"

    except Exception:
        pass

    return "持平"


def _fmt_market_chip_yi(value, with_unit: bool = True) -> str:
    try:
        num = float(value)
        sign = "+" if num > 0 else ""
        unit = "億" if with_unit else ""
        return f"{sign}{num:,.2f}{unit}"
    except Exception:
        return "--"


def _market_chip_color(value) -> str:
    try:
        num = float(value)

        if num > 0:
            return "#FF2D2D"

        if num < 0:
            return "#00B050"

    except Exception:
        pass

    return "#666666"


def _fmt_mmdd(date_text: str) -> str:
    text = str(date_text or "").strip()

    if len(text) >= 10 and "-" in text:
        return text[5:10].replace("-", "/")

    return text.replace("-", "/")


def _build_market_chip_flex(snapshot) -> dict[str, Any]:
    """
    大盤法人卡片。
    顯示方式：
    日期 | 外資 | 投信 | 自營商
    """

    def _cell(
        text: str,
        flex: int,
        color: str = "#333333",
        weight: str = "regular",
        align: str = "end",
    ) -> dict[str, Any]:
        return {
            "type": "text",
            "text": str(text),
            "size": "xs",
            "color": color,
            "weight": weight,
            "flex": flex,
            "align": align,
            "wrap": True,
        }

    def _table_header() -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "6px",
            "backgroundColor": "#EEF1F4",
            "cornerRadius": "sm",
            "contents": [
                _cell("日期", 2, "#555555", "bold", "start"),
                _cell("外資", 3, "#555555", "bold", "end"),
                _cell("投信", 3, "#555555", "bold", "end"),
                _cell("自營商", 3, "#555555", "bold", "end"),
            ],
        }

    def _table_row(item: dict) -> dict[str, Any]:
        foreign = float(item.get("foreign") or 0)
        investment_trust = float(item.get("investment_trust") or 0)
        dealer = float(item.get("dealer") or 0)

        return {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "6px",
            "contents": [
                _cell(_fmt_mmdd(item.get("date", "--")), 2, "#333333", "regular", "start"),
                _cell(_fmt_market_chip_yi(foreign), 3, _market_chip_color(foreign)),
                _cell(_fmt_market_chip_yi(investment_trust), 3, _market_chip_color(investment_trust)),
                _cell(_fmt_market_chip_yi(dealer), 3, _market_chip_color(dealer)),
            ],
        }

    def _summary_row(label: str, value, color: str | None = None) -> dict[str, Any]:
        if color is None:
            color = _market_chip_color(value)

        return {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "sm",
                    "color": "#666666",
                    "flex": 4,
                },
                {
                    "type": "text",
                    "text": _fmt_market_chip_yi(value),
                    "size": "sm",
                    "color": color,
                    "weight": "bold",
                    "flex": 6,
                    "align": "end",
                    "wrap": True,
                },
            ],
        }

    if not getattr(snapshot, "available", False):
        contents: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "大盤法人",
                "size": "xxl",
                "weight": "bold",
                "color": "#111111",
                "wrap": True,
            },
            {
                "type": "separator",
                "margin": "md",
            },
            {
                "type": "text",
                "text": getattr(snapshot, "message", "查無大盤法人資料。"),
                "size": "sm",
                "color": "#666666",
                "wrap": True,
                "margin": "md",
            },
            {
                "type": "separator",
                "margin": "md",
            },
        ]

        contents.extend(_market_index_buttons("market_chip"))

        return {
            "type": "flex",
            "altText": "大盤法人",
            "contents": {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": contents,
                },
            },
        }

    latest_date = str(getattr(snapshot, "latest_date", "") or "--")
    foreign = float(getattr(snapshot, "foreign", 0.0) or 0.0)
    investment_trust = float(getattr(snapshot, "investment_trust", 0.0) or 0.0)
    dealer = float(getattr(snapshot, "dealer", 0.0) or 0.0)
    total = float(getattr(snapshot, "total", 0.0) or 0.0)

    recent_rows = list(getattr(snapshot, "recent_rows", []) or [])[-10:]
    recent_rows = list(reversed(recent_rows))

    table_contents: list[dict[str, Any]] = [_table_header()]

    if recent_rows:
        for item in recent_rows:
            table_contents.append(_table_row(item))
    else:
        table_contents.append(
            {
                "type": "text",
                "text": "暫無近10日資料",
                "size": "sm",
                "color": "#999999",
                "margin": "sm",
            }
        )

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "大盤法人",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"最新日期：{latest_date}",
            "size": "sm",
            "color": "#666666",
            "margin": "xs",
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "margin": "md",
            "contents": [
                _summary_row("外資", foreign),
                _summary_row("投信", investment_trust),
                _summary_row("自營商", dealer),
                {
                    "type": "separator",
                    "margin": "sm",
                },
                _summary_row("三大法人合計", total),
            ],
        },
        {
            "type": "text",
            "text": "近10日買賣超",
            "size": "md",
            "weight": "bold",
            "color": "#222222",
            "margin": "md",
        },
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "margin": "sm",
            "paddingAll": "6px",
            "backgroundColor": "#F8F9FA",
            "cornerRadius": "md",
            "contents": table_contents,
        },
        {
            "type": "text",
            "text": "單位：億元；盤後資料，非即時逐筆。",
            "size": "xs",
            "color": "#888888",
            "wrap": True,
            "margin": "md",
        },
        {
            "type": "separator",
            "margin": "md",
        },
    ]

    contents.extend(_market_index_buttons("market_chip"))

    return {
        "type": "flex",
        "altText": "大盤法人",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": contents,
            },
        },
    }

def _clone_meta_with_yf_symbol(meta, yf_symbol: str):
    """
    複製 meta，並只替換 yf_symbol。
    用途：3081.TW 抓不到時，改試 3081.TWO。
    """
    try:
        from dataclasses import is_dataclass, replace

        if is_dataclass(meta):
            return replace(meta, yf_symbol=yf_symbol)
    except Exception:
        pass

    try:
        import copy

        cloned = copy.copy(meta)
        setattr(cloned, "yf_symbol", yf_symbol)
        return cloned

    except Exception:
        pass

    # 最後備援：做一個簡單 proxy。
    from types import SimpleNamespace

    data = {}

    for key in [
        "stock_id",
        "stock_name",
        "name",
        "yf_symbol",
        "input_text",
    ]:
        try:
            data[key] = getattr(meta, key)
        except Exception:
            pass

    data["yf_symbol"] = yf_symbol

    return SimpleNamespace(**data)


def _is_empty_history_df(df) -> bool:
    try:
        return df is None or len(df) == 0
    except Exception:
        return True


def _is_yfinance_rate_limit_error(exc: Exception) -> bool:
    text = repr(exc)

    return (
        "YFRateLimitError" in text
        or "Too Many Requests" in text
        or "Rate limited" in text
    )


def _get_history_df_tf(meta, requested_tf):
    """
    取得個股行情資料。

    1m / 5m 優先順序：
    1. Yahoo chart API direct
    2. Shioaji kbars
    3. 原本 get_history() / yfinance

    D / W / M：
    維持原本 get_history()。
    """
    import os
    import time

    t0 = time.perf_counter()

    tf = str(requested_tf or "D").strip()
    stock_id = str(getattr(meta, "stock_id", "") or "").strip()
    yf_symbol = str(getattr(meta, "yf_symbol", "") or "").strip()

    if tf in {"1m", "5m"}:
        # -------------------------
        # 1. Yahoo chart API direct
        # -------------------------
        try:
            t_yahoo0 = time.perf_counter()

            df = get_stock_intraday_yahoo_direct(
                stock_id=stock_id,
                yf_symbol=yf_symbol,
                time_frame=tf,
                timeout=int(os.getenv("YAHOO_DIRECT_TIMEOUT_SECONDS", "5")),
            )

            t_yahoo1 = time.perf_counter()

            if df is not None and not df.empty:
                print(
                    "_get_history_df_tf | source=yahoo_direct",
                    "| stock_id =",
                    stock_id,
                    "| yf_symbol =",
                    yf_symbol,
                    "| requested_tf=" + str(requested_tf),
                    "| tf=" + tf,
                    "| rows=",
                    len(df),
                    "| sec=",
                    round(t_yahoo1 - t_yahoo0, 3),
                    "| total_sec=",
                    round(time.perf_counter() - t0, 3),
                    flush=True,
                )

                return df, tf

            print(
                "_get_history_df_tf | source=yahoo_direct_empty",
                "| stock_id =",
                stock_id,
                "| yf_symbol =",
                yf_symbol,
                "| requested_tf=" + str(requested_tf),
                "| tf=" + tf,
                "| sec=",
                round(t_yahoo1 - t_yahoo0, 3),
                flush=True,
            )

        except Exception as exc:
            print(
                "_get_history_df_tf | source=yahoo_direct_failed",
                "| stock_id =",
                stock_id,
                "| yf_symbol =",
                yf_symbol,
                "| requested_tf=" + str(requested_tf),
                "| tf=" + tf,
                "| error=",
                repr(exc),
                flush=True,
            )

        # -------------------------
        # 2. Shioaji kbars fallback
        # -------------------------
        use_shioaji_kbars = str(os.getenv("USE_SHIOAJI_KBARS_FALLBACK", "1")).strip() != "0"

        if use_shioaji_kbars:
            try:
                t_shioaji0 = time.perf_counter()

                df = get_stock_intraday_kbars(stock_id, time_frame=tf, days=1)

                t_shioaji1 = time.perf_counter()

                if df is not None and not df.empty:
                    print(
                        "_get_history_df_tf | source=shioaji_kbars",
                        "| stock_id =",
                        stock_id,
                        "| requested_tf=" + str(requested_tf),
                        "| tf=" + tf,
                        "| rows=",
                        len(df),
                        "| sec=",
                        round(t_shioaji1 - t_shioaji0, 3),
                        "| total_sec=",
                        round(time.perf_counter() - t0, 3),
                        flush=True,
                    )

                    return df, tf

                print(
                    "_get_history_df_tf | source=shioaji_kbars_empty",
                    "| stock_id =",
                    stock_id,
                    "| requested_tf=" + str(requested_tf),
                    "| tf=" + tf,
                    "| sec=",
                    round(t_shioaji1 - t_shioaji0, 3),
                    flush=True,
                )

            except Exception as exc:
                print(
                    "_get_history_df_tf | source=shioaji_kbars_failed",
                    "| stock_id =",
                    stock_id,
                    "| requested_tf=" + str(requested_tf),
                    "| tf=" + tf,
                    "| error=",
                    repr(exc),
                    flush=True,
                )

    # -------------------------
    # 3. 原本 get_history() fallback
    # -------------------------
    t_history0 = time.perf_counter()

    try:
        result = get_history(meta, requested_tf)

        t_history1 = time.perf_counter()

        if isinstance(result, tuple):
            df, tf = result
        else:
            df = result
            tf = requested_tf

        print(
            "_get_history_df_tf | source=get_history",
            "| stock_id =",
            stock_id,
            "| yf_symbol =",
            yf_symbol,
            "| requested_tf=" + str(requested_tf),
            "| tf=" + str(tf),
            "| df_type=" + str(type(df)),
            "| rows=",
            0 if df is None else len(df),
            "| sec=",
            round(t_history1 - t_history0, 3),
            "| total_sec=",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )

        return df, tf

    except Exception as exc:
        print(
            "_get_history_df_tf | source=get_history_failed",
            "| stock_id =",
            stock_id,
            "| yf_symbol =",
            yf_symbol,
            "| requested_tf=" + str(requested_tf),
            "| error=",
            repr(exc),
            "| sec=",
            round(time.perf_counter() - t_history0, 3),
            "| total_sec=",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )

        raise

def _get_history_df_tf_safe(meta, requested_tf: str):
    """
    安全版歷史資料取得。

    先用原本 meta.yf_symbol 查。
    如果失敗或空資料，且 symbol 是 .TW，改試 .TWO。
    適用 3081 這類上櫃股票。
    """
    original_symbol = str(getattr(meta, "yf_symbol", "") or "").strip()
    stock_id = str(getattr(meta, "stock_id", "") or "").strip()

    try:
        df, tf = _get_history_df_tf(meta, requested_tf)

        if not _is_empty_history_df(df):
            return df, tf

        print(
            "DEBUG history empty",
            "| stock_id =",
            stock_id,
            "| yf_symbol =",
            original_symbol,
            "| requested_tf =",
            requested_tf,
            flush=True,
        )

    except Exception as exc:
        if not _is_yfinance_rate_limit_error(exc):
            raise

        print(
            "DEBUG yfinance rate limited on first try",
            "| stock_id =",
            stock_id,
            "| yf_symbol =",
            original_symbol,
            "| requested_tf =",
            requested_tf,
            "| error =",
            repr(exc),
            flush=True,
        )

    # .TW 失敗時，改試 .TWO。
    # 例如 3081 聯亞：3081.TWO。
    fallback_symbol = ""

    if stock_id:
        fallback_symbol = f"{stock_id}.TWO"

    if fallback_symbol and fallback_symbol != original_symbol:
        try:
            meta2 = _clone_meta_with_yf_symbol(meta, fallback_symbol)

            print(
                "DEBUG history retry with TWO",
                "| stock_id =",
                stock_id,
                "| old_symbol =",
                original_symbol,
                "| new_symbol =",
                fallback_symbol,
                "| requested_tf =",
                requested_tf,
                flush=True,
            )

            df2, tf2 = _get_history_df_tf(meta2, requested_tf)

            if not _is_empty_history_df(df2):
                return df2, tf2

            print(
                "DEBUG history TWO empty",
                "| stock_id =",
                stock_id,
                "| yf_symbol =",
                fallback_symbol,
                "| requested_tf =",
                requested_tf,
                flush=True,
            )

        except Exception as exc2:
            if _is_yfinance_rate_limit_error(exc2):
                print(
                    "DEBUG yfinance rate limited on TWO retry",
                    "| stock_id =",
                    stock_id,
                    "| yf_symbol =",
                    fallback_symbol,
                    "| requested_tf =",
                    requested_tf,
                    "| error =",
                    repr(exc2),
                    flush=True,
                )
            else:
                raise


    return None, normalize_time_frame(requested_tf)

def _snap_get(snapshot: dict, *keys, default=None):
    if not isinstance(snapshot, dict):
        return default

    for key in keys:
        if key in snapshot and snapshot.get(key) not in (None, "", "--"):
            return snapshot.get(key)

    raw = snapshot.get("raw")

    if isinstance(raw, dict):
        for key in keys:
            if key in raw and raw.get(key) not in (None, "", "--"):
                return raw.get(key)

    return default


def _snap_float(snapshot: dict, *keys, default: float = 0.0) -> float:
    value = _snap_get(snapshot, *keys, default=None)

    try:
        if value is None:
            return default

        text = str(value).replace(",", "").replace("%", "").strip()

        if not text:
            return default

        return float(text)

    except Exception:
        return default


def _snap_timestamp(snapshot: dict):
    """
    盡量從 Shioaji snapshot 取時間。
    若取不到，就用台北目前時間。
    """
    import pandas as pd

    value = _snap_get(
        snapshot,
        "ts",
        "datetime",
        "date_time",
        "time",
        "quote_time",
        "timestamp",
        default=None,
    )

    if value:
        try:
            ts = pd.to_datetime(value)

            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert("Asia/Taipei").tz_localize(None)

            return ts

        except Exception:
            pass

    try:
        return pd.Timestamp.now(tz="Asia/Taipei").tz_localize(None)
    except Exception:
        return pd.Timestamp.now()


def _append_realtime_snapshot_row(df, snapshot: dict):
    """
    把 Shioaji snapshot 補成 df 最後一列。
    讓 build_price_meta() 看到最新價與最新時間。
    """
    if df is None or getattr(df, "empty", True):
        return df

    close_price = _snap_float(
        snapshot,
        "close",
        "price",
        "last_price",
        "last",
        "Close",
        default=0.0,
    )

    if close_price <= 0:
        return df

    result = df.copy()
    attrs_backup = dict(getattr(df, "attrs", {}) or {})

    ts = _snap_timestamp(snapshot)

    try:
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.tz_convert("Asia/Taipei").tz_localize(None)
    except Exception:
        pass

    open_price = _snap_float(snapshot, "open", "Open", default=close_price) or close_price
    high_price = _snap_float(snapshot, "high", "High", default=close_price) or close_price
    low_price = _snap_float(snapshot, "low", "Low", default=close_price) or close_price

    # snapshot 的 volume / total_volume 多半是累積量，
    # 不適合直接塞到 1 分K當單根成交量，所以這裡用 0，
    # 避免圖下方成交量突然爆大。
    volume = 0

    if "Open" not in result.columns:
        result["Open"] = result["Close"] if "Close" in result.columns else close_price
    if "High" not in result.columns:
        result["High"] = result["Close"] if "Close" in result.columns else close_price
    if "Low" not in result.columns:
        result["Low"] = result["Close"] if "Close" in result.columns else close_price
    if "Close" not in result.columns:
        result["Close"] = close_price
    if "Volume" not in result.columns:
        result["Volume"] = 0

    result.loc[ts, ["Open", "High", "Low", "Close", "Volume"]] = [
        open_price,
        high_price,
        low_price,
        close_price,
        volume,
    ]

    result = result.sort_index()

    try:
        result.attrs.update(attrs_backup)
    except Exception:
        pass

    result.attrs["realtime_snapshot_source"] = "shioaji"
    result.attrs["realtime_snapshot_price"] = close_price
    result.attrs["realtime_snapshot_time"] = str(ts)

    return result


def _apply_shioaji_stock_realtime(df, stock_id: str):
    """
    個股即時修正：
    1. 若 Shioaji API 已熱機，直接抓 snapshot。
    2. 若環境變數 ALLOW_COLD_SHIOAJI_STOCK_APPEND=1，允許冷啟動登入。
    3. 抓到 snapshot 後，補成 df 最新一列。
    """
    import os
    import time

    t0 = time.perf_counter()

    sid = str(stock_id or "").strip()

    if not sid:
        return df

    allow_cold_login = (
        str(os.getenv("ALLOW_COLD_SHIOAJI_STOCK_APPEND", "1")).strip()
        == "1"
    )

    api_ready = False

    try:
        api_ready = bool(is_shioaji_api_ready())
    except Exception:
        api_ready = False

    if not api_ready and not allow_cold_login:
        print(
            "DEBUG stock realtime snapshot skip",
            "| stock_id =",
            sid,
            "| reason = cold_shioaji_api",
            "| set ALLOW_COLD_SHIOAJI_STOCK_APPEND=1 or use /warmup_all",
            "| sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )
        return df

    try:
        snapshot = get_shioaji_stock_snapshot(sid)

        if not isinstance(snapshot, dict):
            print(
                "DEBUG stock realtime snapshot empty",
                "| stock_id =",
                sid,
                "| snapshot_type =",
                type(snapshot),
                "| sec =",
                round(time.perf_counter() - t0, 3),
                flush=True,
            )
            return df

        price = _snap_float(
            snapshot,
            "close",
            "price",
            "last_price",
            "last",
            "Close",
            default=0.0,
        )

        if price <= 0:
            print(
                "DEBUG stock realtime snapshot no_price",
                "| stock_id =",
                sid,
                "| snapshot =",
                snapshot,
                "| sec =",
                round(time.perf_counter() - t0, 3),
                flush=True,
            )
            return df

        result = _append_realtime_snapshot_row(df, snapshot)

        print(
            "DEBUG stock realtime snapshot applied",
            "| stock_id =",
            sid,
            "| price =",
            price,
            "| time =",
            getattr(result, "attrs", {}).get("realtime_snapshot_time"),
            "| api_ready =",
            api_ready,
            "| allow_cold_login =",
            allow_cold_login,
            "| sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )

        return result

    except Exception as exc:
        print(
            "DEBUG stock realtime snapshot failed",
            "| stock_id =",
            sid,
            "| error =",
            repr(exc),
            "| sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )
        return df

def _price_color(change: float) -> str:
    if change > 0:
        return UP_COLOR
    if change < 0:
        return DOWN_COLOR
    return FLAT_COLOR


def _postback_button(
    label: str,
    data: str,
    active: bool = False,
    flex: int = 1,
) -> dict[str, Any]:
    return {
        "type": "box",
        "layout": "vertical",
        "flex": flex,
        "height": "52px",
        "cornerRadius": "10px",
        "backgroundColor": ACTIVE_COLOR if active else INACTIVE_COLOR,
        "justifyContent": "center",
        "alignItems": "center",
        "action": {
            "type": "postback",
            "label": label,
            "data": data,
        },
        "contents": [
            {
                "type": "text",
                "text": label,
                "align": "center",
                "gravity": "center",
                "size": "md",
                "color": "#FFFFFF" if active else "#111111",
                "weight": "bold" if active else "regular",
            }
        ],
    }


def _time_buttons(stock_id: str, active_mode: str, current_tf: str) -> dict[str, Any]:
    mode = _normalize_action(active_mode)
    tf = normalize_time_frame(current_tf)

    items = [
        ("1分", "1m"),
        ("5分", "5m"),
        ("日", "D"),
        ("週", "W"),
        ("月", "M"),
    ]

    buttons = []

    for label, value in items:
        is_active = tf == value

        # 重要：
        # 1分 / 5分 預設走即時圖
        # 日 / 週 / 月 預設走 K 線圖
        if value in {"1m", "5m"}:
            target_action = "instant"
        else:
            target_action = "k_line"

        buttons.append(
            {
                "type": "box",
                "layout": "vertical",
                "height": "46px",
                "cornerRadius": "12px",
                "backgroundColor": ACTIVE_COLOR if is_active else INACTIVE_COLOR,
                "justifyContent": "center",
                "alignItems": "center",
                "action": {
                    "type": "postback",
                    "label": label,
                    "data": f"{stock_id},{target_action},{target_action},{value}",
                    "displayText": f"{stock_id} {label}",
                },
                "contents": [
                    {
                        "type": "text",
                        "text": label,
                        "size": "md",
                        "weight": "bold" if is_active else "regular",
                        "align": "center",
                        "gravity": "center",
                        "color": "#FFFFFF" if is_active else "#111111",
                    }
                ],
            }
        )

    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "md",
        "contents": buttons,
    }

def _market_index_buttons(active_action: str = "market_index") -> list[dict[str, Any]]:
    active_action = str(active_action or "market_index").strip()

    row1 = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "md",
        "contents": [
            _postback_button(
                label="即時",
                data="TAIEX,market_index,market_index,D",
                active=active_action == "market_index",
            ),
            _postback_button(
                label="法人",
                data="TAIEX,market_chip,market_index,D",
                active=active_action == "market_chip",
            ),
        ],
    }

    row2 = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "sm",
        "contents": [
            _postback_button(
                label="融資券",
                data="TAIEX,market_margin,market_index,D",
                active=active_action == "market_margin",
            ),
            _postback_button(
                label="期貨",
                data="TAIEX,market_future_day,market_index,D",
                active=active_action in {"market_future_day", "market_future_all"},
            ),
        ],
    }

    return [row1, row2]

def _mode_buttons(stock_id: str, active_mode: str, current_tf: str) -> list[dict[str, Any]]:
    mode = _normalize_action(active_mode)
    tf = normalize_time_frame(current_tf)

    items = [
        ("即時", "instant"),
        ("K線", "k_line"),
        ("法人", "chip"),
        ("大戶", "large_holder"),
        ("融資券", "margin"),
        ("財務", "financial"),
        ("期貨", "futures"),

    ]

    buttons = []

    for label, action_name in items:
        is_active = mode == action_name

        # 重要：
        # 從 K線切回即時，如果目前是 D/W/M，要自動改成 1m
        # 從即時切回 K線，如果目前是 1m/5m，要自動改成 D
        if action_name == "instant":
            target_tf = tf if tf in {"1m", "5m"} else "1m"
        elif action_name == "k_line":
            target_tf = tf if tf in {"D", "W", "M"} else "D"
        else:
            target_tf = tf

        buttons.append(
            {
                "type": "box",
                "layout": "vertical",
                "height": "34px",
                "cornerRadius": "8px",
                "backgroundColor": ACTIVE_COLOR if is_active else INACTIVE_COLOR,
                "justifyContent": "center",
                "alignItems": "center",
                "action": {
                    "type": "postback",
                    "label": label,
                    "data": f"{stock_id},{action_name},{action_name},{target_tf}",
                    "displayText": f"{stock_id} {label}",
                },
                "contents": [
                    {
                        "type": "text",
                        "text": label,
                        "size": "xxs" if label == "融資券" else "xs",
                        "weight": "bold" if is_active else "regular",
                        "align": "center",
                        "gravity": "center",
                        "color": "#FFFFFF" if is_active else "#111111",
                    }
                ],
            }
        )

    return [
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "xs",
            "margin": "md",
            "contents": buttons,
        }
    ]

def _futures_session_buttons(
    stock_id: str,
    active_session: str,
    current_tf: str,
) -> dict[str, Any]:
    """
    期貨專用：日盤 / 全盤切換按鈕
    active_session:
    - day
    - all
    """
    active_session = str(active_session or "day").strip().lower()

    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "md",
        "contents": [
            _postback_button(
                label="日盤",
                data=f"{stock_id},futures_day,futures,{current_tf}",
                active=active_session == "day",
            ),
            _postback_button(
                label="全盤",
                data=f"{stock_id},futures_all,futures,{current_tf}",
                active=active_session == "all",
            ),
        ],
    }

def _build_market_index_realtime_flex(snapshot) -> dict[str, Any]:
    """
    加權指數即時卡片。
    內容：
    - 即時點位
    - 漲跌 / 漲跌幅
    - K 線圖 + 5MA / 20MA / 60MA / 120MA + 成交量
    - 開 / 高 / 低 / 收 / 漲 / 量
    """

    def _info_row(label: str, value: str, color: str = "#222222") -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "sm",
                    "color": "#888888",
                    "flex": 3,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": str(value),
                    "size": "sm",
                    "color": color,
                    "flex": 7,
                    "align": "end",
                    "wrap": True,
                },
            ],
        }

    if not getattr(snapshot, "available", False):
        contents = [
            {
                "type": "text",
                "text": "加權指數",
                "size": "xxl",
                "weight": "bold",
                "color": "#111111",
                "wrap": True,
            },
            {
                "type": "text",
                "text": "即時",
                "size": "lg",
                "weight": "bold",
                "color": "#444444",
                "margin": "sm",
            },
            {
                "type": "separator",
                "margin": "md",
            },
            {
                "type": "text",
                "text": getattr(snapshot, "message", "查無加權指數即時資料。"),
                "size": "sm",
                "color": "#666666",
                "wrap": True,
                "margin": "md",
            },
            {
                "type": "separator",
                "margin": "md",
            },
        ]

        contents.extend(_market_index_buttons("market_index"))

        return {
            "type": "flex",
            "altText": "加權指數即時",
            "contents": {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "contents": contents,
                },
            },
        }

    change = getattr(snapshot, "change", 0.0)
    change_pct = getattr(snapshot, "change_pct", 0.0)

    change_color = "#FF2D2D" if change > 0 else "#00B050" if change < 0 else "#666666"

    close_text = _fmt_market_price(getattr(snapshot, "close_price", 0.0))
    change_text = f"{_fmt_signed(change)} ({_fmt_signed_pct(change_pct)})"

    chart_url = str(getattr(snapshot, "chart_url", "") or "").strip()

    rows = [
        ("資料", getattr(snapshot, "quote_source", "永豐即時"), "#888888"),
        ("更新", str(getattr(snapshot, "quote_time", "") or "--")[:19], "#888888"),
        ("開", _fmt_market_price(getattr(snapshot, "open_price", 0.0)), "#222222"),
        ("高", _fmt_market_price(getattr(snapshot, "high_price", 0.0)), "#222222"),
        ("低", _fmt_market_price(getattr(snapshot, "low_price", 0.0)), "#222222"),
        ("收", close_text, change_color),
        ("漲", change_text, change_color),
        ("量", _fmt_market_int(getattr(snapshot, "total_volume", 0)), "#222222"),
    ]

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "加權指數",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": close_text,
            "size": "xxl",
            "weight": "bold",
            "color": change_color,
            "margin": "sm",
        },
        {
            "type": "text",
            "text": change_text,
            "size": "md",
            "weight": "bold",
            "color": change_color,
            "margin": "xs",
        },
    ]

    if chart_url:
        contents.append(
            {
                "type": "image",
                "url": chart_url,
                "size": "full",
                "aspectRatio": "4:3",
                "aspectMode": "fit",
                "margin": "md",
            }
        )

    contents.extend(
        [
            {
                "type": "separator",
                "margin": "md",
            },
            {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "margin": "md",
                "contents": [
                    _info_row(label, value, color)
                    for label, value, color in rows
                ],
            },
            {
                "type": "separator",
                "margin": "md",
            },
        ]
    )

    contents.extend(_market_index_buttons("market_index"))

    return {
        "type": "flex",
        "altText": "加權指數即時",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": contents,
            },
        },
    }

def _market_future_session_buttons(active_action: str = "market_future_day") -> list[dict[str, Any]]:
    active_action = str(active_action or "market_future_day").strip()

    row = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "md",
        "contents": [
            _postback_button(
                label="日盤",
                data="TAIEX,market_future_day,market_index,D",
                active=active_action == "market_future_day",
            ),
            _postback_button(
                label="全盤",
                data="TAIEX,market_future_all,market_index,D",
                active=active_action == "market_future_all",
            ),
        ],
    }

    return [row]


def _build_market_future_placeholder_flex(
    action: str = "market_future_day",
) -> dict[str, Any]:
    """
    台指期暫時卡片。
    保留這個函式是為了避免舊路由還在呼叫 placeholder 時爆掉。
    """
    session_text = "全盤" if action == "market_future_all" else "日盤"

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "台指期",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"TXF 近月｜{session_text}",
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "sm",
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": f"台指期 {session_text} 模組已建立，下一步接 Shioaji TXF 即時資料。",
            "size": "sm",
            "color": "#666666",
            "wrap": True,
            "margin": "md",
        },
        {
            "type": "text",
            "text": "之後這裡會顯示：期貨價、漲跌、漲跌幅、開、高、低、量、更新時間。",
            "size": "xs",
            "color": "#888888",
            "wrap": True,
            "margin": "md",
        },
        {
            "type": "separator",
            "margin": "md",
        },
    ]

    contents.extend(_market_index_buttons(action))
    contents.extend(_market_future_session_buttons(action))

    return {
        "type": "flex",
        "altText": "台指期",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": contents,
            },
        },
    }

def _parse_datetime_text(value):
    from datetime import datetime

    text = str(value or "").strip()

    if not text or text in {"--", "-"}:
        return None

    # 只取 YYYY-MM-DD HH:MM:SS
    text = text.replace("T", " ")[:19]

    for fmt in [
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
    ]:
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass

    return None


def _market_future_update_time_text(raw_time, action: str = "market_future_day") -> str:
    """
    台指期更新時間顯示修正。

    - 日盤如果已經超過 13:45，顯示當日 13:45:00。
    - 若 Shioaji snapshot 傳回舊日期，也會被修正成當日日盤收盤時間。
    - 全盤先不強制修正，避免夜盤時間被誤判。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    text = str(raw_time or "").strip()

    if not text:
        text = "--"

    action = str(action or "").strip()

    if action != "market_future_day":
        return text[:19] if text != "--" else "--"

    try:
        now = datetime.now(ZoneInfo("Asia/Taipei")).replace(tzinfo=None)
    except Exception:
        now = datetime.now()

    # 非週一到週五，不強制把日期改成今天，避免週末誤導。
    if now.weekday() > 4:
        return text[:19] if text != "--" else "--"

    close_dt = now.replace(hour=13, minute=45, second=0, microsecond=0)

    # 尚未收盤前，尊重實際 quote_time。
    if now < close_dt:
        return text[:19] if text != "--" else "--"

    parsed = _parse_datetime_text(text)

    # 收盤後：
    # 1. 如果 quote_time 沒有值
    # 2. 或 quote_time 不是今天
    # 3. 或 quote_time 早於 13:45
    # 都顯示當日 13:45:00。
    if parsed is None:
        return close_dt.strftime("%Y-%m-%d %H:%M:%S")

    if parsed.date() != now.date():
        return close_dt.strftime("%Y-%m-%d %H:%M:%S")

    if parsed < close_dt:
        return close_dt.strftime("%Y-%m-%d %H:%M:%S")

    return parsed.strftime("%Y-%m-%d %H:%M:%S")

def _build_market_future_realtime_flex(
    snapshot,
    action: str = "market_future_day",
    index_snapshot=None,
) -> dict[str, Any]:
    """
    台指期 TXF 即時卡片。

    版型目標：
    - 上方顯示台指期價格、漲跌、漲跌幅。
    - 期現價差獨立成摘要區。
    - 日盤 / 全盤按鈕保留。
    - 現貨、成交量、開高低、買賣、更新時間整理成資訊卡。
    - 與個股期貨卡片視覺一致。
    """

    def _to_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _to_int(value, default: int = 0) -> int:
        try:
            return int(float(value))
        except Exception:
            return default

    def _calc_color(value) -> str:
        num = _to_float(value)

        if num > 0:
            return "#FF2D2D"

        if num < 0:
            return "#00B050"

        return "#666666"

    def _info_row(label: str, value: str, color: str = "#222222") -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "sm",
                    "color": "#888888",
                    "flex": 3,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": str(value),
                    "size": "sm",
                    "color": color,
                    "weight": "bold" if color not in {"#888888", "#666666"} else "regular",
                    "flex": 7,
                    "align": "end",
                    "wrap": True,
                },
            ],
        }

    def _metric_box(
        title: str,
        value: str,
        sub_value: str = "",
        value_color: str = "#111111",
    ) -> dict[str, Any]:
        box_contents: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": title,
                "size": "xs",
                "color": "#888888",
                "wrap": True,
            },
            {
                "type": "text",
                "text": value,
                "size": "lg",
                "weight": "bold",
                "color": value_color,
                "margin": "xs",
                "wrap": True,
            },
        ]

        if sub_value:
            box_contents.append(
                {
                    "type": "text",
                    "text": sub_value,
                    "size": "xs",
                    "color": "#888888",
                    "margin": "xs",
                    "wrap": True,
                }
            )

        return {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F8F9FA",
            "cornerRadius": "12px",
            "paddingAll": "10px",
            "contents": box_contents,
        }

    action = str(action or "market_future_day").strip()
    session_text = "全盤" if action == "market_future_all" else "日盤"

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "台指期",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"TXF 近月｜{session_text}",
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "sm",
            "wrap": True,
        },
    ]

    if not getattr(snapshot, "available", False):
        contents.extend(
            [
                {
                    "type": "separator",
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": getattr(snapshot, "message", "查無台指期即時資料。"),
                    "size": "sm",
                    "color": "#666666",
                    "wrap": True,
                    "margin": "md",
                },
                {
                    "type": "separator",
                    "margin": "md",
                },
            ]
        )

        contents.extend(_market_index_buttons(action))
        contents.extend(_market_future_session_buttons(action))

        return {
            "type": "flex",
            "altText": "台指期",
            "contents": {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "14px",
                    "spacing": "sm",
                    "contents": contents,
                },
            },
        }

    future_price = _to_float(getattr(snapshot, "future_price", 0.0))
    change = _to_float(
        getattr(snapshot, "future_change", None)
        if getattr(snapshot, "future_change", None) is not None
        else getattr(snapshot, "change", 0.0)
    )
    change_pct = _to_float(
        getattr(snapshot, "future_change_pct", None)
        if getattr(snapshot, "future_change_pct", None) is not None
        else getattr(snapshot, "change_pct", 0.0)
    )

    change_color = _calc_color(change)

    price_text = _fmt_market_price(future_price)
    change_text = f"{_fmt_signed(change)} ({_fmt_signed_pct(change_pct)})"

    open_price = _to_float(getattr(snapshot, "open_price", 0.0))
    high_price = _to_float(getattr(snapshot, "high_price", 0.0))
    low_price = _to_float(getattr(snapshot, "low_price", 0.0))
    total_volume = _to_int(
        getattr(snapshot, "total_volume", None)
        if getattr(snapshot, "total_volume", None) is not None
        else getattr(snapshot, "volume", 0)
    )

    buy_price = _to_float(
        getattr(snapshot, "buy_price", None)
        if getattr(snapshot, "buy_price", None) is not None
        else getattr(snapshot, "buy", 0.0)
    )
    sell_price = _to_float(
        getattr(snapshot, "sell_price", None)
        if getattr(snapshot, "sell_price", None) is not None
        else getattr(snapshot, "sell", 0.0)
    )

    # 現貨：使用加權指數 snapshot。
    spot_price = 0.0

    if index_snapshot is not None and getattr(index_snapshot, "available", False):
        try:
            spot_price = float(getattr(index_snapshot, "close_price", 0.0) or 0.0)
        except Exception:
            spot_price = 0.0

    basis = future_price - spot_price if future_price > 0 and spot_price > 0 else 0.0
    basis_pct = basis / spot_price * 100 if spot_price > 0 else 0.0
    basis_color = _calc_color(basis)

    basis_text = (
        f"{_fmt_signed(basis)} ({_fmt_signed_pct(basis_pct)})"
        if spot_price > 0 and future_price > 0
        else "--"
    )

    contract_code = str(
        getattr(snapshot, "contract_code", "")
        or getattr(snapshot, "futures_id", "")
        or "TXFR1"
    )

    futures_name = str(
        getattr(snapshot, "futures_name", "")
        or "台指期近月"
    )

    quote_source = str(getattr(snapshot, "quote_source", "") or "永豐即時")
    quote_time = _market_future_update_time_text(
        getattr(snapshot, "quote_time", ""),
        action,
    )

    trading_session = str(
        getattr(snapshot, "trading_session", "")
        or session_text
    )

    # 上方價格摘要
    contents.extend(
        [
            {
                "type": "text",
                "text": price_text,
                "size": "xxl",
                "weight": "bold",
                "color": change_color,
                "margin": "md",
                "wrap": True,
            },
            {
                "type": "text",
                "text": change_text,
                "size": "md",
                "weight": "bold",
                "color": change_color,
                "margin": "xs",
                "wrap": True,
            },
            {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#F8F9FA",
                "cornerRadius": "12px",
                "paddingAll": "10px",
                "margin": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "期現價差",
                        "size": "xs",
                        "color": "#888888",
                    },
                    {
                        "type": "text",
                        "text": basis_text,
                        "size": "lg",
                        "weight": "bold",
                        "color": basis_color,
                        "margin": "xs",
                    },
                ],
            },
            {
                "type": "separator",
                "margin": "md",
            },
        ]
    )

    # 大盤功能按鈕 + 台指期日盤 / 全盤切換
    contents.extend(_market_index_buttons(action))
    contents.extend(_market_future_session_buttons(action))

    # 主要數值卡片
    contents.extend(
        [
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "md",
                "contents": [
                    _metric_box("現貨", _fmt_market_price(spot_price)),
                    _metric_box("成交量", _fmt_market_int(total_volume)),
                ],
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "sm",
                "contents": [
                    _metric_box("開盤", _fmt_market_price(open_price)),
                    _metric_box("最高", _fmt_market_price(high_price)),
                ],
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "sm",
                "contents": [
                    _metric_box("最低", _fmt_market_price(low_price)),
                    _metric_box(
                        "買賣",
                        (
                            f"{_fmt_market_price(buy_price)} / {_fmt_market_price(sell_price)}"
                            if buy_price > 0 or sell_price > 0
                            else "--"
                        ),
                    ),
                ],
            },
            {
                "type": "separator",
                "margin": "md",
            },
            {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "margin": "md",
                "contents": [
                    _info_row("商品", f"{futures_name} ({contract_code})", "#222222"),
                    _info_row("時段", trading_session, "#222222"),
                    _info_row("資料", quote_source, "#888888"),
                    _info_row("更新", quote_time[:19] if quote_time else "--", "#888888"),
                ],
            },
            {
                "type": "text",
                "text": "期現價差＝台指期近月 − 加權指數現貨。日盤收盤後，日盤更新時間會以 13:45 顯示。",
                "size": "xs",
                "color": "#888888",
                "wrap": True,
                "margin": "md",
            },
        ]
    )

    return {
        "type": "flex",
        "altText": "台指期",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "14px",
                "spacing": "sm",
                "contents": contents,
            },
        },
    }

def _build_market_index_placeholder_flex(
    action: str = "market_index",
) -> dict[str, Any]:
    title_map = {
        "market_index": "加權指數即時",
        "market_k": "加權指數K線",
        "market_chip": "加權指數法人",
        "market_margin": "加權指數融資券",
    }

    message_map = {
        "market_index": "加權指數即時模組已建立，下一步接 Shioaji 即時指數資料。",
        "market_k": "加權指數K線模組已建立，下一步加入 5MA / 20MA / 60MA / 120MA 與成交量。",
        "market_chip": "加權指數法人模組已建立，下一步接整體市場三大法人資料。",
        "market_margin": "加權指數融資券模組已建立，下一步接整體市場融資融券資料。",
    }

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "加權指數",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": title_map.get(action, "加權指數"),
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "sm",
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": message_map.get(action, "加權指數模組已建立。"),
            "size": "sm",
            "color": "#666666",
            "wrap": True,
            "margin": "md",
        },
        {
            "type": "text",
            "text": "關鍵字：大盤 / 指數 / 加權 / 加權指數 / TAIEX",
            "size": "xs",
            "color": "#888888",
            "wrap": True,
            "margin": "md",
        },
        {
            "type": "separator",
            "margin": "md",
        },
    ]

    contents.extend(_market_index_buttons(action))

    return {
        "type": "flex",
        "altText": "加權指數",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": contents,
            },
        },
    }

def _build_chart_flex(
    stock_id: str,
    stock_name: str,
    image_url: str,
    price_info: str,
    change_info: str,
    update_time: str,
    price_change: float,
    active_mode: str,
    current_tf: str,
    image_aspect_ratio: str = "4:3",
) -> dict[str, Any]:
    color = _price_color(price_change)
    active_mode_norm = _normalize_action(active_mode)
    tf_norm = normalize_time_frame(current_tf)

    mode_title_map = {
        "instant": "即時走勢",
        "k_line": "K線圖",
        "chip": "法人買賣超",
        "large_holder": "大戶持股",
        "margin": "融資券",
        "futures": "個股期貨",
    }

    mode_title = mode_title_map.get(active_mode_norm, "個股觀測")

    # 讓更新時間不要太長。
    # 例如 2026-07-06 14:30:00 -> 14:30:00
    update_text = str(update_time or "--").strip()

    if len(update_text) >= 19 and update_text[4:5] in {"-", "/"}:
        update_short = update_text[11:19]
    else:
        update_short = update_text

    price_text = str(price_info or "--").strip()
    change_text = str(change_info or "--").strip()

    body_contents: list[dict[str, Any]] = [
        {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "text",
                    "text": f"{stock_id} {stock_name}",
                    "size": "xxl",
                    "weight": "bold",
                    "wrap": True,
                    "color": "#111111",
                    "flex": 7,
                },
                {
                    "type": "text",
                    "text": tf_norm,
                    "size": "sm",
                    "weight": "bold",
                    "color": "#666666",
                    "align": "end",
                    "gravity": "center",
                    "flex": 2,
                },
            ],
        },
        {
            "type": "text",
            "text": mode_title,
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "xs",
            "wrap": True,
        },
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "margin": "md",
            "contents": [
                {
                    "type": "text",
                    "text": price_text,
                    "size": "xxl",
                    "weight": "bold",
                    "color": color,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": change_text,
                    "size": "md",
                    "weight": "bold",
                    "color": color,
                    "wrap": True,
                },
            ],
        },
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "margin": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": f"更新 {update_short}",
                    "size": "xs",
                    "color": "#888888",
                    "flex": 5,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": "資料僅供參考",
                    "size": "xs",
                    "color": "#AAAAAA",
                    "align": "end",
                    "flex": 4,
                    "wrap": True,
                },
            ],
        },
        {
            "type": "separator",
            "margin": "md",
        },
        _time_buttons(stock_id, active_mode_norm, tf_norm),
    ]

    if image_url:
        body_contents.append(
            {
                "type": "image",
                "url": image_url,
                "size": "full",
                "aspectRatio": image_aspect_ratio,
                "aspectMode": "fit",
                "margin": "md",
                "backgroundColor": "#FFFFFF",
            }
        )
    else:
        body_contents.append(
            {
                "type": "text",
                "text": "圖表產生中或暫無圖表。",
                "size": "sm",
                "color": "#888888",
                "margin": "md",
                "wrap": True,
            }
        )

    body_contents.extend(_mode_buttons(stock_id, active_mode_norm, tf_norm))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} {mode_title}",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "16px",
                "spacing": "sm",
                "contents": body_contents,
            },
        },
    }

def _lh_get(row, *keys, default=None):
    if row is None:
        return default

    if isinstance(row, dict):
        for key in keys:
            if key in row and row.get(key) is not None:
                return row.get(key)
        return default

    for key in keys:
        try:
            value = getattr(row, key)

            if value is not None:
                return value

        except Exception:
            continue

    return default


def _lh_float(value, default=0.0):
    try:
        if value is None:
            return default

        text = str(value).replace(",", "").replace("%", "").strip()

        if not text or text in {"--", "-"}:
            return default

        return float(text)

    except Exception:
        return default


def _lh_date_text(value):
    text = str(value or "").strip()

    if not text:
        return "--"

    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[5:10].replace("-", "/")

    if len(text) >= 10 and text[4] == "/" and text[7] == "/":
        return text[5:10]

    if len(text) >= 5 and "/" in text:
        return text[-5:]

    return text


def _lh_pct_text(value):
    number = _lh_float(value, default=0.0)

    if number == 0:
        return "--"

    if 0 < abs(number) <= 1:
        number = number * 100

    return f"{number:.2f}%"


def _lh_change_text(value):
    """
    大戶持股週增減。

    value 已經是「百分點」，例如：
    -0.84%
    +0.28%

    不可以再乘以 100。
    """
    if value is None:
        return "--"

    text = str(value).replace(",", "").replace("%", "").strip()

    if not text or text in {"--", "-"}:
        return "--"

    try:
        number = float(text)
    except Exception:
        return str(value)

    if abs(number) < 0.005:
        return "--"

    return f"{number:+.2f}%"
    
def _lh_change_color(value):
    """
    依週增減正負決定顏色。

    value 已經是百分點，不要乘以 100。
    """
    if value is None:
        return "#666666"

    try:
        number = float(
            str(value)
            .replace(",", "")
            .replace("%", "")
            .strip()
        )
    except Exception:
        return "#666666"

    if number > 0:
        return "#E53935"

    if number < 0:
        return "#1E9F5A"

    return "#666666"
    
def _lh_ratio_from_row(row):
    return _lh_get(
        row,
        "ratio",
        "percentage",
        "percent",
        "pct",
        "holding_ratio",
        "holder_ratio",
        "large_holder_ratio",
        "large_holder_pct",
        "over_1000_ratio",
        "over_1000_pct",
        "value",
        default=None,
    )

def _lh_people_from_row(row):
    return _lh_get(
        row,
        "people",
        "large_holder_people",
        "holder_people",
        "holders",
        "people_count",
        "千張大戶人數",
        "人數",
        default=None,
    )


def _lh_people_text(value):
    """
    千張大戶人數顯示。

    若資料為 None / 空值 / 0，顯示 --。
    避免像國巨這種資料沒寫入時，畫面出現 0。
    """
    if value is None:
        return "--"

    text = str(value).replace(",", "").strip()

    if not text or text in {"--", "-"}:
        return "--"

    try:
        number = int(float(text))

        if number <= 0:
            return "--"

        return f"{number:,}"

    except Exception:
        return str(value)

def _lh_change_from_row(row):
    return _lh_get(
        row,
        "change",
        "diff",
        "difference",
        "ratio_change",
        "pct_change",
        "week_change",
        "wow",
        "delta",
        default=None,
    )


def _lh_sort_key(row):
    return str(
        _lh_get(
            row,
            "date",
            "week",
            "data_date",
            "trade_date",
            "record_date",
            default="",
        )
        or ""
    )

def _large_holder_week_row(
    date_text: str,
    people_text: str,
    ratio_text: str,
    change_text: str,
    change_color: str,
    is_header: bool = False,
) -> dict[str, Any]:
    text_color = "#666666" if is_header else "#111111"
    bg_color = "#F1F3F5" if is_header else "#FFFFFF"
    weight = "bold" if is_header else "regular"

    return {
        "type": "box",
        "layout": "horizontal",
        "backgroundColor": bg_color,
        "cornerRadius": "6px" if is_header else "0px",
        "paddingAll": "5px" if is_header else "3px",
        "contents": [
            {
                "type": "text",
                "text": str(date_text or "--"),
                "size": "xs",
                "color": text_color,
                "weight": weight,
                "flex": 2,
                "align": "start",
            },
            {
                "type": "text",
                "text": str(people_text or "--"),
                "size": "xs",
                "color": text_color,
                "weight": weight,
                "align": "end",
                "flex": 3,
            },
            {
                "type": "text",
                "text": str(ratio_text or "--"),
                "size": "xs",
                "color": "#111111" if not is_header else text_color,
                "weight": "bold" if not is_header else weight,
                "align": "end",
                "flex": 3,
            },
            {
                "type": "text",
                "text": str(change_text or "--"),
                "size": "xs",
                "color": change_color if not is_header else text_color,
                "weight": "bold" if not is_header else weight,
                "align": "end",
                "flex": 3,
            },
        ],
    }

def _lh_with_unit_people(text: str) -> str:
    text = str(text or "--").strip()

    if text in {"", "--", "-"}:
        return "--"

    return f"{text} 人"


def _lh_metric_box(
    title: str,
    value: str,
    sub_value: str = "",
    value_color: str = "#111111",
) -> dict[str, Any]:
    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": title,
            "size": "xs",
            "color": "#888888",
            "wrap": True,
        },
        {
            "type": "text",
            "text": value,
            "size": "lg",
            "weight": "bold",
            "color": value_color,
            "margin": "xs",
            "wrap": True,
        },
    ]

    if sub_value:
        contents.append(
            {
                "type": "text",
                "text": sub_value,
                "size": "xs",
                "color": "#888888",
                "margin": "xs",
                "wrap": True,
            }
        )

    return {
        "type": "box",
        "layout": "vertical",
        "backgroundColor": "#F8F9FA",
        "cornerRadius": "12px",
        "paddingAll": "10px",
        "contents": contents,
    }


def _lh_row_to_computed(row, sorted_rows: list) -> dict[str, Any]:
    date_raw = _lh_get(
        row,
        "date",
        "week",
        "data_date",
        "trade_date",
        "record_date",
        default="",
    )

    ratio_value = _lh_ratio_from_row(row)
    change_value = _lh_change_from_row(row)
    people_value = _lh_people_from_row(row)

    if change_value is None:
        try:
            idx = sorted_rows.index(row)

            if idx > 0:
                this_ratio = _lh_float(_lh_ratio_from_row(row), default=0.0)
                prev_ratio = _lh_float(_lh_ratio_from_row(sorted_rows[idx - 1]), default=0.0)

                if this_ratio and prev_ratio:
                    change_value = this_ratio - prev_ratio

        except Exception:
            pass

    return {
        "date": _lh_date_text(date_raw),
        "people": _lh_people_text(people_value),
        "ratio": _lh_pct_text(ratio_value),
        "change": _lh_change_text(change_value),
        "change_color": _lh_change_color(change_value),
    }

def _build_large_holder_flex(stock_id: str, stock_name: str, rows, current_tf: str = "D"):
    """
    顯示個股大戶持股近 5 週。
    欄位：
    日期 | 千張大戶人數 | 持股比 | 增減
    """
    raw_rows = list(rows or [])

    print(
        "DEBUG large_holder flex",
        "| stock_id =",
        stock_id,
        "| rows_count =",
        len(raw_rows),
        "| sample =",
        raw_rows[:2],
        flush=True,
    )

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{stock_id} {stock_name}",
            "weight": "bold",
            "size": "xxl",
            "color": "#111111",
            "wrap": True,
        }
    ]

    if not raw_rows:
        contents.extend(
            [
                {
                    "type": "text",
                    "text": "大戶持股",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#444444",
                    "margin": "sm",
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": "目前查無大戶持股資料。",
                    "size": "sm",
                    "color": "#777777",
                    "margin": "md",
                    "wrap": True,
                },
            ]
        )
    else:
        sorted_rows = sorted(raw_rows, key=_lh_sort_key)
        latest_rows = list(reversed(sorted_rows[-5:]))

        computed_rows = [
            _lh_row_to_computed(row, sorted_rows)
            for row in latest_rows
        ]

        latest = computed_rows[0] if computed_rows else {
            "date": "--",
            "people": "--",
            "ratio": "--",
            "change": "--",
            "change_color": "#666666",
        }

        contents.extend(
            [
                {
                    "type": "text",
                    "text": f"大戶持股｜最新 {latest.get('date', '--')}",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#444444",
                    "margin": "sm",
                    "wrap": True,
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "margin": "md",
                    "contents": [
                        _lh_metric_box(
                            "千張大戶人數",
                            _lh_with_unit_people(latest.get("people", "--")),
                        ),
                        _lh_metric_box(
                            "持股比",
                            latest.get("ratio", "--"),
                        ),
                    ],
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#F8F9FA",
                    "cornerRadius": "12px",
                    "paddingAll": "10px",
                    "margin": "sm",
                    "contents": [
                        {
                            "type": "text",
                            "text": "週變化",
                            "size": "xs",
                            "color": "#888888",
                        },
                        {
                            "type": "text",
                            "text": latest.get("change", "--"),
                            "size": "lg",
                            "weight": "bold",
                            "color": latest.get("change_color", "#666666"),
                            "margin": "xs",
                        },
                    ],
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": "近5週",
                    "size": "md",
                    "weight": "bold",
                    "color": "#444444",
                    "margin": "md",
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "sm",
                    "spacing": "xs",
                    "contents": [
                        _large_holder_week_row(
                            "日期",
                            "人數",
                            "持股比",
                            "增減",
                            "#666666",
                            is_header=True,
                        ),
                        *[
                            _large_holder_week_row(
                                row["date"],
                                row["people"],
                                row["ratio"],
                                row["change"],
                                row["change_color"],
                            )
                            for row in computed_rows
                        ],
                    ],
                },
            ]
        )

    contents.extend(_mode_buttons(stock_id, "large_holder", current_tf))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 大戶持股",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "14px",
                "spacing": "sm",
                "contents": contents,
            },
        },
    }

def _margin_table_row(
    date_text: str,
    margin_text: str,
    short_text: str,
    ratio_text: str,
    is_header: bool = False,
) -> dict[str, Any]:
    text_color = "#666666" if is_header else "#222222"
    weight = "bold" if is_header else "regular"
    bg_color = "#F7F8FA" if is_header else "#FFFFFF"

    return {
        "type": "box",
        "layout": "horizontal",
        "paddingAll": "6px",
        "backgroundColor": bg_color,
        "cornerRadius": "6px" if is_header else "0px",
        "contents": [
            {
                "type": "text",
                "text": date_text,
                "size": "sm",
                "color": text_color,
                "weight": weight,
                "flex": 2,
                "align": "start",
            },
            {
                "type": "text",
                "text": margin_text,
                "size": "sm",
                "color": text_color,
                "weight": weight,
                "flex": 3,
                "align": "end",
            },
            {
                "type": "text",
                "text": short_text,
                "size": "sm",
                "color": text_color,
                "weight": weight,
                "flex": 2,
                "align": "end",
            },
            {
                "type": "text",
                "text": ratio_text,
                "size": "sm",
                "color": text_color,
                "weight": weight,
                "flex": 2,
                "align": "end",
            },
        ],
    }

def _fmt_stock_chip_int(value, signed: bool = True) -> str:
    try:
        num = int(round(float(value or 0)))

        if signed:
            sign = "+" if num > 0 else ""
            return f"{sign}{num:,}"

        return f"{num:,}"

    except Exception:
        return "--"


def _stock_chip_color(value) -> str:
    try:
        num = float(value or 0)

        if num > 0:
            return "#FF2D2D"

        if num < 0:
            return "#00B050"

    except Exception:
        pass

    return "#666666"

def _stock_chip_dates(chip_rows: dict) -> list[str]:
    """
    依 get_institutional_chips() 回傳順序整理日期。
    chip_service.py 目前每一類法人已經是舊到新排序的近 10 筆。
    """
    dates: list[str] = []

    if not isinstance(chip_rows, dict):
        return dates

    for section in ["foreign", "trust", "dealer"]:
        for item in list(chip_rows.get(section) or []):
            date = str(item.get("date", "") or "").strip()

            if date and date not in dates:
                dates.append(date)

    return dates[-10:]

def _stock_chip_value(chip_rows: dict, section: str, date: str) -> float:
    if not isinstance(chip_rows, dict):
        return 0.0

    for item in list(chip_rows.get(section) or []):
        if str(item.get("date", "") or "").strip() == str(date or "").strip():
            try:
                return float(item.get("buy_sell") or 0)
            except Exception:
                return 0.0

    return 0.0

def _financial_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        text = str(value).replace(",", "").replace("%", "").strip()

        if text in {"", "--", "-"}:
            return default

        return float(text)

    except Exception:
        return default


def _financial_fmt(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except Exception:
        return "--"


def _financial_signed(value, digits: int = 2) -> str:
    try:
        num = float(value)
        sign = "+" if num > 0 else ""
        return f"{sign}{num:,.{digits}f}"
    except Exception:
        return "--"


def _financial_color(value) -> str:
    num = _financial_float(value)

    if num > 0:
        return "#FF2D2D"

    if num < 0:
        return "#00B050"

    return "#666666"


def _financial_metric_box(
    title: str,
    value: str,
    sub_value: str = "",
    value_color: str = "#111111",
) -> dict[str, Any]:
    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": title,
            "size": "xs",
            "color": "#888888",
            "wrap": True,
        },
        {
            "type": "text",
            "text": value,
            "size": "lg",
            "weight": "bold",
            "color": value_color,
            "margin": "xs",
            "wrap": True,
        },
    ]

    if sub_value:
        contents.append(
            {
                "type": "text",
                "text": sub_value,
                "size": "xs",
                "color": "#888888",
                "margin": "xs",
                "wrap": True,
            }
        )

    return {
        "type": "box",
        "layout": "vertical",
        "backgroundColor": "#F8F9FA",
        "cornerRadius": "12px",
        "paddingAll": "10px",
        "contents": contents,
    }


def _financial_table_row(
    quarter: str,
    eps: str,
    eps_change: str,
    ttm_eps: str,
    pe: str,
    change_color: str = "#666666",
    is_header: bool = False,
) -> dict[str, Any]:
    text_color = "#666666" if is_header else "#222222"
    weight = "bold" if is_header else "regular"
    bg_color = "#F1F3F5" if is_header else "#FFFFFF"

    def cell(text, flex, color=None, align="end"):
        return {
            "type": "text",
            "text": str(text),
            "size": "xs",
            "color": color or text_color,
            "weight": weight,
            "flex": flex,
            "align": align,
            "wrap": True,
        }

    return {
        "type": "box",
        "layout": "horizontal",
        "backgroundColor": bg_color,
        "cornerRadius": "6px" if is_header else "0px",
        "paddingAll": "5px" if is_header else "3px",
        "contents": [
            cell(quarter, 2, align="start"),
            cell(eps, 2),
            cell(eps_change, 2, change_color if not is_header else text_color),
            cell(ttm_eps, 2),
            cell(pe, 2),
        ],
    }


def _build_financial_flex(
    stock_id: str,
    stock_name: str,
    snapshot,
    current_tf: str = "D",
) -> dict[str, Any]:
    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{stock_id} {stock_name}",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": "財務｜近四季 EPS",
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "sm",
            "wrap": True,
        },
    ]

    if not getattr(snapshot, "available", False):
        contents.extend(
            [
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": getattr(snapshot, "message", "目前查無 EPS 財務資料。"),
                    "size": "sm",
                    "color": "#666666",
                    "margin": "md",
                    "wrap": True,
                },
            ]
        )

        contents.extend(_mode_buttons(stock_id, "financial", current_tf))

        return {
            "type": "flex",
            "altText": f"{stock_id} {stock_name} 財務",
            "contents": {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "14px",
                    "spacing": "sm",
                    "contents": contents,
                },
            },
        }

    latest_ttm_eps = _financial_float(getattr(snapshot, "latest_ttm_eps", 0.0))
    latest_eps = _financial_float(getattr(snapshot, "latest_eps", 0.0))
    eps_change = _financial_float(getattr(snapshot, "latest_eps_change", 0.0))
    current_price = _financial_float(getattr(snapshot, "current_price", 0.0))
    current_pe = _financial_float(getattr(snapshot, "current_pe", 0.0))

    contents.extend(
        [
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "md",
                "contents": [
                    _financial_metric_box(
                        "近四季 EPS",
                        _financial_fmt(latest_ttm_eps),
                        getattr(snapshot, "latest_quarter", ""),
                    ),
                    _financial_metric_box(
                        "目前本益比",
                        f"{_financial_fmt(current_pe)} 倍" if current_pe > 0 else "--",
                        f"股價 {_financial_fmt(current_price)}",
                    ),
                ],
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "sm",
                "contents": [
                    _financial_metric_box(
                        "最新單季 EPS",
                        _financial_fmt(latest_eps),
                    ),
                    _financial_metric_box(
                        "EPS QoQ",
                        _financial_signed(eps_change),
                        value_color=_financial_color(eps_change),
                    ),
                ],
            },
            {"type": "separator", "margin": "md"},
            {
                "type": "text",
                "text": "近8季",
                "size": "md",
                "weight": "bold",
                "color": "#444444",
                "margin": "md",
            },
        ]
    )

    table_rows = [
        _financial_table_row(
            "季度",
            "EPS",
            "增減",
            "TTM",
            "PE",
            is_header=True,
        )
    ]

    for row in list(getattr(snapshot, "rows", []) or [])[:8]:
        ttm_eps = _financial_float(row.get("ttm_eps"))
        pe = (
            current_price / ttm_eps
            if current_price > 0 and ttm_eps > 0
            else 0.0
        )

        eps_chg = _financial_float(row.get("eps_change"))

        table_rows.append(
            _financial_table_row(
                str(row.get("quarter_label") or "--"),
                _financial_fmt(row.get("eps")),
                _financial_signed(eps_chg) if row.get("eps_change") is not None else "--",
                _financial_fmt(ttm_eps) if ttm_eps > 0 else "--",
                _financial_fmt(pe) if pe > 0 else "--",
                change_color=_financial_color(eps_chg),
            )
        )

    contents.extend(
        [
            {
                "type": "box",
                "layout": "vertical",
                "margin": "sm",
                "spacing": "xs",
                "contents": table_rows,
            },
            {
                "type": "text",
                "text": "本益比＝股價 ÷ 近四季 EPS。EPS 來源為財報資料，實際公布時間可能落後交易日。",
                "size": "xs",
                "color": "#888888",
                "wrap": True,
                "margin": "md",
            },
            {"type": "separator", "margin": "md"},
        ]
    )

    contents.extend(_mode_buttons(stock_id, "financial", current_tf))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 財務",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "14px",
                "spacing": "sm",
                "contents": contents,
            },
        },
    }

def _build_stock_chip_flex(
    stock_id: str,
    stock_name: str,
    chip_rows: dict,
    current_tf: str = "D",
) -> dict[str, Any]:
    """
    個股法人 Flex 表格卡。
    資料來源：get_institutional_chips()

    顯示：
    - 最新一日：外資、投信、自營商、三大法人合計
    - 近10日：日期 | 外資 | 投信 | 自營商
    """
    dates_asc = _stock_chip_dates(chip_rows)
    dates_desc = list(reversed(dates_asc))

    latest_date = dates_desc[0] if dates_desc else "--"

    latest_foreign = _stock_chip_value(chip_rows, "foreign", latest_date)
    latest_trust = _stock_chip_value(chip_rows, "trust", latest_date)
    latest_dealer = _stock_chip_value(chip_rows, "dealer", latest_date)
    latest_total = latest_foreign + latest_trust + latest_dealer

    def _summary_row(label: str, value) -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "sm",
                    "color": "#666666",
                    "flex": 4,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": f"{_fmt_stock_chip_int(value)} 張",
                    "size": "sm",
                    "color": _stock_chip_color(value),
                    "weight": "bold",
                    "flex": 6,
                    "align": "end",
                    "wrap": True,
                },
            ],
        }

    def _cell(
        text: str,
        flex: int,
        color: str = "#333333",
        weight: str = "regular",
        align: str = "end",
    ) -> dict[str, Any]:
        return {
            "type": "text",
            "text": str(text),
            "size": "xs",
            "color": color,
            "weight": weight,
            "flex": flex,
            "align": align,
            "wrap": True,
        }

    def _table_header() -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "6px",
            "backgroundColor": "#EEF1F4",
            "cornerRadius": "sm",
            "contents": [
                _cell("日期", 2, "#555555", "bold", "start"),
                _cell("外資", 3, "#555555", "bold", "end"),
                _cell("投信", 3, "#555555", "bold", "end"),
                _cell("自營商", 3, "#555555", "bold", "end"),
            ],
        }

    def _table_row(date_text: str) -> dict[str, Any]:
        foreign = _stock_chip_value(chip_rows, "foreign", date_text)
        trust = _stock_chip_value(chip_rows, "trust", date_text)
        dealer = _stock_chip_value(chip_rows, "dealer", date_text)

        return {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "6px",
            "contents": [
                _cell(str(date_text or "--"), 2, "#333333", "regular", "start"),
                _cell(_fmt_stock_chip_int(foreign), 3, _stock_chip_color(foreign)),
                _cell(_fmt_stock_chip_int(trust), 3, _stock_chip_color(trust)),
                _cell(_fmt_stock_chip_int(dealer), 3, _stock_chip_color(dealer)),
            ],
        }

    table_contents: list[dict[str, Any]] = [_table_header()]

    if dates_desc:
        for date_text in dates_desc[:10]:
            table_contents.append(_table_row(date_text))
    else:
        table_contents.append(
            {
                "type": "text",
                "text": "暫無近10日資料",
                "size": "sm",
                "color": "#999999",
                "margin": "sm",
            }
        )

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{stock_id} {stock_name}",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": "法人買賣超",
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "sm",
        },
        {
            "type": "text",
            "text": f"最新日期：{latest_date}",
            "size": "sm",
            "color": "#666666",
            "margin": "xs",
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": "最新一日",
            "size": "md",
            "weight": "bold",
            "color": "#222222",
            "margin": "md",
        },
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "margin": "sm",
            "contents": [
                _summary_row("外資", latest_foreign),
                _summary_row("投信", latest_trust),
                _summary_row("自營商", latest_dealer),
                {
                    "type": "separator",
                    "margin": "sm",
                },
                _summary_row("三大法人合計", latest_total),
            ],
        },
        {
            "type": "text",
            "text": "近10日買賣超",
            "size": "md",
            "weight": "bold",
            "color": "#222222",
            "margin": "md",
        },
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "margin": "sm",
            "paddingAll": "6px",
            "backgroundColor": "#F8F9FA",
            "cornerRadius": "md",
            "contents": table_contents,
        },
        {
            "type": "text",
            "text": "單位：張；盤後資料，非即時逐筆。",
            "size": "xs",
            "color": "#888888",
            "wrap": True,
            "margin": "md",
        },
    ]

    contents.extend(_mode_buttons(stock_id, "chip", current_tf))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 法人買賣超",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "14px",
                "spacing": "sm",
                "contents": contents,
            },
        },
    }

def _margin_get(row: dict, *keys, default=None):
    for key in keys:
        if key in row and row.get(key) not in [None, ""]:
            return row.get(key)
    return default


def _margin_to_float(value, default: float = 0.0) -> float:
    try:
        text = str(value).replace(",", "").replace("%", "").strip()
        if text in {"", "--", "-"}:
            return default
        return float(text)
    except Exception:
        return default


def _margin_fmt_lots(value) -> str:
    try:
        return f"{int(round(float(value))):,} 張"
    except Exception:
        return "--"


def _margin_fmt_signed_lots(value) -> str:
    if value is None:
        return "--"

    try:
        number = float(value)
    except Exception:
        return "--"

    if abs(number) < 0.5:
        return "0 張"

    return f"{number:+,.0f} 張"


def _margin_fmt_ratio(value) -> str:
    if value in [None, ""]:
        return "--"

    text = str(value).strip()

    if text in {"--", "-"}:
        return "--"

    if "%" in text:
        return text

    try:
        return f"{float(text):.2f}%"
    except Exception:
        return text


def _margin_change_color(value) -> str:
    try:
        number = float(value)
    except Exception:
        return "#666666"

    if number > 0:
        return "#E53935"

    if number < 0:
        return "#1E9F5A"

    return "#666666"


def _margin_short_date(value) -> str:
    text = str(value or "--").strip()

    if len(text) >= 10 and text[4:5] in {"-", "/"}:
        return text[5:10].replace("-", "/")

    if len(text) == 8 and text.isdigit():
        return f"{text[4:6]}/{text[6:8]}"

    return text


def _margin_sort_key(row: dict):
    from datetime import datetime

    text = str(
        _margin_get(
            row,
            "date",
            "trade_date",
            "日期",
            default="",
        )
    ).strip()

    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%m/%d"]:
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass

    return datetime.min


def _margin_metric_box(
    title: str,
    value: str,
    sub_title: str = "",
    sub_value: str = "",
    sub_color: str = "#666666",
) -> dict[str, Any]:
    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": title,
            "size": "xs",
            "color": "#888888",
            "wrap": True,
        },
        {
            "type": "text",
            "text": value,
            "size": "lg",
            "weight": "bold",
            "color": "#111111",
            "margin": "xs",
            "wrap": True,
        },
    ]

    if sub_title or sub_value:
        contents.append(
            {
                "type": "text",
                "text": f"{sub_title} {sub_value}".strip(),
                "size": "xs",
                "weight": "bold",
                "color": sub_color,
                "margin": "xs",
                "wrap": True,
            }
        )

    return {
        "type": "box",
        "layout": "vertical",
        "backgroundColor": "#F8F9FA",
        "cornerRadius": "12px",
        "paddingAll": "10px",
        "contents": contents,
    }


def _margin_table_row_v2(
    date_text: str,
    margin_text: str,
    short_text: str,
    ratio_text: str,
    is_header: bool = False,
) -> dict[str, Any]:
    text_color = "#666666" if is_header else "#111111"
    bg_color = "#F1F3F5" if is_header else "#FFFFFF"
    weight = "bold" if is_header else "regular"

    return {
        "type": "box",
        "layout": "horizontal",
        "backgroundColor": bg_color,
        "cornerRadius": "6px" if is_header else "0px",
        "paddingAll": "5px" if is_header else "3px",
        "contents": [
            {
                "type": "text",
                "text": date_text,
                "size": "xs",
                "weight": weight,
                "color": text_color,
                "flex": 2,
                "align": "start",
            },
            {
                "type": "text",
                "text": margin_text,
                "size": "xs",
                "weight": weight,
                "color": text_color,
                "flex": 3,
                "align": "end",
            },
            {
                "type": "text",
                "text": short_text,
                "size": "xs",
                "weight": weight,
                "color": text_color,
                "flex": 3,
                "align": "end",
            },
            {
                "type": "text",
                "text": ratio_text,
                "size": "xs",
                "weight": weight,
                "color": text_color,
                "flex": 2,
                "align": "end",
            },
        ],
    }

def _build_margin_flex(
    stock_id: str,
    stock_name: str,
    rows: list[dict],
    current_tf: str,
) -> dict[str, Any]:
    all_rows = sorted(
        list(rows or []),
        key=_margin_sort_key,
        reverse=True,
    )

    table_rows = all_rows[:10]

    latest = table_rows[0] if table_rows else {}
    previous = table_rows[1] if len(table_rows) >= 2 else {}

    latest_date = _margin_get(
        latest,
        "date",
        "trade_date",
        "日期",
        default="--",
    )

    latest_margin = _margin_to_float(
        _margin_get(
            latest,
            "margin",
            "margin_balance",
            "融資",
            "融資餘額",
            default=0,
        )
    )

    latest_short = _margin_to_float(
        _margin_get(
            latest,
            "short",
            "short_balance",
            "融券",
            "融券餘額",
            default=0,
        )
    )

    latest_ratio = _margin_get(
        latest,
        "ratio",
        "short_margin_ratio",
        "資券比",
        "券資比",
        default="--",
    )

    previous_margin = _margin_to_float(
        _margin_get(
            previous,
            "margin",
            "margin_balance",
            "融資",
            "融資餘額",
            default=0,
        )
    )

    previous_short = _margin_to_float(
        _margin_get(
            previous,
            "short",
            "short_balance",
            "融券",
            "融券餘額",
            default=0,
        )
    )

    margin_change = None
    short_change = None

    if previous:
        margin_change = latest_margin - previous_margin
        short_change = latest_short - previous_short

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{stock_id} {stock_name}",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"融資券｜最新 {_margin_short_date(latest_date)}",
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "sm",
            "wrap": True,
        },
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "margin": "md",
            "contents": [
                _margin_metric_box(
                    "融資餘額",
                    _margin_fmt_lots(latest_margin),
                    "增減",
                    _margin_fmt_signed_lots(margin_change),
                    _margin_change_color(margin_change),
                ),
                _margin_metric_box(
                    "融券餘額",
                    _margin_fmt_lots(latest_short),
                    "增減",
                    _margin_fmt_signed_lots(short_change),
                    _margin_change_color(short_change),
                ),
            ],
        },
        {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F8F9FA",
            "cornerRadius": "12px",
            "paddingAll": "10px",
            "margin": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": "券資比",
                    "size": "xs",
                    "color": "#888888",
                },
                {
                    "type": "text",
                    "text": _margin_fmt_ratio(latest_ratio),
                    "size": "lg",
                    "weight": "bold",
                    "color": "#111111",
                    "margin": "xs",
                },
            ],
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": "近10日",
            "size": "md",
            "weight": "bold",
            "color": "#444444",
            "margin": "md",
        },
        {
            "type": "box",
            "layout": "vertical",
            "margin": "sm",
            "spacing": "xs",
            "contents": [
                _margin_table_row_v2(
                    "日期",
                    "融資餘額",
                    "融券餘額",
                    "券資比",
                    is_header=True,
                ),
                *[
                    _margin_table_row_v2(
                        _margin_short_date(
                            _margin_get(
                                r,
                                "date",
                                "trade_date",
                                "日期",
                                default="--",
                            )
                        ),
                        _margin_fmt_lots(
                            _margin_to_float(
                                _margin_get(
                                    r,
                                    "margin",
                                    "margin_balance",
                                    "融資",
                                    "融資餘額",
                                    default=0,
                                )
                            )
                        ),
                        _margin_fmt_lots(
                            _margin_to_float(
                                _margin_get(
                                    r,
                                    "short",
                                    "short_balance",
                                    "融券",
                                    "融券餘額",
                                    default=0,
                                )
                            )
                        ),
                        _margin_fmt_ratio(
                            _margin_get(
                                r,
                                "ratio",
                                "short_margin_ratio",
                                "資券比",
                                "券資比",
                                default="--",
                            )
                        ),
                    )
                    for r in table_rows
                ],
            ],
        },
    ]

    if not table_rows:
        contents.append(
            {
                "type": "text",
                "text": "目前查無融資券資料。",
                "size": "sm",
                "color": "#888888",
                "margin": "md",
                "wrap": True,
            }
        )

    contents.extend(_mode_buttons(stock_id, "margin", current_tf))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 融資券",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "14px",
                "contents": contents,
            },
        },
    }

def _fmt_market_price(value) -> str:
    try:
        num = float(value)

        if num == 0:
            return "--"

        return f"{num:,.2f}"

    except Exception:
        return "--"


def _fmt_market_int(value) -> str:
    try:
        num = int(float(value))

        if num == 0:
            return "--"

        return f"{num:,}"

    except Exception:
        return "--"

def _fmt_price(value: float) -> str:
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return "--"


def _fmt_int(value: int) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return "--"


def _fmt_signed(value: float) -> str:
    try:
        return f"{float(value):+,.2f}"
    except Exception:
        return "--"


def _fmt_signed_pct(value: float) -> str:
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "--"

def _futures_session_buttons(
    stock_id: str,
    active_session: str,
    current_tf: str,
) -> dict[str, Any]:
    """
    期貨專用：日盤 / 全盤切換按鈕
    active_session:
    - day
    - all
    """
    active_session = str(active_session or "day").strip().lower()

    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "md",
        "contents": [
            _postback_button(
                label="日盤",
                data=f"{stock_id},futures_day,futures,{current_tf}",
                active=active_session == "day",
            ),
            _postback_button(
                label="全盤",
                data=f"{stock_id},futures_all,futures,{current_tf}",
                active=active_session == "all",
            ),
        ],
    }

MARKET_INDEX_KEYWORDS = {
    "大盤",
    "台股大盤",
    "台灣大盤",
    "加權",
    "加權指數",
    "台灣加權",
    "台股加權",
    "指數",
    "台指",
    "TAIEX",
    "TWII",
    "^TWII",
}

MARKET_INDEX_ACTIONS = {
    "market_index",
    "market_chip",
    "market_margin",
    "market_future_day",
    "market_future_all",
}

def _is_market_index_request(*values) -> bool:
    for value in values:
        text = str(value or "").strip()

        if not text:
            continue

        upper_text = text.upper()

        if upper_text in MARKET_INDEX_KEYWORDS:
            return True

        if text in MARKET_INDEX_KEYWORDS:
            return True
            
        if "大盤" in text:
            return True

        if "加權" in text:
            return True

        if "指數" in text:
            return True

        if "TAIEX" in upper_text:
            return True

        if "TWII" in upper_text:
            return True
    
    return False

def _is_market_future_request(*values) -> bool:
    keywords = {
        "TXF",
        "台指期",
        "大盤期貨",
        "加權期貨",
        "台指期貨",
    }

    for value in values:
        text = str(value or "").strip()

        if not text:
            continue

        if text.upper() in keywords:
            return True

        if text in keywords:
            return True

    return False

def _build_futures_flex(
    stock_id: str,
    stock_name: str,
    snapshot,
    current_tf: str,
    active_session: str = "day",
) -> dict[str, Any]:
    """
    個股股票期貨 Flex 卡片。

    重點：
    - 期貨價格拉到上方，像行情卡。
    - 期現價差獨立成摘要區。
    - 商品、契約、現貨、成交量等放在明細區。
    - 保留日盤 / 全盤切換。
    - 保留底部個股模式按鈕。
    """

    def _f_num(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _f_int(value, default: int = 0) -> int:
        try:
            return int(float(value))
        except Exception:
            return default

    def _color_by_value(value) -> str:
        num = _f_num(value)

        if num > 0:
            return "#FF2D2D"

        if num < 0:
            return "#00B050"

        return "#666666"

    def _fmt_date_short(value) -> str:
        text = str(value or "--").strip()

        if len(text) >= 10 and text[4:5] in {"-", "/"}:
            return text[:10]

        return text or "--"

    def _info_row(label: str, value: str, color: str = "#222222") -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "sm",
                    "color": "#888888",
                    "flex": 3,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": str(value),
                    "size": "sm",
                    "color": color,
                    "weight": "bold" if color != "#888888" else "regular",
                    "flex": 7,
                    "align": "end",
                    "wrap": True,
                },
            ],
        }

    def _metric_box(
        title: str,
        value: str,
        sub_value: str = "",
        value_color: str = "#111111",
    ) -> dict[str, Any]:
        box_contents: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": title,
                "size": "xs",
                "color": "#888888",
                "wrap": True,
            },
            {
                "type": "text",
                "text": value,
                "size": "lg",
                "weight": "bold",
                "color": value_color,
                "margin": "xs",
                "wrap": True,
            },
        ]

        if sub_value:
            box_contents.append(
                {
                    "type": "text",
                    "text": sub_value,
                    "size": "xs",
                    "color": "#888888",
                    "margin": "xs",
                    "wrap": True,
                }
            )

        return {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F8F9FA",
            "cornerRadius": "12px",
            "paddingAll": "10px",
            "contents": box_contents,
        }

    session_label = "全盤" if str(active_session).lower() == "all" else "日盤"

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{stock_id} {stock_name}",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"股票期貨近月｜{session_label}",
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "sm",
            "wrap": True,
        },
    ]

    # 查不到期貨資料時
    if not getattr(snapshot, "available", False):
        contents.extend(
            [
                {
                    "type": "separator",
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": getattr(snapshot, "message", "查無股票期貨資料。"),
                    "size": "sm",
                    "color": "#666666",
                    "wrap": True,
                    "margin": "md",
                },
                {
                    "type": "separator",
                    "margin": "md",
                },
            ]
        )

        contents.append(
            _futures_session_buttons(
                stock_id=stock_id,
                active_session=active_session,
                current_tf=current_tf,
            )
        )

        contents.extend(_mode_buttons(stock_id, "futures", current_tf))

        return {
            "type": "flex",
            "altText": f"{stock_id} {stock_name} 股票期貨",
            "contents": {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "14px",
                    "spacing": "sm",
                    "contents": contents,
                },
            },
        }

    future_price = _f_num(getattr(snapshot, "future_price", 0.0))
    future_change = _f_num(getattr(snapshot, "future_change", 0.0))
    future_change_pct = _f_num(getattr(snapshot, "future_change_pct", 0.0))

    spot_price = _f_num(getattr(snapshot, "spot_price", 0.0))
    basis = _f_num(getattr(snapshot, "basis", 0.0))
    basis_pct = _f_num(getattr(snapshot, "basis_pct", 0.0))

    volume = _f_int(getattr(snapshot, "volume", 0))
    open_interest = _f_int(getattr(snapshot, "open_interest", 0))

    change_color = _color_by_value(future_change)
    basis_color = _color_by_value(basis)

    price_text = _fmt_price(future_price)
    change_text = (
        f"{_fmt_signed(future_change)} "
        f"({_fmt_signed_pct(future_change_pct)})"
    )

    basis_text = (
        f"{_fmt_signed(basis)} "
        f"({_fmt_signed_pct(basis_pct)})"
    )

    futures_name = str(getattr(snapshot, "futures_name", "") or f"{stock_name}期貨")
    futures_id = str(getattr(snapshot, "futures_id", "") or "--")
    contract_date = str(getattr(snapshot, "contract_date", "") or "--")
    trade_date = _fmt_date_short(getattr(snapshot, "trade_date", "--"))
    quote_source = str(getattr(snapshot, "quote_source", "") or "--")
    quote_time = str(getattr(snapshot, "quote_time", "") or "").strip()
    chart_url = str(getattr(snapshot, "chart_url", "") or "").strip()

    # 價格摘要
    contents.extend(
        [
            {
                "type": "text",
                "text": price_text,
                "size": "xxl",
                "weight": "bold",
                "color": change_color,
                "margin": "md",
                "wrap": True,
            },
            {
                "type": "text",
                "text": change_text,
                "size": "md",
                "weight": "bold",
                "color": change_color,
                "margin": "xs",
                "wrap": True,
            },
            {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#F8F9FA",
                "cornerRadius": "12px",
                "paddingAll": "10px",
                "margin": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "期現價差",
                        "size": "xs",
                        "color": "#888888",
                    },
                    {
                        "type": "text",
                        "text": basis_text,
                        "size": "lg",
                        "weight": "bold",
                        "color": basis_color,
                        "margin": "xs",
                    },
                ],
            },
            {
                "type": "separator",
                "margin": "md",
            },
        ]
    )

    # 日盤 / 全盤切換按鈕
    contents.append(
        _futures_session_buttons(
            stock_id=stock_id,
            active_session=active_session,
            current_tf=current_tf,
        )
    )

    # 圖片
    if chart_url:
        contents.append(
            {
                "type": "image",
                "url": chart_url,
                "size": "full",
                "aspectRatio": "4:3",
                "aspectMode": "fit",
                "margin": "md",
                "backgroundColor": "#FFFFFF",
            }
        )
    else:
        contents.append(
            {
                "type": "text",
                "text": "目前暫無股票期貨圖表。",
                "size": "sm",
                "color": "#888888",
                "margin": "md",
                "wrap": True,
            }
        )

    # 明細摘要：兩欄卡片
    contents.extend(
        [
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "md",
                "contents": [
                    _metric_box(
                        "現貨",
                        _fmt_price(spot_price),
                    ),
                    _metric_box(
                        "成交量",
                        _fmt_int(volume),
                    ),
                ],
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "sm",
                "contents": [
                    _metric_box(
                        "契約",
                        contract_date,
                    ),
                    _metric_box(
                        "未平倉",
                        _fmt_int(open_interest),
                    ),
                ],
            },
            {
                "type": "separator",
                "margin": "md",
            },
            {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "margin": "md",
                "contents": [
                    _info_row("商品", f"{futures_name} ({futures_id})", "#222222"),
                    _info_row("時段", getattr(snapshot, "trading_session", session_label), "#222222"),
                    _info_row("日期", trade_date, "#888888"),
                    _info_row("資料", quote_source, "#888888"),
                ],
            },
        ]
    )

    if quote_time:
        contents[-1]["contents"].append(
            _info_row("更新", quote_time[:19], "#888888")
        )

    contents.extend(
        [
            {
                "type": "text",
                "text": "期現價差＝股票期貨近月 − 現貨。期貨資料可能因交易時段與資料源更新頻率而短暫落差。",
                "size": "xs",
                "color": "#888888",
                "wrap": True,
                "margin": "md",
            },
            {
                "type": "separator",
                "margin": "md",
            },
        ]
    )

    contents.extend(_mode_buttons(stock_id, "futures", current_tf))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 股票期貨",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "14px",
                "spacing": "sm",
                "contents": contents,
            },
        },
    }
    
def _build_text_flex(
    stock_id: str,
    stock_name: str,
    title: str,
    message: str,
    active_mode: str,
    current_tf: str,
) -> dict[str, Any]:
    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{stock_id} {stock_name}",
            "size": "xxl",
            "weight": "bold",
            "wrap": True,
        },
        {
            "type": "text",
            "text": title,
            "size": "lg",
            "weight": "bold",
            "margin": "sm",
            "wrap": True,
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": message,
            "size": "md",
            "wrap": True,
            "margin": "md",
            "color": "#333333",
        },
    ]

    contents.extend(_mode_buttons(stock_id, active_mode, current_tf))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} {title}",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "14px",
                "contents": contents,
            },
        },
    }


def text_message(message: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": message,
    }

def _reply_with_title(title: str, message: dict[str, Any]) -> list[dict[str, Any]]:
    """
    讓每個按鈕都先跳一則文字，再跳 Flex。
    """
    return [
        text_message(title),
        message,
    ]

def handle_request(req: BotRequest) -> dict[str, Any]:
    """
    LINE / Make 進來後的主控制器。
    """
    try:
        action = _normalize_action(req.action)
        current_mode = _normalize_action(req.current_mode or action)
        requested_tf = normalize_time_frame(req.time_frame)

        raw_stock = str(getattr(req, "stock", "") or "").strip()
        raw_text = str(getattr(req, "raw_text", "") or "").strip()

        # =========================
        # 加權指數 / 大盤路由
        # 必須放在 normalize_stock_input() 前面
        # =========================
        if (
            action in MARKET_INDEX_ACTIONS
            or _is_market_index_request(raw_stock, raw_text)
        ):
            if action not in MARKET_INDEX_ACTIONS:
                if _is_market_future_request(raw_stock, raw_text):
                    action = "market_future_day"
                else:
                    action = "market_index"

            if action == "market_index":
                snapshot = ()

                if action == "market_index":
                    import time

                    t0 = time.perf_counter()

                    snapshot = get_market_index_snapshot(with_chart=True)

                    t1 = time.perf_counter()

                    flex = _build_market_index_realtime_flex(snapshot)

                    t2 = time.perf_counter()

                    print(
                        "DEBUG market_index controller timing",
                        "| raw_stock =", raw_stock,
                        "| raw_text =", raw_text,
                        "| action =", action,
                        "| snapshot_sec =", round(t1 - t0, 3),
                        "| flex_sec =", round(t2 - t1, 3),
                        "| total_sec =", round(t2 - t0, 3),
                        "| available =",
                        getattr(snapshot, "available", None),
                        "| chart_url =",
                        bool(getattr(snapshot, "chart_url", "")),
                        flush=True,
                    )

                    return _reply_with_title(
                        "加權指數",
                        flex,
                    )


            if action == "market_chip":
                snapshot = get_market_chip_snapshot()

                return _reply_with_title(
                "大盤法人",
                _build_market_chip_flex(snapshot),
                )

            if action == "market_margin":
                snapshot = get_market_margin_snapshot()

                return _reply_with_title(
                "大盤融資券",
                _build_market_margin_flex(snapshot),
                )
            
            if action == "financial":
                snapshot = get_financial_snapshot(meta.stock_id, stock_name)
                flex = _build_financial_flex(meta.stock_id, stock_name, snapshot, requested_tf)
                return _reply_with_title(f"{stock_name} 財務", flex)
                        
            if action in {"market_future_day", "market_future_all"}:
                session_mode = "all" if action == "market_future_all" else "day"

                snapshot = get_market_future_snapshot(session_mode=session_mode)
                index_snapshot = get_market_index_snapshot(with_chart=False)

                return _reply_with_title(
                    "台指期",
                    _build_market_future_realtime_flex(snapshot, action, index_snapshot),
                )

            return _reply_with_title(
                "加權指數",
                _build_market_index_placeholder_flex(action),
            )

        import time

        stock_t0 = time.perf_counter()

        if action == "financial":
            import time

            t_fin0 = time.perf_counter()

            snapshot = get_financial_snapshot(
                meta.stock_id,
                stock_name,
            )

            t_fin1 = time.perf_counter()

            print(
                "DEBUG stock timing financial",
                "| stock_id =",
                meta.stock_id,
                "| available =",
                bool(getattr(snapshot, "available", False)),
                "| rows =",
                len(getattr(snapshot, "rows", []) or []),
                "| sec =",
                round(t_fin1 - t_fin0, 3),
                flush=True,
            )

            flex = _build_financial_flex(
                meta.stock_id,
                stock_name,
                snapshot,
                requested_tf,
            )

            return _reply_with_title(
                f"{stock_name} 財務",
                flex,
            )
        
        print(
            "DEBUG stock timing enter",
            "| raw_stock =", req.stock,
            "| raw_text =", raw_text,
            "| action =", action,
            "| current_mode =", current_mode,
            "| requested_tf =", requested_tf,
            flush=True,
        )

        # -------------------------
        # 1. 股票代碼 / 名稱解析
        # -------------------------
        t = time.perf_counter()

        meta = normalize_stock_input(req.stock)

        t_meta = time.perf_counter()

        stock_name = get_stock_name(meta)

        t_name = time.perf_counter()

        print(
            "DEBUG stock timing normalize",
            "| input =", req.stock,
            "| stock_id =", getattr(meta, "stock_id", ""),
            "| yf_symbol =", getattr(meta, "yf_symbol", ""),
            "| stock_name =", stock_name,
            "| normalize_sec =", round(t_meta - t, 3),
            "| name_sec =", round(t_name - t_meta, 3),
            flush=True,
        )

        # -------------------------
        # 1.5 模式 / 週期修正
        # -------------------------
        # 規則：
        # - instant 只搭配 1m / 5m
        # - k_line 只搭配 D / W / M
        # - 使用者單純輸入股票代號時：
        #   盤中預設即時 1分，盤後預設日K。
        requested_tf = normalize_time_frame(requested_tf)
        action = _normalize_action(action)
        current_mode = _normalize_action(current_mode)

        is_plain_stock_entry = (
            action == "instant"
            and current_mode == "instant"
            and requested_tf in {"D", "", None}
        )

        if is_plain_stock_entry:
            if _is_tw_stock_live_session():
                action = "instant"
                current_mode = "instant"
                requested_tf = "1m"
            else:
                action = "k_line"
                current_mode = "k_line"
                requested_tf = "D"

        elif action == "instant" and requested_tf in {"D", "W", "M"}:
            action = "k_line"
            current_mode = "k_line"

        elif action == "k_line" and requested_tf in {"1m", "5m"}:
            action = "instant"
            current_mode = "instant"

        print(
            "DEBUG stock timing action_adjust",
            "| stock_id =", getattr(meta, "stock_id", ""),
            "| action =", action,
            "| current_mode =", current_mode,
            "| requested_tf =", requested_tf,
            "| elapsed_sec =", round(time.perf_counter() - stock_t0, 3),
            flush=True,
        )

        # -------------------------
        # 2. 即時 / K 線 / 法人
        # -------------------------
        if action in {"instant", "k_line", "chip"}:
            t_history0 = time.perf_counter()

            df, tf = _get_history_df_tf_safe(meta, requested_tf)

            t_history1 = time.perf_counter()

            print(
                "DEBUG stock timing history",
                "| stock_id =", getattr(meta, "stock_id", ""),
                "| action =", action,
                "| requested_tf =", requested_tf,
                "| final_tf =", tf,
                "| rows =", 0 if df is None else len(df),
                "| sec =", round(t_history1 - t_history0, 3),
                flush=True,
            )

            if df is None or len(df) == 0:
                return text_message(
                    "目前暫時抓不到這檔股票的行情資料。"
                    "若是 Yahoo/yfinance 限流，請稍後再試；"
                    "也可以先查法人、大戶、融資券。"
                )

            t_append0 = time.perf_counter()

            if tf in {"1m", "5m"}:
                import os

                allow_cold_login = (
                    str(os.getenv("ALLOW_COLD_SHIOAJI_STOCK_APPEND", "1")).strip()
                    == "1"
                )

                df_attrs_backup = dict(getattr(df, "attrs", {}) or {})

                try:
                    df = append_stock_snapshot_to_intraday_df_fast(
                        df,
                        meta.stock_id,
                        allow_cold_login=allow_cold_login,
                    )
                except Exception as exc:
                    print(
                        "DEBUG append_stock_snapshot_to_intraday_df_fast failed",
                        "| stock_id =", meta.stock_id,
                        "| error =", repr(exc),
                        flush=True,
                    )

                try:
                    df.attrs.update(df_attrs_backup)
                except Exception:
                    pass

                # 修正 Yahoo 盤中延遲：再用 Shioaji snapshot 強制補最後一列。
                df = _apply_shioaji_stock_realtime(df, meta.stock_id)

            t_append1 = time.perf_counter()

            print(
                "DEBUG stock timing append_snapshot",
                "| stock_id =", getattr(meta, "stock_id", ""),
                "| tf =", tf,
                "| rows =", 0 if df is None else len(df),
                "| sec =", round(t_append1 - t_append0, 3),
                flush=True,
            )

            t_price0 = time.perf_counter()

            price_meta = build_price_meta(df, tf)

            t_price1 = time.perf_counter()

            print(
                "DEBUG stock timing price_meta",
                "| stock_id =", getattr(meta, "stock_id", ""),
                "| tf =", tf,
                "| price_info =", getattr(price_meta, "price_info", ""),
                "| change_info =", getattr(price_meta, "change_info", ""),
                "| sec =", round(t_price1 - t_price0, 3),
                flush=True,
            )

            if action == "instant":
                t_chart0 = time.perf_counter()

                print(
                    "DEBUG instant chart before generate",
                    "| stock_id =", meta.stock_id,
                    "| tf =", tf,
                    "| df_is_none =", df is None,
                    "| rows =", 0 if df is None else len(df),
                    "| columns =", [] if df is None else list(df.columns),
                    flush=True,
                )

                image_url = generate_instant_chart(df, meta.stock_id, stock_name)

                print(
                    "DEBUG instant chart after generate",
                    "| stock_id =", meta.stock_id,
                    "| tf =", tf,
                    "| image_url =", image_url,
                    flush=True,
                )

                t_chart1 = time.perf_counter()

                print(
                    "DEBUG stock timing chart",
                    "| stock_id =", getattr(meta, "stock_id", ""),
                    "| action =", action,
                    "| tf =", tf,
                    "| image_url =", bool(image_url),
                    "| sec =", round(t_chart1 - t_chart0, 3),
                    flush=True,
                )

                t_flex0 = time.perf_counter()

                flex = _build_chart_flex(
                    stock_id=meta.stock_id,
                    stock_name=stock_name,
                    image_url=image_url,
                    price_info=price_meta.price_info,
                    change_info=price_meta.change_info,
                    update_time=price_meta.time_stamp,
                    price_change=price_meta.price_change,
                    active_mode="instant",
                    current_tf=tf,
                )

                t_flex1 = time.perf_counter()

                print(
                    "DEBUG stock timing flex",
                    "| stock_id =", getattr(meta, "stock_id", ""),
                    "| action =", action,
                    "| sec =", round(t_flex1 - t_flex0, 3),
                    flush=True,
                )

                print(
                    "DEBUG stock timing total",
                    "| stock_id =", getattr(meta, "stock_id", ""),
                    "| action =", action,
                    "| total_sec =", round(time.perf_counter() - stock_t0, 3),
                    flush=True,
                )

                return _reply_with_title(
                    f"{stock_name} 即時走勢",
                    flex,
                )

            if action == "k_line":
                t_chart0 = time.perf_counter()

                print(
                    "DEBUG kline chart before generate",
                    "| stock_id =", meta.stock_id,
                    "| tf =", tf,
                    "| df_is_none =", df is None,
                    "| rows =", 0 if df is None else len(df),
                    "| columns =", [] if df is None else list(df.columns),
                    flush=True,
                )

                image_url = generate_kline_chart(df, meta.stock_id, stock_name, tf)

                print(
                    "DEBUG kline chart after generate",
                    "| stock_id =", meta.stock_id,
                    "| tf =", tf,
                    "| image_url =", image_url,
                    flush=True,
                )

                t_chart1 = time.perf_counter()

                print(
                    "DEBUG stock timing chart",
                    "| stock_id =", getattr(meta, "stock_id", ""),
                    "| action =", action,
                    "| tf =", tf,
                    "| image_url =", bool(image_url),
                    "| sec =", round(t_chart1 - t_chart0, 3),
                    flush=True,
                )

                t_flex0 = time.perf_counter()

                flex = _build_chart_flex(
                    stock_id=meta.stock_id,
                    stock_name=stock_name,
                    image_url=image_url,
                    price_info=price_meta.price_info,
                    change_info=price_meta.change_info,
                    update_time=price_meta.time_stamp,
                    price_change=price_meta.price_change,
                    active_mode="k_line",
                    current_tf=tf,
                )

                t_flex1 = time.perf_counter()

                print(
                    "DEBUG stock timing flex",
                    "| stock_id =", getattr(meta, "stock_id", ""),
                    "| action =", action,
                    "| sec =", round(t_flex1 - t_flex0, 3),
                    flush=True,
                )

                print(
                    "DEBUG stock timing total",
                    "| stock_id =", getattr(meta, "stock_id", ""),
                    "| action =", action,
                    "| total_sec =", round(time.perf_counter() - stock_t0, 3),
                    flush=True,
                )

                return _reply_with_title(
                    f"{stock_name} K線",
                    flex,
                )

            if action == "chip":
                t_chip0 = time.perf_counter()

                chip_rows = get_institutional_chips(meta.stock_id)

                t_chip1 = time.perf_counter()

                flex = _build_stock_chip_flex(
                    meta.stock_id,
                    stock_name,
                    chip_rows,
                    tf,
                )

                t_flex1 = time.perf_counter()

                print(
                    "DEBUG stock timing chip_flex",
                    "| stock_id =", getattr(meta, "stock_id", ""),
                    "| data_sec =", round(t_chip1 - t_chip0, 3),
                    "| flex_sec =", round(t_flex1 - t_chip1, 3),
                    "| total_sec =", round(time.perf_counter() - stock_t0, 3),
                    flush=True,
                )

                return _reply_with_title(
                    f"{stock_name} 法人",
                    flex,
                )

        # -------------------------
        # 3. 大戶持股
        # -------------------------
        if action == "large_holder":
            t_data0 = time.perf_counter()

            rows = get_large_holder_table(meta.stock_id)

            t_data1 = time.perf_counter()

            flex = _build_large_holder_flex(
                stock_id=meta.stock_id,
                stock_name=stock_name,
                rows=rows,
                current_tf=requested_tf,
            )

            t_flex1 = time.perf_counter()

            print(
                "DEBUG stock timing large_holder",
                "| stock_id =", getattr(meta, "stock_id", ""),
                "| rows =", 0 if rows is None else len(rows),
                "| data_sec =", round(t_data1 - t_data0, 3),
                "| flex_sec =", round(t_flex1 - t_data1, 3),
                "| total_sec =", round(time.perf_counter() - stock_t0, 3),
                flush=True,
            )

            return _reply_with_title(
                f"{stock_name} 大戶持股",
                flex,
            )

        # -------------------------
        # 4. 融資券
        # -------------------------
        if action == "margin":
            t_data0 = time.perf_counter()

            rows = get_margin_table(meta.stock_id)

            t_data1 = time.perf_counter()

            flex = _build_margin_flex(
                stock_id=meta.stock_id,
                stock_name=stock_name,
                rows=rows,
                current_tf=requested_tf,
            )

            t_flex1 = time.perf_counter()

            print(
                "DEBUG stock timing margin",
                "| stock_id =", getattr(meta, "stock_id", ""),
                "| rows =", 0 if rows is None else len(rows),
                "| data_sec =", round(t_data1 - t_data0, 3),
                "| flex_sec =", round(t_flex1 - t_data1, 3),
                "| total_sec =", round(time.perf_counter() - stock_t0, 3),
                flush=True,
            )

            return _reply_with_title(
                f"{stock_name} 融資券",
                flex,
            )

        # -------------------------
        # 5. 個股期貨
        # -------------------------
        if action in {"futures", "futures_day", "futures_all"}:
            futures_session_mode = "day"

            if action == "futures_all":
                futures_session_mode = "all"

            t_data0 = time.perf_counter()

            snapshot = get_stock_futures_snapshot(
                meta.stock_id,
                stock_name,
                session_mode=futures_session_mode,
            )

            t_data1 = time.perf_counter()

            title = snapshot.futures_name or f"{stock_name}期貨"

            flex = _build_futures_flex(
                stock_id=meta.stock_id,
                stock_name=stock_name,
                snapshot=snapshot,
                current_tf=requested_tf,
                active_session=futures_session_mode,
            )

            t_flex1 = time.perf_counter()

            print(
                "DEBUG stock timing futures",
                "| stock_id =", getattr(meta, "stock_id", ""),
                "| session_mode =", futures_session_mode,
                "| available =", getattr(snapshot, "available", None),
                "| data_sec =", round(t_data1 - t_data0, 3),
                "| flex_sec =", round(t_flex1 - t_data1, 3),
                "| total_sec =", round(time.perf_counter() - stock_t0, 3),
                flush=True,
            )

            return _reply_with_title(
                title,
                flex,
            )

        print(
            "DEBUG stock timing unsupported",
            "| stock_id =", getattr(meta, "stock_id", ""),
            "| action =", action,
            "| total_sec =", round(time.perf_counter() - stock_t0, 3),
            flush=True,
        )

        return text_message(f"目前不支援的功能：{action}")

    except Exception as exc:
        print("controller.handle_request failed traceback:", flush=True)
        print(traceback.format_exc(), flush=True)
        return text_message(f"查詢失敗：{type(exc).__name__}: {exc}")
