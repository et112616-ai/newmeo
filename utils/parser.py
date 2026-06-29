from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from config import DEFAULT_MODE, DEFAULT_STOCK, DEFAULT_TIME_FRAME


@dataclass
class BotRequest:
    stock: str
    action: str
    current_mode: str
    time_frame: str
    raw_text: str = ""


def parse_postback_data(postback_str: str | None) -> Optional[Dict[str, str]]:
    """
    LINE Postback data 格式：stock,action,current_mode,time_frame
    例：2330,k_line,k_line,D
    """
    if not postback_str:
        return None
    try:
        parts = [p.strip() for p in postback_str.split(",")]
        if len(parts) != 4:
            return None
        stock, action, current_mode, time_frame = parts
        return {
            "stock": stock or DEFAULT_STOCK,
            "action": action or DEFAULT_MODE,
            "current_mode": current_mode or action or DEFAULT_MODE,
            "time_frame": time_frame or DEFAULT_TIME_FRAME,
        }
    except Exception:
        return None


def _first_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    events = payload.get("events")
    if isinstance(events, list) and events:
        return events[0] or {}
    if isinstance(events, dict):
        return events
    return {}


def parse_make_payload(payload: Dict[str, Any]) -> BotRequest:
    """
    支援兩種 Make 傳法：
    A. 建議正式版：HTTP body = {"events": [...LINE Watch Events 的 events 陣列...]}
    B. 備援簡化版：HTTP body = {"message":"2330", "postback":{"data":"2330,k_line,k_line,D"}}
    """
    # A. LINE 原始事件格式
    event = _first_event(payload)
    if event:
        postback = event.get("postback") or {}
        if isinstance(postback, dict) and postback.get("data"):
            parsed = parse_postback_data(postback.get("data"))
            if parsed:
                return BotRequest(**parsed)
            raise ValueError("無法解析按鈕資料，請確認 postback data 是否為 stock,action,current_mode,time_frame")

        message = event.get("message") or {}
        if isinstance(message, dict) and message.get("type") == "text":
            text = str(message.get("text", "")).strip()
            if not text:
                raise ValueError("收到空白文字，請輸入股票代號或名稱。")
            return BotRequest(
                stock=text,
                action="instant",
                current_mode="instant",
                time_frame="D",
                raw_text=text,
            )

        raise ValueError("不支援的 LINE 事件類型，請輸入股票代號/名稱或點擊按鈕。")

    # B. 簡化格式備援
    postback = payload.get("postback") or {}
    if isinstance(postback, dict) and postback.get("data"):
        parsed = parse_postback_data(postback.get("data"))
        if parsed:
            return BotRequest(**parsed)
        raise ValueError("無法解析按鈕資料。")

    text = str(payload.get("message") or payload.get("stock") or "").strip()
    if text:
        action = str(payload.get("action") or "instant").strip()
        current_mode = str(payload.get("current_mode") or action or "instant").strip()
        time_frame = str(payload.get("time_frame") or "D").strip()
        return BotRequest(stock=text, action=action, current_mode=current_mode, time_frame=time_frame, raw_text=text)

    raise ValueError("未接收到可解析的 message / postback / events。")
