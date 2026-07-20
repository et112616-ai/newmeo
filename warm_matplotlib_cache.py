"""Render build-time Matplotlib font-cache warmup.

Run after installing requirements:
    python warm_matplotlib_cache.py
"""

from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".mplconfig"
os.environ["MPLCONFIGDIR"] = str(CACHE_DIR)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager


def main() -> None:
    font_dirs = [
        BASE_DIR / "assets" / "fonts",
        Path("/opt/render/project/src/assets/fonts"),
    ]

    registered = []
    for font_dir in font_dirs:
        for filename in (
            "NotoSansTC-Regular.ttf",
            "NotoSansTC-Bold.ttf",
            "NotoSansCJKtc-Regular.otf",
            "NotoSansCJKtc-Bold.otf",
        ):
            path = font_dir / filename
            if path.exists():
                font_manager.fontManager.addfont(str(path))
                registered.append(str(path))

    # 將含自訂繁中字型的 FontManager 明確寫入 Build artifact。
    cache_file = CACHE_DIR / (
        f"fontlist-v{font_manager.FontManager.__version__}.json"
    )
    font_manager.json_dump(font_manager.fontManager, cache_file)

    # 觸發一次實際查找，部署時不必再掃描系統字型。
    font_manager.findfont("DejaVu Sans", fallback_to_default=True)

    print(
        "Matplotlib cache ready",
        "| cache =",
        cache_file,
        "| custom_fonts =",
        len(set(registered)),
        flush=True,
    )


if __name__ == "__main__":
    main()
