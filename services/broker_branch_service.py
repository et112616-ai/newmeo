from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from io import StringIO
from typing import Any
import json
import os

import pandas as pd
import requests

SUPABASE_TABLE = "broker_branch_trading_daily"

@dataclass
class KeyBrokerBranchSnapshot:
    available: bool
    message: str
    stock_id: str
    broker_name: str = ""
    branch_name: str = ""
    branch_key: str = ""
    display_name: str = ""
    net_lots: float = 0.0
    avg_price: float = 0.0
    latest_date: str = ""
    trade_dates: list[str] | None = None
    side: str = ""

@dataclass
class TopBrokerBranchItem:
    display_name: str
    net_lots: float


@dataclass
class BrokerBranchTopListSnapshot:
    available: bool
    message: str
    stock_id: str
    buy_rows: list[TopBrokerBranchItem]
    sell_rows: list[TopBrokerBranchItem]
    trade_dates: list[str] | None = None

def _clean_stock_id(stock_id: str) -> str:
    return str(stock_id or "").replace(".TW", "").replace(".TWO", "").strip()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        text = str(value).strip()
        text = text.replace(",", "")
        text = text.replace("%", "")
        text = text.replace("張", "")
        text = text.replace("元", "")
        text = text.replace("--", "")
        text = text.replace("-", "-")

        if text in {"", "None", "nan"}:
            return default

        return float(text)

    except Exception:
        return default


def _to_date(value: Any) -> str:
    text = str(value or "").strip()

    if not text:
        return ""

    text = text.replace("/", "-")

    try:
        if len(text) >= 10 and "-" in text:
            return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")

        if len(text) >= 8 and text[:8].isdigit():
            return datetime.strptime(text[:8], "%Y%m%d").strftime("%Y-%m-%d")

    except Exception:
        return ""

    return ""


def _pick(row: dict, candidates: list[str], default: Any = "") -> Any:
    for key in candidates:
        if key in row and row.get(key) not in (None, "", "--", "-"):
            return row.get(key)

    return default


def _normalize_name(value: Any) -> str:
    return str(value or "").strip().replace("　", " ")


def _make_branch_key(broker_name: str, branch_name: str) -> str:
    broker = _normalize_name(broker_name)
    branch = _normalize_name(branch_name)

    if broker and branch:
        return f"{broker}-{branch}"

    return broker or branch or "UNKNOWN"


def _supabase_headers() -> dict[str, str]:
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }


def _supabase_url(path: str) -> str:
    base = os.getenv("SUPABASE_URL", "").strip().rstrip("/")

    if not base:
        raise RuntimeError("SUPABASE_URL is missing")

    return f"{base}/rest/v1/{path.lstrip('/')}"


def _upsert_rows(rows: list[dict]) -> dict:
    if not rows:
        return {
            "ok": False,
            "saved": 0,
            "message": "no rows",
        }

    url = _supabase_url(
        f"{SUPABASE_TABLE}?on_conflict=stock_id,trade_date,branch_key"
    )

    res = requests.post(
        url,
        headers=_supabase_headers(),
        data=json.dumps(rows, ensure_ascii=False),
        timeout=30,
    )

    if res.status_code >= 400:
        return {
            "ok": False,
            "saved": 0,
            "status_code": res.status_code,
            "message": res.text[:500],
        }

    try:
        payload = res.json()
    except Exception:
        payload = []

    return {
        "ok": True,
        "saved": len(payload) if isinstance(payload, list) else len(rows),
        "message": "saved",
    }


def _read_csv_text(csv_text: str) -> pd.DataFrame:
    text = str(csv_text or "").replace("\ufeff", "").strip()
    print(
        "DEBUG broker csv text",
        "| chars =",
        len(text),
        "| preview =",
        text[:200].replace("\n", "\\n"),
        flush=True,
    )

    if not text:
        return pd.DataFrame()

    # 常見：逗號、tab、分號
    for sep in [",", "\t", ";"]:
        try:
            df = pd.read_csv(StringIO(text), dtype=str, sep=sep)
            df.columns = [str(c).strip() for c in df.columns]

            if len(df.columns) >= 4 and not df.empty:
                return df.fillna("")

        except Exception:
            continue

    return pd.DataFrame()


