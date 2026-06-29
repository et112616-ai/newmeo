from __future__ import annotations

from typing import Any, Dict

from utils.formatter import trend_color


def text_message(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text[:4900]}


def _button(label: str, data: str, primary: bool = False, height: str | None = None) -> Dict[str, Any]:
    btn = {
        "type": "button",
        "style": "primary" if primary else "secondary",
        "action": {"type": "postback", "label": label, "data": data},
    }
    if height:
        btn["height"] = height
    return btn


def image_dashboard(
    *,
    stock_id: str,
    stock_name: str,
    image_url: str,
    current_mode: str,
    time_frame: str,
    price_info: str = "--",
    change_info: str = "--",
    time_stamp: str = "--",
    price_change: float | None = None,
    title: str = "觀測儀表板",
) -> Dict[str, Any]:
    """LINE Flex Bubble：圖片型主畫面。"""
    tf_buttons = [
        _button("1分", f"{stock_id},{current_mode},{current_mode},1m", time_frame == "1m", "sm"),
        _button("5分", f"{stock_id},{current_mode},{current_mode},5m", time_frame == "5m", "sm"),
        _button("D", f"{stock_id},{current_mode},{current_mode},D", time_frame == "D", "sm"),
        _button("W", f"{stock_id},{current_mode},{current_mode},W", time_frame == "W", "sm"),
        _button("M", f"{stock_id},{current_mode},{current_mode},M", time_frame == "M", "sm"),
    ]

    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} {title}",
        "contents": {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "md",
                "contents": [
                    {
                        "type": "box",
                        "layout": "vertical",
                        "contents": [
                            {"type": "text", "text": f"{stock_id} {stock_name}", "weight": "bold", "size": "xl", "color": "#111111", "wrap": True},
                            {"type": "text", "text": f"{price_info}  ({change_info})", "weight": "bold", "size": "md", "color": trend_color(price_change), "margin": "xs"},
                            {"type": "text", "text": f"更新時間：{time_stamp}", "size": "xs", "color": "#8E8E93", "margin": "xs"},
                        ],
                    },
                    {"type": "separator", "margin": "md"},
                    {"type": "box", "layout": "horizontal", "spacing": "xs", "margin": "md", "contents": tf_buttons},
                    {
                        "type": "box",
                        "layout": "vertical",
                        "margin": "md",
                        "backgroundColor": "#FFFFFF",
                        "cornerRadius": "md",
                        "borderWidth": "1px",
                        "borderColor": "#E5E5EA",
                        "contents": [
                            {"type": "image", "url": image_url, "size": "full", "aspectMode": "fit", "aspectRatio": "4:3"}
                        ],
                    },
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "sm",
                        "contents": [
                            _button("即時", f"{stock_id},instant,instant,{time_frame}", current_mode == "instant"),
                            _button("K線", f"{stock_id},k_line,k_line,{time_frame}", current_mode == "k_line"),
                        ],
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "xs",
                        "contents": [
                            _button("法人", f"{stock_id},legal_person,legal_person,{time_frame}", current_mode == "legal_person", "sm"),
                            _button("大戶", f"{stock_id},large_holder,large_holder,{time_frame}", current_mode == "large_holder", "sm"),
                            _button("融資券", f"{stock_id},margin,margin,{time_frame}", current_mode == "margin", "sm"),
                            _button("期貨", f"{stock_id},futures,futures,{time_frame}", current_mode == "futures", "sm"),
                        ],
                    },
                ],
            },
        },
    }


def futures_notice(stock_id: str, stock_name: str) -> Dict[str, Any]:
    return text_message(
        f"⚠️ {stock_id} {stock_name} 的期貨資料第一版先保留按鈕。\n"
        "台灣個股期貨在 yfinance 並沒有穩定代碼，建議第二版改接期交所或券商 API。"
    )
