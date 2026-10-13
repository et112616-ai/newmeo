from __future__ import annotations

"""
market_disposition_service.py

用途：
    大盤處置日報：整合 TWSE（上市）與 TPEX（上櫃）目前仍在
    處置期間的股票，依「撮合分鐘數」分組，供 LINE 圖卡使用。

資料來源：
    1. TWSE 證交所 OpenAPI：openapi.twse.com.tw/v1/announcement/punish
    2. TPEX 櫃買中心 OpenAPI：www.tpex.org.tw/openapi/v1/tpex_disposal_information

篩選：
    只保留「4 碼以內的純數字代號」，排除權證及其他衍生商品。
    只保留「今天仍落在處置期間內」的股票。

分組：
    依官方公告內容判讀出的撮合分鐘數（例如「約每5分鐘撮合一次」）分組；
    無法判讀者歸入「未列明」。
"""

import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

MARKET_DISPOSITION_SERVICE_VERSION = "2026-10-13-v6-ghost-read-before-write"

TWSE_URL = "https://openapi.twse.com.tw/v1/announcement/punish"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"

TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

MARKET_DISPOSITION_CACHE_TTL_SECONDS = 15 * 60
_MARKET_DISPOSITION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

UNKNOWN_GROUP_KEY = "na"


def _debug(*args):
    print("DEBUG market_disposition |", *args, flush=True)


# ============================================================
# 基本工具
# ============================================================


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_stock_code(code: str) -> bool:
    """只接受純數字、4 碼以下（排除權證等衍生商品）。"""
    code = _clean_text(code)
    return bool(re.fullmatch(r"\d+", code)) and len(code) <= 4


def _today_tpe() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def _parse_date(value: Any):
    """支援 2026/08/18、2026-08-18、115/08/18、1150818。"""
    value = _clean_text(value)
    if not value:
        return None

    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", value)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.fullmatch(r"(\d{3})[/-](\d{1,2})[/-](\d{1,2})", value)
    if m:
        try:
            return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.fullmatch(r"(\d{3})(\d{2})(\d{2})", value)
    if m:
        try:
            return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    return None


def _parse_period(value: Any):
    """解析『115/08/18~115/08/29』『2026/08/18～2026/08/29』。"""
    value = _clean_text(value)
    if not value:
        return None, None

    parts = re.split(r"[~～]", value)
    if len(parts) != 2:
        return None, None

    return _parse_date(parts[0]), _parse_date(parts[1])


_CHINESE_NUMBER = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
    "二十一": 21, "二十二": 22, "二十三": 23, "二十四": 24, "二十五": 25,
    "四十五": 45, "六十": 60,
}


def _detect_matching_minutes(text: Any):
    """從官方處置內容判讀撮合分鐘數，例如『約每5分鐘撮合一次』。"""
    text = _clean_text(text)
    if not text:
        return None

    match = re.search(r"約每\s*(\d+)\s*分鐘\s*撮合", text)
    if match:
        return int(match.group(1))

    for chinese, number in sorted(
        _CHINESE_NUMBER.items(), key=lambda x: len(x[0]), reverse=True
    ):
        pattern = rf"約每\s*{re.escape(chinese)}\s*分鐘\s*撮合"
        if re.search(pattern, text):
            return number

    return None


def _period_display(start, end, fallback: str) -> str:
    """輸出 MM/DD～MM/DD；缺資料時退回官方原始文字。"""
    if start and end:
        return f"{start.month:02d}/{start.day:02d}～{end.month:02d}/{end.day:02d}"
    return fallback or "--"


# ============================================================
# TWSE（上市）
# ============================================================


