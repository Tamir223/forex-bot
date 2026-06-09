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
from market import get_live_price, get_atr, normalize_symbol
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
)
import requests
import yfinance as yf
import pandas as pd

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
}

logger = logging.getLogger(__name__)

# Cache for auto-built signals — keyed by short ID
import uuid as _uuid
AUTO_SIGNAL_CACHE = {}
MAX_CACHE_SIZE = 200

# Asia session high/low tracking per symbol (Power of 3 sweep detection)
_asia_levels: dict = {}  # {symbol: {"high": float, "low": float, "date": str}}

# Twelve Data circuit breaker — once daily credits are exhausted, skip TD calls
# until the next UTC midnight rather than hitting the API on every scan cycle.
import time as _time
_td_credits_exhausted_until: float = 0.0  # epoch seconds
_direction_lock: dict = {}  # {symbol: {"direction": str, "locked_until": float}}


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
    "XAUUSD":        12.0,   # gold needs more room
    "US30":          25.0,
    "NAS100":        20.0,
    "GBPUSD":        0.0015, # 15 pips — most volatile forex
    "EURUSD":        0.0012, # 12 pips
    "USDJPY":        0.12,   # 12 pips JPY
    "USDCAD":        0.0012, # 12 pips
    "USDCHF":        0.0010, # 10 pips
    "AUDUSD":        0.0010, # 10 pips
    "NZDUSD":        0.0010, # 10 pips
    "default_forex": 0.0010, # 10 pips default
    "jpy_forex":     0.10,   # 10 pips JPY default
    "futures":       8.0,
}

MAX_SL_DISTANCE = {
    "XAUUSD":        20.0,   # max 20 points
    "GBPUSD":        0.0020, # max 20 pips
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


def _update_asia_levels(symbol: str, candles: list) -> None:
    """Track Asia session (00:00-07:00 UTC) high/low from today's 15M candles."""
    sym = symbol.upper()
    today = datetime.now(timezone.utc).date()
    asia_candles = []
    for c in candles:
        try:
            dt = datetime.fromisoformat(str(c.get("datetime", "")))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            if dt.date() == today and 0 <= dt.hour < 7:
                asia_candles.append(c)
        except Exception:
            continue
    if asia_candles:
        _asia_levels[sym] = {
            "high": max(c["high"] for c in asia_candles),
            "low":  min(c["low"]  for c in asia_candles),
            "date": str(today),
        }


def _detect_asia_sweep_or_recent(symbol: str, candles: list, direction: str) -> tuple:
    """
    Power of 3: check for Asia range sweep, fall back to recent swing sweep.
    SELL: wick above Asia high then close below it.
    BUY:  wick below Asia low  then close above it.
    Returns (swept, swept_level).
    """
    sym = symbol.upper()
    today = datetime.now(timezone.utc).date()
    asia = _asia_levels.get(sym, {})
    if asia.get("date") == str(today) and asia.get("high") and asia.get("low"):
        asia_high = asia["high"]
        asia_low  = asia["low"]
        for c in candles[:15]:
            if direction == "SELL" and c["high"] > asia_high and c["close"] < asia_high:
                return True, round(asia_high + FUTURES_SPOT_OFFSET.get(sym, 0), 5)
            if direction == "BUY"  and c["low"]  < asia_low  and c["close"] > asia_low:
                return True, round(asia_low  + FUTURES_SPOT_OFFSET.get(sym, 0), 5)
    # Fallback to recent swing sweep
    return detect_liquidity_sweep(candles, direction, symbol)


BASE_URL = "https://api.twelvedata.com"

# Default watchlist — users can customize with /watch command
DEFAULT_WATCHLIST = ["XAUUSD", "EURUSD", "GBPUSD"]  # Forex default

# Scan interval in seconds
SCAN_INTERVAL = 900  # 15 minutes

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

    # Monday-Friday — block only dead hours 02:00-07:00 UTC
    if 2 <= hour < 7:
        return False

    return True


def get_session_interval() -> int:
    """
    Return scan interval in seconds based on current session.
    21:00-00:00 UTC — Asian open (AUDUSD, NZDUSD, USDJPY) = 1 minute
    00:00-02:00 UTC — late Asian = 2 minutes
    02:00-07:00 UTC — dead hours = 15 minutes
    07:00-21:00 UTC — London + NY = 30 seconds
    """
    hour = datetime.now(timezone.utc).hour
    if 7 <= hour < 21:
        return 30    # London + NY — fastest
    elif hour < 2:
        return 120   # late Asian — slower
    elif hour < 7:
        return 900   # dead hours — slowest
    else:
        return 60    # 21:00-00:00 Asian open — medium


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

    return {
        "trend": trend,
        "bos": bos,
        "choch": choch,
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
        _now = datetime.utcnow()
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
        return {
            "type": ob_type,
            "high": round(c["high"], 5),
            "low": round(c["low"], 5),
            "mid": _mid,
            "datetime": c["datetime"],
            "strength": "strong" if (c["high"] - c["low"]) > (candles[0]["high"] - candles[0]["low"]) else "normal",
            "displacement": round(_disp, 5),
            "ob_quality": _quality,
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
                        logger.info(
                            f"[scanner] {symbol} best OB at {ob['mid']} too far "
                            f"({dist:.5f}) — looking for fresher OB"
                        )
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
                        logger.info(
                            f"[scanner] {symbol} best OB at {ob['mid']} too far "
                            f"({dist:.5f}) — looking for fresher OB"
                        )
                        _first_ob = ob
                    else:
                        if dist <= _fresh_threshold:
                            return ob
            return None

    except Exception as e:
        logger.error(f"OB detection error: {e}")

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
        _disp_off = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0) if symbol else 0
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
                    "display_top": round(_top + _disp_off, 3),
                    "display_bottom": round(_bot + _disp_off, 3),
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
                    "display_top": round(_top + _disp_off, 3),
                    "display_bottom": round(_bot + _disp_off, 3),
                    "mid": round((_top + _bot) / 2, 5),
                    "size": round(gap_size, 5),
                    "datetime": c2["datetime"],
                }
    except Exception as e:
        logger.error(f"FVG detection error: {e}")

    return None


def fetch_all_timeframes(symbol: str) -> dict:
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

    candles_15m: list = []
    candles_1h: list  = []
    candles_4h: list  = []
    candles_daily: list = []
    price: float = 0.0
    atr_data: dict = {}

    try:
        if yf_ticker:
            tk = yf.Ticker(yf_ticker)
            candles_15m   = _to_candles(tk.history(period="10d", interval="15m"))[:200]
            candles_1h    = _to_candles(tk.history(period="14d", interval="1h"))[:100]
            candles_4h    = _to_candles(tk.history(period="30d", interval="4h"))[:60]
            candles_daily = _to_candles(tk.history(period="30d", interval="1d"))[:20]

            price = candles_15m[0]["close"] if candles_15m else 0.0

            if sym == "XAUUSD":
                try:
                    _1m = yf.Ticker("GC=F").history(period="1d", interval="1m")
                    if not _1m.empty:
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
        "candles_15m": candles_15m,
        "candles_1h": candles_1h,
        "candles_4h": candles_4h,
        "candles_daily": candles_daily,
        "atr": atr_data,
        "timestamp": datetime.utcnow(),
    }
    return bundle


