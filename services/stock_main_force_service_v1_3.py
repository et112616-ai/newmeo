from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import re
import threading
import time
from typing import Any
from urllib.request import Request, urlopen


MAIN_FORCE_SERVICE_VERSION = "2026-07-29-v1.3-FUBON-BACKEND-DATA"
FUBON_MAIN_FORCE_URL = (
    "https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/"
    "zco_{stock_id}.djhtm"
)
FUBON_MAIN_FORCE_DATA_URL = (
    "https://fubon-ebrokerdj.fbs.com.tw/Z/ZC/ZCO/"
    "CZCO.DJBCD?A={stock_id}"
)

_CACHE_TTL_SECONDS = 300
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, "MainForceSnapshot"]] = {}


@dataclass
class MainForceSnapshot:
    stock_id: str
    stock_name: str
    available: bool = False
    latest_date: str = ""
    status_label: str = "暫無判讀"
    status_key: str = "unknown"
    rows: list[dict[str, Any]] = field(default_factory=list)
    source: str = "富邦 eBroker"
    source_url: str = ""
    message: str = ""
    version: str = MAIN_FORCE_SERVICE_VERSION


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    if text in {"", "-", "--", "nan", "None"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _get_cached(stock_id: str) -> MainForceSnapshot | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(stock_id)
        if not cached:
            return None
        cached_at, snapshot = cached
        if now - cached_at <= _CACHE_TTL_SECONDS:
            return snapshot
        _CACHE.pop(stock_id, None)
    return None


def _set_cached(stock_id: str, snapshot: MainForceSnapshot) -> None:
    with _CACHE_LOCK:
        _CACHE[stock_id] = (time.monotonic(), snapshot)


def _decode_response(raw: bytes) -> str:
    for encoding in ("utf-8", "cp950", "big5"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _http_get_text(url: str, referer: str, timeout_seconds: float) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
            "Accept": "text/plain,text/html,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
            "Referer": referer,
        },
    )
    with urlopen(request, timeout=max(3.0, float(timeout_seconds))) as response:
        return _decode_response(response.read())


def _latest_year_for_mmdd(mmdd: str, today: date | None = None) -> int:
    today = today or datetime.now().date()
    try:
        month = int(mmdd[:2])
        day = int(mmdd[2:])
        candidate = date(today.year, month, day)
    except Exception:
        return today.year

    # 例如 1 月查到的最後一筆仍是 12 月，應歸到前一年。
    if (candidate - today).days > 14:
        return today.year - 1
    return today.year


def _expand_mmdd_dates(values: list[str]) -> list[str]:
    if not values:
        return []

    year = _latest_year_for_mmdd(values[-1])
    output = [""] * len(values)
    next_mmdd = int(values[-1])

    for index in range(len(values) - 1, -1, -1):
        mmdd_text = values[index]
        try:
            mmdd = int(mmdd_text)
            if mmdd > next_mmdd:
                year -= 1
            month = int(mmdd_text[:2])
            day = int(mmdd_text[2:])
            output[index] = date(year, month, day).isoformat()
            next_mmdd = mmdd
        except Exception:
            output[index] = ""

    return output


def _parse_fubon_bcd(text: str) -> list[dict[str, Any]]:
    parts = re.split(r"\s+", str(text or "").strip())
    if len(parts) < 3:
        return []

    mmdd_values = [value.strip() for value in parts[0].split(",")]
    close_values = [value.strip() for value in parts[1].split(",")]
    net_values = [value.strip() for value in parts[2].split(",")]
    trade_dates = _expand_mmdd_dates(mmdd_values)

    size = min(
        len(trade_dates),
        len(close_values),
        len(net_values),
    )
    rows: list[dict[str, Any]] = []
    for index in range(size):
        trade_date = trade_dates[index]
        close_price = _to_float(close_values[index])
        # 此端點會以空欄表示 0 張，例如 2026/07/22。
        net_buy_sell = _to_float(net_values[index])
        if net_buy_sell is None and net_values[index] == "":
            net_buy_sell = 0.0
        if not trade_date or close_price is None or net_buy_sell is None:
            continue
        rows.append(
            {
                "trade_date": trade_date,
                "close_price": close_price,
                "net_buy_sell": int(round(net_buy_sell)),
                "broker_count_diff": None,
                "concentration_5d": None,
                "concentration_20d": None,
                "volume_lots": None,
            }
        )
    return rows


def _daily_volume_map(daily_history: Any) -> dict[str, float]:
    if daily_history is None or getattr(daily_history, "empty", True):
        return {}

    raw_columns = getattr(daily_history, "columns", [])
    columns = list(raw_columns) if raw_columns is not None else []
    volume_column = next(
        (
            column
            for column in columns
            if str(column).strip().lower() == "volume"
        ),
        None,
    )
    if volume_column is None:
        return {}

    attrs = getattr(daily_history, "attrs", {}) or {}
    volume_unit = str(attrs.get("volume_unit") or "shares").lower()
    output: dict[str, float] = {}

    try:
        iterator = daily_history[volume_column].items()
    except Exception:
        return output

    for stamp, raw_volume in iterator:
        volume = _to_float(raw_volume)
        if volume is None or volume <= 0:
            continue
        try:
            trade_date = stamp.strftime("%Y-%m-%d")
        except Exception:
            trade_date = str(stamp)[:10]
        if len(trade_date) != 10:
            continue

        # stock_service 的 D 日線註明 volume_unit=shares。
        # 主力買賣超單位為張，因此必須換算為千股（張）。
        volume_lots = volume if volume_unit in {"lots", "lot", "張"} else volume / 1000.0
        if volume_lots > 0:
            output[trade_date] = volume_lots
    return output


