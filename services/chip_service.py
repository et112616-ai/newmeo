from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO
from typing import Any, Dict, List, Optional

import os
import threading
import time
import pandas as pd
import requests

from config import FINMIND_TOKEN
from services.supabase_service import (
    get_large_holder_history_rows,
    upsert_large_holder_history,
)

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

TDCC_LATEST_CSV_URLS = [
    "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",
    "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5",
]

TDCC_WEEKLY_SYNC_VERSION = "2026-07-18-v2-ONE-CSV-PARALLEL-LATEST"
_TDCC_LATEST_DF_CACHE: tuple[float, pd.DataFrame] = (0.0, pd.DataFrame())
_TDCC_LATEST_DF_CACHE_LOCK = threading.Lock()
TDCC_LATEST_DF_CACHE_TTL_SECONDS = float(
    os.getenv("TDCC_LATEST_DF_CACHE_TTL_SECONDS", "120") or 120
)


# ============================================================
# 通用工具
# ============================================================

def _today() -> datetime.date:
    return datetime.utcnow().date()


def _start_date(days: int = 90) -> str:
    return (_today() - timedelta(days=days)).strftime("%Y-%m-%d")


def _recent_dates(n: int) -> list[str]:
    today = _today()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def _clean_stock_id(stock_id: str) -> str:
    return str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").strip()

            if not value or value in {"--", "-"}:
                return default

        return float(value)

    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_to_float(value, float(default))))
    except Exception:
        return default

def _extract_holder_people(row: dict) -> int:
    """
    從 TDCC row 裡抓人數。
    """
    for key in [
        "人數",
        "people",
        "holders",
        "holder_people",
        "large_holder_people",
        "people_count",
    ]:
        if key in row:
            return _to_int(row.get(key))

    return 0

def _fmt_md(date_str: str) -> str:
    """
    2026-06-26 -> 06/26
    20260626 -> 06/26
    07/03 -> 07/03
    """
    if not date_str:
        return "--"

    s = str(date_str).strip()

    try:
        if len(s) >= 10 and "-" in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%m/%d")

        if len(s) >= 10 and "/" in s:
            return datetime.strptime(s[:10], "%Y/%m/%d").strftime("%m/%d")

        if len(s) >= 8 and s[:8].isdigit():
            return datetime.strptime(s[:8], "%Y%m%d").strftime("%m/%d")

        if len(s) >= 5 and "/" in s:
            return s[-5:]

        return s

    except Exception:
        return s


def _normalize_date_for_db(date_str: str) -> str:
    """
    轉成 Supabase date 欄位需要的 YYYY-MM-DD。

    支援：
    - 20260626
    - 2026-06-26
    - 2026/06/26
    """
    s = str(date_str or "").strip()

    if not s:
        return ""

    try:
        if len(s) >= 10 and "-" in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")

        if len(s) >= 10 and "/" in s:
            return datetime.strptime(s[:10], "%Y/%m/%d").strftime("%Y-%m-%d")

        if len(s) >= 8 and s[:8].isdigit():
            return datetime.strptime(s[:8], "%Y%m%d").strftime("%Y-%m-%d")

    except Exception:
        return ""

    return ""


# ============================================================
# FinMind 通用查詢
# ============================================================

def _request_finmind(
    dataset: str,
    stock_id: str,
    start_date: str,
    end_date: Optional[str] = None,
) -> list[dict]:
    """
    FinMind v4 通用查詢。
    注意：不要在 log 印完整 URL，避免 token 外洩。
    """
    params = {
        "dataset": dataset,
        "data_id": _clean_stock_id(stock_id),
        "start_date": start_date,
    }

    if end_date:
        params["end_date"] = end_date

    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    try:
        res = requests.get(FINMIND_URL, params=params, timeout=15)

        if res.status_code >= 400:
            print(
                "_request_finmind failed:",
                f"dataset={dataset}",
                f"stock_id={stock_id}",
                f"status={res.status_code}",
                f"body={res.text[:200]}",
                flush=True,
            )
            return []

        payload = res.json()

        if payload.get("status") not in (None, 200, "200", True):
            print(
                "FinMind status warning:",
                f"dataset={dataset}",
                f"status={payload.get('status')}",
                f"msg={payload.get('msg')}",
                flush=True,
            )

        rows = payload.get("data") or []

        return rows if isinstance(rows, list) else []

    except Exception as exc:
        print(
            "_request_finmind failed:",
            f"dataset={dataset}",
            f"stock_id={stock_id}",
            f"error={exc}",
            flush=True,
        )
        return []


# ============================================================
# 法人
# ============================================================

def _mock_institutional() -> Dict[str, List[dict]]:
    dates = _recent_dates(10)
    base = [1200, -850, 430, 2100, -1500, 600, -300, 900, -450, 2300]

    return {
        "foreign": [
            {
                "date": _fmt_md(d),
                "buy_sell": v,
                "ratio": 23.15 + i * 0.03,
            }
            for i, (d, v) in enumerate(zip(dates, base))
        ],
        "trust": [
            {
                "date": _fmt_md(d),
                "buy_sell": int(v * 0.25),
                "ratio": 2.35 + i * 0.01,
            }
            for i, (d, v) in enumerate(zip(dates, base))
        ],
        "dealer": [
            {
                "date": _fmt_md(d),
                "buy_sell": int(v * -0.15),
                "ratio": 1.12 + i * 0.005,
            }
            for i, (d, v) in enumerate(zip(dates, base))
        ],
    }


def _normalize_institution_name(name: str) -> str | None:
    text = str(name or "").strip()

    key_map = {
        # 外資
        "Foreign_Investor": "foreign",
        "Foreign_Investor_Self": "foreign",
        "Foreign_Dealer_Self": "foreign",
        "Foreign_Investor_Dealer": "foreign",
        "外資": "foreign",
        "外資及陸資": "foreign",
        "外資及陸資不含外資自營商": "foreign",

        # 投信
        "Investment_Trust": "trust",
        "投信": "trust",

        # 自營商
        "Dealer": "dealer",
        "Dealer_self": "dealer",
        "Dealer_Hedging": "dealer",
        "自營商": "dealer",
        "自營商自行買賣": "dealer",
        "自營商避險": "dealer",
    }

    return key_map.get(text)


def _extract_ratio(row: dict):
    """
    嘗試從 FinMind row 裡抓持股比率。
    若資料源沒有提供，回傳 None，圖上會顯示 --。
    """
    for key in [
        "holding_ratio",
        "shareholding_ratio",
        "foreign_investor_ratio",
        "foreign_ratio",
        "ratio",
        "持股比率",
        "持股比",
        "percentage",
        "percent",
    ]:
        if key in row and row.get(key) not in (None, "", "--"):
            try:
                return float(
                    str(row.get(key))
                    .replace("%", "")
                    .replace(",", "")
                    .strip()
                )
            except Exception:
                pass

    return None


def _extract_buy_sell_value(row: dict) -> float:
    """
    抓法人買賣超，並統一轉成「張」。

    FinMind 的法人買賣超常見單位是「股」，
    所以這裡統一除以 1000，轉成「張」。
    """
    value_shares = 0.0

    if "buy" in row and "sell" in row:
        value_shares = _to_float(row.get("buy")) - _to_float(row.get("sell"))
    else:
        for key in [
            "buy_sell",
            "buy_sell_amount",
            "net_buy_sell",
            "買賣超",
            "買賣超股數",
        ]:
            if key in row:
                value_shares = _to_float(row.get(key))
                break

    return value_shares / 1000.0


