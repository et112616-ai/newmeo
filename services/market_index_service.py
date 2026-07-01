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


MARKET_INDEX_CACHE_TTL_SECONDS = 3
_MARKET_INDEX_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class MarketIndexSnapshot:
    available: bool
    message: str

    index_id: str = "TAIEX"
    index_name: str = "加權指數"

    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0

    change: float = 0.0
    change_pct: float = 0.0

    volume: int = 0
    total_volume: int = 0
    amount: int = 0
    total_amount: int = 0

    quote_time: str = ""
    quote_source: str = "永豐即時"


def _debug(*args):
    print("DEBUG market_index |", *args, flush=True)


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

            # 不放 enum / 特殊物件，避免 deepcopy 或 pickle 問題
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


def _get_taiex_contract(api):
    """
    Shioaji 加權指數 contract：
    api.Contracts.Indexs.TSE["001"]
    或 api.Contracts.Indexs.TSE.TSE001
    """
    try:
        return api.Contracts.Indexs.TSE["001"]
    except Exception:
        pass

    try:
        return api.Contracts.Indexs.TSE.TSE001
    except Exception:
        pass

    try:
        return api.Contracts.Indexs["TSE"]["001"]
    except Exception:
        pass

    return None


def get_market_index_snapshot() -> MarketIndexSnapshot:
    """
    取得加權指數即時 snapshot。
    """
    cache_key = "TAIEX"
    now = time.time()

    cached = _MARKET_INDEX_CACHE.get(cache_key)

    if cached:
        ts, data = cached

        if now - ts <= MARKET_INDEX_CACHE_TTL_SECONDS:
            return _snapshot_from_dict(data)

    api = get_api()

    if api is None:
        return MarketIndexSnapshot(
            available=False,
            message="Shioaji 尚未登入，無法取得加權指數即時資料。",
        )

    contract = _get_taiex_contract(api)

    if contract is None:
        return MarketIndexSnapshot(
            available=False,
            message="找不到加權指數 contract：TSE001。",
        )

    try:
        snapshots = api.snapshots([contract])

        if not snapshots:
            return MarketIndexSnapshot(
                available=False,
                message="Shioaji 沒有回傳加權指數 snapshot。",
            )

        raw = _to_dict(snapshots[0])

        data = {
            "available": True,
            "message": "ok",
            "index_id": "TAIEX",
            "index_name": "加權指數",
            "open_price": _safe_float(raw.get("open")),
            "high_price": _safe_float(raw.get("high")),
            "low_price": _safe_float(raw.get("low")),
            "close_price": _safe_float(raw.get("close")),
            "change": _safe_float(raw.get("change_price")),
            "change_pct": _safe_float(raw.get("change_rate")),
            "volume": _safe_int(raw.get("volume")),
            "total_volume": _safe_int(raw.get("total_volume")),
            "amount": _safe_int(raw.get("amount")),
            "total_amount": _safe_int(raw.get("total_amount")),
            "quote_time": _normalize_ts(raw.get("ts")),
            "quote_source": "永豐即時",
        }

        _MARKET_INDEX_CACHE[cache_key] = (now, data)

        _debug(
            "snapshot",
            "close =",
            data["close_price"],
            "change =",
            data["change"],
            "change_pct =",
            data["change_pct"],
            "volume =",
            data["total_volume"],
            "time =",
            data["quote_time"],
        )

        return _snapshot_from_dict(data)

    except Exception as exc:
        _debug("snapshot failed", exc)

        return MarketIndexSnapshot(
            available=False,
            message=f"取得加權指數即時資料失敗：{exc}",
        )


def _snapshot_from_dict(data: dict[str, Any]) -> MarketIndexSnapshot:
    return MarketIndexSnapshot(
        available=bool(data.get("available")),
        message=str(data.get("message") or ""),
        index_id=str(data.get("index_id") or "TAIEX"),
        index_name=str(data.get("index_name") or "加權指數"),
        open_price=_safe_float(data.get("open_price")),
        high_price=_safe_float(data.get("high_price")),
        low_price=_safe_float(data.get("low_price")),
        close_price=_safe_float(data.get("close_price")),
        change=_safe_float(data.get("change")),
        change_pct=_safe_float(data.get("change_pct")),
        volume=_safe_int(data.get("volume")),
        total_volume=_safe_int(data.get("total_volume")),
        amount=_safe_int(data.get("amount")),
        total_amount=_safe_int(data.get("total_amount")),
        quote_time=str(data.get("quote_time") or ""),
        quote_source=str(data.get("quote_source") or "永豐即時"),
    )
