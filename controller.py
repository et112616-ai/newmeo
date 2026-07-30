from __future__ import annotations

import os

# ============================================================
# VERIFIED DWM CARD PRICE FIX — open this file and check line 4
# W / M charts use daily history for card price, change and date.
# ============================================================
DWM_CARD_PRICE_FIX_VERSION = "2026-07-16-v6-YAHOO-LIVE-CARD-PRICE"
LIVE_DAILY_CANDLE_VERSION = "2026-07-27-v6.5-LIVE-DAILY-CANDLE"
INTRADAY_UNIFIED_FIX_VERSION = "2026-07-16-v2-UNIFIED-1M-ALL-INTRADAY-TF"
MARKET_DATA_FRESHNESS_VERSION = "2026-07-16-v1-STOCK-CARD-FRESHNESS"
ALL_CARD_FRESHNESS_VERSION = "2026-07-17-v2-STOCK-MARKET-FUTURES-FRESHNESS"
MARKET_MARGIN_SWITCH_VERSION = "2026-07-23-v5-TPEX-MARGIN-MONEY"
STOCK_FLEX_RESILIENT_VERSION = "2026-07-24-v1-STOCK-CARD-RESILIENT"
POST_MARKET_COMPARISON_VERSION = (
    "2026-07-28-v2.5-CARD-ALIGNMENT"
)
MARKET_PREDICTION_RELEASE_GATE_VERSION = (
    "2026-07-27-v1.1-LINE-PREDICTION-CARD-CLARITY"
)
PE_RIVER_DISPLAY_TEXT_VERSION = (
    "2026-07-28-v6.6-PE-RIVER-DISPLAY-TEXT"
)
FINANCIAL_RIVER_VISUAL_VERSION = (
    "2026-07-28-v6.7-FINANCIAL-RIVER-VISUAL"
)
POST_MARKET_CARD_ALIGNMENT_VERSION = (
    "2026-07-28-v6.8-POST-MARKET-CARD-ALIGNMENT"
)
POST_MARKET_OUTER_CARD_VERSION = (
    "2026-07-28-v6.9-POST-MARKET-OUTER-CARD"
)
MARKET_DAILY_CONTRIBUTION_CARD_VERSION = (
    "2026-07-28-v7.0-DAILY-TSE-OTC-CONTRIBUTION"
)
CONCEPT_PEER_CARD_VERSION = (
    "2026-07-29-v7.7-GROUP-TREND-BEFORE-COMPARISON"
)
MAIN_FORCE_CARD_VERSION = (
    "2026-07-29-v8.1-FUBON-BACKEND-DATA"
)
INTRADAY_TIME_FRAMES = {"1m", "5m", "15m", "30m", "60m"}
INTRADAY_RESAMPLE_RULES = {
    "1m": "",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "60m": "60min",
}

from services.sinopac_quote_service import (
    append_stock_snapshot_to_intraday_df_fast,
    get_stock_intraday_kbars,
    get_stock_intraday_yahoo_direct,
    get_stock_snapshot as get_shioaji_stock_snapshot,
    is_shioaji_api_ready,
)
from services.market_margin_service import get_market_margin_snapshot
from services.financial_service import get_financial_snapshot
from services.afterhours_analysis_service_v2_5_card_alignment import (
    generate_post_market_analysis_chart,
)
from services.broker_branch_service import get_top_broker_branches
from services.pe_river_service import get_pe_river_snapshot

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
from services.market_future_kline_service import get_market_future_kline_snapshot
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
from utils.flex_style import (
    ACTIVE_COLOR,
    DOWN_COLOR,
    FLAT_COLOR,
    INACTIVE_COLOR,
    UP_COLOR,
    build_chart_fallback,
    build_chart_reload_hint,
    build_postback_button,
    card_context_badge,
)
from utils.parser import BotRequest
import traceback

from datetime import datetime, time
from zoneinfo import ZoneInfo

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

        "market_prediction": "market_prediction",
        "market_predict": "market_prediction",
        "index_prediction": "market_prediction",
        "taiex_prediction": "market_prediction",
        "大盤預測": "market_prediction",
        "大盤15分預測": "market_prediction",
        "大盤15分鐘預測": "market_prediction",
        "15分預測": "market_prediction",
        "15分鐘預測": "market_prediction",

        "market_afterhours": "market_afterhours",
        "market_after_hours": "market_afterhours",
        "market_close_digest": "market_afterhours",
        "大盤盤後": "market_afterhours",
        "大盤盤後分析": "market_afterhours",
        "大盤收盤": "market_afterhours",
        "盤後總覽": "market_afterhours",

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
        "market_margin_tse": "market_margin_tse",
        "market_margin_otc": "market_margin_otc",
        "index_margin": "market_margin",
        "大盤融資券": "market_margin",
        "加權融資券": "market_margin",

        # 大盤 / 加權指數期貨：台指期 TXF
        "market_future": "market_future_all",
        "market_future_day": "market_future_all",
        "market_future_all": "market_future_all",
        "index_future": "market_future_all",
        "taiex_future": "market_future_all",
        "txf": "market_future_all",
        "台指期": "market_future_all",
        "大盤期貨": "market_future_all",
        "加權期貨": "market_future_all",
        "台指期日盤": "market_future_all",
        "台指期全盤": "market_future_all",
        "大盤期貨日盤": "market_future_all",
        "大盤期貨全盤": "market_future_all",

        "market_future_k": "market_future_k",
        "market_future_kline": "market_future_k",
        "台指期k": "market_future_k",
        "台指期k線": "market_future_k",
        "大盤期貨k": "market_future_k",
        "大盤期貨k線": "market_future_k",
        
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

        "large_holder_200": "large_holder_200",
        "large_holder_400": "large_holder_400",
        "large_holder_600": "large_holder_600",
        "large_holder_800": "large_holder_800",
        "large_holder_1000": "large_holder_1000",
        "holder_200": "large_holder_200",
        "holder_400": "large_holder_400",
        "holder_600": "large_holder_600",
        "holder_800": "large_holder_800",
        "holder_1000": "large_holder_1000",
        "200": "large_holder_200",
        "400": "large_holder_400",
        "600": "large_holder_600",
        "800": "large_holder_800",
        "1000": "large_holder_1000",
        "200大戶": "large_holder_200",
        "400大戶": "large_holder_400",
        "600大戶": "large_holder_600",
        "800大戶": "large_holder_800",
        "1000大戶": "large_holder_1000",
        "200張": "large_holder_200",
        "400張": "large_holder_400",
        "600張": "large_holder_600",
        "800張": "large_holder_800",
        "1000張": "large_holder_1000",
        "200張大戶": "large_holder_200",
        "400張大戶": "large_holder_400",
        "600張大戶": "large_holder_600",
        "800張大戶": "large_holder_800",
        "1000張大戶": "large_holder_1000",

        # EPS
        "financial": "financial",
        "finance": "financial",
        "fundamental": "financial",
        "財務": "financial",
        "eps": "financial",

        # 盤後分析：預設進入短線 5 日觀察；另提供隔日沖模式。
        "post_market": "post_market_short",
        "postmarket": "post_market_short",
        "afterhours": "post_market_short",
        "after_hours": "post_market_short",
        "after_market": "post_market_short",
        "盤後": "post_market_short",
        "盤後分析": "post_market_short",
        "支撐壓力": "post_market_short",
        "支撐": "post_market_short",
        "壓力": "post_market_short",
        "post_market_short": "post_market_short",
        "short_support": "post_market_short",
        "短線": "post_market_short",
        "短線支撐": "post_market_short",
        "短線支撐壓力": "post_market_short",
        "post_market_daytrade": "post_market_daytrade",
        "daytrade": "post_market_daytrade",
        "next_day_trade": "post_market_daytrade",
        "隔日沖": "post_market_daytrade",
        "當沖參考": "post_market_daytrade",
        "post_market_method": "post_market_method",
        "post_market_formula": "post_market_method",
        "盤後算法": "post_market_method",
        "計算方式": "post_market_method",
        "黃金切割率": "post_market_finbonacci",

        # 本益比河流圖
        "pe_river": "pe_river",
        "periver": "pe_river",
        "river": "pe_river",
        "pe": "pe_river",
        "本益比河流圖": "pe_river",
        "本益比河流": "pe_river",
        "河流圖": "pe_river",
        "河流": "pe_river",
        "估值": "pe_river",

        # 主力進出（富邦 eBroker 券商分點彙總）
        "main_force": "main_force",
        "mainforce": "main_force",
        "major_force": "main_force",
        "major_trend": "main_force",
        "主力": "main_force",
        "主力進出": "main_force",
        "主力籌碼": "main_force",
        "主力買賣超": "main_force",

        # 概念族群比較
        "peer_compare": "peer_compare",
        "peer_comparison": "peer_compare",
        "concept_peer": "peer_compare",
        "concept_compare": "peer_compare",
        "同族群": "peer_compare",
        "同族群比較": "peer_compare",
        "族群": "peer_compare",
        "族群比較": "peer_compare",
        "概念族群": "peer_compare",
        "概念比較": "peer_compare",
        # 舊卡片相容：原有「雙刀／配對」postback 全部轉入族群比較。
        "double_knife": "peer_compare",
        "pair_trade": "peer_compare",
        "pair_research": "peer_compare",
        "雙刀": "peer_compare",
        "雙刀研究": "peer_compare",
        "配對研究": "peer_compare",
        "配對交易": "peer_compare",

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
    上市／上櫃大盤融資券卡片。
    """
    market_scope = str(
        getattr(snapshot, "market_scope", "tse") or "tse"
    ).strip().lower()
    market_scope = "otc" if market_scope == "otc" else "tse"
    market_name = "上櫃" if market_scope == "otc" else "上市"
    has_margin_money = bool(
        getattr(snapshot, "has_margin_money", market_scope == "tse")
    )
    card_title = f"{market_name}大盤融資券"

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
        margin_column = "融資金額增減" if has_margin_money else "融資增減"

        return {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "6px",
            "backgroundColor": "#EEF1F4",
            "cornerRadius": "sm",
            "contents": [
                _cell("日期", 2, "#555555", "bold", "start"),
                _cell(margin_column, 4, "#555555", "bold", "end"),
                _cell("融券增減", 3, "#555555", "bold", "end"),
                _cell("資券比", 2, "#555555", "bold", "end"),
            ],
        }

    def _table_row(item: dict) -> dict[str, Any]:
        margin_change = float(item.get("margin_change") or 0)
        margin_money_change = float(item.get("margin_money_change") or 0)
        short_change = int(item.get("short_change") or 0)
        ratio = float(item.get("margin_short_ratio") or 0)
        margin_display = (
            _fmt_margin_money_yi(margin_money_change, signed=True)
            if has_margin_money
            else _fmt_margin_int(margin_change, signed=True)
        )
        margin_color_value = (
            margin_money_change if has_margin_money else margin_change
        )

        return {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "6px",
            "contents": [
                _cell(
                    _fmt_margin_mmdd(item.get("date", "--")),
                    2,
                    "#333333",
                    "regular",
                    "start",
                ),
                _cell(
                    margin_display,
                    4,
                    _margin_change_color(margin_color_value),
                ),
                _cell(
                    _fmt_margin_int(short_change, signed=True),
                    3,
                    _margin_change_color(short_change),
                ),
                _cell(
                    _fmt_margin_ratio(ratio),
                    2,
                    "#333333",
                ),
            ],
        }

    if not getattr(snapshot, "available", False):
        contents: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": card_title,
                "size": "xxl",
                "weight": "bold",
                "color": "#111111",
                "wrap": True,
            },
            _market_margin_scope_buttons(market_scope),
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
            "altText": card_title,
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

    summary_contents: list[dict[str, Any]] = [
        _summary_row("融資餘額", _fmt_margin_int(margin_balance), "#222222"),
        _summary_row(
            "融資增減",
            _fmt_margin_int(margin_change, signed=True),
            _margin_change_color(margin_change),
        ),
        _summary_row("融券餘額", _fmt_margin_int(short_balance), "#222222"),
        _summary_row(
            "融券增減",
            _fmt_margin_int(short_change, signed=True),
            _margin_change_color(short_change),
        ),
        _summary_row("資券比", _fmt_margin_ratio(ratio), "#222222"),
    ]

    if has_margin_money:
        summary_contents.extend(
            [
                _summary_row(
                    "融資金額",
                    _fmt_margin_money_yi(margin_money_balance),
                    "#222222",
                ),
                _summary_row(
                    "融資金額增減",
                    _fmt_margin_money_yi(
                        margin_money_change,
                        signed=True,
                    ),
                    _margin_change_color(margin_money_change),
                ),
            ]
        )

    source = str(getattr(snapshot, "source", "") or "")
    trend_title = (
        "近5日融資金額與融券變化"
        if has_margin_money
        else "近5日融資與融券變化"
    )
    unit_note = (
        "融資金額增減：億元；融券增減：張；"
        "資券比：融券餘額 ÷ 融資餘額。"
        if has_margin_money
        else
        "融資增減、融券增減：張；"
        "資券比：融券餘額 ÷ 融資餘額。"
    )

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": card_title,
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
        _market_margin_scope_buttons(market_scope),
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "margin": "md",
            "contents": summary_contents,
        },
        {
            "type": "text",
            "text": trend_title,
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
            "text": f"{unit_note}資料來源：{source}；盤後資料。",
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
        "altText": card_title,
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


def _copy_df_with_attrs(df):
    if df is None:
        return df

    result = df.copy()
    try:
        result.attrs.update(dict(getattr(df, "attrs", {}) or {}))
    except Exception:
        pass
    return result


def _slice_latest_intraday_session(df):
    """從多日 1 分底稿切出最新交易日，供即時圖與圖卡價格使用。"""
    if df is None or len(df) == 0:
        return df

    try:
        import pandas as pd

        work = _copy_df_with_attrs(df).sort_index()
        if not isinstance(work.index, pd.DatetimeIndex):
            work.index = pd.to_datetime(work.index, errors="coerce")
            work = work[~work.index.isna()].copy()

        if len(work) == 0:
            return work

        latest_date = work.index[-1].date()
        result = work[work.index.date == latest_date].copy()
        result.attrs.update(dict(getattr(work, "attrs", {}) or {}))
        result.attrs["latest_session_date"] = str(latest_date)
        result.attrs["intraday_base_tf"] = "1m"
        result.attrs["display_tf"] = "1m"

        if len(result):
            result.attrs["latest_quote_time"] = str(result.index[-1])
            result.attrs["latest_quote_price"] = float(result["Close"].iloc[-1])

        print(
            "DEBUG INTRADAY LATEST SESSION SLICE",
            "| version =", INTRADAY_UNIFIED_FIX_VERSION,
            "| raw_rows =", len(work),
            "| session_rows =", len(result),
            "| session_date =", latest_date,
            "| raw_last =", work.index[-1],
            flush=True,
        )
        return result

    except Exception as exc:
        print(
            "DEBUG INTRADAY LATEST SESSION SLICE FAILED",
            "| version =", INTRADAY_UNIFIED_FIX_VERSION,
            "| error =", repr(exc),
            flush=True,
        )
        return df


def _resample_intraday_from_1m(df, target_tf: str):
    """把同一份 1 分底稿聚合成 1/5/15/30/60 分圖，並保留原始最新報價時間。"""
    tf = str(target_tf or "1m").strip()

    if df is None or len(df) == 0 or tf not in INTRADAY_TIME_FRAMES:
        return df

    try:
        import pandas as pd

        attrs = dict(getattr(df, "attrs", {}) or {})
        work = df.copy().sort_index()

        if not isinstance(work.index, pd.DatetimeIndex):
            work.index = pd.to_datetime(work.index, errors="coerce")
            work = work[~work.index.isna()].copy()

        if len(work) == 0:
            return work

        rule = INTRADAY_RESAMPLE_RULES.get(tf, "")

        if not rule:
            result = work.copy()
        else:
            result = (
                work.resample(rule, label="left", closed="left")
                .agg(
                    {
                        "Open": "first",
                        "High": "max",
                        "Low": "min",
                        "Close": "last",
                        "Volume": "sum",
                    }
                )
                .dropna(subset=["Open", "High", "Low", "Close"])
            )
            result = result[result["Close"] > 0]

        result.attrs.update(attrs)
        result.attrs["intraday_base_tf"] = "1m"
        result.attrs["display_tf"] = tf
        result.attrs["intraday_unified_version"] = INTRADAY_UNIFIED_FIX_VERSION
        result.attrs["latest_quote_time"] = str(work.index[-1])
        result.attrs["latest_quote_price"] = float(work["Close"].iloc[-1])

        print(
            "DEBUG INTRADAY RESAMPLE ACTIVE",
            "| version =", INTRADAY_UNIFIED_FIX_VERSION,
            "| target_tf =", tf,
            "| rule =", rule or "1min_raw",
            "| raw_rows =", len(work),
            "| chart_rows =", len(result),
            "| raw_first =", work.index[0],
            "| raw_last =", work.index[-1],
            "| chart_last =", result.index[-1] if len(result) else "",
            flush=True,
        )
        return result

    except Exception as exc:
        print(
            "DEBUG INTRADAY RESAMPLE FAILED",
            "| version =", INTRADAY_UNIFIED_FIX_VERSION,
            "| target_tf =", tf,
            "| error =", repr(exc),
            flush=True,
        )
        return df

def _get_history_df_tf(meta, requested_tf):
    """
    取得個股行情資料。

    1m / 5m / 15m / 30m / 60m 優先順序：
    1. Yahoo chart API direct：永遠取得同一份多日 1 分底稿
    2. Shioaji 1 分 kbars fallback
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

    if tf in INTRADAY_TIME_FRAMES:
        # -------------------------
        # 1. Yahoo chart API direct
        # -------------------------
        try:
            t_yahoo0 = time.perf_counter()

            df = get_stock_intraday_yahoo_direct(
                stock_id=stock_id,
                yf_symbol=yf_symbol,
                time_frame="1m",
                timeout=int(os.getenv("YAHOO_DIRECT_TIMEOUT_SECONDS", "5")),
            )

            if df is not None and not df.empty:
                try:
                    df.attrs["intraday_base_tf"] = "1m"
                    df.attrs["requested_tf"] = tf
                    df.attrs["intraday_unified_version"] = INTRADAY_UNIFIED_FIX_VERSION
                except Exception:
                    pass

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

                base_days = max(1, int(os.getenv("SHIOAJI_INTRADAY_BASE_DAYS", "7") or 7))

                df = get_stock_intraday_kbars(stock_id, time_frame="1m", days=base_days)

                t_shioaji1 = time.perf_counter()

                if df is not None and not df.empty:
                    try:
                        df.attrs["intraday_base_tf"] = "1m"
                        df.attrs["requested_tf"] = tf
                        df.attrs["intraday_unified_version"] = INTRADAY_UNIFIED_FIX_VERSION
                    except Exception:
                        pass

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


def _fmt_stock_card_price(value) -> str:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "--"


def _fmt_stock_card_signed(value) -> str:
    try:
        return f"{float(value):+.2f}"
    except Exception:
        return "+0.00"


def _fmt_stock_card_signed_pct(value) -> str:
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "+0.00%"


