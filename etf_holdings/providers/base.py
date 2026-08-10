from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..models import ETFHolding


class ETFHoldingsProvider(ABC):

    @abstractmethod
    def get_holdings(
        self,
        etf_code: str,
        trade_date: date,
    ) -> list[ETFHolding]:
        """
        取得指定 ETF、指定交易日的完整持股快照。
        """
        raise NotImplementedError
