import os

# Render 建議使用 3.11 或 3.12。請在 Render 設定 PYTHON_VERSION。
APP_ENV = os.getenv("APP_ENV", "production")
PORT = int(os.getenv("PORT", "5000"))

# 你已測試可用的圖片 URL，可在 Render Environment Variables 設定 IMAGE_URL。
# 第一版先使用固定圖床 URL，之後可替換成 Cloudinary / S3 / Render Static。
IMAGE_URL = os.getenv(
    "IMAGE_URL",
    "image_url = save_chart_and_get_url(fig, stock_id, mode, time_frame)",
)

# FinMind Token：第一版可不填，會使用 mock fallback。
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

# yfinance timeout / cache 可再擴充
DEFAULT_STOCK = os.getenv("DEFAULT_STOCK", "2330")
DEFAULT_TIME_FRAME = os.getenv("DEFAULT_TIME_FRAME", "D")
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "instant")
