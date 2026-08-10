from __future__ import annotations

from datetime import date

from .models import ETFHolding
from .providers.yuanta import YuantaProvider


class ETFHoldingsService:
    """ETF 持股資料統一服務。"""

    def __init__(self):
        self.yuanta = YuantaProvider()

    def get_holdings(
        self,
        etf_code: str,
        trade_date: date,
    ) -> list[ETFHolding]:
        """取得指定 ETF 指定日期的持股資料。"""

        etf_code = str(etf_code or "").strip().upper()

        if not etf_code:
            raise ValueError("ETF code is required")

        return self.yuanta.get_holdings(
            etf_code=etf_code,
            trade_date=trade_date,
        )