def get_institutional_chips(stock_id: str) -> Dict[str, List[dict]]:
    """
    三大法人買賣超。

    回傳格式：
    {
      "foreign": [
        {"date": "06/30", "buy_sell": 1234, "ratio": 12.34}
      ],
      "trust": [...],
      "dealer": [...]
    }

    注意：
    FinMind 的法人買賣超資料不一定提供「持股比」。
    若沒有 ratio 欄位，會回傳 "--"。
    """
    start_date = _start_date(45)

    dataset_candidates = [
        "TaiwanStockInstitutionalInvestorsBuySell",
        "InstitutionalInvestorsBuySell",
    ]

    rows: list[dict] = []

    for dataset in dataset_candidates:
        rows = _request_finmind(dataset, stock_id, start_date)

        if rows:
            break

    if not rows:
        return _mock_institutional()

    result = {
        "foreign": [],
        "trust": [],
        "dealer": [],
    }

    temp: dict[str, dict[str, float]] = {
        "foreign": {},
        "trust": {},
        "dealer": {},
    }

    temp_ratio: dict[str, dict[str, Any]] = {
        "foreign": {},
        "trust": {},
        "dealer": {},
    }

    for r in rows:
        name = (
            r.get("name")
            or r.get("institutional_investors")
            or r.get("investor")
            or r.get("type")
            or ""
        )

        section = _normalize_institution_name(str(name))

        if not section:
            continue

        date = str(r.get("date", ""))[:10]

        if not date:
            continue

        value = _extract_buy_sell_value(r)

        temp[section][date] = temp[section].get(date, 0.0) + value

        ratio = _extract_ratio(r)

        if ratio is not None:
            temp_ratio[section][date] = ratio

    for section in result:
        items = sorted(temp[section].items())[-10:]

        result[section] = [
            {
                "date": _fmt_md(d),
                "buy_sell": int(round(v)),
                "ratio": temp_ratio[section].get(d, "--"),
            }
            for d, v in items
        ]

    if all(not result[k] for k in result):
        return _mock_institutional()

    return result


# ============================================================
# 大戶：Supabase + TDCC latest CSV + optional FinMind sponsor
# ============================================================

def _is_large_holder_level(level_raw: Any) -> bool:
    """
    判斷是否為千張以上。

    TDCC 股權分散表：
    第 15 級 = 1,000,001 股以上。

    注意：
    這裡只能抓 level == 15。
    不可以用 >= 15，否則會把其他特殊級距也加總，造成比例錯誤。
    """
    text = str(level_raw or "").replace(",", "").replace(" ", "").strip()

    if not text:
        return False

    if "合計" in text or "total" in text.lower():
        return False

    if "1000001" in text and ("以上" in text or "up" in text.lower()):
        return True

    try:
        level_num = int(float(text))
        return level_num == 15
    except Exception:
        return False



LARGE_HOLDER_THRESHOLDS = [400, 600, 800, 1000]


def _normalize_large_holder_threshold(value: Any = 1000) -> int:
    try:
        num = int(float(str(value).replace("張", "").replace("+", "").strip()))
    except Exception:
        num = 1000

    if num in {400, 600, 800, 1000}:
        return num

    return 1000


def _tdcc_holding_level_num(level_raw: Any) -> int:
    """
    TDCC 股權分散表持股分級：
    12 = 400,001 ~ 600,000 股
    13 = 600,001 ~ 800,000 股
    14 = 800,001 ~ 1,000,000 股
    15 = 1,000,001 股以上

    回傳 0 代表無法辨識或合計列。
    """
    import re

    text = str(level_raw or "").replace(",", "").replace(" ", "").strip()

    if not text:
        return 0

    lowered = text.lower()

    if "合計" in text or "total" in lowered:
        return 0

    # 直接是 1~15 分級代碼。
    try:
        if re.fullmatch(r"\d+(\.0)?", text):
            level_num = int(float(text))
            if 1 <= level_num <= 20:
                return level_num
    except Exception:
        pass

    # 文字級距。
    if "1000001" in text and ("以上" in text or "up" in lowered):
        return 15
    if "800001" in text and "1000000" in text:
        return 14
    if "600001" in text and "800000" in text:
        return 13
    if "400001" in text and "600000" in text:
        return 12

    # 備援：從文字中的第一個數字推估。
    nums = re.findall(r"\d+", text)

    if not nums:
        return 0

    try:
        first = int(nums[0])

        if first >= 1000001:
            return 15
        if first >= 800001:
            return 14
        if first >= 600001:
            return 13
        if first >= 400001:
            return 12

    except Exception:
        pass

    return 0


def _threshold_from_tdcc_level(level_num: int) -> list[int]:
    """
    回傳此 TDCC 分級應該累計到哪些門檻。
    """
    if level_num == 15:
        return [400, 600, 800, 1000]
    if level_num == 14:
        return [400, 600, 800]
    if level_num == 13:
        return [400, 600]
    if level_num == 12:
        return [400]
    return []


def _empty_large_holder_metrics() -> dict[int, dict[str, float]]:
    return {
        threshold: {
            "ratio": 0.0,
            "people": 0,
        }
        for threshold in LARGE_HOLDER_THRESHOLDS
    }


def _calc_large_holder_threshold_metrics(rows: list[dict]) -> dict[int, dict[str, float]]:
    """
    從同一天同一檔股票的 TDCC 分級 rows，
    加總 400 / 600 / 800 / 1000 張以上人數與持股比。
    """
    metrics = _empty_large_holder_metrics()

    for r in rows or []:
        level_num = _tdcc_holding_level_num(r.get("持股分級", ""))

        if level_num <= 0:
            continue

        ratio = _to_float(r.get("占集保庫存數比例%"))
        people = _extract_holder_people(r)

        for threshold in _threshold_from_tdcc_level(level_num):
            metrics[threshold]["ratio"] += ratio
            metrics[threshold]["people"] += people

    return metrics


def _large_holder_payload_from_metrics(
    stock_id: str,
    trade_date: str,
    metrics: dict[int, dict[str, float]],
    source: str = "TDCC",
) -> dict[str, Any]:
    sid = _clean_stock_id(stock_id)

    ratio_1000 = float(metrics.get(1000, {}).get("ratio", 0.0) or 0.0)
    people_1000 = int(metrics.get(1000, {}).get("people", 0) or 0)

    payload: dict[str, Any] = {
        "stock_id": sid,
        "trade_date": trade_date,
        "large_holder_ratio": ratio_1000,
        "large_holder_people": people_1000,
        "source": source,
    }

    for threshold in LARGE_HOLDER_THRESHOLDS:
        item = metrics.get(threshold, {}) or {}
        payload[f"large_holder_{threshold}_ratio"] = float(item.get("ratio", 0.0) or 0.0)
        payload[f"large_holder_{threshold}_people"] = int(item.get("people", 0) or 0)

    return payload


def _supabase_headers_for_large_holder() -> dict[str, str]:
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


def _upsert_large_holder_history_full(payload: dict[str, Any]) -> bool:
    """
    寫入含 400 / 600 / 800 / 1000 欄位的大戶資料。
    若新欄位尚未建立，會失敗並回 False，呼叫端會 fallback 舊版 upsert。
    """
    url = _supabase_table_url("tdcc_large_holder_history")

    if not url:
        return False

    headers = _supabase_headers_for_large_holder()
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"

    try:
        res = requests.post(
            url,
            headers=headers,
            params={"on_conflict": "stock_id,trade_date"},
            json=payload,
            timeout=15,
        )

        if res.status_code >= 400:
            print(
                "DEBUG large_holder full upsert failed",
                "| stock_id =",
                payload.get("stock_id"),
                "| trade_date =",
                payload.get("trade_date"),
                "| status =",
                res.status_code,
                "| body =",
                res.text[:300],
                flush=True,
            )
            return False

        return True

    except Exception as exc:
        print(
            "DEBUG large_holder full upsert exception",
            "| stock_id =",
            payload.get("stock_id"),
            "| trade_date =",
            payload.get("trade_date"),
            "| error =",
            repr(exc),
            flush=True,
        )
        return False


