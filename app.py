from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import os
import platform
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(__file__).resolve().parent / ".mplconfig"),
)

APP_BUILD_VERSION = "2026-07-21-v1.8-PREDICTION-FINMIND-BACKFILL"
APP_STARTED_TS = time.time()

print(
    "APP BOOT VERSION",
    APP_BUILD_VERSION,
    "| file =",
    __file__,
    flush=True,
)

from services.futures_map_service import sync_stock_futures_map_from_taifex
from services.market_future_service import get_market_future_snapshot

_MARKET_INDEX_FN = None
_MARKET_INDEX_IMPORT_LOCK = threading.Lock()


def get_market_index_snapshot(*args, **kwargs):
    global _MARKET_INDEX_FN

    if _MARKET_INDEX_FN is None:
        with _MARKET_INDEX_IMPORT_LOCK:
            if _MARKET_INDEX_FN is None:
                started = time.perf_counter()
                module = importlib.import_module("services.market_index_service")
                _MARKET_INDEX_FN = getattr(module, "get_market_index_snapshot")
                print(
                    "DEBUG lazy market_index_service import",
                    "| sec =",
                    round(time.perf_counter() - started, 3),
                    flush=True,
                )

    return _MARKET_INDEX_FN(*args, **kwargs)

# 完全容錯匯入 quote service：
# 1. 不直接 from ... import 版本常數。
# 2. 舊版沒有健康函式時仍可啟動。
# 3. quote service 自身暫時匯入失敗時，Flask 仍可先啟動並由 /health 顯示錯誤。
_SINOPAC_QUOTE_IMPORT_ERROR = ""

try:
    _sinopac_quote_service = importlib.import_module(
        "services.sinopac_quote_service"
    )
except Exception as exc:
    _sinopac_quote_service = None
    _SINOPAC_QUOTE_IMPORT_ERROR = repr(exc)
    print(
        "APP QUOTE SERVICE IMPORT FAILED",
        "| version =",
        APP_BUILD_VERSION,
        "| error =",
        _SINOPAC_QUOTE_IMPORT_ERROR,
        flush=True,
    )


def _quote_attr(name: str, default: Any = None) -> Any:
    if _sinopac_quote_service is None:
        return default

    try:
        return getattr(_sinopac_quote_service, name, default)
    except Exception:
        return default


QUOTE_SERVICE_VERSION = str(
    _quote_attr(
        "QUOTE_SERVICE_VERSION",
        _quote_attr(
            "INTRADAY_UNIFIED_FIX_VERSION",
            "legacy-unversioned",
        ),
    )
)


def get_api():
    fn = _quote_attr("get_api")

    if not callable(fn):
        return None

    try:
        return fn()
    except Exception:
        print("APP compatibility get_api failed", flush=True)
        print(traceback.format_exc(), flush=True)
        return None


def get_stock_snapshot(stock_id: str):
    fn = _quote_attr("get_stock_snapshot")

    if not callable(fn):
        return None

    try:
        return fn(stock_id)
    except Exception:
        print("APP compatibility get_stock_snapshot failed", stock_id, flush=True)
        print(traceback.format_exc(), flush=True)
        return None


def get_shioaji_status() -> dict[str, Any]:
    fn = _quote_attr("get_shioaji_status")

    if callable(fn):
        try:
            status = fn()
            if isinstance(status, dict):
                return status
        except Exception as exc:
            return {
                "ready": False,
                "quote_service_version": QUOTE_SERVICE_VERSION,
                "last_login_error": repr(exc),
                "compatibility_mode": True,
            }

    ready_fn = _quote_attr("is_shioaji_api_ready")
    ready = False

    if callable(ready_fn):
        try:
            ready = bool(ready_fn())
        except Exception:
            ready = False

    return {
        "ready": ready,
        "quote_service_version": QUOTE_SERVICE_VERSION,
        "last_login_success": "",
        "last_login_error": _SINOPAC_QUOTE_IMPORT_ERROR,
        "consecutive_failures": 0,
        "reconnect_monitor_alive": False,
        "compatibility_mode": True,
        "quote_module_loaded": _sinopac_quote_service is not None,
    }


