"""
scanner.py — Autonomous Market Scanner
TNL Trader — Phase 3

Scans watchlist instruments every 15 minutes during trading hours.
Detects SMC setups: order blocks, FVGs, structure breaks.
Sends proactive alerts to subscribed users via Telegram.
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from market import normalize_symbol
from config import TWELVE_DATA_API_KEY
from scanner_improvements import (
    is_news_window, get_session_score_bonus, validate_entry,
    check_1h_candle_confirmation, check_consecutive_losses,
    get_loss_warning_message, run_pre_scan_checks,
    detect_liquidity_sweep, detect_rejection_candle, is_ranging_market,
    get_previous_day_levels, is_optimal_time_for_pair, validate_risk_reward,
    check_pair_correlation, detect_mtf_ob_confluence, detect_momentum,
    check_daily_bias_alignment,
    detect_equal_highs_lows, detect_market_structure_shift,
    check_premium_discount_zone, is_kill_zone,
    analyze_market_structure, get_trade_direction,
    detect_displacement, is_price_in_displacement_fvg,
    get_draw_on_liquidity,
    score_ob_quality,
    is_fvg_mitigated, mark_fvg_mitigated,
    is_ob_mitigated, mark_ob_mitigated,
    clear_daily_mitigation_state,
    detect_liquidity_run,
    detect_weekly_level_sweep,
    detect_round_number_sweep,
    score_bos_quality,
    get_weekly_levels,
    is_asia_range_tight,
    get_pip_spec,
)
import requests
import yfinance as yf

# GC=F and similar futures trade above spot — subtract to approximate MT5 spot price
FUTURES_SPOT_OFFSET = {
    "XAUUSD": -30,    # GC=F ~30 pts above XAUUSD spot
    "XAGUSD": -0.30,  # SI=F above spot silver
}

# yFinance ticker map for futures
YFINANCE_FUTURES_MAP = {
    'ES': 'ES=F', 'MES': 'MES=F', 'NQ': 'NQ=F', 'MNQ': 'MNQ=F',
    'RTY': 'RTY=F', 'YM': 'YM=F', 'CL': 'CL=F', 'MCL': 'MCL=F',
    'GC': 'GC=F', 'MGC': 'MGC=F', 'NG': 'NG=F',
    'US100': 'NQ=F', 'US30': 'YM=F',
    'US500': 'ES=F',
    'USOIL': 'CL=F',   # WTI Crude Oil — maps to CME CL futures for data
}

logger = logging.getLogger(__name__)

# Cache for auto-built signals — keyed by short ID
import uuid as _uuid
AUTO_SIGNAL_CACHE = {}
MAX_CACHE_SIZE = 200

# Asia session high/low tracking per symbol (Power of 3 sweep detection)
_asia_levels: dict = {}  # {symbol: {"high": float, "low": float, "date": str}}
# Midnight open (00:00 UTC) per symbol — Power of Three PO3 reference price.
# Bullish PO3: price drops below midnight open (manipulation) then rallies above it (distribution).
# Bearish PO3: price spikes above midnight open (manipulation) then collapses below it.
_midnight_open: dict = {}  # {symbol: {"price": float, "date": str}}
_last_mitigation_clear_date: str = ""  # track when we last cleared FVG/OB mitigation dicts

# Equity indices that close overnight and have no meaningful Asian session data.
# For these symbols _update_asia_levels() uses the previous day's D1 high/low
# instead of today's 00:00-07:00 UTC candles, which are empty (GER40) or very
# thin overnight-futures prints (US100, US30, US500).
_EQUITY_INDEX_SYMBOLS = {'US100', 'US30', 'US500', 'USOIL'}

# Twelve Data circuit breaker — once daily credits are exhausted, skip TD calls
# until the next UTC midnight rather than hitting the API on every scan cycle.
import time as _time
import time
import re
import concurrent.futures
_td_credits_exhausted_until: float = 0.0  # epoch seconds
_direction_lock: dict = {}  # {symbol: {"direction": str, "locked_until": float}}
_last_signal_time: dict = {}   # {symbol_upper: monotonic_float} — signal dedup cooldown
_last_swept_level: dict = {}   # {symbol_upper: float} — last swept level that fired a signal
_last_signal_entry: dict = {}  # {symbol: float}


def _td_available() -> bool:
    """Return False once TD daily limit has been hit (until next UTC midnight)."""
    return _time.time() >= _td_credits_exhausted_until


def _mark_td_exhausted() -> None:
    """Disable TD calls for the rest of the current UTC day."""
    global _td_credits_exhausted_until
    from datetime import timedelta
    _next_midnight = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    _td_credits_exhausted_until = _next_midnight.timestamp()
    logger.warning(
        f"[candles] Twelve Data credits exhausted — yFinance-only until "
        f"{_next_midnight.strftime('%Y-%m-%d %H:%M UTC')}"
    )

# Rotation index for spreading Twelve Data API calls across scan cycles
_scan_rotation_index = 0


MIN_SL_DISTANCE = {
    # Gold: ICT setups require 80-300pt SL anchored below swept level.
    # Previous 12pt min was too tight — a single tick wipes it at $4200 gold.
    "XAUUSD":        30.0,   # 30 points minimum — allows natural swept-level SL anchoring
    "US100":         80.0,   # NQ equivalent — 80 pts minimum
    "US30":          60.0,   # YM equivalent — 60 pts minimum
    # US500 min raised from 2.0 to 8.0 — 2pt SL on SP500 is sub-tick level
    "US500":          8.0,   # S&P500 CFD — 8 pts minimum (realistic 15M range)
    "NAS100":        20.0,
    # WTI Crude: 15M range = $0.40-$0.80. Min 40 cents (40 pips at $0.01/pip)
    "USOIL":          0.40,  # 40 cents minimum SL
    "GBPUSD":        0.0015, # 15 pips — most volatile forex
    "EURUSD":        0.0012, # 12 pips
    "USDJPY":        0.08,   # 8 pips JPY
    "USDCAD":        0.0012, # 12 pips
    "USDCHF":        0.0010, # 10 pips
    "AUDUSD":        0.0010, # 10 pips
    "NZDUSD":        0.0010, # 10 pips
    "default_forex": 0.0010, # 10 pips default
    "jpy_forex":     0.10,   # 10 pips JPY default
    "futures":       8.0,
}

MAX_SL_DISTANCE = {
    # Gold: max 300 points — allows swept-level SL anchoring on big Judas moves
    "XAUUSD":        300.0,  # max 300 points — ICT gold setups can need this room
    "US100":         300.0,  # max 300 pts
    "US30":          250.0,  # max 250 pts
    "US500":          30.0,  # max 30 pts — SP500 15M range is typically 5-20pts
    # WTI Crude: max $2.00 SL — wide enough for NY open volatility
    "USOIL":          2.0,   # max $2.00 SL
    "GBPUSD":        0.0025, # max 25 pips — raised from 20 for swept-level room
    "EURUSD":        0.0020, # max 20 pips
    "USDJPY":        0.20,   # max 20 pips JPY
    "USDCAD":        0.0020, # max 20 pips
    "AUDUSD":        0.0018, # max 18 pips
    "NZDUSD":        0.0018, # max 18 pips
    "USDCHF":        0.0018, # max 18 pips
    "default_forex": 0.0020,
    "jpy_forex":     0.20,
    "futures":       20.0,
}


def _min_sl_dist(symbol: str) -> float:
    """Return the minimum acceptable SL distance for a symbol (in price units)."""
    sym = symbol.upper()
    if sym in MIN_SL_DISTANCE:
        return MIN_SL_DISTANCE[sym]
    if sym in YFINANCE_FUTURES_MAP:
        return MIN_SL_DISTANCE["futures"]
    if "JPY" in sym:
        return MIN_SL_DISTANCE["jpy_forex"]
    return MIN_SL_DISTANCE["default_forex"]


def _max_sl_dist(symbol: str) -> float:
    """Return the maximum acceptable SL distance for a symbol (in price units)."""
    sym = symbol.upper()
    if sym in MAX_SL_DISTANCE:
        return MAX_SL_DISTANCE[sym]
    if sym in YFINANCE_FUTURES_MAP:
        return MAX_SL_DISTANCE["futures"]
    if "JPY" in sym:
        return MAX_SL_DISTANCE["jpy_forex"]
    return MAX_SL_DISTANCE["default_forex"]


def _swept_sl_buffer(symbol: str) -> float:
    """Buffer below swept low (BUY) or above swept high (SELL), in price units."""
    sym = symbol.upper()
    if sym == "XAUUSD":
        return 12.0
    if sym in ("US100", "US30", "US500", "NAS100") or sym in YFINANCE_FUTURES_MAP:
        return 80.0
    if "JPY" in sym:
        return 0.05   # 5 pips JPY
    return 0.0005     # 5 pips standard forex


# News-resumption alert state — fires once when a news block clears
_news_was_blocked: bool = False
_news_resume_sent: bool = False


def _cache_signal(signal_text: str, score: int = None) -> str:
    """Store a signal and return its short cache key."""
    key = _uuid.uuid4().hex[:12]
    AUTO_SIGNAL_CACHE[key] = {"signal": signal_text, "score": score, "timestamp": datetime.now(timezone.utc)}
    # Keep cache bounded
    if len(AUTO_SIGNAL_CACHE) > MAX_CACHE_SIZE:
        oldest = next(iter(AUTO_SIGNAL_CACHE))
        del AUTO_SIGNAL_CACHE[oldest]
    return key


def get_cached_signal(key: str) -> str | None:
    """Retrieve a cached signal by key. Returns None if missing or older than 5 minutes."""
    entry = AUTO_SIGNAL_CACHE.get(key)
    if entry is None:
        return None
    age = datetime.now(timezone.utc) - entry["timestamp"]
    if age.total_seconds() > 120:
        del AUTO_SIGNAL_CACHE[key]
        return None
    return entry["signal"]


def get_cached_score(key: str) -> int | None:
    """Retrieve the scanner score stored alongside a cached signal."""
    entry = AUTO_SIGNAL_CACHE.get(key)
    if entry is None:
        return None
    return entry.get("score")


async def _update_asia_levels(symbol: str, candles: list = None) -> None:
    """
    Set reference levels used for liquidity-sweep detection.

    Forex / XAUUSD  — Asia session (00:00-07:00 UTC) high/low from today's 15M candles.
    Equity indices (GER40, US100, US30, US500) — previous day's D1 high/low.
      GER40 is closed during Asian hours; US indices have only thin overnight-futures
      prints, making the prev-day session high/low a far more meaningful sweep reference.
    """
    global _last_mitigation_clear_date
    sym = symbol.upper()
    today = datetime.now(timezone.utc).date()

    # Clear FVG/OB mitigation state once per day on new session
    today_str = str(today)
    if today_str != _last_mitigation_clear_date:
        _last_mitigation_clear_date = today_str
        clear_daily_mitigation_state()

    # ── EQUITY INDICES: prev-day D1 high/low ─────────────────────────────────
    if sym in _EQUITY_INDEX_SYMBOLS:
        existing = _asia_levels.get(sym, {})
        # Within one trading day, skip the refetch once levels are populated.
        if (existing.get("source") == "prev_day"
                and existing.get("date") == str(today)
                and existing.get("high", 0) > 0):
            return
        try:
            bundle = await fetch_all_timeframes(sym)
            d1 = bundle.get("candles_daily", [])  # newest-first; [0]=today partial, [1]=yesterday
            if len(d1) >= 2:
                prev = d1[1]
                ph = prev.get("high", 0)
                pl = prev.get("low",  0)
                if ph > 0 and pl > 0:
                    _asia_levels[sym] = {
                        "high":   ph,
                        "low":    pl,
                        "date":   str(today),
                        "source": "prev_day",
                    }
                    logger.info(f"[asia] {sym} prev_day levels — high={ph} low={pl}")
                    return
                logger.warning(f"[asia] {sym} prev_day D1 returned zeros — falling back to 15M")
            else:
                logger.warning(f"[asia] {sym} insufficient D1 data ({len(d1)} candles) — falling back to 15M")
        except Exception as e:
            logger.warning(f"[asia] {sym} prev_day fetch failed: {e} — falling back to 15M")

    # ── FOREX / XAUUSD: Asia session 00:00-07:00 UTC ─────────────────────────
    if candles is None:
        try:
            bundle = await fetch_all_timeframes(sym)
            candles = bundle.get("candles_15m", [])
        except Exception as e:
            logger.warning(f"[asia] {sym} — could not fetch candles: {e}")
            return
    asia_candles = []
    _midnight_open_found = False
    for c in candles:
        try:
            dt = datetime.fromisoformat(str(c.get("datetime", "")))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            if dt.date() == today and 0 <= dt.hour < 7:
                asia_candles.append(c)
                # Midnight open = open price of the 00:00 UTC candle (first 15M candle of the day)
                if dt.hour == 0 and dt.minute == 0 and not _midnight_open_found:
                    _mo_existing = _midnight_open.get(sym, {})
                    if _mo_existing.get("date") != str(today):
                        _midnight_open[sym] = {"price": float(c["open"]), "date": str(today)}
                        logger.info(f"[po3] {sym} midnight open set: {c['open']}")
                    _midnight_open_found = True
        except Exception:
            continue
    if asia_candles:
        asia_high = max(c["high"] for c in asia_candles)
        asia_low  = min(c["low"]  for c in asia_candles)
        if asia_high > 0 and asia_low > 0:
            _asia_levels[sym] = {
                "high":   asia_high,
                "low":    asia_low,
                "date":   str(today),
                "source": "asia_session",
            }
            logger.info(f"[asia] {sym} levels set — high={asia_high} low={asia_low}")
        else:
            logger.warning(f"[asia] {sym} levels returned 0 — keeping existing")


def _detect_asia_sweep_or_recent(symbol: str, candles: list, direction: str) -> tuple:
    """
    Power of 3: check for Asia range sweep, fall back to recent swing sweep.
    SELL: wick above Asia high then close below it.
    BUY:  wick below Asia low  then close above it.
    Returns (swept, swept_level, sweep_type) where sweep_type is
    'judas_swing' (wick through + close back = high-prob reversal) or
    'liquidity_run' (closes through without reversing = trend continuation, lower prob).
    """
    sym = symbol.upper()
    today = datetime.now(timezone.utc).date()
    asia = _asia_levels.get(sym, {})
    if (asia.get("date") == str(today)
            and asia.get("high", 0) > 0
            and asia.get("low", 0) > 0):
        asia_high = asia["high"]
        asia_low  = asia["low"]
        for c in candles[:15]:
            if direction == "SELL" and c["high"] > asia_high and c["close"] < asia_high:
                return True, round(asia_high + FUTURES_SPOT_OFFSET.get(sym, 0), 5), "judas_swing"
            if direction == "BUY"  and c["low"]  < asia_low  and c["close"] > asia_low:
                return True, round(asia_low  + FUTURES_SPOT_OFFSET.get(sym, 0), 5), "judas_swing"
    else:
        if asia.get("high", 0) == 0 or asia.get("low", 0) == 0:
            logger.warning(f"[asia] {sym} levels not set — sweep detection skipped, falling back to liquidity sweep")

    # Weekly level sweep — HTF, wick through + close back = Judas Swing
    weekly_swept, weekly_level, weekly_label = detect_weekly_level_sweep(candles, direction, symbol)
    if weekly_swept:
        logger.info(f"[sweep] {sym} {weekly_label}")
        return True, weekly_level, "judas_swing"

    # Round number sweep — wick through + close back = Judas Swing
    round_swept, round_level, round_label = detect_round_number_sweep(candles, direction, symbol)
    if round_swept:
        logger.info(f"[sweep] {sym} {round_label}")
        return True, round_level, "judas_swing"

    # Liquidity run — closes THROUGH the level (no reversal close); lower probability
    run_detected, run_level = detect_liquidity_run(candles, direction, symbol)
    if run_detected:
        logger.info(f"[sweep] {sym} liquidity run through {run_level:.5f}")
        return True, run_level, "liquidity_run"

    # Final fallback — standard recent-swing sweep (wick through + close back = Judas Swing)
    swept, level = detect_liquidity_sweep(candles, direction, symbol)
    return swept, level, "judas_swing" if swept else "none"


BASE_URL = "https://api.twelvedata.com"

SYMBOLS = [
    'XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY',
    'AUDUSD', 'USDCAD', 'NZDUSD', 'USDCHF',
    'US100', 'US30', 'US500',
]

# Default watchlist — users can customize with /watch command
DEFAULT_WATCHLIST = ["EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD", "XAUUSD", "USOIL", "US100", "US30", "US500"]



# Trading hours UTC — scanner only runs during active sessions
SCANNER_START_HOUR = 7   # 7 AM UTC = London open
SCANNER_END_HOUR = 21    # 9 PM UTC = NY close


def is_scan_window() -> bool:
    now = datetime.now(timezone.utc)
    day = now.weekday()
    hour = now.hour

    # Saturday — market closed all day
    if day == 5:
        return False

    # Sunday — only open after 21:00 UTC (Sydney open)
    if day == 6:
        return hour >= 21

    # Monday-Friday — block only dead hours 03:00-06:00 UTC
    if 3 <= hour < 6:
        return False

    return True


def get_session_interval() -> int:
    """
    Return scan interval in seconds based on current session.
    03:00-06:00 UTC — dead hours = 15 minutes (no liquid pairs active)
    06:00-08:00 UTC — London open = 15 seconds (Judas Swing window, highest priority)
    All other hours  — 30 seconds (London, NY, Asian open, late Asian)

    30s sleep + ~29s scan cycle (12 pairs) = ~59s end-to-end. Acceptable.
    120s was too slow — signals fired 2+ minutes late causing missed fills.
    """
    hour = datetime.now(timezone.utc).hour
    if 3 <= hour < 6:
        return 900   # dead hours — no liquid pairs, no point scanning
    if 6 <= hour < 8:
        return 15    # London open — Judas Swing forms here, catch it as it happens
    if 12 <= hour < 16:
        return 10    # NY open — fast-moving session, 30s cycle misses OB entries
    return 30        # 30s floor for all other active windows


def get_current_session() -> str:
    """Return the name of the current trading session."""
    hour = datetime.now(timezone.utc).hour
    if 7 <= hour < 10:
        return "London Open"
    elif 10 <= hour < 13:
        return "London"
    elif 13 <= hour < 16:
        return "NY Open"
    elif 16 <= hour < 21:
        return "NY"
    elif 0 <= hour < 3:
        return "Asian"
    return "Off-Session"


def get_candles_yfinance(symbol: str, outputsize: int = 200) -> list | None:
    """Fetch futures candles from yFinance (free, no rate limit)."""
    try:
        ticker = YFINANCE_FUTURES_MAP.get(symbol.upper())
        if not ticker:
            return None
        hist = yf.Ticker(ticker).history(period="10d", interval="15m")
        if hist.empty:
            return None
        # Convert to our candle format, newest first
        result = []
        for ts, row in hist.iloc[::-1].iterrows():
            result.append({
                "datetime": str(ts),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row.get("Volume", 0)),
            })
        return result[:outputsize]
    except Exception as e:
        logger.error(f"yFinance candle error for {symbol}: {e}")
        return None


def _get_candles_yfinance_forex(symbol: str, outputsize: int = 200) -> list | None:
    """Fetch forex 15M candles from yFinance as Twelve Data fallback."""
    from market import YFINANCE_FOREX_MAP as _YF_FOREX_MAP
    ticker = _YF_FOREX_MAP.get(symbol.upper())
    if not ticker:
        return None
    try:
        hist = yf.Ticker(ticker).history(period="10d", interval="15m")
        if hist.empty:
            return None
        result = []
        for ts, row in hist.iloc[::-1].iterrows():
            result.append({
                "datetime": str(ts),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row.get("Volume", 0)),
            })
        return result[:outputsize]
    except Exception as e:
        logger.error(f"yFinance forex candle error for {symbol}: {e}")
        return None


def get_candles(symbol: str, interval: str = "15min", outputsize: int = 200) -> list | None:
    """Fetch candles — uses yFinance for futures and XAUUSD, Twelve Data for forex."""
    if symbol.upper() in YFINANCE_FUTURES_MAP:
        return get_candles_yfinance(symbol, outputsize)
    if symbol.upper() == "XAUUSD":
        # Use yFinance GC=F for gold — free, no Twelve Data credits consumed
        try:
            hist = yf.Ticker("GC=F").history(period="10d", interval="15m")
            if hist.empty:
                return None
            result = []
            for ts, row in hist.iloc[::-1].iterrows():
                result.append({
                    "datetime": str(ts),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row.get("Volume", 0)),
                })
            return result[:outputsize]
        except Exception as e:
            logger.error(f"yFinance candle error for XAUUSD: {e}")
            return None
    from market import YFINANCE_FOREX_MAP as _YF_FOREX_MAP

    # Forex — TD primary, yFinance fallback (skipped when daily credits exhausted)
    if TWELVE_DATA_API_KEY and _td_available():
        td_symbol = normalize_symbol(symbol)
        try:
            resp = requests.get(f"{BASE_URL}/time_series",
                params={"symbol": td_symbol, "interval": interval,
                        "outputsize": outputsize, "apikey": TWELVE_DATA_API_KEY}, timeout=8)
            data = resp.json()
            if data.get("status") != "error" and "values" in data:
                candles = []
                for v in data["values"]:
                    candles.append({
                        "datetime": v["datetime"],
                        "open": float(v["open"]),
                        "high": float(v["high"]),
                        "low": float(v["low"]),
                        "close": float(v["close"]),
                        "volume": float(v.get("volume", 0)),
                    })
                return candles
            _msg = data.get("message", "unknown")
            if "credits" in _msg.lower():
                _mark_td_exhausted()
            else:
                logger.warning(f"[candles] TD failed for {symbol}: {_msg} — falling back to yFinance")
        except Exception as e:
            logger.warning(f"[candles] TD exception for {symbol}: {e} — falling back to yFinance")

    if symbol.upper() in _YF_FOREX_MAP:
        return _get_candles_yfinance_forex(symbol, outputsize)
    return None


def detect_structure(candles: list) -> dict:
    """
    Detect market structure: Higher Highs, Lower Lows, BOS, CHOCH.
    Uses last 20 candles.
    """
    if not candles or len(candles) < 10:
        return {"trend": "unclear", "bos": False, "choch": False}

    recent = candles[:20]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]

    # Simple HH/HL or LH/LL detection
    hh = highs[0] > highs[2] > highs[4]  # recent highs rising
    hl = lows[0] > lows[2] > lows[4]     # recent lows rising
    lh = highs[0] < highs[2] < highs[4]  # recent highs falling
    ll = lows[0] < lows[2] < lows[4]     # recent lows falling

    if hh and hl:
        trend = "bullish"
    elif lh and ll:
        trend = "bearish"
    else:
        trend = "ranging"

    # Break of structure — current close beyond recent swing
    current_close = candles[0]["close"]
    prev_high = max(highs[1:6])
    prev_low = min(lows[1:6])

    bos_bull = current_close > prev_high and trend == "bullish"
    bos_bear = current_close < prev_low and trend == "bearish"
    bos = bos_bull or bos_bear

    # Change of character — close beyond structure in opposite direction
    choch = (current_close > prev_high and trend == "bearish") or \
            (current_close < prev_low and trend == "bullish")

    # Classify BOS subtype:
    # 'choch' = Change of Character — close breaks structure AGAINST the prevailing trend.
    #           This is a reversal confirmation — highest-probability ICT entry signal.
    # 'bos'   = Break of Structure IN trend direction — continuation entry, lower urgency.
    if choch:
        bos_type = "choch"      # reversal: sweep + CHoCH = Judas Swing confirmation
    elif bos:
        bos_type = "bos"        # continuation: structure break in trend direction
    else:
        bos_type = None

    return {
        "trend": trend,
        "bos": bos,
        "choch": choch,
        "bos_type": bos_type,   # 'choch', 'bos', or None
        "prev_high": round(prev_high, 5),
        "prev_low": round(prev_low, 5),
        "current": round(current_close, 5),
    }


def detect_order_block(candles: list, trend: str, max_candles_back: int = None, symbol: str = "") -> dict | None:
    """
    Detect the most recent order block within max_candles_back candles.
    Bullish OB: last bearish candle before a strong bullish move.
    Bearish OB: last bullish candle before a strong bearish move.
    Also scores OB quality by measuring displacement from OB mid to the breakout candle.
    """
    if not candles or len(candles) < 5:
        return None

    if max_candles_back is None:
        # Dynamic lookback: 15M candles since today's Forex session open (21:00 UTC)
        _now = datetime.now(timezone.utc)
        _session_open = _now.replace(hour=21, minute=0, second=0, microsecond=0)
        if _now.hour < 21:
            _session_open -= timedelta(days=1)
        _candles_since_open = int((_now - _session_open).total_seconds() / 60 / 15)
        max_candles_back = min(max(_candles_since_open, 5), 96)

    # Per-pair displacement thresholds (raw price units — points for gold, price for forex)
    _OB_THRESHOLDS = {
        "XAUUSD": {"strong": 25.0,  "valid": 12.0},
        "GBPUSD": {"strong": 0.0020, "valid": 0.0010},
        "EURUSD": {"strong": 0.0015, "valid": 0.0008},
        "USDJPY": {"strong": 0.15,   "valid": 0.08},
        "USDCAD": {"strong": 0.0012, "valid": 0.0006},
        "AUDUSD": {"strong": 0.0012, "valid": 0.0006},
        "NZDUSD": {"strong": 0.0010, "valid": 0.0005},
        "USDCHF": {"strong": 0.0010, "valid": 0.0005},
    }
    _sym_upper = symbol.upper() if symbol else ""
    # Futures (non-XAUUSD) fall back to XAUUSD-style point thresholds
    if _sym_upper in YFINANCE_FUTURES_MAP and _sym_upper != "XAUUSD":
        _thresholds = {"strong": 15.0, "valid": 8.0}
    else:
        _thresholds = _OB_THRESHOLDS.get(_sym_upper, {"strong": 0.0015, "valid": 0.0008})
    _strong_disp = _thresholds["strong"]
    _valid_disp  = _thresholds["valid"]

    def _ob_quality(ob_mid: float, breakout_close: float) -> tuple:
        disp = abs(breakout_close - ob_mid)
        if disp >= _strong_disp:
            return disp, "strong"
        if disp >= _valid_disp:
            return disp, "valid"
        return disp, "weak"

    def _build_ob(ob_type: str, c: dict, breakout_close: float) -> dict:
        _mid = round((c["high"] + c["low"]) / 2, 5)
        _disp, _quality = _ob_quality(_mid, breakout_close)
        _ob_score, _ob_tier = score_ob_quality(c, symbol)
        return {
            "type": ob_type,
            "high": round(c["high"], 5),
            "low": round(c["low"], 5),
            "mid": _mid,
            "datetime": c["datetime"],
            "strength": "strong" if (c["high"] - c["low"]) > (candles[0]["high"] - candles[0]["low"]) else "normal",
            "displacement": round(_disp, 5),
            "ob_quality": _quality,
            "tier": _ob_tier,
        }

    # Proximity thresholds — OB must be within this distance of current price
    _is_pts = _sym_upper == "XAUUSD" or _sym_upper in YFINANCE_FUTURES_MAP
    _far_threshold   = 30.0  if _is_pts else 0.0030   # first OB beyond this → search for closer
    _fresh_threshold = 15.0  if _is_pts else 0.0015   # fallback OB must be within this

    current_price = candles[0]["close"]

    try:
        if trend == "bullish":
            _first_ob = None
            for i in range(1, min(max_candles_back, len(candles))):
                c = candles[i]
                if c["close"] < c["open"] and candles[i-1]["close"] > c["high"]:
                    ob = _build_ob("bullish_ob", c, candles[i-1]["close"])
                    dist = abs(current_price - ob["mid"])
                    if _first_ob is None:
                        # First (most recent) OB found
                        if dist <= _far_threshold:
                            return ob  # close enough — use it
                        # Direction-aware OB distance check:
                        # BUY setup: bullish OB should be BELOW price (demand zone entry).
                        # If it's on the correct side, keep it even if far — distance
                        # just means price hasn't retraced to the zone yet.
                        # Only reject if OB is ABOVE price (wrong side for BUY).
                        _ob_wrong_side = ob["mid"] > current_price
                        if _ob_wrong_side:
                            logger.info(
                                f"[scanner] {symbol} OB at {ob['mid']} wrong side + too far "
                                f"({dist:.5f}) — skipping"
                            )
                        else:
                            logger.info(
                                f"[scanner] {symbol} OB at {ob['mid']} far ({dist:.5f}) "
                                f"but correct side for BUY — keeping"
                            )
                            return ob
                        _first_ob = ob
                    else:
                        # Scanning for a closer OB
                        if dist <= _fresh_threshold:
                            return ob
            return None  # first OB too far, no fresh OB found

        elif trend == "bearish":
            _first_ob = None
            for i in range(1, min(max_candles_back, len(candles))):
                c = candles[i]
                if c["close"] > c["open"] and candles[i-1]["close"] < c["low"]:
                    ob = _build_ob("bearish_ob", c, candles[i-1]["close"])
                    dist = abs(current_price - ob["mid"])
                    if _first_ob is None:
                        if dist <= _far_threshold:
                            return ob
                        # Direction-aware OB distance check:
                        # SELL setup: bearish OB should be ABOVE price (supply zone entry).
                        # If it's on the correct side, keep it even if far — distance
                        # just means price hasn't rallied to the zone yet.
                        # Only reject if OB is BELOW price (wrong side for SELL).
                        _ob_wrong_side = ob["mid"] < current_price
                        if _ob_wrong_side:
                            logger.info(
                                f"[scanner] {symbol} OB at {ob['mid']} wrong side + too far "
                                f"({dist:.5f}) — skipping"
                            )
                        else:
                            logger.info(
                                f"[scanner] {symbol} OB at {ob['mid']} far ({dist:.5f}) "
                                f"but correct side for SELL — keeping"
                            )
                            return ob
                        _first_ob = ob
                    else:
                        if dist <= _fresh_threshold:
                            return ob
            return None

    except Exception as e:
        logger.error(f"OB detection error: {e}")

    return None


def refine_entry_5m(symbol: str, candles_5m: list, direction: str, ob_15m: dict) -> dict | None:
    """
    Find the most recent 5M OB within the 15M OB zone for a tighter entry.
    Candles are newest-first. Returns {entry, sl, timeframe} or None.

    SELL: last bullish 5M candle before bearish displacement, within 15M OB zone.
    BUY:  last bearish 5M candle before bullish displacement, within 15M OB zone.
    """
    if not candles_5m or len(candles_5m) < 3 or not ob_15m:
        return None

    ob_zone_high = ob_15m['high']
    ob_zone_low  = ob_15m['low']

    # i=1..n-1: candles_5m[i] is the OB candle (older), candles_5m[i-1] is the displacement (newer)
    for i in range(1, len(candles_5m)):
        candle      = candles_5m[i]
        next_candle = candles_5m[i - 1]

        if direction == 'SELL':
            if (candle['close'] > candle['open'] and
                    next_candle['close'] < next_candle['open'] and
                    ob_zone_low <= candle['high'] <= ob_zone_high):
                return {
                    'entry':    candle['high'],
                    'sl':       candle['high'] + _min_sl_dist(symbol),
                    'timeframe': '5M',
                }

        if direction == 'BUY':
            if (candle['close'] < candle['open'] and
                    next_candle['close'] > next_candle['open'] and
                    ob_zone_low <= candle['low'] <= ob_zone_high):
                return {
                    'entry':    candle['low'],
                    'sl':       candle['low'] - _min_sl_dist(symbol),
                    'timeframe': '5M',
                }

    return None


def detect_fvg(candles: list, symbol: str = "") -> dict | None:
    """
    Detect Fair Value Gap (imbalance between candle 1 high and candle 3 low).
    Bullish FVG: candle[2].high < candle[0].low
    Bearish FVG: candle[2].low > candle[0].high
    """
    if not candles or len(candles) < 3:
        return None

    try:
        _sym_u = symbol.upper() if symbol else ""
        _disp_off = FUTURES_SPOT_OFFSET.get(_sym_u, 0)
        if _sym_u == "XAUUSD" or _sym_u in YFINANCE_FUTURES_MAP:
            _dp = 2
        elif "JPY" in _sym_u:
            _dp = 3
        else:
            _dp = 5
        for i in range(len(candles) - 2):
            c1 = candles[i+2]   # oldest of three
            c2 = candles[i+1]   # middle
            c3 = candles[i]     # newest

            # Bullish FVG
            if c1["high"] < c3["low"]:
                gap_size = c3["low"] - c1["high"]
                _top = round(c3["low"], 5)
                _bot = round(c1["high"], 5)
                return {
                    "type": "bullish_fvg",
                    "top": _top,
                    "bottom": _bot,
                    "display_top": round(_top + _disp_off, _dp),
                    "display_bottom": round(_bot + _disp_off, _dp),
                    "mid": round((_top + _bot) / 2, 5),
                    "size": round(gap_size, 5),
                    "datetime": c2["datetime"],
                }

            # Bearish FVG
            if c1["low"] > c3["high"]:
                gap_size = c1["low"] - c3["high"]
                _top = round(c1["low"], 5)
                _bot = round(c3["high"], 5)
                return {
                    "type": "bearish_fvg",
                    "top": _top,
                    "bottom": _bot,
                    "display_top": round(_top + _disp_off, _dp),
                    "display_bottom": round(_bot + _disp_off, _dp),
                    "mid": round((_top + _bot) / 2, 5),
                    "size": round(gap_size, 5),
                    "datetime": c2["datetime"],
                }
    except Exception as e:
        logger.error(f"FVG detection error: {e}")

    return None


async def fetch_all_timeframes(symbol: str) -> dict:
    """
    Fetch all timeframes via yFinance and return a unified bundle.
    Always fetches fresh data — no caching.

    Returns dict with keys: symbol, price, candles_15m, candles_1h, candles_4h,
    candles_daily (all newest-first), atr (dict), timestamp.
    """
    sym = symbol.upper()

    from market import YFINANCE_FOREX_MAP as _YF_FOREX_MAP
    from scanner_improvements import get_pip_spec as _get_pip_spec

    if sym in YFINANCE_FUTURES_MAP:
        yf_ticker = YFINANCE_FUTURES_MAP[sym]
    elif sym == "XAUUSD":
        yf_ticker = "GC=F"
    else:
        yf_ticker = _YF_FOREX_MAP.get(sym)

    def _to_candles(hist):
        if hist is None or hist.empty:
            return []
        result = []
        for ts, row in hist.iloc[::-1].iterrows():
            result.append({
                "datetime": str(ts),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row.get("Volume", 0)),
            })
        return result

    candles_5m: list  = []
    candles_15m: list = []
    candles_1h: list  = []
    candles_4h: list  = []
    candles_daily: list = []
    price: float = 0.0
    atr_data: dict = {}

    try:
        if yf_ticker:
            def _fetch_yf_all(sym):
                # Each ticker gets its own object — no shared state, thread-safe
                _t = yf.Ticker(sym)
                result = {
                    "h5m": _t.history(period="5d",   interval="5m"),
                    "h15": _t.history(period="10d",  interval="15m"),
                    "h1":  _t.history(period="14d",  interval="1h"),
                    "h4":  _t.history(period="30d",  interval="4h"),
                    "hd":  _t.history(period="30d",  interval="1d"),
                }
                if sym == "GC=F":
                    try:
                        result["h1m"] = _t.history(period="1d", interval="1m")
                    except Exception:
                        result["h1m"] = None
                return result

            _loop = asyncio.get_event_loop()
            _yf_data = await _loop.run_in_executor(None, _fetch_yf_all, yf_ticker)
            h15 = _yf_data["h15"]
            h1  = _yf_data["h1"]
            h4  = _yf_data["h4"]
            hd  = _yf_data["hd"]

            candles_5m    = _to_candles(_yf_data["h5m"])[:50]
            candles_15m   = _to_candles(h15)[:200]
            candles_1h    = _to_candles(h1)[:100]
            candles_4h    = _to_candles(h4)[:60]
            candles_daily = _to_candles(hd)[:20]

            price = candles_15m[0]["close"] if candles_15m else 0.0

            if sym == "XAUUSD":
                try:
                    _1m = _yf_data.get("h1m")
                    if _1m is not None and not _1m.empty:
                        price = float(_1m["Close"].iloc[-1])
                except Exception:
                    pass
                price = round(price + FUTURES_SPOT_OFFSET.get(sym, 0), 3)

            if len(candles_15m) >= 14:
                ranges = [c["high"] - c["low"] for c in candles_15m[:14]]
                atr_val = sum(ranges) / len(ranges)
                _futures_thr = {
                    "ES": 5.0, "MES": 5.0, "NQ": 20.0, "MNQ": 20.0,
                    "CL": 0.3, "MCL": 0.3, "GC": 5.0, "MGC": 5.0,
                    "RTY": 3.0, "YM": 50.0, "XAUUSD": 3.0,
                }
                threshold = _futures_thr.get(sym, _get_pip_spec(sym).get("min_atr", 0.0007))
                atr_data = {"atr": atr_val, "is_low_volatility": atr_val < threshold}

    except Exception as e:
        logger.error(f"[fetch_all_timeframes] {symbol}: {e}")

    bundle = {
        "symbol": sym,
        "price": price,
        "candles_5m": candles_5m,
        "candles_15m": candles_15m,
        "candles_1h": candles_1h,
        "candles_4h": candles_4h,
        "candles_daily": candles_daily,
        "atr": atr_data,
        "timestamp": datetime.now(timezone.utc),
    }
    return bundle


def fetch_all_timeframes_sync(symbol: str) -> dict:
    """Synchronous wrapper for fetch_all_timeframes — used by bot commands."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, fetch_all_timeframes(symbol))
                return future.result(timeout=30)
        else:
            return loop.run_until_complete(fetch_all_timeframes(symbol))
    except Exception as e:
        logger.error(f"[fetch_sync] {symbol} error: {e}")
        return {}