def _apply_realtime_snapshot_price_meta(price_meta, price_df, tf: str):
    """
    日／週／月 K 圖卡上方的現價與漲跌幅，一律優先採 Shioaji snapshot。

    K 線圖本身仍維持 D / W / M 歷史週期；只有圖卡上方資訊改成：
    - 現價：snapshot close
    - 漲跌：snapshot change
    - 漲跌幅：snapshot change_pct

    snapshot 不存在時，完整保留原本 build_price_meta() 結果。
    """
    normalized_tf = normalize_time_frame(tf)

    if normalized_tf not in {"D", "W", "M"}:
        return price_meta

    attrs = dict(getattr(price_df, "attrs", {}) or {})

    if str(attrs.get("realtime_snapshot_source") or "").lower() != "shioaji":
        return price_meta

    try:
        latest_price = float(attrs.get("realtime_snapshot_price") or 0.0)
    except Exception:
        latest_price = 0.0

    if latest_price <= 0:
        return price_meta

    has_change = bool(attrs.get("realtime_snapshot_has_change"))
    has_change_pct = bool(attrs.get("realtime_snapshot_has_change_pct"))

    try:
        price_change = float(attrs.get("realtime_snapshot_change") or 0.0)
    except Exception:
        price_change = 0.0

    try:
        change_pct = float(attrs.get("realtime_snapshot_change_pct") or 0.0)
    except Exception:
        change_pct = 0.0

    # 少數 snapshot 只提供其中一個欄位時，互相反推。
    if has_change and not has_change_pct:
        prev_close = latest_price - price_change

        if prev_close > 0:
            change_pct = price_change / prev_close * 100
            has_change_pct = True

    elif has_change_pct and not has_change:
        denominator = 1 + change_pct / 100

        if denominator > 0:
            prev_close = latest_price / denominator
            price_change = latest_price - prev_close
            has_change = True

    # change=0 / pct=0 也可能是有效平盤資料，所以用 has_* 判斷。
    if not has_change and not has_change_pct:
        return price_meta

    timestamp = str(attrs.get("realtime_snapshot_time") or "").strip()
    date_text = ""

    if timestamp:
        try:
            import pandas as pd

            parsed = pd.to_datetime(timestamp, errors="coerce")

            if pd.notna(parsed):
                date_text = parsed.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            date_text = timestamp[:10]

    if not date_text:
        date_text = str(getattr(price_meta, "time_stamp", "--") or "--")

    from types import SimpleNamespace

    realtime_meta = SimpleNamespace(
        price_info=_fmt_stock_card_price(latest_price),
        change_info=(
            f"{_fmt_stock_card_signed(price_change)} "
            f"({_fmt_stock_card_signed_pct(change_pct)})"
        ),
        time_stamp=date_text,
        price_change=price_change,
        latest_price=latest_price,
    )

    print(
        "DEBUG stock realtime price_meta override",
        "| tf =", normalized_tf,
        "| latest =", latest_price,
        "| change =", price_change,
        "| pct =", change_pct,
        "| time =", date_text,
        flush=True,
    )

    return realtime_meta


def _apply_yahoo_intraday_price_meta(price_meta, meta, chart_tf: str):
    """
    Shioaji 尚未熱機時，使用 Yahoo chart API 盤中資料覆蓋 D/W/M 圖卡資訊。

    注意：
    - 漲跌採市場標準：最新價 - 前一日收盤價。
    - 不是用最新價 - 今日開盤價。
    - 日K圖片會在上游合併今日未完成K棒；此函式只負責圖卡文字備援。
    - 週K、月K圖片維持原本歷史週期。
    """
    normalized_tf = normalize_time_frame(chart_tf)

    if normalized_tf not in {"D", "W", "M"}:
        return price_meta

    try:
        import os
        import pandas as pd
        from types import SimpleNamespace

        stock_id = str(getattr(meta, "stock_id", "") or "").strip()
        yf_symbol = str(getattr(meta, "yf_symbol", "") or "").strip()

        yahoo_df = get_stock_intraday_yahoo_direct(
            stock_id=stock_id,
            yf_symbol=yf_symbol,
            time_frame="1m",
            timeout=int(os.getenv("YAHOO_CARD_TIMEOUT_SECONDS", "4")),
        )

        if yahoo_df is None or yahoo_df.empty:
            print(
                "DEBUG DWM yahoo card price skipped",
                "| version =", DWM_CARD_PRICE_FIX_VERSION,
                "| stock_id =", stock_id,
                "| chart_tf =", normalized_tf,
                "| reason = empty_yahoo_intraday",
                flush=True,
            )
            return price_meta

        close_series = pd.to_numeric(yahoo_df.get("Close"), errors="coerce").dropna()

        if close_series.empty:
            return price_meta

        attrs = dict(getattr(yahoo_df, "attrs", {}) or {})

        try:
            regular_price = float(attrs.get("regular_market_price") or 0.0)
        except Exception:
            regular_price = 0.0

        latest_price = regular_price if regular_price > 0 else float(close_series.iloc[-1])

        try:
            previous_close = float(attrs.get("previous_close") or 0.0)
        except Exception:
            previous_close = 0.0

        if latest_price <= 0 or previous_close <= 0:
            print(
                "DEBUG DWM yahoo card price skipped",
                "| version =", DWM_CARD_PRICE_FIX_VERSION,
                "| stock_id =", stock_id,
                "| chart_tf =", normalized_tf,
                "| latest =", latest_price,
                "| previous_close =", previous_close,
                "| reason = invalid_price_or_previous_close",
                flush=True,
            )
            return price_meta

        yahoo_ts = pd.to_datetime(yahoo_df.index[-1], errors="coerce")

        if pd.isna(yahoo_ts):
            return price_meta

        # Yahoo 若比日 K 還舊，不覆蓋，避免非交易日反而倒退。
        current_stamp = str(getattr(price_meta, "time_stamp", "") or "").strip()

        if current_stamp:
            current_ts = pd.to_datetime(current_stamp, errors="coerce")

            if pd.notna(current_ts) and yahoo_ts.normalize() < current_ts.normalize():
                print(
                    "DEBUG DWM yahoo card price skipped",
                    "| version =", DWM_CARD_PRICE_FIX_VERSION,
                    "| stock_id =", stock_id,
                    "| chart_tf =", normalized_tf,
                    "| yahoo_time =", str(yahoo_ts),
                    "| current_time =", current_stamp,
                    "| reason = yahoo_older_than_daily",
                    flush=True,
                )
                return price_meta

        price_change = latest_price - previous_close
        change_pct = price_change / previous_close * 100
        time_text = yahoo_ts.strftime("%Y-%m-%d %H:%M:%S")

        yahoo_meta = SimpleNamespace(
            price_info=_fmt_stock_card_price(latest_price),
            change_info=(
                f"{_fmt_stock_card_signed(price_change)} "
                f"({_fmt_stock_card_signed_pct(change_pct)})"
            ),
            time_stamp=time_text,
            price_change=price_change,
            latest_price=latest_price,
        )

        print(
            "DEBUG DWM YAHOO CARD PRICE ACTIVE",
            "| version =", DWM_CARD_PRICE_FIX_VERSION,
            "| stock_id =", stock_id,
            "| chart_tf =", normalized_tf,
            "| latest =", latest_price,
            "| previous_close =", previous_close,
            "| change =", price_change,
            "| pct =", change_pct,
            "| yahoo_time =", time_text,
            "| source = yahoo_direct",
            flush=True,
        )

        return yahoo_meta

    except Exception as exc:
        print(
            "DEBUG DWM yahoo card price failed",
            "| version =", DWM_CARD_PRICE_FIX_VERSION,
            "| stock_id =", str(getattr(meta, "stock_id", "") or ""),
            "| chart_tf =", normalized_tf,
            "| error =", repr(exc),
            flush=True,
        )
        return price_meta


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

    # snapshot 的 volume /  多半是累積量，
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

    change_raw = _snap_get(
        snapshot,
        "change",
        "change_price",
        "price_change",
        default=None,
    )
    change_pct_raw = _snap_get(
        snapshot,
        "change_pct",
        "change_rate",
        "price_change_pct",
        default=None,
    )

    result.attrs["realtime_snapshot_source"] = "shioaji"
    result.attrs["realtime_snapshot_price"] = close_price
    result.attrs["realtime_snapshot_change"] = _snap_float(
        snapshot,
        "change",
        "change_price",
        "price_change",
        default=0.0,
    )
    result.attrs["realtime_snapshot_change_pct"] = _snap_float(
        snapshot,
        "change_pct",
        "change_rate",
        "price_change_pct",
        default=0.0,
    )
    result.attrs["realtime_snapshot_has_change"] = change_raw is not None
    result.attrs["realtime_snapshot_has_change_pct"] = change_pct_raw is not None
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


def _upsert_live_daily_candle(history_df, meta):
    """
    將今天盤中的 1 分資料聚合為一根未完成日 K，覆蓋日線最後一根。

    資料優先序：
    1. Yahoo 多日 1 分底稿提供今日完整 Open / High / Low / Volume。
    2. Shioaji snapshot 補上較新的 Close、當日 High / Low 與時間。
    3. Yahoo 暫時無資料時，才使用 Shioaji snapshot 單獨建立今日 K。

    同一天永遠只保留一列，避免 MA、成交量與高低點被重複計算。
    """
    import time as time_module

    import pandas as pd

    t0 = time_module.perf_counter()

    if history_df is None or getattr(history_df, "empty", True):
        return history_df

    stock_id = str(getattr(meta, "stock_id", "") or "").strip()
    yf_symbol = str(getattr(meta, "yf_symbol", "") or "").strip()

    if not stock_id:
        return history_df

    try:
        now_tpe = pd.Timestamp.now(tz="Asia/Taipei").tz_localize(None)
    except Exception:
        now_tpe = pd.Timestamp.now()

    today = now_tpe.normalize()
    live_row = None
    live_time = None
    live_source = ""
    live_attrs: dict[str, Any] = {}

    # 先使用既有 Yahoo 1 分底稿，不增加新的行情供應商。
    try:
        intraday_df = get_stock_intraday_yahoo_direct(
            stock_id=stock_id,
            yf_symbol=yf_symbol,
            time_frame="1m",
            timeout=int(os.getenv("YAHOO_CARD_TIMEOUT_SECONDS", "4")),
        )

        if intraday_df is not None and not intraday_df.empty:
            intraday_attrs = dict(getattr(intraday_df, "attrs", {}) or {})
            work_1m = intraday_df.copy()

            if not isinstance(work_1m.index, pd.DatetimeIndex):
                work_1m.index = pd.to_datetime(work_1m.index, errors="coerce")
                work_1m = work_1m.loc[~work_1m.index.isna()].copy()

            if getattr(work_1m.index, "tz", None) is not None:
                work_1m.index = (
                    work_1m.index.tz_convert("Asia/Taipei").tz_localize(None)
                )

            work_1m = work_1m.sort_index()
            work_1m = work_1m.loc[work_1m.index.normalize() == today].copy()

            try:
                work_1m.attrs.update(intraday_attrs)
            except Exception:
                pass

            if not work_1m.empty:
                # 永豐 snapshot 已有快取時此步幾乎不增加耗時，並可補上
                # Yahoo 延遲區間至目前價。
                work_1m = _apply_shioaji_stock_realtime(work_1m, stock_id)
                live_attrs = dict(getattr(work_1m, "attrs", {}) or {})

                for col in ["Open", "High", "Low", "Close", "Volume"]:
                    if col not in work_1m.columns:
                        work_1m[col] = 0.0
                    work_1m[col] = pd.to_numeric(
                        work_1m[col],
                        errors="coerce",
                    )

                valid_close = work_1m["Close"].gt(0) & work_1m["Close"].notna()
                work_1m = work_1m.loc[valid_close].copy()

                if not work_1m.empty:
                    for col in ["Open", "High", "Low"]:
                        invalid = work_1m[col].isna() | work_1m[col].le(0)
                        work_1m.loc[invalid, col] = work_1m.loc[invalid, "Close"]

                    day_open = float(work_1m["Open"].iloc[0])
                    day_high = float(
                        work_1m[["Open", "High", "Close"]]
                        .max(axis=1)
                        .max()
                    )
                    day_low = float(
                        work_1m[["Open", "Low", "Close"]]
                        .min(axis=1)
                        .min()
                    )
                    day_close = float(work_1m["Close"].iloc[-1])
                    day_volume = float(
                        work_1m["Volume"].fillna(0).clip(lower=0).sum()
                    )

                    live_row = {
                        "Open": day_open,
                        "High": max(day_high, day_open, day_close),
                        "Low": min(day_low, day_open, day_close),
                        "Close": day_close,
                        "Volume": day_volume,
                    }
                    live_time = work_1m.index[-1]
                    live_source = (
                        "shioaji+yahoo_1m"
                        if str(
                            live_attrs.get("realtime_snapshot_source") or ""
                        ).lower()
                        == "shioaji"
                        else "yahoo_1m"
                    )

    except Exception as exc:
        print(
            "DEBUG live daily candle yahoo failed",
            "| version =", LIVE_DAILY_CANDLE_VERSION,
            "| stock_id =", stock_id,
            "| error =", repr(exc),
            flush=True,
        )

    # Yahoo 暫時無法建立今日 K 時，以永豐 snapshot 的當日 OHLC 備援。
    if live_row is None:
        snapshot = None

        try:
            allow_cold_login = (
                str(os.getenv("ALLOW_COLD_SHIOAJI_STOCK_APPEND", "0")).strip()
                == "1"
            )
            api_ready = bool(is_shioaji_api_ready())

            if api_ready or allow_cold_login:
                snapshot = get_shioaji_stock_snapshot(stock_id)
        except Exception:
            snapshot = None

        if isinstance(snapshot, dict):
            close_price = _snap_float(
                snapshot,
                "close",
                "price",
                "last_price",
                "last",
                "Close",
                default=0.0,
            )
            snap_time = _snap_timestamp(snapshot)

            try:
                if getattr(snap_time, "tzinfo", None) is not None:
                    snap_time = (
                        snap_time.tz_convert("Asia/Taipei").tz_localize(None)
                    )
            except Exception:
                pass

            if (
                close_price > 0
                and pd.Timestamp(snap_time).normalize() == today
            ):
                open_price = _snap_float(
                    snapshot,
                    "open",
                    "Open",
                    default=close_price,
                ) or close_price
                high_price = _snap_float(
                    snapshot,
                    "high",
                    "High",
                    default=close_price,
                ) or close_price
                low_price = _snap_float(
                    snapshot,
                    "low",
                    "Low",
                    default=close_price,
                ) or close_price
                total_volume = _snap_float(
                    snapshot,
                    "total_volume",
                    "volume",
                    default=0.0,
                )

                live_row = {
                    "Open": open_price,
                    "High": max(high_price, open_price, close_price),
                    "Low": min(low_price, open_price, close_price),
                    "Close": close_price,
                    "Volume": max(total_volume, 0.0),
                }
                live_time = pd.Timestamp(snap_time)
                live_source = "shioaji_snapshot"
                live_attrs = {
                    "realtime_snapshot_source": "shioaji",
                    "realtime_snapshot_price": close_price,
                    "realtime_snapshot_change": _snap_float(
                        snapshot,
                        "change",
                        "change_price",
                        "price_change",
                        default=0.0,
                    ),
                    "realtime_snapshot_change_pct": _snap_float(
                        snapshot,
                        "change_pct",
                        "change_rate",
                        "price_change_pct",
                        default=0.0,
                    ),
                    "realtime_snapshot_has_change": _snap_get(
                        snapshot,
                        "change",
                        "change_price",
                        "price_change",
                        default=None,
                    )
                    is not None,
                    "realtime_snapshot_has_change_pct": _snap_get(
                        snapshot,
                        "change_pct",
                        "change_rate",
                        "price_change_pct",
                        default=None,
                    )
                    is not None,
                    "realtime_snapshot_time": str(live_time),
                }

    if live_row is None or live_time is None:
        print(
            "DEBUG live daily candle skipped",
            "| version =", LIVE_DAILY_CANDLE_VERSION,
            "| stock_id =", stock_id,
            "| reason = no_today_intraday_or_snapshot",
            "| sec =", round(time_module.perf_counter() - t0, 3),
            flush=True,
        )
        return history_df

    try:
        attrs_backup = dict(getattr(history_df, "attrs", {}) or {})
        result = history_df.copy()

        if not isinstance(result.index, pd.DatetimeIndex):
            result.index = pd.to_datetime(result.index, errors="coerce")
            result = result.loc[~result.index.isna()].copy()

        if getattr(result.index, "tz", None) is not None:
            result.index = (
                result.index.tz_convert("Asia/Taipei").tz_localize(None)
            )

        result = result.sort_index()
        original_rows = len(result)

        # 若 FinMind 已經有今天的部分資料，保留更完整的高低價與成交量，
        # 但 Close 一律採用較新的盤中價。
        same_day = result.index.normalize() == today
        existing_today = result.loc[same_day].copy()

        if not existing_today.empty:
            existing_high = pd.to_numeric(
                existing_today.get("High"),
                errors="coerce",
            ).dropna()
            existing_low = pd.to_numeric(
                existing_today.get("Low"),
                errors="coerce",
            ).dropna()
            existing_volume = pd.to_numeric(
                existing_today.get("Volume"),
                errors="coerce",
            ).dropna()

            if not existing_high.empty:
                live_row["High"] = max(
                    float(live_row["High"]),
                    float(existing_high.max()),
                )
            if not existing_low.empty:
                positive_low = existing_low.loc[existing_low > 0]
                if not positive_low.empty:
                    live_row["Low"] = min(
                        float(live_row["Low"]),
                        float(positive_low.min()),
                    )
            if not existing_volume.empty:
                live_row["Volume"] = max(
                    float(live_row["Volume"]),
                    float(existing_volume.max()),
                )

        # 先刪除今天所有舊列，再以 00:00 的單一日K列寫回。
        result = result.loc[~same_day].copy()

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col not in result.columns:
                result[col] = 0.0

        result.loc[
            today,
            ["Open", "High", "Low", "Close", "Volume"],
        ] = [
            float(live_row["Open"]),
            float(live_row["High"]),
            float(live_row["Low"]),
            float(live_row["Close"]),
            float(live_row["Volume"]),
        ]

        if "_display_timestamp" in result.columns:
            result.loc[today, "_display_timestamp"] = today.strftime("%Y-%m-%d")

        result = result.sort_index()
        keep_rows = max(original_rows, 180)
        result = result.tail(keep_rows).copy()

        try:
            result.attrs.update(attrs_backup)
            result.attrs.update(live_attrs)
        except Exception:
            pass

        result.attrs["live_daily_candle_version"] = LIVE_DAILY_CANDLE_VERSION
        result.attrs["live_daily_candle_source"] = live_source
        result.attrs["live_daily_candle_time"] = str(live_time)
        result.attrs["display_timestamp"] = today.strftime("%Y-%m-%d")

        print(
            "DEBUG live daily candle upserted",
            "| version =", LIVE_DAILY_CANDLE_VERSION,
            "| stock_id =", stock_id,
            "| source =", live_source,
            "| time =", live_time,
            "| open =", live_row["Open"],
            "| high =", live_row["High"],
            "| low =", live_row["Low"],
            "| close =", live_row["Close"],
            "| volume =", live_row["Volume"],
            "| rows =", len(result),
            "| today_rows =",
            int((result.index.normalize() == today).sum()),
            "| sec =", round(time_module.perf_counter() - t0, 3),
            flush=True,
        )

        return result

    except Exception as exc:
        print(
            "DEBUG live daily candle upsert failed",
            "| version =", LIVE_DAILY_CANDLE_VERSION,
            "| stock_id =", stock_id,
            "| error =", repr(exc),
            "| sec =", round(time_module.perf_counter() - t0, 3),
            flush=True,
        )
        return history_df


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
    display_text: str | None = None,
    height: str = "52px",
    text_size: str = "md",
    corner_radius: str = "10px",
) -> dict[str, Any]:
    return build_postback_button(
        label=label,
        data=data,
        active=active,
        flex=flex,
        display_text=display_text,
        height=height,
        text_size=text_size,
        corner_radius=corner_radius,
    )