def _attach_concentrations(
    rows: list[dict[str, Any]],
    daily_history: Any,
) -> list[dict[str, Any]]:
    volume_map = _daily_volume_map(daily_history)
    if not rows:
        return rows

    for row in rows:
        row["volume_lots"] = volume_map.get(str(row.get("trade_date") or ""))

    for index, row in enumerate(rows):
        for window, key in (
            (5, "concentration_5d"),
            (20, "concentration_20d"),
        ):
            start = index - window + 1
            if start < 0:
                continue
            selected = rows[start : index + 1]
            volumes = [_to_float(item.get("volume_lots")) for item in selected]
            nets = [_to_float(item.get("net_buy_sell")) for item in selected]
            if any(value is None or value <= 0 for value in volumes):
                continue
            if any(value is None for value in nets):
                continue
            total_volume = sum(float(value) for value in volumes if value is not None)
            total_net = sum(float(value) for value in nets if value is not None)
            if total_volume > 0:
                row[key] = round(total_net / total_volume * 100.0, 4)
    return rows


def _status_from_latest(row: dict[str, Any]) -> tuple[str, str]:
    net_buy_sell = _to_float(row.get("net_buy_sell"))
    concentration_5d = _to_float(row.get("concentration_5d"))
    if net_buy_sell is None or concentration_5d is None:
        return "資料待補", "unknown"
    if net_buy_sell > 0 and concentration_5d > 0:
        return "主力偏買", "buy"
    if net_buy_sell < 0 and concentration_5d < 0:
        return "主力偏賣", "sell"
    if net_buy_sell == 0 and concentration_5d == 0:
        return "主力中性", "neutral"
    return "主力分歧", "divergence"


def get_stock_main_force_snapshot(
    stock_id: str,
    stock_name: str = "",
    daily_history: Any = None,
    timeout_seconds: float = 10.0,
) -> MainForceSnapshot:
    normalized_id = re.sub(r"\D", "", str(stock_id or "").strip())
    display_name = str(stock_name or normalized_id).strip()
    source_url = FUBON_MAIN_FORCE_URL.format(stock_id=normalized_id)
    data_url = FUBON_MAIN_FORCE_DATA_URL.format(stock_id=normalized_id)

    if not normalized_id:
        return MainForceSnapshot(
            stock_id="",
            stock_name=display_name,
            source_url=source_url,
            message="股票代號不正確。",
        )

    cached = _get_cached(normalized_id)
    if cached is not None:
        return cached

    started_at = time.perf_counter()
    try:
        text = _http_get_text(data_url, source_url, timeout_seconds)
        rows = _attach_concentrations(
            _parse_fubon_bcd(text),
            daily_history,
        )

        if not rows:
            snapshot = MainForceSnapshot(
                stock_id=normalized_id,
                stock_name=display_name,
                source_url=source_url,
                message="富邦 eBroker 目前未回傳可辨識的主力進出資料。",
            )
        else:
            rows_desc = list(reversed(rows))
            status_label, status_key = _status_from_latest(rows_desc[0])
            snapshot = MainForceSnapshot(
                stock_id=normalized_id,
                stock_name=display_name,
                available=True,
                latest_date=str(rows_desc[0].get("trade_date") or ""),
                status_label=status_label,
                status_key=status_key,
                rows=rows_desc[:45],
                source_url=source_url,
                message="ok",
            )

        print(
            "DEBUG stock_main_force | fetch",
            "| version =", MAIN_FORCE_SERVICE_VERSION,
            "| stock_id =", normalized_id,
            "| endpoint = FUBON_DJBCD",
            "| raw_chars =", len(text),
            "| rows =", len(snapshot.rows),
            "| latest_date =", snapshot.latest_date,
            "| latest_net =",
            snapshot.rows[0].get("net_buy_sell") if snapshot.rows else None,
            "| concentration_ready =",
            bool(
                snapshot.rows
                and snapshot.rows[0].get("concentration_5d") is not None
            ),
            "| available =", snapshot.available,
            "| sec =", round(time.perf_counter() - started_at, 3),
            flush=True,
        )
        if snapshot.available:
            _set_cached(normalized_id, snapshot)
        return snapshot
    except Exception as exc:
        print(
            "DEBUG stock_main_force | failed",
            "| version =", MAIN_FORCE_SERVICE_VERSION,
            "| stock_id =", normalized_id,
            "| error =", repr(exc),
            "| sec =", round(time.perf_counter() - started_at, 3),
            flush=True,
        )
        return MainForceSnapshot(
            stock_id=normalized_id,
            stock_name=display_name,
            source_url=source_url,
            message="主力進出資料暫時無法取得，請稍後再試。",
        )
