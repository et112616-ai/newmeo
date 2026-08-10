from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class ETFHolding:
    etf_code: str
    trade_date: date
    stock_code: str
    stock_name: str
    shares: float
    weight: float | None = None
    market_value: float | None = None
    source: str = ""
    source_url: str = ""