def _normalize_amount_to_yuan(value: Any, key_name: str = "") -> float:
    """
    預設視為元。
    若欄位名稱含「千元」，乘 1000。
    若欄位名稱含「萬元」，乘 10000。
    """
    amount = _to_float(value, 0.0)

    if amount <= 0:
        return 0.0

    key = str(key_name or "")

    if "千元" in key:
        return amount * 1000

    if "萬元" in key:
        return amount * 10000

    return amount


def _row_to_record(
    row: dict,
    default_stock_id: str = "",
    default_trade_date: str = "",
    source: str = "MANUAL_CSV",
) -> dict | None:
    stock_id = _clean_stock_id(
        _pick(
            row,
            [
                "stock_id",
                "coid",
                "證券代號",
                "股票代號",
                "代號",
                "商品代號",
            ],
            default_stock_id,
        )
    )

    trade_date = _to_date(
        _pick(
            row,
            [
                "trade_date",
                "date",
                "mdate",
                "日期",
                "資料日期",
                "交易日期",
            ],
            default_trade_date,
        )
    )

    broker_name = _normalize_name(
        _pick(
            row,
            [
                "broker_name",
                "broker",
                "券商",
                "證券商",
                "券商名稱",
                "證券商名稱",
            ],
            "",
        )
    )

    branch_name = _normalize_name(
        _pick(
            row,
            [
                "branch_name",
                "branch",
                "分點",
                "分公司",
                "分點名稱",
                "券商分點",
            ],
            "",
        )
    )

    # 有些資料會把券商與分點放在同一欄
    combined = _normalize_name(
        _pick(
            row,
            [
                "broker_branch",
                "branch_key",
                "券商分點",
                "分點名稱",
            ],
            "",
        )
    )

    if combined and not branch_name:
        branch_name = combined

    branch_key = _make_branch_key(broker_name, branch_name)

    buy_lots = _to_float(
        _pick(
            row,
            [
                "buy_lots",
                "buy",
                "買進張數",
                "買進",
                "買張",
                "買進股數",
            ],
            0,
        )
    )

    sell_lots = _to_float(
        _pick(
            row,
            [
                "sell_lots",
                "sell",
                "賣出張數",
                "賣出",
                "賣張",
                "賣出股數",
            ],
            0,
        )
    )

    # 若來源是股數，轉張
    buy_key_hit = ""
    sell_key_hit = ""

    for key in ["買進股數", "buy_shares"]:
        if key in row:
            buy_key_hit = key
            break

    for key in ["賣出股數", "sell_shares"]:
        if key in row:
            sell_key_hit = key
            break

    if buy_key_hit:
        buy_lots = buy_lots / 1000.0

    if sell_key_hit:
        sell_lots = sell_lots / 1000.0

    net_lots = _to_float(
        _pick(
            row,
            [
                "net_lots",
                "net",
                "買賣超",
                "買賣超張數",
                "買賣超股數",
            ],
            0,
        )
    )

    if "買賣超股數" in row:
        net_lots = net_lots / 1000.0

    if not net_lots:
        net_lots = buy_lots - sell_lots

    buy_amount_key = ""
    sell_amount_key = ""

    for key in [
        "buy_amount",
        "買進金額",
        "買進金額(元)",
        "買進金額(千元)",
        "買進金額(萬元)",
    ]:
        if key in row:
            buy_amount_key = key
            break

    for key in [
        "sell_amount",
        "賣出金額",
        "賣出金額(元)",
        "賣出金額(千元)",
        "賣出金額(萬元)",
    ]:
        if key in row:
            sell_amount_key = key
            break

    buy_amount = _normalize_amount_to_yuan(row.get(buy_amount_key), buy_amount_key) if buy_amount_key else 0.0
    sell_amount = _normalize_amount_to_yuan(row.get(sell_amount_key), sell_amount_key) if sell_amount_key else 0.0

    avg_buy_price = _to_float(
        _pick(
            row,
            [
                "avg_buy_price",
                "buy_avg_price",
                "買進均價",
                "買均價",
            ],
            0,
        )
    )

    avg_sell_price = _to_float(
        _pick(
            row,
            [
                "avg_sell_price",
                "sell_avg_price",
                "賣出均價",
                "賣均價",
            ],
            0,
        )
    )

    if buy_amount <= 0 and avg_buy_price > 0 and buy_lots > 0:
        buy_amount = buy_lots * 1000 * avg_buy_price

    if sell_amount <= 0 and avg_sell_price > 0 and sell_lots > 0:
        sell_amount = sell_lots * 1000 * avg_sell_price

    if avg_buy_price <= 0 and buy_amount > 0 and buy_lots > 0:
        avg_buy_price = buy_amount / (buy_lots * 1000)

    if avg_sell_price <= 0 and sell_amount > 0 and sell_lots > 0:
        avg_sell_price = sell_amount / (sell_lots * 1000)

    if not stock_id or not trade_date or branch_key == "UNKNOWN":
        return None

    return {
        "stock_id": stock_id,
        "trade_date": trade_date,
        "broker_name": broker_name,
        "branch_name": branch_name,
        "branch_key": branch_key,
        "buy_lots": round(float(buy_lots), 3),
        "sell_lots": round(float(sell_lots), 3),
        "net_lots": round(float(net_lots), 3),
        "buy_amount": round(float(buy_amount), 3),
        "sell_amount": round(float(sell_amount), 3),
        "avg_buy_price": round(float(avg_buy_price), 4) if avg_buy_price else None,
        "avg_sell_price": round(float(avg_sell_price), 4) if avg_sell_price else None,
        "source": source,
        "raw": row,
        "updated_at": datetime.utcnow().isoformat(),
    }


