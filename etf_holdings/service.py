from __future__ import annotations

from datetime import date

from .models import ETFHolding
from .providers.yuanta import YuantaProvider
from .providers.unified import UnifiedProvider


class ETFHoldingsService:

    def __init__(self):
        self.yuanta = YuantaProvider()
        self.unified = UnifiedProvider()

    def get_holdings(
        self,
        etf_code: str,
        trade_date: date,
    ) -> list[ETFHolding]:

        etf_code = str(etf_code or "").strip().upper()

        if not etf_code:
            raise ValueError("ETF code is required")

        # 元大
        if etf_code == "0050":
            return self.yuanta.get_holdings(
                etf_code=etf_code,
                trade_date=trade_date,
            )

        # 統一
        if etf_code == "00981A":
            return self.unified.get_holdings(
                etf_code=etf_code,
                trade_date=trade_date,
            )

        raise ValueError(
            f"尚未設定 ETF Provider: {etf_code}"
        )