def warmup_shioaji_once(force_reconnect: bool = False) -> dict[str, Any]:
    fn = _quote_attr("warmup_shioaji_once")

    if callable(fn):
        try:
            status = fn(force_reconnect=force_reconnect)
            if isinstance(status, dict):
                return status
        except TypeError:
            try:
                status = fn()
                if isinstance(status, dict):
                    return status
            except Exception:
                pass
        except Exception:
            pass

    started = time.perf_counter()
    api = get_api()
    status = get_shioaji_status()
    status["ready"] = bool(api is not None)
    status["warmup_seconds"] = round(time.perf_counter() - started, 3)
    status["compatibility_mode"] = True
    return status


def start_shioaji_reconnect_monitor() -> bool:
    fn = _quote_attr("start_shioaji_reconnect_monitor")

    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            print("DEBUG compatibility reconnect monitor failed", flush=True)
            print(traceback.format_exc(), flush=True)

    return False


from config import PORT, TDCC_SYNC_STOCKS, TDCC_SYNC_TOKEN
from services.chip_service import sync_tdcc_latest_large_holder_many
from utils.parser import parse_make_payload

_HANDLE_REQUEST_FN = None
_CONTROLLER_IMPORT_LOCK = threading.Lock()


def _load_handle_request():
    global _HANDLE_REQUEST_FN

    if _HANDLE_REQUEST_FN is None:
        with _CONTROLLER_IMPORT_LOCK:
            if _HANDLE_REQUEST_FN is None:
                started = time.perf_counter()
                module = importlib.import_module("controller")
                _HANDLE_REQUEST_FN = getattr(module, "handle_request")
                print(
                    "DEBUG lazy controller import",
                    "| sec =",
                    round(time.perf_counter() - started, 3),
                    flush=True,
                )

    return _HANDLE_REQUEST_FN


def handle_request(bot_req):
    return _load_handle_request()(bot_req)

_LINE_EVENT_SEEN: dict[str, float] = {}
_LINE_EVENT_SEEN_TTL_SECONDS = 180


def _line_should_process_event(event: dict) -> bool:
    """
    避免同一個 webhookEventId 被重複處理。
    但不要因為 isRedelivery=True 就直接丟掉。

    LINE redelivery 的 webhookEventId 和 replyToken 會跟原事件相同；
    所以用 webhookEventId 去重比較安全。
    """
    event_id = str(event.get("webhookEventId") or "").strip()

    if not event_id:
        return True

    now = time.time()

    expired_ids = [
        k
        for k, ts in list(_LINE_EVENT_SEEN.items())
        if now - ts > _LINE_EVENT_SEEN_TTL_SECONDS
    ]

    for k in expired_ids:
        _LINE_EVENT_SEEN.pop(k, None)

    if event_id in _LINE_EVENT_SEEN:
        print(
            "LINE duplicate event ignored:",
            event_id,
            "| isRedelivery =",
            event.get("deliveryContext", {}).get("isRedelivery"),
            flush=True,
        )
        return False

    _LINE_EVENT_SEEN[event_id] = now
    return True

app = Flask(__name__)


@app.after_request
def _add_chart_cache_headers(response):
    """唯一檔名圖表可長效快取，避免每台 LINE 裝置重複回源 Render。"""
    try:
        if request.path.startswith("/static/charts/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            response.headers["X-Content-Type-Options"] = "nosniff"
    except Exception:
        pass
    return response


@app.route("/route_probe", methods=["GET"])
def route_probe():
    return jsonify({
        "status": "ok",
        "message": "route_probe registered",
    }), 200



def _server_time_text() -> str:
    try:
        return datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _collect_module_versions(module_name: str) -> dict[str, str]:
    """收集模組內所有 *_VERSION 常數，避免人工維護版本清單。"""
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        return {"import_error": repr(exc)}

    versions: dict[str, str] = {}

    for name in sorted(dir(module)):
        if not name.endswith("_VERSION"):
            continue

        try:
            value = getattr(module, name)
        except Exception:
            continue

        if isinstance(value, (str, int, float, bool)):
            versions[name] = str(value)

    return versions or {"version": "unversioned"}


def _check_chart_directory() -> dict[str, Any]:
    chart_dir = Path(__file__).resolve().parent / "static" / "charts"

    try:
        chart_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="health_",
            suffix=".tmp",
            dir=str(chart_dir),
            delete=False,
            encoding="utf-8",
        ) as fp:
            fp.write("ok")
            probe_path = Path(fp.name)

        probe_path.unlink(missing_ok=True)

        return {
            "ok": True,
            "path": str(chart_dir),
            "writable": True,
        }

    except Exception as exc:
        return {
            "ok": False,
            "path": str(chart_dir),
            "writable": False,
            "error": repr(exc),
        }