def get_htf_bias(symbol: str, candles_1h: list = None, candles_4h: list = None, candles_daily: list = None) -> dict:
    """Get Daily, 4H, and 1H trend bias for multi-timeframe confirmation."""
    result = {"h1_trend": "unclear", "h4_trend": "unclear", "d1_trend": "unclear", "aligned": False, "bias": "unclear"}
    try:
        # Fast path — use pre-fetched candles (newest-first) from the unified bundle
        if candles_1h and candles_4h and candles_daily:
            # Use recent 5-candle window only — comparing newest to 30-day-ago close
            # gives false readings when price recovered after a longer-term drop.
            # 5 candles = 5 hours on 1H, 20 hours on 4H, 5 days on D1 — captures
            # the current institutional bias without being distorted by distant history.
            _h1_window = candles_1h[:5]
            _h4_window = candles_4h[:5]
            _d1_window = candles_daily[:5]
            h1_trend = "bullish" if _h1_window[0]["close"] > _h1_window[-1]["close"] else "bearish"
            h4_trend = "bullish" if _h4_window[0]["close"] > _h4_window[-1]["close"] else "bearish"
            d1_trend = "bullish" if _d1_window[0]["close"] > _d1_window[-1]["close"] else "bearish"
            result["h1_trend"] = h1_trend
            result["h4_trend"] = h4_trend
            result["d1_trend"] = d1_trend
            all_aligned = h1_trend == h4_trend == d1_trend
            result["aligned"] = all_aligned
            result["bias"] = h1_trend if all_aligned else ("mixed" if h1_trend != h4_trend else h4_trend)
            return result
    except Exception as _e:
        logger.debug(f"[get_htf_bias] pre-fetch path failed for {symbol}: {_e} — falling back to fetch")
    try:
        if symbol.upper() in YFINANCE_FUTURES_MAP:
            ticker = YFINANCE_FUTURES_MAP[symbol.upper()]
            h1 = yf.Ticker(ticker).history(period="14d", interval="1h")
            h4 = yf.Ticker(ticker).history(period="30d", interval="4h")
            d1 = yf.Ticker(ticker).history(period="60d", interval="1d")
            if h1.empty or h4.empty or d1.empty:
                return result
            h1_trend = "bullish" if h1["Close"].iloc[-1] > h1["Close"].iloc[-5] else "bearish"
            h4_trend = "bullish" if h4["Close"].iloc[-1] > h4["Close"].iloc[-5] else "bearish"
            d1_trend = "bullish" if d1["Close"].iloc[-1] > d1["Close"].iloc[-5] else "bearish"
        else:
            from market import YFINANCE_FOREX_MAP as _YF_FOREX_MAP
            _yf_sym = ("GC=F" if symbol.upper() == "XAUUSD"
                       else _YF_FOREX_MAP.get(symbol.upper()))

            h1_trend = h4_trend = d1_trend = None

            # TD primary for forex HTF candles (not XAUUSD — uses yFinance GC=F)
            if TWELVE_DATA_API_KEY and symbol.upper() != "XAUUSD" and _td_available():
                try:
                    td_symbol = normalize_symbol(symbol)
                    def _fetch_td_htf(interval, outputsize=30):
                        resp = requests.get(f"{BASE_URL}/time_series",
                            params={"symbol": td_symbol, "interval": interval,
                                    "outputsize": outputsize, "apikey": TWELVE_DATA_API_KEY}, timeout=8)
                        data = resp.json()
                        if data.get("status") != "error" and "values" in data:
                            return [float(v["close"]) for v in data["values"]]
                        if "credits" in data.get("message", "").lower():
                            _mark_td_exhausted()
                        return None
                    _h1 = _fetch_td_htf("1h", 30)
                    _h4 = _fetch_td_htf("4h", 30)
                    _d1 = _fetch_td_htf("1day", 30)
                    if _h1 and _h4 and _d1:
                        # TD values are newest-first; [0]=newest, [-1]=oldest
                        # Newest-first: [0]=newest, [4]=5 candles ago
                        h1_trend = "bullish" if _h1[0] > _h1[min(4, len(_h1)-1)] else "bearish"
                        h4_trend = "bullish" if _h4[0] > _h4[min(4, len(_h4)-1)] else "bearish"
                        d1_trend = "bullish" if _d1[0] > _d1[min(4, len(_d1)-1)] else "bearish"
                except Exception as _e:
                    logger.warning(f"[htf_bias] TD failed for {symbol}: {_e} — falling back to yFinance")

            # yFinance fallback (also primary for XAUUSD)
            if None in (h1_trend, h4_trend, d1_trend):
                if not _yf_sym:
                    return result
                h1 = yf.Ticker(_yf_sym).history(period="14d", interval="1h")
                h4 = yf.Ticker(_yf_sym).history(period="30d", interval="4h")
                d1 = yf.Ticker(_yf_sym).history(period="30d", interval="1d")
                if h1.empty or h4.empty or d1.empty:
                    return result
                h1_trend = "bullish" if h1["Close"].iloc[-1] > h1["Close"].iloc[-5] else "bearish"
                h4_trend = "bullish" if h4["Close"].iloc[-1] > h4["Close"].iloc[-5] else "bearish"
                d1_trend = "bullish" if d1["Close"].iloc[-1] > d1["Close"].iloc[-5] else "bearish"

        result["h1_trend"] = h1_trend
        result["h4_trend"] = h4_trend
        result["d1_trend"] = d1_trend
        all_aligned = h1_trend == h4_trend == d1_trend
        result["aligned"] = all_aligned
        result["bias"] = h1_trend if all_aligned else ("mixed" if h1_trend != h4_trend else h4_trend)
    except Exception as e:
        logger.warning(f"HTF bias error for {symbol}: {e}")
    return result