def _market_margin_scope_buttons(
    active_scope: str = "tse",
) -> dict[str, Any]:
    """
    大盤融資券市場切換列。

    使用獨立 action 而不是增加第 5 個 postback 欄位，
    可維持現有四欄 parser 相容性。
    """
    scope = str(active_scope or "tse").strip().lower()
    scope = "otc" if scope == "otc" else "tse"

    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "xs",
        "margin": "md",
        "contents": [
            _postback_button(
                label="上市",
                data="TAIEX,market_margin_tse,market_index,D",
                active=scope == "tse",
                display_text="大盤融資券 上市",
                height="42px",
                text_size="sm",
            ),
            _postback_button(
                label="上櫃",
                data="TAIEX,market_margin_otc,market_index,D",
                active=scope == "otc",
                display_text="大盤融資券 上櫃",
                height="42px",
                text_size="sm",
            ),
        ],
    }


def _market_index_buttons(active_action: str = "market_index") -> list[dict[str, Any]]:
    """
    大盤主功能列：
    現貨｜法人｜融資券｜預測｜期貨

    大盤期貨一律導向全盤。
    """
    active_action = _normalize_action(active_action or "market_index")

    row = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "xs",
        "margin": "md",
        "contents": [
            _postback_button(
                label="現貨",
                data="TAIEX,market_index,market_index,D",
                active=active_action == "market_index",
                display_text="大盤 現貨",
                height="50px",
                text_size="xs",
            ),
            _postback_button(
                label="法人",
                data="TAIEX,market_chip,market_index,D",
                active=active_action == "market_chip",
                display_text="大盤 法人",
                height="50px",
                text_size="xs",
            ),
            _postback_button(
                label="融資券",
                data="TAIEX,market_margin,market_index,D",
                active=active_action in {
                    "market_margin",
                    "market_margin_tse",
                    "market_margin_otc",
                },
                display_text="大盤 融資券",
                height="50px",
                text_size="xxs",
            ),
            _postback_button(
                label="預測",
                data="TAIEX,market_prediction,market_index,D",
                active=active_action == "market_prediction",
                display_text="大盤 15分鐘預測",
                height="50px",
                text_size="xxs",
            ),
            _postback_button(
                label="期貨",
                data="TAIEX,market_future_all,market_index,D",
                active=active_action in {"market_future_all", "market_future_k"},
                display_text="大盤 期貨",
                height="50px",
                text_size="xxs",
            ),
        ],
    }

    afterhours_row = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "xs",
        "margin": "xs",
        "contents": [
            _postback_button(
                label="盤後總覽",
                data="TAIEX,market_afterhours,market_index,D",
                active=active_action == "market_afterhours",
                display_text="大盤 盤後總覽",
                height="42px",
                text_size="sm",
            ),
        ],
    }

    return [row, afterhours_row]


def _build_market_prediction_shadow_flex(
    result: dict[str, Any],
) -> dict[str, Any]:
    """大盤未來15分鐘影子預測；只顯示測試結果，不宣稱交易訊號。"""

    def probability_text(value: Any) -> str:
        try:
            return f"{float(value) * 100:.1f}%"
        except Exception:
            return "--"

    def taipei_hm(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo("Asia/Taipei"))
            return parsed.strftime("%H:%M")
        except Exception:
            return text[-5:] if len(text) >= 5 else text

    if not bool(result.get("ok")):
        contents: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "大盤15分鐘預測",
                "weight": "bold",
                "size": "xl",
                "color": "#111827",
            },
            {
                "type": "text",
                "text": str(result.get("message") or "目前無法產生預測"),
                "size": "sm",
                "color": "#6B7280",
                "wrap": True,
                "margin": "md",
            },
            {
                "type": "text",
                "text": "模型測試中，非交易建議",
                "size": "xs",
                "color": "#9CA3AF",
                "wrap": True,
                "margin": "md",
            },
        ]
        contents.extend(_market_index_buttons("market_prediction"))
        return {
            "type": "flex",
            "altText": "大盤15分鐘預測暫時無法使用",
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

    release = result.get("release") or {}
    quality = result.get("release_quality") or {}
    effective_mode = str(
        release.get("effective_mode")
        or result.get("release_mode_effective")
        or "shadow"
    ).strip().lower()
    if effective_mode not in {"shadow", "beta", "public"}:
        effective_mode = "shadow"

    signal = str(result.get("signal") or "observe").strip().lower()
    if effective_mode == "shadow":
        trade_days = int(quality.get("trade_days") or 0)
        minimum_days = int(quality.get("minimum_shadow_days") or 20)
        signal_label = "資料收集中"
        signal_color = "#7C3AED"
        signal_note = "正在累積前瞻樣本，方向預測暫不公開"
        mode_badge = "影子測試"
    else:
        mode_badge = "內部測試" if effective_mode == "beta" else "模型觀測"
        signal_meta = {
            "up": (
                "偏多測試" if effective_mode == "beta" else "偏多觀察",
                UP_COLOR,
                "預測15分鐘後可能高於 +100點",
            ),
            "down": (
                "偏空測試" if effective_mode == "beta" else "偏空觀察",
                DOWN_COLOR,
                "預測15分鐘後可能低於 -100點",
            ),
            "observe": (
                "暫時觀察",
                FLAT_COLOR,
                "方向信心未達訊號門檻",
            ),
        }
        signal_label, signal_color, signal_note = signal_meta.get(
            signal,
            signal_meta["observe"],
        )
    close_value = result.get("taiex_close")
    try:
        close_text = f"{float(close_value):,.2f}"
    except Exception:
        close_text = "--"

    display_time = str(result.get("display_time") or "").strip()
    freshness = str(result.get("freshness_status") or "").strip()
    prediction_start = taipei_hm(result.get("prediction_ts"))
    if not prediction_start and display_time:
        prediction_start = display_time[-5:]
    prediction_end = taipei_hm(result.get("horizon_ts"))
    interval_text = (
        f"預測 {prediction_start} → {prediction_end}"
        if prediction_start and prediction_end
        else f"更新 {display_time[-5:]}"
        if display_time
        else ""
    )
    update_text = "｜".join(
        value
        for value in (
            interval_text,
            freshness,
        )
        if value
    )
    event_probability = result.get("event_probability")
    up_probability = result.get("up_probability")
    down_probability = result.get("down_probability")
    if down_probability is None:
        try:
            down_probability = 1.0 - float(up_probability)
        except Exception:
            down_probability = None

    thresholds = result.get("thresholds") or {}
    try:
        event_threshold = float(
            thresholds.get("event_probability_threshold", 0.45)
        )
    except Exception:
        event_threshold = 0.45
    try:
        direction_threshold = float(
            thresholds.get("direction_confidence_threshold", 0.60)
        )
    except Exception:
        direction_threshold = 0.60
    direction_confidence = result.get("direction_confidence")

    if effective_mode == "shadow":
        shadow_metrics = [
            (
                "交易日",
                f"{int(quality.get('trade_days') or 0)}"
                f"/{int(quality.get('minimum_shadow_days') or 20)}",
            ),
            ("已結算", f"{int(quality.get('settled_rows') or 0)}"),
            ("明確訊號", f"{int(quality.get('signal_rows') or 0)}"),
        ]
        remaining_days = max(minimum_days - trade_days, 0)
        definition_text = (
            f"至少還需 {remaining_days} 個交易日｜"
            "通過品質門檻前不公開方向"
            if remaining_days > 0
            else "交易日門檻已達｜仍須通過命中率與樣本品質檢查"
        )
        footer_note = "自動品質門檻保護中，非交易建議"
    else:
        left_metric_label = "波動逾100點機率"
        left_metric_value = probability_text(event_probability)
        right_metric_label = "若出現大波動"
        right_metric_value = (
            f"漲 {probability_text(up_probability)}"
            f"｜跌 {probability_text(down_probability)}"
        )
        if signal == "observe":
            observe_reasons: list[str] = []
            try:
                if float(event_probability) < event_threshold:
                    observe_reasons.append(
                        f"波動機率 {probability_text(event_probability)}"
                        f" 未達 {event_threshold * 100:.0f}%"
                    )
            except Exception:
                pass
            try:
                if float(direction_confidence) < direction_threshold:
                    observe_reasons.append(
                        f"方向信心 {probability_text(direction_confidence)}"
                        f" 未達 {direction_threshold * 100:.0f}%"
                    )
            except Exception:
                pass
            signal_note = (
                "暫不顯示方向｜" + "；".join(observe_reasons)
                if observe_reasons
                else "暫不顯示方向｜目前未通過訊號門檻"
            )
        definition_text = (
            "上漲 > +100點｜盤整 -100～+100點｜下跌 < -100點"
        )
        footer_note = (
            "內部測試中，非交易建議"
            if effective_mode == "beta"
            else "模型觀測，非交易建議"
        )

    contents = [
        {
            "type": "box",
            "layout": "horizontal",
            "alignItems": "center",
            "contents": [
                {
                    "type": "text",
                    "text": "大盤15分鐘預測",
                    "weight": "bold",
                    "size": "xl",
                    "color": "#111827",
                    "flex": 1,
                },
                {
                    "type": "text",
                    "text": mode_badge,
                    "size": "xs",
                    "weight": "bold",
                    "color": "#7C3AED",
                    "align": "end",
                    "flex": 0,
                },
            ],
        },
        {
            "type": "text",
            "text": close_text,
            "size": "xxl",
            "weight": "bold",
            "color": "#111827",
            "margin": "md",
        },
        {
            "type": "text",
            "text": signal_label,
            "size": "xl",
            "weight": "bold",
            "color": signal_color,
            "margin": "sm",
        },
        {
            "type": "text",
            "text": signal_note,
            "size": "sm",
            "color": signal_color,
            "wrap": True,
        },
        {
            "type": "text",
            "text": update_text or "更新時間未提供",
            "size": "xs",
            "color": "#6B7280",
            "margin": "sm",
        },
        {
            "type": "separator",
            "margin": "md",
        },
        (
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "xs",
                "margin": "md",
                "contents": [
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 1,
                        "backgroundColor": "#F3F4F6",
                        "cornerRadius": "10px",
                        "paddingAll": "8px",
                        "contents": [
                            {
                                "type": "text",
                                "text": metric_label,
                                "size": "xs",
                                "color": "#6B7280",
                                "align": "center",
                            },
                            {
                                "type": "text",
                                "text": metric_value,
                                "size": "lg",
                                "weight": "bold",
                                "color": "#111827",
                                "align": "center",
                                "margin": "sm",
                            },
                        ],
                    }
                    for metric_label, metric_value in shadow_metrics
                ],
            }
            if effective_mode == "shadow"
            else {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "md",
                "contents": [
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 1,
                        "backgroundColor": "#F3F4F6",
                        "cornerRadius": "10px",
                        "paddingAll": "10px",
                        "contents": [
                            {
                                "type": "text",
                                "text": left_metric_label,
                                "size": "xs",
                                "color": "#6B7280",
                                "align": "center",
                            },
                            {
                                "type": "text",
                                "text": left_metric_value,
                                "size": "lg",
                                "weight": "bold",
                                "color": "#111827",
                                "align": "center",
                                "margin": "sm",
                            },
                        ],
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 1,
                        "backgroundColor": "#F3F4F6",
                        "cornerRadius": "10px",
                        "paddingAll": "10px",
                        "contents": [
                            {
                                "type": "text",
                                "text": right_metric_label,
                                "size": "xs",
                                "color": "#6B7280",
                                "align": "center",
                            },
                            {
                                "type": "text",
                                "text": right_metric_value,
                                "size": "sm",
                                "weight": "bold",
                                "color": "#111827",
                                "align": "center",
                                "margin": "sm",
                            },
                        ],
                    },
                ],
            }
        ),
        {
            "type": "text",
            "text": definition_text,
            "size": "xs",
            "color": "#4B5563",
            "wrap": True,
            "margin": "md",
        },
        {
            "type": "text",
            "text": footer_note,
            "size": "xs",
            "color": "#9CA3AF",
            "wrap": True,
            "margin": "sm",
        },
    ]
    contents.extend(_market_index_buttons("market_prediction"))

    return {
        "type": "flex",
        "altText": f"大盤15分鐘預測：{signal_label}",
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


def _build_market_afterhours_digest_flex(
    result: dict[str, Any],
) -> dict[str, Any]:
    """大盤盤後總覽＋明日觀察；資料日期分開標示，避免混淆。"""

    def number(value: Any, decimals: int = 2) -> str:
        try:
            return f"{float(value):,.{decimals}f}"
        except Exception:
            return "--"

    def signed(value: Any, decimals: int = 2, suffix: str = "") -> str:
        try:
            return f"{float(value):+,.{decimals}f}{suffix}"
        except Exception:
            return "--"

    def value_color(value: Any) -> str:
        try:
            numeric = float(value)
        except Exception:
            return FLAT_COLOR
        if numeric > 0:
            return UP_COLOR
        if numeric < 0:
            return DOWN_COLOR
        return FLAT_COLOR

    def mmdd(value: Any) -> str:
        text = str(value or "").strip()
        if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
            return text[5:10].replace("-", "/")
        return text or "--"

    def info_row(
        label: str,
        value: str,
        color: str = "#111827",
        label_flex: int = 4,
    ) -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "sm",
                    "color": "#6B7280",
                    "flex": label_flex,
                },
                {
                    "type": "text",
                    "text": value,
                    "size": "sm",
                    "weight": "bold",
                    "color": color,
                    "align": "end",
                    "flex": 6,
                    "wrap": True,
                },
            ],
        }

    def mini_cell(
        label: str,
        value: str,
        color: str = "#111827",
    ) -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F3F4F6",
            "cornerRadius": "10px",
            "paddingAll": "9px",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "xs",
                    "color": "#6B7280",
                    "align": "center",
                },
                {
                    "type": "text",
                    "text": value,
                    "size": "sm",
                    "weight": "bold",
                    "color": color,
                    "align": "center",
                    "margin": "xs",
                    "wrap": True,
                },
            ],
        }

    def contribution_item(
        item: dict[str, Any],
        fallback_rank: int,
    ) -> dict[str, Any]:
        points = item.get("contribution_points")
        rank = item.get("rank") or fallback_rank
        stock_name = str(
            item.get("stock_name")
            or item.get("stock_id")
            or "--"
        )
        return {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": f"{rank}. {stock_name}",
                    "size": "sm",
                    "color": "#374151",
                    "flex": 7,
                    "maxLines": 1,
                },
                {
                    "type": "text",
                    "text": signed(points, 1, "點"),
                    "size": "sm",
                    "weight": "bold",
                    "color": value_color(points),
                    "align": "end",
                    "flex": 3,
                },
            ],
        }

    def contribution_card_contents(
        snapshot: dict[str, Any],
        market_label: str,
    ) -> list[dict[str, Any]]:
        available = bool(snapshot.get("available"))
        positive = [
            item
            for item in list(snapshot.get("positive") or [])[:5]
            if isinstance(item, dict)
        ]
        negative = [
            item
            for item in list(snapshot.get("negative") or [])[:5]
            if isinstance(item, dict)
        ]
        contents: list[dict[str, Any]] = [
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": f"盤後貢獻｜{market_label}",
                        "size": "xl",
                        "weight": "bold",
                        "color": "#111827",
                        "flex": 1,
                    },
                    {
                        "type": "text",
                        "text": "估算",
                        "size": "xs",
                        "weight": "bold",
                        "color": "#7C3AED",
                        "align": "end",
                        "flex": 0,
                    },
                ],
            },
            {
                "type": "text",
                "text": (
                    f"{snapshot.get('index_name') or market_label} "
                    f"{signed(snapshot.get('index_change_points'), 2, '點')}"
                    f"（{signed(snapshot.get('index_change_pct'), 2, '%')}）"
                    if available
                    else "尚未同步當日盤後貢獻"
                ),
                "size": "md",
                "weight": "bold",
                "color": (
                    value_color(snapshot.get("index_change_points"))
                    if available
                    else "#9CA3AF"
                ),
                "wrap": True,
                "margin": "sm",
            },
            {
                "type": "text",
                "text": f"資料日 {mmdd(snapshot.get('date'))}",
                "size": "xs",
                "color": "#6B7280",
            },
        ]
        if not available:
            contents.append(
                {
                    "type": "text",
                    "text": "請先執行每日盤後貢獻同步；本卡不使用盤中快照代替。",
                    "size": "sm",
                    "color": "#6B7280",
                    "wrap": True,
                    "margin": "lg",
                }
            )
            contents.extend(_market_index_buttons("market_afterhours"))
            return contents

        contents.extend(
            [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "xs",
                    "margin": "md",
                    "contents": [
                        mini_cell(
                            "拉抬合計",
                            signed(
                                snapshot.get(
                                    "positive_contribution_points"
                                ),
                                1,
                                "點",
                            ),
                            UP_COLOR,
                        ),
                        mini_cell(
                            "拖累合計",
                            signed(
                                snapshot.get(
                                    "negative_contribution_points"
                                ),
                                1,
                                "點",
                            ),
                            DOWN_COLOR,
                        ),
                    ],
                },
                {
                    "type": "text",
                    "text": "正貢獻｜拉抬前5名",
                    "size": "md",
                    "weight": "bold",
                    "color": UP_COLOR,
                    "margin": "md",
                },
                *[
                    contribution_item(item, rank)
                    for rank, item in enumerate(positive, start=1)
                ],
                {
                    "type": "separator",
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": "負貢獻｜拖累前5名",
                    "size": "md",
                    "weight": "bold",
                    "color": DOWN_COLOR,
                    "margin": "md",
                },
                *[
                    contribution_item(item, rank)
                    for rank, item in enumerate(negative, start=1)
                ],
                {
                    "type": "text",
                    "text": (
                        "依前日市值權重估算，並以官方指數當日漲跌校準；"
                        "個股點數非交易所發布的官方逐檔數值。"
                    ),
                    "size": "xs",
                    "color": "#9CA3AF",
                    "wrap": True,
                    "margin": "md",
                },
            ]
        )
        contents.extend(_market_index_buttons("market_afterhours"))
        return contents

    if not bool(result.get("ok")):
        contents: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "大盤盤後總覽",
                "weight": "bold",
                "size": "xl",
                "color": "#111827",
            },
            {
                "type": "text",
                "text": str(result.get("message") or "目前無法取得盤後資料"),
                "size": "sm",
                "color": "#6B7280",
                "wrap": True,
                "margin": "md",
            },
        ]
        contents.extend(_market_index_buttons("market_afterhours"))
        return {
            "type": "flex",
            "altText": "大盤盤後總覽暫時無法使用",
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

    market = result.get("market") or {}
    future = result.get("future") or {}
    chip = result.get("chip") or {}
    margin = result.get("margin") or {}
    margin_tse = margin.get("tse") or {}
    margin_otc = margin.get("otc") or {}
    contribution = result.get("contribution") or {}
    tse_contribution = contribution.get("tse") or {}
    otc_contribution = contribution.get("otc") or {}
    tse_largest_negative = (
        tse_contribution.get("largest_negative") or {}
    )
    otc_largest_negative = (
        otc_contribution.get("largest_negative") or {}
    )
    trade_date = str(result.get("trade_date") or "")
    data_mode = str(result.get("data_mode") or "收盤資料")
    change = market.get("change")
    change_pct = market.get("change_pct")
    close_color = value_color(change)

    summary_contents: list[dict[str, Any]] = [
        {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "text",
                    "text": "台股盤後總覽",
                    "size": "xl",
                    "weight": "bold",
                    "color": "#111827",
                    "flex": 1,
                },
                {
                    "type": "text",
                    "text": f"{mmdd(trade_date)}｜{data_mode}",
                    "size": "sm",
                    "weight": "bold",
                    "color": (
                        "#E0A800"
                        if data_mode == "盤中暫估"
                        else "#6B7280"
                    ),
                    "align": "end",
                    "flex": 0,
                },
            ],
        },
        {
            "type": "text",
            "text": number(market.get("close")),
            "size": "xxl",
            "weight": "bold",
            "color": close_color,
            "margin": "md",
        },
        {
            "type": "text",
            "text": (
                f"{signed(change)}（{signed(change_pct, 2, '%')}）"
            ),
            "size": "md",
            "weight": "bold",
            "color": close_color,
        },
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "xs",
            "margin": "md",
            "contents": [
                mini_cell("開盤", number(market.get("open"))),
                mini_cell("最高", number(market.get("high"))),
                mini_cell("最低", number(market.get("low"))),
            ],
        },
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "xs",
            "margin": "xs",
            "contents": [
                mini_cell(
                    "日內振幅",
                    f"{number(market.get('range_points'))}點",
                ),
                mini_cell(
                    "收盤位置",
                    f"{number(market.get('close_position_pct'), 1)}%",
                ),
                mini_cell(
                    "成交金額",
                    f"{number(market.get('turnover_yi'), 0)}億",
                ),
            ],
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": "收盤結構",
            "size": "md",
            "weight": "bold",
            "color": "#374151",
            "margin": "md",
        },
        info_row(
            str(future.get("phase") or "台指期"),
            (
                f"{number(future.get('price'))}"
                f"｜價差 {signed(future.get('basis_points'), 0)}點"
            )
            if future.get("available")
            else "資料待更新",
            value_color(future.get("basis_points")),
        ),
        info_row(
            f"三大法人 {mmdd(chip.get('date'))}",
            (
                signed(chip.get("total_yi"), 1, "億")
                if chip.get("available")
                else "資料待更新"
            ),
            value_color(chip.get("total_yi")),
        ),
        info_row(
            f"上市融資 {mmdd(margin_tse.get('date'))}",
            (
                signed(margin_tse.get("money_change_yi"), 1, "億")
                if margin_tse.get("available")
                else "資料待更新"
            ),
            value_color(margin_tse.get("money_change_yi")),
        ),
        info_row(
            f"上櫃融資 {mmdd(margin_otc.get('date'))}",
            (
                signed(margin_otc.get("money_change_yi"), 1, "億")
                if margin_otc.get("available")
                else "資料待更新"
            ),
            value_color(margin_otc.get("money_change_yi")),
        ),
        info_row(
            f"上市最大拖累 {mmdd(tse_contribution.get('date'))}",
            (
                (
                    f"{tse_largest_negative.get('stock_name') or '--'} "
                    f"{signed(tse_largest_negative.get('contribution_points'), 1, '點')}"
                )
                if tse_contribution.get("available")
                else "資料待更新"
            ),
            value_color(
                tse_largest_negative.get("contribution_points")
            ),
        ),
        info_row(
            f"上櫃最大拖累 {mmdd(otc_contribution.get('date'))}",
            (
                (
                    f"{otc_largest_negative.get('stock_name') or '--'} "
                    f"{signed(otc_largest_negative.get('contribution_points'), 2, '點')}"
                )
                if otc_contribution.get("available")
                else "資料待更新"
            ),
            value_color(
                otc_largest_negative.get("contribution_points")
            ),
        ),
        {
            "type": "text",
            "text": str(result.get("note") or ""),
            "size": "xs",
            "color": "#9CA3AF",
            "wrap": True,
            "margin": "md",
        },
    ]
    summary_contents.extend(_market_index_buttons("market_afterhours"))

    status_contents: list[dict[str, Any]] = []
    for item in list(result.get("data_status") or []):
        if not isinstance(item, dict):
            continue
        available = bool(item.get("available"))
        status_contents.append(
            info_row(
                str(item.get("label") or "資料"),
                (
                    f"已更新 {mmdd(item.get('date'))}"
                    if available
                    else "待更新"
                ),
                "#374151" if available else "#9CA3AF",
            )
        )

    observation_contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "明日觀察",
            "size": "xl",
            "weight": "bold",
            "color": "#111827",
        },
        {
            "type": "text",
            "text": str(
                market.get("close_position_label")
                or "收盤區間資料不足"
            ),
            "size": "md",
            "weight": "bold",
            "color": close_color,
            "margin": "sm",
        },
        {
            "type": "text",
            "text": (
                "以下為今日高低與 Pivot 區間參考，"
                "不是明日漲跌預測。"
            ),
            "size": "xs",
            "color": "#6B7280",
            "wrap": True,
        },
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "xs",
            "margin": "md",
            "contents": [
                mini_cell(
                    "上方參考",
                    number(market.get("resistance_1")),
                    UP_COLOR,
                ),
                mini_cell(
                    "中軸 Pivot",
                    number(market.get("pivot")),
                    "#E0A800",
                ),
                mini_cell(
                    "下方參考",
                    number(market.get("support_1")),
                    DOWN_COLOR,
                ),
            ],
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": "籌碼觀察",
            "size": "md",
            "weight": "bold",
            "color": "#374151",
            "margin": "md",
        },
        info_row(
            "外資",
            (
                signed(chip.get("foreign_yi"), 1, "億")
                if chip.get("available")
                else "待更新"
            ),
            value_color(chip.get("foreign_yi")),
        ),
        info_row(
            "投信",
            (
                signed(chip.get("trust_yi"), 1, "億")
                if chip.get("available")
                else "待更新"
            ),
            value_color(chip.get("trust_yi")),
        ),
        info_row(
            "上市融券增減",
            (
                signed(margin_tse.get("short_change"), 0, "張")
                if margin_tse.get("available")
                else "待更新"
            ),
            value_color(margin_tse.get("short_change")),
        ),
        info_row(
            "上櫃融券增減",
            (
                signed(margin_otc.get("short_change"), 0, "張")
                if margin_otc.get("available")
                else "待更新"
            ),
            value_color(margin_otc.get("short_change")),
        ),
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": "資料完整度",
            "size": "md",
            "weight": "bold",
            "color": "#374151",
            "margin": "md",
        },
        *status_contents,
        {
            "type": "text",
            "text": "各來源更新日不同，圖卡已分別標示日期。",
            "size": "xs",
            "color": "#9CA3AF",
            "wrap": True,
            "margin": "md",
        },
    ]
    tse_contribution_contents = contribution_card_contents(
        tse_contribution,
        "上市",
    )
    otc_contribution_contents = contribution_card_contents(
        otc_contribution,
        "上櫃",
    )

    return {
        "type": "flex",
        "altText": f"大盤盤後總覽 {trade_date}",
        "contents": {
            "type": "carousel",
            "contents": [
                {
                    "type": "bubble",
                    "size": "mega",
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "sm",
                        "contents": summary_contents,
                    },
                },
                {
                    "type": "bubble",
                    "size": "mega",
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "sm",
                        "contents": tse_contribution_contents,
                    },
                },
                {
                    "type": "bubble",
                    "size": "mega",
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "sm",
                        "contents": otc_contribution_contents,
                    },
                },
                {
                    "type": "bubble",
                    "size": "mega",
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "sm",
                        "contents": observation_contents,
                    },
                },
            ],
        },
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

        # 1分 / 5分 預設走即時圖
        # 日 / 週 / 月 預設走 K 線圖
        if value in {"1m", "5m"}:
            target_action = "instant"
        else:
            target_action = "k_line"

        buttons.append(
            _postback_button(
                label=label,
                data=f"{stock_id},{target_action},{target_action},{value}",
                active=is_active,
                display_text=f"{stock_id} {label}",
                height="46px",
                text_size="md",
                corner_radius="12px",
            )
        )

    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "md",
        "contents": buttons,
    }


