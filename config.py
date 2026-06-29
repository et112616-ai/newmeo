import os

APP_ENV = os.getenv("APP_ENV", "production")
PORT = int(os.getenv("PORT", "5000"))

# Render 網址，請在 Render Environment Variables 設定：
# PUBLIC_BASE_URL=https://newmeo.onrender.com
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://newmeo.onrender.com").rstrip("/")

# 圖片實際儲存位置
CHART_DIR = os.getenv("CHART_DIR", "static/charts")

# 對外圖片網址前綴
CHART_URL_PREFIX = os.getenv("CHART_URL_PREFIX", "/static/charts")

# 如果產圖失敗，才使用這張測試圖
IMAGE_URL = os.getenv(
    "IMAGE_URL",
    "https://dummyimage.com/600x450/007aff/ffffff.png&text=Stock+Chart+Test"
)

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

DEFAULT_STOCK = os.getenv("DEFAULT_STOCK", "2330")
DEFAULT_TIME_FRAME = os.getenv("DEFAULT_TIME_FRAME", "D")
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "instant")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
TDCC_SYNC_TOKEN = os.getenv("TDCC_SYNC_TOKEN", "")
TDCC_SYNC_STOCKS = os.getenv("TDCC_SYNC_STOCKS", "2330,2344,2337")
