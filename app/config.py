"""Settings, all read from environment variables (see .env.example)."""
import os

# Empty values (e.g. an unset field on Render) fall back to the defaults.
DATABASE_URL = os.environ.get("DATABASE_URL") or "sqlite:///./straywatch.db"
# Render/Neon give "postgres://"; SQLAlchemy wants "postgresql://".
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

# Render sets RENDER_EXTERNAL_URL automatically, so the public address works without extra setup.
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
                   or "http://localhost:8000").rstrip("/")

# BharatCode (Adaption) models. The SDK reads ADAPTION_API_KEY itself.
VISION_MODEL = os.environ.get("SW_VISION_MODEL", "qwen3.5-397b")      # triage, matching
FAST_MODEL = os.environ.get("SW_FAST_MODEL", "gemma-4-31b-it")         # check-ins, small jobs
TEXT_MODEL = os.environ.get("SW_TEXT_MODEL", "qwen3.5-397b")           # translation, agent
AI_TIMEOUT = float(os.environ.get("SW_AI_TIMEOUT", "60"))

# WhatsApp Cloud API (Meta)
WA_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WA_PHONE_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WA_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN") or "straywatch-verify"
WA_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")  # optional: checks webhook signatures
WA_DISPLAY_NUMBER = os.environ.get("WHATSAPP_DISPLAY_NUMBER", "")  # e.g. 919800000000, for wa.me links
WA_ALERT_TEMPLATE = os.environ.get("WHATSAPP_ALERT_TEMPLATE", "")  # approved template for alerts outside 24 h
WA_TEMPLATE_LANG = os.environ.get("WHATSAPP_TEMPLATE_LANG") or "en"
GRAPH = "https://graph.facebook.com/v21.0"

# Rescue rules
ESCALATE_AFTER_SECONDS = int(os.environ.get("SW_ESCALATE_AFTER", "180"))  # 3 minutes
HELPER_RADIUS_KM = float(os.environ.get("SW_HELPER_RADIUS_KM", "8"))
REID_RADIUS_KM = float(os.environ.get("SW_REID_RADIUS_KM", "1.0"))
LOST_SEARCH_RADIUS_KM = float(os.environ.get("SW_LOST_RADIUS_KM", "15"))
LOST_SEARCH_DAYS = int(os.environ.get("SW_LOST_DAYS", "14"))
MAX_ALERTS_PER_HELPER_PER_DAY = int(os.environ.get("SW_MAX_ALERTS", "6"))

# Public views round locations to about this many decimals (2 = ~1 km).
PUBLIC_COORD_DECIMALS = 2

# Demo mode seeds sample helpers so the browser demo works without real people.
DEMO_MODE = os.environ.get("SW_DEMO_MODE", "1") == "1"