def _upsert_large_holder_payload(payload: dict[str, Any], source: str = "TDCC") -> bool:
    """
    優先寫入新版完整欄位；
    若還沒跑 SQL 新增欄位，就 fallback 寫舊欄位，避免既有 1000 張功能中斷。
    """
    if not payload:
        return False

    payload = dict(payload)
    payload["source"] = source

    if _upsert_large_holder_history_full(payload):
        return True

    return bool(
        upsert_large_holder_history(
            stock_id=payload.get("stock_id"),
            trade_date=payload.get("trade_date"),
            large_holder_ratio=payload.get("large_holder_ratio"),
            large_holder_people=payload.get("large_holder_people"),
            source=source,
        )
    )


def _fetch_large_holder_history_rows_full(stock_id: str, limit: int = 8) -> list[dict]:
    """
    直接從 Supabase 讀完整欄位，避免 supabase_service 舊版 select 沒有帶到新增欄位。
    """
    sid = _clean_stock_id(stock_id)
    url = _supabase_table_url("tdcc_large_holder_history")

    if not url:
        return []

    try:
        res = requests.get(
            url,
            headers=_supabase_headers_for_large_holder(),
            params={
                "stock_id": f"eq.{sid}",
                "select": "*",
                "order": "trade_date.desc",
                "limit": str(max(1, int(limit))),
            },
            timeout=15,
        )

        if res.status_code >= 400:
            print(
                "DEBUG large_holder full select failed",
                "| stock_id =",
                sid,
                "| status =",
                res.status_code,
                "| body =",
                res.text[:300],
                flush=True,
            )
            return []

        rows = res.json()

        return rows if isinstance(rows, list) else []

    except Exception as exc:
        print(
            "DEBUG large_holder full select exception",
            "| stock_id =",
            sid,
            "| error =",
            repr(exc),
            flush=True,
        )
        return []

def _extract_holder_percent(row: dict) -> float:
    candidates = [
        "percentage",
        "percent",
        "rate",
        "ratio",
        "占集保庫存數比例%",
        "占集保庫存數比例",
        "占集保庫存比例%",
        "占集保庫存比例",
    ]

    for key in candidates:
        if key in row:
            return _to_float(row.get(key))

    return 0.0


def _large_holder_from_finmind_rows(rows: list[dict]) -> list[dict]:
    by_date: dict[str, float] = {}

    for r in rows:
        date = str(r.get("date", ""))[:10]

        if not date:
            continue

        level_raw = (
            r.get("HoldingSharesLevel")
            or r.get("holding_shares_level")
            or r.get("level")
            or r.get("持股分級")
            or r.get("Securities Holding Range")
            or ""
        )

        if not _is_large_holder_level(level_raw):
            continue

        by_date[date] = by_date.get(date, 0.0) + _extract_holder_percent(r)

    items = sorted(by_date.items())

    if not items:
        return []

    last_items = items[-6:]
    output: list[dict] = []

    for idx in range(len(last_items) - 1, -1, -1):
        date, ratio = last_items[idx]

        if idx > 0:
            prev_ratio = last_items[idx - 1][1]
            diff = ratio - prev_ratio
        else:
            diff = 0.0

        output.append(
            {
                "date": _fmt_md(date),
                "ratio": f"{ratio:.2f}%",
                "diff": f"{diff:+.2f}%" if diff else "--",
            }
        )

    return output


def _read_tdcc_csv(text: str) -> pd.DataFrame:
    """
    讀取 TDCC 集保 CSV。
    有些環境讀到的 CSV 可能沒有表頭，因此做兩段式相容。
    """
    text = text.replace("\ufeff", "").strip()

    if not text:
        return pd.DataFrame()

    try:
        df = pd.read_csv(StringIO(text), dtype=str)
        df.columns = [str(c).strip() for c in df.columns]

        if "資料日期" in df.columns and "證券代號" in df.columns:
            return df.fillna("")

    except Exception:
        pass

    try:
        df = pd.read_csv(
            StringIO(text),
            dtype=str,
            header=None,
            names=[
                "資料日期",
                "證券代號",
                "持股分級",
                "人數",
                "股數",
                "占集保庫存數比例%",
            ],
        )

        return df.fillna("")

    except Exception as exc:
        print(f"_read_tdcc_csv failed: {exc}", flush=True)
        return pd.DataFrame()


def _request_tdcc_latest_dataframe(force_refresh: bool = False) -> pd.DataFrame:
    """每次批次只下載一次 TDCC 最新全市場 CSV，其他股票共用同一份資料。"""
    global _TDCC_LATEST_DF_CACHE

    now = time.monotonic()
    cache_ts, cached_df = _TDCC_LATEST_DF_CACHE

    if (
        not force_refresh
        and cached_df is not None
        and not cached_df.empty
        and now - cache_ts < TDCC_LATEST_DF_CACHE_TTL_SECONDS
    ):
        return cached_df.copy()

    with _TDCC_LATEST_DF_CACHE_LOCK:
        now = time.monotonic()
        cache_ts, cached_df = _TDCC_LATEST_DF_CACHE

        if (
            not force_refresh
            and cached_df is not None
            and not cached_df.empty
            and now - cache_ts < TDCC_LATEST_DF_CACHE_TTL_SECONDS
        ):
            return cached_df.copy()

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            )
        }
        started = time.perf_counter()

        for url in TDCC_LATEST_CSV_URLS:
            try:
                res = requests.get(url, headers=headers, timeout=(4, 12))

                if res.status_code >= 400:
                    print(
                        "TDCC_WEEKLY csv failed",
                        "| version =", TDCC_WEEKLY_SYNC_VERSION,
                        "| status =", res.status_code,
                        "| url =", url,
                        flush=True,
                    )
                    continue

                text = res.content.decode("utf-8-sig", errors="ignore")
                df = _read_tdcc_csv(text)

                required = {
                    "資料日期",
                    "證券代號",
                    "持股分級",
                    "占集保庫存數比例%",
                }
                if df.empty or not required.issubset(set(df.columns)):
                    print(
                        "TDCC_WEEKLY csv invalid",
                        "| version =", TDCC_WEEKLY_SYNC_VERSION,
                        "| url =", url,
                        "| rows =", len(df),
                        "| columns =", list(df.columns),
                        flush=True,
                    )
                    continue

                df = df.fillna("").copy()
                df["證券代號"] = df["證券代號"].astype(str).str.strip()
                _TDCC_LATEST_DF_CACHE = (time.monotonic(), df.copy())

                print(
                    "TDCC_WEEKLY csv ready",
                    "| version =", TDCC_WEEKLY_SYNC_VERSION,
                    "| rows =", len(df),
                    "| sec =", round(time.perf_counter() - started, 3),
                    "| url =", url,
                    flush=True,
                )
                return df

            except Exception as exc:
                print(
                    "TDCC_WEEKLY csv exception",
                    "| version =", TDCC_WEEKLY_SYNC_VERSION,
                    "| url =", url,
                    "| error =", repr(exc),
                    flush=True,
                )

        print(
            "TDCC_WEEKLY csv unavailable",
            "| version =", TDCC_WEEKLY_SYNC_VERSION,
            "| sec =", round(time.perf_counter() - started, 3),
            flush=True,
        )
        return pd.DataFrame()


def _request_tdcc_latest_rows(stock_id: str) -> list[dict]:
    """
    抓 TDCC 最新一週全市場集保戶股權分散表 CSV。
    這個來源不用 FinMind token。

    注意：
    這份 open data CSV 通常只包含一個資料日期。
    回傳的 17 筆是同一天的 17 個持股分級，不是 17 週。
    """
    sid = _clean_stock_id(stock_id)

    df = _request_tdcc_latest_dataframe()

    if df.empty:
        return []

    target = df[df["證券代號"] == sid]

    if target.empty:
        print(
            "TDCC_WEEKLY stock missing",
            "| version =", TDCC_WEEKLY_SYNC_VERSION,
            "| stock_id =", sid,
            flush=True,
        )
        return []

    return target.to_dict("records")


