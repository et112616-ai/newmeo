from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlencode

import requests

from ..models import ETFHolding


YUANTA_API = "https://etfapi.yuantaetfs.com/ectranslation/api/bridge"


class YuantaProvider:
    """元大投信 ETF 官方資料 Provider。"""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def _build_url(
        self,
        etf_code: str,
        trade_date: date,
    ) -> str:

        params = {
            "APIType": "ETFAPI",
            "CompanyName": "YUANTAFUNDS",
            "PageName": f"/tradeInfo/pcf/{etf_code}",
            "DeviceId": "null",
            "FuncId": "PCF/Daily",
            "AppName": "ETF",
            "Device": "3",
            "Platform": "ETF",
            "ticker": etf_code,
            "ndate": trade_date.strftime("%Y%m%d"),
        }

        return f"{YUANTA_API}?{urlencode(params)}"

    def get_holdings(
        self,
        etf_code: str,
        trade_date: date,
    ) -> list[ETFHolding]:

        etf_code = str(etf_code or "").strip().upper()

        if not etf_code:
            raise ValueError("ETF code is required")

        url = self._build_url(
            etf_code,
            trade_date,
        )

        response = requests.get(
            url,
            timeout=self.timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/151.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
            },
        )

        response.raise_for_status()

        payload: Any = response.json()

        print()
        print("=" * 80)
        print("YUANTA DEBUG")
        print("=" * 80)
        print("URL:")
        print(url)
        print()
        print("HTTP STATUS:")
        print(response.status_code)
        print()
        print("PAYLOAD TYPE:")
        print(type(payload))
        print()
        print("RAW PAYLOAD:")
        print(payload)
        print()
        print("=" * 80)
        print("YUANTA URL:", url)
        print("YUANTA STATUS:", response.status_code)
        print("YUANTA PAYLOAD:", payload)

        rows = self._extract_rows(payload)

        if not rows:
            raise ValueError(
                f"Yuanta returned no holdings: "
                f"{etf_code} {trade_date}"
            )

        holdings: list[ETFHolding] = []

        for row in rows:

            stock_code = str(
                row.get("holding_ticker")
                or row.get("ticker")
                or row.get("stock_code")
                or ""
            ).strip()

            stock_name = str(
                row.get("holding_name")
                or row.get("stock_name")
                or ""
            ).strip()

            shares = self._number(
                row.get("holding_units")
                or row.get("shares")
                or row.get("quantity")
            )

            weight = self._number_or_none(
                row.get("holding_weight")
                or row.get("weight")
            )

            if not stock_code:
                continue

            # ETF / 期貨 / 其他非股票項目先不進入
            # 第一版只收台股股票代號。
            if not stock_code.isdigit():
                continue

            if shares <= 0:
                continue

            holdings.append(
                ETFHolding(
                    etf_code=etf_code,
                    trade_date=trade_date,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    shares=shares,
                    weight=weight,
                    source="yuanta_official_pcf",
                    source_url=url,
                )
            )

        if not holdings:
            raise ValueError(
                f"Yuanta holdings parsed to zero rows: "
                f"{etf_code} {trade_date}"
            )

        return holdings

    @staticmethod
    def _extract_rows(payload: Any) -> list[dict]:

        if isinstance(payload, list):
            return [
                row for row in payload
                if isinstance(row, dict)
            ]

        if not isinstance(payload, dict):
            return []

        # 常見可能位置
        for key in (
            "data",
            "Data",
            "result",
            "Result",
            "rows",
            "Rows",
        ):
            value = payload.get(key)

            if isinstance(value, list):
                return [
                    row for row in value
                    if isinstance(row, dict)
                ]

            if isinstance(value, dict):
                nested = YuantaProvider._extract_rows(value)

                if nested:
                    return nested

        return []

    @staticmethod
    def _number(value: Any) -> float:

        if value is None:
            return 0.0

        if isinstance(value, (int, float)):
            return float(value)

        text = (
            str(value)
            .strip()
            .replace(",", "")
            .replace("%", "")
        )

        if not text:
            return 0.0

        try:
            return float(text)
        except ValueError:
            return 0.0

    @classmethod
    def _number_or_none(cls, value: Any):
        if value is None:
            return None

        value = cls._number(value)

        return value if value != 0 else None
