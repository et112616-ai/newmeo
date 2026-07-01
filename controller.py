from __future__ import annotations

from services.sinopac_quote_service import append_stock_snapshot_to_intraday_df

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
from services.futures_service import get_stock_futures_snapshot
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

def _get_history_df_tf(meta, requested_tf: str):
    """
    相容不同版本的 get_history()。

    可能回傳：
    1. df
    2. df, tf
    3. (df, tf), something   # 防止巢狀 tuple
    """
    result = get_history(meta, requested_tf)
    tf = normalize_time_frame(requested_tf)

    # 防止 get_history 回傳 tuple 或巢狀 tuple
    while isinstance(result, tuple):
        if len(result) >= 2 and result[1]:
            try:
                tf = normalize_time_frame(result[1])
            except Exception:
                pass

        result = result[0] if len(result) >= 1 else result

    df = result

    print(
        "_get_history_df_tf | "
        f"requested_tf={requested_tf} | tf={tf} | "
        f"df_type={type(df)}",
        flush=True,
    )

    return df, tf
    
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


def _build_large_holder_flex(
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
            "text": "大戶持股週報",
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

    for r in rows[:8]:
        date = str(r.get("date", "--"))
        ratio = str(r.get("ratio", "--"))
        diff = str(r.get("diff", "--"))

        diff_color = UP_COLOR if diff.startswith("+") else DOWN_COLOR if diff.startswith("-") else FLAT_COLOR

        contents.append(
            {
                "type": "box",
                "layout": "horizontal",
                "margin": "sm",
                "contents": [
                    {
                        "type": "text",
                        "text": f"🗓 {date}",
                        "size": "md",
                        "flex": 3,
                        "color": "#333333",
                    },
                    {
                        "type": "text",
                        "text": ratio,
                        "size": "md",
                        "flex": 3,
                        "color": "#333333",
                    },
                    {
                        "type": "text",
                        "text": diff,
                        "size": "md",
                        "flex": 3,
                        "color": diff_color,
                        "align": "end",
                    },
                ],
            }
        )

    contents.extend(_mode_buttons(stock_id, "large_holder", current_tf))

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} 大戶持股週報",
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
    "指數",
    "加權",
    "加權指數",
    "台股大盤",
    "台灣加權",
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
                snapshot = get_market_index_snapshot()

                return _reply_with_title(
                    "加權指數",
                    _build_market_index_realtime_flex(snapshot),
                )

            if action in {"market_future_day", "market_future_all"}:
                return _reply_with_title(
                    "台指期",
                _build_market_future_placeholder_flex(action),
                )

            return _reply_with_title(
                "加權指數",
                _build_market_index_placeholder_flex(action),
            )

        return _reply_with_title(
            "加權指數",
            _build_market_index_placeholder_flex(action),
        )

        meta = normalize_stock_input(req.stock)
        stock_name = get_stock_name(meta)
        
        # 文字輸入預設是 instant，但 parser 可能給 D。
        # 即時圖只適合 1m / 5m，所以預設改成 1m。
        if action == "instant" and requested_tf not in {"1m", "5m"}:
            requested_tf = "1m"

        # 如果使用者在即時模式按 D/W/M，改用 K 線。
        if action == "instant" and requested_tf in {"D", "W", "M"}:
            action = "k_line"
            current_mode = "k_line"

        # 即時 / K 線 / 法人圖都需要行情資料
        if action in {"instant", "k_line", "chip"}:
            df, tf = _get_history_df_tf(meta, requested_tf)

            if tf in {"1m", "5m"}:
                df = append_stock_snapshot_to_intraday_df(df, meta.stock_id)
            
            price_meta = build_price_meta(df, tf)

            if action == "instant":
                image_url = generate_instant_chart(df, meta.stock_id, stock_name)
                return _reply_with_title(
                    f"{stock_name} 即時走勢",
                    _build_chart_flex(
                        stock_id=meta.stock_id,
                        stock_name=stock_name,
                        image_url=image_url,
                        price_info=price_meta.price_info,
                        change_info=price_meta.change_info,
                        update_time=price_meta.time_stamp,
                        price_change=price_meta.price_change,
                        active_mode="instant",
                        current_tf=tf,
                    ),
                )

            if action == "k_line":
                image_url = generate_kline_chart(df, meta.stock_id, stock_name, tf)
                return _reply_with_title(
                    f"{stock_name} K線",
                    _build_chart_flex(
                        stock_id=meta.stock_id,
                        stock_name=stock_name,
                        image_url=image_url,
                        price_info=price_meta.price_info,
                        change_info=price_meta.change_info,
                        update_time=price_meta.time_stamp,
                        price_change=price_meta.price_change,
                        active_mode="k_line",
                        current_tf=tf,
                    ),
                )

            if action == "chip":
                chip_rows = get_institutional_chips(meta.stock_id)
                image_url = generate_chip_chart(meta.stock_id, stock_name, chip_rows)

                return _reply_with_title(
                    f"{stock_name} 法人籌碼",
                    _build_chart_flex(
                        stock_id=meta.stock_id,
                        stock_name=stock_name,
                        image_url=image_url,
                        price_info=price_meta.price_info,
                        change_info=price_meta.change_info,
                        update_time=price_meta.time_stamp,
                        price_change=price_meta.price_change,
                        active_mode="chip",
                        current_tf=tf,
                        image_aspect_ratio="4:5",   # 🔥 法人圖改高一點
                    ),
                )

        if action == "large_holder":
            rows = get_large_holder_table(meta.stock_id)
            return _reply_with_title(
                f"{stock_name} 大戶持股",
                _build_large_holder_flex(
                    stock_id=meta.stock_id,
                    stock_name=stock_name,
                    rows=rows,
                    current_tf=requested_tf,
                ),
            )
                
        if action == "margin":
            rows = get_margin_table(meta.stock_id)
            return _reply_with_title(
                f"{stock_name} 融資券",
                _build_margin_flex(
                    stock_id=meta.stock_id,
                    stock_name=stock_name,
                    rows=rows,
                    current_tf=requested_tf,
                ),
            )
                
        if action in {"futures", "futures_day", "futures_all"}:
            futures_session_mode = "day"

            if action == "futures_all":
                futures_session_mode = "all"

            snapshot = get_stock_futures_snapshot(
                meta.stock_id,
                stock_name,
                session_mode=futures_session_mode,
            )

            title = snapshot.futures_name or f"{stock_name}期貨"

            return _reply_with_title(
                title,
                _build_futures_flex(
                    stock_id=meta.stock_id,
                    stock_name=stock_name,
                    snapshot=snapshot,
                    current_tf=requested_tf,
                    active_session=futures_session_mode,
                ),
            )
            title = snapshot.futures_name or f"{stock_name}期貨"

            return _reply_with_title(
                title,
                _build_futures_flex(
                    stock_id=meta.stock_id,
                    stock_name=stock_name,
                    snapshot=snapshot,
                    current_tf=requested_tf,
                ),
            )

        return text_message(f"目前不支援的功能：{action}")

    except Exception as exc:
        print("controller.handle_request failed traceback:", flush=True)
        print(traceback.format_exc(), flush=True)
        return text_message(f"查詢失敗：{type(exc).__name__}: {exc}")