def _fetch_twse() -> list[dict[str, Any]]:
    try:
        response = requests.get(TWSE_URL, headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list):
            _debug("TWSE 回傳非 list", type(data))
            return []

        result: list[dict[str, Any]] = []

        for item in data:
            code = _clean_text(item.get("Code"))
            if not _is_stock_code(code):
                continue

            name = _clean_text(item.get("Name"))
            period = _clean_text(item.get("DispositionPeriod"))
            measures = _clean_text(item.get("DispositionMeasures"))
            detail = _clean_text(item.get("Detail"))

            start_date, end_date = _parse_period(period)
            minutes = _detect_matching_minutes(detail) or _detect_matching_minutes(
                measures
            )

            result.append(
                {
                    "market": "上市",
                    "code": code,
                    "name": name,
                    "period": period,
                    "start_date": start_date,
                    "end_date": end_date,
                    "minutes": minutes,
                }
            )

        return result

    except Exception as e:
        _debug("TWSE 取得失敗", type(e).__name__, str(e))
        return []


# ============================================================
# TPEX（上櫃）
# ============================================================


def _fetch_tpex() -> list[dict[str, Any]]:
    try:
        response = requests.get(TPEX_URL, headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list):
            _debug("TPEX 回傳非 list", type(data))
            return []

        result: list[dict[str, Any]] = []

        for item in data:
            code = ""
            for key in (
                "SecuritiesCompanyCode",
                "Code",
                "SecurityCode",
                "證券代號",
                "代號",
            ):
                if item.get(key):
                    code = _clean_text(item.get(key))
                    break

            if not _is_stock_code(code):
                continue

            name = ""
            for key in ("CompanyName", "Name", "SecurityName", "證券名稱", "名稱"):
                if item.get(key):
                    name = _clean_text(item.get(key))
                    break

            period = ""
            for key in ("DispositionPeriod", "Period", "處置起訖時間"):
                if item.get(key):
                    period = _clean_text(item.get(key))
                    break

            detail_parts = [
                _clean_text(value) for value in item.values() if _clean_text(value)
            ]
            detail = " ".join(detail_parts)

            start_date, end_date = _parse_period(period)
            minutes = _detect_matching_minutes(detail)

            result.append(
                {
                    "market": "上櫃",
                    "code": code,
                    "name": name,
                    "period": period,
                    "start_date": start_date,
                    "end_date": end_date,
                    "minutes": minutes,
                }
            )

        return result

    except Exception as e:
        _debug("TPEX 取得失敗", type(e).__name__, str(e))
        return []


# ============================================================
# Snapshot
# ============================================================


@dataclass
class MarketDispositionGroup:
    key: str
    minutes: int | None
    label: str
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MarketDispositionSnapshot:
    available: bool
    message: str = ""
    trade_date: str = ""
    groups: list[MarketDispositionGroup] = field(default_factory=list)
    all_rows: list[dict[str, Any]] = field(default_factory=list)
    total_count: int = 0
    source: str = "TWSE / TPEX"
    ghost_count: int = 0
    last_seen_upsert: dict[str, Any] = field(default_factory=dict)


def _group_key(minutes: int | None) -> str:
    return UNKNOWN_GROUP_KEY if minutes is None else str(minutes)


def _group_label(minutes: int | None) -> str:
    return "未列明" if minutes is None else f"{minutes}分鐘"


def _row_sort_key(row: dict[str, Any]):
    """
    依「新增日期」新→舊排序；同一天新增的再依「實際解除日」近→遠排序，
    最後用股號做穩定排序。
    → 最上面是最新增的，最下面是快要出關的。
    """
    start_date = row.get("start_date")
    release_date = row.get("release_date") or row.get("end_date")

    # 用 toordinal() 讓 None 也能安全比較：
    # start_date 用負值做「新到舊」；release_date 用正值做「近到遠」。
    start_order = -start_date.toordinal() if start_date else 0
    release_order = release_date.toordinal() if release_date else 0

    return (start_order, release_order, row.get("code", ""))


def _build_groups(rows: list[dict[str, Any]]) -> list[MarketDispositionGroup]:
    by_key: dict[str, MarketDispositionGroup] = {}

    for row in rows:
        minutes = row.get("minutes")
        key = _group_key(minutes)

        if key not in by_key:
            by_key[key] = MarketDispositionGroup(
                key=key, minutes=minutes, label=_group_label(minutes)
            )

        by_key[key].rows.append(row)

    def sort_key(group: MarketDispositionGroup):
        # 撮合分鐘數由小到大；未列明排最後。
        return (group.minutes is None, group.minutes or 0)

    groups = sorted(by_key.values(), key=sort_key)

    for group in groups:
        group.rows.sort(key=_row_sort_key)

    return groups