def _version_payload() -> dict[str, Any]:
    modules = {
        "controller": _collect_module_versions("controller"),
        "chart_service": _collect_module_versions("services.chart_service"),
        "sinopac_quote_service": _collect_module_versions("services.sinopac_quote_service"),
        "market_index_service": _collect_module_versions("services.market_index_service"),
        "market_turnover_service": _collect_module_versions("services.market_turnover_service"),
        "futures_service": _collect_module_versions("services.futures_service"),
        "market_future_service": _collect_module_versions("services.market_future_service"),
        "market_future_kline_service": _collect_module_versions("services.market_future_kline_service"),
    }

    return {
        "service": "stock-line-bot",
        "app_build_version": APP_BUILD_VERSION,
        "quote_service_version": QUOTE_SERVICE_VERSION,
        "render_git_commit": str(os.getenv("RENDER_GIT_COMMIT", "") or ""),
        "render_service_id": str(os.getenv("RENDER_SERVICE_ID", "") or ""),
        "python": platform.python_version(),
        "pid": os.getpid(),
        "server_time": _server_time_text(),
        "uptime_seconds": round(time.time() - APP_STARTED_TS, 3),
        "modules": modules,
    }


@app.route("/version", methods=["GET"])
def version_info():
    return jsonify(_version_payload()), 200


@app.route("/health", methods=["GET"])
def health_detail():
    shioaji_status = get_shioaji_status()
    chart_status = _check_chart_directory()
    deep = str(request.args.get("deep", "0") or "0").strip() == "1"

    market_index_status: dict[str, Any] = {
        "checked": False,
        "message": "Use /health?deep=1 to run a live market-index check.",
    }

    if deep:
        started = time.perf_counter()
        try:
            snapshot = get_market_index_snapshot(with_chart=False)
            market_index_status = {
                "checked": True,
                "available": bool(getattr(snapshot, "available", False)),
                "close": getattr(snapshot, "close_price", None),
                "quote_time": str(getattr(snapshot, "quote_time", "") or ""),
                "seconds": round(time.perf_counter() - started, 3),
            }
        except Exception as exc:
            market_index_status = {
                "checked": True,
                "available": False,
                "error": repr(exc),
                "seconds": round(time.perf_counter() - started, 3),
            }

    if not chart_status.get("ok"):
        overall = "error"
    elif not shioaji_status.get("ready"):
        overall = "degraded"
    elif deep and not market_index_status.get("available"):
        overall = "degraded"
    else:
        overall = "ok"

    payload = {
        "status": overall,
        "service": "stock-line-bot",
        "app_build_version": APP_BUILD_VERSION,
        "server_time": _server_time_text(),
        "uptime_seconds": round(time.time() - APP_STARTED_TS, 3),
        "line_webhook": "enabled",
        "thread_count": threading.active_count(),
        "shioaji": shioaji_status,
        "chart_directory": chart_status,
        "market_index": market_index_status,
    }

    # degraded 仍回 200，避免 Render 因外部行情暫時異常重啟服務。
    return jsonify(payload), 200


LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")


def _get_internal_token_from_request() -> str:
    token = request.args.get("token", "").strip()

    if not token:
        token = request.headers.get("X-Sync-Token", "").strip()

    return token


def _check_internal_token() -> bool:
    expected = str(os.getenv("TDCC_SYNC_TOKEN", "") or "").strip()
    token = _get_internal_token_from_request()

    return bool(expected and token and token == expected)


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


def reply_to_line(reply_token: str, messages: list[dict[str, Any]]) -> bool:
    """
    Render 直接呼叫 LINE Reply API，不再經過 Make。
    """
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("LINE_CHANNEL_ACCESS_TOKEN not set, skip LINE reply.", flush=True)
        return False

    if not reply_token:
        print("No replyToken, skip LINE reply.", flush=True)
        return False

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
        return 200 <= resp.status_code < 300

    except Exception:
        print("LINE reply failed traceback:", flush=True)
        print(traceback.format_exc(), flush=True)
        return False


