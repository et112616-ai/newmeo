from __future__ import annotations

import os
import time
import re
from typing import Optional

import matplotlib.pyplot as plt

from config import CHART_DIR, CHART_URL_PREFIX, PUBLIC_BASE_URL, IMAGE_URL


def _safe_filename(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^0-9A-Za-z_\-.]+", "_", value)
    return value[:80] or "chart"


def save_chart_and_get_url(
    fig,
    stock_id: str,
    mode: str,
    time_frame: str,
    fallback_url: Optional[str] = None
) -> str:
    """
    將 matplotlib figure 存到 static/charts，
    並回傳 LINE 可讀取的公開圖片 URL。
    """
    os.makedirs(CHART_DIR, exist_ok=True)

    timestamp = int(time.time())
    filename = f"{_safe_filename(stock_id)}_{_safe_filename(mode)}_{_safe_filename(time_frame)}_{timestamp}.png"
    file_path = os.path.join(CHART_DIR, filename)

    try:
        fig.savefig(
            file_path,
            dpi=160,
            bbox_inches="tight",
            facecolor="white"
        )

        public_url = f"{PUBLIC_BASE_URL}{CHART_URL_PREFIX}/{filename}"
        return public_url

    except Exception as exc:
        print(f"save_chart_and_get_url failed: {exc}")
        return fallback_url or IMAGE_URL

    finally:
        plt.close(fig)
