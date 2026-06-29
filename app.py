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


def extract_reply_token(payload: Dict[str, Any]) -> str:
    direct_token = payload.get("replyToken", "")

    if isinstance(direct_token, str) and direct_token.strip():
        return direct_token.strip()

    if isinstance(direct_token, list) and len(direct_token) > 0:
        return str(direct_token[0]).strip()

    events = payload.get("events", [])

    if isinstance(events, list) and len(events) > 0:
        first_event = events[0]
        if isinstance(first_event, dict):
            return first_event.get("replyToken", "")

    return ""


def make_reply_payload(message: Dict[str, Any], reply_token: str = "") -> Dict[str, Any]:
    messages = [message]

    reply_body = {
        "replyToken": reply_token,
        "messages": messages
    }

    return {
        "replyToken": reply_token,
        "messages": messages,
        "messages_json": json.dumps(messages, ensure_ascii=False),
        "reply_body_json": json.dumps(reply_body, ensure_ascii=False)
    }


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "stock-line-bot"
    }), 200


@app.route("/get_chart", methods=["POST"])
def get_chart():
    try:
        payload: Dict[str, Any] = request.get_json(force=True, silent=False) or {}

        reply_token = extract_reply_token(payload)

        bot_req = parse_make_payload(payload)

        msg = handle_request(bot_req)

        return jsonify(make_reply_payload(msg, reply_token)), 200

    except Exception as exc:
        print("ERROR in /get_chart:", str(exc))
        print(traceback.format_exc())

        error_msg = text_message(f"伺服器內部錯誤：{str(exc)}")

        reply_token = ""
        try:
            payload = request.get_json(force=True, silent=True) or {}
            reply_token = extract_reply_token(payload)
        except Exception:
            pass

        return jsonify(make_reply_payload(error_msg, reply_token)), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
