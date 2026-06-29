from __future__ import annotations

import os
import re
import time

import matplotlib.pyplot as plt

from config import CHART_DIR, CHART_URL_PREFIX, PUBLIC_BASE_URL, IMAGE_URL


def _safe_filename(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^0-9A-Za-z_\-.]+", "_", value)
    return value[:80] or "chart"


def publish_figure(fig, name: str) -> str:
    """
    將 Matplotlib figure 存到 static/charts，
    並回傳 LINE 可以讀取的公開圖片網址。
    """
    os.makedirs(CHART_DIR, exist_ok=True)

    timestamp = int(time.time())
    filename = f"{_safe_filename(name)}_{timestamp}.png"
    file_path = os.path.join(CHART_DIR, filename)

    try:
        fig.savefig(
            file_path,
            dpi=160,
            bbox_inches="tight",
            facecolor="white"
        )

        return f"{PUBLIC_BASE_URL}{CHART_URL_PREFIX}/{filename}"

    except Exception as exc:
        print(f"publish_figure failed: {exc}")
        return IMAGE_URL

    finally:
        plt.close(fig)