def _dedupe_latest_by_code(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    同一檔股票若因多次處置公告而重複出現（例如同代號有兩段不同
    的處置期間），只保留「最新一筆」：以 start_date 較新者為準，
    若 start_date 相同則以 end_date 較新者為準。
    """
    best_by_code: dict[str, dict[str, Any]] = {}

    for row in rows:
        code = row.get("code")
        existing = best_by_code.get(code)

        if existing is None:
            best_by_code[code] = row
            continue

        cur_start = row.get("start_date") or date.min
        exist_start = existing.get("start_date") or date.min

        if cur_start > exist_start:
            best_by_code[code] = row
        elif cur_start == exist_start:
            cur_end = row.get("end_date") or date.min
            exist_end = existing.get("end_date") or date.min
            if cur_end > exist_end:
                best_by_code[code] = row

    return list(best_by_code.values())


# ============================================================
# 「最後一次看到」持久化（彌補官方 API 隔天就把資料拿掉的問題）
# ============================================================
#
# TWSE／TPEX 的處置公告 API，一旦處置期間「正式結束」（end_date 那天
# 過後），官方清單就會直接把該檔股票移除，不會多留一天。但我們的
# 「解除日＝期間結束後下一個交易日」這個顯示邏輯，需要在解除日當天
# 還能看到這筆資料才能顯示「今日解除」——而官方不會配合留到那天。
#
# 解法：每次抓到即時資料時，把當下看到的每一檔都記錄「最後一次看到」
# 的完整資訊存進 Supabase。組「今日解除」清單時，除了今天官方清單
# 裡還有的，也去查這張表：凡是「解除日剛好是今天」但官方清單已經
# 不見的股票，用存起來的資料補回來顯示這一天。隔天 release_date 不再
# 等於今天，自然就不會再被抓出來，不需要額外清理。

MARKET_DISPOSITION_LAST_SEEN_TABLE = "market_disposition_last_seen"


def _supabase_headers() -> dict[str, str]:
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _supabase_table_url(table: str) -> str:
    base = os.getenv("SUPABASE_URL", "").strip().rstrip("/")

    if not base:
        return ""

    return f"{base}/rest/v1/{table}"


def _last_seen_payload(row: dict[str, Any], today: date) -> dict[str, Any] | None:
    code = _clean_text(row.get("code"))
    start_date = row.get("start_date")
    end_date = row.get("end_date")
    release_date = row.get("release_date")

    if not code or not start_date or not end_date or not release_date:
        return None

    return {
        "stock_id": code,
        "market": row.get("market") or "",
        "name": row.get("name") or "",
        "period_text": row.get("period") or "",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "release_date": release_date.isoformat(),
        "minutes": row.get("minutes"),
        "last_seen_date": today.isoformat(),
    }


_LAST_UPSERT_STATUS: dict[str, Any] = {}


def get_last_upsert_status() -> dict[str, Any]:
    """最近一次寫入 last_seen 的結果（給同步路由／診斷用）。"""
    return dict(_LAST_UPSERT_STATUS)


def _upsert_last_seen_rows(rows: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """
    把今天官方清單裡看到的股票，存一份「最後一次看到」的快照。

    重點：
    1. 同一個股票代號只送一筆（保留最新一筆）。PostgreSQL 的批次 upsert
       只要同一批裡有重複主鍵，整批都會被拒絕。
    2. 只存「尚未完全結束」（release_date >= 今天）的資料，避免官方清單
       裡殘留的舊紀錄去覆蓋掉比較新的紀錄。
    3. 回傳寫入結果，失敗時不再只留 log，同步路由可以直接看到。
    """
    status: dict[str, Any] = {
        "attempted": 0,
        "deduped_from": 0,
        "ok": False,
        "http_status": None,
        "error": "",
    }

    url = _supabase_table_url(MARKET_DISPOSITION_LAST_SEEN_TABLE)

    if not url:
        status["error"] = "SUPABASE_URL not set"
        _LAST_UPSERT_STATUS.clear()
        _LAST_UPSERT_STATUS.update(status)
        return status

    candidates = [
        r for r in rows
        if r.get("release_date") and r["release_date"] >= today
    ]
    status["deduped_from"] = len(candidates)
    candidates = _dedupe_latest_by_code(candidates)

    payloads = [
        p for p in (
            _last_seen_payload(row, today) for row in candidates
        ) if p
    ]
    status["attempted"] = len(payloads)

    if not payloads:
        status["ok"] = True
        _LAST_UPSERT_STATUS.clear()
        _LAST_UPSERT_STATUS.update(status)
        return status

    headers = _supabase_headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"

    try:
        res = requests.post(
            url,
            headers=headers,
            params={"on_conflict": "stock_id"},
            json=payloads,
            timeout=15,
        )
        status["http_status"] = res.status_code

        if res.status_code >= 400:
            status["error"] = res.text[:300]
            _debug(
                "last_seen upsert failed",
                "| status =", res.status_code,
                "| body =", res.text[:300],
            )
        else:
            status["ok"] = True

    except Exception as e:
        status["error"] = f"{type(e).__name__}: {e}"
        _debug("last_seen upsert exception", type(e).__name__, str(e))

    _LAST_UPSERT_STATUS.clear()
    _LAST_UPSERT_STATUS.update(status)
    return status


def _fetch_ghost_released_today(
    today: date,
    live_codes: set[str],
) -> list[dict[str, Any]]:
    """
    找出「解除日剛好是今天、但官方清單裡已經不見了」的股票，
    用上次看到的資料補回來顯示這最後一天。
    """
    url = _supabase_table_url(MARKET_DISPOSITION_LAST_SEEN_TABLE)

    if not url:
        return []

    headers = _supabase_headers()

    try:
        res = requests.get(
            url,
            headers=headers,
            params={
                "select": (
                    "stock_id,market,name,period_text,"
                    "start_date,end_date,release_date,minutes"
                ),
                "release_date": f"eq.{today.isoformat()}",
            },
            timeout=15,
        )

        if res.status_code >= 400:
            _debug(
                "last_seen ghost fetch failed",
                "| status =", res.status_code,
                "| body =", res.text[:300],
            )
            return []

        records = res.json() or []

    except Exception as e:
        _debug("last_seen ghost fetch exception", type(e).__name__, str(e))
        return []

    ghosts: list[dict[str, Any]] = []

    for record in records:
        code = _clean_text(record.get("stock_id"))

        if not code or code in live_codes:
            continue

        start_date = _parse_date(record.get("start_date"))
        end_date = _parse_date(record.get("end_date"))
        release_date = _parse_date(record.get("release_date"))

        if not start_date or not end_date or not release_date:
            continue

        ghosts.append(
            {
                "market": record.get("market") or "",
                "code": code,
                "name": record.get("name") or "",
                "period": record.get("period_text") or "",
                "start_date": start_date,
                "end_date": end_date,
                "release_date": release_date,
                "minutes": record.get("minutes"),
            }
        )

    if ghosts:
        _debug(
            "last_seen ghost rows found",
            "| today =", today.isoformat(),
            "| codes =", [g["code"] for g in ghosts],
        )

    return ghosts


def _next_trading_day(d: date) -> date:
    """
    往後找下一個「交易日」（只跳過六日，不含國定假日／補假）。

    注意：台灣證交所實際上還有國定假日不開盤，這裡沒有內建假日曆，
    所以遇到連假（例如農曆年、國慶連假）算出來的解除日會提前，
    需要另外維護假日清單才能完全準確。目前只處理最常見的週末情況。
    """
    next_day = d + timedelta(days=1)
    while next_day.weekday() >= 5:  # 5=Saturday, 6=Sunday
        next_day += timedelta(days=1)
    return next_day


def get_market_disposition_snapshot(
    force_refresh: bool = False,
) -> MarketDispositionSnapshot:
    """取得目前仍在處置期間（含當日剛解除者）的上市＋上櫃股票，並依撮合分鐘數分組。"""

    cache_key = "market_disposition:v1"
    now = time.time()

    if not force_refresh:
        cached = _MARKET_DISPOSITION_CACHE.get(cache_key)
        if cached:
            ts, data = cached
            if now - ts <= MARKET_DISPOSITION_CACHE_TTL_SECONDS:
                return _snapshot_from_dict(data)

    today = _today_tpe()

    twse_rows = _fetch_twse()
    tpex_rows = _fetch_tpex()
    raw_rows = twse_rows + tpex_rows

    # 先把今天官方清單裡每一檔都算好 release_date。
    rows_with_release: list[dict[str, Any]] = []

    for row in raw_rows:
        start_date = row.get("start_date")
        end_date = row.get("end_date")

        if not start_date or not end_date:
            continue

        row = dict(row)
        row["release_date"] = _next_trading_day(end_date)
        rows_with_release.append(row)

    # 今天官方清單裡「真的有效（處置中或今日解除）」的股票代號。
    # 只有這些才會擋住補回：官方清單只是出現同代號、但那筆不在有效期間內
    # （例如殘留的舊紀錄或未來才開始的新公告），不應該讓我們放棄補回。
    live_active_codes: set[str] = {
        row.get("code", "")
        for row in rows_with_release
        if row["start_date"] <= today <= row["release_date"]
    }

    # 順序很重要：先讀昨天存的紀錄，再把今天看到的寫進去。
    # 如果反過來，今天官方清單的資料會先把舊紀錄蓋掉，就補不回來了。
    ghost_rows = _fetch_ghost_released_today(today, live_active_codes)
    upsert_status = _upsert_last_seen_rows(rows_with_release, today)

    active_rows: list[dict[str, Any]] = []
    for row in rows_with_release + ghost_rows:
        start_date = row.get("start_date")
        end_date = row.get("end_date")
        release_date = row.get("release_date")

        if not start_date or not end_date or not release_date:
            continue

        # 公告的處置期間「最後一天」當天仍受限，真正解除是期間結束後
        # 的下一個交易日（例如期間 8/13~8/21〔五〕，下一個交易日
        # 8/24〔一〕才解除）。所以顯示範圍要延伸到 release_date。
        if start_date <= today <= release_date:
            row = dict(row)
            row["period_display"] = _period_display(
                start_date, end_date, row.get("period", "")
            )
            row["is_new_today"] = start_date == today
            row["is_released_today"] = release_date == today
            active_rows.append(row)

    # 同一股票代號若重複出現（多筆處置公告，或官方清單＋補回的舊資料
    # 重疊），只保留最新一筆。
    active_rows = _dedupe_latest_by_code(active_rows)

    active_rows.sort(key=_row_sort_key)

    data = {
        "available": True,
        "message": "ok" if active_rows else "目前沒有處置中股票。",
        "trade_date": today.strftime("%Y/%m/%d"),
        "rows": active_rows,
        "total_count": len(active_rows),
        "source": "TWSE / TPEX",
        "twse_count": len(twse_rows),
        "tpex_count": len(tpex_rows),
        "ghost_count": len(ghost_rows),
        "last_seen_upsert": upsert_status,
    }

    _MARKET_DISPOSITION_CACHE[cache_key] = (now, data)

    _debug(
        "version =", MARKET_DISPOSITION_SERVICE_VERSION,
        "| twse =", len(twse_rows),
        "| tpex =", len(tpex_rows),
        "| ghosts =", len(ghost_rows),
        "| active =", len(active_rows),
    )

    return _snapshot_from_dict(data)


def _trim_item(item: dict[str, Any], limit: int = 80) -> dict[str, Any]:
    return {
        str(k): (str(v)[:limit] if v is not None else None)
        for k, v in item.items()
    }


def _supabase_get(params: dict[str, str]) -> dict[str, Any]:
    url = _supabase_table_url(MARKET_DISPOSITION_LAST_SEEN_TABLE)

    if not url:
        return {"ok": False, "error": "SUPABASE_URL not set", "rows": []}

    try:
        res = requests.get(
            url,
            headers=_supabase_headers(),
            params=params,
            timeout=15,
        )

        if res.status_code >= 400:
            return {
                "ok": False,
                "http_status": res.status_code,
                "error": res.text[:300],
                "rows": [],
            }

        return {"ok": True, "rows": res.json() or []}

    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "rows": []}


def debug_disposition_state(codes: list[str]) -> dict[str, Any]:
    """
    診斷用（只讀）：
    1. 官方 TWSE / TPEX 清單裡，指定股票目前有哪些原始資料
    2. 官方清單裡有哪些股票代號重複出現（會讓批次寫入失敗的原因）
    3. last_seen 資料表裡，指定股票存了什麼、今天解除的有哪些
    """
    today = _today_tpe()
    wanted = {c.strip() for c in codes if c.strip()}

    result: dict[str, Any] = {
        "today": today.isoformat(),
        "version": MARKET_DISPOSITION_SERVICE_VERSION,
        "codes": sorted(wanted),
    }

    twse_items: list[Any] = []
    tpex_items: list[Any] = []

    try:
        r = requests.get(TWSE_URL, headers=HEADERS, timeout=TIMEOUT)
        twse_items = r.json() if r.ok else []
        result["twse_http_status"] = r.status_code
    except Exception as e:
        result["twse_error"] = f"{type(e).__name__}: {e}"

    try:
        r = requests.get(TPEX_URL, headers=HEADERS, timeout=TIMEOUT)
        tpex_items = r.json() if r.ok else []
        result["tpex_http_status"] = r.status_code
    except Exception as e:
        result["tpex_error"] = f"{type(e).__name__}: {e}"

    twse_items = twse_items if isinstance(twse_items, list) else []
    tpex_items = tpex_items if isinstance(tpex_items, list) else []

    result["twse_total_items"] = len(twse_items)
    result["tpex_total_items"] = len(tpex_items)

    def tpex_code(item: dict[str, Any]) -> str:
        for key in (
            "SecuritiesCompanyCode", "Code", "SecurityCode",
            "證券代號", "代號",
        ):
            if item.get(key):
                return _clean_text(item.get(key))
        return ""

    result["twse_entries"] = [
        _trim_item(i) for i in twse_items
        if _clean_text(i.get("Code")) in wanted
    ]
    result["tpex_entries"] = [
        _trim_item(i) for i in tpex_items
        if tpex_code(i) in wanted
    ]

    # 官方清單裡重複出現的股票代號（會讓整批寫入失敗）
    seen: dict[str, int] = {}
    for i in twse_items:
        code = _clean_text(i.get("Code"))
        if _is_stock_code(code):
            seen[code] = seen.get(code, 0) + 1
    for i in tpex_items:
        code = tpex_code(i)
        if _is_stock_code(code):
            seen[code] = seen.get(code, 0) + 1
    result["duplicate_codes_in_feed"] = sorted(
        c for c, n in seen.items() if n > 1
    )

    # last_seen 資料表狀態
    if wanted:
        in_list = ",".join(sorted(wanted))
        result["last_seen_for_codes"] = _supabase_get(
            {"select": "*", "stock_id": f"in.({in_list})"}
        )

    result["last_seen_release_today"] = _supabase_get(
        {
            "select": "stock_id,name,start_date,end_date,release_date,last_seen_date",
            "release_date": f"eq.{today.isoformat()}",
        }
    )
    total = _supabase_get({"select": "stock_id", "limit": "1000"})
    result["last_seen_total_rows"] = (
        len(total["rows"]) if total.get("ok") else total
    )
    result["last_upsert_status"] = get_last_upsert_status()

    return result


def _snapshot_from_dict(data: dict[str, Any]) -> MarketDispositionSnapshot:
    rows = list(data.get("rows", []))
    groups = _build_groups(rows)

    return MarketDispositionSnapshot(
        available=bool(data.get("available", False)),
        message=str(data.get("message", "")),
        trade_date=str(data.get("trade_date", "")),
        groups=groups,
        all_rows=rows,
        total_count=int(data.get("total_count", 0)),
        source=str(data.get("source", "TWSE / TPEX")),
        ghost_count=int(data.get("ghost_count", 0)),
        last_seen_upsert=dict(data.get("last_seen_upsert") or {}),
    )