def detect_breakout(candles: list, direction: str) -> bool:
    """
    Returns True when the most recent close breaks beyond the last 5 candles' extremes:
    BUY  — close above the highest high of candles[1..5]
    SELL — close below the lowest low of candles[1..5]
    """
    if not candles or len(candles) < 6:
        return False
    current_close = candles[0]["close"]
    prev5 = candles[1:6]
    if direction == "BUY":
        return current_close > max(c["high"] for c in prev5)
    if direction == "SELL":
        return current_close < min(c["low"] for c in prev5)
    return False


async def check_tjr_gates(symbol: str, candles: list, ob: dict, fvg: dict,
                    htf_bias: dict, market_structure: str, daily_bias: dict,
                    atr_data: dict, direction: str, structure: dict, ms: dict,
                    data: dict = None, displacement: dict = None,
                    current_price: float = 0.0, draw: dict = None) -> tuple:
    """
    7 binary gates — all must pass for a signal to fire.
    Returns (all_passed, gates, gate_details, failed, kz_label, swept_level).
    """
    gates = {}
    gate_details = {}

    # GATE 1 — Kill zone active (London 07-09 UTC or NY 12-14 UTC)
    _kz_active, _kz_label = is_kill_zone(symbol)
    gates['kill_zone'] = _kz_active
    _kz_short = (
        _kz_label.replace("Kill zone active — ", "").replace(" — peak institutional activity", "")
        if _kz_active else "outside kill zone"
    )
    gate_details['kill_zone'] = _kz_short

    # GATE 2 — HTF bias confirmed (Daily + 4H agree with signal direction, daily bias confirmed)
    _htf_dir = "bullish" if direction == "BUY" else "bearish"
    # G2: weighted daily bias scoring covers D1 — only require 4H from get_htf_bias
    # Raw 5-candle D1 comparison gets distorted by prior week highs (e.g. gold at 4224 last week)
    _htf_ok = htf_bias.get("h4_trend") == _htf_dir  # 4H must align
    _bias_aligned, _bias_msg = check_daily_bias_alignment(symbol, direction, _prefetched=daily_bias)
    gates['htf_bias'] = _htf_ok and _bias_aligned
    _d1 = htf_bias.get("d1_trend", "unclear")
    _h4 = htf_bias.get("h4_trend", "unclear")
    if _htf_ok and _bias_aligned:
        gate_details['htf_bias'] = f"Daily/4H {_htf_dir}"
    elif not _bias_aligned:
        gate_details['htf_bias'] = _bias_msg or "daily bias unconfirmed"
    else:
        gate_details['htf_bias'] = f"D1={_d1} 4H={_h4} need={_htf_dir}"

    # 1H structure — informational context only, does not block signal
    candles_1h = data.get('candles_1h', []) if data else []
    if candles_1h:
        ms_1h = analyze_market_structure(candles_1h)
        gate_details['htf_1h'] = ms_1h.get('structure', 'ranging')
    else:
        gate_details['htf_1h'] = 'unknown'

    # GATE 3 — Market structure confirmed (not ranging)
    gates['structure'] = market_structure in ('uptrend', 'downtrend')
    gate_details['structure'] = (
        "Uptrend" if market_structure == "uptrend" else
        "Downtrend" if market_structure == "downtrend" else
        f"{market_structure} — not trending"
    )

    # GATE 4 — Liquidity sweep detected (Asia range or recent swing)
    await _update_asia_levels(symbol, candles)
    _sweep_ok, _swept_level, _sweep_type = _detect_asia_sweep_or_recent(symbol, candles, direction)

    # Asia range size check: wide ranges degrade Judas Swing edge significantly.
    _sym_upper_g4 = symbol.upper()
    _asia_lvl = _asia_levels.get(_sym_upper_g4, {})
    _asia_h = _asia_lvl.get("high", 0.0)
    _asia_l = _asia_lvl.get("low", 0.0)
    _range_tight, _range_reason = is_asia_range_tight(symbol, _asia_h, _asia_l)
    if not _range_tight and _sweep_ok:
        logger.info(
            f"[G4] {symbol} Asia range wide — {_range_reason} (sweep still passed, quality noted)"
        )

    gates['sweep'] = _sweep_ok
    _sym_upper = symbol.upper()
    _is_pts = _sym_upper in ("XAUUSD", "US30", "NAS100") or _sym_upper in YFINANCE_FUTURES_MAP
    _swept_dp = 3 if _is_pts else 5
    # Draw context: if draw on liquidity is confirmed, enrich sweep description with WHERE price is heading
    _draw_detail = ""
    if draw:
        _draw_pips = draw.get('distance_pips', 0)
        _draw_type = draw.get('type', '')
        _draw_detail = f" → draw: {_draw_type} ({_draw_pips:.0f}p)"
    _sweep_label = "liquidity run" if _sweep_type == "liquidity_run" else "Judas Swing"
    gate_details['sweep'] = (
        (f"{_sweep_label} at {_swept_level:.{_swept_dp}f}" + _draw_detail) if (_sweep_ok and _swept_level) else
        f"no liquidity sweep{_draw_detail}"
    )
    gate_details['sweep_type'] = _sweep_type

    # GATE 5 — OB or FVG present; C-tier OBs fail; liquidity runs require OB+FVG confluence
    _has_ob = bool(ob) and ob.get('tier', '') != 'C-tier'
    _has_fvg = bool(fvg)
    _has_displacement_fvg = bool(displacement) and is_price_in_displacement_fvg(current_price, displacement)
    _has_confluence = _has_ob and _has_fvg
    _is_liquidity_run = _sweep_type == "liquidity_run"

    if _is_liquidity_run:
        # Lower-probability setup — single OB or FVG not enough; need both or displacement FVG
        gates['ob_fvg'] = _has_confluence or _has_displacement_fvg
    else:
        gates['ob_fvg'] = _has_ob or _has_fvg or _has_displacement_fvg

    _disp_off = FUTURES_SPOT_OFFSET.get(_sym_upper, 0)
    _dp = 3 if _is_pts else 5
    if _has_displacement_fvg and not _has_ob and not _has_fvg:
        gate_details['ob_fvg'] = (
            f"Displacement FVG {round(displacement['fvg_bottom'] + _disp_off, _dp)}"
            f"-{round(displacement['fvg_top'] + _disp_off, _dp)}"
            f" | CE={round(displacement['fvg_mid'] + _disp_off, _dp)}"
            f" | OTE={round(displacement.get('ote_low', 0) + _disp_off, _dp)}"
            f"-{round(displacement.get('ote_high', 0) + _disp_off, _dp)}"
        )
    elif ob:
        _ob_lo = round(ob['low']  + _disp_off, _dp)
        _ob_hi = round(ob['high'] + _disp_off, _dp)
        _ob_tier = ob.get('tier', '')
        _tier_suffix = f" ({_ob_tier})" if _ob_tier else ""
        if _ob_tier == 'C-tier':
            gate_details['ob_fvg'] = f"{_ob_lo}-{_ob_hi} (C-tier — gate fail)"
        elif _has_confluence:
            gate_details['ob_fvg'] = f"{_ob_lo}-{_ob_hi}{_tier_suffix} + FVG (confluence)"
        elif _is_liquidity_run:
            gate_details['ob_fvg'] = f"{_ob_lo}-{_ob_hi}{_tier_suffix} (OB only — confluence required)"
        else:
            gate_details['ob_fvg'] = f"{_ob_lo}-{_ob_hi}{_tier_suffix} (OB only)"
    elif fvg:
        _fb = fvg.get("display_bottom", round(fvg["bottom"] + _disp_off, _dp))
        _ft = fvg.get("display_top",    round(fvg["top"]    + _disp_off, _dp))
        if _is_liquidity_run:
            gate_details['ob_fvg'] = f"FVG {_fb}-{_ft} (FVG only — confluence required)"
        else:
            gate_details['ob_fvg'] = f"FVG {_fb}-{_ft} (fresh)"
    else:
        gate_details['ob_fvg'] = "no OB or FVG found"

    # INFORMATIONAL — Premium/Discount zone (does not block signal)
    _d1_candles = (data.get("candles_daily") or []) if data else []
    if _d1_candles and current_price:
        _pd_ok, _pd_msg = check_premium_discount_zone(_d1_candles, current_price, direction)
        if _pd_ok:
            _pd_label = "✅ Discount zone" if direction == "BUY" else "✅ Premium zone"
        else:
            _pd_label = "⚠️ Premium zone (caution)" if direction == "BUY" else "⚠️ Discount zone (caution)"
        gate_details['premium_discount'] = _pd_label
        if not _pd_ok:
            logger.warning(
                "[premium_discount] %s %s — %s", symbol, direction, _pd_msg
            )
    else:
        gate_details['premium_discount'] = ""

    # GATE 6 — BOS confirmed after sweep; requires >= 2 consecutive displacement candles.
    # CHoCH (Change of Character): close breaks structure AGAINST the prior trend after a sweep.
    #   → Highest-probability reversal. The sweep trapped one side; CHoCH confirms the flip.
    #   → SL anchor: swept level (tight, institutional).
    # BOS in trend: close breaks structure IN the trend direction.
    #   → Continuation entry, moderate confidence. SL anchor: OB/FVG zone.
    _bos_ok = bool(structure.get('bos') or ms.get('bos') or structure.get('choch') or ms.get('choch'))
    _bos_type = structure.get('bos_type') or ms.get('bos_type')   # 'choch', 'bos', or None
    _is_choch = _bos_type == 'choch' or bool(structure.get('choch') or ms.get('choch'))
    if _bos_ok:
        _bos_quality, _bos_count, _bos_desc = score_bos_quality(candles, direction)
        if _bos_quality == "weak":
            gates['bos'] = False
            gate_details['bos'] = f"weak displacement ({_bos_count} candle) — gate fail"
            gate_details['bos_type'] = _bos_type or 'unknown'
        else:
            gates['bos'] = True
            if _is_choch:
                # CHoCH after sweep = Judas Swing fully confirmed — highest quality
                gate_details['bos'] = (
                    f"CHoCH confirmed ({_bos_quality} displacement, {_bos_count} candles) — "
                    f"reversal confirmed ✅ SL: swept level"
                )
                gate_details['bos_type'] = 'choch'
            else:
                # Standard BOS in trend direction
                gate_details['bos'] = (
                    f"BOS confirmed ({_bos_quality} displacement, {_bos_count} candles) — "
                    f"continuation"
                )
                gate_details['bos_type'] = 'bos'
    else:
        gates['bos'] = False
        gate_details['bos'] = "not confirmed"
        gate_details['bos_type'] = None

    # GATE 7 — Volatility adequate (not low vol)
    _vol_ok = not atr_data.get('is_low_volatility', False) if atr_data else True
    gates['volatility'] = _vol_ok
    gate_details['volatility'] = "healthy" if _vol_ok else "low volatility"

    # ── POWER OF THREE — Midnight open context (informational, does not block) ───
    # Confirms whether the current sweep is on the correct PO3 side.
    # Bullish PO3: current price should be below midnight open (manipulation phase) before reversing up.
    # Bearish PO3: current price should be above midnight open (manipulation phase) before reversing down.
    _mo_data = _midnight_open.get(symbol.upper(), {})
    _mo_price = _mo_data.get("price", 0.0)
    _mo_is_today = _mo_data.get("date") == str(datetime.now(timezone.utc).date())
    if _mo_price and _mo_is_today:
        _is_pts_mo = symbol.upper() in ("XAUUSD", "US100", "US30", "US500") or symbol.upper() in YFINANCE_FUTURES_MAP
        _dp_mo = 3 if _is_pts_mo else 5
        _mo_display = round(_mo_price + FUTURES_SPOT_OFFSET.get(symbol.upper(), 0), _dp_mo)
        if current_price and current_price > 0:
            _po3_side = "below" if current_price < _mo_price else "above"
            if direction == "BUY" and current_price < _mo_price:
                gate_details['po3'] = f"✅ PO3: price {_po3_side} midnight open ({_mo_display}) — manipulation confirmed"
            elif direction == "SELL" and current_price > _mo_price:
                gate_details['po3'] = f"✅ PO3: price {_po3_side} midnight open ({_mo_display}) — manipulation confirmed"
            elif direction == "BUY" and current_price > _mo_price:
                gate_details['po3'] = f"⚠️ PO3: price ABOVE midnight open ({_mo_display}) — possible distribution already underway"
            else:
                gate_details['po3'] = f"⚠️ PO3: price BELOW midnight open ({_mo_display}) — possible distribution already underway"
        else:
            gate_details['po3'] = f"📊 PO3: midnight open {_mo_display}"
    else:
        gate_details['po3'] = "📊 PO3: midnight open not yet available (pre-00:00 UTC)"

    all_passed = all(gates.values())
    failed = [k for k, v in gates.items() if not v]

    return all_passed, gates, gate_details, failed, _kz_label, _swept_level


