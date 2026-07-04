from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO
from typing import Any, Dict, List, Optional

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


def _today() -> datetime.date:
    return datetime.utcnow().date()


def _start_date(days: int = 90) -> str:
    return (_today() - timedelta(days=days)).strftime("%Y-%m-%d")


def _recent_dates(n: int) -> list[str]:
    today = _today()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def _clean_stock_id(stock_id: str) -> str:
    return str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()


def _request_finmind(
    dataset: str,
    stock_id: str,
    start_date: str,
    end_date: Optional[str] = None,
) -> list[dict]:
    """
    FinMind v4 通用查詢。
    重要：不要在 log 印完整 URL，避免 token 外洩。
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
                f"_request_finmind failed: "
                f"dataset={dataset}, stock_id={stock_id}, status={res.status_code}, "
                f"body={res.text[:200]}"
            )
            return []

        payload = res.json()

        if payload.get("status") not in (None, 200, "200", True):
            print(
                f"FinMind status warning: "
                f"dataset={dataset}, status={payload.get('status')}, msg={payload.get('msg')}"
            )

        rows = payload.get("data") or []

        return rows if isinstance(rows, list) else []

    except Exception as exc:
        print(f"_request_finmind failed: dataset={dataset}, stock_id={stock_id}, error={exc}")
        return []

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
    
def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").strip()

            if not value:
                return default

        return float(value)

    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_to_float(value, float(default))))
    except Exception:
        return default


def _fmt_md(date_str: str) -> str:
    """
    2026-06-26 -> 06/26
    20260626 -> 06/26
    """
    if not date_str:
        return "--"

    s = str(date_str).strip()

    try:
        if len(s) >= 10 and "-" in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%m/%d")

        if len(s) >= 8 and s[:8].isdigit():
            return datetime.strptime(s[:8], "%Y%m%d").strftime("%m/%d")

        return s

    except Exception:
        return s


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
    """
    將 FinMind 的法人類別名稱轉成本系統三大類。
    """
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


def _extract_buy_sell_value(row: dict) -> float:
    """
    抓法人買賣超，並統一轉成「張」。

    FinMind 的法人買賣超常見單位是「股」，
    所以這裡統一除以 1000，轉成「張」。

    例：
    -20,610,326 股 -> -20,610 張
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

    # 股 -> 張
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

    # temp 用來依日期彙總買賣超
    temp: dict[str, dict[str, float]] = {
        "foreign": {},
        "trust": {},
        "dealer": {},
    }

    # temp_ratio 用來依日期存持股比
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

        # 同一日期、同一法人類別可能有多筆，這裡彙總
        temp[section][date] = temp[section].get(date, 0.0) + value

        ratio = _extract_ratio(r)

        if ratio is not None:
            temp_ratio[section][date] = ratio

    for section in result:
        items = sorted(temp[section].items())[-10:]

        result[section] = [
            {
                "date": _fmt_md(d),
                "buy_sell": int(round(v)),  # 已經是「張」
                "ratio": temp_ratio[section].get(d, "--"),
            }
            for d, v in items
        ]

    if all(not result[k] for k in result):
        return _mock_institutional()

    return result
    
# ============================================================
# 大戶：FinMind + TDCC OpenData
# ============================================================

def _is_large_holder_level(level_raw: Any) -> bool:
    """
    判斷是否為千張以上。

    TDCC 股權分散表常見定義：
    第 15 級 = 1,000,001 股以上

    注意：
    這裡只能抓 level == 15。
    不可以用 >= 15，否則會把其他特殊級距也加總，造成比例超過 100%。
    """
    text = str(level_raw or "").replace(",", "").replace(" ", "").strip()

    if not text:
        return False

    if "合計" in text or "total" in text.lower():
        return False

    # 文字型級距：1,000,001以上 / 1000001以上
    if "1000001" in text and ("以上" in text or "up" in text.lower()):
        return True

    # 數字型級距：15 = 1,000,001 股以上
    try:
        level_num = int(float(text))
        return level_num == 15
    except Exception:
        return False
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
                "diff": f"{diff:+.2f}%",
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
        print(f"_read_tdcc_csv failed: {exc}")
        return pd.DataFrame()


