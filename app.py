from __future__ import annotations
from services.futures_map_service import sync_stock_futures_map_from_taifex

import base64
import hashlib
import hmac
import json
import os
import traceback
from typing import Any, Dict

import requests
from flask import Flask, jsonify, request

from config import PORT, TDCC_SYNC_STOCKS, TDCC_SYNC_TOKEN
from controller import handle_request
from services.chip_service import sync_tdcc_latest_large_holder_many
from utils.parser import parse_make_payload


app = Flask(__name__)


LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")


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
            return str(first_event.get("replyToken", "")).strip()

    return ""


def make_reply_payload(message: Any, reply_token: str = "") -> Dict[str, Any]:
    if isinstance(message, list):
        messages = message
    else:
        messages = [message]

    messages = messages[:5]

    reply_body = {
        "replyToken": reply_token,
        "messages": messages,
    }

    return {
        "replyToken": reply_token,
        "messages": messages,
        "messages_json": json.dumps(messages, ensure_ascii=False),
        "reply_body_json": json.dumps(reply_body, ensure_ascii=False),
    }


def verify_line_signature(body: bytes, signature: str) -> bool:
    """
    驗證 LINE webhook 簽章。
    測試階段如果 LINE_CHANNEL_SECRET 沒設定，先放行。
    正式使用建議一定要設定 LINE_CHANNEL_SECRET。
    """
    if not LINE_CHANNEL_SECRET:
        print("LINE_CHANNEL_SECRET not set, skip signature verification.", flush=True)
        return True

    if not signature:
        print("Missing X-Line-Signature.", flush=True)
        return False

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()

    expected_signature = base64.b64encode(digest).decode("utf-8")

    return hmac.compare_digest(expected_signature, signature)


def reply_to_line(reply_token: str, messages: list[dict[str, Any]]) -> None:
    """
    Render 直接呼叫 LINE Reply API，不再經過 Make。
    """
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("LINE_CHANNEL_ACCESS_TOKEN not set, skip LINE reply.", flush=True)
        return

    if not reply_token:
        print("No replyToken, skip LINE reply.", flush=True)
        return

    url = "https://api.line.me/v2/bot/message/reply"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }

    body = {
        "replyToken": reply_token,
        "messages": messages[:5],
    }

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=15,
        )

        print(
            "LINE reply status:",
            resp.status_code,
            resp.text,
            flush=True,
        )

    except Exception:
        print("LINE reply failed traceback:", flush=True)
        print(traceback.format_exc(), flush=True)


@app.route("/", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "service": "stock-line-bot",
            "line_webhook": "enabled",
        }
    ), 200

@app.route("/sync_stock_futures_map", methods=["GET", "POST"])
def sync_stock_futures_map_route():
    token = request.args.get("token", "").strip()

    if not token:
        token = request.headers.get("X-Sync-Token", "").strip()

    if not TDCC_SYNC_TOKEN or token != TDCC_SYNC_TOKEN:
        return jsonify(
            {
                "status": "forbidden",
                "message": "invalid token",
            }
        ), 403

    result = sync_stock_futures_map_from_taifex()

    return jsonify(
        {
            "status": "ok" if result.get("ok") else "error",
            "result": result,
        }
    ), 200

@app.route("/sync_tdcc_large_holder", methods=["GET", "POST"])
def sync_tdcc_large_holder_route():
    token = request.args.get("token", "").strip()

    if not token:
        token = request.headers.get("X-Sync-Token", "").strip()

    if not TDCC_SYNC_TOKEN or token != TDCC_SYNC_TOKEN:
        return jsonify(
            {
                "status": "forbidden",
                "message": "invalid token",
            }
        ), 403

    stocks_param = request.args.get("stocks", "").strip()

    if not stocks_param and request.method == "POST":
        payload = request.get_json(force=True, silent=True) or {}
        stocks_param = str(payload.get("stocks", "")).strip()

    if not stocks_param:
        stocks_param = TDCC_SYNC_STOCKS

    stock_ids = [
        s.strip()
        for s in stocks_param.split(",")
        if s.strip()
    ]

    results = sync_tdcc_latest_large_holder_many(stock_ids)

    return jsonify(
        {
            "status": "ok",
            "count": len(results),
            "results": results,
        }
    ), 200