def get_htf_bias(symbol: str, candles_1h: list = None, candles_4h: list = None, candles_daily: list = None) -> dict:
    """Get Daily, 4H, and 1H trend bias for multi-timeframe confirmation."""
    result = {"h1_trend": "unclear", "h4_trend": "unclear", "d1_trend": "unclear", "aligned": False, "bias": "unclear"}
    try:
        # Fast path — use pre-fetched candles (newest-first) from the unified bundle
        if candles_1h and candles_4h and candles_daily:
            h1_trend = "bullish" if candles_1h[0]["close"]    > candles_1h[-1]["close"]    else "bearish"
            h4_trend = "bullish" if candles_4h[0]["close"]    > candles_4h[-1]["close"]    else "bearish"
            d1_trend = "bullish" if candles_daily[0]["close"] > candles_daily[-1]["close"] else "bearish"
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
            h1_trend = "bullish" if h1["Close"].iloc[-1] > h1["Close"].iloc[0] else "bearish"
            h4_trend = "bullish" if h4["Close"].iloc[-1] > h4["Close"].iloc[0] else "bearish"
            d1_trend = "bullish" if d1["Close"].iloc[-1] > d1["Close"].iloc[0] else "bearish"
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
                        h1_trend = "bullish" if _h1[0] > _h1[-1] else "bearish"
                        h4_trend = "bullish" if _h4[0] > _h4[-1] else "bearish"
                        d1_trend = "bullish" if _d1[0] > _d1[-1] else "bearish"
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
                h1_trend = "bullish" if h1["Close"].iloc[-1] > h1["Close"].iloc[0] else "bearish"
                h4_trend = "bullish" if h4["Close"].iloc[-1] > h4["Close"].iloc[0] else "bearish"
                d1_trend = "bullish" if d1["Close"].iloc[-1] > d1["Close"].iloc[0] else "bearish"

        result["h1_trend"] = h1_trend
        result["h4_trend"] = h4_trend
        result["d1_trend"] = d1_trend
        all_aligned = h1_trend == h4_trend == d1_trend
        result["aligned"] = all_aligned
        result["bias"] = h1_trend if all_aligned else ("mixed" if h1_trend != h4_trend else h4_trend)
    except Exception as e:
        logger.warning(f"HTF bias error for {symbol}: {e}")
    return result


def detect_trend_strength(candles: list) -> tuple:
    """
    Count consecutive same-direction candles from most recent (newest-first list).
    Returns (count, direction) where direction is 'bullish' or 'bearish'.
    """
    if not candles:
        return 0, ""
    first = candles[0]
    if first["close"] == first["open"]:
        return 0, ""
    streak_dir = "bullish" if first["close"] > first["open"] else "bearish"
    count = 0
    for c in candles:
        if streak_dir == "bullish" and c["close"] > c["open"]:
            count += 1
        elif streak_dir == "bearish" and c["close"] < c["open"]:
            count += 1
        else:
            break
    return count, streak_dir


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


def check_tjr_gates(symbol: str, candles: list, ob: dict, fvg: dict,
                    htf_bias: dict, market_structure: str, daily_bias: dict,
                    atr_data: dict, direction: str, structure: dict, ms: dict) -> tuple:
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

    # GATE 2 — HTF bias confirmed (Daily + 4H agree with signal direction)
    _htf_dir = "bullish" if direction == "BUY" else "bearish"
    _htf_ok = (htf_bias.get("d1_trend") == _htf_dir and htf_bias.get("h4_trend") == _htf_dir)
    gates['htf_bias'] = _htf_ok
    _d1 = htf_bias.get("d1_trend", "unclear")
    _h4 = htf_bias.get("h4_trend", "unclear")
    gate_details['htf_bias'] = f"Daily/4H {_htf_dir}" if _htf_ok else f"D1={_d1} 4H={_h4} need={_htf_dir}"

    # GATE 3 — Market structure confirmed (not ranging)
    gates['structure'] = market_structure in ('uptrend', 'downtrend')
    gate_details['structure'] = (
        "Uptrend" if market_structure == "uptrend" else
        "Downtrend" if market_structure == "downtrend" else
        f"{market_structure} — not trending"
    )

    # GATE 4 — Liquidity sweep detected (Asia range or recent swing)
    _update_asia_levels(symbol, candles)
    _sweep_ok, _swept_level = _detect_asia_sweep_or_recent(symbol, candles, direction)
    gates['sweep'] = _sweep_ok
    _sym_upper = symbol.upper()
    _is_pts = _sym_upper in ("XAUUSD", "US30", "NAS100") or _sym_upper in YFINANCE_FUTURES_MAP
    _swept_dp = 3 if _is_pts else 5
    gate_details['sweep'] = (
        f"swept at {_swept_level:.{_swept_dp}f}" if (_sweep_ok and _swept_level) else
        "no liquidity sweep"
    )

    # GATE 5 — OB or FVG present within range of price
    gates['ob_fvg'] = ob is not None or fvg is not None
    _disp_off = FUTURES_SPOT_OFFSET.get(_sym_upper, 0)
    _dp = 3 if _is_pts else 5
    if ob:
        _ob_lo = round(ob['low']  + _disp_off, _dp)
        _ob_hi = round(ob['high'] + _disp_off, _dp)
        gate_details['ob_fvg'] = f"{_ob_lo}-{_ob_hi}"
    elif fvg:
        _fb = fvg.get("display_bottom", round(fvg["bottom"] + _disp_off, _dp))
        _ft = fvg.get("display_top",    round(fvg["top"]    + _disp_off, _dp))
        gate_details['ob_fvg'] = f"FVG {_fb}-{_ft}"
    else:
        gate_details['ob_fvg'] = "no OB or FVG found"

    # GATE 6 — BOS confirmed after sweep
    _bos_ok = bool(structure.get('bos') or ms.get('bos'))
    gates['bos'] = _bos_ok
    gate_details['bos'] = "confirmed" if _bos_ok else "not confirmed"

    # GATE 7 — Volatility adequate (not low vol)
    _vol_ok = not atr_data.get('is_low_volatility', False) if atr_data else True
    gates['volatility'] = _vol_ok
    gate_details['volatility'] = "healthy" if _vol_ok else "low volatility"

    all_passed = all(gates.values())
    failed = [k for k, v in gates.items() if not v]

    return all_passed, gates, gate_details, failed, _kz_label, _swept_level


