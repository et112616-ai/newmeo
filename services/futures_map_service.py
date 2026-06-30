from __future__ import annotations

import csv
from io import StringIO
from typing import Any

import requests

from services.supabase_service import get_supabase_client


# 政府資料開放平臺「股票期貨交易標的」CSV
# data.gov.tw dataset: 22644
TAIFEX_STOCK_FUTURES_CSV_URL = (
    "https://www.taifex.com.tw/data_gov/taifex_open_data.asp?data_name=SSFLists"
)


def _debug(*args):
    print("DEBUG futures_map |", *args, flush=True)


def _decode_response_content(content: bytes) -> str:
    """
    期交所 / data.gov CSV 可能是 utf-8-sig、big5、cp950。
    這裡逐一嘗試，避免 Render 上編碼錯誤。
    """
    for enc in ["utf-8-sig", "utf-8", "cp950", "big5"]:
        try:
            return content.decode(enc)
        except Exception:
            continue

    return content.decode("utf-8", errors="ignore")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_stock_id(value: Any) -> str:
    return _clean_text(value).replace(".TW", "").replace(".TWO", "").strip()


def _build_candidate_ids(futures_code: str) -> list[str]:
    """
    期交所資料通常是基礎代碼，例如：
    2408 南亞科 = CY

    FinMind 可能吃：
    CYF 或 CY

    所以自動產生：
    CY  -> CYF, CY
    CYF -> CYF, CY
    """
    code = _clean_text(futures_code).upper()

    if not code:
        return []

    candidates: list[str] = []

    if code.endswith("F"):
        candidates.append(code)
        candidates.append(code[:-1])
    else:
        candidates.append(f"{code}F")
        candidates.append(code)

    return list(dict.fromkeys(candidates))


def _is_standard_stock_future(stock_id: str, security_type: str) -> bool:
    """
    只保留普通股票期貨，排除 ETF 期貨。

    股票通常是 4 碼，例如 2330、2408。
    ETF 常見是 00 開頭，例如 0050、006208。
    """
    sid = _clean_stock_id(stock_id)
    stype = _clean_text(security_type)

    if not sid.isdigit():
        return False

    # 排除 ETF / 受益憑證 / 指數股票型基金
    if (
        "ETF" in stype.upper()
        or "受益憑證" in stype
        or "指數股票型基金" in stype
        or sid.startswith("00")
    ):
        return False

    # 普通股多為 4 碼
    if len(sid) != 4:
        return False

    return True


def _find_value(row: dict[str, Any], possible_keys: list[str]) -> str:
    """
    欄位名稱可能有空白或 BOM，這裡做寬鬆比對。
    """
    normalized = {
        str(k).replace("\ufeff", "").strip(): v
        for k, v in row.items()
    }

    for key in possible_keys:
        if key in normalized:
            return _clean_text(normalized.get(key))

    # fallback：用包含關係找欄位
    for key in possible_keys:
        for col, value in normalized.items():
            if key in col:
                return _clean_text(value)

    return ""


def fetch_stock_futures_map_from_csv() -> list[dict[str, Any]]:
    """
    從期交所 open data CSV 取得股票期貨標的對照表。
    """
    headers = {
        "User-Agent": "Mozilla/5.0",
    }

    resp = requests.get(
        TAIFEX_STOCK_FUTURES_CSV_URL,
        headers=headers,
        timeout=20,
    )

    resp.raise_for_status()

    text = _decode_response_content(resp.content)

    # 去掉空行
    lines = [
        line
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines:
        return []

    reader = csv.DictReader(StringIO("\n".join(lines)))

    records: list[dict[str, Any]] = []

    for row in reader:
        futures_code = _find_value(
            row,
            [
                "股票期貨商品代碼",
                "商品代碼",
                "期貨商品代碼",
            ],
        )

        stock_full_name = _find_value(
            row,
            [
                "標的證券",
                "標的證券名稱",
            ],
        )

        stock_id = _find_value(
            row,
            [
                "證券代號",
                "股票代號",
            ],
        )

        stock_short_name = _find_value(
            row,
            [
                "標的證券簡稱",
                "證券簡稱",
                "簡稱",
            ],
        )

        security_type = _find_value(
            row,
            [
                "標的證券種類",
                "證券種類",
            ],
        )

        stock_id = _clean_stock_id(stock_id)
        futures_code = _clean_text(futures_code).upper()
        stock_short_name = _clean_text(stock_short_name)
        stock_full_name = _clean_text(stock_full_name)

        if not stock_id or not futures_code:
            continue

        if not _is_standard_stock_future(stock_id, security_type):
            continue

        stock_name = stock_short_name or stock_full_name or stock_id

        records.append(
            {
                "stock_id": stock_id,
                "stock_name": stock_name,
                "futures_code": futures_code,
                "futures_name": f"{stock_name}期貨",
                "contract_size": 2000,
                "source": "TAIFEX_OPEN_DATA",
            }
        )

    # 去重，以 stock_id 為主
    dedup: dict[str, dict[str, Any]] = {}

    for r in records:
        dedup[r["stock_id"]] = r

    return list(dedup.values())


def sync_stock_futures_map_from_taifex() -> dict[str, Any]:
    """
    同步股票期貨對照表到 Supabase。
    """
    try:
        records = fetch_stock_futures_map_from_csv()
    except Exception as exc:
        _debug("fetch csv failed:", exc)

        return {
            "ok": False,
            "message": f"fetch csv failed: {exc}",
            "count": 0,
        }

    if not records:
        return {
            "ok": False,
            "message": "no records parsed from csv",
            "count": 0,
        }

    try:
        supabase = get_supabase_client()

        supabase.table("stock_futures_map").upsert(
            records,
            on_conflict="stock_id",
        ).execute()

    except Exception as exc:
        _debug("supabase upsert failed:", exc)

        return {
            "ok": False,
            "message": f"supabase upsert failed: {exc}",
            "count": 0,
            "sample": records[:3],
        }

    sample_2408 = [
        r for r in records
        if r.get("stock_id") == "2408"
    ]

    return {
        "ok": True,
        "message": "synced",
        "count": len(records),
        "sample_2408": sample_2408,
    }


def get_stock_futures_mapping(stock_id: str) -> dict[str, Any] | None:
    """
    從 Supabase 查股票代號對應的股票期貨代號。
    """
    sid = _clean_stock_id(stock_id)

    if not sid:
        return None

    try:
        supabase = get_supabase_client()

        resp = (
            supabase.table("stock_futures_map")
            .select("*")
            .eq("stock_id", sid)
            .limit(1)
            .execute()
        )

        data = resp.data or []

    except Exception as exc:
        _debug("get mapping failed:", sid, exc)
        return None

    if not data:
        return None

    row = data[0]

    futures_code = _clean_text(row.get("futures_code")).upper()

    row["candidates"] = _build_candidate_ids(futures_code)

    return row
