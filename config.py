"""Configuration. Everything tunable is here."""
from __future__ import annotations

import os
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _i(k, d):
    try:
        return int(float(os.getenv(k, d)))
    except (TypeError, ValueError):
        return d


def _f(k, d):
    try:
        return float(os.getenv(k, d))
    except (TypeError, ValueError):
        return d


def _t(k, d):
    try:
        h, m = os.getenv(k, d).split(":")
        return dtime(int(h), int(m))
    except Exception:
        h, m = d.split(":")
        return dtime(int(h), int(m))


DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
for _d in (DATA_DIR, LOG_DIR):
    _d.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "signals.db"

# ------------------------------------------------------------------ exchange
EXCHANGE = "binanceusdm"
WATCHLIST_SIZE = _i("WATCHLIST_SIZE", 80)          # top N by 24h quote volume
MIN_24H_QUOTE_VOLUME = _f("MIN_24H_QUOTE_VOLUME", 10_000_000)

# Four timeframes, 400 candles each, all streamed over websocket.
# 1d is dropped: it changes too slowly to matter for a scalp/day-trade bot
# running on an hourly cadence.
TIMEFRAMES = ["5m", "15m", "1h", "4h"]
CANDLES = _i("CANDLES", 400)
MAIN_TFS = ["4h", "1h", "15m", "5m"]     # full blocks in the hourly bulk scan
ACTIVE_TFS = ["5m", "15m"]               # full blocks in the 30-minute loop
DIGEST_TFS = ["4h", "1h"]                # digested into the 30-minute loop
HTF, LTF = "1h", "5m"

# ------------------------------------------------------------------ schedule
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "Asia/Colombo"))
ACTIVE_START = _t("ACTIVE_START", "05:00")
ACTIVE_END = _t("ACTIVE_END", "21:00")
# The bulk scan fires on the hourly candle close, so it runs 15 times between
# 05:00 and 20:00 local. The active loop runs every 30 minutes so scalping
# entries do not slip past on the 15m candle.
MAIN_SECONDS = 3600          # bulk scan: every hour close
ACTIVE_SECONDS = 1800        # active-setup loop: every 30m close
HTF_SECONDS = MAIN_SECONDS   # kept for the startup banner
ALIGN_OFFSET = _i("ALIGN_OFFSET", 20)               # seconds after the close
WATCHLIST_REFRESH = _t("WATCHLIST_REFRESH", "05:00")  # daily, local time
MONITOR_SECONDS = _i("MONITOR_SECONDS", 60)
# Run one scan immediately on startup instead of waiting for the next hour
# close, so a restart mid-window is productive. Guarded by MIN_RESCAN_MINUTES.
SCAN_ON_START = _i("SCAN_ON_START", 1)
MIN_RESCAN_MINUTES = _i("MIN_RESCAN_MINUTES", 30)
HEARTBEAT_SECONDS = _i("HEARTBEAT_SECONDS", 30)

# ----------------------------------------------------------------- SMC engine
SWING = _i("SWING", 2)
SCAN_WINDOW = _i("SCAN_WINDOW", 250)
DISPLACEMENT = _f("DISPLACEMENT", 1.5)
OB_LOOKBACK = _i("OB_LOOKBACK", 10)
KEEP_FVG = _i("KEEP_FVG", 3)
KEEP_OB = _i("KEEP_OB", 3)
KEEP_BB = _i("KEEP_BB", 2)
KEEP_LIQ = _i("KEEP_LIQ", 2)
KEEP_SR = _i("KEEP_SR", 2)
KEEP_EVENTS = _i("KEEP_EVENTS", 2)

FRESH_ONLY = _i("FRESH_ONLY", 1)
MAX_FVG_FILL = _f("MAX_FVG_FILL", 50.0)
MAX_ZONE_AGE = _i("MAX_ZONE_AGE", 120)
ZONE_NEAR_PCT = {"5m": _f("NEAR_5M", 1.0), "15m": _f("NEAR_15M", 2.0),
                 "1h": _f("NEAR_1H", 3.5), "4h": _f("NEAR_4H", 7.0)}
SIG_DIGITS = _i("SIG_DIGITS", 5)

# ------------------------------------------------------------------- DeepSeek
API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low").strip()
TEMPERATURE = _f("TEMPERATURE", 0.2)
REQUEST_TIMEOUT = _i("REQUEST_TIMEOUT", 300)

COINS_PER_REQUEST = _i("COINS_PER_REQUEST", 20)     # 80 coins -> 4 requests
# The bulk scan gets one round: a second would resend the whole payload.
# The active loop is small, so it can afford a follow-up round.
ACTIVE_ROUNDS = _i("ACTIVE_ROUNDS", 2)
BUSY_ROUND_WARN = _i("BUSY_ROUND_WARN", 4)
MAX_FLAGS_PER_SCAN = _i("MAX_FLAGS_PER_SCAN", 4)
AGENT_CONCURRENCY = _i("AGENT_CONCURRENCY", 2)

# A setup is flagged into the 30-minute loop only if its POI is this close to CMP.
POI_MAX_DIST_PCT = _f("POI_MAX_DIST_PCT", 3.0)
MAX_ACTIVE = _i("MAX_ACTIVE", 3)          # concurrent coins in the 30m loop
SETUP_MAX_HOURS = _i("SETUP_MAX_HOURS", 8)   # expiry if it never fires
ACTIVE_BATCH = _i("ACTIVE_BATCH", 3)      # coins per 30m-loop request

# USD per 1M tokens, for the /cost report only
PRICE_IN_HIT = _f("PRICE_IN_HIT", 0.014)
PRICE_IN_MISS = _f("PRICE_IN_MISS", 0.28)
PRICE_OUT = _f("PRICE_OUT", 1.32)

# ------------------------------------------------------------------- telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_PROXY = os.getenv("TELEGRAM_PROXY", "").strip() or None
TG_POLLING = _i("TELEGRAM_POLLING", 1)
TG_TIMEOUT = _i("TELEGRAM_TIMEOUT", 30)
TG_RETRY_SECONDS = _i("TELEGRAM_RETRY_SECONDS", 60)
TG_QUEUE_MAX = _i("TG_QUEUE_MAX", 200)

# --------------------------------------------------------------------- signals
MIN_CONFIDENCE = _i("MIN_CONFIDENCE", 7)
MIN_RR = _f("MIN_RR", 1.5)
DUP_COOLDOWN_MIN = _i("DUP_COOLDOWN_MIN", 240)
PENDING_EXPIRY_MIN = _i("PENDING_EXPIRY_MIN", 480)
TP_SPLIT = [1 / 3, 1 / 3, 1 / 3]
MODE = "mtf"