def _extract_tdcc_large_holder_records(stock_id: str) -> list[dict]:
    """
    從 TDCC latest CSV 抓出最新一週 400 / 600 / 800 / 1000 張以上資料。
    """
    sid = _clean_stock_id(stock_id)
    rows = _request_tdcc_latest_rows(sid)

    if not rows:
        print(
            "DEBUG large_holder tdcc records",
            "| stock_id =",
            sid,
            "| rows_count = 0",
            "| records_count = 0",
            flush=True,
        )
        return []

    by_date: dict[str, list[dict]] = {}

    for r in rows:
        raw_date = str(r.get("資料日期", "")).strip()
        trade_date = _normalize_date_for_db(raw_date)

        if not trade_date:
            continue

        by_date.setdefault(trade_date, []).append(r)

    records = []

    for trade_date, date_rows in sorted(by_date.items(), reverse=True):
        metrics = _calc_large_holder_threshold_metrics(date_rows)

        payload = _large_holder_payload_from_metrics(
            stock_id=sid,
            trade_date=trade_date,
            metrics=metrics,
            source="TDCC",
        )

        records.append(
            {
                "stock_id": sid,
                "trade_date": trade_date,
                "ratio": payload["large_holder_ratio"],
                "people": payload["large_holder_people"],
                "payload": payload,
                "metrics": metrics,
            }
        )

    print(
        "DEBUG large_holder tdcc records",
        "| stock_id =",
        sid,
        "| rows_count =",
        len(rows),
        "| records_count =",
        len(records),
        "| records =",
        records[:3],
        flush=True,
    )

    return records

def _extract_tdcc_latest_large_holder_record(stock_id: str) -> dict | None:
    records = _extract_tdcc_large_holder_records(stock_id)

    if not records:
        return None

    return records[0]


def _large_holder_from_tdcc_latest_rows(rows: list[dict]) -> list[dict]:
    """
    TDCC latest CSV 通常只有最新一週，
    因此這裡只能回最新日期一筆。
    """
    if not rows:
        return []

    by_date: dict[str, float] = {}

    for r in rows:
        raw_date = str(r.get("資料日期", "")).strip()
        trade_date = _normalize_date_for_db(raw_date)
        level = r.get("持股分級", "")

        if not trade_date:
            continue

        if not _is_large_holder_level(level):
            continue

        ratio = _to_float(r.get("占集保庫存數比例%"))

        by_date[trade_date] = by_date.get(trade_date, 0.0) + ratio

    items = sorted(by_date.items())

    if not items:
        return []

    date, ratio = items[-1]

    return [
        {
            "date": _fmt_md(date),
            "ratio": f"{ratio:.2f}%",
            "diff": "--",
        }
    ]


def _large_holder_unavailable(reason: str = "資料未取得") -> list[dict]:
    return [
        {
            "date": "--",
            "ratio": "資料未取得",
            "diff": reason,
        }
    ]


def _large_holder_from_supabase_history(
    stock_id: str,
    limit: int = 6,
    threshold: int = 1000,
) -> list[dict]:
    """
    從 Supabase 撈最近幾週大戶資料。

    threshold:
    - 400 / 600 / 800 / 1000
    """
    sid = _clean_stock_id(stock_id)
    threshold = _normalize_large_holder_threshold(threshold)

    rows = _fetch_large_holder_history_rows_full(sid, limit=limit + 1)

    if not rows:
        # fallback 舊版 supabase_service
        rows = get_large_holder_history_rows(sid, limit=limit + 1)

    if not rows:
        return []

    if threshold == 1000:
        ratio_col = "large_holder_ratio"
        people_col = "large_holder_people"
    else:
        ratio_col = f"large_holder_{threshold}_ratio"
        people_col = f"large_holder_{threshold}_people"

    normalized = []

    for r in rows:
        date = str(r.get("trade_date", "")).strip()

        if not date:
            continue

        ratio = _to_float(r.get(ratio_col))
        people = _to_int(r.get(people_col))

        # 1000 張欄位若新欄位不存在，回到舊欄位。
        if threshold == 1000 and ratio <= 0:
            ratio = _to_float(r.get("large_holder_1000_ratio") or r.get("large_holder_ratio"))

        if threshold == 1000 and people <= 0:
            people = _to_int(r.get("large_holder_1000_people") or r.get("large_holder_people"))

        # 400/600/800 若尚未同步新欄位，不硬拿 1000 欄位誤導。
        if threshold != 1000 and ratio <= 0:
            continue

        normalized.append(
            {
                "date": date,
                "ratio": ratio,
                "people": people,
                "threshold": threshold,
            }
        )

    if not normalized:
        return []

    normalized = sorted(normalized, key=lambda x: x["date"])

    output_asc = []

    for i, item in enumerate(normalized):
        ratio = item["ratio"]

        if i > 0:
            prev_ratio = normalized[i - 1]["ratio"]
            diff = ratio - prev_ratio
            diff_text = f"{diff:+.2f}%" if diff else "--"
        else:
            diff_text = "--"

        output_asc.append(
            {
                "date": _fmt_md(item["date"]),
                "people": item.get("people", 0),
                "ratio": f"{ratio:.2f}%",
                "diff": diff_text,
                "threshold": threshold,
            }
        )

    return output_asc[-limit:][::-1]


def sync_tdcc_latest_large_holder(stock_id: str) -> dict:
    """
    同步單檔 TDCC latest CSV 的最新一週 400 / 600 / 800 / 1000 張以上資料。
    """
    sid = _clean_stock_id(stock_id)
    record = _extract_tdcc_latest_large_holder_record(sid)

    if not record:
        return {
            "stock_id": sid,
            "ok": False,
            "message": "TDCC 最新資料未取得",
        }

    payload = dict(record.get("payload") or {})

    if not payload:
        payload = _large_holder_payload_from_metrics(
            stock_id=record["stock_id"],
            trade_date=record["trade_date"],
            metrics=record.get("metrics") or {
                1000: {
                    "ratio": record.get("ratio", 0.0),
                    "people": record.get("people", 0),
                }
            },
            source="TDCC",
        )

    ok = _upsert_large_holder_payload(payload, source="TDCC")

    return {
        "stock_id": sid,
        "ok": bool(ok),
        "trade_date": payload.get("trade_date"),
        "ratio": payload.get("large_holder_ratio"),
        "people": payload.get("large_holder_people"),
        "ratio_400": payload.get("large_holder_400_ratio"),
        "people_400": payload.get("large_holder_400_people"),
        "ratio_600": payload.get("large_holder_600_ratio"),
        "people_600": payload.get("large_holder_600_people"),
        "ratio_800": payload.get("large_holder_800_ratio"),
        "people_800": payload.get("large_holder_800_people"),
        "ratio_1000": payload.get("large_holder_1000_ratio"),
        "people_1000": payload.get("large_holder_1000_people"),
        "message": "synced" if ok else "Supabase 寫入失敗",
    }

