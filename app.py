from __future__ import annotations

import traceback
from typing import Any, Dict

from flask import Flask, jsonify, request

from config import PORT
from controller import handle_request
from flex.flex_builder import text_message
from utils.parser import parse_make_payload

app = Flask(__name__)


@app.route("/", methods=["GET"])
def health() -> tuple[dict, int]:
    return {"status": "ok", "service": "stock-line-bot"}, 200


@app.route("/get_chart", methods=["POST"])
def get_chart():
    """
    Make 4 Module 專用入口。

    建議 HTTP Body：
    {
      "events": [...LINE Watch Events 的 events 陣列...]
    }

    回傳固定：
    {
      "messages": [ LINE message object ]
    }
    """
    try:
        payload: Dict[str, Any] = request.get_json(force=True, silent=False) or {}
        bot_req = parse_make_payload(payload)
        msg = handle_request(bot_req)
        return jsonify({"messages": [msg]}), 200
    except Exception as exc:
        # 回 200 是為了避免 Make module 直接變紅；錯誤交給 LINE 文字訊息顯示。
        print("ERROR in /get_chart:", str(exc))
        print(traceback.format_exc())
        return jsonify({"messages": [text_message(f"❌ 系統執行錯誤：{str(exc)}")]}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