def build_auto_signal(symbol: str, direction: str, price: float,
                      ob: dict, fvg: dict, structure: dict,
                      htf_bias: dict) -> str:
    """
    Auto-build a complete formatted signal from scanner data.
    This is what gets sent to the grader when user taps Grade button.
    """
    trend = structure.get("trend", "bullish")

    # Calculate entry, stop loss, and targets
    from futures_instruments import is_futures, get_spec
    spec = get_spec(symbol) if is_futures(symbol) else None

    if direction == "BUY":
        # Entry: if price is inside OB/FVG zone use current price, else use zone mid (limit order)
        if ob and ob["type"] == "bullish_ob":
            entry = price if ob["low"] <= price <= ob["high"] else ob["mid"]
            sl = round(ob["low"] - (ob["high"] - ob["low"]) * 0.1, 5)
        elif fvg and fvg["type"] == "bullish_fvg":
            entry = price if fvg["bottom"] <= price <= fvg["top"] else fvg["top"]
            sl = round(fvg["bottom"] - (fvg["top"] - fvg["bottom"]) * 0.5, 5)
        else:
            entry = price
            sl = round(price * 0.998, 5) if not spec else round(price - spec["typical_sl_pts"], 3)

        sl_dist = abs(entry - sl)
        sl_dist = max(sl_dist, _min_sl_dist(symbol))  # floor — never too tight
        sl_dist = min(sl_dist, _max_sl_dist(symbol))  # ceiling — never too wide
        _use_3dp = spec or symbol.upper() in ("XAUUSD", "US100", "US30", "NAS100")
        sl = round(entry - sl_dist, 3) if _use_3dp else round(entry - sl_dist, 5)
        tp1 = round(entry + sl_dist * 2.0, 3 if _use_3dp else 5)
        tp2 = round(entry + sl_dist * 3.0, 3 if _use_3dp else 5)
        tp3 = round(entry + sl_dist * 5.0, 3 if _use_3dp else 5)
        logger.info(f"[build_signal] {symbol} BUY sl_dist={sl_dist:.5f} min={_min_sl_dist(symbol):.5f} max={_max_sl_dist(symbol):.5f} entry={entry} sl={sl} tp1={tp1}")

    else:  # SELL
        # Entry: if price is inside OB/FVG zone use current price, else use zone mid (limit order)
        if ob and ob["type"] == "bearish_ob":
            entry = price if ob["low"] <= price <= ob["high"] else ob["mid"]
            sl = round(ob["high"] + (ob["high"] - ob["low"]) * 0.1, 5)
        elif fvg and fvg["type"] == "bearish_fvg":
            entry = price if fvg["bottom"] <= price <= fvg["top"] else fvg["bottom"]
            sl = round(fvg["top"] + (fvg["top"] - fvg["bottom"]) * 0.5, 5)
        else:
            entry = price
            sl = round(price * 1.002, 5) if not spec else round(price + spec["typical_sl_pts"], 3)

        sl_dist = abs(sl - entry)
        sl_dist = max(sl_dist, _min_sl_dist(symbol))  # floor — never too tight
        sl_dist = min(sl_dist, _max_sl_dist(symbol))  # ceiling — never too wide
        _use_3dp = spec or symbol.upper() in ("XAUUSD", "US100", "US30", "NAS100")
        sl = round(entry + sl_dist, 3) if _use_3dp else round(entry + sl_dist, 5)
        tp1 = round(entry - sl_dist * 2.0, 3 if _use_3dp else 5)
        tp2 = round(entry - sl_dist * 3.0, 3 if _use_3dp else 5)
        tp3 = round(entry - sl_dist * 5.0, 3 if _use_3dp else 5)
        logger.info(f"[build_signal] {symbol} SELL sl_dist={sl_dist:.5f} min={_min_sl_dist(symbol):.5f} max={_max_sl_dist(symbol):.5f} entry={entry} sl={sl} tp1={tp1}")

    # Build setup description
    setup_parts = []
    if ob:
        setup_parts.append("Order Block Retest")
    if fvg:
        setup_parts.append("Fair Value Gap")
    if structure.get("bos"):
        setup_parts.append("Break of Structure")
    setup = " + ".join(setup_parts) if setup_parts else "Structure Setup"

    # HTF confirmation text
    htf_parts = []
    if htf_bias:
        d1 = htf_bias.get("d1_trend", "")
        h4 = htf_bias.get("h4_trend", "")
        h1 = htf_bias.get("h1_trend", "")
        if d1 and h4 and h1:
            htf_parts.append(f"Daily {d1}, 4H {h4}, 1H {h1}")
    htf_str = ", ".join(htf_parts) if htf_parts else "HTF aligned"

    trend_dir = "Bullish" if direction == "BUY" else "Bearish"

    # Enforce minimum 2.0:1 TP1 floor
    _tp1_min_dist = sl_dist * 2.0
    if direction == "BUY" and (tp1 - entry) < _tp1_min_dist - 0.0001:
        tp1 = round(entry + _tp1_min_dist, 3 if _use_3dp else 5)
        logger.warning(f"[build_signal] {symbol} BUY TP1 corrected to 2.0R: tp1={tp1}")
    elif direction == "SELL" and (entry - tp1) < _tp1_min_dist - 0.0001:
        tp1 = round(entry - _tp1_min_dist, 3 if _use_3dp else 5)
        logger.warning(f"[build_signal] {symbol} SELL TP1 corrected to 2.0R: tp1={tp1}")

    # Round prices cleanly and apply spot offset for instruments where yFinance
    # returns futures prices that differ from MT5 spot price
    _spot_offset = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)
    def rp(v):
        adjusted = float(v) + _spot_offset
        if spec:
            return round(adjusted, 3)  # futures
        elif symbol.upper() in ("XAUUSD", "US100", "US30", "NAS100"):
            return round(adjusted, 3)  # gold and indices use 3dp
        else:
            return round(adjusted, 5)  # forex pairs use 5dp
    entry = rp(entry)
    sl = rp(sl)
    tp1 = rp(tp1)
    tp2 = rp(tp2)
    tp3 = rp(tp3)
    logger.info(f"[build_signal] {symbol} {direction} entry={entry} sl={sl} tp1={tp1} domain=spot")

    signal = (
        f"{symbol} {direction} SIGNAL\n"
        f"Provider: TNL Scanner\n"
        f"Timeframe: 15M\n"
        f"Setup: {setup}\n"
        f"Entry Zone: {entry}\n"
        f"Stop Loss: {sl}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"TP3: {tp3}\n"
        f"Trend: {trend_dir}\n"
        f"Confirmation: Yes — all 7 institutional gates passed, {htf_str}"
    )
    return signal



