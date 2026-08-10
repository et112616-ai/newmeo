from __future__ import annotations

from datetime import date

from .providers.yuanta import YuantaProvider


def sync_yuanta_etf_holdings(
    supabase_client,
    etf_code: str,
    trade_date: date,
) -> dict:

    provider = YuantaProvider()

    holdings = provider.get_holdings(
        etf_code=etf_code,
        trade_date=trade_date,
    )

    # 第一層資料品質保護
    if len(holdings) < 5:
        raise ValueError(
            f"Too few holdings for {etf_code}: "
            f"{len(holdings)}"
        )

    rows = []

    for holding in holdings:

        rows.append(
            {
                "etf_code": holding.etf_code,
                "trade_date": holding.trade_date.isoformat(),
                "stock_code": holding.stock_code,
                "stock_name": holding.stock_name,
                "shares": holding.shares,
                "weight": holding.weight,
                "market_value": holding.market_value,
                "source": holding.source,
                "source_url": holding.source_url,
            }
        )

    response = (
        supabase_client
        .table("etf_holdings_daily")
        .upsert(
            rows,
            on_conflict="etf_code,trade_date,stock_code",
        )
        .execute()
    )

    return {
        "etf_code": etf_code,
        "trade_date": trade_date.isoformat(),
        "holdings_count": len(holdings),
        "status": "ok",
        "source": "yuanta_official_pcf",
        "rows": response.data or [],
    }