def sync_tdcc_large_holder_history(stock_id: str, weeks: int = 6) -> dict:
    """
    相容舊函式名稱。
    TDCC latest CSV 本身通常只有最新一週。
    """
    sid = _clean_stock_id(stock_id)

    records = _extract_tdcc_large_holder_records(sid)

    if not records:
        return {
            "stock_id": sid,
            "ok": False,
            "synced_count": 0,
            "message": "TDCC 無可同步資料",
        }

    synced_count = 0
    failed_count = 0

    for record in records[: max(1, int(weeks))]:
        payload = dict(record.get("payload") or {})

        if not payload:
            payload = _large_holder_payload_from_metrics(
                stock_id=record["stock_id"],
                trade_date=record["trade_date"],
                metrics=record.get("metrics") or {
                    1000: {
                        "ratio": record.get("ratio", 0.0),
                        "people": record.get("people", 0),
                    }
                },
                source="TDCC",
            )

        ok = _upsert_large_holder_payload(payload, source="TDCC")

        if ok:
            synced_count += 1
        else:
            failed_count += 1

    return {
        "stock_id": sid,
        "ok": synced_count > 0,
        "synced_count": synced_count,
        "failed_count": failed_count,
        "records_count": len(records),
        "dates": [r["trade_date"] for r in records[: max(1, int(weeks))]],
        "message": "synced" if synced_count > 0 else "Supabase 寫入失敗",
    }

TDCC_HISTORY_PAGE_URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
TDCC_HISTORY_AJAX_URL = TDCC_HISTORY_PAGE_URL

def _tdcc_history_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Referer": TDCC_HISTORY_PAGE_URL,
        "Origin": "https://www.tdcc.com.tw",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
    }

def _normalize_yyyymmdd(value: str) -> str:
    from datetime import datetime

    text = str(value or "").strip()

    if not text:
        return ""

    try:
        if len(text) >= 10 and "-" in text:
            return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y%m%d")

        if len(text) >= 10 and "/" in text:
            return datetime.strptime(text[:10], "%Y/%m/%d").strftime("%Y%m%d")

        if len(text) >= 8 and text[:8].isdigit():
            return text[:8]

    except Exception:
        return ""

    return ""


def _yyyymmdd_to_db_date(value: str) -> str:
    from datetime import datetime

    text = _normalize_yyyymmdd(value)

    if not text:
        return ""

    try:
        return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return ""


def _request_tdcc_available_dates_since(start_date: str = "20260626", max_dates: int = 8) -> list[str]:
    import re
    import requests

    start_yyyymmdd = _normalize_yyyymmdd(start_date)

    try:
        res = requests.get(
            TDCC_HISTORY_PAGE_URL,
            headers=_tdcc_history_headers(),
            timeout=15,
        )

        html = res.text or ""

        dates = sorted(
            {
                d
                for d in re.findall(r"20\d{6}", html)
                if not start_yyyymmdd or d >= start_yyyymmdd
            },
            reverse=True,
        )

        dates = dates[: max(1, int(max_dates))]

        print(
            "DEBUG tdcc history dates",
            "| start =",
            start_yyyymmdd,
            "| dates =",
            dates,
            flush=True,
        )

        return dates

    except Exception as exc:
        print(
            "DEBUG tdcc history dates failed",
            "| start =",
            start_yyyymmdd,
            "| error =",
            repr(exc),
            flush=True,
        )
        return []


def _find_col(columns, keywords: list[str]) -> str:
    for col in columns:
        text = str(col or "").replace(" ", "").replace("\n", "").strip()

        if all(k in text for k in keywords):
            return col

    return ""


def _parse_tdcc_history_html_to_rows(html: str, stock_id: str, sca_date: str) -> list[dict]:
    from io import StringIO

    import pandas as pd

    sid = _clean_stock_id(stock_id)
    date_text = _normalize_yyyymmdd(sca_date)

    if not html or "持股" not in html:
        return []

    try:
        tables = pd.read_html(StringIO(html))
    except Exception as exc:
        print(
            "DEBUG tdcc history parse read_html failed",
            "| stock_id =",
            sid,
            "| sca_date =",
            date_text,
            "| error =",
            repr(exc),
            flush=True,
        )
        return []

    output = []

    for table_no, df in enumerate(tables, start=1):
        if df is None or df.empty:
            continue

        df = df.fillna("")
        df.columns = [str(c).replace("\n", "").replace(" ", "").strip() for c in df.columns]

        columns = list(df.columns)

        print(
            "DEBUG tdcc history table columns",
            "| stock_id =",
            sid,
            "| sca_date =",
            date_text,
            "| table_no =",
            table_no,
            "| columns =",
            columns,
            flush=True,
        )

        level_col = (
            _find_col(columns, ["持股", "分級"])
            or _find_col(columns, ["單位數", "分級"])
            or _find_col(columns, ["分級"])
        )

        people_col = (
            _find_col(columns, ["人數"])
            or _find_col(columns, ["人", "數"])
            or _find_col(columns, ["持有人"])
        )

        percent_col = (
            _find_col(columns, ["占集保", "比例"])
            or _find_col(columns, ["庫存", "比例"])
            or _find_col(columns, ["比例"])
        )

        if not level_col or not percent_col:
            continue

        for _, row in df.iterrows():
            level = str(row.get(level_col, "")).strip()
            percent = str(row.get(percent_col, "")).strip()

            if not level or not percent:
                continue

            people = "0"

            if people_col:
                people = str(row.get(people_col, "0")).strip()

            output.append(
                {
                    "資料日期": date_text,
                    "證券代號": sid,
                    "持股分級": level,
                    "人數": people,
                    "占集保庫存數比例%": percent,
                }
            )

    print(
        "DEBUG tdcc history parse",
        "| stock_id =",
        sid,
        "| sca_date =",
        date_text,
        "| rows =",
        len(output),
        "| sample =",
        output[:3],
        flush=True,
    )

    return output

def _request_tdcc_rows_by_date(stock_id: str, sca_date: str) -> list[dict]:
    """
    用 TDCC 官網目前的 portal 頁面查單一股票、單一日期。

    重要：
    舊的 /smWeb/QryStockAjax.do 現在會回 404，
    所以這裡改成 POST 到 /portal/zh/smWeb/qryStock 本頁。
    """
    import re
    import requests

    sid = _clean_stock_id(stock_id)
    date_text = _normalize_yyyymmdd(sca_date)

    if not sid or not date_text:
        return []

    session = requests.Session()
    headers = _tdcc_history_headers()

    token = ""

    try:
        page = session.get(TDCC_HISTORY_PAGE_URL, headers=headers, timeout=20)
        html = page.text or ""

        m = re.search(
            r'name=["\']SYNCHRONIZER_TOKEN["\']\s+value=["\']([^"\']+)["\']',
            html,
            flags=re.I,
        )

        if m:
            token = m.group(1).strip()

        print(
            "DEBUG tdcc history page",
            "| stock_id =",
            sid,
            "| sca_date =",
            date_text,
            "| page_status =",
            page.status_code,
            "| token =",
            bool(token),
            "| html_len =",
            len(html),
            flush=True,
        )

    except Exception as exc:
        print(
            "DEBUG tdcc history page token failed",
            "| stock_id =",
            sid,
            "| sca_date =",
            date_text,
            "| error =",
            repr(exc),
            flush=True,
        )

    payloads = [
        {
            "method": "submit",
            "firDate": date_text,
            "scaDate": date_text,
            "sqlMethod": "StockNo",
            "stockNo": sid,
            "stockName": "",
            "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock",
            "SYNCHRONIZER_TOKEN": token,
        },
        {
            "method": "submit",
            "firDate": date_text,
            "scaDate": date_text,
            "sqlMethod": "StockNo",
            "stockNo": sid,
            "StockNo": sid,
            "stockName": "",
            "StockName": "",
            "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock",
            "SYNCHRONIZER_TOKEN": token,
        },
        {
            "scaDates": date_text,
            "scaDate": date_text,
            "SqlMethod": "StockNo",
            "StockNo": sid,
            "radioStockNo": sid,
            "StockName": "",
            "REQ_OPR": "SELECT",
            "clkStockNo": sid,
            "clkStockName": "",
        },
    ]

    # 第一優先：POST 到目前官方 portal 頁面。
    # 第二備援：若有人在環境變數指定其他 URL，就試那個。
    urls = [TDCC_HISTORY_PAGE_URL]

    # TDCC_HISTORY_AJAX_URL 現在應該等於 TDCC_HISTORY_PAGE_URL。
    # 若你保留其他值，這裡也會當備援嘗試。
    if TDCC_HISTORY_AJAX_URL not in urls:
        urls.append(TDCC_HISTORY_AJAX_URL)

    for url in urls:
        for i, payload in enumerate(payloads, start=1):
            try:
                res = session.post(
                    url,
                    data=payload,
                    headers=headers,
                    timeout=25,
                    allow_redirects=True,
                )

                text = res.text or ""

                print(
                    "DEBUG tdcc history post",
                    "| stock_id =",
                    sid,
                    "| sca_date =",
                    date_text,
                    "| url =",
                    url,
                    "| payload_no =",
                    i,
                    "| status =",
                    res.status_code,
                    "| final_url =",
                    getattr(res, "url", ""),
                    "| body_head =",
                    text[:80].replace("\n", " "),
                    flush=True,
                )

                if res.status_code >= 400:
                    continue

                rows = _parse_tdcc_history_html_to_rows(text, sid, date_text)

                if rows:
                    print(
                        "DEBUG tdcc history post ok",
                        "| stock_id =",
                        sid,
                        "| sca_date =",
                        date_text,
                        "| payload_no =",
                        i,
                        "| rows =",
                        len(rows),
                        flush=True,
                    )
                    return rows

            except Exception as exc:
                print(
                    "DEBUG tdcc history post failed",
                    "| stock_id =",
                    sid,
                    "| sca_date =",
                    date_text,
                    "| url =",
                    url,
                    "| payload_no =",
                    i,
                    "| error =",
                    repr(exc),
                    flush=True,
                )

    return []

