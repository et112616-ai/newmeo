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
    LINE Postback data 格式：
    stock,action,current_mode,time_frame

    例：
    2330,k_line,k_line,D
    """
    if not postback_str:
        return None

    try:
        parts = [p.strip() for p in str(postback_str).split(",")]

        if len(parts) != 4:
            return None

        stock, action, current_mode, time_frame = parts

        action = action or DEFAULT_MODE
        current_mode = current_mode or action or DEFAULT_MODE

        return {
            "stock": stock or DEFAULT_STOCK,
            "action": action,
            "current_mode": current_mode,
            "time_frame": time_frame or DEFAULT_TIME_FRAME,
        }

    except Exception:
        return None


def _first_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    events = payload.get("events")

    if isinstance(events, list) and events:
        first = events[0]
        return first if isinstance(first, dict) else {}

    if isinstance(events, dict):
        return events

    return {}


def _first_value(value: Any) -> Any:
    """
    Make 有時會把 events[].xxx 傳成 list。
    這裡統一取第一筆。
    """
    if isinstance(value, list):
        return _first_value(value[0]) if value else ""

    return value or ""


def _dict_get_first(data: Any, key: str) -> Any:
    if not isinstance(data, dict):
        return ""

    return _first_value(data.get(key))


def parse_make_payload(payload: Dict[str, Any]) -> BotRequest:
    """
    支援兩種 Make 傳法：

    A. 正式版：
    {
      "events": [...LINE Watch Events 的 events 陣列...]
    }

    B. 簡化版：
    {
      "message": "2330",
      "postback": {
        "data": "2330,k_line,k_line,D"
      }
    }
    """

    # ============================================================
    # A. LINE 原始 events 格式
    # ============================================================
    event = _first_event(payload)

    if event:
        postback = event.get("postback") or {}

        if isinstance(postback, dict):
            postback_data = _dict_get_first(postback, "data")

            if postback_data:
                parsed = parse_postback_data(postback_data)

                if parsed:
                    return BotRequest(**parsed)

                raise ValueError(
                    "無法解析按鈕資料，請確認 postback data 是否為 stock,action,current_mode,time_frame"
                )

        message = event.get("message") or {}

        if isinstance(message, dict) and message.get("type") == "text":
            text = str(_dict_get_first(message, "text")).strip()

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

    # ============================================================
    # B. Make 簡化格式備援
    # ============================================================
    postback = payload.get("postback") or {}
    postback_data = (
        _dict_get_first(postback, "data")
        or _first_value(payload.get("postbackData"))
    )

    if postback_data:
        parsed = parse_postback_data(postback_data)

        if parsed:
            return BotRequest(**parsed)

        raise ValueError("無法解析按鈕資料。")

    text = str(
        _first_value(payload.get("message"))
        or _first_value(payload.get("stock"))
        or ""
    ).strip()

    if text:
        action = str(_first_value(payload.get("action")) or "instant").strip()
        current_mode = str(
            _first_value(payload.get("current_mode")) or action or "instant"
        ).strip()
        time_frame = str(_first_value(payload.get("time_frame")) or "D").strip()

        return BotRequest(
            stock=text,
            action=action,
            current_mode=current_mode,
            time_frame=time_frame,
            raw_text=text,
        )

    raise ValueError("未接收到可解析的 message / postback / events。")
