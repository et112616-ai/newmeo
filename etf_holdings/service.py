from __future__ import annotations

from datetime import date

from .registry import get_etf_registry
from .providers.yuanta import YuantaProvider
from .providers.fuhwa import FuhwaProvider
from .providers.unified import UnifiedProvider


PROVIDERS = {
    "yuanta": YuantaProvider,
    "fuhwa": FuhwaProvider,
    "unified": UnifiedProvider,
}


def get_provider(provider_name: str):
    provider_class = PROVIDERS.get(
        str(provider_name or "").strip().lower()
    )

    if provider_class is None:
        raise ValueError(
            f"Unsupported ETF provider: {provider_name}"
        )

    return provider_class()


def get_etf_holdings(
    client,
    etf_code: str,
    trade_date: date,
):
    registry = get_etf_registry(
        client,
        etf_code,
    )

    if not registry:
        raise ValueError(
            f"ETF not found: {etf_code}"
        )

    provider = get_provider(
        registry["provider"]
    )

    return provider.get_holdings(
        etf_code=etf_code,
        trade_date=trade_date,
    )
