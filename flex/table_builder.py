from __future__ import annotations

from typing import Any, Dict, List

from flex.flex_builder import _button
from utils.formatter import format_int


def _header(stock_id: str, stock_name: str, title: str) -> list[dict]:
    return [
        {"type": "text", "text": f"{stock_id} {stock_name}", "weight": "bold", "size": "xl", "wrap": True},
        {"type": "text", "text": title, "weight": "bold", "size": "md", "color": "#555555", "margin": "xs"},
        {"type": "separator", "margin": "md"},
    ]


def large_holder_table(stock_id: str, stock_name: str, rows: List[dict], time_frame: str = "D") -> Dict[str, Any]:
    contents: list[dict] = _header(stock_id, stock_name, "大戶持股週報")
    for r in rows[:6]:
        diff = str(r.get("diff", "--"))
        color = "#FF3B30" if diff.startswith("+") else "#34C759" if diff.startswith("-") else "#8E8E93"
        contents.append(
            {
                "type": "text",
                "text": f"📅 {r.get('date','--')} │ 📊 {r.get('ratio','--')} │ {'📈' if diff.startswith('+') else '📉'} {diff}",
                "size": "sm",
                "color": color,
                "margin": "sm",
                "wrap": False,
            }
        )
    return _table_bubble(stock_id, stock_name, "大戶持股週報", contents, "large_holder", time_frame)


def margin_table(stock_id: str, stock_name: str, rows: List[dict], time_frame: str = "D") -> Dict[str, Any]:
    contents: list[dict] = _header(stock_id, stock_name, "融資券10日動態")
    contents.append({"type": "text", "text": "日期    │ 融資      │ 融券    │ 資券比", "size": "xs", "color": "#666666", "margin": "sm"})
    for r in rows[:10]:
        contents.append(
            {
                "type": "text",
                "text": f"{str(r.get('date','--')).ljust(6)} │ {format_int(r.get('margin')).rjust(8)} │ {format_int(r.get('short')).rjust(6)} │ {r.get('ratio','--')}",
                "size": "xs",
                "color": "#111111",
                "margin": "xs",
                "wrap": False,
            }
        )
    return _table_bubble(stock_id, stock_name, "融資券10日動態", contents, "margin", time_frame)


def _table_bubble(stock_id: str, stock_name: str, title: str, body_contents: list[dict], mode: str, time_frame: str) -> Dict[str, Any]:
    return {
        "type": "flex",
        "altText": f"{stock_id} {stock_name} {title}",
        "contents": {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical", "paddingAll": "md", "contents": body_contents},
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
                            _button("即時", f"{stock_id},instant,instant,{time_frame}", False),
                            _button("K線", f"{stock_id},k_line,k_line,{time_frame}", False),
                        ],
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "xs",
                        "contents": [
                            _button("法人", f"{stock_id},legal_person,legal_person,{time_frame}", mode == "legal_person", "sm"),
                            _button("大戶", f"{stock_id},large_holder,large_holder,{time_frame}", mode == "large_holder", "sm"),
                            _button("融資券", f"{stock_id},margin,margin,{time_frame}", mode == "margin", "sm"),
                            _button("期貨", f"{stock_id},futures,futures,{time_frame}", False, "sm"),
                        ],
                    },
                ],
            },
        },
    }