def format_unified_signal(symbol: str, direction: str,
                          entry: float, sl: float, tp1: float, tp2: float,
                          ob: dict, fvg: dict, structure: dict, htf_bias: dict,
                          swept_level: float,
                          kill_zone_label: str, lot_str: str,
                          gates: dict = None, gate_details: dict = None,
                          entry_tf: str = "15M Entry",
                          displacement: dict = None,
                          draw: dict = None, tp3: float = None) -> str:
    """Build the single clean TNL TRADER SIGNAL block — no scan alert, no grade step."""
    DIV = "━━━━━━━━━━━━━━━━━━━━"
    sym = symbol.upper()

    # Decimal places and SL display
    _is_pts = sym in ("XAUUSD", "US30", "NAS100") or sym in YFINANCE_FUTURES_MAP
    _dp = 3 if _is_pts else 5
    sl_dist = abs(entry - sl)
    if _is_pts:
        _sl_display = f"{round(sl_dist, 1)} pts"
    elif "JPY" in sym:
        _sl_display = f"{round(sl_dist * 100, 1)} pips"
    else:
        _sl_display = f"{round(sl_dist * 10000, 1)} pips"

    # Order type
    if ob:
        _type_str = "OB Retracement / LIMIT ORDER"
    elif fvg:
        _type_str = "FVG Fill / LIMIT ORDER"
    elif gates and gates.get('ob_fvg'):
        _type_str = "Displacement FVG / LIMIT ORDER"
    else:
        _type_str = "Structure Setup / LIMIT ORDER"

    # HTF summary line
    d1 = htf_bias.get("d1_trend", "")
    h4 = htf_bias.get("h4_trend", "")
    h1 = htf_bias.get("h1_trend", "")
    _htf_display = f"Daily/4H/1H {d1}" if (d1 and h4 and h1) else f"HTF {direction.lower()}"

    # Swept level (already has spot offset applied from _detect_asia_sweep_or_recent)
    _swept_dp = 3 if _is_pts else 5
    _swept_str = f"{swept_level:.{_swept_dp}f}" if swept_level else "recent high/low"
    _is_liq_run = bool(gate_details) and gate_details.get('sweep_type') == 'liquidity_run'
    _sweep_line = (f"🔄 Liquidity Run: through {_swept_str}"
                   if _is_liq_run else f"✅ Judas Swing: swept at {_swept_str}")

    # OB / FVG condition lines
    _cond_lines = [
        f"✅ HTF: {_htf_display}",
        _sweep_line,
    ]
    if ob:
        _ob_off  = FUTURES_SPOT_OFFSET.get(sym, 0)
        _ob_type = "Bullish OB" if ob["type"] == "bullish_ob" else "Bearish OB"
        _ob_lo   = round(ob["low"]  + _ob_off, _dp)
        _ob_hi   = round(ob["high"] + _ob_off, _dp)
        _ob_tier = ob.get('tier', '')
        _ob_tier_suffix = f" ({_ob_tier})" if _ob_tier else ""
        _ob_warn = "⚠️" if _ob_tier == "C-tier" else "✅"
        _cond_lines.append(f"{_ob_warn} {_ob_type}: {_ob_lo}-{_ob_hi}{_ob_tier_suffix}")
    if fvg:
        _fvg_bot = fvg.get("display_bottom", round(fvg["bottom"] + FUTURES_SPOT_OFFSET.get(sym, 0), _dp))
        _fvg_top = fvg.get("display_top",    round(fvg["top"]    + FUTURES_SPOT_OFFSET.get(sym, 0), _dp))
        _cond_lines.append(f"✅ FVG: {_fvg_bot}-{_fvg_top} (fresh)")
    if structure.get("bos"):
        _cond_lines.append("✅ BOS confirmed (body close)")
    _kz_short = (kill_zone_label
                 .replace("Kill zone active — ", "")
                 .replace(" — peak institutional activity", ""))
    _cond_lines.append(f"✅ Kill zone: {_kz_short}")

    # Action line
    _dir_word = "Buy" if direction == "BUY" else "Sell"
    _action = f"🔄 Set {_dir_word} Limit at {entry:.{_dp}f}"

    # Gate checklist — show which of the 7 gates passed, plus 1H context
    if gates and gate_details:
        _1h_struct = gate_details.get('htf_1h', 'unknown')
        _dir_structure = 'uptrend' if direction == 'BUY' else 'downtrend'
        if _1h_struct == _dir_structure:
            _1h_line = f"✅ 1H: {_1h_struct}"
        elif _1h_struct == 'ranging':
            _1h_line = "⚠️ 1H: Ranging (lagging)"
        elif _1h_struct == 'unknown':
            _1h_line = "📊 1H: no data"
        else:
            _1h_line = f"⚠️ 1H: {_1h_struct} (conflict — caution)"
        _gate_lines = [
            f"✅ Kill zone: {gate_details.get('kill_zone', '')}",
            f"✅ HTF: {gate_details.get('htf_bias', '')}",
            _1h_line,
            f"✅ Structure: {gate_details.get('structure', '')}",
            f"✅ Sweep: {gate_details.get('sweep', '')}",
        ]
        if ob:
            _ob_label = "Bullish OB" if ob["type"] == "bullish_ob" else "Bearish OB"
            _ob_detail = gate_details.get('ob_fvg', '')
            _ob_warn_g = "⚠️" if ob.get('tier') == "C-tier" else "✅"
            _gate_lines.append(f"{_ob_warn_g} {_ob_label}: {_ob_detail}")
        elif fvg:
            _gate_lines.append(f"✅ FVG: {gate_details.get('ob_fvg', '')}")
        elif gates and gates.get('ob_fvg'):
            _gate_lines.append(f"✅ OB/FVG: {gate_details.get('ob_fvg', 'Displacement retracement')}")
        _bos_type_label = gate_details.get('bos_type', '')
        _bos_detail = gate_details.get('bos', 'confirmed (body close)')
        if _bos_type_label == 'choch':
            _gate_lines.append(f"🔥 CHoCH: {_bos_detail}")
        else:
            _gate_lines.append(f"✅ BOS: {_bos_detail}")
        _gate_lines.append(f"✅ Volatility: {gate_details.get('volatility', 'healthy')}")
        _po3 = gate_details.get('po3', '')
        if _po3:
            _gate_lines.append(_po3)
        _pd = gate_details.get('premium_discount', '')
        if _pd:
            _gate_lines.append(_pd)
        if draw:
            _draw_level_disp = round(draw.get('level', 0) + FUTURES_SPOT_OFFSET.get(sym, 0), _dp)
            _draw_unit = "pts" if _is_pts else "pips"
            _gate_lines.append(
                f"🎯 Draw on Liquidity: {draw['type']} at {_draw_level_disp:.{_dp}f} ({draw['distance_pips']:.1f} {_draw_unit})"
            )
    else:
        # Fallback when no gate data available
        _gate_lines = _cond_lines

    _is_ote_entry = bool(displacement and displacement.get('ote_mid') is not None and not ob and not fvg)
    _entry_label = "Entry"
    _entry_ote_suffix = " (OTE 62-79%)" if _is_ote_entry else ""
    _ote_lines = []

    lines = [
        DIV,
        "🏆 TNL TRADER SIGNAL",
        DIV,
        f"📊 {symbol} | {direction} | 7/7 Gates ✅ | {entry_tf}",
        f"📍 Entry: {entry:.{_dp}f}{_entry_ote_suffix}",
        f"🛑 SL:       {sl:.{_dp}f}  ({_sl_display})",
        f"🎯 TP1:      {tp1:.{_dp}f}  (2.0R)",
        f"🎯 TP2:      {tp2:.{_dp}f}  (3.0R)",
    ] + ([f"🎯 TP3:      {tp3:.{_dp}f}  (Draw)"] if tp3 else []) + [
        f"📦 Lots:     {lot_str}",
        f"⚡ Type:     {_type_str}",
    ] + _ote_lines + [
        DIV,
    ] + _gate_lines + [
        DIV,
        _action,
        DIV,
    ]
    return "\n".join(lines)