def _post_market_mode_buttons(stock_id: str, active_mode: str) -> dict[str, Any]:
    mode = _normalize_action(active_mode)
    items = [
        ("短線5日", "post_market_short"),
        ("隔日沖", "post_market_daytrade"),
    ]
    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "md",
        "contents": [
            _postback_button(
                label=label,
                data=f"{stock_id},{action_name},{action_name},D",
                active=mode == action_name,
                display_text=f"{stock_id} 盤後分析 {label}",
                height="40px",
                text_size="sm",
                corner_radius="12px",
            )
            for label, action_name in items
        ],
    }


def _post_market_method_button(stock_id: str) -> dict[str, Any]:
    return {
        "type": "box",
        "layout": "horizontal",
        "margin": "sm",
        "contents": [
            _postback_button(
                label="查看計算方式",
                data=(
                    f"{stock_id},post_market_method,"
                    "post_market_method,D"
                ),
                active=False,
                display_text=f"{stock_id} 盤後分析計算方式",
                height="36px",
                text_size="sm",
                corner_radius="10px",
            )
        ],
    }


def _mode_buttons(stock_id: str, active_mode: str, current_tf: str) -> list[dict[str, Any]]:
    mode = _normalize_action(active_mode)
    tf = normalize_time_frame(current_tf)

    rows = [
        [
            ("即時", "instant"),
            ("K線", "k_line"),
            ("法人", "chip"),
            ("大戶", "large_holder"),
        ],
        [
            ("融資券", "margin"),
            ("財務", "financial"),
            ("期貨", "futures"),
            ("盤後分析", "post_market_short"),
        ],
        [
            ("主力進出", "main_force"),
            ("族群比較", "peer_compare"),
        ],
    ]

    def _target_tf_for(action_name: str) -> str:
        if action_name == "instant":
            return tf if tf in {"1m", "5m"} else "1m"

        if action_name == "k_line":
            return tf if tf in {"D", "W", "M"} else "D"

        if action_name in {"post_market_short", "post_market_daytrade"}:
            return "D"

        return tf

    output: list[dict[str, Any]] = []

    for row_idx, row_items in enumerate(rows):
        buttons = []

        for label, action_name in row_items:
            is_active = (
                mode in {"post_market_short", "post_market_daytrade"}
                if action_name == "post_market_short"
                else mode == action_name
            )
            target_tf = _target_tf_for(action_name)

            buttons.append(
                _postback_button(
                    label=label,
                    data=f"{stock_id},{action_name},{action_name},{target_tf}",
                    active=is_active,
                    display_text=f"{stock_id} {label}",
                    height="25px",
                    text_size=(
                        "xxs"
                        if label
                        in {
                            "融資券",
                            "盤後分析",
                            "主力進出",
                            "族群比較",
                        }
                        else "xs"
                    ),
                    corner_radius="8px",
                )
            )

        output.append(
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "xs",
                "margin": "md" if row_idx == 0 else "xs",
                "contents": buttons,
            }
        )

    return output

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

    market_update_text, market_update_color = _fresh_update_display(
        str(getattr(snapshot, "quote_time", "") or "--"),
        active_mode="instant",
        current_tf="1m",
        price_source=str(getattr(snapshot, "quote_source", "") or ""),
    )

    rows = [
        ("資料", getattr(snapshot, "quote_source", "永豐即時"), "#888888"),
        ("更新", market_update_text, market_update_color),
        ("開", _fmt_market_price(getattr(snapshot, "open_price", 0.0)), "#222222"),
        ("高", _fmt_market_price(getattr(snapshot, "high_price", 0.0)), "#222222"),
        ("低", _fmt_market_price(getattr(snapshot, "low_price", 0.0)), "#222222"),
        ("收", close_text, change_color),
        ("漲", change_text, change_color),
        ("成交金額(億)", _fmt_market_int(getattr(snapshot, "total_volume", 0)), "#222222"),
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

def _market_future_nav_buttons(active_action: str = "market_future_all") -> list[dict[str, Any]]:
    """
    台指期頁面第一排：
    現貨｜法人｜融資券｜預測｜全盤
    """
    active_action = _normalize_action(active_action or "market_future_all")

    row = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "xs",
        "margin": "md",
        "contents": [
            _postback_button(
                label="現貨",
                data="TAIEX,market_index,market_index,D",
                active=active_action == "market_index",
                display_text="大盤 現貨",
                height="50px",
                text_size="xxs",
            ),
            _postback_button(
                label="法人",
                data="TAIEX,market_chip,market_index,D",
                active=active_action == "market_chip",
                display_text="大盤 法人",
                height="50px",
                text_size="xxs",
            ),
            _postback_button(
                label="融資券",
                data="TAIEX,market_margin,market_index,D",
                active=active_action in {
                    "market_margin",
                    "market_margin_tse",
                    "market_margin_otc",
                },
                display_text="大盤 融資券",
                height="50px",
                text_size="xxs",
            ),
            _postback_button(
                label="預測",
                data="TAIEX,market_prediction,market_index,D",
                active=active_action == "market_prediction",
                display_text="大盤 15分鐘預測",
                height="50px",
                text_size="xxs",
            ),
            _postback_button(
                label="全盤",
                data="TAIEX,market_future_all,market_index,D",
                active=active_action == "market_future_all",
                display_text="台指期 全盤",
                height="50px",
                text_size="xxs",
            ),
        ],
    }

    afterhours_row = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "xs",
        "margin": "xs",
        "contents": [
            _postback_button(
                label="盤後總覽",
                data="TAIEX,market_afterhours,market_index,D",
                active=active_action == "market_afterhours",
                display_text="大盤 盤後總覽",
                height="42px",
                text_size="sm",
            ),
        ],
    }

    return [row, afterhours_row]


def _market_future_kline_tf_buttons(active_tf: str = "1m") -> list[dict[str, Any]]:
    """
    台指期 K 線週期列。
    第二階段會接真正 K 線圖。
    """
    tf = str(active_tf or "1m").strip()

    items = [
        ("1分", "1m"),
        ("5分", "5m"),
        ("15分", "15m"),
        ("30分", "30m"),
        ("60分", "60m"),
    ]

    row = {
        "type": "box",
        "layout": "horizontal",
        "spacing": "xs",
        "margin": "xs",
        "contents": [
            _postback_button(
                label=label,
                data=f"TAIEX,market_future_k,market_index,{value}",
                active=tf == value,
                display_text=f"台指期 {label}K",
                height="42px",
                text_size="xxs",
            )
            for label, value in items
        ],
    }

    return [row]


# 保留舊函式名稱，避免其他地方尚未改到時失敗。
def _market_future_session_buttons(active_action: str = "market_future_all") -> list[dict[str, Any]]:
    return _market_future_nav_buttons(active_action)

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
            "text": "之後這裡會顯示：期貨價、漲跌、漲跌幅、開、高、低、、更新時間。",
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

    contents.extend(_market_future_nav_buttons(action))
    contents.extend(_market_future_kline_tf_buttons("1m"))

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