def push_to_line(target_id: str, messages: list[dict[str, Any]]) -> bool:
    """使用 push API 傳送完成結果，不依賴可能失效的 replyToken。"""
    target_id = str(target_id or "").strip()

    if not LINE_CHANNEL_ACCESS_TOKEN or not target_id:
        print("LINE push skipped: missing token or target_id", flush=True)
        return False

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    body = {
        "to": target_id,
        "messages": messages[:5],
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=15)
        print("LINE push status:", resp.status_code, resp.text, flush=True)
        return 200 <= resp.status_code < 300
    except Exception:
        print("LINE push failed traceback:", flush=True)
        print(traceback.format_exc(), flush=True)
        return False


def _line_target_id(event: dict[str, Any]) -> str:
    source = event.get("source") if isinstance(event, dict) else {}

    if not isinstance(source, dict):
        return ""

    for key in ("userId", "groupId", "roomId"):
        value = str(source.get(key) or "").strip()
        if value:
            return value

    return ""


def _process_line_event_async(event: dict[str, Any]) -> None:
    """在背景 thread 執行查詢；使用者事件優先 Reply，失敗才 Push。"""
    event_id = str(event.get("webhookEventId") or "").strip()
    target_id = _line_target_id(event)
    reply_token = str(event.get("replyToken") or "").strip()
    started = time.perf_counter()

    try:
        bot_req = parse_make_payload({"events": [event]})

        print(
            "line async parsed bot_req:",
            {
                "event_id": event_id,
                "stock": getattr(bot_req, "stock", None),
                "action": getattr(bot_req, "action", None),
                "current_mode": getattr(bot_req, "current_mode", None),
                "time_frame": getattr(bot_req, "time_frame", None),
                "raw_text": getattr(bot_req, "raw_text", None),
            },
            flush=True,
        )

        msg = handle_request(bot_req)
        messages = msg if isinstance(msg, list) else [msg]

        # Reply 不計入 LINE OA 每月訊息數，且目前完整查詢約 5~7 秒，
        # 通常仍在 replyToken 可用時間內。只有 Reply 失敗才退回 Push。
        sent = reply_to_line(reply_token, messages) if reply_token else False

        if not sent:
            sent = push_to_line(target_id, messages)

        print(
            "LINE async event complete",
            "| event_id =", event_id,
            "| target =", bool(target_id),
            "| elapsed_sec =", round(time.perf_counter() - started, 3),
            flush=True,
        )

    except Exception:
        print("LINE async event failed traceback:", flush=True)
        print(traceback.format_exc(), flush=True)

        error_messages = [{"type": "text", "text": "查詢失敗，請稍後再試。"}]

        sent = reply_to_line(reply_token, error_messages) if reply_token else False
        if not sent:
            push_to_line(target_id, error_messages)


_BACKGROUND_WARMUP_STARTED = False
_BACKGROUND_WARMUP_LOCK = threading.Lock()
_BACKGROUND_WARMUP_PID = 0


def _background_shioaji_warmup() -> None:
    """Gunicorn worker 啟動後背景登入 Shioaji，避免第一位使用者承擔冷登入。"""
    delay = float(os.getenv("SHIOAJI_WARMUP_DELAY_SECONDS", "1") or 1)
    time.sleep(max(0.0, delay))

    try:
        status = warmup_shioaji_once(force_reconnect=False)
        print(
            "DEBUG background shioaji warmup",
            "| ok =", status.get("ready"),
            "| sec =", status.get("warmup_seconds"),
            "| last_success =", status.get("last_login_success"),
            "| error =", status.get("last_login_error"),
            flush=True,
        )
    except Exception:
        print("DEBUG background shioaji warmup failed", flush=True)
        print(traceback.format_exc(), flush=True)

    if str(os.getenv("ENABLE_BACKGROUND_CONTROLLER_WARMUP", "1")).strip() == "1":
        try:
            started = time.perf_counter()
            _load_handle_request()
            print(
                "DEBUG background controller warmup",
                "| ok = True",
                "| sec =",
                round(time.perf_counter() - started, 3),
                flush=True,
            )
        except Exception:
            print("DEBUG background controller warmup failed", flush=True)
            print(traceback.format_exc(), flush=True)