async def scan_symbol(symbol: str, active_signals: list = None) -> dict | None:
    """
    Run full scan on one symbol. Returns alert dict if setup found, None otherwise.
    All 7 binary gates must pass — no scoring, no thresholds.
    """
    try:
        # ── UNIFIED DATA FETCH ───────────────────────────────────────────────
        # Single call fetches all timeframes (cached 4 min) — every component
        # below reads from this bundle instead of fetching independently.
        _data = await fetch_all_timeframes(symbol)
        candles = _data.get("candles_15m", [])
        if not candles or len(candles) < 10:
            return None

        # ── TIER 1: CANDLE CLOSE CONFIRMATION ────────────────────────────────
        # Only fire on confirmed closed candles — skip if most recent candle just opened.
        _dt_str = candles[0].get("datetime", "")
        if _dt_str:
            try:
                _candle_dt = datetime.fromisoformat(_dt_str)
                if _candle_dt.tzinfo is None:
                    _candle_dt = _candle_dt.replace(tzinfo=timezone.utc)
                else:
                    _candle_dt = _candle_dt.astimezone(timezone.utc)
                _candle_age = (datetime.now(timezone.utc) - _candle_dt).total_seconds()
                if _candle_age < 60:
                    logger.info(
                        f"[scanner] {symbol} — candle just opened ({_candle_age:.0f}s ago) — waiting for close"
                    )
                    return None
            except Exception:
                pass  # unparseable datetime string — proceed normally

        # ── MARKET STRUCTURE + DIRECTION — PRIMARY GATE ─────────────────────
        ms = analyze_market_structure(candles)
        _gt_direction, _gt_strength = get_trade_direction(symbol, candles)
        if _gt_direction is None:
            logger.info(f"[scanner] {symbol} {_gt_strength} — no trade")
            return None
        # Block weak signals — neutral daily bias means no D1 institutional alignment.
        # Without HTF confirmation, the trade is structure-only: lower probability,
        # higher risk of being on the wrong side of a larger move. Skip for FTMO.
        if _gt_strength == "weak":
            logger.info(f"[scanner] {symbol} direction weak (neutral D1 bias) — skipping, no HTF alignment")
            return None
        logger.info(f"[scanner] {symbol} direction {_gt_direction} ({_gt_strength})")
        market_structure = ms["structure"]  # "uptrend" or "downtrend" (ranging already gated)

        price_data = {"price": _data["price"]} if _data.get("price") else None
        atr_data   = _data.get("atr") or None

        # ── TIER 1: HARD BLOCKS ──────────────────────────────────────────────
        news_blocked, news_reason, news_warning = is_news_window(symbol)
        if news_blocked:
            logger.info(f"[scanner] {symbol} BLOCKED — {news_reason}")
            return None
        if news_warning:
            logger.info(f"[scanner] {symbol} WARNING — {news_warning}")

        # Structure and setup detection
        structure = detect_structure(candles)
        trend = structure.get("trend", "unclear")
        if trend == "unclear":
            return None

        # OB detection uses direction from the primary gate (not detect_structure trend)
        _ob_trend = "bullish" if _gt_direction == "BUY" else "bearish"
        ob = detect_order_block(candles, _ob_trend, symbol=symbol)
        _cp = float(candles[0]["close"]) if candles else 0.0

        # FIX 3 — OB freshness: skip already-mitigated OBs; mark new ones if price inside
        if ob:
            if is_ob_mitigated(symbol, ob['low'], ob['high']):
                logger.info(f"[ob] {symbol} OB already mitigated — skip")
                ob = None
            elif ob['low'] <= _cp <= ob['high']:
                mark_ob_mitigated(symbol, ob['low'], ob['high'])
                ob = None

        fvg = detect_fvg(candles, symbol)

        # FIX 2 — FVG freshness: skip already-mitigated FVGs; mark new ones if price inside
        if fvg:
            if is_fvg_mitigated(symbol, fvg['bottom'], fvg['top']):
                logger.info(f"[fvg] {symbol} FVG already mitigated — skip")
                fvg = None
            else:
                # True ICT mitigation: candle body must close inside the FVG
                # A wick entry is NOT mitigation — it's price respecting the zone
                _close_price = candles[0].get("close", _cp) if candles else _cp
                if fvg['bottom'] <= float(_close_price) <= fvg['top']:
                    mark_fvg_mitigated(symbol, fvg['bottom'], fvg['top'])
                    fvg = None

        from scanner_improvements import get_daily_bias as _get_daily_bias
        daily_bias = _get_daily_bias(symbol, candles=_data.get("candles_daily"))

        htf_bias = get_htf_bias(
            symbol,
            candles_1h=_data.get("candles_1h"),
            candles_4h=_data.get("candles_4h"),
            candles_daily=_data.get("candles_daily"),
        )
        direction = _gt_direction

        # ── DRAW ON LIQUIDITY ─────────────────────────────────────────────────
        await _update_asia_levels(symbol, candles)
        draw = get_draw_on_liquidity(symbol, candles, direction, _asia_levels.get(symbol, {}))
        if draw:
            _draw_unit = "pts" if symbol.upper() in ("XAUUSD", "US100", "US30") or symbol.upper() in YFINANCE_FUTURES_MAP else "pips"
            logger.info(f"[draw] {symbol} draw on liquidity: {draw['type']} at {draw['level']:.5f} ({draw['distance_pips']:.1f} {_draw_unit})")

        displacement = detect_displacement(candles, direction, symbol)
        if displacement:
            logger.info(
                f"[displacement] {symbol} detected {displacement['candle_count']} candle displacement "
                f"— FVG {displacement['fvg_bottom']:.5f}-{displacement['fvg_top']:.5f} | CE={displacement['fvg_mid']:.5f}"
            )

        # ── DIRECTION LOCK ──────────────────────────────────────────────────────
        _lock = _direction_lock.get(symbol)
        if _lock and _time.time() < _lock["locked_until"]:
            if direction != _lock["direction"]:
                logger.info(f"[scanner] {symbol} direction locked {_lock['direction']} — blocking {direction} signal")
                return None

        # ── 7 BINARY GATES — ALL MUST PASS ─────────────────────────────────────
        all_passed, gates, gate_details, failed_gates, _kz_label, _swept_level = await check_tjr_gates(
            symbol, candles, ob, fvg, htf_bias, market_structure,
            daily_bias, atr_data, direction, structure, ms, data=_data,
            displacement=displacement,
            current_price=float(candles[0]["close"]) if candles else 0.0,
            draw=draw,
        )

        if not all_passed:
            logger.info(f"[scanner] {symbol} — gates failed: {failed_gates} — silent skip")
            return None

        logger.info(f"[scanner] {symbol} {direction} — all 7 gates passed")

        # Signal cooldown — suppress if a signal for this symbol fired < 10 min ago.
        # Uses monotonic time so NTP clock corrections cannot cause false expiry.
        _sym_key = symbol.upper()
        if _sym_key in _last_signal_time:
            _elapsed = _time.monotonic() - _last_signal_time[_sym_key]
            # Check if this is a genuinely new setup — different swept level means
            # a new liquidity grab formed. Same swept level = same setup, suppress it.
            # ICT: once a sweep fires a signal, that setup is consumed. A new sweep
            # at a different level is a new trade opportunity regardless of time elapsed.
            _prev_swept = _last_swept_level.get(_sym_key, 0.0)
            _pip_size = get_pip_spec(_sym_key).get("pip", 0.0001)
            _level_changed = abs(_swept_level - _prev_swept) > (_pip_size * 3)  # 3 pip difference = new level
            if _elapsed < 180 and not _level_changed:
                # Same setup, within 3 minutes — suppress
                logger.info(
                    f"[scanner] {symbol} signal suppressed — same swept level "
                    f"{_swept_level} (cooldown {_elapsed/60:.1f} min)"
                )
                return None
            elif not _level_changed and _elapsed < 300:  # Gold: 5min cooldown (was 10min)
                # Same level, between 3-10 min — suppress
                logger.info(
                    f"[scanner] {symbol} signal suppressed — same swept level "
                    f"{_swept_level} ({_elapsed/60:.1f} min since last)"
                )
                return None
            elif _level_changed:
                logger.info(
                    f"[scanner] {symbol} new swept level {_swept_level} "
                    f"(prev {_prev_swept}) — allowing new signal"
                )

        # ── 5M ENTRY REFINEMENT ──────────────────────────────────────────────────
        candles_5m = _data.get("candles_5m", [])
        _refinement_5m = refine_entry_5m(symbol, candles_5m, direction, ob) if (ob and candles_5m) else None
        _entry_tf = "5M Entry" if _refinement_5m else "15M Entry"
        if _refinement_5m:
            logger.info(f"[scanner] {symbol} 5M OB found within 15M zone: entry={_refinement_5m['entry']} sl={_refinement_5m['sl']}")
        else:
            logger.info(f"[scanner] {symbol} no 5M OB in zone — falling back to 15M entry")

        # ── BUILD SIGNAL LEVELS ─────────────────────────────────────────────────
        current_price = price_data.get("price", 0) if price_data else 0
        _build_cp = float(current_price) - FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)

        auto_signal = build_auto_signal(
            symbol, direction, _build_cp,
            ob, fvg, structure, htf_bias or {}
        )

        signal_key = _cache_signal(auto_signal, score=None)

        # ── RR VERIFICATION ─────────────────────────────────────────────────────
        _sig_entry_m = re.search(r"Entry Zone:\s*([\d.]+)", auto_signal)
        _sig_sl_m    = re.search(r"Stop Loss:\s*([\d.]+)", auto_signal)
        _sig_tp1_m   = _re_sig.search(r"TP1:\s*([\d.]+)", auto_signal)
        _sig_tp2_m   = _re_sig.search(r"TP2:\s*([\d.]+)", auto_signal)
        if _sig_entry_m and _sig_sl_m and _sig_tp1_m:
            _sig_entry = float(_sig_entry_m.group(1))
            _sig_sl    = float(_sig_sl_m.group(1))
            _sig_tp1   = float(_sig_tp1_m.group(1))
            _sig_rr_valid, _sig_actual_rr = validate_risk_reward(_sig_entry, _sig_sl, _sig_tp1)
            if not _sig_rr_valid:
                logger.info(f"[scanner] {symbol} blocked — TP1 RR {_sig_actual_rr:.2f} below minimum 1.5")
                return None
        else:
            _sig_entry = float(current_price) if current_price else 0.0
            _sig_sl = 0.0
            _sig_tp1 = 0.0

        _sig_tp2 = float(_sig_tp2_m.group(1)) if _sig_tp2_m else 0.0
        _sig_tp3_m = _re_sig.search(r"TP3:\s*([\d.]+)", auto_signal)
        _sig_tp3 = float(_sig_tp3_m.group(1)) if _sig_tp3_m else 0.0
        if draw:
            _spot_off_draw = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)
            _is_pts_draw = symbol.upper() in ("XAUUSD", "US30", "NAS100") or symbol.upper() in YFINANCE_FUTURES_MAP
            _dp_draw = 3 if _is_pts_draw else 5
            _sig_tp3 = round(draw['level'] + _spot_off_draw, _dp_draw)
            logger.info(f"[draw] {symbol} TP3 set to draw level {_sig_tp3} ({draw['type']})")

        # ── APPLY 5M ENTRY OVERRIDE ───────────────────────────────────────────────
        if _refinement_5m and _sig_entry_m:
            _spot_offset_5m = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)
            _is_pts_5m = symbol.upper() in ("XAUUSD", "US30", "NAS100") or symbol.upper() in YFINANCE_FUTURES_MAP
            _dp_5m = 3 if _is_pts_5m else 5
            _sig_entry = round(_refinement_5m["entry"] + _spot_offset_5m, _dp_5m)
            _raw_sl_5m = round(_refinement_5m["sl"] + _spot_offset_5m, _dp_5m)
            _5m_sl_dist = max(abs(_sig_entry - _raw_sl_5m), _min_sl_dist(symbol))
            _5m_sl_dist = min(_5m_sl_dist, _max_sl_dist(symbol))
            if direction == "BUY":
                _sig_sl  = round(_sig_entry - _5m_sl_dist, _dp_5m)
                _sig_tp1 = round(_sig_entry + _5m_sl_dist * 1.5, _dp_5m)
                _sig_tp2 = round(_sig_entry + _5m_sl_dist * 2.5, _dp_5m)
            else:
                _sig_sl  = round(_sig_entry + _5m_sl_dist, _dp_5m)
                _sig_tp1 = round(_sig_entry - _5m_sl_dist * 1.5, _dp_5m)
                _sig_tp2 = round(_sig_entry - _5m_sl_dist * 2.5, _dp_5m)
            logger.info(
                f"[scanner] {symbol} 5M levels applied: entry={_sig_entry} sl={_sig_sl} "
                f"tp1={_sig_tp1} tp2={_sig_tp2}"
            )

        # ── DISPLACEMENT FVG ENTRY OVERRIDE (OTE midpoint) ──────────────────────
        _raw_candle_price = float(candles[0]["close"]) if candles else 0.0
        _has_displacement_fvg = displacement and is_price_in_displacement_fvg(_raw_candle_price, displacement)
        if _has_displacement_fvg and not ob and not fvg:
            _is_pts_d = symbol.upper() in ("XAUUSD", "US30", "NAS100") or symbol.upper() in YFINANCE_FUTURES_MAP
            _dp_d = 3 if _is_pts_d else 5
            _spot_off_d = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)
            # Use OTE midpoint (62-79% retracement) as entry; fall back to FVG mid (CE) if OTE absent
            _ote_entry = displacement.get('ote_mid') or displacement['fvg_mid']
            _sig_entry = round(_ote_entry + _spot_off_d, _dp_d)
            if direction == "BUY":
                _sl_dist_d = max(abs(_sig_entry - round(displacement['fvg_bottom'] + _spot_off_d, _dp_d)), _min_sl_dist(symbol))
                _sl_dist_d = min(_sl_dist_d, _max_sl_dist(symbol))
                _sig_sl  = round(_sig_entry - _sl_dist_d, _dp_d)
                _sig_tp1 = round(_sig_entry + _sl_dist_d * 1.5, _dp_d)
                _sig_tp2 = round(_sig_entry + _sl_dist_d * 2.5, _dp_d)
            else:
                _sl_dist_d = max(abs(round(displacement['fvg_top'] + _spot_off_d, _dp_d) - _sig_entry), _min_sl_dist(symbol))
                _sl_dist_d = min(_sl_dist_d, _max_sl_dist(symbol))
                _sig_sl  = round(_sig_entry + _sl_dist_d, _dp_d)
                _sig_tp1 = round(_sig_entry - _sl_dist_d * 1.5, _dp_d)
                _sig_tp2 = round(_sig_entry - _sl_dist_d * 2.5, _dp_d)
            logger.info(
                f"[displacement] {symbol} OTE entry override: entry={_sig_entry} sl={_sig_sl} tp1={_sig_tp1} "
                f"ote_zone={round(displacement.get('ote_low', 0) + _spot_off_d, _dp_d)}"
                f"-{round(displacement.get('ote_high', 0) + _spot_off_d, _dp_d)}"
            )

        # ── SWEPT-LEVEL SL ANCHOR (Gate 4) ─────────────────────────────────────
        # Anchor SL to the swept liquidity level + buffer rather than OB/FVG geometry.
        # _swept_level is already in spot price domain (offset applied in _detect_asia_sweep_or_recent).
        # For equity indices, the overnight swept level produces SLs of 200-500pts.
        # The 5M OB SL is far more appropriate for intraday index entries.
        # Skip swept-level SL override for indices — use 5M OB anchor instead.
        _is_index = symbol.upper() in ("US100", "US30", "US500", "NAS100", "SP500", "USOIL")
        if _swept_level and not _is_index:
            _is_pts_sw = symbol.upper() in ("XAUUSD", "US30", "NAS100", "US100", "US500") or symbol.upper() in YFINANCE_FUTURES_MAP
            _dp_sw = 3 if _is_pts_sw else 5
            _sw_buf = _swept_sl_buffer(symbol)
            if direction == "BUY":
                _swept_raw_sl = _swept_level - _sw_buf
                _sw_sl_dist = max(abs(_sig_entry - _swept_raw_sl), _min_sl_dist(symbol))
                _sw_sl_dist = min(_sw_sl_dist, _max_sl_dist(symbol))
                _sig_sl  = round(_sig_entry - _sw_sl_dist, _dp_sw)
                _sig_tp1 = round(_sig_entry + _sw_sl_dist * 2.0, _dp_sw)
                _sig_tp2 = round(_sig_entry + _sw_sl_dist * 3.0, _dp_sw)
            else:
                _swept_raw_sl = _swept_level + _sw_buf
                _sw_sl_dist = max(abs(_swept_raw_sl - _sig_entry), _min_sl_dist(symbol))
                _sw_sl_dist = min(_sw_sl_dist, _max_sl_dist(symbol))
                _sig_sl  = round(_sig_entry + _sw_sl_dist, _dp_sw)
                _sig_tp1 = round(_sig_entry - _sw_sl_dist * 2.0, _dp_sw)
                _sig_tp2 = round(_sig_entry - _sw_sl_dist * 3.0, _dp_sw)
            logger.info(
                f"[scanner] {symbol} swept-level SL: swept={_swept_level} buffer={_sw_buf} "
                f"sl={_sig_sl} entry={_sig_entry} sl_dist={_sw_sl_dist:.5f} tp1={_sig_tp1}"
            )

        # TP3 ordering validation — draw level must be further than TP2, not behind entry
        if _sig_tp3:
            if direction == "BUY":
                if _sig_tp3 <= _sig_entry:
                    logger.info(f"[draw] {symbol} TP3 {_sig_tp3} <= entry {_sig_entry} for BUY — dropping TP3")
                    _sig_tp3 = 0.0
                elif _sig_tp2 and _sig_tp3 <= _sig_tp2:
                    logger.info(f"[draw] {symbol} TP3 {_sig_tp3} <= TP2 {_sig_tp2} for BUY — dropping TP3")
                    _sig_tp3 = 0.0
            else:  # SELL
                if _sig_tp3 >= _sig_entry:
                    logger.info(f"[draw] {symbol} TP3 {_sig_tp3} >= entry {_sig_entry} for SELL — dropping TP3")
                    _sig_tp3 = 0.0
                elif _sig_tp2 and _sig_tp3 >= _sig_tp2:
                    logger.info(f"[draw] {symbol} TP3 {_sig_tp3} >= TP2 {_sig_tp2} for SELL — dropping TP3")
                    _sig_tp3 = 0.0

        # Entry direction validation
        if _sig_entry_m and current_price:
            _spot_entry_for_dir = float(_sig_entry_m.group(1))
            _spot_price_for_dir = float(current_price)
            _pip_size_dir = get_pip_spec(symbol.upper()).get("pip", 0.0001)
            _entry_vs_price_pips = (_spot_entry_for_dir - _spot_price_for_dir) / _pip_size_dir

            if direction == "SELL" and _spot_entry_for_dir <= _spot_price_for_dir:
                # Entry below current price for SELL — price already dropped past entry
                # If within 10 pips, switch to market execution note
                if abs(_entry_vs_price_pips) <= 25:
                    logger.info(f"[scanner] {symbol} SELL entry {_spot_entry_for_dir} slightly below price {_spot_price_for_dir} ({abs(_entry_vs_price_pips):.1f}p) — allowing as market entry")
                else:
                    logger.info(f"[scanner] {symbol} SELL entry missed — price {_spot_price_for_dir} already {abs(_entry_vs_price_pips):.1f}p below entry {_spot_entry_for_dir}")
                    return None
            if direction == "BUY" and _spot_entry_for_dir >= _spot_price_for_dir:
                # Entry above current price for BUY — price already rallied past entry
                if abs(_entry_vs_price_pips) <= 25:
                    logger.info(f"[scanner] {symbol} BUY entry {_spot_entry_for_dir} slightly above price {_spot_price_for_dir} ({abs(_entry_vs_price_pips):.1f}p) — allowing as market entry")
                else:
                    logger.info(f"[scanner] {symbol} BUY entry missed — price {_spot_price_for_dir} already {abs(_entry_vs_price_pips):.1f}p above entry {_spot_entry_for_dir}")
                    return None

        # Entry zone proximity check
        if ob:
            entry_check = ob["mid"]
        elif fvg:
            entry_check = fvg.get("mid", current_price)
        else:
            entry_check = current_price

        _spot_offset = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)
        price_for_ob_check = float(current_price) - _spot_offset if _spot_offset != 0 else float(current_price)
        entry_valid, deviation = validate_entry(symbol, entry_check, price_for_ob_check)

        if not entry_valid:
            logger.info(f"[scanner] {symbol} entry missed by {deviation} — blocking")
            return None

        # OB-based RR hard check
        _min_sl = _min_sl_dist(symbol)
        if direction == "BUY":
            _sl_rr = (round(ob["low"] - (ob["high"] - ob["low"]) * 0.1, 5) if ob and ob.get("type") == "bullish_ob"
                      else round(fvg["bottom"] - (fvg["top"] - fvg["bottom"]) * 0.5, 5) if fvg
                      else round(float(current_price) * 0.998, 5))
            _sl_dist_rr = max(abs(float(entry_check) - _sl_rr), _min_sl)
            _sl_rr   = round(float(entry_check) - _sl_dist_rr, 5)
            _tp1_rr  = round(float(entry_check) + _sl_dist_rr * 1.5, 5)
        else:
            _sl_rr = (round(ob["high"] + (ob["high"] - ob["low"]) * 0.1, 5) if ob and ob.get("type") == "bearish_ob"
                      else round(fvg["top"] + (fvg["top"] - fvg["bottom"]) * 0.5, 5) if fvg
                      else round(float(current_price) * 1.002, 5))
            _sl_dist_rr = max(abs(_sl_rr - float(entry_check)), _min_sl)
            _sl_rr   = round(float(entry_check) + _sl_dist_rr, 5)
            _tp1_rr  = round(float(entry_check) - _sl_dist_rr * 1.5, 5)

        _rr_valid, _actual_rr = validate_risk_reward(float(entry_check), _sl_rr, _tp1_rr)
        if not _rr_valid:
            logger.info(f"[scanner] {symbol} blocked — TP1 RR {_actual_rr:.2f} below minimum 1.5")
            return None

        # Correlation check
        corr_warning = ""
        if active_signals:
            corr_ok, corr_reason, corr_warning = check_pair_correlation(symbol, direction, active_signals)
            if not corr_ok:
                logger.info(f"[scanner] {symbol} correlation skip — {corr_reason}")
                return None

        # ── BUILD UNIFIED SIGNAL ────────────────────────────────────────────────
        from claude import _calculate_lot_size as _cals, _PIP_SIZE as _lots_pip_size, _DEFAULT_PIP_SIZE as _lots_default_pip
        _risk_pct = 0.75  # Always 0.75% — all gate-passing signals are equal quality
        _sl_dist_raw = abs(_sig_entry - _sig_sl) if _sig_sl else _min_sl
        _pip_size_lots = _lots_pip_size.get(symbol.upper(), _lots_default_pip)
        _sl_dist_for_lots = _sl_dist_raw / _pip_size_lots  # convert raw price distance to pips
        try:
            _lot_full = _cals(_risk_pct, _sl_dist_for_lots, symbol, current_price=float(_sig_entry))
            _lot_str = _lot_full.split(" ")[0] if _lot_full else "0.10"
        except Exception:
            _lot_str = "0.10"

        _unified_signal = format_unified_signal(
            symbol=symbol,
            direction=direction,
            entry=_sig_entry,
            sl=_sig_sl,
            tp1=_sig_tp1,
            tp2=_sig_tp2,
            ob=ob,
            fvg=fvg,
            structure=structure,
            htf_bias=htf_bias or {},
            swept_level=_swept_level,
            kill_zone_label=_kz_label,
            lot_str=_lot_str,
            gates=gates,
            gate_details=gate_details,
            entry_tf=_entry_tf,
            displacement=displacement,
            draw=draw,
            tp3=_sig_tp3 if _sig_tp3 else None,
        )
        logger.info(f"[scanner] {symbol} {direction} — all 7 gates passed, unified signal built")

        # Record signal time for dedup cooldown
        _last_signal_time[_sym_key] = _time.monotonic()
        _last_swept_level[_sym_key] = _swept_level if _swept_level else 0.0
        _last_signal_entry[_sym_key] = _sig_entry

        return {
            "symbol": symbol,
            "score": 10,  # all 7 gates passed = institutional quality
            "recommendation": "STRONG",
            "direction": direction,
            "unified_signal": _unified_signal,
            "signal_key": signal_key,
            "auto_signal": auto_signal,
            "entry_valid": entry_valid,
            "deviation": deviation,
            "correlation_warning": corr_warning,
            "bias_unknown": False,
            "ob": ob,
            "fvg": fvg,
            "structure": structure,
            "htf_bias": htf_bias or {},
            "entry":    _sig_entry,
            "sl":       _sig_sl,
            "tp1":      _sig_tp1,
            "tp2":      _sig_tp2,
            "tp3":      _sig_tp3,
            "draw":     draw,
            "risk_pct": _risk_pct,
            "swept_level": _swept_level,
            "gates": gates,
            "gate_details": gate_details,
        }

    except Exception as e:
        logger.error(f"[scanner] Error scanning {symbol}: {e}")
        return None


