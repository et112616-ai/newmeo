from __future__ import annotations

import os
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd


SHIOAJI_API_KEY = os.getenv("SHIOAJI_API_KEY", "").strip()
SHIOAJI_SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY", "").strip()
SHIOAJI_SIMULATION = os.getenv("SHIOAJI_SIMULATION", "false").strip().lower() == "true"

_API = None
_LOGIN_TS = 0.0
LOGIN_TTL_SECONDS = 60 * 60 * 12

_STOCK_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
STOCK_SNAPSHOT_CACHE_TTL_SECONDS = 3


def _debug(*args):
    print("DEBUG shioaji |", *args, flush=True)


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

            result[key] = value
        except Exception:
            continue

    return result


def _normalize_ts(value: Any) -> str:
    """
    Shioaji snapshot 的 ts 可能是：
    - datetime
    - pandas Timestamp
    - int / float timestamp
    - 字串
    """
    if value is None or value == "":
        return ""

    try:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")

        ts = pd.to_datetime(value, errors="coerce")

        if pd.notna(ts):
            # 若有時區，轉台北時間；若沒有，就直接顯示
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert("Asia/Taipei")

            return ts.strftime("%Y-%m-%d %H:%M:%S")

    except Exception:
        pass

    return str(value)


def get_api():
    """
    Lazy login。
    不在 import 時登入，避免 Render boot 時卡住。
    """
    global _API, _LOGIN_TS

    if not SHIOAJI_API_KEY or not SHIOAJI_SECRET_KEY:
        _debug("missing api key or secret")
        return None

    now = time.time()

    if _API is not None and now - _LOGIN_TS < LOGIN_TTL_SECONDS:
        return _API

    try:
        import shioaji as sj

        api = sj.Shioaji(simulation=SHIOAJI_SIMULATION)

        api.login(
            api_key=SHIOAJI_API_KEY,
            secret_key=SHIOAJI_SECRET_KEY,
            contracts_timeout=10000,
        )

        _API = api
        _LOGIN_TS = now

        _debug("login ok", "simulation =", SHIOAJI_SIMULATION)

        return _API

    except Exception as exc:
        _debug("login failed", exc)
        _API = None
        return None


def _get_stock_contract(api, stock_id: str):
    sid = str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()

    if not sid:
        return None

    try:
        return api.Contracts.Stocks[sid]
    except Exception:
        pass

    try:
        return api.Contracts.Stocks.TSE[sid]
    except Exception:
        pass

    try:
        return api.Contracts.Stocks.OTC[sid]
    except Exception:
        pass

    return None


def get_stock_snapshot(stock_id: str) -> dict[str, Any] | None:
    """
    查現股即時 snapshot。

    回傳：
    {
      stock_id,
      close,
      change,
      change_pct,
      open,
      high,
      low,
      volume,
      total_volume,
      ts,
      source
    }
    """
    sid = str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()

    if not sid:
        return None

    now = time.time()
    cached = _STOCK_SNAPSHOT_CACHE.get(sid)

    if cached:
        ts, data = cached
        if now - ts <= STOCK_SNAPSHOT_CACHE_TTL_SECONDS:
            return data

    api = get_api()

    if api is None:
        return None

    contract = _get_stock_contract(api, sid)

    if contract is None:
        _debug("stock contract not found", sid)
        return None

    try:
        snapshots = api.snapshots([contract])

        if not snapshots:
            return None

        raw = _to_dict(snapshots[0])

        close = _safe_float(raw.get("close"))
        change = _safe_float(raw.get("change_price"))
        change_pct = _safe_float(raw.get("change_rate"))

        data = {
            "stock_id": sid,
            "close": close,
            "change": change,
            "change_pct": change_pct,
            "open": _safe_float(raw.get("open")),
            "high": _safe_float(raw.get("high")),
            "low": _safe_float(raw.get("low")),
            "volume": _safe_int(raw.get("volume")),
            "total_volume": _safe_int(raw.get("total_volume")),
            "buy_price": _safe_float(raw.get("buy_price")),
            "sell_price": _safe_float(raw.get("sell_price")),
            "ts": _normalize_ts(raw.get("ts")),
            "source": "Shioaji",
        }

        _STOCK_SNAPSHOT_CACHE[sid] = (now, data)

        _debug(
            "stock snapshot",
            sid,
            "close =",
            data["close"],
            "change =",
            data["change"],
            "change_pct =",
            data["change_pct"],
            "ts =",
            data["ts"],
        )

        return data

    except Exception as exc:
        _debug("stock snapshot failed", sid, exc)
        return None


def append_stock_snapshot_to_intraday_df(df: pd.DataFrame, stock_id: str) -> pd.DataFrame:
    """
    把 Shioaji 即時 snapshot 補到 1m / 5m 圖表最後一點。

    注意：
    不要把 Shioaji 原始物件放進 df.attrs，
    否則 pandas deepcopy 時可能出現：
    TypeError: cannot pickle 'builtins.TickType' object
    """
    if df is None or df.empty:
        return df

    snapshot = get_stock_snapshot(stock_id)

    if not snapshot:
        return df

    price = _safe_float(snapshot.get("close"))

    if price <= 0:
        return df

    ts_text = str(snapshot.get("ts") or "").strip()

    if not ts_text:
        return df

    try:
        snap_ts = pd.to_datetime(ts_text)

        if pd.isna(snap_ts):
            return df

        result = df.copy()

        # 只存純文字 / 純數字，避免 TickType 之類物件造成 deepcopy 錯誤
        result.attrs["shioaji_snapshot"] = {
            "stock_id": str(snapshot.get("stock_id") or ""),
            "close": _safe_float(snapshot.get("close")),
            "change": _safe_float(snapshot.get("change")),
            "change_pct": _safe_float(snapshot.get("change_pct")),
            "volume": _safe_int(snapshot.get("volume")),
            "total_volume": _safe_int(snapshot.get("total_volume")),
            "ts": str(snapshot.get("ts") or ""),
            "source": "Shioaji",
        }

        # 對齊 index 時區
        if getattr(result.index, "tz", None) is not None:
            if snap_ts.tzinfo is None:
                snap_ts = snap_ts.tz_localize("Asia/Taipei")

            snap_ts = snap_ts.tz_convert(result.index.tz)

        else:
            if snap_ts.tzinfo is not None:
                snap_ts = snap_ts.tz_convert("Asia/Taipei").tz_localize(None)

        last_idx = result.index[-1]

        # 如果 Shioaji 時間沒有比 K 線新，就不硬塞點，但保留 attrs 給價格區使用
        if snap_ts <= last_idx:
            return result

        last_row = result.iloc[-1].copy()

        last_row["Open"] = price
        last_row["High"] = max(price, _safe_float(last_row.get("High"), price))
        last_row["Low"] = min(price, _safe_float(last_row.get("Low"), price))
        last_row["Close"] = price

        if "Volume" in result.columns:
            last_row["Volume"] = _safe_int(
                snapshot.get("volume")
                or snapshot.get("total_volume")
            )

        result.loc[snap_ts] = last_row
        result = result.sort_index()

        return result

    except Exception as exc:
        _debug("append snapshot failed", stock_id, exc)
        return df