def _extract_tdcc_large_holder_record_by_date(stock_id: str, sca_date: str) -> dict | None:
    sid = _clean_stock_id(stock_id)
    rows = _request_tdcc_rows_by_date(sid, sca_date)

    if not rows:
        return None

    trade_date = _yyyymmdd_to_db_date(sca_date)
    metrics = _calc_large_holder_threshold_metrics(rows)

    payload = _large_holder_payload_from_metrics(
        stock_id=sid,
        trade_date=trade_date,
        metrics=metrics,
        source="TDCC_HISTORY",
    )

    if _to_float(payload.get("large_holder_ratio")) <= 0:
        print(
            "DEBUG tdcc history no large holder ratio",
            "| stock_id =",
            sid,
            "| sca_date =",
            sca_date,
            "| rows =",
            rows[:3],
            "| payload =",
            payload,
            flush=True,
        )
        return None

    return {
        "stock_id": sid,
        "trade_date": trade_date,
        "ratio": payload["large_holder_ratio"],
        "people": payload["large_holder_people"],
        "payload": payload,
        "metrics": metrics,
    }

def sync_tdcc_large_holder_history_since(
    stock_id: str,
    start_date: str = "20260626",
    max_weeks: int = 8,
) -> dict:
    import time

    t0 = time.perf_counter()
    sid = _clean_stock_id(stock_id)

    dates = _request_tdcc_available_dates_since(
        start_date=start_date,
        max_dates=max_weeks,
    )

    if not dates:
        return {
            "stock_id": sid,
            "ok": False,
            "source": "TDCC_HISTORY",
            "synced_count": 0,
            "message": "TDCC 官網日期清單未取得",
        }

    records = []

    for sca_date in dates:
        record = _extract_tdcc_large_holder_record_by_date(sid, sca_date)

        if record:
            records.append(record)

    if not records:
        return {
            "stock_id": sid,
            "ok": False,
            "source": "TDCC_HISTORY",
            "available_dates": dates,
            "synced_count": 0,
            "message": "TDCC 官網歷史查詢無資料",
        }

    synced_count = 0
    failed_count = 0

    for record in records:
        payload = dict(record.get("payload") or {})

        if not payload:
            payload = _large_holder_payload_from_metrics(
                stock_id=record["stock_id"],
                trade_date=record["trade_date"],
                metrics=record.get("metrics") or {
                    1000: {
                        "ratio": record.get("ratio", 0.0),
                        "people": record.get("people", 0),
                    }
                },
                source="TDCC_HISTORY",
            )

        ok = _upsert_large_holder_payload(payload, source="TDCC_HISTORY")

        if ok:
            synced_count += 1
        else:
            failed_count += 1

    result = {
        "stock_id": sid,
        "ok": synced_count > 0,
        "source": "TDCC_HISTORY",
        "start_date": start_date,
        "available_dates": dates,
        "synced_count": synced_count,
        "failed_count": failed_count,
        "records_count": len(records),
        "dates": [r["trade_date"] for r in records],
        "seconds": round(time.perf_counter() - t0, 3),
        "message": "synced" if synced_count > 0 else "Supabase 寫入失敗",
    }

    print(
        "DEBUG tdcc history sync since",
        "| result =",
        result,
        flush=True,
    )

    return result