async def run_scan(watchlist: list, bot, user_chat_ids: list, force: bool = False):
    """
    Scan symbols in watchlist and send alerts to users.
    force=True bypasses the scan window check (for manual /scan command).

    Scans all pairs every cycle: XAUUSD first, then remaining in preferred order.
    """
    logger.info("[scanner] run_scan called — starting cycle")
    global _scan_rotation_index

    if not force and not is_scan_window():
        logger.info("[scanner] Outside scan window — skipping")
        return

    # Always seed with the full symbol list (SYMBOLS) so new pairs like US100/US30 are
    # scanned even before users add them to personal watchlists; then union with each
    # user's DB watchlist so custom additions are picked up too.
    from database import get_user_by_chat_id, get_user_watchlist as _get_user_wl
    _user_symbol_union: set = set(s.upper() for s in watchlist)
    for _cid in user_chat_ids:
        _u = get_user_by_chat_id(str(_cid))
        if _u:
            _wl_str = _get_user_wl(_u.id)
            if _wl_str:
                _user_symbol_union.update(s.strip().upper() for s in _wl_str.split(","))

    # Scan all pairs every cycle — XAUUSD always first, then remaining in preferred order
    _preferred_order = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF", "US100", "US30", "US500"]
    pairs_this_cycle = [s for s in _preferred_order if s in _user_symbol_union]
    pairs_this_cycle += [s for s in _user_symbol_union if s not in _preferred_order]

    _scan_rotation_index += 1
    cycle_num = _scan_rotation_index
    cycle_start = _time.time()

    logger.info(
        f"[scanner] Cycle {cycle_num} — scanning all {len(pairs_this_cycle)} symbols: {pairs_this_cycle}"
    )

    alerts_sent = 0
    active_signals_this_scan = []

    # Run all pairs concurrently instead of sequentially
    # Sequential: 12 pairs × 3s = 36s scan time
    # Concurrent: all 12 pairs run in parallel = ~3-5s scan time
    # This means setups are detected within 15s of forming vs 46s before
    async def _scan_one(sym):
        try:
            return sym, await scan_symbol(sym, active_signals=active_signals_this_scan)
        except Exception as e:
            logger.error(f"[run_scan] {sym} scan error: {e}")
            return sym, None

    _scan_results = await asyncio.gather(*[_scan_one(sym) for sym in pairs_this_cycle])

    for symbol, result in _scan_results:
        try:
            if result is None:
                continue
            if result:
                active_signals_this_scan.append({
                    "symbol": result.get("symbol", symbol),
                    "direction": result.get("direction", ""),
                })

                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                signal_key = result.get("signal_key", "")
                score = result.get("score", 0)
                direction = result.get("direction", "")
                symbol = result.get("symbol", "")

                for chat_id in user_chat_ids:
                    try:
                        # Filter by user watchlist
                        from database import get_user_by_chat_id, get_user_watchlist, log_trade, Trade
                        _chat_user = get_user_by_chat_id(str(chat_id))
                        if _chat_user:
                            _user_wl = get_user_watchlist(_chat_user.id)
                            if _user_wl:
                                _user_symbols = [s.strip().upper() for s in _user_wl.split(",")]
                                if symbol.upper() not in _user_symbols:
                                    logger.info(f"[scanner] Skipping {symbol} for {chat_id} — not in watchlist")
                                    continue

                        user = get_user_by_chat_id(str(chat_id))
                        if not user or not user.is_active:
                            continue

                        # ── DRAWDOWN GUARD — per-user pre-send check ───────────────────────
                        try:
                            from database import load_challenge_state
                            from drawdown_tracker import (
                                state_from_json, check_signal_allowed, DrawdownTracker
                            )
                            from prop_firm_profiles import get_profile as _get_profile_guard
                            _cs_json_guard = load_challenge_state(user.id)
                            if _cs_json_guard:
                                _state_guard = state_from_json(_cs_json_guard)
                                _profile_guard = _get_profile_guard(_state_guard.firm_code)
                                if _profile_guard:
                                    _sig_ok, _sig_reason = check_signal_allowed(_state_guard, _profile_guard)
                                    if not _sig_ok:
                                        logger.info(
                                            f"[drawdown_guard] {symbol} blocked for user {user.id}: {_sig_reason}"
                                        )
                                        await bot.send_message(
                                            chat_id=chat_id,
                                            text=f"⛔ *Signal blocked — drawdown protection*\n{_sig_reason}",
                                        )
                                        continue
                            _dt_guard = DrawdownTracker()
                            _paused, _pause_reason = _dt_guard.is_signals_paused(user.id)
                            if _paused:
                                logger.info(
                                    f"[drawdown_guard] {symbol} suppressed for user {user.id} — {_pause_reason}"
                                )
                                continue
                        except Exception as _dg_err:
                            logger.error(f"[drawdown_guard] check failed for user {user.id}: {_dg_err}")
                        # ── END DRAWDOWN GUARD ─────────────────────────────────────────────

                        # EA auto-execution — write signal file if user opted in
                        try:
                            from signal_bridge import write_signal
                            # Pass user's actual account size from their prop firm profile
                            # so lot sizing is calculated correctly per user (not hardcoded $10k)
                            try:
                                from database import load_challenge_state
                                from drawdown_tracker import state_from_json
                                from prop_firm_profiles import get_profile as _gp_lots
                                _cs_lots = load_challenge_state(user.id)
                                if _cs_lots:
                                    _st_lots = state_from_json(_cs_lots)
                                    _pf_lots = _gp_lots(_st_lots.firm_code)
                                    _acct_size = _pf_lots.account_size if _pf_lots else 10000.0
                                else:
                                    _acct_size = 10000.0
                            except Exception:
                                _acct_size = 10000.0
                            write_signal(result, user.id, account_size=_acct_size)
                        except Exception as _sb_err:
                            logger.error(f"[signal_bridge] {symbol} write failed for user {user.id}: {_sb_err}")

                        # Build minimal analysis dict for YES/NO trade handler
                        _risk_pct_val = result.get("risk_pct", 0.50)
                        _analysis = {
                            "pair":          result["symbol"],
                            "direction":     result["direction"],
                            "entry_zone":    result.get("entry", 0),
                            "stop_loss":     result.get("sl", 0),
                            "tp1":           result.get("tp1", 0),
                            "tp2":           result.get("tp2", 0),
                            "risk_percent":  _risk_pct_val,
                            "confidence":    score,
                            "grade":         "A" if score == 10 else "B",
                            "signal_source": "TNL Scanner",
                        }

                        # Store signal for YES/NO handler — do NOT log trade yet
                        # Trade is only logged when user presses YES in Telegram
                        _trade_id = None
                        from bot import last_analysis, last_trade_id
                        last_analysis[str(chat_id)] = _analysis

                        # Build per-user lot string using their actual account size
                        # Extract ALL variables from result dict — these were local to
                        # scan_symbol and are no longer in scope after asyncio.gather
                        _lot_str      = result.get("lot_str", "0.10")
                        score         = result.get("score", 10)
                        _sig_entry    = result.get("entry", 0.0)
                        _sig_sl       = result.get("sl", 0.0)
                        _sig_tp1      = result.get("tp1", 0.0)
                        _sig_tp2      = result.get("tp2", 0.0)
                        _sig_tp3      = result.get("tp3", 0.0)
                        ob            = result.get("ob")
                        fvg           = result.get("fvg")
                        structure     = result.get("structure", "ranging")
                        htf_bias      = result.get("htf_bias", {})
                        _swept_level  = result.get("swept_level")
                        gates         = result.get("gates", {})
                        gate_details  = result.get("gate_details", {})
                        displacement  = result.get("displacement")
                        draw          = result.get("draw")
                        _risk_pct     = result.get("risk_pct", 0.75)
                        _kz_label     = result.get("kz_label", "")
                        _entry_tf     = result.get("entry_tf", "15M")
                        # Correct pip size per pair for lot calculation
                        from scanner_improvements import get_pip_spec as _gps
                        _pip_spec_user = _gps(symbol.upper())
                        _lots_default_pip = _pip_spec_user.get("pip", 0.0001)
                        try:
                            from claude import _calculate_lot_size as _cals_user
                            _sl_dist_user = abs(_sig_entry - _sig_sl)
                            # For lot sizing: XAUUSD/indices use price distance directly (pts)
                            # Forex uses distance / pip_size to get pips
                            _sym_upper = symbol.upper()
                            _is_pts_pair = _sym_upper in ("XAUUSD","US100","US30","US500","USOIL","NAS100")
                            if _is_pts_pair:
                                _sl_pts_user = _sl_dist_user  # already in points
                            else:
                                _sl_pts_user = round(_sl_dist_user / _lots_default_pip, 2) if _lots_default_pip > 0 else 10
                            _lot_full_user = _cals_user(
                                _risk_pct, _sl_pts_user, symbol,
                                account_size=_acct_size,
                                current_price=float(_sig_entry)
                            )
                            _lot_str_user = _lot_full_user.split(" ")[0] if _lot_full_user else _lot_str
                        except Exception:
                            _lot_str_user = _lot_str

                        msg = format_unified_signal(
                            symbol=symbol, direction=direction,
                            entry=_sig_entry, sl=_sig_sl,
                            tp1=_sig_tp1, tp2=_sig_tp2,
                            ob=ob, fvg=fvg, structure=structure,
                            htf_bias=htf_bias or {},
                            swept_level=_swept_level,
                            kill_zone_label=_kz_label,
                            lot_str=_lot_str_user,
                            gates=gates, gate_details=gate_details,
                            entry_tf=_entry_tf,
                            displacement=displacement,
                            draw=draw,
                            tp3=_sig_tp3,
                        )

                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ YES — Execute", callback_data="trade_yes"),
                            InlineKeyboardButton("❌ NO — Skip",     callback_data="trade_no"),
                        ]])
                        await bot.send_message(
                            chat_id=chat_id,
                            text=msg,
                            reply_markup=keyboard,
                        )

                        # Lock direction for 30 min after a signal fires
                        _locked_until = _time.time() + 1800
                        _direction_lock[symbol] = {"direction": direction, "locked_until": _locked_until}
                        logger.info(f"[direction_lock] {symbol} locked {direction} for 30 min")

                        alerts_sent += 1
                    except Exception as e:
                        logger.error(f"[scanner] Failed to send alert to {chat_id}: {e}")

        except Exception as e:
            logger.error(f"[scanner] Symbol scan failed for {symbol}: {e}")

    cycle_time = _time.time() - cycle_start
    logger.info(f"[scanner] Cycle {cycle_num} completed in {cycle_time:.1f}s — {alerts_sent} alerts sent")
    return alerts_sent


