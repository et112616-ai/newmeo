from __future__ import annotations

import os
import re
import time
from uuid import uuid4

import matplotlib.pyplot as plt

from config import CHART_DIR, CHART_URL_PREFIX, IMAGE_URL, PUBLIC_BASE_URL
from utils.chart_style import (
    LINE_IMAGE_EXPORT_DPI,
    LINE_IMAGE_MAX_EDGE,
    LINE_IMAGE_TARGET_BYTES,
)


UPLOAD_SERVICE_VERSION = "2026-07-20-v2.1-LINE-STABLE-960PX-RGB-ATOMIC"


def _safe_filename(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^0-9A-Za-z_\-.]+", "_", value)
    return value[:80] or "chart"


def _ensure_chart_dir() -> None:
    if os.path.exists(CHART_DIR) and not os.path.isdir(CHART_DIR):
        os.remove(CHART_DIR)

    os.makedirs(CHART_DIR, exist_ok=True)


def _render_dpi_for_line(fig) -> float:
    """依畫布英吋數動態降 DPI，讓初次輸出就不超過 LINE 安全尺寸。"""
    try:
        width_in, height_in = fig.get_size_inches()
        longest_in = max(float(width_in), float(height_in), 1.0)
        return max(60.0, min(float(LINE_IMAGE_EXPORT_DPI), LINE_IMAGE_MAX_EDGE / longest_in))
    except Exception:
        return float(LINE_IMAGE_EXPORT_DPI)


def _optimize_png_for_line(source_path: str, output_path: str) -> tuple[int, int, int]:
    """
    驗證並壓縮 PNG。

    - 最長邊不超過 960px。
    - 一般先保留 RGB；若仍超過約 900KB，再降為 256 色調色盤 PNG。
    - 回傳 width, height, bytes，供 Render Log 驗證。
    """
    from PIL import Image

    with Image.open(source_path) as image:
        image.load()  # 完整解碼，避免把半張或損壞圖片發布出去。

        # LINE 官方雖支援透明 PNG，但部分桌面版／舊客戶端載入 RGBA 圖片較不穩。
        # 圖表原本就是白底，因此一律壓平成標準 RGB PNG，跨裝置最單純。
        if image.mode == "RGBA" or "A" in image.getbands():
            rgba = image.convert("RGBA")
            white = Image.new("RGB", rgba.size, "white")
            white.paste(rgba, mask=rgba.getchannel("A"))
            image = white
        elif image.mode != "RGB":
            image = image.convert("RGB")

        width, height = image.size
        longest = max(width, height)

        if longest > LINE_IMAGE_MAX_EDGE:
            ratio = LINE_IMAGE_MAX_EDGE / float(longest)
            new_size = (
                max(1, int(round(width * ratio))),
                max(1, int(round(height * ratio))),
            )
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image = image.resize(new_size, resampling)

        image.save(
            output_path,
            format="PNG",
            optimize=True,
            compress_level=8,
            dpi=(LINE_IMAGE_EXPORT_DPI, LINE_IMAGE_EXPORT_DPI),
        )

    # 線圖與數字不需要全彩；超過目標大小時再使用 256 色，避免平常無謂失真。
    if os.path.getsize(output_path) > LINE_IMAGE_TARGET_BYTES:
        with Image.open(output_path) as image:
            image.load()
            adaptive = getattr(Image, "Palette", Image).ADAPTIVE
            image = image.convert("RGB").quantize(colors=256, method=adaptive)
            image.save(
                output_path,
                format="PNG",
                optimize=True,
                compress_level=9,
                dpi=(LINE_IMAGE_EXPORT_DPI, LINE_IMAGE_EXPORT_DPI),
            )

    with Image.open(output_path) as verified:
        verified.verify()

    with Image.open(output_path) as verified:
        width, height = verified.size

    return width, height, os.path.getsize(output_path)


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
        render_path = f"{file_path}.rendering"
        optimized_path = f"{file_path}.optimized"

        render_dpi = _render_dpi_for_line(fig)

        fig.savefig(
            render_path,
            dpi=render_dpi,
            bbox_inches="tight",
            facecolor="white",
            format="png",
        )

        try:
            width, height, file_bytes = _optimize_png_for_line(
                render_path,
                optimized_path,
            )
        except Exception as optimize_exc:
            # Pillow 極端異常時仍採用已用動態 DPI 限縮的 Matplotlib PNG。
            print(
                "DEBUG publish figure optimize fallback",
                "| version =", UPLOAD_SERVICE_VERSION,
                "| error =", repr(optimize_exc),
                flush=True,
            )
            os.replace(render_path, optimized_path)
            width = 0
            height = 0
            file_bytes = os.path.getsize(optimized_path)

        # 只有完整寫入、驗證及壓縮完成後才公開正式檔名。
        os.replace(optimized_path, file_path)

        if os.path.exists(render_path):
            os.remove(render_path)

        print(
            "DEBUG publish figure",
            "| version =", UPLOAD_SERVICE_VERSION,
            "| name =", name,
            "| pixels =", f"{width}x{height}" if width and height else "dynamic_dpi",
            "| bytes =", file_bytes,
            "| dpi =", round(render_dpi, 1),
            "| url =", f"{PUBLIC_BASE_URL}{CHART_URL_PREFIX}/{filename}",
            flush=True,
        )

        return f"{PUBLIC_BASE_URL}{CHART_URL_PREFIX}/{filename}"

    except Exception as exc:
        print(
            "publish_figure failed:",
            repr(exc),
            "| version =", UPLOAD_SERVICE_VERSION,
            flush=True,
        )
        return IMAGE_URL

    finally:
        for pending_path in (
            locals().get("render_path"),
            locals().get("optimized_path"),
        ):
            try:
                if pending_path and os.path.exists(pending_path):
                    os.remove(pending_path)
            except Exception:
                pass
        plt.close(fig)
