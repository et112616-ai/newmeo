from __future__ import annotations


def signed_number(value: float, digits: int = 2) -> str:
    return f"{value:+.{digits}f}"


def signed_percent(value: float, digits: int = 2) -> str:
    return f"{value:+.{digits}f}%"


def format_int(value: float | int | None) -> str:
    if value is None:
        return "--"
    try:
        return f"{int(round(float(value))):,}"
    except Exception:
        return "--"


def normalize_time_frame(tf: str | None) -> str:
    tf = (tf or "D").strip().upper()
    aliases = {
        "1M": "1m",
        "1MIN": "1m",
        "5M": "5m",
        "5MIN": "5m",
        "DAY": "D",
        "DAILY": "D",
        "WEEK": "W",
        "WEEKLY": "W",
        "MONTH": "M",
        "MONTHLY": "M",
    }
    return aliases.get(tf, tf if tf in {"1m", "5m", "D", "W", "M"} else "D")


def trend_color(change: float | None) -> str:
    # 台股慣例：漲紅、跌綠
    if change is None:
        return "#8E8E93"
    return "#FF3B30" if change >= 0 else "#34C759"