def _request_tdcc_latest_rows(stock_id: str) -> list[dict]:
    """
    抓 TDCC 最新一週全市場集保戶股權分散表 CSV。
    這個來源不用 FinMind token。
    """
    sid = _clean_stock_id(stock_id)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    }

    for url in TDCC_LATEST_CSV_URLS:
        try:
            res = requests.get(url, headers=headers, timeout=25)

            if res.status_code >= 400:
                print(f"_request_tdcc_latest_rows failed: status={res.status_code}, url={url}")
                continue

            text = res.content.decode("utf-8-sig", errors="ignore")
            df = _read_tdcc_csv(text)

            if df.empty:
                print(f"_request_tdcc_latest_rows empty csv: url={url}")
                continue

            df.columns = [str(c).strip() for c in df.columns]

            required = [
                "資料日期",
                "證券代號",
                "持股分級",
                "占集保庫存數比例%",
            ]

            if not all(c in df.columns for c in required):
                print(f"_request_tdcc_latest_rows missing columns: {list(df.columns)}")
                continue

            df["證券代號"] = df["證券代號"].astype(str).str.strip()
            target = df[df["證券代號"] == sid]

            if target.empty:
                print(f"_request_tdcc_latest_rows no stock: stock_id={sid}, url={url}")
                continue

            return target.to_dict("records")

        except Exception as exc:
            print(f"_request_tdcc_latest_rows failed: stock_id={sid}, error={exc}")

    return []


def _large_holder_from_tdcc_latest_rows(rows: list[dict]) -> list[dict]:
    """
    TDCC OpenData CSV 通常只有最新一週，
    因此這裡只能回最新日期一筆。
    """
    if not rows:
        return []

    by_date: dict[str, float] = {}

    for r in rows:
        date = str(r.get("資料日期", "")).strip()
        level = r.get("持股分級", "")

        if not date:
            continue

        if not _is_large_holder_level(level):
            continue

        ratio = _to_float(r.get("占集保庫存數比例%"))

        by_date[date] = by_date.get(date, 0.0) + ratio

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

def _normalize_date_for_db(date_str: str) -> str:
    """
    轉成 Supabase date 欄位需要的 YYYY-MM-DD。

    支援：
    20260626
    2026-06-26
    """
    s = str(date_str or "").strip()

    if not s:
        return ""

    try:
        if len(s) >= 10 and "-" in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")

        if len(s) >= 8 and s[:8].isdigit():
            return datetime.strptime(s[:8], "%Y%m%d").strftime("%Y-%m-%d")

    except Exception:
        return ""

    return ""


def _extract_tdcc_latest_large_holder_record(stock_id: str) -> dict | None:
    """
    從 TDCC 最新 CSV 抓出某檔股票的千張大戶比例。
    回傳：
    {
        "stock_id": "2344",
        "trade_date": "2026-06-26",
        "ratio": 65.42
    }
    """
    sid = _clean_stock_id(stock_id)
    rows = _request_tdcc_latest_rows(sid)

    if not rows:
        return None

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

    if not by_date:
        return None

    trade_date, ratio = sorted(by_date.items())[-1]

    return {
        "stock_id": sid,
        "trade_date": trade_date,
        "ratio": ratio,
    }


def _extract_tdcc_large_holder_records(stock_id: str) -> list[dict]:
    """
    從 TDCC rows 抓出某檔股票所有可取得日期的千張大戶比例。

    回傳新到舊：
    [
        {
            "stock_id": "2344",
            "trade_date": "2026-07-03",
            "ratio": 65.42,
        },
        {
            "stock_id": "2344",
            "trade_date": "2026-06-27",
            "ratio": 64.80,
        },
        ...
    ]

    若 _request_tdcc_latest_rows() 只回最新一週，這裡也只會有 1 筆。
    """
    sid = _clean_stock_id(stock_id)

    rows = _request_tdcc_latest_rows(sid)

    if not rows:
        print(
            "DEBUG large_holder records",
            "| stock_id =",
            sid,
            "| rows_count = 0",
            "| records_count = 0",
            flush=True,
        )
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

    records = [
        {
            "stock_id": sid,
            "trade_date": trade_date,
            "ratio": ratio,
        }
        for trade_date, ratio in sorted(by_date.items(), reverse=True)
    ]

    print(
        "DEBUG large_holder records",
        "| stock_id =",
        sid,
        "| rows_count =",
        len(rows),
        "| records_count =",
        len(records),
        "| dates =",
        [r["trade_date"] for r in records[:10]],
        flush=True,
    )

    return records


