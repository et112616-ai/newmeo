from __future__ import annotations


def classify_etf_changes(
    yesterday: list[dict],
    today: list[dict],
) -> dict[str, list[dict]]:

    old = {
        str(row["stock_code"]): row
        for row in yesterday
    }

    new = {
        str(row["stock_code"]): row
        for row in today
    }

    added = []
    increased = []
    removed = []
    decreased = []

    all_codes = set(old) | set(new)

    for stock_code in all_codes:

        old_row = old.get(stock_code)
        new_row = new.get(stock_code)

        old_shares = float(
            old_row.get("shares", 0)
            if old_row
            else 0
        )

        new_shares = float(
            new_row.get("shares", 0)
            if new_row
            else 0
        )

        delta = new_shares - old_shares

        if delta == 0:
            continue

        stock_name = (
            new_row.get("stock_name")
            if new_row
            else old_row.get("stock_name")
        )

        item = {
            "stock_code": stock_code,
            "stock_name": stock_name or "",
            "old_shares": old_shares,
            "new_shares": new_shares,
            "delta": delta,
        }

        if old_shares == 0 and new_shares > 0:
            added.append(item)

        elif old_shares > 0 and new_shares == 0:
            removed.append(item)

        elif delta > 0:
            increased.append(item)

        else:
            decreased.append(item)

    def sort_items(items):
        return sorted(
            items,
            key=lambda x: abs(x["delta"]),
            reverse=True,
        )

    return {
        "added": sort_items(added),
        "increased": sort_items(increased),
        "removed": sort_items(removed),
        "decreased": sort_items(decreased),
    }
