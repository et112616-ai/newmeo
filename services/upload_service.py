from __future__ import annotations

import os
import re
import time
from uuid import uuid4

import matplotlib.pyplot as plt

from config import CHART_DIR, CHART_URL_PREFIX, IMAGE_URL, PUBLIC_BASE_URL


def _safe_filename(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^0-9A-Za-z_\-.]+", "_", value)
    return value[:80] or "chart"


def _ensure_chart_dir() -> None:
    if os.path.exists(CHART_DIR) and not os.path.isdir(CHART_DIR):
        os.remove(CHART_DIR)

    os.makedirs(CHART_DIR, exist_ok=True)


def publish_figure(fig, name: str) -> str:
    """
    將 Matplotlib 圖片存成公開 URL。

    重點：
    - 使用毫秒 timestamp + uuid，避免 LINE 快取或檔名覆蓋。
    - 圖片尺寸不要太大，避免 LINE 客戶端載入不穩。
    """
    try:
        _ensure_chart_dir()

        timestamp = int(time.time() * 1000)
        unique_id = uuid4().hex[:8]

        filename = f"{_safe_filename(name)}_{timestamp}_{unique_id}.png"
        file_path = os.path.join(CHART_DIR, filename)

        fig.savefig(
            file_path,
            dpi=130,
            bbox_inches="tight",
            facecolor="white",
            format="png",
        )

        return f"{PUBLIC_BASE_URL}{CHART_URL_PREFIX}/{filename}"

    except Exception as exc:
        print(f"publish_figure failed: {exc}")
        return IMAGE_URL

    finally:
        plt.close(fig)