def build_auto_signal(symbol: str, direction: str, price: float,
                      ob: dict, fvg: dict, structure: dict,
                      score_data: dict, htf_bias: dict) -> str:
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
        _use_3dp = spec or symbol.upper() in ("XAUUSD", "US30", "NAS100")
        sl = round(entry - sl_dist, 3) if _use_3dp else round(entry - sl_dist, 5)
        tp1 = round(entry + sl_dist * 1.5, 3 if _use_3dp else 5)
        tp2 = round(entry + sl_dist * 2.5, 3 if _use_3dp else 5)
        tp3 = round(entry + sl_dist * 4.0, 3 if _use_3dp else 5)
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
        _use_3dp = spec or symbol.upper() in ("XAUUSD", "US30", "NAS100")
        sl = round(entry + sl_dist, 3) if _use_3dp else round(entry + sl_dist, 5)
        tp1 = round(entry - sl_dist * 1.5, 3 if _use_3dp else 5)
        tp2 = round(entry - sl_dist * 2.5, 3 if _use_3dp else 5)
        tp3 = round(entry - sl_dist * 4.0, 3 if _use_3dp else 5)
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

    # Enforce minimum 1.5:1 TP1 floor before offset is applied
    _tp1_min_dist = sl_dist * 1.5
    if direction == "BUY" and (tp1 - entry) < _tp1_min_dist - 0.0001:
        tp1 = round(entry + _tp1_min_dist, 3 if _use_3dp else 5)
        logger.warning(f"[build_signal] {symbol} BUY TP1 corrected to 1.5R: tp1={tp1}")
    elif direction == "SELL" and (entry - tp1) < _tp1_min_dist - 0.0001:
        tp1 = round(entry - _tp1_min_dist, 3 if _use_3dp else 5)
        logger.warning(f"[build_signal] {symbol} SELL TP1 corrected to 1.5R: tp1={tp1}")

    # Round prices cleanly and apply spot offset for instruments where yFinance
    # returns futures prices that differ from MT5 spot price
    _spot_offset = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)
    def rp(v):
        adjusted = float(v) + _spot_offset
        if spec:
            return round(adjusted, 3)  # futures
        elif symbol.upper() in ("XAUUSD", "US30", "NAS100"):
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


def format_scan_alert(symbol: str, structure: dict, ob: dict, fvg: dict, score_data: dict, price_data: dict, htf_bias: dict = None, candles: list = None) -> str:
    """Format a scan alert for Telegram."""
    trend = structure.get("trend", "unclear")
    direction = score_data.get("direction", "")
    score = score_data.get("score", 0)
    rec = score_data.get("recommendation", "WEAK")
    factors = score_data.get("factors", [])
    current_price = price_data.get("price", "--") if price_data else "--"
    if current_price == "--" and candles:
        current_price = candles[0]["close"]
    if isinstance(current_price, float):
        # Gold and indices: 2dp; forex: 5dp
        if symbol.upper() in ("XAUUSD", "US30", "NAS100") or symbol.upper() in YFINANCE_FUTURES_MAP:
            current_price = round(current_price, 3)
        else:
            current_price = round(current_price, 5)

    rec_emoji = "🔥" if rec == "STRONG" else "⚡" if rec == "MODERATE" else "👀"
    dir_emoji = "📈" if direction == "BUY" else "📉" if direction == "SELL" else "↔️"

    lines = [
        f"{rec_emoji} *{symbol} SETUP DETECTED*",
        f"{'─' * 28}",
        f"{dir_emoji} Direction: {direction or 'Unclear'}",
        f"💰 Price: {current_price}",
        f"📊 Setup Score: {score}/10 — {rec}",
        f"📈 Trend: {'Bullish' if direction == 'BUY' else 'Bearish' if direction == 'SELL' else trend.capitalize()}",
        "",
        "✅ *Confluence Factors:*",
    ]

    for f in factors:
        lines.append(f"  • {f}")

    _disp_offset = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)

    if ob:
        ob_label = "🟢 Bullish OB" if ob["type"] == "bullish_ob" else "🔴 Bearish OB"
        _ob_low  = round(ob["low"]  + _disp_offset, 3)
        _ob_high = round(ob["high"] + _disp_offset, 3)
        _ob_mid  = round(ob["mid"]  + _disp_offset, 3)
        lines.append(f"\n{ob_label}: {_ob_low} — {_ob_high} (mid: {_ob_mid})")

    if fvg:
        _fvg_bottom = round(fvg["bottom"] + _disp_offset, 3)
        _fvg_top    = round(fvg["top"]    + _disp_offset, 3)
        _fvg_mid = (_fvg_bottom + _fvg_top) / 2
        _entry_ref = (round(ob["mid"] + _disp_offset, 3) if ob
                      else (float(current_price) if isinstance(current_price, (int, float)) else _fvg_mid))
        if direction == "BUY":
            _fvg_emoji = "🟢"
            _fvg_desc = "Bullish FVG target" if _fvg_mid > _entry_ref else "FVG support"
        elif direction == "SELL":
            _fvg_emoji = "🔴"
            _fvg_desc = "Bearish FVG target" if _fvg_mid < _entry_ref else "FVG resistance"
        else:
            _fvg_emoji = "🟢" if fvg["type"] == "bullish_fvg" else "🔴"
            _fvg_desc = "Bullish FVG" if fvg["type"] == "bullish_fvg" else "Bearish FVG"
        lines.append(f"{_fvg_emoji} {_fvg_desc}: {_fvg_bottom} — {_fvg_top}")

    lines += [
        "",
        f"⏱ Scanned: {datetime.now(timezone.utc).strftime('%H:%M UTC')} — {get_current_session()} Session",
        f"📡 Send this signal to the bot to get a full grade and execute report.",
    ]

    return "\n".join(lines)