@app.route("/get_chart", methods=["POST"])
def get_chart():
    """
    保留給 Make 或測試用。
    """
    reply_token = ""

    try:
        payload: Dict[str, Any] = request.get_json(force=True, silent=False) or {}

        reply_token = extract_reply_token(payload)

        print("get_chart payload:", json.dumps(payload, ensure_ascii=False), flush=True)

        bot_req = parse_make_payload(payload)

        print(
            "parsed bot_req:",
            {
                "stock": getattr(bot_req, "stock", None),
                "action": getattr(bot_req, "action", None),
                "current_mode": getattr(bot_req, "current_mode", None),
                "time_frame": getattr(bot_req, "time_frame", None),
                "raw_text": getattr(bot_req, "raw_text", None),
            },
            flush=True,
        )

        msg = handle_request(bot_req)

        return jsonify(make_reply_payload(msg, reply_token)), 200

    except Exception as exc:
        print("get_chart failed traceback:", flush=True)
        print(traceback.format_exc(), flush=True)

        error_text = f"查詢失敗：{type(exc).__name__}: {exc}"

        return jsonify(
            make_reply_payload(
                {
                    "type": "text",
                    "text": error_text,
                },
                reply_token,
            )
        ), 200


@app.route("/line_webhook", methods=["GET", "POST"])
def line_webhook():
    """
    LINE 直接打 Render 的 webhook。

    正式流程：
    LINE -> Render /line_webhook -> handle_request -> LINE Reply API
    """

    # 讓你可以用瀏覽器測試網址是否存在
    if request.method == "GET":
        return jsonify(
            {
                "status": "ok",
                "message": "LINE webhook endpoint is ready",
            }
        ), 200

    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    try:
        if not verify_line_signature(body, signature):
            print("Invalid LINE signature", flush=True)
            return jsonify({"status": "invalid signature"}), 400

        payload: Dict[str, Any] = request.get_json(force=True, silent=True) or {}

        print(
            "line_webhook payload:",
            json.dumps(payload, ensure_ascii=False),
            flush=True,
        )

        events = payload.get("events", [])

        # LINE Verify 可能送空 events。
        # 回 200 讓 Verify 通過。
        if not events:
            return jsonify({"status": "ok", "message": "no events"}), 200

        for event in events:
            reply_token = str(event.get("replyToken", "")).strip()

            bot_payload = {
                "events": [event],
            }

            try:
                bot_req = parse_make_payload(bot_payload)

                print(
                    "line parsed bot_req:",
                    {
                        "stock": getattr(bot_req, "stock", None),
                        "action": getattr(bot_req, "action", None),
                        "current_mode": getattr(bot_req, "current_mode", None),
                        "time_frame": getattr(bot_req, "time_frame", None),
                        "raw_text": getattr(bot_req, "raw_text", None),
                    },
                    flush=True,
                )

                msg = handle_request(bot_req)

                if isinstance(msg, list):
                    messages = msg
                else:
                    messages = [msg]

                reply_to_line(reply_token, messages)

            except Exception:
                print("line_webhook event failed traceback:", flush=True)
                print(traceback.format_exc(), flush=True)

                reply_to_line(
                    reply_token,
                    [
                        {
                            "type": "text",
                            "text": "查詢失敗，請稍後再試。",
                        }
                    ],
                )

        return jsonify({"status": "ok"}), 200

    except Exception:
        print("line_webhook failed traceback:", flush=True)
        print(traceback.format_exc(), flush=True)

        # 先回 200，避免 LINE 一直重送
        return jsonify({"status": "error"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
