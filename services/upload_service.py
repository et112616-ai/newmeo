from __future__ import annotations

import matplotlib.pyplot as plt

from config import IMAGE_URL


def publish_figure(fig, filename_hint: str = "chart") -> str:
    """
    第一版：使用已測試可用的固定 IMAGE_URL，確保 LINE Flex 能穩定顯示圖片。

    之後若要改成 Cloudinary / S3，只需要改這個函數，不用改其他模組。
    """
    try:
        return IMAGE_URL
    finally:
        plt.close(fig)