def _build_market_future_kline_flex(snapshot, active_tf: str = "1m") -> dict[str, Any]:
    tf = str(active_tf or "1m").strip()

    label_map = {
        "1m": "1分",
        "5m": "5分",
        "15m": "15分",
        "30m": "30分",
        "60m": "60分",
    }

    label = label_map.get(tf, tf)
    kline_update_text, _kline_update_color = _fresh_update_display(
        str(getattr(snapshot, "latest_time", "") or "--"),
        active_mode="k_line",
        current_tf=tf,
        price_source="shioaji",
    )

    def _info_row(label_text: str, value_text: str) -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": label_text,
                    "size": "sm",
                    "color": "#888888",
                    "flex": 4,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": str(value_text),
                    "size": "sm",
                    "color": "#222222",
                    "weight": "bold",
                    "flex": 6,
                    "align": "end",
                    "wrap": True,
                },
            ],
        }

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "台指期 K線",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"TXF 近月｜全盤｜{label}K",
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "sm",
            "wrap": True,
        },
    ]

    if getattr(snapshot, "available", False):
        image_url = str(getattr(snapshot, "image_url", "") or "").strip()

        if image_url:
            contents.append(
                {
                    "type": "image",
                    "url": image_url,
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
                        _info_row("契約", getattr(snapshot, "contract_code", "TXFR1")),
                        _info_row("更新", kline_update_text),
                        _info_row("K棒數", str(getattr(snapshot, "rows", 0) or 0)),
                    ],
                },
                {
                    "type": "text",
                    "text": "布林通道：20期中線 ± 2倍標準差；數值顯示於圖上方。",
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

    else:
        contents.extend(
            [
                {
                    "type": "separator",
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": getattr(snapshot, "message", "台指期 K 線暫時查無資料。"),
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

    contents.extend(_market_future_nav_buttons("market_future_k"))
    contents.extend(_market_future_kline_tf_buttons(tf))

    return {
        "type": "flex",
        "altText": f"台指期 {label}K",
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

def _build_market_future_kline_placeholder_flex(active_tf: str = "1m") -> dict[str, Any]:
    """
    台指期 K 線第二階段佔位卡。
    先讓按鈕可點、不報錯；下一步接布林通道 K 線圖。
    """
    tf = str(active_tf or "1m").strip()

    label_map = {
        "1m": "1分",
        "5m": "5分",
        "15m": "15分",
        "30m": "30分",
        "60m": "60分",
    }

    label = label_map.get(tf, tf)

    contents: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "台指期 K線",
            "size": "xxl",
            "weight": "bold",
            "color": "#111111",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"TXF 近月｜全盤｜{label}K",
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "sm",
            "wrap": True,
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": "布林通道 K 線圖建置中。下一步會接 120 根 K 棒、BB上/中/下。",
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

    contents.extend(_market_future_nav_buttons("market_future_k"))
    contents.extend(_market_future_kline_tf_buttons(tf))

    return {
        "type": "flex",
        "altText": f"台指期 {label}K",
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
    - 現貨、次月期貨、開高低、買賣、更新時間整理成資訊卡。
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

    action = _normalize_action(action or "market_future_all")
    session_text = "全盤"

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

        contents.extend(_market_future_nav_buttons(action))
        contents.extend(_market_future_kline_tf_buttons("1m"))

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
    total_volume = _to_float(
        getattr(snapshot, "total_volume", None)
        if getattr(snapshot, "total_volume", None) is not None
        else getattr(snapshot, "volume", 0)
    )

    next_future_price = _to_float(getattr(snapshot, "next_future_price", 0.0))
    next_future_change = _to_float(getattr(snapshot, "next_future_change", 0.0))
    next_future_change_pct = _to_float(getattr(snapshot, "next_future_change_pct", 0.0))
    next_contract_code = str(getattr(snapshot, "next_contract_code", "") or "").strip()

    next_future_color = _calc_color(next_future_change)
    next_future_text = _fmt_market_price(next_future_price) if next_future_price > 0 else "--"

    if next_future_price > 0 and (next_future_change != 0 or next_future_change_pct != 0):
        next_future_sub_text = f"{next_contract_code}｜{_fmt_signed(next_future_change)} ({_fmt_signed_pct(next_future_change_pct)})"
    elif next_contract_code:
        next_future_sub_text = next_contract_code
    else:
        next_future_sub_text = ""

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
    future_update_text, future_update_color = _fresh_update_display(
        quote_time,
        active_mode="instant",
        current_tf="1m",
        price_source=quote_source,
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
    contents.extend(_market_future_nav_buttons(action))
    contents.extend(_market_future_kline_tf_buttons("1m"))

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
                    _metric_box(
                        "次月期貨",
                        next_future_text,
                        next_future_sub_text,
                        next_future_color if next_future_price > 0 else "#111111",
                    ),
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
                    _info_row("更新", future_update_text, future_update_color),
                ],
            },
            {
                "type": "text",
                "text": "期現價差＝台指期近月 − 加權指數現貨。台指期以全盤即時資料顯示。",
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


def _parse_card_update_time(value: str):
    """將圖卡更新時間解析為台北時間的 naive datetime。"""
    text = str(value or "").strip()

    if not text or text in {"--", "None", "nan"}:
        return None, False

    has_clock = ":" in text
    normalized = text.replace("/", "-")

    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m-%d %H:%M:%S",
        "%m-%d %H:%M",
    ]

    for fmt in candidates:
        try:
            parsed = datetime.strptime(normalized, fmt)

            # 沒有年份的格式，補上台北當年。
            if fmt.startswith("%m"):
                parsed = parsed.replace(
                    year=datetime.now(ZoneInfo("Asia/Taipei")).year
                )

            return parsed, has_clock
        except Exception:
            continue

    try:
        import pandas as pd

        parsed = pd.to_datetime(normalized, errors="coerce")
        if pd.isna(parsed):
            return None, has_clock

        if getattr(parsed, "tzinfo", None) is not None:
            parsed = parsed.tz_convert("Asia/Taipei").tz_localize(None)

        return parsed.to_pydatetime(), has_clock
    except Exception:
        return None, has_clock


def _short_card_update_time(value: str) -> str:
    text = str(value or "--").strip()
    parsed, has_clock = _parse_card_update_time(text)

    if parsed is None:
        return text

    now_tpe = datetime.now(ZoneInfo("Asia/Taipei")).replace(tzinfo=None)

    if has_clock:
        if parsed.date() == now_tpe.date():
            return parsed.strftime("%H:%M")
        return parsed.strftime("%m/%d %H:%M")

    return parsed.strftime("%m/%d")


def _stock_card_freshness(
    update_time: str,
    active_mode: str,
    current_tf: str,
    price_source: str = "",
) -> tuple[str, str]:
    """
    回傳 (狀態文字, 顏色)。

    規則：
    - 盤中資料 0~2 分鐘：即時
    - 3~10 分鐘：稍有延遲
    - 超過 10 分鐘：延遲行情
    - 收盤後同日且最後資料接近收盤：收盤資料
    - 只有日期、沒有時間：收盤資料
    """
    parsed, has_clock = _parse_card_update_time(update_time)
    now_tpe = datetime.now(ZoneInfo("Asia/Taipei")).replace(tzinfo=None)
    tf = normalize_time_frame(current_tf)
    mode = _normalize_action(active_mode)
    source = str(price_source or "").strip().lower()

    colors = {
        "即時": "#16A34A",
        "稍有延遲": "#D97706",
        "延遲行情": "#DC2626",
        "收盤資料": "#6B7280",
        "前一交易日": "#6B7280",
        "時間未知": "#9CA3AF",
    }

    if parsed is None:
        return "時間未知", colors["時間未知"]

    intraday_context = mode == "instant" or tf in INTRADAY_TIME_FRAMES

    # 日／週／月歷史資料通常只有日期，不應假裝是即時資料。
    if not has_clock:
        if parsed.date() < now_tpe.date():
            return "收盤資料", colors["收盤資料"]
        return "收盤資料", colors["收盤資料"]

    # 不是今日資料，明確標示為前一交易日。
    if parsed.date() < now_tpe.date():
        return "前一交易日", colors["前一交易日"]

    # 未來時間通常是時區誤差，當作 0 分鐘處理。
    age_minutes = max(0.0, (now_tpe - parsed).total_seconds() / 60.0)
    now_clock = now_tpe.time()
    update_clock = parsed.time()

    # 台股收盤後，若同日最後資料已接近收盤，顯示收盤資料而非延遲。
    if now_clock > time(13, 35) and update_clock >= time(13, 20):
        return "收盤資料", colors["收盤資料"]

    # 非盤中模式但有盤中時間，仍可依新鮮度標示。
    if age_minutes <= 2.0:
        return "即時", colors["即時"]

    if age_minutes <= 10.0:
        return "稍有延遲", colors["稍有延遲"]

    # Yahoo 的盤中資料常有延遲；來源資訊只用於 log，不硬改時間判斷。
    if intraday_context or source in {"yahoo_direct", "unified_1m_base"}:
        return "延遲行情", colors["延遲行情"]

    return "延遲行情", colors["延遲行情"]


def _fresh_update_display(
    update_time: str,
    active_mode: str = "instant",
    current_tf: str = "1m",
    price_source: str = "",
) -> tuple[str, str]:
    """統一回傳圖卡更新欄位，例如「13:30｜即時」及對應顏色。"""
    raw_text = str(update_time or "--").strip()
    short_text = _short_card_update_time(raw_text)
    freshness_text, freshness_color = _stock_card_freshness(
        update_time=raw_text,
        active_mode=active_mode,
        current_tf=current_tf,
        price_source=price_source,
    )
    return f"{short_text}｜{freshness_text}", freshness_color


def _build_post_market_method_bubble(
    stock_id: str,
    stock_name: str,
) -> dict[str, Any]:
    """盤後分析第三張說明卡：公開算法、缺口規則與限制。"""

    def section(
        title: str,
        lines: list[str],
        background: str,
        title_color: str,
    ) -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": background,
            "cornerRadius": "12px",
            "paddingAll": "12px",
            "margin": "md",
            "spacing": "xs",
            "contents": [
                {
                    "type": "text",
                    "text": title,
                    "size": "md",
                    "weight": "bold",
                    "color": title_color,
                    "wrap": True,
                },
                *[
                    {
                        "type": "text",
                        "text": f"• {line}",
                        "size": "xs",
                        "color": "#374151",
                        "wrap": True,
                    }
                    for line in lines
                ],
            ],
        }

    body_contents: list[dict[str, Any]] = [
        {
            "type": "box",
            "layout": "horizontal",
            "alignItems": "center",
            "contents": [
                {
                    "type": "text",
                    "text": f"{stock_id} {stock_name}",
                    "size": "xxl",
                    "weight": "bold",
                    "color": "#111111",
                    "wrap": True,
                    "flex": 7,
                },
                {
                    "type": "text",
                    "text": "說明",
                    "size": "sm",
                    "weight": "bold",
                    "color": "#666666",
                    "align": "end",
                    "flex": 2,
                },
            ],
        },
        {
            "type": "text",
            "text": "盤後分析｜計算方式",
            "size": "lg",
            "weight": "bold",
            "color": "#444444",
            "margin": "xs",
        },
        {
            "type": "text",
            "text": "支撐壓力沒有唯一算法；以下為本機器人的固定規則。",
            "size": "xs",
            "color": "#6B7280",
            "wrap": True,
            "margin": "sm",
        },
        section(
            "短線5日｜混合支撐壓力",
            [
                "近60日轉折＋5／10／20／40日高低與均價",
                "高量日典型價＋近20日未回補缺口",
                "ATR14決定一般區間寬度",
            ],
            "#F1F7FF",
            "#2563EB",
        ),
        section(
            "隔日沖｜Classic Pivot",
            [
                "P＝（高＋低＋收）÷3",
                "R1＝2P－低｜S1＝2P－高",
                "R2＝P＋（高－低）",
                "S2＝P－（高－低）",
                "ATR5決定區間寬度",
            ],
            "#FFF4F1",
            "#DC2626",
        ),
        section(
            "跳空缺口規則",
            [
                "向上跳空列支撐；向下跳空列壓力",
                "部分回補縮小；完全回補自動移除",
                "只取距離現價較近的前兩區",
            ],
            "#F2FBF6",
            "#009B4D",
        ),
        {
            "type": "text",
            "text": (
                "圖中「來源・強弱」為主要形成依據；強弱不代表一定守住或突破。"
                "除權息價格重置仍可能造成缺口誤判。"
            ),
            "size": "xs",
            "color": "#4B5563",
            "wrap": True,
            "margin": "md",
        },
        {
            "type": "text",
            "text": "盤後技術觀察，非目標價與交易建議。",
            "size": "xs",
            "color": "#9CA3AF",
            "wrap": True,
            "margin": "sm",
        },
    ]
    body_contents.extend(_mode_buttons(stock_id, "post_market_short", "D"))

    return {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "spacing": "sm",
            "contents": body_contents,
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
    image_aspect_ratio: str = "6:5",
    price_source: str = "",
    show_period_buttons: bool = True,
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
        "post_market": "盤後分析｜短線5日",
        "post_market_short": "盤後分析｜短線5日",
        "post_market_daytrade": "盤後分析｜隔日沖",
        "post_market_fibonacci": "盤後分析｜黃金切割",
    }

    mode_title = mode_title_map.get(active_mode_norm, "個股觀測")
    if active_mode_norm == "post_market_short":
        context_badge = "短線 1/2"
    elif active_mode_norm == "post_market_daytrade":
        context_badge = "隔日 2/2"
    elif active_mode_norm ==  "post_market_fibonacci":
        context_badge = "黃金切割"    
    else:
        context_badge = card_context_badge(active_mode_norm, tf_norm)

    update_text = str(update_time or "--").strip()
    update_short = _short_card_update_time(update_text)
    freshness_text, freshness_color = _stock_card_freshness(
        update_time=update_text,
        active_mode=active_mode_norm,
        current_tf=tf_norm,
        price_source=price_source,
    )

    print(
        "DEBUG stock card freshness",
        "| version =", MARKET_DATA_FRESHNESS_VERSION,
        "| stock_id =", stock_id,
        "| mode =", active_mode_norm,
        "| tf =", tf_norm,
        "| source =", price_source,
        "| update_time =", update_text,
        "| status =", freshness_text,
        "| flex_version =", STOCK_FLEX_RESILIENT_VERSION,
        flush=True,
    )

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
                    "text": context_badge,
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
                    "text": f"更新 {update_short}｜{freshness_text}",
                    "size": "xs",
                    "weight": "bold",
                    "color": freshness_color,
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
    ]

    if show_period_buttons:
        body_contents.extend(
            [
                {
                    "type": "separator",
                    "margin": "md",
                },
                (
                    _post_market_mode_buttons(stock_id, active_mode_norm)
                    if active_mode_norm in {"post_market_short", "post_market_daytrade"}
                    else _time_buttons(stock_id, active_mode_norm, tf_norm)
                ),
            ]
        )

    if image_url:
        body_contents.append(
            {
                "type": "image",
                "url": image_url,
                "size": "full",
                "aspectRatio": image_aspect_ratio,
                "aspectMode": "fit",
                "margin": (
                    "sm"
                    if active_mode_norm in {
                        "post_market_short",
                        "post_market_daytrade",
                    }
                    else "md"
                ),
                "backgroundColor": "#FFFFFF",
            }
        )
        body_contents.append(
            build_chart_reload_hint(
                stock_id=stock_id,
                active_mode=active_mode_norm,
                current_tf=tf_norm,
            )
        )
        if active_mode_norm in {
            "post_market_short",
            "post_market_daytrade",
        }:
            body_contents.append(
                _post_market_method_button(stock_id)
            )
    else:
        body_contents.append(
            build_chart_fallback(
                stock_id=stock_id,
                active_mode=active_mode_norm,
                current_tf=tf_norm,
            )
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


def _large_holder_threshold_from_action(action: str | None) -> int:
    text = str(action or "").strip().lower()

    for threshold in [200, 400, 600, 800, 1000]:
        if str(threshold) in text:
            return threshold

    return 1000


def _large_holder_threshold_label(threshold: int) -> str:
    try:
        threshold = int(threshold)
    except Exception:
        threshold = 1000

    return f"{threshold}張以上"


def _large_holder_threshold_buttons(
    stock_id: str,
    active_threshold: int,
    current_tf: str = "D",
) -> dict[str, Any]:
    try:
        active_threshold = int(active_threshold)
    except Exception:
        active_threshold = 1000

    tf = normalize_time_frame(current_tf)

    buttons = []

    for threshold in [200, 400, 600, 800, 1000]:
        action_name = f"large_holder_{threshold}"
        is_active = active_threshold == threshold

        buttons.append(
            {
                "type": "box",
                "layout": "vertical",
                "height": "36px",
                "cornerRadius": "8px",
                "backgroundColor": ACTIVE_COLOR if is_active else INACTIVE_COLOR,
                "justifyContent": "center",
                "alignItems": "center",
                "action": {
                    "type": "postback",
                    "label": str(threshold),
                    "data": f"{stock_id},{action_name},{action_name},{tf}",
                    "displayText": f"{stock_id} 大戶{threshold}",
                },
                "contents": [
                    {
                        "type": "text",
                        "text": str(threshold),
                        "size": "xs",
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
        "spacing": "xs",
        "margin": "md",
        "contents": buttons,
    }


def _build_large_holder_flex(
    stock_id: str,
    stock_name: str,
    rows,
    current_tf: str = "D",
    threshold: int = 1000,
):
    """
    顯示個股大戶持股近 5 週。
    欄位：
    日期 | 大戶人數 | 持股比 | 增減
    """
    try:
        threshold = int(threshold)
    except Exception:
        threshold = 1000

    if threshold not in {200, 400, 600, 800, 1000}:
        threshold = 1000

    threshold_label = _large_holder_threshold_label(threshold)
    raw_rows = list(rows or [])

    print(
        "DEBUG large_holder flex",
        "| stock_id =",
        stock_id,
        "| threshold =",
        threshold,
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
                    "text": f"大戶持股｜{threshold_label}",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#444444",
                    "margin": "sm",
                    "wrap": True,
                },
                _large_holder_threshold_buttons(stock_id, threshold, current_tf),
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": f"目前查無{threshold_label}大戶持股資料。",
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
                    "text": f"大戶持股｜{threshold_label}｜最新 {latest.get('date', '--')}",
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
                            f"{threshold_label}人數",
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
                _large_holder_threshold_buttons(stock_id, threshold, current_tf),
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


def _financial_tint(value) -> str:
    num = _financial_float(value)

    if num > 0:
        return "#FFF3F3"

    if num < 0:
        return "#F0FBF5"

    return "#F8F9FA"


def _pe_zone_style(zone_label: str) -> tuple[str, str]:
    zone = str(zone_label or "").strip()

    if zone in {"低估區", "偏低區", "合理偏低"}:
        return "#EAF8F0", "#079455"

    if zone in {"偏高區", "高估區", "極高區"}:
        return "#FFF1F1", "#D92D20"

    return "#FFF9E8", "#B77900"


def _financial_metric_box(
    title: str,
    value: str,
    sub_value: str = "",
    value_color: str = "#111111",
    background_color: str = "#F8F9FA",
    value_size: str = "lg",
    flex: int = 1,
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
            "size": value_size,
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
        "flex": flex,
        "backgroundColor": background_color,
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
            cell(quarter, 3, align="start"),
            cell(eps, 2),
            cell(eps_change, 2, change_color if not is_header else text_color),
            cell(ttm_eps, 2),
            cell(pe, 2),
        ],
    }



def _pe_river_button_row(stock_id: str, active: bool = False) -> dict[str, Any]:
    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "md",
        "contents": [
            _postback_button(
                label="財務",
                data=f"{stock_id},financial,financial,D",
                active=not active,
                display_text=f"{stock_id} 財務",
            ),
            _postback_button(
                label="河流圖",
                data=f"{stock_id},pe_river,financial,D",
                active=active,
                display_text=f"{stock_id} 河流圖",
            ),
        ],
    }


def _fmt_pe_river_value(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except Exception:
        return "--"


def _build_pe_river_flex(
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
            "text": "本益比河流圖",
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
                    "text": getattr(snapshot, "message", "目前無法產生本益比河流圖。"),
                    "size": "sm",
                    "color": "#666666",
                    "margin": "md",
                    "wrap": True,
                },
                _pe_river_button_row(stock_id, active=True),
                {"type": "separator", "margin": "md"},
            ]
        )
        contents.append(_pe_river_button_row(stock_id, active=False))
        contents.extend(_mode_buttons(stock_id, "financial", current_tf))

        return {
            "type": "flex",
            "altText": f"{stock_id} {stock_name} 本益比河流圖",
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

    current_pe = float(getattr(snapshot, "current_pe", 0.0) or 0.0)
    median_pe = float(getattr(snapshot, "median_pe", 0.0) or 0.0)
    latest_close = float(getattr(snapshot, "latest_close", 0.0) or 0.0)
    latest_ttm_eps = float(getattr(snapshot, "latest_ttm_eps", 0.0) or 0.0)
    latest_date = str(getattr(snapshot, "latest_date", "") or "--")
    zone_label = str(getattr(snapshot, "zone_label", "") or "--")
    levels = list(getattr(snapshot, "pe_levels", []) or [])

    level_text_top = "--"
    level_text_bottom = ""

    if len(levels) >= 6:
        level_text_top = (
            f"10% {levels[0]:.1f}｜25% {levels[1]:.1f}｜"
            f"40% {levels[2]:.1f}"
        )
        level_text_bottom = (
            f"60% {levels[3]:.1f}｜75% {levels[4]:.1f}｜"
            f"90% {levels[5]:.1f}"
        )

    zone_bg, zone_color = _pe_zone_style(zone_label)

    contents.extend(
        [
            {
                "type": "text",
                "text": "估值摘要",
                "size": "sm",
                "weight": "bold",
                "color": "#344054",
                "margin": "md",
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "xs",
                "margin": "sm",
                "contents": [
                    _financial_metric_box(
                        "目前 PE",
                        f"{_fmt_pe_river_value(current_pe)} 倍",
                        f"股價 {_fmt_pe_river_value(latest_close)}",
                        background_color="#F5F8FF",
                        value_size="md",
                    ),
                    _financial_metric_box(
                        "中位 PE",
                        (
                            f"{_fmt_pe_river_value(median_pe)} 倍"
                            if median_pe > 0
                            else "--"
                        ),
                        "歷史 50%",
                        background_color="#F5F8FF",
                        value_size="md",
                    ),
                    _financial_metric_box(
                        "估值位置",
                        zone_label,
                        f"TTM EPS {_fmt_pe_river_value(latest_ttm_eps)}",
                        value_color=zone_color,
                        background_color=zone_bg,
                        value_size="md",
                    ),
                ],
            },
            {
                "type": "text",
                "text": f"資料日期 {latest_date}",
                "size": "xs",
                "color": "#777777",
                "wrap": True,
                "margin": "sm",
            },
        ]
    )

    chart_url = str(getattr(snapshot, "chart_url", "") or "").strip()

    if chart_url:
        contents.append(
            {
                "type": "image",
                "url": chart_url,
                "size": "full",
                "aspectRatio": "7:5",
                "aspectMode": "fit",
                "margin": "md",
            }
        )

    contents.extend(
        [
            {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#F8FAFC",
                "cornerRadius": "10px",
                "paddingAll": "8px",
                "margin": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "歷史 PE 分位",
                        "size": "xs",
                        "weight": "bold",
                        "color": "#475467",
                    },
                    {
                        "type": "text",
                        "text": level_text_top,
                        "size": "xs",
                        "color": "#667085",
                        "wrap": True,
                        "margin": "xs",
                    },
                    *(
                        [
                            {
                                "type": "text",
                                "text": level_text_bottom,
                                "size": "xs",
                                "color": "#667085",
                                "wrap": True,
                                "margin": "xs",
                            }
                        ]
                        if level_text_bottom
                        else []
                    ),
                ],
            },
            {
                "type": "text",
                "text": "河流區間＝歷史 PE 分位數 × 當期近四季 EPS；EPS 資料可能落後公告，僅作估值區間觀察。",
                "size": "xs",
                "color": "#888888",
                "wrap": True,
                "margin": "sm",
            },
            _pe_river_button_row(stock_id, active=True),
            {"type": "separator", "margin": "md"},
        ]
    )

    contents.extend(_mode_buttons(stock_id, "financial", current_tf))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 本益比河流圖",
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
                "type": "text",
                "text": "核心估值",
                "size": "sm",
                "weight": "bold",
                "color": "#344054",
                "margin": "md",
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "sm",
                "contents": [
                    _financial_metric_box(
                        "近四季 EPS",
                        _financial_fmt(latest_ttm_eps),
                        getattr(snapshot, "latest_quarter", ""),
                        background_color="#F5F8FF",
                    ),
                    _financial_metric_box(
                        "目前本益比",
                        f"{_financial_fmt(current_pe)} 倍" if current_pe > 0 else "--",
                        f"股價 {_financial_fmt(current_price)}",
                        background_color="#F5F8FF",
                    ),
                ],
            },
            {
                "type": "text",
                "text": "最新季度",
                "size": "sm",
                "weight": "bold",
                "color": "#344054",
                "margin": "md",
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
                        getattr(snapshot, "latest_quarter", ""),
                    ),
                    _financial_metric_box(
                        "EPS QoQ",
                        _financial_signed(eps_change),
                        "較前一季",
                        value_color=_financial_color(eps_change),
                        background_color=_financial_tint(eps_change),
                    ),
                ],
            },
            {"type": "separator", "margin": "md"},
            {
                "type": "text",
                "text": "近8季明細",
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
            _pe_river_button_row(stock_id, active=False),
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
                "flex": 4,
                "align": "end",
                "wrap": True,
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

    latest_margin_usage = _margin_get(
        latest,
        "margin_usage_rate",
        "margin_usage",
        "financing_usage_rate",
        "融資使用率",
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
            "layout": "horizontal",
            "spacing": "sm",
            "margin": "sm",
            "contents": [
                _margin_metric_box(
                    "券資比",
                    _margin_fmt_ratio(latest_ratio),
                ),
                _margin_metric_box(
                    "融資使用率",
                    _margin_fmt_ratio(latest_margin_usage),
                ),
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
                    "融資使用率",
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
                                "margin_usage_rate",
                                "margin_usage",
                                "financing_usage_rate",
                                "融資使用率",
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
                display_text=f"{stock_id} 期貨日盤",
            ),
            _postback_button(
                label="全盤",
                data=f"{stock_id},futures_all,futures,{current_tf}",
                active=active_session == "all",
                display_text=f"{stock_id} 期貨全盤",
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
    "market_prediction",
    "market_afterhours",
    "market_chip",
    "market_margin",
    "market_margin_tse",
    "market_margin_otc",
    "market_future_day",
    "market_future_all",
    "market_future_k",
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

        if "15分預測" in text or "15分鐘預測" in text:
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
    stock_future_update_text, stock_future_update_color = _fresh_update_display(
        quote_time or "--",
        active_mode="instant",
        current_tf=current_tf,
        price_source=quote_source,
    )

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

    contents[-1]["contents"].append(
        _info_row("更新", stock_future_update_text, stock_future_update_color)
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


def _build_main_force_flex(
    stock_id: str,
    stock_name: str,
    snapshot,
    current_tf: str = "D",
) -> dict[str, Any]:
    rows = list(getattr(snapshot, "rows", []) or [])
    latest = rows[0] if rows else {}
    latest_date = str(
        getattr(snapshot, "latest_date", "")
        or latest.get("trade_date")
        or "--"
    )
    status_label = str(
        getattr(snapshot, "status_label", "")
        or "暫無判讀"
    )
    status_key = str(
        getattr(snapshot, "status_key", "")
        or "unknown"
    )

    status_style = {
        "buy": ("#B91C1C", "#FEE2E2"),
        "sell": ("#047857", "#D1FAE5"),
        "divergence": ("#B45309", "#FEF3C7"),
        "neutral": ("#6B7280", "#F3F4F6"),
        "unknown": ("#6B7280", "#F3F4F6"),
    }
    status_color, status_background = status_style.get(
        status_key,
        status_style["unknown"],
    )

    def number(value) -> float | None:
        if value is None:
            return None
        try:
            return float(str(value).replace(",", "").replace("%", "").strip())
        except Exception:
            return None

    def signed_integer(value, suffix: str = "") -> str:
        parsed = number(value)
        if parsed is None:
            return "--"
        text = f"{parsed:+,.0f}"
        return f"{text}{suffix}"

    def plain_price(value) -> str:
        parsed = number(value)
        if parsed is None:
            return "--"
        return f"{parsed:,.2f}"

    def percentage(value, signed: bool = True) -> str:
        parsed = number(value)
        if parsed is None:
            return "--"
        return f"{parsed:+.2f}%" if signed else f"{parsed:.2f}%"

    def value_color(value) -> str:
        parsed = number(value)
        if parsed is None or parsed == 0:
            return "#6B7280"
        return "#DC2626" if parsed > 0 else "#059669"

    def short_date(value) -> str:
        text = str(value or "--").strip()
        if len(text) >= 10 and text[4:5] in {"-", "/"}:
            return text[5:10].replace("-", "/")
        return text

    def badge() -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": status_background,
            "cornerRadius": "12px",
            "paddingAll": "7px",
            "contents": [
                {
                    "type": "text",
                    "text": status_label,
                    "size": "xs",
                    "weight": "bold",
                    "color": status_color,
                    "align": "center",
                    "wrap": True,
                }
            ],
        }

    def metric_box(
        label: str,
        value_text: str,
        color: str = "#111827",
    ) -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F8FAFC",
            "cornerRadius": "12px",
            "paddingAll": "10px",
            "flex": 1,
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "xs",
                    "color": "#6B7280",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": value_text,
                    "size": "md",
                    "weight": "bold",
                    "color": color,
                    "margin": "xs",
                    "wrap": True,
                },
            ],
        }

    def table_cell(
        text: str,
        flex: int,
        *,
        color: str = "#111827",
        align: str = "end",
        weight: str = "regular",
    ) -> dict[str, Any]:
        return {
            "type": "text",
            "text": str(text),
            "size": "xs",
            "color": color,
            "align": align,
            "weight": weight,
            "flex": flex,
            "wrap": False,
        }

    def table_row(row: dict[str, Any], is_header: bool = False) -> dict[str, Any]:
        if is_header:
            values = ("日期", "收盤價", "買賣超", "5日集中")
            colors = ("#4B5563",) * 4
        else:
            net_value = row.get("net_buy_sell")
            concentration_value = row.get("concentration_5d")
            values = (
                short_date(row.get("trade_date")),
                plain_price(row.get("close_price")),
                signed_integer(net_value),
                percentage(concentration_value),
            )
            colors = (
                "#374151",
                "#111827",
                value_color(net_value),
                value_color(concentration_value),
            )

        return {
            "type": "box",
            "layout": "horizontal",
            "backgroundColor": "#EEF2F7" if is_header else "#FFFFFF",
            "cornerRadius": "7px" if is_header else "0px",
            "paddingAll": "5px",
            "contents": [
                table_cell(
                    values[0],
                    3,
                    color=colors[0],
                    align="start",
                    weight="bold" if is_header else "regular",
                ),
                table_cell(
                    values[1],
                    3,
                    color=colors[1],
                    weight="bold" if is_header else "regular",
                ),
                table_cell(
                    values[2],
                    4,
                    color=colors[2],
                    weight="bold",
                ),
                table_cell(
                    values[3],
                    4,
                    color=colors[3],
                    weight="bold",
                ),
            ],
        }

    available = bool(getattr(snapshot, "available", False)) and bool(rows)
    contents: list[dict[str, Any]] = [
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "alignItems": "center",
            "contents": [
                {
                    "type": "text",
                    "text": f"{stock_id} {stock_name}",
                    "size": "xxl",
                    "weight": "bold",
                    "color": "#111827",
                    "flex": 1,
                    "wrap": True,
                },
                badge(),
            ],
        },
        {
            "type": "text",
            "text": "主力進出",
            "size": "lg",
            "weight": "bold",
            "color": "#374151",
            "margin": "sm",
        },
        {
            "type": "text",
            "text": f"最新日期：{latest_date}",
            "size": "sm",
            "color": "#6B7280",
            "margin": "xs",
        },
    ]

    if available:
        net_buy_sell = latest.get("net_buy_sell")
        count_diff = latest.get("broker_count_diff")
        concentration_5d = latest.get("concentration_5d")
        concentration_20d = latest.get("concentration_20d")

        contents.extend(
            [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "margin": "md",
                    "contents": [
                        metric_box(
                            "今日主力買賣超",
                            signed_integer(net_buy_sell, " 張"),
                            value_color(net_buy_sell),
                        ),
                        metric_box(
                            "家數差",
                            (
                                signed_integer(count_diff, " 家")
                                if number(count_diff) is not None
                                else "來源未提供"
                            ),
                            (
                                value_color(count_diff)
                                if number(count_diff) is not None
                                else "#6B7280"
                            ),
                        ),
                    ],
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "margin": "sm",
                    "contents": [
                        metric_box(
                            "5日集中度",
                            percentage(concentration_5d),
                            value_color(concentration_5d),
                        ),
                        metric_box(
                            "20日集中度",
                            percentage(concentration_20d),
                            value_color(concentration_20d),
                        ),
                    ],
                },
                {
                    "type": "separator",
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": "最近10日",
                    "size": "md",
                    "weight": "bold",
                    "color": "#374151",
                    "margin": "md",
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "none",
                    "margin": "sm",
                    "paddingAll": "5px",
                    "backgroundColor": "#F8FAFC",
                    "cornerRadius": "10px",
                    "contents": [
                        table_row({}, is_header=True),
                        *[table_row(row) for row in rows[:10]],
                    ],
                },
                {
                    "type": "text",
                    "text": (
                        "判讀：今日買賣超與5日集中度同為正＝偏買；"
                        "同為負＝偏賣；方向不同＝分歧。"
                    ),
                    "size": "xs",
                    "color": "#6B7280",
                    "wrap": True,
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": (
                        "計算：主力買賣超＝前15大買超合計－前15大賣超合計；"
                        "集中度＝近5／20日主力買賣超合計÷同期成交量合計。"
                    ),
                    "size": "xs",
                    "color": "#6B7280",
                    "wrap": True,
                    "margin": "xs",
                },
                {
                    "type": "text",
                    "text": (
                        "資料來源：富邦 eBroker｜家數差來源未提供；"
                        "盤後資料，僅供參考"
                    ),
                    "size": "xs",
                    "color": "#9CA3AF",
                    "wrap": True,
                    "margin": "xs",
                },
            ]
        )
    else:
        contents.extend(
            [
                {
                    "type": "separator",
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": str(
                        getattr(snapshot, "message", "")
                        or "目前查無主力進出資料。"
                    ),
                    "size": "sm",
                    "color": "#6B7280",
                    "wrap": True,
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": "資料來源：富邦 eBroker",
                    "size": "xs",
                    "color": "#9CA3AF",
                    "margin": "md",
                },
            ]
        )

    contents.extend(_mode_buttons(stock_id, "main_force", current_tf))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 主力進出",
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


