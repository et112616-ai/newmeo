from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd

try:
    from services.sinopac_quote_service import get_api
except Exception:
    def get_api():
        return None


MARKET_FUTURE_CACHE_TTL_SECONDS = 3
_MARKET_FUTURE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class MarketFutureSnapshot:
    available: bool
    message: str

    futures_id: str = "TXF"
    futures_name: str = "台指期近月"
    contract_code: str = ""
    session_mode: str = "day"
    trading_session: str = "日盤"

    future_price: float = 0.0
    future_change: float = 0.0
    future_change_pct: float = 0.0

    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0

    volume: int = 0
    total_volume: int = 0

    buy_price: float = 0.0
    sell_price: float = 0.0

    quote_time: str = ""
    quote_source: str = "永豐即時"


def _debug(*args):
    print("DEBUG market_future |", *args, flush=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        if isinstance(value, Decimal):
            return float(value)

        text = str(value).replace(",", "").replace("%", "").strip()

        if not text:
            return default

        return float(text)

    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, float(default))))
    except Exception:
        return default


def _to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}

    if isinstance(obj, dict):
        return obj

    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass

    result: dict[str, Any] = {}

    for key in dir(obj):
        if key.startswith("_"):
            continue

        try:
            value = getattr(obj, key)

            if callable(value):
                continue

            # 避免 enum / 特殊物件造成 deepcopy 或 pickle 問題
            if key in {"tick_type", "change_type"}:
                result[key] = str(value)
            else:
                result[key] = value

        except Exception:
            continue

    return result


def _normalize_ts(value: Any) -> str:
    if value is None or value == "":
        return ""

    try:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")

        ts = pd.to_datetime(value, errors="coerce")

        if pd.notna(ts):
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert("Asia/Taipei")

            return ts.strftime("%Y-%m-%d %H:%M:%S")

    except Exception:
        pass

    return str(value)


def _try_get_contract(container, code: str):
    if container is None:
        return None

    try:
        return container[code]
    except Exception:
        pass

    try:
        return getattr(container, code)
    except Exception:
        pass

    return None


def _get_txf_contract(api):
    """
    取得台指期 TXF 近月 contract。
    優先使用 TXFR1。
    """
    futures_root = getattr(api.Contracts, "Futures", None)

    if futures_root is None:
        return None

    txf_group = _try_get_contract(futures_root, "TXF")

    candidates = [
        "TXFR1",
        "TXFR2",
        "TXF",
    ]

    # 先從 TXF 群組找
    for code in candidates:
        contract = _try_get_contract(txf_group, code)

        if contract is not None:
            return contract

    # 再從 Futures 根目錄找
    for code in candidates:
        contract = _try_get_contract(futures_root, code)

        if contract is not None:
            return contract

    return None


def _snapshot_from_dict(data: dict[str, Any]) -> MarketFutureSnapshot:
    return MarketFutureSnapshot(
        available=bool(data.get("available")),
        message=str(data.get("message") or ""),

        futures_id=str(data.get("futures_id") or "TXF"),
        futures_name=str(data.get("futures_name") or "台指期近月"),
        contract_code=str(data.get("contract_code") or ""),
        session_mode=str(data.get("session_mode") or "day"),
        trading_session=str(data.get("trading_session") or "日盤"),

        future_price=_safe_float(data.get("future_price")),
        future_change=_safe_float(data.get("future_change")),
        future_change_pct=_safe_float(data.get("future_change_pct")),

        open_price=_safe_float(data.get("open_price")),
        high_price=_safe_float(data.get("high_price")),
        low_price=_safe_float(data.get("low_price")),

        volume=_safe_int(data.get("volume")),
        total_volume=_safe_int(data.get("total_volume")),

        buy_price=_safe_float(data.get("buy_price")),
        sell_price=_safe_float(data.get("sell_price")),

        quote_time=str(data.get("quote_time") or ""),
        quote_source=str(data.get("quote_source") or "永豐即時"),
    )


def get_market_future_snapshot(session_mode: str = "day") -> MarketFutureSnapshot:
    """
    取得台指期 TXF 近月即時報價。

    session_mode:
    - day：日盤
    - all：全盤

    第一版先使用 Shioaji TXFR1 即時 snapshot。
    日盤 / 全盤按鈕先保留，之後如果要畫歷史 K 線，
    再用 session_mode 過濾或合併日盤、夜盤資料。
    """
    session_mode = str(session_mode or "day").strip().lower()

    if session_mode not in {"day", "all"}:
        session_mode = "day"

    trading_session = "全盤" if session_mode == "all" else "日盤"
    cache_key = f"TXF:{session_mode}"
    now = time.time()

    cached = _MARKET_FUTURE_CACHE.get(cache_key)

    if cached:
        ts, data = cached

        if now - ts <= MARKET_FUTURE_CACHE_TTL_SECONDS:
            return _snapshot_from_dict(data)

    api = get_api()

    if api is None:
        return MarketFutureSnapshot(
            available=False,
            message="Shioaji 尚未登入，無法取得台指期即時資料。",
            session_mode=session_mode,
            trading_session=trading_session,
        )

    contract = _get_txf_contract(api)

    if contract is None:
        return MarketFutureSnapshot(
            available=False,
            message="找不到台指期 TXF 近月 contract：TXFR1。",
            session_mode=session_mode,
            trading_session=trading_session,
        )

    try:
        snapshots = api.snapshots([contract])

        if not snapshots:
            return MarketFutureSnapshot(
                available=False,
                message="Shioaji 沒有回傳台指期 snapshot。",
                session_mode=session_mode,
                trading_session=trading_session,
            )

        raw = _to_dict(snapshots[0])

        contract_code = str(
            raw.get("code")
            or getattr(contract, "code", "")
            or "TXFR1"
        )

        data = {
            "available": True,
            "message": "ok",

            "futures_id": "TXF",
            "futures_name": "台指期近月",
            "contract_code": contract_code,
            "session_mode": session_mode,
            "trading_session": trading_session,

            "future_price": _safe_float(raw.get("close")),
            "future_change": _safe_float(raw.get("change_price")),
            "future_change_pct": _safe_float(raw.get("change_rate")),

            "open_price": _safe_float(raw.get("open")),
            "high_price": _safe_float(raw.get("high")),
            "low_price": _safe_float(raw.get("low")),

            "volume": _safe_int(raw.get("volume")),
            "total_volume": _safe_int(raw.get("total_volume")),

            "buy_price": _safe_float(raw.get("buy_price")),
            "sell_price": _safe_float(raw.get("sell_price")),

            "quote_time": _normalize_ts(raw.get("ts")),
            "quote_source": "永豐即時",
        }

        _MARKET_FUTURE_CACHE[cache_key] = (now, data)

        _debug(
            "snapshot",
            contract_code,
            "session =",
            trading_session,
            "close =",
            data["future_price"],
            "change =",
            data["future_change"],
            "change_pct =",
            data["future_change_pct"],
            "volume =",
            data["total_volume"],
            "time =",
            data["quote_time"],
        )

        return _snapshot_from_dict(data)

    except Exception as exc:
        _debug("snapshot failed", exc)

        return MarketFutureSnapshot(
            available=False,
            message=f"取得台指期即時資料失敗：{exc}",
            session_mode=session_mode,
            trading_session=trading_session,
        )