def sync_broker_branch_csv(
    csv_text: str,
    stock_id: str = "",
    trade_date: str = "",
    source: str = "MANUAL_CSV",
) -> dict:
    df = _read_csv_text(csv_text)

    if df.empty:
        return {
            "ok": False,
            "saved": 0,
            "message": "CSV empty or unreadable",
        }

    records: list[dict] = []

    for _, series in df.iterrows():
        row = {str(k).strip(): v for k, v in series.to_dict().items()}
        record = _row_to_record(
            row,
            default_stock_id=stock_id,
            default_trade_date=trade_date,
            source=source,
        )

        if record:
            records.append(record)

    if not records:
        return {
            "ok": False,
            "saved": 0,
            "message": "No valid broker branch rows parsed",
            "columns": list(df.columns),
        }

    result = _upsert_rows(records)

    return {
        **result,
        "parsed": len(records),
        "columns": list(df.columns),
        "sample": records[:3],
    }


def _query_recent_rows(stock_id: str, lookback_days: int = 20) -> list[dict]:
    sid = _clean_stock_id(stock_id)
    start_date = (datetime.utcnow().date() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    url = _supabase_url(SUPABASE_TABLE)

    params = {
        "stock_id": f"eq.{sid}",
        "trade_date": f"gte.{start_date}",
        "select": "stock_id,trade_date,broker_name,branch_name,branch_key,buy_lots,sell_lots,net_lots,buy_amount,sell_amount,avg_buy_price,avg_sell_price,source",
        "order": "trade_date.desc",
    }

    res = requests.get(
        url,
        headers=_supabase_headers(),
        params=params,
        timeout=20,
    )

    if res.status_code >= 400:
        print(
            "DEBUG broker branch query failed",
            "| stock_id =",
            sid,
            "| status =",
            res.status_code,
            "| body =",
            res.text[:300],
            flush=True,
        )
        return []

    try:
        payload = res.json()
    except Exception:
        return []

    return payload if isinstance(payload, list) else []


def _aggregate_branch(rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}

    if not rows:
        return []

    latest_date = max(str(r.get("trade_date", "")) for r in rows)

    for r in rows:
        key = str(r.get("branch_key") or "").strip()

        if not key:
            continue

        item = grouped.setdefault(
            key,
            {
                "branch_key": key,
                "broker_name": str(r.get("broker_name") or ""),
                "branch_name": str(r.get("branch_name") or ""),
                "net_lots": 0.0,
                "latest_net_lots": 0.0,
                "buy_lots": 0.0,
                "sell_lots": 0.0,
                "buy_amount": 0.0,
                "sell_amount": 0.0,
            },
        )

        net = _to_float(r.get("net_lots"), 0.0)
        buy = _to_float(r.get("buy_lots"), 0.0)
        sell = _to_float(r.get("sell_lots"), 0.0)
        buy_amount = _to_float(r.get("buy_amount"), 0.0)
        sell_amount = _to_float(r.get("sell_amount"), 0.0)

        item["net_lots"] += net
        item["buy_lots"] += buy
        item["sell_lots"] += sell
        item["buy_amount"] += buy_amount
        item["sell_amount"] += sell_amount

        if str(r.get("trade_date", "")) == latest_date:
            item["latest_net_lots"] += net

    return list(grouped.values())


def _pick_key_branch(agg_rows: list[dict]) -> dict | None:
    if not agg_rows:
        return None

    buy_candidates = [
        r for r in agg_rows
        if _to_float(r.get("net_lots")) > 0 and _to_float(r.get("latest_net_lots")) > 0
    ]

    sell_candidates = [
        r for r in agg_rows
        if _to_float(r.get("net_lots")) < 0 and _to_float(r.get("latest_net_lots")) < 0
    ]

    best_buy = max(
        buy_candidates,
        key=lambda r: _to_float(r.get("net_lots")),
        default=None,
    )

    best_sell = min(
        sell_candidates,
        key=lambda r: _to_float(r.get("net_lots")),
        default=None,
    )

    if best_buy and best_sell:
        if abs(_to_float(best_sell.get("net_lots"))) > abs(_to_float(best_buy.get("net_lots"))):
            return best_sell

        return best_buy

    if best_buy:
        return best_buy

    if best_sell:
        return best_sell

    return max(
        agg_rows,
        key=lambda r: abs(_to_float(r.get("net_lots"))),
        default=None,
    )


def _calc_avg_price(item: dict) -> float:
    net_lots = _to_float(item.get("net_lots"), 0.0)

    if net_lots >= 0:
        buy_lots = _to_float(item.get("buy_lots"), 0.0)
        buy_amount = _to_float(item.get("buy_amount"), 0.0)

        if buy_lots > 0 and buy_amount > 0:
            return buy_amount / (buy_lots * 1000)

    else:
        sell_lots = _to_float(item.get("sell_lots"), 0.0)
        sell_amount = _to_float(item.get("sell_amount"), 0.0)

        if sell_lots > 0 and sell_amount > 0:
            return sell_amount / (sell_lots * 1000)

    return 0.0


def get_key_broker_branch(
    stock_id: str,
    trade_days: int = 3,
    lookback_days: int = 20,
) -> KeyBrokerBranchSnapshot:
    sid = _clean_stock_id(stock_id)
    rows = _query_recent_rows(sid, lookback_days=lookback_days)

    if not rows:
        return KeyBrokerBranchSnapshot(
            available=False,
            message="尚無分點資料",
            stock_id=sid,
            trade_dates=[],
        )

    all_dates = sorted(
        {
            str(r.get("trade_date", "")).strip()
            for r in rows
            if str(r.get("trade_date", "")).strip()
        },
        reverse=True,
    )

    selected_dates = all_dates[: max(1, int(trade_days))]

    selected_rows = [
        r for r in rows
        if str(r.get("trade_date", "")).strip() in selected_dates
    ]

    if not selected_rows:
        return KeyBrokerBranchSnapshot(
            available=False,
            message="近3日分點資料不足",
            stock_id=sid,
            trade_dates=selected_dates,
        )

    agg_rows = _aggregate_branch(selected_rows)
    picked = _pick_key_branch(agg_rows)

    if not picked:
        return KeyBrokerBranchSnapshot(
            available=False,
            message="找不到關鍵分點",
            stock_id=sid,
            trade_dates=selected_dates,
        )

    net_lots = _to_float(picked.get("net_lots"), 0.0)
    avg_price = _calc_avg_price(picked)

    broker_name = str(picked.get("broker_name") or "").strip()
    branch_name = str(picked.get("branch_name") or "").strip()
    branch_key = str(picked.get("branch_key") or "").strip()

    display_name = branch_key

    if broker_name and branch_name:
        display_name = f"{broker_name}-{branch_name}"

    side = "buy" if net_lots >= 0 else "sell"

    latest_date = selected_dates[0] if selected_dates else ""

    print(
        "DEBUG key broker branch",
        "| stock_id =",
        sid,
        "| dates =",
        selected_dates,
        "| branch =",
        display_name,
        "| net_lots =",
        net_lots,
        "| avg_price =",
        avg_price,
        "| side =",
        side,
        flush=True,
    )

    return KeyBrokerBranchSnapshot(
        available=True,
        message="ok",
        stock_id=sid,
        broker_name=broker_name,
        branch_name=branch_name,
        branch_key=branch_key,
        display_name=display_name,
        net_lots=net_lots,
        avg_price=avg_price,
        latest_date=latest_date,
        trade_dates=selected_dates,
        side=side,
    )
def _branch_display_name(item: dict) -> str:
    broker_name = str(item.get("broker_name") or "").strip()
    branch_name = str(item.get("branch_name") or "").strip()
    branch_key = str(item.get("branch_key") or "").strip()

    if broker_name and branch_name:
        return f"{broker_name}-{branch_name}"

    return branch_key or broker_name or branch_name or "--"


def get_top_broker_branches(
    stock_id: str,
    trade_days: int = 3,
    lookback_days: int = 20,
    top_n: int = 3,
) -> BrokerBranchTopListSnapshot:
    """
    近 N 個交易日三大買超 / 賣超分點。

    買超：
    - 近 N 日 net_lots 加總 > 0
    - 依 net_lots 由大到小排序

    賣超：
    - 近 N 日 net_lots 加總 < 0
    - 依 net_lots 由小到大排序
    """
    sid = _clean_stock_id(stock_id)
    rows = _query_recent_rows(sid, lookback_days=lookback_days)

    if not rows:
        return BrokerBranchTopListSnapshot(
            available=False,
            message="尚無分點資料",
            stock_id=sid,
            buy_rows=[],
            sell_rows=[],
            trade_dates=[],
        )

    all_dates = sorted(
        {
            str(r.get("trade_date", "")).strip()
            for r in rows
            if str(r.get("trade_date", "")).strip()
        },
        reverse=True,
    )

    selected_dates = all_dates[: max(1, int(trade_days))]

    selected_rows = [
        r for r in rows
        if str(r.get("trade_date", "")).strip() in selected_dates
    ]

    if not selected_rows:
        return BrokerBranchTopListSnapshot(
            available=False,
            message="近3日分點資料不足",
            stock_id=sid,
            buy_rows=[],
            sell_rows=[],
            trade_dates=selected_dates,
        )

    agg_rows = _aggregate_branch(selected_rows)

    buy_candidates = [
        r for r in agg_rows
        if _to_float(r.get("net_lots"), 0.0) > 0
    ]

    sell_candidates = [
        r for r in agg_rows
        if _to_float(r.get("net_lots"), 0.0) < 0
    ]

    buy_candidates = sorted(
        buy_candidates,
        key=lambda r: _to_float(r.get("net_lots"), 0.0),
        reverse=True,
    )[: max(1, int(top_n))]

    sell_candidates = sorted(
        sell_candidates,
        key=lambda r: _to_float(r.get("net_lots"), 0.0),
    )[: max(1, int(top_n))]

    buy_rows = [
        TopBrokerBranchItem(
            display_name=_branch_display_name(item),
            net_lots=_to_float(item.get("net_lots"), 0.0),
        )
        for item in buy_candidates
    ]

    sell_rows = [
        TopBrokerBranchItem(
            display_name=_branch_display_name(item),
            net_lots=_to_float(item.get("net_lots"), 0.0),
        )
        for item in sell_candidates
    ]

    print(
        "DEBUG top broker branches",
        "| stock_id =",
        sid,
        "| dates =",
        selected_dates,
        "| buy_rows =",
        [(r.display_name, r.net_lots) for r in buy_rows],
        "| sell_rows =",
        [(r.display_name, r.net_lots) for r in sell_rows],
        flush=True,
    )

    return BrokerBranchTopListSnapshot(
        available=bool(buy_rows or sell_rows),
        message="ok" if (buy_rows or sell_rows) else "查無近3日主買賣分點",
        stock_id=sid,
        buy_rows=buy_rows,
        sell_rows=sell_rows,
        trade_dates=selected_dates,
    )
