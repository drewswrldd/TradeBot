import os
from dotenv import load_dotenv

load_dotenv()

# ── NinjaTrader Bridge ────────────────────────────────────
NINJATRADER_BRIDGE_URL = os.getenv("NINJATRADER_BRIDGE_URL", "http://localhost:8080")

# ── Tradovate (legacy) ────────────────────────────────────
TRADOVATE_ENV         = os.getenv("TRADOVATE_ENV", "demo")
TRADOVATE_USERNAME    = os.getenv("TRADOVATE_USERNAME")
TRADOVATE_PASSWORD    = os.getenv("TRADOVATE_PASSWORD")
TRADOVATE_APP_ID      = os.getenv("TRADOVATE_APP_ID")
TRADOVATE_APP_VERSION = os.getenv("TRADOVATE_APP_VERSION", "1.0")
TRADOVATE_CID         = os.getenv("TRADOVATE_CID")
TRADOVATE_SEC         = os.getenv("TRADOVATE_SEC")

_BASE = {
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1",
}
_WS_TRADING = {
    "demo": "wss://demo.tradovateapi.com/v1/websocket",
    "live": "wss://live.tradovateapi.com/v1/websocket",
}
_WS_MD = {
    "demo": "wss://md.tradovateapi.com/v1/websocket",
    "live": "wss://md.tradovateapi.com/v1/websocket",
}

TRADOVATE_BASE_URL    = _BASE[TRADOVATE_ENV]
TRADOVATE_WS_TRADING  = _WS_TRADING[TRADOVATE_ENV]
TRADOVATE_WS_MD       = _WS_MD[TRADOVATE_ENV]

# ── Anthropic ──────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# ── Database ───────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")

# ── Webhook ────────────────────────────────────────────────
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# ── MFFU Rules ─────────────────────────────────────────────
ACCOUNT_SIZE    = float(os.getenv("ACCOUNT_SIZE",    50000))
MAX_DRAWDOWN    = float(os.getenv("MAX_DRAWDOWN",     2000))
PROFIT_TARGET   = float(os.getenv("PROFIT_TARGET",   3000))
MAX_CONTRACTS   = int(os.getenv("MAX_CONTRACTS",        2))   # reduced from 3
RISK_PERCENT    = float(os.getenv("RISK_PERCENT",      1.5)) # reduced from 2.0

# ── Internal Safety Limits (stricter than MFFU) ─────────────
DAILY_LOSS_LIMIT       = float(os.getenv("DAILY_LOSS_LIMIT",       500))
DAILY_PROFIT_TARGET    = float(os.getenv("DAILY_PROFIT_TARGET",    300))
WEEKLY_PROFIT_TARGET   = float(os.getenv("WEEKLY_PROFIT_TARGET",  1000))
INTERNAL_MAX_DRAWDOWN  = float(os.getenv("INTERNAL_MAX_DRAWDOWN", 1500))
TRAILING_DRAWDOWN_SHUTDOWN = os.getenv("TRAILING_DRAWDOWN_SHUTDOWN", "True").lower() == "true"

# ── Strategy & Instrument ──────────────────────────────────
STRATEGY   = int(os.getenv("STRATEGY", 1))
INSTRUMENT = os.getenv("INSTRUMENT", "MESH6")   # update each quarterly roll

# ── MES contract spec ──────────────────────────────────────
MES_TICK_SIZE  = 0.25   # minimum price movement
MES_TICK_VALUE = 1.25   # dollars per tick
MES_POINT_VALUE = 5.0   # dollars per full point