def build_bias_report(symbols: list) -> str:
    """Build a formatted daily bias report for the given symbol list."""
    from scanner_improvements import get_daily_bias
    lines = ["📊 *DAILY BIAS REPORT*", "━━━━━━━━━━━━━━━━━━━━"]
    for sym in symbols:
        try:
            b = get_daily_bias(sym)
            bias = b["bias"]
            strength = b["strength"]
            reason = b["reason"]
            emoji = "🟢" if bias == "bullish" else "🔴" if bias == "bearish" else "⚪"
            lines.append(f"{emoji} *{sym}*: {bias.upper()} ({strength}) — {reason}")
        except Exception:
            lines.append(f"⚪ *{sym}*: data unavailable")
    lines += ["━━━━━━━━━━━━━━━━━━━━", "Check this every morning before trading."]
    return "\n".join(lines)


async def check_bias_shifts(symbols: list, bot, user_chat_ids: list):
    """
    Check each symbol for a daily bias shift since the last scan cycle.
    Fires a Telegram alert only when the bias direction has changed AND
    intraday_override is True (strong intraday candle confirms the shift).
    """
    from scanner_improvements import get_daily_bias, _previous_bias, _last_bias_shift

    for symbol in symbols:
        try:
            b = get_daily_bias(symbol)
            new_bias = b.get("bias", "neutral")
            intraday_override = b.get("intraday_override", False)
            intraday_move_pct = b.get("intraday_move_pct", 0.0)

            old_bias = _previous_bias.get(symbol)

            if abs(intraday_move_pct) < 0.20:
                logger.info(f"[bias_shift] {symbol} move {intraday_move_pct:.3f}% too small — ignoring")
                _previous_bias[symbol] = new_bias
                continue

            if (old_bias is not None
                    and old_bias != new_bias
                    and new_bias != "neutral"
                    and intraday_override):
                if time.time() - _last_bias_shift.get(symbol, 0) < 1800:
                    _previous_bias[symbol] = new_bias
                    continue
                msg = (
                    f"🔄 BIAS SHIFT — {symbol}\n"
                    f"{old_bias.upper()} → {new_bias.upper()}\n"
                    f"Intraday move: {intraday_move_pct:.2f}%\n"
                    f"Update your trading direction accordingly."
                )
                for chat_id in user_chat_ids:
                    try:
                        await bot.send_message(chat_id=chat_id, text=msg)
                    except Exception as _se:
                        logger.error(f"[bias_shift] Send error to {chat_id}: {_se}")
                _last_bias_shift[symbol] = time.time()
                logger.info(f"[bias_shift] {symbol}: {old_bias} → {new_bias} (intraday {intraday_move_pct:.2f}%)")

            _previous_bias[symbol] = new_bias

        except Exception as e:
            logger.error(f"[bias_shift] Error checking {symbol}: {e}")


async def start_scanner(bot, get_active_users_fn):
    """
    Main scanner loop. Runs every SCAN_INTERVAL seconds.
    Call this from main.py alongside start_bot().
    """
    logger.info("[scanner] Scanner started")
    _bias_sent_date = None  # track last date bias report was sent
    _last_cleanup_time: float = 0.0  # epoch — track hourly PENDING cleanup

    # Populate Asia levels on startup
    for sym in ['XAUUSD','EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','NZDUSD','USDCHF','US100','US30','US500']:
        try:
            await _update_asia_levels(sym)
            logger.info(f"[startup] Asia levels initialized for {sym}")
        except Exception as e:
            logger.warning(f"[startup] Asia levels failed for {sym}: {e}")

    while True:
        try:
            logger.info("[scanner] Loop iteration starting")
            interval = get_session_interval()
            logger.info(f"[scanner] Sleeping {interval}s before next scan")
            await asyncio.sleep(interval)

            # Hourly cleanup — expire stale PENDING trades and zero out exposure
            if _time.time() - _last_cleanup_time >= 3600:
                try:
                    from database import get_conn
                    with get_conn() as _conn:
                        with _conn.cursor() as _cur:
                            _cur.execute(
                                "UPDATE trades SET result='EXPIRED' "
                                "WHERE result='PENDING' AND created_at < NOW() - INTERVAL '2 hours'"
                            )
                            _cur.execute(
                                "UPDATE user_state SET live_exposure=0.0, open_trades=0 "
                                "WHERE user_id IN ("
                                "  SELECT DISTINCT user_id FROM trades WHERE result='EXPIRED'"
                                ")"
                            )
                        _conn.commit()
                    _last_cleanup_time = _time.time()
                    logger.info("[scanner] Hourly PENDING cleanup complete")
                except Exception as _ce:
                    logger.error(f"[scanner] PENDING cleanup error: {_ce}")

            if not is_scan_window():
                continue

            # Get active users and their watchlists
            users = get_active_users_fn()
            if not users:
                continue

            # Get all unique symbols across all user watchlists
            # Seed with full SYMBOLS list so all 10 pairs always get scanned
            all_symbols = set(SYMBOLS)
            user_chat_ids = []

            for user in users:
                watchlist = getattr(user, "watchlist", None)
                if watchlist:
                    symbols = [s.strip().upper() for s in watchlist.split(",")]
                else:
                    symbols = DEFAULT_WATCHLIST
                all_symbols.update(symbols)
                user_chat_ids.append(user.telegram_chat_id)

            # 5:30 AM UTC daily bias report — sent once per trading day
            _now = datetime.now(timezone.utc)
            _today = _now.date()
            if (_now.hour == 5 and _now.minute < 35 and
                    _today.weekday() < 5 and _bias_sent_date != _today):
                try:
                    _bias_symbols = list(all_symbols)[:8]  # cap at 8 to keep message readable
                    _report = build_bias_report(_bias_symbols)
                    for _cid in user_chat_ids:
                        try:
                            await bot.send_message(chat_id=_cid, text=_report, parse_mode="Markdown")
                        except Exception as _be:
                            logger.error(f"[scanner] bias report send error to {_cid}: {_be}")
                    _bias_sent_date = _today
                    logger.info(f"[scanner] Daily bias report sent to {len(user_chat_ids)} users")
                except Exception as _bre:
                    logger.error(f"[scanner] bias report build error: {_bre}")

            # ── NEWS RESUMPTION ALERT ────────────────────────────────────────
            global _news_was_blocked, _news_resume_sent
            _currently_blocked, _, _ = is_news_window()

            if _currently_blocked and not _news_was_blocked:
                _news_was_blocked = True
                _news_resume_sent = False
                logger.info("[scanner] News window opened — resumption alert armed")

            elif not _currently_blocked and _news_was_blocked and not _news_resume_sent:
                resume_msg = (
                    "✅ News window cleared — scanner resuming.\n"
                    "All pairs now active. Watch for fresh setups."
                )
                try:
                    _bias_symbols = list(all_symbols)[:8]
                    _bias_report = build_bias_report(_bias_symbols)
                    post_news_msg = f"📊 Post-news bias update:\n{_bias_report}"
                    for _cid in user_chat_ids:
                        try:
                            await bot.send_message(chat_id=_cid, text=resume_msg)
                            await bot.send_message(chat_id=_cid, text=post_news_msg, parse_mode="Markdown")
                        except Exception as _se:
                            logger.error(f"[scanner] news resume send error to {_cid}: {_se}")
                    logger.info(f"[scanner] News resumption alert sent to {len(user_chat_ids)} users")
                except Exception as _re:
                    logger.error(f"[scanner] news resumption alert error: {_re}")
                _news_resume_sent = True
                _news_was_blocked = False

            await run_scan(list(all_symbols), bot, user_chat_ids)

            # Bias shift monitor — runs every cycle alongside regular scanning
            try:
                await check_bias_shifts(list(all_symbols), bot, user_chat_ids)
            except Exception as _bs_err:
                logger.error(f"[scanner] bias shift check error: {_bs_err}")

            # Check active monitored trades after each scan cycle
            try:
                from trade_monitor import trade_monitor
                await trade_monitor.check_all_trades(bot)
            except Exception as _tm_err:
                logger.error(f"[scanner] trade_monitor check error: {_tm_err}")

        except Exception as e:
            logger.error(f"[scanner] Loop error: {e}", exc_info=True)
            await asyncio.sleep(60)