def format_unified_signal(symbol: str, direction: str,
                          entry: float, sl: float, tp1: float, tp2: float,
                          ob: dict, fvg: dict, structure: dict, htf_bias: dict,
                          swept_level: float, is_trend_continuation: bool,
                          kill_zone_label: str, lot_str: str,
                          gates: dict = None, gate_details: dict = None) -> str:
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
    if is_trend_continuation:
        _type_str = "Trend Continuation / MARKET ORDER"
    elif ob:
        _type_str = "OB Retracement / LIMIT ORDER"
    elif fvg:
        _type_str = "FVG Fill / LIMIT ORDER"
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

    # OB / FVG condition lines
    _cond_lines = [
        f"✅ HTF: {_htf_display}",
        f"✅ Liquidity sweep at {_swept_str}",
    ]
    if ob:
        _ob_off  = FUTURES_SPOT_OFFSET.get(sym, 0)
        _ob_type = "Bullish OB" if ob["type"] == "bullish_ob" else "Bearish OB"
        _ob_lo   = round(ob["low"]  + _ob_off, _dp)
        _ob_hi   = round(ob["high"] + _ob_off, _dp)
        _cond_lines.append(f"✅ {_ob_type}: {_ob_lo}-{_ob_hi}")
    if fvg:
        _fvg_bot = fvg.get("display_bottom", round(fvg["bottom"] + FUTURES_SPOT_OFFSET.get(sym, 0), _dp))
        _fvg_top = fvg.get("display_top",    round(fvg["top"]    + FUTURES_SPOT_OFFSET.get(sym, 0), _dp))
        _cond_lines.append(f"✅ FVG: {_fvg_bot}-{_fvg_top}")
    if structure.get("bos"):
        _cond_lines.append("✅ BOS confirmed")
    _kz_short = (kill_zone_label
                 .replace("Kill zone active — ", "")
                 .replace(" — peak institutional activity", ""))
    _cond_lines.append(f"✅ Kill zone: {_kz_short}")

    # Action line
    _dir_word = "Buy" if direction == "BUY" else "Sell"
    if is_trend_continuation:
        _action = f"⚡ Market {_dir_word} NOW at {entry:.{_dp}f}"
    else:
        _action = f"🔄 Set {_dir_word} Limit at {entry:.{_dp}f}"

    # Gate checklist — show which of the 7 gates passed
    if gates and gate_details:
        _gate_lines = [
            f"✅ Kill zone: {gate_details.get('kill_zone', '')}",
            f"✅ HTF: {gate_details.get('htf_bias', '')}",
            f"✅ Structure: {gate_details.get('structure', '')}",
            f"✅ Sweep: {gate_details.get('sweep', '')}",
        ]
        if ob:
            _ob_label = "Bullish OB" if ob["type"] == "bullish_ob" else "Bearish OB"
            _gate_lines.append(f"✅ {_ob_label}: {gate_details.get('ob_fvg', '')}")
        elif fvg:
            _gate_lines.append(f"✅ FVG: {gate_details.get('ob_fvg', '')}")
        _gate_lines.append(f"✅ BOS: {gate_details.get('bos', 'confirmed')}")
        _gate_lines.append(f"✅ Volatility: {gate_details.get('volatility', 'healthy')}")
    else:
        # Fallback when no gate data available
        _gate_lines = _cond_lines

    lines = [
        DIV,
        "🏆 TNL TRADER SIGNAL",
        DIV,
        f"📊 {symbol} | {direction} | 7/7 Gates ✅",
        f"📍 Entry:    {entry:.{_dp}f}",
        f"🛑 SL:       {sl:.{_dp}f}  ({_sl_display})",
        f"🎯 TP1:      {tp1:.{_dp}f}  (1.5R)",
        f"🎯 TP2:      {tp2:.{_dp}f}  (2.5R)",
        f"📦 Lots:     {lot_str}",
        f"⚡ Type:     {_type_str}",
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
        _data = fetch_all_timeframes(symbol)
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
        logger.info(f"[scanner] {symbol} direction {_gt_direction} ({_gt_strength})")
        market_structure = ms["structure"]  # "uptrend" or "downtrend" (ranging already gated)

        price_data = {"price": _data["price"]} if _data.get("price") else None
        atr_data   = _data.get("atr") or None

        # ── TIER 1: HARD BLOCKS ──────────────────────────────────────────────
        news_blocked, news_reason = is_news_window()
        if news_blocked:
            logger.info(f"[scanner] {symbol} BLOCKED — {news_reason}")
            return None

        # Structure and setup detection
        structure = detect_structure(candles)
        trend = structure.get("trend", "unclear")
        if trend == "unclear":
            return None

        # OB detection uses direction from the primary gate (not detect_structure trend)
        _ob_trend = "bullish" if _gt_direction == "BUY" else "bearish"
        ob = detect_order_block(candles, _ob_trend, symbol=symbol)
        fvg = detect_fvg(candles, symbol)

        from scanner_improvements import get_daily_bias as _get_daily_bias
        daily_bias = _get_daily_bias(symbol, candles=_data.get("candles_daily"))

        htf_bias = get_htf_bias(
            symbol,
            candles_1h=_data.get("candles_1h"),
            candles_4h=_data.get("candles_4h"),
            candles_daily=_data.get("candles_daily"),
        )
        direction = _gt_direction

        # ── DIRECTION LOCK ──────────────────────────────────────────────────────
        _lock = _direction_lock.get(symbol)
        if _lock and _time.time() < _lock["locked_until"]:
            if direction != _lock["direction"]:
                logger.info(f"[scanner] {symbol} direction locked {_lock['direction']} — blocking {direction} signal")
                return None

        # ── 7 BINARY GATES — ALL MUST PASS ─────────────────────────────────────
        all_passed, gates, gate_details, failed_gates, _kz_label, _swept_level = check_tjr_gates(
            symbol, candles, ob, fvg, htf_bias, market_structure,
            daily_bias, atr_data, direction, structure, ms
        )

        if not all_passed:
            logger.info(f"[scanner] {symbol} — gates failed: {failed_gates} — silent skip")
            return None

        logger.info(f"[scanner] {symbol} {direction} — all 7 gates passed")

        # ── TREND CONTINUATION DETECTION ───────────────────────────────────────
        _trend_streak, _ts_dir = detect_trend_strength(candles)
        _recent10 = candles[:10]
        if direction == "BUY":
            _momentum_candles = sum(1 for c in _recent10 if c["close"] > c["open"])
        else:
            _momentum_candles = sum(1 for c in _recent10 if c["close"] < c["open"])

        _is_trend_continuation = (
            market_structure in ("uptrend", "downtrend") and
            ((_trend_streak >= 5) or (_momentum_candles >= 8))
        )

        # ── BUILD SIGNAL LEVELS ─────────────────────────────────────────────────
        current_price = price_data.get("price", 0) if price_data else 0
        _build_cp = float(current_price) - FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)

        if _is_trend_continuation:
            _atr_val = (atr_data.get("atr", 0) if atr_data else 0) or 0
            _sl_dist_tc = min(2.0 * _atr_val, _max_sl_dist(symbol)) if _atr_val > 0 else _max_sl_dist(symbol)
            _sl_dist_tc = max(_sl_dist_tc, _min_sl_dist(symbol))
            from futures_instruments import is_futures as _is_fut_tc, get_spec as _get_spec_tc
            _spec_tc = _get_spec_tc(symbol) if _is_fut_tc(symbol) else None
            _dp_tc = 3 if (_spec_tc or symbol.upper() in ("XAUUSD", "US30", "NAS100")) else 5
            _tc_entry = round(float(current_price), _dp_tc)
            if direction == "BUY":
                _tc_sl  = round(float(current_price) - _sl_dist_tc, _dp_tc)
                _tc_tp1 = round(float(current_price) + 1.5 * _sl_dist_tc, _dp_tc)
                _tc_tp2 = round(float(current_price) + 2.5 * _sl_dist_tc, _dp_tc)
                _tc_tp3 = round(float(current_price) + 4.0 * _sl_dist_tc, _dp_tc)
            else:
                _tc_sl  = round(float(current_price) + _sl_dist_tc, _dp_tc)
                _tc_tp1 = round(float(current_price) - 1.5 * _sl_dist_tc, _dp_tc)
                _tc_tp2 = round(float(current_price) - 2.5 * _sl_dist_tc, _dp_tc)
                _tc_tp3 = round(float(current_price) - 4.0 * _sl_dist_tc, _dp_tc)
            _htf_str = "HTF aligned"
            if htf_bias:
                _d1_tc = htf_bias.get("d1_trend", "")
                _h4_tc = htf_bias.get("h4_trend", "")
                _h1_tc = htf_bias.get("h1_trend", "")
                if _d1_tc and _h4_tc and _h1_tc:
                    _htf_str = f"Daily {_d1_tc}, 4H {_h4_tc}, 1H {_h1_tc}"
            auto_signal = (
                f"{symbol} {direction} SIGNAL\n"
                f"Provider: TNL Scanner\n"
                f"Timeframe: 15M\n"
                f"Setup: TREND CONTINUATION\n"
                f"Entry Zone: {_tc_entry}\n"
                f"Stop Loss: {_tc_sl}\n"
                f"TP1: {_tc_tp1}\n"
                f"TP2: {_tc_tp2}\n"
                f"TP3: {_tc_tp3}\n"
                f"Trend: {'Bullish' if direction == 'BUY' else 'Bearish'}\n"
                f"Confirmation: Yes — all 7 institutional gates passed, {_htf_str}"
            )
        else:
            auto_signal = build_auto_signal(
                symbol, direction, _build_cp,
                ob, fvg, structure, {}, htf_bias or {}
            )
            _tc_entry = _tc_sl = _tc_tp1 = _tc_tp2 = None

        signal_key = _cache_signal(auto_signal, score=None)

        # ── RR VERIFICATION ─────────────────────────────────────────────────────
        import re as _re_sig
        _sig_entry_m = _re_sig.search(r"Entry Zone:\s*([\d.]+)", auto_signal)
        _sig_sl_m    = _re_sig.search(r"Stop Loss:\s*([\d.]+)", auto_signal)
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

        # Entry direction validation (skip for market orders)
        if not _is_trend_continuation and _sig_entry_m and current_price:
            _spot_entry_for_dir = float(_sig_entry_m.group(1))
            _spot_price_for_dir = float(current_price)
            if direction == "SELL" and _spot_entry_for_dir <= _spot_price_for_dir:
                logger.info(f"[scanner] {symbol} SELL entry {_spot_entry_for_dir} not above price {_spot_price_for_dir} — invalid Sell Limit")
                return None
            if direction == "BUY" and _spot_entry_for_dir >= _spot_price_for_dir:
                logger.info(f"[scanner] {symbol} BUY entry {_spot_entry_for_dir} not below price {_spot_price_for_dir} — invalid Buy Limit")
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

        if not entry_valid and not _is_trend_continuation:
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
        from claude import RISK_TIERS as _RISK_TIERS, _calculate_lot_size as _cals
        _risk_pct = _RISK_TIERS.get(10, 0.005) * 100  # all 7 gates passed = top quality
        _sl_dist_for_lots = abs(_sig_entry - _sig_sl) if _sig_sl else _min_sl
        try:
            _lot_full = _cals(_risk_pct, _sl_dist_for_lots, symbol)
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
            is_trend_continuation=_is_trend_continuation,
            kill_zone_label=_kz_label,
            lot_str=_lot_str,
            gates=gates,
            gate_details=gate_details,
        )
        logger.info(f"[scanner] {symbol} {direction} — all gates passed, unified signal built")

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
            "is_trend_continuation": _is_trend_continuation,
            "tc_entry": _tc_entry,
            "tc_sl":    _tc_sl,
            "tc_tp1":   _tc_tp1,
            "tc_tp2":   _tc_tp2,
            "ob": ob,
            "fvg": fvg,
            "structure": structure,
            "htf_bias": htf_bias or {},
            "score_data": {"score": 10, "factors": [], "direction": direction,
                           "momentum_candles": _momentum_candles, "trend_streak": _trend_streak},
            "entry":    _sig_entry,
            "sl":       _sig_sl,
            "tp1":      _sig_tp1,
            "tp2":      _sig_tp2,
            "risk_pct": _risk_pct,
            "swept_level": _swept_level,
            "gates": gates,
            "gate_details": gate_details,
        }

    except Exception as e:
        logger.error(f"[scanner] Error scanning {symbol}: {e}")
        return None


