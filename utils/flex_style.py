from __future__ import annotations

from typing import Any


FLEX_STYLE_VERSION = "2026-07-24-v1-STOCK-CARD-RESILIENT"

UP_COLOR = "#FF2D2D"
DOWN_COLOR = "#00B050"
FLAT_COLOR = "#666666"
ACTIVE_COLOR = "#16C957"
INACTIVE_COLOR = "#D9DDE3"
TEXT_PRIMARY = "#111111"
TEXT_SECONDARY = "#666666"
TEXT_MUTED = "#8A8A8A"
SURFACE_MUTED = "#F5F7F9"
WARNING_COLOR = "#C47A00"


def build_postback_button(
    label: str,
    data: str,
    active: bool = False,
    flex: int = 1,
    display_text: str | None = None,
    height: str = "52px",
    text_size: str = "md",
    corner_radius: str = "10px",
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": "postback",
        "label": label,
        "data": data,
    }
    if display_text:
        action["displayText"] = display_text

    return {
        "type": "box",
        "layout": "vertical",
        "flex": flex,
        "height": height,
        "cornerRadius": corner_radius,
        "backgroundColor": ACTIVE_COLOR if active else INACTIVE_COLOR,
        "justifyContent": "center",
        "alignItems": "center",
        "action": action,
        "contents": [
            {
                "type": "text",
                "text": label,
                "align": "center",
                "gravity": "center",
                "size": text_size,
                "color": "#FFFFFF" if active else TEXT_PRIMARY,
                "weight": "bold" if active else "regular",
                "wrap": True,
            }
        ],
    }


def card_context_badge(active_mode: str, current_tf: str) -> str:
    mode = str(active_mode or "").strip().lower()
    tf = str(current_tf or "").strip()
    intraday_labels = {
        "1m": "1分",
        "5m": "5分",
        "15m": "15分",
        "30m": "30分",
        "60m": "60分",
    }
    kline_labels = {
        "D": "日K",
        "W": "週K",
        "M": "月K",
    }
    mode_labels = {
        "chip": "法人",
        "large_holder": "大戶",
        "margin": "融資券",
        "financial": "財務",
        "futures": "期貨",
        "post_market": "盤後",
    }
    if mode == "instant":
        return intraday_labels.get(tf, "即時")
    if mode == "k_line":
        return kline_labels.get(tf, "K線")
    return mode_labels.get(mode, kline_labels.get(tf, tf or "觀測"))


def build_chart_fallback(
    stock_id: str,
    active_mode: str,
    current_tf: str,
) -> dict[str, Any]:
    mode = str(active_mode or "instant").strip().lower()
    tf = str(current_tf or "1m").strip()
    return {
        "type": "box",
        "layout": "vertical",
        "margin": "md",
        "paddingAll": "12px",
        "spacing": "sm",
        "cornerRadius": "10px",
        "backgroundColor": SURFACE_MUTED,
        "contents": [
            {
                "type": "text",
                "text": "圖表暫時無法載入",
                "size": "sm",
                "weight": "bold",
                "color": WARNING_COLOR,
                "align": "center",
            },
            {
                "type": "text",
                "text": "行情與更新時間仍可使用，可稍後重新載入。",
                "size": "xs",
                "color": TEXT_MUTED,
                "align": "center",
                "wrap": True,
            },
            build_postback_button(
                label="重新載入",
                data=f"{stock_id},{mode},{mode},{tf}",
                active=False,
                display_text=f"{stock_id} 重新載入",
                height="34px",
                text_size="sm",
                corner_radius="8px",
            ),
        ],
    }


def build_chart_reload_hint(
    stock_id: str,
    active_mode: str,
    current_tf: str,
) -> dict[str, Any]:
    mode = str(active_mode or "instant").strip().lower()
    tf = str(current_tf or "1m").strip()
    return {
        "type": "box",
        "layout": "horizontal",
        "margin": "xs",
        "justifyContent": "flex-end",
        "alignItems": "center",
        "contents": [
            {
                "type": "text",
                "text": "圖表未顯示？",
                "size": "xxs",
                "color": TEXT_MUTED,
                "align": "end",
                "flex": 0,
            },
            {
                "type": "text",
                "text": "重新載入",
                "size": "xxs",
                "weight": "bold",
                "color": ACTIVE_COLOR,
                "align": "end",
                "flex": 0,
                "margin": "sm",
                "action": {
                    "type": "postback",
                    "label": "重新載入",
                    "data": f"{stock_id},{mode},{mode},{tf}",
                    "displayText": f"{stock_id} 重新載入",
                },
            },
        ],
    }