def _start_background_warmup_once() -> None:
    global _BACKGROUND_WARMUP_STARTED, _BACKGROUND_WARMUP_PID

    current_pid = os.getpid()

    # Gunicorn --preload：Master import 時的旗標會被 fork 到 Worker，
    # 但 Master 建立的 thread 不會存活。PID 改變時必須重設一次。
    with _BACKGROUND_WARMUP_LOCK:
        if _BACKGROUND_WARMUP_PID != current_pid:
            _BACKGROUND_WARMUP_PID = current_pid
            _BACKGROUND_WARMUP_STARTED = False

    try:
        start_shioaji_reconnect_monitor()
    except Exception:
        print("DEBUG shioaji reconnect monitor start failed", flush=True)
        print(traceback.format_exc(), flush=True)

    enabled = str(os.getenv("ENABLE_BACKGROUND_SHIOAJI_WARMUP", "1")).strip() == "1"

    if not enabled:
        return

    with _BACKGROUND_WARMUP_LOCK:
        if _BACKGROUND_WARMUP_STARTED:
            return

        _BACKGROUND_WARMUP_STARTED = True
        threading.Thread(
            target=_background_shioaji_warmup,
            name="shioaji-warmup",
            daemon=True,
        ).start()


@app.before_request
def _ensure_worker_background_warmup():
    """第一個 Worker request 非阻塞啟動行情預熱，兼容 Gunicorn --preload。"""
    _start_background_warmup_once()

@app.route("/warmup_all", methods=["GET"])
def warmup_all():
    """
    預熱 LINE Bot 常用資料。

    目標：
    - 讓 Render 不冷啟動
    - 先執行 Shioaji get_api()，把 login / contracts 成本提前
    - 預先生大盤 K 線圖
    - 預先查台指期與常用股票 snapshot
    """

    token = request.args.get("token", "")

    if TDCC_SYNC_TOKEN and token != TDCC_SYNC_TOKEN:
        return jsonify(
            {
                "ok": False,
                "message": "unauthorized",
            }
        ), 401

    import os
    import time

    t0 = time.perf_counter()

    result = {
        "ok": True,
        "items": {},
        "stocks": {},
    }

    # -------------------------
    # 0. Shioaji 明確登入 / contracts 預載
    # -------------------------
    try:
        t = time.perf_counter()

        shioaji_status = warmup_shioaji_once(force_reconnect=False)

        result["items"]["shioaji_login"] = {
            "ok": bool(shioaji_status.get("ready")),
            "seconds": round(time.perf_counter() - t, 3),
            "last_login_success": shioaji_status.get("last_login_success"),
            "last_login_error": shioaji_status.get("last_login_error"),
            "reconnect_monitor_alive": shioaji_status.get("reconnect_monitor_alive"),
        }

    except Exception as exc:
        result["items"]["shioaji_login"] = {
            "ok": False,
            "error": str(exc),
        }

    # -------------------------
    # 1. 大盤：即時 + K線圖
    # -------------------------
    try:
        t = time.perf_counter()

        snapshot = get_market_index_snapshot(with_chart=True)

        result["items"]["market_index"] = {
            "ok": bool(getattr(snapshot, "available", False)),
            "chart_url": bool(getattr(snapshot, "chart_url", "")),
            "quote_time": str(getattr(snapshot, "quote_time", "") or ""),
            "seconds": round(time.perf_counter() - t, 3),
        }

    except Exception as exc:
        result["items"]["market_index"] = {
            "ok": False,
            "error": str(exc),
        }

    # -------------------------
    # 2. 台指期：日盤
    # -------------------------
    try:
        t = time.perf_counter()

        snapshot = get_market_future_snapshot(session_mode="day")

        result["items"]["market_future_day"] = {
            "ok": bool(getattr(snapshot, "available", False)),
            "contract_code": str(getattr(snapshot, "contract_code", "") or ""),
            "quote_time": str(getattr(snapshot, "quote_time", "") or ""),
            "seconds": round(time.perf_counter() - t, 3),
        }

    except Exception as exc:
        result["items"]["market_future_day"] = {
            "ok": False,
            "error": str(exc),
        }

    # -------------------------
    # 3. 台指期：全盤
    # -------------------------
    try:
        t = time.perf_counter()

        snapshot = get_market_future_snapshot(session_mode="all")

        result["items"]["market_future_all"] = {
            "ok": bool(getattr(snapshot, "available", False)),
            "contract_code": str(getattr(snapshot, "contract_code", "") or ""),
            "quote_time": str(getattr(snapshot, "quote_time", "") or ""),
            "seconds": round(time.perf_counter() - t, 3),
        }

    except Exception as exc:
        result["items"]["market_future_all"] = {
            "ok": False,
            "error": str(exc),
        }

    # -------------------------
    # 4. 常用個股 snapshot
    # -------------------------
    stocks_text = (
        request.args.get("stocks")
        or os.getenv(
            "WARMUP_STOCKS",
            "2330,2303,2408,2313,2301,2634,0052,009816",
        )
    )

    stock_ids = []

    for item in str(stocks_text or "").replace("，", ",").split(","):
        sid = item.strip()

        if sid and sid not in stock_ids:
            stock_ids.append(sid)

    for sid in stock_ids:
        try:
            t = time.perf_counter()

            snapshot = get_stock_snapshot(sid)

            result["stocks"][sid] = {
                "ok": bool(snapshot),
                "close": snapshot.get("close") if isinstance(snapshot, dict) else None,
                "quote_time": snapshot.get("ts") if isinstance(snapshot, dict) else "",
                "seconds": round(time.perf_counter() - t, 3),
            }

        except Exception as exc:
            result["stocks"][sid] = {
                "ok": False,
                "error": str(exc),
            }

    result["total_seconds"] = round(time.perf_counter() - t0, 3)

    print(
        "DEBUG warmup_all",
        "| total_seconds =",
        result["total_seconds"],
        "| shioaji_login =",
        result["items"].get("shioaji_login"),
        "| market_index =",
        result["items"].get("market_index"),
        "| market_future_day =",
        result["items"].get("market_future_day"),
        "| stocks_count =",
        len(stock_ids),
        flush=True,
    )

    return jsonify(result)