def _extract_tdcc_latest_large_holder_record(stock_id: str) -> dict | None:
    """
    從 TDCC 抓出某檔股票最新一週千張大戶比例。
    """
    records = _extract_tdcc_large_holder_records(stock_id)

    if not records:
        return None

    return records[0]

def sync_tdcc_large_holder_history(stock_id: str, weeks: int = 6) -> dict:
    """
    嘗試把 TDCC 可取得的近 weeks 週大戶資料寫入 Supabase。

    注意：
    若 _request_tdcc_latest_rows() 只提供最新一週，
    這裡 synced_count 仍然只會是 1。
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
        ok = upsert_large_holder_history(
            stock_id=record["stock_id"],
            trade_date=record["trade_date"],
            large_holder_ratio=record["ratio"],
            source="TDCC",
        )

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



def _large_holder_from_supabase_history(stock_id: str, limit: int = 6) -> list[dict]:
    """
    從 Supabase 撈最近 6 週大戶資料。
    若資料庫目前只有 1 週，就只顯示 1 週。
    """
    sid = _clean_stock_id(stock_id)

    # 多撈 1 筆，方便計算最舊一筆變化。
    rows = get_large_holder_history_rows(sid, limit=limit + 1)

    if not rows:
        return []

    normalized = []

    for r in rows:
        date = str(r.get("trade_date", "")).strip()
        ratio = _to_float(r.get("large_holder_ratio"))

        if not date:
            continue

        normalized.append(
            {
                "date": date,
                "ratio": ratio,
            }
        )

    if not normalized:
        return []

    # Supabase 撈出來是新到舊，這裡先轉成舊到新計算 diff。
    normalized = sorted(normalized, key=lambda x: x["date"])

    output_asc = []

    for i, item in enumerate(normalized):
        ratio = item["ratio"]

        if i > 0:
            prev_ratio = normalized[i - 1]["ratio"]
            diff_text = f"{ratio - prev_ratio:+.2f}%"
        else:
            diff_text = "--"

        output_asc.append(
            {
                "date": _fmt_md(item["date"]),
                "ratio": f"{ratio:.2f}%",
                "diff": diff_text,
            }
        )

    # 回傳新到舊，最多 limit 筆。
    return output_asc[-limit:][::-1]

def _tdcc_float(value, default=0.0):
    try:
        if value is None:
            return default

        text = str(value).replace(",", "").replace("%", "").strip()

        if not text or text in {"--", "-"}:
            return default

        return float(text)

    except Exception:
        return default


def _tdcc_get(row, *keys, default=None):
    if row is None:
        return default

    if isinstance(row, dict):
        for key in keys:
            if key in row and row.get(key) is not None:
                return row.get(key)
        return default

    for key in keys:
        try:
            value = getattr(row, key)

            if value is not None:
                return value

        except Exception:
            continue

    return default


def _tdcc_date_text(value):
    text = str(value or "").strip()

    if not text:
        return "--"

    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[5:10].replace("-", "/")

    if len(text) >= 10 and text[4] == "/" and text[7] == "/":
        return text[5:10]

    if len(text) >= 5 and "/" in text:
        return text[-5:]

    return text


def _tdcc_format_ratio(value):
    number = _tdcc_float(value, default=0.0)

    if number == 0:
        return "--"

    if 0 < abs(number) <= 1:
        number = number * 100

    return f"{number:.2f}%"


def _tdcc_format_diff(value):
    if value is None:
        return "--"

    number = _tdcc_float(value, default=0.0)

    if number == 0:
        return "--"

    if 0 < abs(number) <= 1:
        number = number * 100

    return f"{number:+.2f}%"


def _tdcc_ratio_value(row):
    return _tdcc_get(
        row,
        "ratio",
        "percentage",
        "percent",
        "pct",
        "holding_ratio",
        "holder_ratio",
        "large_holder_ratio",
        "large_holder_pct",
        "over_1000_ratio",
        "over_1000_pct",
        "value",
        default=None,
    )


def _tdcc_date_value(row):
    return _tdcc_get(
        row,
        "date",
        "week",
        "data_date",
        "trade_date",
        "record_date",
        default="",
    )


def _build_large_holder_5week_rows(raw_rows):
    """
    raw_rows：原始大戶持股資料，至少要包含 date + ratio。
    回傳：最新 5 週，格式給 controller.py 使用：
    [
      {"date": "07/03", "ratio": "66.38%", "diff": "--"},
      ...
    ]
    """
    rows = list(raw_rows or [])

    if not rows:
        return []

    rows = sorted(rows, key=lambda r: str(_tdcc_date_value(r) or ""))

    result = []
    latest_rows = list(reversed(rows[-5:]))

    for row in latest_rows:
        try:
            idx = rows.index(row)
        except Exception:
            idx = -1

        ratio = _tdcc_ratio_value(row)

        diff = _tdcc_get(
            row,
            "diff",
            "change",
            "difference",
            "ratio_change",
            "pct_change",
            "week_change",
            "wow",
            "delta",
            default=None,
        )

        if diff is None and idx > 0:
            this_ratio = _tdcc_float(_tdcc_ratio_value(row), default=0.0)
            prev_ratio = _tdcc_float(_tdcc_ratio_value(rows[idx - 1]), default=0.0)

            if this_ratio and prev_ratio:
                diff = this_ratio - prev_ratio

        result.append(
            {
                "date": _tdcc_date_text(_tdcc_date_value(row)),
                "ratio": _tdcc_format_ratio(ratio),
                "diff": _tdcc_format_diff(diff),
            }
        )

    return result

def get_large_holder_table(stock_id: str) -> list[dict]:
    """
    千張大戶持股比率。

    流程：
    1. 先嘗試抓 TDCC 最新資料並寫入 Supabase。
    2. 從 Supabase 撈最近 6 週。
    3. 如果 Supabase 不足 5 週，再嘗試同步 TDCC 可取得的多週資料。
    4. 再撈一次 Supabase。
    5. 若仍無資料，至少顯示最新一週。
    """
    sid = _clean_stock_id(stock_id)

    sync_result = sync_tdcc_latest_large_holder(sid)

    history = _large_holder_from_supabase_history(sid, limit=6)

    print(
        "DEBUG large_holder table before_history_sync",
        "| stock_id =",
        sid,
        "| sync_ok =",
        sync_result.get("ok"),
        "| sync_trade_date =",
        sync_result.get("trade_date"),
        "| history_count =",
        len(history or []),
        "| history =",
        history,
        flush=True,
    )

    if len(history or []) >= 5:
        return history[:5]

    # Supabase 不足 5 週時，嘗試把 TDCC rows 裡所有可取得週別補進 Supabase。
    history_sync = sync_tdcc_large_holder_history(sid, weeks=6)

    history = _large_holder_from_supabase_history(sid, limit=6)

    print(
        "DEBUG large_holder table after_history_sync",
        "| stock_id =",
        sid,
        "| history_sync =",
        history_sync,
        "| history_count =",
        len(history or []),
        "| history =",
        history,
        flush=True,
    )

    if history:
        return history[:5]

    if sync_result.get("ok"):
        return [
            {
                "date": _fmt_md(sync_result.get("trade_date", "")),
                "ratio": f"{_to_float(sync_result.get('ratio')):.2f}%",
                "diff": "--",
            }
        ]

    return _large_holder_unavailable("Supabase/TDCC皆無資料")

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

    return rows


def _pick_first_number(row: dict, keys: list[str]) -> float:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return _to_float(row.get(key))

    return 0.0


def get_margin_table(stock_id: str) -> list[dict]:
    """
    融資券 10 日動態。
    優先使用 FinMind TaiwanStockMarginPurchaseShortSale。
    融券比 = 融券餘額 / 融資餘額 * 100%
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

        output.append(
            {
                "date": _fmt_md(date),
                "margin": _to_int(margin_balance),
                "short": _to_int(short_balance),
                "ratio": f"{ratio:.2f}%" if margin_balance else "--",
            }
        )

    return output or _mock_margin_table()