def _build_concept_peer_flex(
    result: dict[str, Any],
    stock_id: str,
    stock_name: str,
    current_tf: str,
) -> dict[str, Any]:
    """人工主分類優先、理財網輔助的族群比較。"""

    def number(value: Any, digits: int = 2) -> str:
        try:
            return f"{float(value):,.{digits}f}"
        except Exception:
            return "--"

    def signed_pct(value: Any, digits: int = 1) -> str:
        try:
            return f"{float(value):+,.{digits}f}%"
        except Exception:
            return "--"

    def ratio_text(value: Any) -> str:
        try:
            numeric = float(value)
            if abs(numeric) < 1:
                return f"{numeric:.3f}"
            if abs(numeric) < 10:
                return f"{numeric:.2f}"
            return f"{numeric:,.1f}"
        except Exception:
            return "--"

    def value_color(value: Any) -> str:
        try:
            numeric = float(value)
            if numeric > 0:
                return UP_COLOR
            if numeric < 0:
                return DOWN_COLOR
        except Exception:
            pass
        return FLAT_COLOR

    def metric_box(
        label: str,
        value: str,
        value_color_hex: str = "#111827",
    ) -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "vertical",
            "flex": 1,
            "backgroundColor": "#F5F6F8",
            "cornerRadius": "12px",
            "paddingAll": "9px",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "xxs",
                    "color": "#6B7280",
                    "align": "center",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": value,
                    "size": "md",
                    "weight": "bold",
                    "color": value_color_hex,
                    "align": "center",
                    "margin": "xs",
                    "wrap": True,
                },
            ],
        }

    def context_badge() -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "vertical",
            "flex": 0,
            "backgroundColor": "#F3E8FF",
            "cornerRadius": "8px",
            "paddingAll": "6px",
            "contents": [
                {
                    "type": "text",
                    "text": "族群",
                    "size": "xs",
                    "weight": "bold",
                    "color": "#7C3AED",
                    "align": "center",
                }
            ],
        }

    def info_row(
        label: str,
        value: str,
        color: str = "#374151",
    ) -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "sm",
                    "color": "#6B7280",
                    "flex": 4,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": value,
                    "size": "sm",
                    "weight": "bold",
                    "color": color,
                    "flex": 6,
                    "align": "end",
                    "wrap": True,
                },
            ],
        }

    def compact_number(value: Any, digits: int = 2) -> str:
        try:
            numeric = float(value)
            if abs(numeric) >= 1000:
                return f"{numeric:,.0f}"
            if abs(numeric) >= 100:
                return f"{numeric:,.1f}"
            return f"{numeric:,.{digits}f}"
        except Exception:
            return "--"

    def compact_signed(value: Any) -> str:
        try:
            numeric = float(value)
            if abs(numeric) >= 100:
                return f"{numeric:+,.0f}"
            return f"{numeric:+,.2f}"
        except Exception:
            return "--"

    def compact_volume(value: Any) -> str:
        try:
            lots = max(float(value), 0.0)
            if lots >= 10000:
                return f"{lots / 10000:.1f}萬"
            if lots >= 1000:
                return f"{lots:,.0f}"
            return f"{lots:,.0f}"
        except Exception:
            return "--"

    def build_group_trend_bubble(
        trend: dict[str, Any],
    ) -> dict[str, Any]:
        trend_rows = list(trend.get("rows") or [])
        up_count = sum(
            1
            for row in trend_rows
            if float(row.get("change") or 0.0) > 0
        )
        down_count = sum(
            1
            for row in trend_rows
            if float(row.get("change") or 0.0) < 0
        )
        flat_count = max(len(trend_rows) - up_count - down_count, 0)

        table_rows: list[dict[str, Any]] = [
            {
                "type": "box",
                "layout": "horizontal",
                "backgroundColor": "#F3F4F6",
                "cornerRadius": "8px",
                "paddingAll": "6px",
                "contents": [
                    {
                        "type": "text",
                        "text": "代號／股名",
                        "size": "xxs",
                        "weight": "bold",
                        "color": "#6B7280",
                        "flex": 5,
                    },
                    {
                        "type": "text",
                        "text": "日K",
                        "size": "xxs",
                        "weight": "bold",
                        "color": "#6B7280",
                        "align": "end",
                        "flex": 2,
                    },
                    {
                        "type": "text",
                        "text": "漲跌",
                        "size": "xxs",
                        "weight": "bold",
                        "color": "#6B7280",
                        "align": "end",
                        "flex": 2,
                    },
                    {
                        "type": "text",
                        "text": "漲幅",
                        "size": "xxs",
                        "weight": "bold",
                        "color": "#6B7280",
                        "align": "end",
                        "flex": 2,
                    },
                    {
                        "type": "text",
                        "text": "總量",
                        "size": "xxs",
                        "weight": "bold",
                        "color": "#6B7280",
                        "align": "end",
                        "flex": 2,
                    },
                ],
            }
        ]
        for row in trend_rows:
            change_value = float(row.get("change") or 0.0)
            change_pct = float(row.get("change_pct") or 0.0)
            change_color = (
                UP_COLOR
                if change_value > 0
                else DOWN_COLOR if change_value < 0 else FLAT_COLOR
            )
            is_target = bool(row.get("is_target"))
            table_rows.append(
                {
                    "type": "box",
                    "layout": "horizontal",
                    "backgroundColor": (
                        "#F5F3FF" if is_target else "#FFFFFF"
                    ),
                    "cornerRadius": "7px",
                    "paddingAll": "6px",
                    "margin": "xs",
                    "contents": [
                        {
                            "type": "text",
                            "text": (
                                f"{row.get('stock_id') or '--'} "
                                f"{row.get('stock_name') or ''}"
                            ),
                            "size": "xxs",
                            "weight": "bold" if is_target else "regular",
                            "color": (
                                "#7C3AED" if is_target else "#111827"
                            ),
                            "flex": 5,
                            "wrap": False,
                        },
                        {
                            "type": "text",
                            "text": compact_number(row.get("price")),
                            "size": "xxs",
                            "color": "#111827",
                            "align": "end",
                            "flex": 2,
                        },
                        {
                            "type": "text",
                            "text": compact_signed(change_value),
                            "size": "xxs",
                            "color": change_color,
                            "align": "end",
                            "flex": 2,
                        },
                        {
                            "type": "text",
                            "text": f"{change_pct:+.2f}%",
                            "size": "xxs",
                            "color": change_color,
                            "align": "end",
                            "flex": 2,
                        },
                        {
                            "type": "text",
                            "text": compact_volume(
                                row.get("volume_lots")
                            ),
                            "size": "xxs",
                            "color": "#4B5563",
                            "align": "end",
                            "flex": 2,
                        },
                    ],
                }
            )

        contents: list[dict[str, Any]] = [
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{stock_id} {stock_name}",
                        "size": "xxl",
                        "weight": "bold",
                        "color": "#111827",
                        "flex": 1,
                        "wrap": True,
                    },
                    context_badge(),
                ],
            },
            {
                "type": "text",
                "text": "族群趨勢",
                "size": "lg",
                "weight": "bold",
                "color": "#374151",
                "margin": "sm",
            },
            {
                "type": "text",
                "text": str(trend.get("group_name") or "主分類"),
                "size": "md",
                "weight": "bold",
                "color": "#7C3AED",
                "wrap": True,
                "margin": "xs",
            },
            {
                "type": "box",
                "layout": "horizontal",
                "backgroundColor": "#F9FAFB",
                "cornerRadius": "9px",
                "paddingAll": "7px",
                "margin": "sm",
                "contents": [
                    {
                        "type": "text",
                        "text": f"上漲 {up_count}",
                        "size": "xs",
                        "weight": "bold",
                        "color": UP_COLOR,
                        "align": "center",
                        "flex": 1,
                    },
                    {
                        "type": "text",
                        "text": f"下跌 {down_count}",
                        "size": "xs",
                        "weight": "bold",
                        "color": DOWN_COLOR,
                        "align": "center",
                        "flex": 1,
                    },
                    {
                        "type": "text",
                        "text": f"平盤 {flat_count}",
                        "size": "xs",
                        "weight": "bold",
                        "color": FLAT_COLOR,
                        "align": "center",
                        "flex": 1,
                    },
                ],
            },
            {
                "type": "box",
                "layout": "vertical",
                "spacing": "none",
                "margin": "md",
                "contents": table_rows,
            },
            {
                "type": "text",
                "text": (
                    f"資料日 {trend.get('data_date') or '--'}｜"
                    "依漲跌%由高到低｜總量單位：張"
                ),
                "size": "xxs",
                "color": "#9CA3AF",
                "align": "center",
                "wrap": True,
                "margin": "md",
            },
            {
                "type": "text",
                "text": (
                    "族群趨勢列出主分類全部股票，不套用70%相似度門檻；"
                    "價格、漲跌與總量均取最新日K。"
                ),
                "size": "xs",
                "color": "#6B7280",
                "align": "center",
                "wrap": True,
                "margin": "xs",
            },
        ]
        contents.extend(_mode_buttons(stock_id, "peer_compare", current_tf))
        return {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "14px",
                "spacing": "sm",
                "contents": contents,
            },
        }

    comparisons = list(result.get("comparisons") or [])
    group_trends = list(result.get("group_trends") or [])
    bubbles: list[dict[str, Any]] = [
        build_group_trend_bubble(trend)
        for trend in group_trends
        if trend.get("rows")
    ]
    if not bool(result.get("available")) or not comparisons:
        contents: list[dict[str, Any]] = [
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{stock_id} {stock_name}",
                        "size": "xxl",
                        "weight": "bold",
                        "color": "#111827",
                        "flex": 1,
                        "wrap": True,
                    },
                    context_badge(),
                ],
            },
            {
                "type": "text",
                "text": "族群比較",
                "size": "lg",
                "weight": "bold",
                "color": "#374151",
                "margin": "sm",
            },
            {
                "type": "text",
                "text": str(
                    result.get("message")
                    or "目前沒有足夠的同族群資料可比較"
                ),
                "size": "sm",
                "color": "#6B7280",
                "wrap": True,
                "margin": "md",
            },
            {
                "type": "text",
                "text": (
                    "分類以人工整理主清單為準，理財網概念僅作補充；"
                    "未收錄不代表公司沒有相關題材。"
                ),
                "size": "xs",
                "color": "#9CA3AF",
                "wrap": True,
                "margin": "md",
            },
        ]
        contents.extend(_mode_buttons(stock_id, "peer_compare", current_tf))
        unavailable_bubble = {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "14px",
                "spacing": "sm",
                "contents": contents,
            },
        }
        bubbles.append(unavailable_bubble)
        flex_contents: dict[str, Any]
        if len(bubbles) == 1:
            flex_contents = bubbles[0]
        else:
            flex_contents = {
                "type": "carousel",
                "contents": bubbles,
            }
        return {
            "type": "flex",
            "altText": f"{stock_id} {stock_name} 族群趨勢與比較",
            "contents": flex_contents,
        }

    methodology = result.get("methodology") or {}
    window_days = int(methodology.get("window_days") or 240)
    minimum_similarity_pct = float(
        methodology.get("minimum_similarity_pct")
        or result.get("minimum_similarity_pct")
        or 70
    )
    for comparison in comparisons:
        peer_id = str(comparison.get("peer_id") or "")
        peer_name = str(comparison.get("peer_name") or peer_id)
        concept_text = "、".join(
            str(value)
            for value in (comparison.get("concepts") or [])
            if value
        ) or "概念族群"
        similarity = float(
            comparison.get("similarity_pct") or 0.0
        )
        similarity_color = (
            "#7C3AED"
            if similarity >= 80
            else (
                "#2563EB"
                if similarity >= minimum_similarity_pct
                else "#6B7280"
            )
        )
        relative_5 = comparison.get("relative_strength_5_pct")
        relative_20 = comparison.get("relative_strength_20_pct")
        chart_url = str(comparison.get("chart_url") or "").strip()
        contents: list[dict[str, Any]] = [
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{stock_id} {stock_name}",
                        "size": "xxl",
                        "weight": "bold",
                        "color": "#111827",
                        "flex": 1,
                        "wrap": True,
                    },
                    context_badge(),
                ],
            },
            {
                "type": "text",
                "text": f"族群比較｜{concept_text}",
                "size": "lg",
                "weight": "bold",
                "color": "#374151",
                "margin": "sm",
                "wrap": True,
            },
            {
                "type": "text",
                "text": f"與 {peer_id} {peer_name} 進行比較",
                "size": "md",
                "weight": "bold",
                "color": "#111827",
                "margin": "sm",
                "wrap": True,
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "xs",
                "margin": "md",
                "contents": [
                    metric_box(
                        "相似度",
                        f"{number(similarity, 0)}%",
                        similarity_color,
                    ),
                    metric_box(
                        "目前比值",
                        ratio_text(comparison.get("ratio_current")),
                    ),
                    metric_box(
                        f"{window_days}日均值",
                        ratio_text(comparison.get("ratio_mean")),
                    ),
                ],
            },
        ]
        if chart_url:
            contents.append(
                {
                    "type": "image",
                    "url": chart_url,
                    "size": "full",
                    "aspectRatio": "9:4",
                    "aspectMode": "fit",
                    "margin": "md",
                    "backgroundColor": "#FFFFFF",
                }
            )
        contents.extend(
            [
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "margin": "md",
                    "contents": [
                        info_row(
                            "比值偏離均值",
                            signed_pct(
                                comparison.get("ratio_deviation_pct")
                            ),
                            value_color(
                                comparison.get("ratio_deviation_pct")
                            ),
                        ),
                        info_row(
                            "比值 Z-score",
                            number(
                                comparison.get("ratio_zscore"),
                                2,
                            ),
                            value_color(
                                comparison.get("ratio_zscore")
                            ),
                        ),
                        info_row(
                            "近5日相對強弱",
                            signed_pct(relative_5),
                            value_color(relative_5),
                        ),
                        info_row(
                            "近20日相對強弱",
                            signed_pct(relative_20),
                            value_color(relative_20),
                        ),
                        info_row(
                            "日報酬相關",
                            number(
                                comparison.get("return_correlation"),
                                2,
                            ),
                        ),
                        info_row(
                            "同向交易日",
                            (
                                f"{number(comparison.get('direction_agreement_pct'), 0)}%"
                            ),
                        ),
                    ],
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#F5F3FF",
                    "cornerRadius": "10px",
                    "paddingAll": "9px",
                    "margin": "md",
                    "contents": [
                        {
                            "type": "text",
                            "text": str(
                                comparison.get("status")
                                or "比值觀察中"
                            ),
                            "size": "sm",
                            "weight": "bold",
                            "color": similarity_color,
                            "align": "center",
                            "wrap": True,
                        }
                    ],
                },
                {
                    "type": "text",
                    "text": (
                        f"資料日 {comparison.get('data_date') or '--'}｜"
                        f"共同樣本 {comparison.get('sample_days') or 0} 日"
                    ),
                    "size": "xs",
                    "color": "#9CA3AF",
                    "align": "center",
                    "wrap": True,
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": (
                        f"分類：{comparison.get('classification_source') or result.get('catalog_source') or '人工主分類'}"
                        f"｜人工維護 {result.get('catalog_updated_at') or '--'}"
                    ),
                    "size": "xs",
                    "color": "#7C3AED",
                    "align": "center",
                    "wrap": True,
                    "margin": "xs",
                },
                {
                    "type": "text",
                    "text": str(
                        methodology.get("similarity")
                        or (
                            "相似度依日報酬、同向率、"
                            "波動與量能連動計算。"
                        )
                    ),
                    "size": "xs",
                    "color": "#6B7280",
                    "align": "center",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": (
                        "概念分類與歷史連動僅供觀察；"
                        "不代表未來仍同步。"
                    ),
                    "size": "xs",
                    "color": "#9CA3AF",
                    "align": "center",
                    "wrap": True,
                    "margin": "xs",
                },
            ]
        )
        contents.extend(
            _mode_buttons(stock_id, "peer_compare", current_tf)
        )
        bubbles.append(
            {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "14px",
                    "spacing": "sm",
                    "contents": contents,
                },
            }
        )

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 族群趨勢與比較",
        "contents": {
            "type": "carousel",
            "contents": bubbles,
        },
    }


