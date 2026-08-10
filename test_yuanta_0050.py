from datetime import date

from etf_holdings.providers.yuanta import YuantaProvider


provider = YuantaProvider()

holdings = provider.get_holdings(
    etf_code="0050",
    trade_date=date(2026, 8, 7),
)

print()
print("=" * 60)
print("0050 元大官方 ETF 持股測試")
print("=" * 60)
print(f"筆數：{len(holdings)}")
print()

for row in holdings[:10]:
    print(
        f"{row.stock_code:6s} "
        f"{row.stock_name:10s} "
        f"{row.shares:,.0f} 股 "
        f"權重 {row.weight}"
    )

print()
print("=" * 60)
