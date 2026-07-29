from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from io import StringIO
import re
import threading
import time
from typing import Any

import pandas as pd
import requests


MAIN_FORCE_SERVICE_VERSION = "2026-07-29-v1.1-WANTGOO-DIV-TEXT-FALLBACK"
MAIN_FORCE_PARSER_MODE = "html_table_or_visible_text"
WANTGOO_MAIN_TREND_URL = (
    "https://www.wantgoo.com/stock/{stock_id}/major-investors/main-trend"
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
    source: str = "玩股網"
    source_url: str = ""
    message: str = ""
    version: str = MAIN_FORCE_SERVICE_VERSION


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()
    return re.sub(r"\s+", "", text)


def _to_float(value: Any) -> float | None:
    text = _clean_text(value).replace(",", "").replace("%", "")
    if text in {"", "-", "--", "nan", "None"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _normalize_date(value: Any) -> str:
    text = _clean_text(value)
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""


def _flatten_columns(frame: pd.DataFrame) -> list[str]:
    columns: list[str] = []
    for column in frame.columns:
        if isinstance(column, tuple):
            parts = [
                _clean_text(part)
                for part in column
                if _clean_text(part)
                and not _clean_text(part).lower().startswith("unnamed")
            ]
            columns.append(parts[-1] if parts else "")
        else:
            columns.append(_clean_text(column))
    return columns


def _find_column(columns: list[str], *keywords: str) -> str:
    for column in columns:
        normalized = _clean_text(column)
        if all(keyword in normalized for keyword in keywords):
            return column
    return ""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
            return
        if not self._ignored_depth and tag in {
            "br",
            "td",
            "th",
            "tr",
            "div",
            "li",
            "p",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if not self._ignored_depth and tag in {
            "td",
            "th",
            "tr",
            "div",
            "li",
            "p",
        }:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = str(data or "").strip()
        if text:
            self.parts.extend([text, "\n"])

    def text(self) -> str:
        return "\n".join(self.parts)


def _rows_from_visible_text(html_text: str) -> list[dict[str, Any]]:
    parser = _VisibleTextParser()
    try:
        parser.feed(str(html_text or ""))
        visible_text = parser.text()
    except Exception:
        visible_text = unescape(
            re.sub(r"<[^>]+>", "\n", str(html_text or ""))
        )

    visible_text = (
        unescape(visible_text)
        .replace("\xa0", " ")
        .replace("－", "-")
        .replace("−", "-")
        .replace("＋", "+")
    )

    # 玩股網目前的歷史列可能由 div 組成，不一定是真正的 table。
    # 依「日期、收盤價、買賣超、家數差、5日、20日」六欄順序辨識。
    number = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    pattern = re.compile(
        rf"(?P<date>20\d{{2}}[/-]\d{{1,2}}[/-]\d{{1,2}})"
        rf"\s+(?P<close>{number})"
        rf"\s+(?P<net>{number})"
        rf"\s+(?P<count>{number})"
        rf"\s+(?P<c5>{number})\s*%"
        rf"\s+(?P<c20>{number})\s*%",
        flags=re.MULTILINE,
    )

    rows: list[dict[str, Any]] = []
    for match in pattern.finditer(visible_text):
        trade_date = _normalize_date(match.group("date"))
        close_price = _to_float(match.group("close"))
        net_buy_sell = _to_float(match.group("net"))
        broker_count_diff = _to_float(match.group("count"))
        concentration_5d = _to_float(match.group("c5"))
        concentration_20d = _to_float(match.group("c20"))
        if not trade_date or close_price is None or net_buy_sell is None:
            continue
        rows.append(
            {
                "trade_date": trade_date,
                "close_price": close_price,
                "net_buy_sell": int(round(net_buy_sell)),
                "broker_count_diff": (
                    int(round(broker_count_diff))
                    if broker_count_diff is not None
                    else None
                ),
                "concentration_5d": concentration_5d,
                "concentration_20d": concentration_20d,
            }
        )
    return rows


def _parse_main_force_html(html_text: str) -> list[dict[str, Any]]:
    if not str(html_text or "").strip():
        return []

    try:
        tables = pd.read_html(StringIO(html_text), flavor="lxml")
    except Exception:
        tables = []

    parsed_rows: list[dict[str, Any]] = []

    for original in tables:
        frame = original.copy()
        frame.columns = _flatten_columns(frame)
        columns = [str(column) for column in frame.columns]

        date_col = _find_column(columns, "日期")
        close_col = _find_column(columns, "收盤價")
        net_col = _find_column(columns, "買賣超")
        count_col = _find_column(columns, "家數差")
        concentration_5_col = _find_column(columns, "5日", "集中")
        concentration_20_col = _find_column(columns, "20日", "集中")

        required = {
            date_col,
            close_col,
            net_col,
            count_col,
            concentration_5_col,
            concentration_20_col,
        }
        if "" in required or len(required) < 6:
            continue

        for _, raw in frame.iterrows():
            trade_date = _normalize_date(raw.get(date_col))
            if not trade_date:
                continue

            close_price = _to_float(raw.get(close_col))
            net_buy_sell = _to_float(raw.get(net_col))
            broker_count_diff = _to_float(raw.get(count_col))
            concentration_5d = _to_float(raw.get(concentration_5_col))
            concentration_20d = _to_float(raw.get(concentration_20_col))

            if close_price is None or net_buy_sell is None:
                continue

            parsed_rows.append(
                {
                    "trade_date": trade_date,
                    "close_price": close_price,
                    "net_buy_sell": int(round(net_buy_sell)),
                    "broker_count_diff": (
                        int(round(broker_count_diff))
                        if broker_count_diff is not None
                        else None
                    ),
                    "concentration_5d": concentration_5d,
                    "concentration_20d": concentration_20d,
                }
            )

        if parsed_rows:
            break

    if not parsed_rows:
        parsed_rows = _rows_from_visible_text(html_text)

    unique_rows: dict[str, dict[str, Any]] = {}
    for row in parsed_rows:
        unique_rows[str(row["trade_date"])] = row

    return sorted(
        unique_rows.values(),
        key=lambda row: str(row.get("trade_date") or ""),
        reverse=True,
    )


def _status_from_latest(row: dict[str, Any]) -> tuple[str, str]:
    net_buy_sell = _to_float(row.get("net_buy_sell"))
    concentration_5d = _to_float(row.get("concentration_5d"))

    if net_buy_sell is None or concentration_5d is None:
        return "暫無判讀", "unknown"
    if net_buy_sell > 0 and concentration_5d > 0:
        return "主力偏買", "buy"
    if net_buy_sell < 0 and concentration_5d < 0:
        return "主力偏賣", "sell"
    if net_buy_sell == 0 and concentration_5d == 0:
        return "主力中性", "neutral"
    return "主力分歧", "divergence"


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


def get_stock_main_force_snapshot(
    stock_id: str,
    stock_name: str = "",
    timeout_seconds: float = 10.0,
) -> MainForceSnapshot:
    normalized_id = re.sub(r"\D", "", str(stock_id or "").strip())
    display_name = str(stock_name or normalized_id).strip()
    source_url = WANTGOO_MAIN_TREND_URL.format(stock_id=normalized_id)

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
        response = requests.get(
            source_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Referer": "https://www.wantgoo.com/",
            },
            timeout=(3.05, max(3.0, float(timeout_seconds))),
        )
        response.raise_for_status()
        rows = _parse_main_force_html(response.text)
        if not rows:
            snapshot = MainForceSnapshot(
                stock_id=normalized_id,
                stock_name=display_name,
                source_url=source_url,
                message="玩股網目前未回傳可辨識的主力進出資料。",
            )
        else:
            status_label, status_key = _status_from_latest(rows[0])
            snapshot = MainForceSnapshot(
                stock_id=normalized_id,
                stock_name=display_name,
                available=True,
                latest_date=str(rows[0].get("trade_date") or ""),
                status_label=status_label,
                status_key=status_key,
                rows=rows[:30],
                source_url=source_url,
                message="ok",
            )
        print(
            "DEBUG stock_main_force | fetch",
            "| version =", MAIN_FORCE_SERVICE_VERSION,
            "| stock_id =", normalized_id,
            "| http_status =", response.status_code,
            "| content_type =", response.headers.get("content-type", ""),
            "| html_chars =", len(response.text or ""),
            "| has_main_title =", "主力進出" in (response.text or ""),
            "| has_table_columns =",
            all(
                value in (response.text or "")
                for value in ("收盤價", "買賣超", "家數差")
            ),
            "| rows =", len(snapshot.rows),
            "| latest_date =", snapshot.latest_date,
            "| available =", snapshot.available,
            "| sec =", round(time.perf_counter() - started_at, 3),
            flush=True,
        )
        # 只快取成功內容；防爬驗證頁或暫時空頁不應讓使用者等5分鐘。
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
