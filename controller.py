from __future__ import annotations

from services.sinopac_quote_service import (
    append_stock_snapshot_to_intraday_df_fast,
    get_stock_intraday_kbars,
    get_stock_intraday_yahoo_direct,
    get_stock_snapshot as get_shioaji_stock_snapshot,
    is_shioaji_api_ready,
)
from services.market_margin_service import get_market_margin_snapshot

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

    recent_rows = list(getattr(snapshot, "recent_rows", []) or [])[-5:]
    recent_rows = list(reversed(recent_rows))

    table_contents: list[dict[str, Any]] = [_table_header()]

    if recent_rows:
        for item in recent_rows:
            table_contents.append(_table_row(item))
    else:
        table_contents.append(
            {
                "type": "text",
                "text": "暫無近5日資料",
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

    recent_rows = list(getattr(snapshot, "recent_rows", []) or [])[-5:]
    recent_rows = list(reversed(recent_rows))

    table_contents: list[dict[str, Any]] = [_table_header()]

    if recent_rows:
        for item in recent_rows:
            table_contents.append(_table_row(item))
    else:
        table_contents.append(
            {
                "type": "text",
                "text": "暫無近5日資料",
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
            "text": "近5日買賣超",
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
        str(os.getenv("ALLOW_COLD_SHIOAJI_STOCK_APPEND", "0")).strip()
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


def _time_buttons(stock_id: str, current_mode: str, current_tf: str) -> dict[str, Any]:
    """
    上方時間按鈕。

    即時圖只適合 1m / 5m。
    如果按 D / W / M，直接切到 K 線模式。
    """
    current_mode = _normalize_action(current_mode)
    current_tf = normalize_time_frame(current_tf)

    items = [
        ("1分", "1m"),
        ("5分", "5m"),
        ("D", "D"),
        ("W", "W"),
        ("M", "M"),
    ]

    buttons = []

    for label, tf in items:
        if tf in {"D", "W", "M"}:
            action = "k_line"
            mode = "k_line"
        else:
            action = current_mode if current_mode in {"instant", "k_line"} else "instant"
            mode = action

        buttons.append(
            _postback_button(
                label=label,
                data=f"{stock_id},{action},{mode},{tf}",
                active=(current_tf == tf),
            )
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
    active_mode = _normalize_action(active_mode)
    current_tf = normalize_time_frame(current_tf)

    row1 = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "lg",
        "contents": [
            _postback_button(
                "即時",
                f"{stock_id},instant,instant,1m",
                active=active_mode == "instant",
            ),
            _postback_button(
                "K線",
                f"{stock_id},k_line,k_line,D",
                active=active_mode == "k_line",
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
                "法人",
                f"{stock_id},chip,chip,{current_tf}",
                active=active_mode == "chip",
            ),
            _postback_button(
                "大戶",
                f"{stock_id},large_holder,large_holder,{current_tf}",
                active=active_mode == "large_holder",
            ),
            _postback_button(
                "融資券",
                f"{stock_id},margin,margin,{current_tf}",
                active=active_mode == "margin",
            ),
            _postback_button(
                "期貨",
                f"{stock_id},futures,futures,{current_tf}",
                active=active_mode == "futures",
            ),
        ],
    }

    return [row1, row2]

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


def _build_market_future_realtime_flex(
    snapshot,
    action: str = "market_future_day",
    index_snapshot=None,
) -> dict[str, Any]:
    """
    台指期 TXF 即時卡片。
    加入：
    - 現貨：加權指數
    - 期現價差
    - 基差率
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

    def _calc_color(value) -> str:
        try:
            num = float(value)

            if num > 0:
                return "#FF2D2D"

            if num < 0:
                return "#00B050"

        except Exception:
            pass

        return "#666666"

    session_text = "全盤" if action == "market_future_all" else "日盤"

    if not getattr(snapshot, "available", False):
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

    future_price = float(getattr(snapshot, "future_price", 0.0) or 0.0)
    change = float(getattr(snapshot, "future_change", 0.0) or 0.0)
    change_pct = float(getattr(snapshot, "future_change_pct", 0.0) or 0.0)

    change_color = _calc_color(change)

    price_text = _fmt_market_price(future_price)
    change_text = f"{_fmt_signed(change)} ({_fmt_signed_pct(change_pct)})"

    spot_price = 0.0

    if index_snapshot is not None and getattr(index_snapshot, "available", False):
        try:
            spot_price = float(getattr(index_snapshot, "close_price", 0.0) or 0.0)
        except Exception:
            spot_price = 0.0

    rows = [
        (
            "商品",
            f"{getattr(snapshot, 'futures_name', '台指期近月')} ({getattr(snapshot, 'contract_code', 'TXFR1')})",
            "#222222",
        ),
        ("時段", getattr(snapshot, "trading_session", session_text), "#222222"),
        ("資料", getattr(snapshot, "quote_source", "永豐即時"), "#888888"),
        ("更新", str(getattr(snapshot, "quote_time", "") or "--")[:19], "#888888"),
        ("開", _fmt_market_price(getattr(snapshot, "open_price", 0.0)), "#222222"),
        ("高", _fmt_market_price(getattr(snapshot, "high_price", 0.0)), "#222222"),
        ("低", _fmt_market_price(getattr(snapshot, "low_price", 0.0)), "#222222"),
        ("期貨", price_text, change_color),
    ]

    if spot_price > 0 and future_price > 0:
        basis = future_price - spot_price
        basis_pct = basis / spot_price * 100
        basis_color = _calc_color(basis)

        rows.extend(
            [
                ("現貨", _fmt_market_price(spot_price), "#222222"),
                (
                    "期現價差",
                    f"{_fmt_signed(basis)} ({_fmt_signed_pct(basis_pct)})",
                    basis_color,
                ),
            ]
        )

    rows.extend(
        [
            ("漲", change_text, change_color),
            ("量", _fmt_market_int(getattr(snapshot, "total_volume", 0)), "#222222"),
        ]
    )

    buy_price = getattr(snapshot, "buy_price", 0.0)
    sell_price = getattr(snapshot, "sell_price", 0.0)

    if buy_price or sell_price:
        rows.append(
            (
                "買賣",
                f"{_fmt_market_price(buy_price)} / {_fmt_market_price(sell_price)}",
                "#222222",
            )
        )

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
            "type": "text",
            "text": price_text,
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
            "type": "text",
            "text": "期現價差＝台指期近月 − 加權指數現貨。",
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

    body_contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{stock_id} {stock_name}",
            "size": "xxl",
            "weight": "bold",
            "wrap": True,
            "color": "#111111",
        },
        {
            "type": "text",
            "text": f"{price_info}  ({change_info})",
            "size": "lg",
            "weight": "bold",
            "color": color,
            "margin": "sm",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"更新時間：{update_time}",
            "size": "sm",
            "color": "#888888",
            "margin": "xs",
            "wrap": True,
        },
        {
            "type": "separator",
            "margin": "md",
        },
        _time_buttons(stock_id, active_mode, current_tf),
        {
            "type": "image",
            "url": image_url,
            "size": "full",
            "aspectRatio": image_aspect_ratio,
            "aspectMode": "fit",
            "margin": "md",
            "backgroundColor": "#FFFFFF",
        },
    ]

    body_contents.extend(_mode_buttons(stock_id, active_mode, current_tf))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name}",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "14px",
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
    if value is None:
        return "--"

    number = _lh_float(value, default=0.0)

    if number == 0:
        return "--"

    if 0 < abs(number) <= 1:
        number = number * 100

    return f"{number:+.2f}%"


def _lh_change_color(value):
    number = _lh_float(value, default=0.0)

    if 0 < abs(number) <= 1:
        number = number * 100

    if number > 0:
        return "#E53935"

    if number < 0:
        return "#1E9F5A"

    return "#00AA55"


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


def _large_holder_week_row(date_text, ratio_text, change_text, change_color):
    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "sm",
        "contents": [
            {
                "type": "text",
                "text": date_text,
                "size": "md",
                "color": "#555555",
                "flex": 3,
            },
            {
                "type": "text",
                "text": ratio_text,
                "size": "md",
                "weight": "bold",
                "color": "#333333",
                "align": "end",
                "flex": 4,
            },
            {
                "type": "text",
                "text": change_text,
                "size": "md",
                "weight": "bold",
                "color": change_color,
                "align": "end",
                "flex": 3,
            },
        ],
    }


def _build_large_holder_flex(stock_id: str, stock_name: str, rows, current_tf: str = "D"):
    """
    顯示個股大戶持股近 5 週。
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

    if not raw_rows:
        body_contents = [
            {
                "type": "text",
                "text": f"{stock_id} {stock_name}",
                "weight": "bold",
                "size": "xxl",
                "color": "#111111",
            },
            {
                "type": "text",
                "text": "大戶持股近5週",
                "weight": "bold",
                "size": "xl",
                "color": "#444444",
                "margin": "md",
            },
            {"type": "separator", "margin": "lg"},
            {
                "type": "text",
                "text": "目前查無大戶持股資料",
                "size": "md",
                "color": "#777777",
                "margin": "lg",
            },
        ]
    else:
        sorted_rows = sorted(raw_rows, key=_lh_sort_key)
        latest_rows = list(reversed(sorted_rows[-5:]))

        computed_rows = []

        for row in latest_rows:
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

            computed_rows.append(
                {
                    "date": _lh_date_text(date_raw),
                    "ratio": _lh_pct_text(ratio_value),
                    "change": _lh_change_text(change_value),
                    "change_color": _lh_change_color(change_value),
                }
            )

        row_boxes = [
            _large_holder_week_row(
                row["date"],
                row["ratio"],
                row["change"],
                row["change_color"],
            )
            for row in computed_rows
        ]

        body_contents = [
            {
                "type": "text",
                "text": f"{stock_id} {stock_name}",
                "weight": "bold",
                "size": "xxl",
                "color": "#111111",
            },
            {
                "type": "text",
                "text": "大戶持股近5週",
                "weight": "bold",
                "size": "xl",
                "color": "#444444",
                "margin": "md",
            },
            {"type": "separator", "margin": "lg"},
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "lg",
                "contents": [
                    {
                        "type": "text",
                        "text": "日期",
                        "size": "sm",
                        "color": "#888888",
                        "flex": 3,
                    },
                    {
                        "type": "text",
                        "text": "持股比",
                        "size": "sm",
                        "color": "#888888",
                        "align": "end",
                        "flex": 4,
                    },
                    {
                        "type": "text",
                        "text": "週增減",
                        "size": "sm",
                        "color": "#888888",
                        "align": "end",
                        "flex": 3,
                    },
                ],
            },
            *row_boxes,
        ]

    body_contents.extend(
        [
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "xl",
                "contents": [
                    _postback_button(
                        label="即時",
                        data=f"{stock_id},instant,instant,{current_tf}",
                        active=False,
                    ),
                    _postback_button(
                        label="K線",
                        data=f"{stock_id},k_line,k_line,{current_tf}",
                        active=False,
                    ),
                ],
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "sm",
                "contents": [
                    _postback_button(
                        label="法人",
                        data=f"{stock_id},chip,chip,{current_tf}",
                        active=False,
                    ),
                    _postback_button(
                        label="大戶",
                        data=f"{stock_id},large_holder,large_holder,{current_tf}",
                        active=True,
                    ),
                    _postback_button(
                        label="融資券",
                        data=f"{stock_id},margin,margin,{current_tf}",
                        active=False,
                    ),
                    _postback_button(
                        label="期貨",
                        data=f"{stock_id},futures,futures,{current_tf}",
                        active=False,
                    ),
                ],
            },
        ]
    )

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 大戶持股近5週",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "20px",
                "contents": body_contents,
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

def _build_margin_flex(
    stock_id: str,
    stock_name: str,
    rows: list[dict],
    current_tf: str,
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
            "text": "融資券10日動態",
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
            "type": "box",
            "layout": "vertical",
            "margin": "md",
            "spacing": "xs",
            "contents": [
                _margin_table_row("日期", "融資", "融券", "資券比", is_header=True),
                *[
                    _margin_table_row(
                        str(r.get("date", "--")),
                        f"{int(r.get('margin', 0) or 0):,}",
                        f"{int(r.get('short', 0) or 0):,}",
                        str(r.get("ratio", "--")),
                    )
                    for r in rows[:10]
                ],
            ],
        },
    ]

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
    期貨 Flex 卡片。

    需要搭配：
    - _futures_session_buttons()
    - _mode_buttons()
    - _fmt_price()
    - _fmt_signed()
    - _fmt_signed_pct()
    - _fmt_int()
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
            "text": "股票期貨近月",
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "sm",
        },
        {
            "type": "separator",
            "margin": "md",
        },
    ]

    # 日盤 / 全盤切換按鈕
    contents.append(
        _futures_session_buttons(
            stock_id=stock_id,
            active_session=active_session,
            current_tf=current_tf,
        )
    )

    # 查不到期貨資料時
    if not getattr(snapshot, "available", False):
        contents.extend(
            [
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
                    "spacing": "sm",
                    "contents": contents,
                },
            },
        }

    # 圖片
    chart_url = getattr(snapshot, "chart_url", "")

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

    future_change = getattr(snapshot, "future_change", 0.0)
    future_change_pct = getattr(snapshot, "future_change_pct", 0.0)
    basis = getattr(snapshot, "basis", 0.0)

    change_color = "#FF2D2D" if future_change > 0 else "#00B050" if future_change < 0 else "#666666"
    basis_color = "#FF2D2D" if basis > 0 else "#00B050" if basis < 0 else "#666666"

    rows = [
        (
            "商品",
            f"{getattr(snapshot, 'futures_name', '')} ({getattr(snapshot, 'futures_id', '')})",
            "#222222",
        ),
        (
            "契約",
            getattr(snapshot, "contract_date", "--"),
            "#222222",
        ),
        (
            "時段",
            getattr(snapshot, "trading_session", "--"),
            "#222222",
        ),
        (
            "日期",
            getattr(snapshot, "trade_date", "--"),
            "#888888",
        ),
    ]

    quote_source = getattr(snapshot, "quote_source", "")
    quote_time = getattr(snapshot, "quote_time", "")

    if quote_source:
        rows.append(
            (
                "資料",
                quote_source,
                "#888888",
            )
        )

    if quote_time:
        rows.append(
            (
                "更新",
                str(quote_time)[:19],
                "#888888",
            )
        )

    rows.extend(
        [
            (
                "期貨",
                f"{_fmt_price(getattr(snapshot, 'future_price', 0.0))}  "
                f"{_fmt_signed(getattr(snapshot, 'future_change', 0.0))} "
                f"({_fmt_signed_pct(getattr(snapshot, 'future_change_pct', 0.0))})",
                change_color,
            ),
            (
                "現貨",
                _fmt_price(getattr(snapshot, "spot_price", 0.0)),
                "#222222",
            ),
            (
                "期現價差",
                f"{_fmt_signed(getattr(snapshot, 'basis', 0.0))} "
                f"({_fmt_signed_pct(getattr(snapshot, 'basis_pct', 0.0))})",
                basis_color,
            ),
            (
                "成交量",
                _fmt_int(getattr(snapshot, "volume", 0)),
                "#222222",
            ),
            (
                "未平倉",
                _fmt_int(getattr(snapshot, "open_interest", 0)),
                "#222222",
            ),
        ]
    )

    contents.append(
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "margin": "md",
            "contents": [
                _info_row(label, value, color)
                for label, value, color in rows
            ],
        }
    )

    contents.append(
        {
            "type": "text",
            "text": "規則：標準股票期貨、只抓近月；日盤只顯示日盤資料，全盤合併盤後與日盤資料。",
            "size": "xs",
            "color": "#888888",
            "wrap": True,
            "margin": "md",
        }
    )

    contents.append(
        {
            "type": "separator",
            "margin": "md",
        }
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

        # 文字輸入預設是 instant，但 parser 可能給 D。
        # 即時圖只適合 1m / 5m，所以預設改成 1m。
        if action == "instant" and requested_tf not in {"1m", "5m"}:
            requested_tf = "1m"

        # 如果使用者在即時模式按 D/W/M，改用 K 線。
        if action == "instant" and requested_tf in {"D", "W", "M"}:
            action = "k_line"
            current_mode = "k_line"

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
        # 2. 即時 / K 線 / 法人圖
        # -------------------------
        if action in {"instant", "k_line", "chip"}:
            t_history0 = time.perf_counter()

            df, tf = _get_history_df_tf(meta, requested_tf)

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

            t_append0 = time.perf_counter()

            if tf in {"1m", "5m"}:
                import os

                allow_cold_login = (
                    str(os.getenv("ALLOW_COLD_SHIOAJI_STOCK_APPEND", "0")).strip()
                    == "1"
                )

                df_attrs_backup = dict(getattr(df, "attrs", {}) or {})

                df = append_stock_snapshot_to_intraday_df_fast(
                    df,
                    meta.stock_id,
                    allow_cold_login=allow_cold_login,
                )

                try:
                    df.attrs.update(df_attrs_backup)
                except Exception:
                    pass

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

            if tf in {"1m", "5m"}:
                import os

                df_attrs_backup = dict(getattr(df, "attrs", {}) or {})

                try:
                    df = append_stock_snapshot_to_intraday_df_fast(
                        df,
                        meta.stock_id,
                        allow_cold_login=(
                            str(os.getenv("ALLOW_COLD_SHIOAJI_STOCK_APPEND", "0")).strip()
                            == "1"
                        ),
                    )
                except Exception as exc:
                    print(
                        "DEBUG append_stock_snapshot_to_intraday_df_fast failed",
                        "| stock_id =",
                        meta.stock_id,
                        "| error =",
                        repr(exc),
                        flush=True,
                    )

                try:
                    df.attrs.update(df_attrs_backup)
                except Exception:
                    pass
            
                # 修正 Yahoo 盤中延遲的關鍵：
                # 再用 Shioaji snapshot 強制補最後一列。
                df = _apply_shioaji_stock_realtime(df, meta.stock_id)
            
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

                image_url = generate_instant_chart(df, meta.stock_id, stock_name)

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

                image_url = generate_kline_chart(df, meta.stock_id, stock_name, tf)

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

                image_url = generate_chip_chart(meta.stock_id, stock_name, chip_rows)

                t_chart1 = time.perf_counter()

                print(
                    "DEBUG stock timing chip_data_chart",
                    "| stock_id =", getattr(meta, "stock_id", ""),
                    "| data_sec =", round(t_chip1 - t_chip0, 3),
                    "| chart_sec =", round(t_chart1 - t_chip1, 3),
                    "| image_url =", bool(image_url),
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
                    active_mode="chip",
                    current_tf=tf,
                    image_aspect_ratio="4:5",
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
                    f"{stock_name} 法人籌碼",
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