def sync_tdcc_latest_large_holder_many(
    stock_ids=None,
    history: bool = False,
    start_date: str | None = None,
    max_weeks: int | None = None,
    max_workers: int = 4,
) -> dict:
    """
    每週排程預設只同步 TDCC 最新一期。

    history=True 才執行多週歷史補抓，避免一般 Make 排程超過 40 秒。
    最新模式會先下載一次全市場 CSV，再平行寫入各股票的 Supabase 資料。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t0 = time.perf_counter()

    if stock_ids is None:
        stock_ids = os.getenv("TDCC_SYNC_STOCKS", "")

    if isinstance(stock_ids, str):
        raw_items = stock_ids.replace("，", ",").split(",")
    else:
        raw_items = list(stock_ids or [])

    clean_ids: list[str] = []
    for item in raw_items:
        sid = _clean_stock_id(str(item or "").strip())
        if sid and sid not in clean_ids:
            clean_ids.append(sid)

    start_date = (
        str(start_date or "").strip()
        or os.getenv("TDCC_HISTORY_START_DATE", "20260626").strip()
        or "20260626"
    )
    max_weeks = max(1, min(int(max_weeks or os.getenv("TDCC_HISTORY_MAX_WEEKS", "8")), 12))
    mode = "history" if history else "latest"

    result = {
        "ok": True,
        "version": TDCC_WEEKLY_SYNC_VERSION,
        "mode": mode,
        "source": "TDCC_HISTORY" if history else "TDCC_LATEST_CSV",
        "start_date": start_date if history else "",
        "total": len(clean_ids),
        "success": 0,
        "failed": 0,
        "items": [],
    }

    print(
        "TDCC_WEEKLY batch start",
        "| version =", TDCC_WEEKLY_SYNC_VERSION,
        "| mode =", mode,
        "| stocks =", clean_ids,
        flush=True,
    )

    if not clean_ids:
        result.update({"ok": False, "message": "no stocks", "seconds": 0.0})
        return result

    if history:
        # 歷史模式保留循序執行；請由人工或小批次呼叫，不供每週 Make 使用。
        ordered_items = []
        for sid in clean_ids:
            try:
                item = sync_tdcc_large_holder_history_since(
                    sid,
                    start_date=start_date,
                    max_weeks=max_weeks,
                )
            except Exception as exc:
                item = {"stock_id": sid, "ok": False, "error": repr(exc)}
            ordered_items.append(item)
    else:
        # 先在主執行緒下載一次；工作執行緒只做記憶體篩選與 Supabase upsert。
        latest_df = _request_tdcc_latest_dataframe(force_refresh=True)
        if latest_df.empty:
            result.update(
                {
                    "ok": False,
                    "failed": len(clean_ids),
                    "items": [
                        {"stock_id": sid, "ok": False, "message": "TDCC CSV 未取得"}
                        for sid in clean_ids
                    ],
                    "seconds": round(time.perf_counter() - t0, 3),
                }
            )
            return result

        item_by_stock: dict[str, dict] = {}
        worker_count = max(1, min(int(max_workers or 4), 6, len(clean_ids)))

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(sync_tdcc_latest_large_holder, sid): sid
                for sid in clean_ids
            }

            for future in as_completed(future_map):
                sid = future_map[future]
                try:
                    item = future.result()
                except Exception as exc:
                    item = {"stock_id": sid, "ok": False, "error": repr(exc)}
                item_by_stock[sid] = item
                print(
                    "TDCC_WEEKLY stock done",
                    "| version =", TDCC_WEEKLY_SYNC_VERSION,
                    "| stock_id =", sid,
                    "| ok =", bool(item.get("ok")),
                    "| message =", item.get("message", ""),
                    flush=True,
                )

        ordered_items = [item_by_stock[sid] for sid in clean_ids]

    result["items"] = ordered_items
    result["success"] = sum(1 for item in ordered_items if item.get("ok"))
    result["failed"] = len(ordered_items) - result["success"]
    result["ok"] = result["failed"] == 0
    result["seconds"] = round(time.perf_counter() - t0, 3)

    print(
        "TDCC_WEEKLY batch done",
        "| version =", TDCC_WEEKLY_SYNC_VERSION,
        "| mode =", mode,
        "| total =", result["total"],
        "| success =", result["success"],
        "| failed =", result["failed"],
        "| seconds =", result["seconds"],
        flush=True,
    )
    return result

def _finmind_large_holder_level(level) -> bool:
    import re

    text = str(level or "").replace(",", "").strip()

    if not text:
        return False

    if "1000001" in text:
        return True

    nums = re.findall(r"\d+", text)

    if not nums:
        return False

    try:
        first = int(nums[0])
        return first >= 1000001
    except Exception:
        return False


def _fetch_finmind_holding_shares_per(stock_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    FinMind TaiwanStockHoldingSharesPer。
    這個資料集通常需要 sponsor/backer 權限。
    """
    sid = _clean_stock_id(stock_id)

    if not FINMIND_TOKEN:
        print(
            "DEBUG finmind large_holder",
            "| stock_id =",
            sid,
            "| error = missing FINMIND_TOKEN",
            flush=True,
        )
        return pd.DataFrame()

    params = {
        "dataset": "TaiwanStockHoldingSharesPer",
        "data_id": sid,
        "start_date": start_date,
        "end_date": end_date,
        "token": FINMIND_TOKEN,
    }

    try:
        res = requests.get(FINMIND_URL, params=params, timeout=15)
        payload = res.json()
        data = payload.get("data") or []

        if not data:
            print(
                "DEBUG finmind large_holder empty",
                "| stock_id =",
                sid,
                "| status =",
                res.status_code,
                "| msg =",
                payload.get("msg") or payload.get("message") or payload,
                flush=True,
            )
            return pd.DataFrame()

        df = pd.DataFrame(data)

        print(
            "DEBUG finmind large_holder raw",
            "| stock_id =",
            sid,
            "| rows =",
            len(df),
            "| columns =",
            list(df.columns),
            flush=True,
        )

        return df

    except Exception as exc:
        print(
            "DEBUG finmind large_holder failed",
            "| stock_id =",
            sid,
            "| error =",
            repr(exc),
            flush=True,
        )
        return pd.DataFrame()


def _extract_finmind_large_holder_records(stock_id: str, weeks: int = 8) -> list[dict]:
    sid = _clean_stock_id(stock_id)

    end_date = _today()
    start_date = end_date - timedelta(days=max(90, int(weeks) * 14))

    df = _fetch_finmind_holding_shares_per(
        sid,
        start_date=start_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),
    )

    if df is None or df.empty:
        return []

    required = {"date", "stock_id", "HoldingSharesLevel", "percent"}

    if not required.issubset(set(df.columns)):
        print(
            "DEBUG finmind large_holder missing columns",
            "| stock_id =",
            sid,
            "| columns =",
            list(df.columns),
            flush=True,
        )
        return []

    work = df.copy()
    work["stock_id"] = work["stock_id"].astype(str).str.strip()
    work = work[work["stock_id"] == sid]

    if work.empty:
        return []

    work = work[work["HoldingSharesLevel"].apply(_finmind_large_holder_level)]

    if work.empty:
        print(
            "DEBUG finmind large_holder no large level",
            "| stock_id =",
            sid,
            "| sample_levels =",
            df["HoldingSharesLevel"].dropna().astype(str).unique().tolist()[:20],
            flush=True,
        )
        return []

    work["percent"] = pd.to_numeric(work["percent"], errors="coerce").fillna(0)

    grouped = (
        work.groupby("date", as_index=False)["percent"]
        .sum()
        .sort_values("date", ascending=False)
    )

    records = []

    for _, row in grouped.head(max(1, int(weeks))).iterrows():
        trade_date = str(row["date"]).strip()
        ratio = _to_float(row["percent"])

        if not trade_date or ratio <= 0:
            continue

        records.append(
            {
                "stock_id": sid,
                "trade_date": trade_date,
                "ratio": ratio,
            }
        )

    print(
        "DEBUG finmind large_holder records",
        "| stock_id =",
        sid,
        "| records_count =",
        len(records),
        "| dates =",
        [r["trade_date"] for r in records],
        flush=True,
    )

    return records


def sync_finmind_large_holder_history(stock_id: str, weeks: int = 8) -> dict:
    sid = _clean_stock_id(stock_id)

    records = _extract_finmind_large_holder_records(sid, weeks=weeks)

    if not records:
        return {
            "stock_id": sid,
            "ok": False,
            "source": "FinMind",
            "synced_count": 0,
            "message": "FinMind 無資料或無權限",
        }

    synced_count = 0
    failed_count = 0

    for record in records:
        ok = upsert_large_holder_history(
            stock_id=record["stock_id"],
            trade_date=record["trade_date"],
            large_holder_ratio=record["ratio"],
            source="FinMind",
        )

        if ok:
            synced_count += 1
        else:
            failed_count += 1

    return {
        "stock_id": sid,
        "ok": synced_count > 0,
        "source": "FinMind",
        "synced_count": synced_count,
        "failed_count": failed_count,
        "records_count": len(records),
        "dates": [r["trade_date"] for r in records],
        "message": "synced" if synced_count > 0 else "Supabase 寫入失敗",
    }


