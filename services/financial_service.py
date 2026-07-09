from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
import os

import requests

try:
    import yfinance as yf
except Exception:
    yf = None

try:
    from config import FINMIND_TOKEN
except Exception:
    FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

try:
    from services.sinopac_quote_service import get_stock_snapshot as get_shioaji_stock_snapshot
except Exception:
    def get_shioaji_stock_snapshot(stock_id: str):
        return None


FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


@dataclass
class FinancialSnapshot:
    available: bool
    message: str
    stock_id: str
    stock_name: str
    rows: list[dict[str, Any]]
    latest_quarter: str = ""
    latest_eps: float = 0.0
    latest_eps_change: float = 0.0
    latest_ttm_eps: float = 0.0
    current_price: float = 0.0
    current_pe: float = 0.0
    source: str = "FinMind"


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None:
            return default

        text = str(value).replace(",", "").replace("%", "").strip()

        if text in {"", "--", "-", "nan", "None"}:
            return default

        return float(text)

    except Exception:
        return default


def _clean_stock_id(stock_id: str) -> str:
    return str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()


def _supabase_headers() -> dict[str, str]:
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _supabase_url(path: str) -> str:
    base = os.getenv("SUPABASE_URL", "").strip().rstrip("/")

    if not base:
        return ""

    return f"{base}/rest/v1/{path.lstrip('/')}"


def _quarter_from_date(date_text: str) -> tuple[int, int, str]:
    dt = datetime.strptime(str(date_text)[:10], "%Y-%m-%d")
    quarter = (dt.month - 1) // 3 + 1
    label = f"{dt.year % 100:02d}Q{quarter}"

    return dt.year, quarter, label


def _start_date_years(years: int = 6) -> str:
    return (datetime.utcnow().date() - timedelta(days=365 * years + 120)).strftime("%Y-%m-%d")


def _request_finmind_financial_statements(stock_id: str, start_date: str = "") -> list[dict[str, Any]]:
    sid = _clean_stock_id(stock_id)

    params = {
        "dataset": "TaiwanStockFinancialStatements",
        "data_id": sid,
        "start_date": start_date or _start_date_years(6),
    }

    token = str(FINMIND_TOKEN or os.getenv("FINMIND_TOKEN", "") or "").strip()

    if token:
        params["token"] = token

    try:
        res = requests.get(FINMIND_URL, params=params, timeout=20)

        if res.status_code >= 400:
            print(
                "DEBUG financial finmind failed",
                "| stock_id =",
                sid,
                "| status =",
                res.status_code,
                "| body =",
                res.text[:200],
                flush=True,
            )
            return []

        payload = res.json()
        rows = payload.get("data") or []

        if not isinstance(rows, list):
            return []

        return rows

    except Exception as exc:
        print(
            "DEBUG financial finmind exception",
            "| stock_id =",
            sid,
            "| error =",
            repr(exc),
            flush=True,
        )
        return []


