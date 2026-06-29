from __future__ import annotations

import json
import traceback
from typing import Any, Dict

from flask import Flask, jsonify, request

from config import PORT
from controller import handle_request
from flex.flex_builder import text_message
from utils.parser import parse_make_payload

app = Flask(__name__)


def make_reply_payload(message: Dict[str, Any]) -> Dict[str, Any]:
    """
    同時回傳：
    1. messages：給 Make / Debug 看
    2. messages_json：給最後一顆 LINE Make an API Call 直接塞進 body
    """
    messages = [message]

    return {
        "messages": messages,
        "messages_json": json.dumps(messages, ensure_ascii=False)
    }


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "stock-line-bot"
    }), 200


@app.route("/get_chart", methods=["POST"])
def get_chart():
    """
    Make 4 Module 專用入口。

    Make HTTP Body 建議：
    {
      "events": [...]
    }

    回傳固定：
    {
      "messages": [...],
      "messages_json": "[...]"
    }
    """
    try:
        payload: Dict[str, Any] = request.get_json(force=True, silent=False) or {}

        bot_req = parse_make_payload(payload)

        msg = handle_request(bot_req)

        return jsonify(make_reply_payload(msg)), 200

    except Exception as exc:
        print("ERROR in /get_chart:", str(exc))
        print(traceback.format_exc())

        error_msg = text_message(f"伺服器內部錯誤：{str(exc)}")

        return jsonify(make_reply_payload(error_msg)), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
