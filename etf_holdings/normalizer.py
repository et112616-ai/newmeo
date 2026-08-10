from __future__ import annotations

import re
from typing import Any


def clean_stock_code(value: Any) -> str:
    text = str(value or "").strip()

    match = re.search(r"\b(\d{4,6})\b", text)

    if match:
        return match.group(1)

    return text


def clean_number(value: Any) -> float:
    if value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    text = (
        str(value)
        .strip()
        .replace(",", "")
        .replace("%", "")
        .replace(" ", "")
    )

    if not text:
        return 0.0

    try:
        return float(text)
    except ValueError:
        return 0.0