def _extract_eps_quarters(
    stock_id: str,
    stock_name: str,
    raw_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sid = _clean_stock_id(stock_id)
    eps_rows: list[dict[str, Any]] = []

    for r in raw_rows or []:
        row_type = str(r.get("type") or "").strip()
        origin_name = str(r.get("origin_name") or "").strip()

        if row_type != "EPS" and "基本每股盈餘" not in origin_name:
            continue

        date_text = str(r.get("date") or "").strip()

        if not date_text:
            continue

        eps = _safe_float(r.get("value"), default=None)

        if eps is None:
            continue

        try:
            year, quarter, label = _quarter_from_date(date_text)
        except Exception:
            continue

        eps_rows.append(
            {
                "stock_id": sid,
                "stock_name": stock_name,
                "fiscal_year": year,
                "fiscal_quarter": quarter,
                "quarter_label": label,
                "eps": eps,
                "source": "FinMind",
            }
        )

    by_key: dict[tuple[int, int], dict[str, Any]] = {}

    for row in sorted(eps_rows, key=lambda x: (x["fiscal_year"], x["fiscal_quarter"])):
        by_key[(row["fiscal_year"], row["fiscal_quarter"])] = row

    rows = list(by_key.values())
    rows = sorted(rows, key=lambda x: (x["fiscal_year"], x["fiscal_quarter"]))

    for i, row in enumerate(rows):
        prev = rows[i - 1] if i > 0 else None
        row["eps_change"] = (
            round(row["eps"] - prev["eps"], 4)
            if prev is not None and prev.get("eps") is not None
            else None
        )

        yoy = None

        for old in rows:
            if (
                old["fiscal_year"] == row["fiscal_year"] - 1
                and old["fiscal_quarter"] == row["fiscal_quarter"]
            ):
                yoy = round(row["eps"] - old["eps"], 4)
                break

        row["eps_yoy_change"] = yoy

        if i >= 3:
            last4 = rows[i - 3 : i + 1]
            row["ttm_eps"] = round(sum(float(x.get("eps") or 0) for x in last4), 4)
        else:
            row["ttm_eps"] = None

    return rows


def _upsert_financial_rows(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0

    url = _supabase_url("stock_financial_quarterly")

    if not url:
        print("DEBUG financial upsert skip: SUPABASE_URL missing", flush=True)
        return 0

    headers = _supabase_headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"

    try:
        res = requests.post(
            url,
            headers=headers,
            params={"on_conflict": "stock_id,fiscal_year,fiscal_quarter"},
            json=rows,
            timeout=20,
        )

        if res.status_code >= 400:
            print(
                "DEBUG financial supabase upsert failed",
                "| status =",
                res.status_code,
                "| body =",
                res.text[:300],
                flush=True,
            )
            return 0

        return len(rows)

    except Exception as exc:
        print(
            "DEBUG financial supabase upsert exception",
            "| error =",
            repr(exc),
            flush=True,
        )
        return 0


def sync_stock_financial_quarterly(
    stock_id: str,
    stock_name: str = "",
    start_date: str = "",
) -> dict[str, Any]:
    sid = _clean_stock_id(stock_id)

    raw_rows = _request_finmind_financial_statements(
        sid,
        start_date=start_date or _start_date_years(6),
    )

    eps_rows = _extract_eps_quarters(
        sid,
        stock_name,
        raw_rows,
    )

    saved = _upsert_financial_rows(eps_rows)

    print(
        "DEBUG financial sync",
        "| stock_id =",
        sid,
        "| raw_rows =",
        len(raw_rows),
        "| eps_rows =",
        len(eps_rows),
        "| saved =",
        saved,
        flush=True,
    )

    return {
        "stock_id": sid,
        "raw_rows": len(raw_rows),
        "eps_rows": len(eps_rows),
        "saved": saved,
    }


def get_financial_rows(stock_id: str, limit: int = 8) -> list[dict[str, Any]]:
    sid = _clean_stock_id(stock_id)
    url = _supabase_url("stock_financial_quarterly")

    if not url:
        return []

    try:
        res = requests.get(
            url,
            headers=_supabase_headers(),
            params={
                "stock_id": f"eq.{sid}",
                "select": "*",
                "order": "fiscal_year.desc,fiscal_quarter.desc",
                "limit": str(limit),
            },
            timeout=15,
        )

        if res.status_code >= 400:
            print(
                "DEBUG financial supabase select failed",
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
            "DEBUG financial supabase select exception",
            "| stock_id =",
            sid,
            "| error =",
            repr(exc),
            flush=True,
        )
        return []


def _get_current_price(stock_id: str) -> float:
    sid = _clean_stock_id(stock_id)

    try:
        q = get_shioaji_stock_snapshot(sid)

        if isinstance(q, dict):
            for key in ["close", "price", "last_price", "last", "Close"]:
                price = _safe_float(q.get(key))

                if price and price > 0:
                    return float(price)

    except Exception:
        pass

    if yf is not None:
        for symbol in [f"{sid}.TW", f"{sid}.TWO"]:
            try:
                ticker = yf.Ticker(symbol)

                try:
                    fast_info = ticker.fast_info

                    for key in ["last_price", "regularMarketPrice", "lastPrice"]:
                        try:
                            price = _safe_float(fast_info.get(key))

                            if price and price > 0:
                                return float(price)

                        except Exception:
                            pass

                except Exception:
                    pass

                try:
                    info = ticker.info or {}

                    for key in ["regularMarketPrice", "currentPrice", "lastPrice"]:
                        price = _safe_float(info.get(key))

                        if price and price > 0:
                            return float(price)

                except Exception:
                    pass

            except Exception:
                continue

    return 0.0


def get_financial_snapshot(
    stock_id: str,
    stock_name: str = "",
    auto_sync: bool = True,
) -> FinancialSnapshot:
    sid = _clean_stock_id(stock_id)

    rows = get_financial_rows(sid, limit=8)

    if auto_sync and len(rows) < 4:
        sync_stock_financial_quarterly(sid, stock_name)
        rows = get_financial_rows(sid, limit=8)

    if not rows:
        return FinancialSnapshot(
            available=False,
            message="目前查無 EPS 財務資料。",
            stock_id=sid,
            stock_name=stock_name,
            rows=[],
        )

    latest = rows[0]
    latest_ttm_eps = float(_safe_float(latest.get("ttm_eps")) or 0)
    current_price = _get_current_price(sid)

    current_pe = (
        round(current_price / latest_ttm_eps, 2)
        if current_price > 0 and latest_ttm_eps > 0
        else 0.0
    )

    return FinancialSnapshot(
        available=True,
        message="ok",
        stock_id=sid,
        stock_name=stock_name,
        rows=rows,
        latest_quarter=str(latest.get("quarter_label") or ""),
        latest_eps=float(_safe_float(latest.get("eps")) or 0),
        latest_eps_change=float(_safe_float(latest.get("eps_change")) or 0),
        latest_ttm_eps=latest_ttm_eps,
        current_price=current_price,
        current_pe=current_pe,
        source=str(latest.get("source") or "FinMind"),
    )