def get_large_holder_table(stock_id: str, threshold: int = 1000) -> list[dict]:
    """
    大戶持股比率。

    threshold:
    - 400：400張以上
    - 600：600張以上
    - 800：800張以上
    - 1000：1000張以上
    """
    import time

    t0 = time.perf_counter()
    sid = _clean_stock_id(stock_id)
    threshold = _normalize_large_holder_threshold(threshold)

    history = _large_holder_from_supabase_history(
        sid,
        limit=6,
        threshold=threshold,
    )

    print(
        "DEBUG large_holder table supabase_first",
        "| stock_id =",
        sid,
        "| threshold =",
        threshold,
        "| history_count =",
        len(history or []),
        "| history =",
        history,
        flush=True,
    )

    # 如果 Supabase 目前不足 2 週，第一次查詢時自動補歷史。
    # 這會把 400 / 600 / 800 / 1000 一起寫入。
    if len(history or []) < 2:
        try:
            start_date = os.getenv("TDCC_HISTORY_START_DATE", "20260626").strip() or "20260626"
            max_weeks = int(os.getenv("TDCC_HISTORY_MAX_WEEKS", "8"))

            sync_result = sync_tdcc_large_holder_history_since(
                sid,
                start_date=start_date,
                max_weeks=max_weeks,
            )

            history = _large_holder_from_supabase_history(
                sid,
                limit=6,
                threshold=threshold,
            )

            print(
                "DEBUG large_holder table after_auto_history_sync",
                "| stock_id =",
                sid,
                "| threshold =",
                threshold,
                "| sync_result =",
                sync_result,
                "| history_count =",
                len(history or []),
                "| history =",
                history,
                flush=True,
            )

        except Exception as exc:
            print(
                "DEBUG large_holder table auto_history_sync_failed",
                "| stock_id =",
                sid,
                "| threshold =",
                threshold,
                "| error =",
                repr(exc),
                flush=True,
            )

    if len(history or []) >= 5:
        return history[:5]

    # FinMind fallback 目前只維持 1000 張以上相容，不用它誤補 400/600/800。
    if threshold == 1000 and _bool_env("USE_FINMIND_LARGE_HOLDER_HISTORY", default=False):
        finmind_sync = sync_finmind_large_holder_history(sid, weeks=8)
        history = _large_holder_from_supabase_history(sid, limit=6, threshold=threshold)

        print(
            "DEBUG large_holder table after_finmind",
            "| stock_id =",
            sid,
            "| threshold =",
            threshold,
            "| finmind_sync =",
            finmind_sync,
            "| history_count =",
            len(history or []),
            "| history =",
            history,
            "| sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )

        if len(history or []) >= 5:
            return history[:5]

    # 預設不在每次 LINE 查詢同步 TDCC latest，避免查一次大戶就等太久。
    if _bool_env("LARGE_HOLDER_SYNC_ON_QUERY", default=False):
        sync_result = sync_tdcc_latest_large_holder(sid)
        history = _large_holder_from_supabase_history(sid, limit=6, threshold=threshold)

        print(
            "DEBUG large_holder table after_tdcc_latest",
            "| stock_id =",
            sid,
            "| threshold =",
            threshold,
            "| sync_result =",
            sync_result,
            "| history_count =",
            len(history or []),
            "| history =",
            history,
            "| sec =",
            round(time.perf_counter() - t0, 3),
            flush=True,
        )

    if history:
        return history[:5]

    # 如果資料庫完全沒有資料，最後才即時抓 TDCC latest CSV 補一筆。
    sync_result = sync_tdcc_latest_large_holder(sid)
    history = _large_holder_from_supabase_history(sid, limit=6, threshold=threshold)

    if history:
        return history[:5]

    if sync_result.get("ok") and threshold == 1000:
        return [
            {
                "date": _fmt_md(sync_result.get("trade_date", "")),
                "people": _to_int(sync_result.get("people")),
                "ratio": f"{_to_float(sync_result.get('ratio')):.2f}%",
                "diff": "--",
                "threshold": threshold,
            }
        ]

    return _large_holder_unavailable(f"{threshold}張以上資料未取得")


# ============================================================
# 融資券
# ============================================================

def _mock_margin_table() -> list[dict]:
    rows = [
        {"date": "6/23", "margin": 12450, "short": 1200},
        {"date": "6/22", "margin": 12100, "short": 1250},
        {"date": "6/19", "margin": 11950, "short": 1100},
        {"date": "6/18", "margin": 12000, "short": 1050},
        {"date": "6/17", "margin": 12200, "short": 980},
        {"date": "6/16", "margin": 12150, "short": 1020},
        {"date": "6/15", "margin": 11800, "short": 950},
        {"date": "6/12", "margin": 11900, "short": 900},
        {"date": "6/11", "margin": 11750, "short": 880},
        {"date": "6/10", "margin": 11600, "short": 850},
    ]

    for r in rows:
        r["ratio"] = f"{(r['short'] / r['margin'] * 100):.2f}%" if r["margin"] else "--"
        r["margin_usage_rate"] = "--"

    return rows


def _pick_first_number(row: dict, keys: list[str]) -> float:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return _to_float(row.get(key))

    return 0.0



def _extract_margin_limit(row: dict) -> float:
    """
    嘗試抓融資限額，單位通常是「張」。

    融資使用率 = 融資餘額 ÷ 融資限額 × 100%

    不同資料源欄位名稱不完全一致，所以採多組候選欄位。
    若資料源沒有提供，回傳 0，前端會顯示 --。
    """
    candidates = [
        # FinMind / English common candidates
        "MarginPurchaseLimit",
        "MarginPurchaseQuota",
        "MarginPurchaseCeiling",
        "MarginPurchaseTodayLimit",
        "margin_purchase_limit",
        "margin_purchase_quota",
        "margin_purchase_ceiling",
        "financing_limit",
        "margin_limit",
        "margin_quota",

        # Chinese candidates
        "融資限額",
        "融資最高限額",
        "融資額度",
        "融資限額張數",
        "融資限額(張)",
        "融資額度(張)",
    ]

    for key in candidates:
        if key not in row or row.get(key) in (None, "", "--", "-"):
            continue

        value = _to_float(row.get(key), 0.0)

        if value > 0:
            return value

    return 0.0


def _calculate_margin_usage_rate(
    margin_balance_lots: float,
    margin_limit_lots: float,
) -> float:
    """
    融資使用率：

    融資使用率 = 融資餘額 ÷ 融資限額 × 100%
    """
    try:
        margin_balance_lots = float(margin_balance_lots)
        margin_limit_lots = float(margin_limit_lots)

        if margin_balance_lots <= 0 or margin_limit_lots <= 0:
            return 0.0

        return margin_balance_lots / margin_limit_lots * 100

    except Exception:
        return 0.0


def get_margin_table(stock_id: str) -> list[dict]:
    """
    融資券 10 日動態。
    優先使用 FinMind TaiwanStockMarginPurchaseShortSale。

    券資比 = 融券餘額 / 融資餘額 * 100%
    融資使用率 = 融資餘額 / 融資限額 * 100%

    注意：
    若資料源沒有提供「融資限額」，融資使用率會顯示 --，不硬推估。
    """
    start_date = _start_date(45)

    rows = _request_finmind(
        dataset="TaiwanStockMarginPurchaseShortSale",
        stock_id=stock_id,
        start_date=start_date,
    )

    if not rows:
        return _mock_margin_table()

    rows = sorted(rows, key=lambda r: str(r.get("date", "")))

    output: list[dict] = []

    for r in rows[-10:][::-1]:
        date = str(r.get("date", ""))[:10]

        margin_balance = _pick_first_number(
            r,
            [
                "MarginPurchaseTodayBalance",
                "MarginPurchaseTodayBalanceAmount",
                "margin_purchase_today_balance",
                "融資餘額",
                "MarginPurchaseBalance",
            ],
        )

        short_balance = _pick_first_number(
            r,
            [
                "ShortSaleTodayBalance",
                "ShortSaleTodayBalanceAmount",
                "short_sale_today_balance",
                "融券餘額",
                "ShortSaleBalance",
            ],
        )

        ratio = (short_balance / margin_balance * 100) if margin_balance else 0.0

        margin_limit = _extract_margin_limit(r)

        margin_usage_rate = _calculate_margin_usage_rate(
            margin_balance_lots=margin_balance,
            margin_limit_lots=margin_limit,
        )

        output.append(
            {
                "date": _fmt_md(date),
                "trade_date": date,
                "margin": _to_int(margin_balance),
                "short": _to_int(short_balance),
                "ratio": f"{ratio:.2f}%" if margin_balance else "--",
                "margin_usage_rate": f"{margin_usage_rate:.2f}%" if margin_usage_rate > 0 else "--",
                "margin_limit": margin_limit,
            }
        )

    print(
        "DEBUG margin table",
        "| stock_id =",
        _clean_stock_id(stock_id),
        "| rows =",
        len(output),
        "| margin_usage_available =",
        sum(1 for r in output if r.get("margin_usage_rate") not in (None, "", "--")),
        flush=True,
    )

    return output or _mock_margin_table()