async def auto_grade_and_send(result: dict, bot, chat_id: str, user):
    """
    Automatically grade a high-score signal and send full report.
    Called when scanner score is 9 or 10 — no user tap needed.
    """
    try:
        from drawdown_tracker import DrawdownTracker
        _dt = DrawdownTracker()
        _paused, _pause_reason = _dt.is_signals_paused(user.id)
        if _paused:
            logger.info(f"[auto-grade] {user.id} signals paused — {_pause_reason}")
            return False
        _loss_warning = _pause_reason if (not _paused and _pause_reason) else None
        if _loss_warning:
            logger.info(f"[auto-grade] {user.id} loss warning — {_loss_warning}")

        signal_text = result.get("auto_signal", "")
        if not signal_text:
            return False

        # Entry validation — reject if price has moved outside strict tolerance since signal fired
        import re as _re
        _limit_note = None
        _symbol = result.get("symbol", "")
        _direction = result.get("direction", "")
        _is_tc = result.get("is_trend_continuation", False)
        _entry_match = _re.search(r"Entry Zone:\s*([\d.]+)", signal_text)
        _live_price = None
        if _entry_match:
            _entry_price = float(_entry_match.group(1))
            if _symbol.upper() in YFINANCE_FUTURES_MAP:
                _candles = get_candles_yfinance(_symbol, outputsize=5)
                _live_price = float(_candles[0]["close"]) if _candles else None
                _tolerance = 5.0
            elif _symbol.upper() == "XAUUSD":
                # Use 1m GC=F candle, then subtract futures basis to match spot entry levels
                try:
                    _gc_hist = yf.Ticker("GC=F").history(period="1d", interval="1m")
                    _live_price = float(_gc_hist["Close"].iloc[-1]) if not _gc_hist.empty else None
                    if _live_price is not None:
                        _live_price = round(_live_price + FUTURES_SPOT_OFFSET.get("XAUUSD", 0), 3)
                except Exception:
                    _live_price = None
                _tolerance = 8.0  # gold volatility needs wider window than other instruments
            else:
                _price_data = get_live_price(_symbol)
                _live_price = float(_price_data["price"]) if _price_data else None
                _tolerance = 0.10 if "JPY" in _symbol.upper() else 0.0005
            if _live_price is not None:
                logger.info(f"[entry_check] {_symbol} live={_live_price} entry={_entry_price} diff={abs(_live_price - _entry_price):.5f} tolerance={_tolerance}")
                logger.info(f"[entry_validation] {_symbol} live_spot={_live_price} entry_spot={_entry_price} diff={abs(_live_price - _entry_price)}")
            # Direction validation — skip for TREND CONTINUATION (market order, entry = current price)
            if _live_price is not None and not _is_tc:
                if _direction == "SELL" and _entry_price <= _live_price:
                    logger.info(f"[scanner] {_symbol} SELL entry {_entry_price} not above price {_live_price} — invalid Sell Limit")
                    return False
                if _direction == "BUY" and _entry_price >= _live_price:
                    logger.info(f"[scanner] {_symbol} BUY entry {_entry_price} not below price {_live_price} — invalid Buy Limit")
                    return False

            if _live_price is not None and not _is_tc and abs(_live_price - _entry_price) > _tolerance:
                _score = int(result.get("score", 0))
                logger.info(f"[grade_block] score={_score} type={type(_score)} direction={_direction} live={_live_price} entry={_entry_price}")
                if _score == 10:
                    if _direction == "BUY" and _live_price > _entry_price:
                        _limit_note = (
                            f"⚠️ Entry zone passed — price already moved in your direction.\n"
                            f"📌 LIMIT ORDER SUGGESTION: Set a Buy Limit at {_entry_price} if price retraces back to the OB zone.\n"
                            f"Cancel limit order before 8:00 AM EDT if unfilled."
                        )
                    elif _direction == "SELL" and _live_price < _entry_price:
                        _limit_note = (
                            f"⚠️ Entry zone passed — price already moved in your direction.\n"
                            f"📌 LIMIT ORDER SUGGESTION: Set a Sell Limit at {_entry_price} if price retraces back to the OB zone.\n"
                            f"Cancel limit order before 8:00 AM EDT if unfilled."
                        )
                if _limit_note is None:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"⏰ Signal expired — {_symbol} entry at {_entry_price} but price is now at {_live_price}. Entry zone missed."
                    )
                    try:
                        from database import get_conn
                        with get_conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "UPDATE trades SET result='CANCELLED' WHERE user_id=%s AND result='PENDING' AND created_at > NOW() - INTERVAL '1 hour'",
                                    (user.id,)
                                )
                            conn.commit()
                    except Exception as _ce:
                        logger.error(f"PENDING cleanup error on signal expiry: {_ce}")
                    return False

        # Regenerate signal with fresh live price so entry reflects current OB zone position
        _ob = result.get("ob")
        _fvg = result.get("fvg")
        if _live_price is not None and (_ob or _fvg):
            try:
                # OB/FVG levels are in futures domain (from yFinance candles).
                # _live_price is already spot (offset applied above), so convert back to
                # futures domain before passing to build_auto_signal — rp() will re-apply
                # the offset and all output prices come out correctly in spot domain.
                _build_price = _live_price - FUTURES_SPOT_OFFSET.get(_symbol.upper(), 0)
                _fresh_signal = build_auto_signal(
                    _symbol, _direction, _build_price,
                    _ob, _fvg,
                    result.get("structure", {}),
                    result.get("score_data", {}),
                    result.get("htf_bias", {}),
                )
                if _fresh_signal:
                    signal_text = _fresh_signal
                    logger.info(f"[auto-grade] {_symbol} signal regenerated with build_price={_build_price} (spot={_live_price})")
            except Exception as _regen_err:
                logger.warning(f"[auto-grade] {_symbol} signal regen failed: {_regen_err} — using cached signal")

        from claude import analyze_signal
        from report import execute_report, blocked_report
        from database import get_user_firm, load_challenge_state
        from prop_firm_profiles import get_profile
        from drawdown_tracker import state_from_json, check_signal_allowed
        from database import get_state, log_trade
        from database import Trade

        firm_code = get_user_firm(user.id)
        profile = get_profile(firm_code)
        state = get_state(user.id)

        _scanner_score = int(result.get('score', 9))
        state_dict = {
            "trades_today": state.trades_today,
            "open_trades": state.open_trades,
            "live_exposure": state.live_exposure,
            "session_losses": state.session_losses,
            "weekly_losses": state.weekly_losses,
            "daily_pnl": state.daily_pnl,
            "account_size": profile.account_size if profile else 10000.0,
            "risk_percent": 1.0,
            "max_contracts": profile.max_contracts if profile else None,
            "score": _scanner_score,  # inject scanner score so analyze_signal uses it for risk tier
        }

        # For TC signals, use scanner-calculated levels directly from result dict
        _tc_sl_f = _tc_tp1_f = _tc_tp2_f = None  # floats for post-grade override
        if _is_tc and _live_price is not None:
            _tc_sl_f  = result.get("tc_sl")
            _tc_tp1_f = result.get("tc_tp1")
            _tc_tp2_f = result.get("tc_tp2")
            _tc_sl  = str(_tc_sl_f)  if _tc_sl_f  is not None else "N/A"
            _tc_tp1 = str(_tc_tp1_f) if _tc_tp1_f is not None else "N/A"
            _tc_tp2 = str(_tc_tp2_f) if _tc_tp2_f is not None else "N/A"
            # Calculate SL distance in pips/points for Groq context
            if _tc_sl_f is not None:
                _raw_dist = abs(_live_price - _tc_sl_f)
                if "JPY" in _symbol.upper():
                    _sl_pips = f"{round(_raw_dist * 100, 1)} pips"
                elif _symbol.upper() in ("XAUUSD",) or _symbol.upper() in YFINANCE_FUTURES_MAP:
                    _sl_pips = f"{round(_raw_dist, 1)} points"
                else:
                    _sl_pips = f"{round(_raw_dist * 10000, 1)} pips"
            else:
                _sl_pips = "N/A"
            _tc_mc     = result.get("score_data", {}).get("momentum_candles", 0)
            _tc_streak = result.get("score_data", {}).get("trend_streak", 0)
            _tc_trend  = "BULLISH" if _direction == "BUY" else "BEARISH"
            _tc_htf    = result.get("htf_bias", {})
            _tc_bias   = _tc_htf.get("bias", "unknown") if _tc_htf else "unknown"
            signal_text = (
                f"TREND CONTINUATION SIGNAL — MARKET ORDER\n"
                f"Pair: {_symbol}\n"
                f"Direction: {_direction}\n"
                f"Trend direction: {_tc_trend} — this is a {_direction} signal\n"
                f"DO NOT return {'bullish' if _direction == 'SELL' else 'bearish'} trend on a {_direction} signal\n"
                f"Entry: MARKET ORDER at current price {_live_price} — DO NOT recalculate entry\n"
                f"Stop Loss: {_tc_sl} ({_sl_pips}) — USE EXACTLY THIS LEVEL\n"
                f"TP1: {_tc_tp1} (1.5R) — USE EXACTLY THIS LEVEL\n"
                f"TP2: {_tc_tp2 if _tc_tp2 != 'N/A' else 'N/A'} (2.5R) — USE EXACTLY THIS LEVEL\n"
                f"Setup: TREND CONTINUATION\n"
                f"Momentum: {_tc_mc}/10 candles in signal direction, streak={_tc_streak}\n"
                f"HTF Bias: {_tc_bias}\n"
                f"Action: EXECUTE at market price NOW — do not wait for retracement\n"
                f"IMPORTANT: DO NOT recalculate SL, TP1, or TP2. Use the exact values provided above.\n"
            )
            logger.info(f"[auto-grade] {_symbol} TC signal text built for Groq — entry={_live_price} sl={_tc_sl} ({_sl_pips}) tp1={_tc_tp1}")

        analysis = analyze_signal(signal_text, state_dict, user_id=user.id)
        if not analysis:
            return False

        # Override risk tier and confidence from scanner score — single source of truth
        from claude import RISK_TIERS as _RISK_TIERS
        _scanner_risk = _RISK_TIERS.get(_scanner_score, 0.005)
        _final_risk = round(_scanner_risk * 100, 4)
        analysis['risk_percent'] = _final_risk
        analysis['confidence'] = _scanner_score  # sync confidence display with scanner score
        analysis['score'] = _scanner_score        # ensure score field matches scanner score
        result['risk_percent'] = _final_risk      # keep scan result in sync too
        logger.info(f"[risk] score={_scanner_score} risk={_final_risk}%")

        # TC signals: override Groq's levels with scanner-calculated values
        if _is_tc and _live_price is not None:
            analysis['entry_zone'] = _live_price
            if _tc_sl_f  is not None: analysis['stop_loss'] = _tc_sl_f
            if _tc_tp1_f is not None: analysis['tp1']       = _tc_tp1_f
            if _tc_tp2_f is not None: analysis['tp2']       = _tc_tp2_f
            analysis['tp1_rr'] = "1.5R"
            analysis['tp2_rr'] = "2.5R"
            logger.info(f"[auto-grade] {_symbol} TC levels locked — entry={_live_price} sl={_tc_sl_f} tp1={_tc_tp1_f} tp2={_tc_tp2_f}")

        block_risk = f"{_final_risk}%"

        # Apply firm risk cap
        if profile:
            max_daily_pct = profile.max_daily_loss_pct * 100
            signal_risk = analysis.get("risk_percent", 0) or 0
            if max_daily_pct > 0 and signal_risk > (max_daily_pct / 5):
                analysis["risk_percent"] = round(max_daily_pct / 5, 2)
            priority_header = f"⚡ AUTO-GRADED — {profile.name}\n🏆 7/7 Gates ✅ — {result['recommendation']}\n"
        else:
            priority_header = f"⚡ AUTO-GRADED — 7/7 Gates ✅\n"
        if _loss_warning:
            priority_header = f"{_loss_warning}\n\n{priority_header}"

        decision = analysis.get("decision", "").upper()
        grade = analysis.get("grade", "")

        if decision == "BLOCK" or grade in ["C", "D", "F"]:
            logger.info(f"[auto-grade] {result['symbol']} blocked — {grade}")
            if "Entry missed" in analysis.get("reason", ""):
                if _is_tc:
                    # TC signals are market orders — entry IS current price, never "missed"
                    logger.info(f"[auto-grade] {result['symbol']} TC signal — ignoring LLM entry missed block, continuing")
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"⛔ {result['symbol']} {_direction} — BLOCKED — Entry missed\n📊 Risk if valid: {block_risk}"
                    )
                    return True
            elif not _is_tc:
                return False

        report_text = priority_header + execute_report(analysis)
        if _limit_note:
            report_text += f"\n\n{_limit_note}"

        _losses_today = _dt.get_losses_today(user.id)
        if _losses_today == 1:
            report_text += "\n\n⚠️ 1 loss today — another loss pauses signals. Trade carefully."

        # Store for WIN/LOSS tracking
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        execute_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ YES — Execute", callback_data="trade_yes"),
            InlineKeyboardButton("❌ NO — Skip", callback_data="trade_no"),
        ]])

        await bot.send_message(
            chat_id=chat_id,
            text=report_text,
            parse_mode="Markdown",
            reply_markup=execute_keyboard
        )

        # Log the trade
        trade_id = log_trade(Trade(
            user_id=user.id,
            pair=analysis.get("pair", ""),
            direction=analysis.get("direction", ""),
            grade=analysis.get("grade", ""),
            confidence=analysis.get("confidence", 0),
            signal_source="TNL Scanner (Auto)",
            risk_percent=analysis.get("risk_percent", 0),
            entry_zone=str(analysis.get("entry_zone", "")),
            stop_loss=str(analysis.get("stop_loss", "")),
        ))

        # Store analysis for WIN/LOSS reply
        # Use a simple module-level dict accessible from bot.py
        from bot import last_analysis, last_trade_id
        last_analysis[chat_id] = analysis
        if trade_id:
            last_trade_id[chat_id] = trade_id

        # Don't count trade until user taps YES
        # log_trade_opened called in callback_trade_button instead
        logger.info(f"[auto-grade] {result['symbol']} {result['direction']} — {grade} {analysis.get('confidence')}/10 — sent to {chat_id}")
        return True

    except Exception as e:
        import traceback
        logger.error(f"[auto-grade] Error: {e}")
        logger.error(f"[auto-grade] Traceback: {traceback.format_exc()}")
        return False


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

    # Rebuild the symbol set from each user's actual DB watchlist so only symbols
    # explicitly added by these users drive the scan (same DB fetch as per-alert filtering)
    from database import get_user_by_chat_id, get_user_watchlist as _get_user_wl
    _user_symbol_union: set = set()
    for _cid in user_chat_ids:
        _u = get_user_by_chat_id(str(_cid))
        if _u:
            _wl_str = _get_user_wl(_u.id)
            if _wl_str:
                _user_symbol_union.update(s.strip().upper() for s in _wl_str.split(","))
    if not _user_symbol_union:
        _user_symbol_union = {s.upper() for s in watchlist}

    # Scan all pairs every cycle — XAUUSD always first, then remaining in preferred order
    _preferred_order = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF"]
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

    for i, symbol in enumerate(pairs_this_cycle):
        try:
            result = await scan_symbol(symbol, active_signals=active_signals_this_scan)
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

                        # Log trade for tracking (confirmed when user taps YES)
                        _trade_id = log_trade(Trade(
                            user_id=user.id,
                            pair=result["symbol"],
                            direction=result["direction"],
                            grade=_analysis["grade"],
                            confidence=score,
                            signal_source="TNL Scanner",
                            risk_percent=_risk_pct_val,
                            entry_zone=str(result.get("entry", "")),
                            stop_loss=str(result.get("sl", "")),
                        ))

                        from bot import last_analysis, last_trade_id
                        last_analysis[str(chat_id)] = _analysis
                        if _trade_id:
                            last_trade_id[str(chat_id)] = _trade_id

                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ YES — Execute", callback_data="trade_yes"),
                            InlineKeyboardButton("❌ NO — Skip",     callback_data="trade_no"),
                        ]])
                        await bot.send_message(
                            chat_id=chat_id,
                            text=result["unified_signal"],
                            reply_markup=keyboard,
                        )

                        # Lock direction for 30 min after a signal fires
                        _locked_until = _time.time() + 1800
                        _direction_lock[symbol] = {"direction": direction, "locked_until": _locked_until}
                        logger.info(f"[direction_lock] {symbol} locked {direction} for 30 min")

                        alerts_sent += 1
                    except Exception as e:
                        logger.error(f"[scanner] Failed to send alert to {chat_id}: {e}")

            # 2-second gap between each pair scan to avoid burst rate limiting
            if i < len(pairs_this_cycle) - 1:
                await asyncio.sleep(2)

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
    import time
    from scanner_improvements import get_daily_bias, _previous_bias, _last_bias_shift

    for symbol in symbols:
        try:
            b = get_daily_bias(symbol)
            new_bias = b.get("bias", "neutral")
            intraday_override = b.get("intraday_override", False)
            intraday_move_pct = b.get("intraday_move_pct", 0.0)

            old_bias = _previous_bias.get(symbol)

            if abs(intraday_move_pct) < 0.1:
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
            all_symbols = set()
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
            _currently_blocked, _ = is_news_window()

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