def _build_double_knife_flex(
    result: dict[str, Any],
    stock_id: str,
    stock_name: str,
    current_tf: str,
) -> dict[str, Any]:
    """240日雙刀配對研究卡；只呈現研究狀態，不產生交易指令。"""

    def number(value: Any, digits: int = 2) -> str:
        try:
            return f"{float(value):,.{digits}f}"
        except Exception:
            return "--"

    def signed_pct(value: Any, digits: int = 2) -> str:
        try:
            return f"{float(value):+,.{digits}f}%"
        except Exception:
            return "--"

    def value_color(value: Any) -> str:
        try:
            numeric = float(value)
            if numeric > 0:
                return UP_COLOR
            if numeric < 0:
                return DOWN_COLOR
        except Exception:
            pass
        return FLAT_COLOR

    def metric_box(
        label: str,
        value: str,
        color: str = "#111827",
    ) -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "vertical",
            "flex": 1,
            "backgroundColor": "#F5F6F8",
            "cornerRadius": "12px",
            "paddingAll": "9px",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "xxs",
                    "color": "#6B7280",
                    "align": "center",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": value,
                    "size": "md",
                    "weight": "bold",
                    "color": color,
                    "align": "center",
                    "margin": "xs",
                    "wrap": True,
                },
            ],
        }

    def info_row(
        label: str,
        value: str,
        color: str = "#374151",
    ) -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": label,
                    "size": "sm",
                    "color": "#6B7280",
                    "flex": 5,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": value,
                    "size": "sm",
                    "weight": "bold",
                    "color": color,
                    "flex": 6,
                    "align": "end",
                    "wrap": True,
                },
            ],
        }

    def badge() -> dict[str, Any]:
        return {
            "type": "box",
            "layout": "vertical",
            "flex": 0,
            "backgroundColor": "#FEF3C7",
            "cornerRadius": "8px",
            "paddingAll": "6px",
            "contents": [
                {
                    "type": "text",
                    "text": "雙刀",
                    "size": "xs",
                    "weight": "bold",
                    "color": "#B45309",
                    "align": "center",
                }
            ],
        }

    pairs = list(result.get("pairs") or [])
    if not bool(result.get("available")) or not pairs:
        contents: list[dict[str, Any]] = [
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{stock_id} {stock_name}",
                        "size": "xxl",
                        "weight": "bold",
                        "color": "#111827",
                        "flex": 1,
                        "wrap": True,
                    },
                    badge(),
                ],
            },
            {
                "type": "text",
                "text": "雙刀配對研究",
                "size": "lg",
                "weight": "bold",
                "color": "#374151",
                "margin": "sm",
            },
            {
                "type": "text",
                "text": str(
                    result.get("message")
                    or "目前沒有足夠的240日配對資料"
                ),
                "size": "sm",
                "color": "#6B7280",
                "wrap": True,
                "margin": "md",
            },
            {
                "type": "text",
                "text": "此功能為配對研究工具，不代表建議放空或買進。",
                "size": "xs",
                "color": "#9CA3AF",
                "wrap": True,
                "margin": "md",
            },
        ]
        contents.extend(_mode_buttons(stock_id, "double_knife", current_tf))
        return {
            "type": "flex",
            "altText": f"{stock_id} {stock_name} 雙刀配對研究",
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

    bubbles: list[dict[str, Any]] = []
    disposition = result.get("disposition") or {}
    for pair in pairs:
        long_id = str(pair.get("long_id") or "")
        long_name = str(pair.get("long_name") or long_id)
        concept_text = "、".join(
            str(value)
            for value in (pair.get("concepts") or [])
            if value
        ) or "概念族群"
        correlation = float(pair.get("return_correlation") or 0.0)
        zscore = float(pair.get("spread_zscore") or 0.0)
        chart_url = str(pair.get("chart_url") or "").strip()
        state = str(pair.get("spread_state") or "觀察中")
        state_color = (
            "#059669"
            if state in {"正在收斂", "接近均值"}
            else (
                "#DC2626"
                if state == "持續發散"
                else "#B45309"
            )
        )
        contents: list[dict[str, Any]] = [
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{stock_id} {stock_name}",
                        "size": "xxl",
                        "weight": "bold",
                        "color": "#111827",
                        "flex": 1,
                        "wrap": True,
                    },
                    badge(),
                ],
            },
            {
                "type": "text",
                "text": f"雙刀研究｜{concept_text}",
                "size": "lg",
                "weight": "bold",
                "color": "#374151",
                "margin": "sm",
                "wrap": True,
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "md",
                "contents": [
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 1,
                        "backgroundColor": "#FEF2F2",
                        "cornerRadius": "12px",
                        "paddingAll": "10px",
                        "contents": [
                            {
                                "type": "text",
                                "text": "空方觀察",
                                "size": "xxs",
                                "color": "#B91C1C",
                                "align": "center",
                            },
                            {
                                "type": "text",
                                "text": f"{stock_id} {stock_name}",
                                "size": "sm",
                                "weight": "bold",
                                "color": "#7F1D1D",
                                "align": "center",
                                "wrap": True,
                                "margin": "xs",
                            },
                        ],
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "flex": 1,
                        "backgroundColor": "#ECFDF5",
                        "cornerRadius": "12px",
                        "paddingAll": "10px",
                        "contents": [
                            {
                                "type": "text",
                                "text": "多方配對",
                                "size": "xxs",
                                "color": "#047857",
                                "align": "center",
                            },
                            {
                                "type": "text",
                                "text": f"{long_id} {long_name}",
                                "size": "sm",
                                "weight": "bold",
                                "color": "#065F46",
                                "align": "center",
                                "wrap": True,
                                "margin": "xs",
                            },
                        ],
                    },
                ],
            },
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "xs",
                "margin": "md",
                "contents": [
                    metric_box(
                        "240日相關",
                        number(correlation, 2),
                        "#2563EB" if correlation >= 0.55 else "#6B7280",
                    ),
                    metric_box(
                        "避險 Beta",
                        number(pair.get("hedge_beta"), 2),
                    ),
                    metric_box(
                        "價差 Z",
                        number(zscore, 2),
                        value_color(zscore),
                    ),
                ],
            },
        ]
        if chart_url:
            contents.append(
                {
                    "type": "image",
                    "url": chart_url,
                    "size": "full",
                    "aspectRatio": "9:4",
                    "aspectMode": "fit",
                    "margin": "md",
                    "backgroundColor": "#FFFFFF",
                }
            )
        contents.extend(
            [
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "margin": "md",
                    "contents": [
                        info_row("價差狀態", state, state_color),
                        info_row(
                            "今日四象限",
                            str(pair.get("quadrant") or "--"),
                        ),
                        info_row(
                            "空方今日",
                            signed_pct(pair.get("short_return_1d_pct")),
                            value_color(pair.get("short_return_1d_pct")),
                        ),
                        info_row(
                            "多方今日",
                            signed_pct(pair.get("long_return_1d_pct")),
                            value_color(pair.get("long_return_1d_pct")),
                        ),
                        info_row(
                            "配對研究報酬",
                            signed_pct(pair.get("pair_return_1d_pct")),
                            value_color(pair.get("pair_return_1d_pct")),
                        ),
                        info_row(
                            "同向交易日",
                            f"{number(pair.get('direction_agreement_pct'), 0)}%",
                        ),
                    ],
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#FFFBEB",
                    "cornerRadius": "10px",
                    "paddingAll": "9px",
                    "margin": "md",
                    "contents": [
                        {
                            "type": "text",
                            "text": str(
                                pair.get("research_status")
                                or "配對觀察中"
                            ),
                            "size": "sm",
                            "weight": "bold",
                            "color": "#92400E",
                            "align": "center",
                            "wrap": True,
                        }
                    ],
                },
                {
                    "type": "text",
                    "text": (
                        f"官方處置資料："
                        f"{disposition.get('status') or '尚未接入'}"
                    ),
                    "size": "xs",
                    "color": "#6B7280",
                    "align": "center",
                    "wrap": True,
                    "margin": "md",
                },
                {
                    "type": "text",
                    "text": (
                        f"資料日 {pair.get('data_date') or '--'}｜"
                        f"共同樣本 {pair.get('sample_days') or 0} 日"
                    ),
                    "size": "xs",
                    "color": "#9CA3AF",
                    "align": "center",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": (
                        "Z-score向0縮小才是收斂；同漲或同跌仍須比較幅度。"
                    ),
                    "size": "xs",
                    "color": "#6B7280",
                    "align": "center",
                    "wrap": True,
                    "margin": "xs",
                },
                {
                    "type": "text",
                    "text": (
                        "研究卡未納入交易成本、融券限制與處置事件，"
                        "不代表交易建議。"
                    ),
                    "size": "xs",
                    "color": "#9CA3AF",
                    "align": "center",
                    "wrap": True,
                    "margin": "xs",
                },
            ]
        )
        contents.extend(_mode_buttons(stock_id, "double_knife", current_tf))
        bubbles.append(
            {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "paddingAll": "14px",
                    "spacing": "sm",
                    "contents": contents,
                },
            }
        )

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 雙刀配對研究",
        "contents": {
            "type": "carousel",
            "contents": bubbles,
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
                request_text = f"{raw_stock} {raw_text}".strip()
                if (
                    "盤後" in request_text
                    and (
                        "大盤" in request_text
                        or "加權" in request_text
                        or "盤後總覽" in request_text
                    )
                ):
                    action = "market_afterhours"
                elif (
                    "預測" in request_text
                    and (
                        "大盤" in request_text
                        or "加權" in request_text
                        or "15分" in request_text
                        or "15分鐘" in request_text
                    )
                ):
                    action = "market_prediction"
                elif _is_market_future_request(raw_stock, raw_text):
                    action = "market_future_all"
                else:
                    action = "market_index"

            if action == "market_afterhours":
                # 盤後服務延遲載入，避免增加一般圖卡冷啟動時間。
                from services.market_afterhours_digest_service_v2 import (
                    build_market_afterhours_digest,
                )

                digest = build_market_afterhours_digest()
                print(
                    "DEBUG market_afterhours controller",
                    "| ok =", digest.get("ok"),
                    "| date =", digest.get("trade_date"),
                    "| close =", (digest.get("market") or {}).get("close"),
                    "| contribution_date =",
                    (digest.get("weight") or {}).get("date"),
                    "| sec =", digest.get("seconds"),
                    flush=True,
                )
                return _reply_with_title(
                    "大盤盤後總覽",
                    _build_market_afterhours_digest_flex(digest),
                )

            if action == "market_prediction":
                # 預測模組延遲載入，避免拖慢一般股票與大盤圖卡冷啟動。
                from services.market_prediction_shadow_service_v1_3_release_gate import (
                    evaluate_shadow_history,
                    predict_market_shadow,
                    resolve_market_prediction_release,
                )

                result = predict_market_shadow(persist=False)
                quality = evaluate_shadow_history()
                release = resolve_market_prediction_release(quality)
                result["release_quality"] = quality
                result["release"] = release
                result["release_mode_requested"] = release.get(
                    "requested_mode"
                )
                result["release_mode_effective"] = release.get(
                    "effective_mode"
                )
                print(
                    "DEBUG market_prediction shadow controller",
                    "| ok =", result.get("ok"),
                    "| signal =", result.get("signal"),
                    "| time =", result.get("display_time"),
                    "| freshness =", result.get("freshness_status"),
                    "| release_requested =",
                    release.get("requested_mode"),
                    "| release_effective =",
                    release.get("effective_mode"),
                    "| public_ready =",
                    quality.get("ready_for_public_signal"),
                    "| sec =", result.get("seconds"),
                    flush=True,
                )
                return _reply_with_title(
                    (
                        "大盤15分鐘預測"
                        if release.get("effective_mode") == "public"
                        else "大盤15分鐘預測（測試中）"
                    ),
                    _build_market_prediction_shadow_flex(result),
                )

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

            if action in {
                "market_margin",
                "market_margin_tse",
                "market_margin_otc",
            }:
                market_scope = (
                    "otc"
                    if action == "market_margin_otc"
                    else "tse"
                )
                snapshot = get_market_margin_snapshot(
                    market_scope=market_scope
                )
                market_name = (
                    "上櫃"
                    if market_scope == "otc"
                    else "上市"
                )

                print(
                    "DEBUG market_margin controller",
                    "| version =",
                    MARKET_MARGIN_SWITCH_VERSION,
                    "| action =",
                    action,
                    "| market_scope =",
                    market_scope,
                    "| available =",
                    getattr(snapshot, "available", None),
                    "| latest_date =",
                    getattr(snapshot, "latest_date", ""),
                    "| source =",
                    getattr(snapshot, "source", ""),
                    flush=True,
                )

                return _reply_with_title(
                f"{market_name}大盤融資券",
                _build_market_margin_flex(snapshot),
                )


            if action in {"market_future_day", "market_future_all"}:
                # 大盤期貨一律走全盤；market_future_day 只保留舊按鈕相容。
                action = "market_future_all"
                session_mode = "all"

                snapshot = get_market_future_snapshot(session_mode=session_mode)
                index_snapshot = get_market_index_snapshot(with_chart=False)

                return _reply_with_title(
                    "台指期 全盤",
                    _build_market_future_realtime_flex(snapshot, action, index_snapshot),
                )

            if action == "market_future_k":
                tf = str(
                    getattr(req, "time_frame", "")
                    or requested_tf
                    or "1m"
                ).strip()

                if tf not in {"1m", "5m", "15m", "30m", "60m"}:
                    tf = "1m"

                kline_snapshot = get_market_future_kline_snapshot(
                    time_frame=tf,
                    rows=60,
                )

                return _reply_with_title(
                    f"台指期 {tf.replace('m', '分')}K",
                    _build_market_future_kline_flex(kline_snapshot, tf),
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
        # 1.7 主力進出
        # -------------------------
        if action == "main_force":
            t_main_force0 = time.perf_counter()

            # 延遲載入，避免一般行情與K線查詢承擔額外啟動成本。
            from services.stock_main_force_service_v1_3 import (
                get_stock_main_force_snapshot,
            )

            daily_history = None
            try:
                daily_history = get_history(meta, "D")
                if isinstance(daily_history, tuple):
                    daily_history = daily_history[0]
            except Exception as history_exc:
                print(
                    "DEBUG stock_main_force | daily_history_failed",
                    "| stock_id =", meta.stock_id,
                    "| error =", repr(history_exc),
                    flush=True,
                )

            snapshot = get_stock_main_force_snapshot(
                stock_id=meta.stock_id,
                stock_name=stock_name,
                daily_history=daily_history,
            )
            t_main_force1 = time.perf_counter()
            flex = _build_main_force_flex(
                stock_id=meta.stock_id,
                stock_name=stock_name,
                snapshot=snapshot,
                current_tf=requested_tf,
            )

            print(
                "DEBUG stock timing main_force",
                "| version =", MAIN_FORCE_CARD_VERSION,
                "| stock_id =", meta.stock_id,
                "| available =", bool(
                    getattr(snapshot, "available", False)
                ),
                "| latest_date =",
                getattr(snapshot, "latest_date", ""),
                "| rows =",
                len(getattr(snapshot, "rows", []) or []),
                "| data_sec =", round(t_main_force1 - t_main_force0, 3),
                "| total_sec =",
                round(time.perf_counter() - stock_t0, 3),
                flush=True,
            )
            return _reply_with_title(
                f"{stock_name} 主力進出",
                flex,
            )

        # -------------------------
        # 1.8 概念族群比較
        # -------------------------
        if action == "peer_compare":
            t_peer0 = time.perf_counter()

            # 延遲載入，避免一般行情與K線查詢承擔額外啟動成本。
            from services.stock_group_comparison_service_v5_1_group_trend import (
                build_stock_group_comparison,
            )

            result = build_stock_group_comparison(
                stock_id=meta.stock_id,
                stock_name=stock_name,
            )
            t_peer1 = time.perf_counter()
            flex = _build_concept_peer_flex(
                result=result,
                stock_id=meta.stock_id,
                stock_name=stock_name,
                current_tf=requested_tf,
            )

            print(
                "DEBUG stock timing concept_peer",
                "| version =", CONCEPT_PEER_CARD_VERSION,
                "| stock_id =", meta.stock_id,
                "| concepts =", result.get("concepts"),
                "| comparisons =",
                len(result.get("comparisons") or []),
                "| best_peer =",
                (
                    (result.get("comparisons") or [{}])[0].get("peer_id")
                    if result.get("comparisons")
                    else ""
                ),
                "| data_sec =", round(t_peer1 - t_peer0, 3),
                "| total_sec =",
                round(time.perf_counter() - stock_t0, 3),
                flush=True,
            )
            return _reply_with_title(
                f"{stock_name} 族群比較",
                flex,
            )

        # -------------------------
        # 2. 財務
        # -------------------------
        if action == "financial":
            t_fin0 = time.perf_counter()

            stock_id_for_financial = str(getattr(meta, "stock_id", "") or "").strip()

            snapshot = get_financial_snapshot(
                stock_id_for_financial,
                stock_name,
            )

            t_fin1 = time.perf_counter()

            print(
                "DEBUG stock timing financial",
                "| stock_id =", stock_id_for_financial,
                "| available =", bool(getattr(snapshot, "available", False)),
                "| rows =", len(getattr(snapshot, "rows", []) or []),
                "| sec =", round(t_fin1 - t_fin0, 3),
                "| total_sec =", round(time.perf_counter() - stock_t0, 3),
                flush=True,
            )

            flex = _build_financial_flex(
                stock_id_for_financial,
                stock_name,
                snapshot,
                requested_tf,
            )

            return _reply_with_title(
                f"{stock_name} 財務",
                flex,
            )


        # -------------------------
        # 2.5 本益比河流圖
        # -------------------------
        if action == "pe_river":
            t_river0 = time.perf_counter()

            stock_id_for_river = str(getattr(meta, "stock_id", "") or "").strip()

            snapshot = get_pe_river_snapshot(
                stock_id_for_river,
                stock_name,
            )

            t_river1 = time.perf_counter()

            print(
                "DEBUG stock timing pe_river",
                "| stock_id =", stock_id_for_river,
                "| available =", bool(getattr(snapshot, "available", False)),
                "| current_pe =", getattr(snapshot, "current_pe", None),
                "| zone =", getattr(snapshot, "zone_label", None),
                "| sec =", round(t_river1 - t_river0, 3),
                "| total_sec =", round(time.perf_counter() - stock_t0, 3),
                flush=True,
            )

            flex = _build_pe_river_flex(
                stock_id_for_river,
                stock_name,
                snapshot,
                requested_tf,
            )

            return _reply_with_title(
                f"{stock_name} 本益比河流圖",
                flex,
            )

        # -------------------------
        # 2.5 盤後分析計算方式
        # -------------------------
        if action == "post_market_method":
            method_flex = {
                "type": "flex",
                "altText": f"{meta.stock_id} {stock_name} 盤後分析計算方式",
                "contents": _build_post_market_method_bubble(
                    meta.stock_id,
                    stock_name,
                ),
            }
            return _reply_with_title(
                f"{stock_name} 盤後分析計算方式",
                method_flex,
            )

        # -------------------------
        # 2.5 盤後分析
        # -------------------------
        if action in {"post_market", "post_market_short", "post_market_daytrade"}:
            t_post0 = time.perf_counter()

            df, tf = _get_history_df_tf_safe(meta, "D")

            if df is None or len(df) == 0:
                return text_message("目前暫時抓不到這檔股票的日K資料，無法產生盤後分析。")

            try:
                # 若 FinMind 日K尚未更新到今日，補 Shioaji snapshot。
                df_for_post = _apply_shioaji_stock_realtime(df, meta.stock_id)
            except Exception as exc:
                print(
                    "DEBUG post_market realtime append failed",
                    "| stock_id =", meta.stock_id,
                    "| error =", repr(exc),
                    flush=True,
                )
                df_for_post = df

            # 先給預設值，避免 price_meta 建立失敗時整個查詢失敗。
            price_info = "--"
            change_info = "--"
            update_time = "--"
            price_change = 0.0

            try:
                price_meta = build_price_meta(df_for_post, "D")
                price_meta = _apply_realtime_snapshot_price_meta(
                    price_meta,
                    df_for_post,
                    "D",
                )

                price_info = getattr(price_meta, "price_info", "--")
                change_info = getattr(price_meta, "change_info", "--")
                update_time = getattr(price_meta, "time_stamp", "--")
                price_change = float(getattr(price_meta, "price_change", 0.0) or 0.0)

            except Exception as exc:
                print(
                    "DEBUG post_market price_meta failed",
                    "| stock_id =", meta.stock_id,
                    "| error =", repr(exc),
                    flush=True,
                )

            daytrade_ratio = None
            daytrade_date = ""
            daytrade_status = ""
            try:
                # 官方當沖資料為盤後資料；失敗時只隱藏欄位，不阻擋任一圖卡。
                from services.stock_daytrade_ratio_service_v1 import (
                    get_stock_daytrade_ratio,
                )

                daytrade_snapshot = get_stock_daytrade_ratio(
                    meta.stock_id,
                    daily_df=df_for_post,
                )
                if getattr(daytrade_snapshot, "available", False):
                    daytrade_ratio = float(
                        getattr(daytrade_snapshot, "ratio_pct", 0.0) or 0.0
                    )
                    daytrade_date = str(
                        getattr(daytrade_snapshot, "latest_date", "") or ""
                    )
                    daytrade_status = str(
                        getattr(daytrade_snapshot, "publication_status", "") or ""
                    )
            except Exception as exc:
                print(
                    "DEBUG post_market daytrade ratio failed",
                    "| stock_id =", meta.stock_id,
                    "| error =", repr(exc),
                    flush=True,
                )

            short_image_url = generate_post_market_analysis_chart(
                df_for_post,
                meta.stock_id,
                stock_name,
                analysis_mode="short",
            )
            daytrade_image_url = generate_post_market_analysis_chart(
                df_for_post,
                meta.stock_id,
                stock_name,
                analysis_mode="daytrade",
                daytrade_ratio=daytrade_ratio,
                daytrade_date=daytrade_date,
                daytrade_status=daytrade_status,
            )

            fib_image_url = generate_kline_chart(
                df,
                meta.stock_id,
                stock_name,
                tf,
                show_fibonacci=True,
            )

            print(
                "DEBUG stock timing post_market",
                "| version =", POST_MARKET_COMPARISON_VERSION,
                "| outer_card_version =", POST_MARKET_OUTER_CARD_VERSION,
                "| stock_id =", meta.stock_id,
                "| rows =", 0 if df_for_post is None else len(df_for_post),
                "| daytrade_ratio =", daytrade_ratio,
                "| short_image_url =", short_image_url,
                "| daytrade_image_url =", daytrade_image_url,
                "| sec =", round(time.perf_counter() - t_post0, 3),
                flush=True,
            )

            short_flex = _build_chart_flex(
                stock_id=meta.stock_id,
                stock_name=stock_name,
                image_url=short_image_url,
                price_info=price_info,
                change_info=change_info,
                update_time=update_time,
                price_change=price_change,
                active_mode="post_market_short",
                current_tf="D",
                image_aspect_ratio="1:1",
                price_source="daily_history",
                show_period_buttons=True,
            )
            daytrade_flex = _build_chart_flex(
                stock_id=meta.stock_id,
                stock_name=stock_name,
                image_url=daytrade_image_url,
                price_info=price_info,
                change_info=change_info,
                update_time=update_time,
                price_change=price_change,
                active_mode="post_market_daytrade",
                current_tf="D",
                image_aspect_ratio="1:1",
                price_source="daily_history",
                show_period_buttons=True,
            )
            fib_flex = _build_chart_flex(
                stock_id=meta.stock_id,
                stock_name=stock_name,
                image_url=fib_image_url,
                price_info=price_info,
                change_info=change_info,
                update_time=update_time,
                price_change=price_change,
                active_mode="post_market_short",
                current_tf="D",
                image_aspect_ratio="1:1",
                price_source="daily_history",
                show_period_buttons=True,
            )

            carousel = {
                "type": "flex",
                "altText": f"{meta.stock_id} {stock_name} 盤後分析",
                "contents": {
                    "type": "carousel",
                    "contents": [
                        short_flex["contents"],
                        daytrade_flex["contents"],
                        fib_flex["contents"],
                    ],
                },
            }
            return _reply_with_title(f"{stock_name} 盤後分析", carousel)
                
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

            # 日K盤中不能停在昨日收盤：
            # 將既有的 Yahoo 1分底稿 + Shioaji snapshot 聚合成今天一根
            # 未完成日K，覆蓋同日舊列後同時提供圖表與圖卡使用。
            if action == "k_line" and tf == "D":
                df = _upsert_live_daily_candle(df, meta)

            if tf in INTRADAY_TIME_FRAMES:
                import os

                allow_cold_login = (
                    str(os.getenv("ALLOW_COLD_SHIOAJI_STOCK_APPEND", "0")).strip()
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

                # Yahoo 盤中可能延遲，再用 Shioaji snapshot 補到同一份 1 分底稿。
                df = _apply_shioaji_stock_realtime(df, meta.stock_id)

            # 所有個股分 K 統一資料流：
            # 多日 1 分底稿 -> 補 snapshot -> 依顯示週期重採樣。
            # 即時圖只顯示最新交易日；15/30/60 分 K 可保留多日資料。
            intraday_price_df = None
            if tf in INTRADAY_TIME_FRAMES:
                raw_1m_df = df
                intraday_price_df = _slice_latest_intraday_session(raw_1m_df)

                if action == "instant":
                    chart_base_df = intraday_price_df
                else:
                    chart_base_df = raw_1m_df

                df = _resample_intraday_from_1m(chart_base_df, tf)

                print(
                    "DEBUG INTRADAY UNIFIED PIPELINE ACTIVE",
                    "| version =", INTRADAY_UNIFIED_FIX_VERSION,
                    "| stock_id =", getattr(meta, "stock_id", ""),
                    "| action =", action,
                    "| chart_tf =", tf,
                    "| price_tf = 1m",
                    "| raw_rows =", 0 if raw_1m_df is None else len(raw_1m_df),
                    "| session_rows =", 0 if intraday_price_df is None else len(intraday_price_df),
                    "| chart_rows =", 0 if df is None else len(df),
                    "| raw_last =", "" if raw_1m_df is None or len(raw_1m_df) == 0 else raw_1m_df.index[-1],
                    "| price_last =", "" if intraday_price_df is None or len(intraday_price_df) == 0 else intraday_price_df.index[-1],
                    "| chart_last =", "" if df is None or len(df) == 0 else df.index[-1],
                    flush=True,
                )

            # 圖表週期與圖卡價格週期分離：
            # - df / tf：只負責畫 D / W / M K 線。
            # - price_df / price_tf：只負責圖卡上方現價、當日漲跌與更新日期。
            #
            # 週 K、月 K 不可用前一週／前一月計算圖卡漲跌，否則會出現：
            # 798 - 900 = -102、795 - 1140 = -345 這類週／月差額。
            # 因此 W / M 一律另外取得日 K 最新兩筆，改算「當日漲跌」。
            if tf in INTRADAY_TIME_FRAMES and intraday_price_df is not None:
                price_df = intraday_price_df
                price_tf = "1m"
                price_source = "unified_1m_base"
            else:
                price_df = df
                price_tf = tf
                price_source = "chart_history"

            if action == "k_line" and tf in {"W", "M"}:
                try:
                    daily_df, daily_tf = _get_history_df_tf_safe(meta, "D")

                    if daily_df is not None and len(daily_df) >= 2:
                        price_df = daily_df
                        price_tf = "D"
                        price_source = "daily_history"

                        print(
                            "DEBUG DWM CARD PRICE FIX ACTIVE",
                            "| version =", DWM_CARD_PRICE_FIX_VERSION,
                            "| stock_id =", getattr(meta, "stock_id", ""),
                            "| chart_tf =", tf,
                            "| price_tf =", price_tf,
                            "| price_rows =", len(price_df),
                            "| daily_latest =", str(price_df.index[-1]),
                            flush=True,
                        )
                    else:
                        print(
                            "DEBUG DWM daily history unavailable",
                            "| version =", DWM_CARD_PRICE_FIX_VERSION,
                            "| stock_id =", getattr(meta, "stock_id", ""),
                            "| chart_tf =", tf,
                            "| daily_rows =", 0 if daily_df is None else len(daily_df),
                            flush=True,
                        )

                except Exception as exc:
                    print(
                        "DEBUG DWM daily history failed",
                        "| version =", DWM_CARD_PRICE_FIX_VERSION,
                        "| stock_id =", getattr(meta, "stock_id", ""),
                        "| chart_tf =", tf,
                        "| error =", repr(exc),
                        flush=True,
                    )

            # D / W / M 的圖卡價格都可再用 Shioaji 即時 snapshot 覆蓋；
            # 若 Shioaji 尚未熱機，W / M 仍保留上面的日 K 當日漲跌，不退回週／月差額。
            if action == "k_line" and tf in {"D", "W", "M"}:
                try:
                    realtime_price_df = _apply_shioaji_stock_realtime(
                        price_df,
                        meta.stock_id,
                    )

                    if realtime_price_df is not None and len(realtime_price_df) > 0:
                        price_df = realtime_price_df

                        attrs = dict(getattr(price_df, "attrs", {}) or {})
                        if str(attrs.get("realtime_snapshot_source") or "").lower() == "shioaji":
                            price_source = "shioaji_snapshot"

                except Exception as exc:
                    print(
                        "DEBUG kline realtime price_meta fallback failed",
                        "| version =", DWM_CARD_PRICE_FIX_VERSION,
                        "| stock_id =", meta.stock_id,
                        "| chart_tf =", tf,
                        "| price_tf =", price_tf,
                        "| error =", repr(exc),
                        flush=True,
                    )

            t_append1 = time.perf_counter()

            print(
                "DEBUG stock timing append_snapshot",
                "| version =", DWM_CARD_PRICE_FIX_VERSION,
                "| stock_id =", getattr(meta, "stock_id", ""),
                "| chart_tf =", tf,
                "| price_tf =", price_tf,
                "| price_source =", price_source,
                "| rows =", 0 if df is None else len(df),
                "| price_rows =", 0 if price_df is None else len(price_df),
                "| sec =", round(t_append1 - t_append0, 3),
                flush=True,
            )

            t_price0 = time.perf_counter()

            # 關鍵：這裡必須傳 price_tf，而不是圖表週期 tf。
            # W / M 在沒有 Shioaji 時，price_tf 會是 D。
            price_meta = build_price_meta(price_df, price_tf)

            if action == "k_line" and tf in {"D", "W", "M"}:
                price_meta = _apply_realtime_snapshot_price_meta(
                    price_meta,
                    price_df,
                    price_tf,
                )

                # Shioaji 尚未熱機時，使用 Yahoo 盤中資料更新圖卡價格、
                # 對前收漲跌與時間；不改動 D / W / M K 線圖片。
                if price_source != "shioaji_snapshot":
                    yahoo_price_meta = _apply_yahoo_intraday_price_meta(
                        price_meta,
                        meta,
                        tf,
                    )

                    if yahoo_price_meta is not price_meta:
                        price_meta = yahoo_price_meta
                        price_source = "yahoo_direct"

            t_price1 = time.perf_counter()

            print(
                "DEBUG stock timing price_meta",
                "| version =", DWM_CARD_PRICE_FIX_VERSION,
                "| stock_id =", getattr(meta, "stock_id", ""),
                "| chart_tf =", tf,
                "| price_tf =", price_tf,
                "| price_source =", price_source,
                "| price_info =", getattr(price_meta, "price_info", ""),
                "| change_info =", getattr(price_meta, "change_info", ""),
                "| update_time =", getattr(price_meta, "time_stamp", ""),
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
                    price_source=price_source,
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
                    price_source=price_source,
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
        if action in {"large_holder", "large_holder_200", "large_holder_400", "large_holder_600", "large_holder_800", "large_holder_1000"}:
            t_data0 = time.perf_counter()

            holder_threshold = _large_holder_threshold_from_action(action)

            rows = get_large_holder_table(
                meta.stock_id,
                threshold=holder_threshold,
            )

            t_data1 = time.perf_counter()

            flex = _build_large_holder_flex(
                stock_id=meta.stock_id,
                stock_name=stock_name,
                rows=rows,
                current_tf=requested_tf,
                threshold=holder_threshold,
            )

            t_flex1 = time.perf_counter()

            print(
                "DEBUG stock timing large_holder",
                "| stock_id =", getattr(meta, "stock_id", ""),
                "| threshold =", holder_threshold,
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