@app.route("/", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "service": "stock-line-bot",
            "app_build_version": APP_BUILD_VERSION,
            "line_webhook": "enabled",
            "version_url": "/version",
            "health_url": "/health",
            "server_time": _server_time_text(),
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

@app.route("/debug_routes_public", methods=["GET"])
def debug_routes_public():
    routes = []

    for rule in app.url_map.iter_rules():
        routes.append(str(rule))

    return jsonify({
        "status": "ok",
        "routes": sorted(routes),
    }), 200

@app.route("/sync_financial", methods=["GET", "POST"])
def sync_financial():
    if not _check_internal_token():
        return jsonify({"status": "forbidden"}), 403

    try:
        from services.financial_service import sync_stock_financial_quarterly
    except Exception as exc:
        print(
            "DEBUG sync_financial import failed",
            repr(exc),
            flush=True,
        )
        return jsonify({
            "status": "error",
            "message": "financial_service import failed",
            "error": repr(exc),
        }), 500

    stock_id = request.args.get("stock_id", "").strip()
    stock_name = request.args.get("stock_name", "").strip()
    start_date = request.args.get("start_date", "").strip()

    if not stock_id:
        return jsonify({
            "status": "error",
            "message": "missing stock_id",
        }), 400

    try:
        result = sync_stock_financial_quarterly(
            stock_id=stock_id,
            stock_name=stock_name,
            start_date=start_date,
        )

        return jsonify({
            "status": "ok",
            "result": result,
        }), 200

    except Exception as exc:
        import traceback

        print(
            "DEBUG sync_financial failed",
            repr(exc),
            flush=True,
        )
        print(traceback.format_exc(), flush=True)

        return jsonify({
            "status": "error",
            "message": "sync financial failed",
            "error": repr(exc),
        }), 500


@app.route("/sync_market_prediction_data", methods=["GET", "POST"])
def sync_market_prediction_data_route():
    """抓取並檢查 TAIEX/TXF 對齊資料；persist=1 時寫入 Supabase。"""
    if not _check_internal_token():
        return jsonify({"ok": False, "message": "invalid token"}), 403

    start_date = str(request.args.get("start_date", "") or "").strip() or None
    end_date = str(request.args.get("end_date", "") or "").strip() or None
    persist = str(request.args.get("persist", "0") or "0").strip().lower() in {
        "1", "true", "yes", "y", "on",
    }

    started = time.perf_counter()

    try:
        # 僅在維運端點被呼叫時載入，避免大盤繪圖服務拖慢 Gunicorn cold boot。
        module = importlib.import_module("services.market_prediction_data_service")
        sync_fn = getattr(module, "sync_market_prediction_data")
        result = sync_fn(
            start_date=start_date,
            end_date=end_date,
            persist=persist,
        )

        print(
            "MARKET_PREDICTION_DATA_ROUTE",
            "| ok =", result.get("ok"),
            "| start =", result.get("start_date"),
            "| end =", result.get("end_date"),
            "| rows =", (result.get("quality") or {}).get("aligned_rows"),
            "| persist =", (result.get("persist") or {}).get("success"),
            "| sec =", round(time.perf_counter() - started, 3),
            flush=True,
        )
        return jsonify(result), 200 if result.get("ok") else 422

    except Exception as exc:
        print(
            "MARKET_PREDICTION_DATA_ROUTE failed",
            "| error =", repr(exc),
            "| sec =", round(time.perf_counter() - started, 3),
            flush=True,
        )
        print(traceback.format_exc(), flush=True)
        return jsonify({
            "ok": False,
            "message": "market prediction data sync failed",
            "error": repr(exc),
        }), 500

@app.get("/sync_tdcc_large_holder")
def sync_tdcc_large_holder_route():
    route_t0 = time.perf_counter()
    request_id = f"tdcc-{int(time.time())}-{threading.get_ident()}"
    token = str(request.args.get("token", "") or "").strip()
    expected_token = str(os.getenv("TDCC_SYNC_TOKEN", "") or "").strip()

    if expected_token and token != expected_token:
        return jsonify(
            {
                "ok": False,
                "message": "invalid token",
            }
        ), 403

    stocks = str(request.args.get("stocks", "") or "").strip()

    if not stocks:
        stocks = str(os.getenv("TDCC_SYNC_STOCKS", "") or "").strip()

    # 先預設你常查的股票；之後可改 TDCC_SYNC_STOCKS。
    if not stocks:
        stocks = "2317,2330,2327,3264"

    stock_list = []
    for item in stocks.replace("，", ",").split(","):
        sid = str(item or "").strip().replace(".TW", "").replace(".TWO", "")
        if sid and sid not in stock_list:
            stock_list.append(sid)

    mode = str(request.args.get("mode", "latest") or "latest").strip().lower()
    history = mode in {"history", "backfill", "full"}
    start_date = str(request.args.get("start_date", "") or "").strip() or None

    try:
        max_weeks = max(1, min(int(request.args.get("max_weeks", 8) or 8), 12))
    except Exception:
        max_weeks = 8

    try:
        offset = max(0, int(request.args.get("offset", 0) or 0))
    except Exception:
        offset = 0

    try:
        default_limit = 1 if history else 20
        limit = max(1, min(int(request.args.get("limit", default_limit) or default_limit), 50))
    except Exception:
        limit = 1 if history else 20

    batch_stocks = stock_list[offset : offset + limit]
    next_offset = offset + len(batch_stocks)
    has_more = next_offset < len(stock_list)

    print(
        "TDCC_ROUTE start",
        "| request_id =", request_id,
        "| mode =", "history" if history else "latest",
        "| total_stocks =", len(stock_list),
        "| offset =", offset,
        "| limit =", limit,
        "| batch =", batch_stocks,
        flush=True,
    )

    try:
        result = sync_tdcc_latest_large_holder_many(
            batch_stocks,
            history=history,
            start_date=start_date,
            max_weeks=max_weeks,
        )
        result["request_id"] = request_id
        result["batch"] = {
            "offset": offset,
            "limit": limit,
            "processed": len(batch_stocks),
            "total_stocks": len(stock_list),
            "next_offset": next_offset if has_more else None,
            "has_more": has_more,
        }
        result["route_seconds"] = round(time.perf_counter() - route_t0, 3)

        print(
            "TDCC_ROUTE done",
            "| request_id =", request_id,
            "| ok =", bool(result.get("ok")),
            "| success =", result.get("success", 0),
            "| failed =", result.get("failed", 0),
            "| has_more =", has_more,
            "| sec =", result["route_seconds"],
            flush=True,
        )
        return jsonify(result), 200

    except Exception as exc:
        print(
            "TDCC_ROUTE failed",
            "| request_id =", request_id,
            "| error =", repr(exc),
            "| sec =", round(time.perf_counter() - route_t0, 3),
            flush=True,
        )
        print(traceback.format_exc(), flush=True)
        return jsonify(
            {
                "ok": False,
                "request_id": request_id,
                "message": "TDCC sync failed",
                "error": repr(exc),
            }
        ), 500


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

@app.route("/sync_broker_branch_csv", methods=["POST"])
def sync_broker_branch_csv_route():
    if not _check_internal_token():
        return jsonify({"status": "forbidden"}), 403

    try:
        from services.broker_branch_service import sync_broker_branch_csv

        stock_id = request.args.get("stock_id", "").strip()
        trade_date = request.args.get("trade_date", "").strip()
        source = request.args.get("source", "MANUAL_CSV").strip()

        csv_text = ""

        if upload:
            raw = upload.read()

            csv_text = ""

            for encoding in ["utf-8-sig", "utf-16", "cp950", "big5", "utf-8"]:
                try:
                    csv_text = raw.decode(encoding)
                    if csv_text.strip():
                        print(
                            "DEBUG broker csv decoded",
                            "| encoding =",
                            encoding,
                            "| chars =",
                            len(csv_text),
                            "| preview =",
                            csv_text[:120].replace("\n", "\\n"),
                            flush=True,
                        )
                        break
                except Exception:
                    continue

            if not csv_text.strip():
                csv_text = raw.decode("utf-8", errors="ignore")

        elif request.is_json:
            payload = request.get_json(silent=True) or {}
            csv_text = (
                payload.get("csv_text")
                or payload.get("csv")
                or payload.get("text")
                or ""
            )

        else:
            csv_text = request.get_data(as_text=True) or ""

        if not csv_text.strip():
            return jsonify({
                "status": "error",
                "message": "CSV text or file is required.",
            }), 400

        result = sync_broker_branch_csv(
            csv_text=csv_text,
            stock_id=stock_id,
            trade_date=trade_date,
            source=source,
        )

        return jsonify({
            "status": "ok" if result.get("ok") else "error",
            "result": result,
        }), 200

    except Exception as exc:
        import traceback

        print(traceback.format_exc(), flush=True)

        return jsonify({
            "status": "error",
            "message": repr(exc),
        }), 500

@app.route("/line_webhook", methods=["GET", "POST"])
def line_webhook():
    """
    LINE webhook：驗證後立即排入背景 thread 並回 200。

    避免行情、Shioaji 登入與製圖阻塞 webhook，造成 redelivery 或 replyToken 失效。
    完成結果改由 LINE push API 傳送。
    """
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
        events = payload.get("events", [])

        if not events:
            return jsonify({"status": "ok", "message": "no events"}), 200

        accepted = 0

        for event in events:
            if not isinstance(event, dict):
                continue

            if not _line_should_process_event(event):
                continue

            is_redelivery = bool(
                event.get("deliveryContext", {}).get("isRedelivery")
            )

            print(
                "LINE event accepted",
                "| event_id =", event.get("webhookEventId"),
                "| isRedelivery =", is_redelivery,
                "| target =", bool(_line_target_id(event)),
                flush=True,
            )

            threading.Thread(
                target=_process_line_event_async,
                args=(dict(event),),
                name=f"line-event-{accepted + 1}",
                daemon=True,
            ).start()
            accepted += 1

        # 重點：不要等待 handle_request / Shioaji / Matplotlib。
        return jsonify({"status": "accepted", "events": accepted}), 200

    except Exception:
        print("line_webhook failed traceback:", flush=True)
        print(traceback.format_exc(), flush=True)
        return jsonify({"status": "error"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
