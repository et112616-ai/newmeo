from __future__ import annotations

from datetime import date
from typing import Any

import requests

from ..models import ETFHolding


class UnifiedProvider:
    """統一投信 ETF 官方資料 Provider。"""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def get_holdings(
        self,
        etf_code: str,
        trade_date: date,
    ) -> list[ETFHolding]:

        etf_code = str(etf_code or "").strip().upper()

        if not etf_code:
            raise ValueError("ETF code is required")

        raise NotImplementedError(
            f"UnifiedProvider 尚未設定官方資料來源: "
            f"{etf_code} {trade_date}"
        )
