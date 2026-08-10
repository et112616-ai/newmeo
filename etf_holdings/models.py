from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class ETFHolding:
    etf_code: str
    trade_date: date

    stock_code: str
    stock_name: str

    shares: float

    weight: Optional[float] = None
    market_value: Optional[float] = None

    source: str = ""
    source_url: str = ""
