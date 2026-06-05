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
    check_premium_discount_zone, is_kill_zone
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
# Allows one-tap grading without user typing anything
import uuid as _uuid
AUTO_SIGNAL_CACHE = {}
MAX_CACHE_SIZE = 200

# Twelve Data circuit breaker — once daily credits are exhausted, skip TD calls
# until the next UTC midnight rather than hitting the API on every scan cycle.
import time as _time
_td_credits_exhausted_until: float = 0.0  # epoch seconds


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

# Tracks last known bias per symbol for shift detection
_last_bias: dict = {}

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

BASE_URL = "https://api.twelvedata.com"

# Default watchlist — users can customize with /watch command
DEFAULT_WATCHLIST = ["XAUUSD", "EURUSD", "GBPUSD"]  # Forex default

# Scan interval in seconds
SCAN_INTERVAL = 900  # 15 minutes

# Trading hours UTC — scanner only runs during active sessions
SCANNER_START_HOUR = 7   # 7 AM UTC = London open
SCANNER_END_HOUR = 21    # 9 PM UTC = NY close


def is_scan_window() -> bool:
    hour = datetime.now(timezone.utc).hour
    day = datetime.now(timezone.utc).weekday()
    if day >= 5:
        return False
    return SCANNER_START_HOUR <= hour <= SCANNER_END_HOUR


def get_session_interval() -> int:
    """
    Return scan interval in seconds based on current session.
    Prime session 07:00-21:00 UTC (London open + NY session) = every 5 min.
    Off session 21:00-07:00 UTC = every 15 min.
    """
    hour = datetime.now(timezone.utc).hour
    if 7 <= hour < 21:
        return 300   # 5 minutes
    return 900       # 15 minutes


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


def detect_order_block(candles: list, trend: str, max_candles_back: int = None) -> dict | None:
    """
    Detect the most recent order block within max_candles_back candles.
    Bullish OB: last bearish candle before a strong bullish move.
    Bearish OB: last bullish candle before a strong bearish move.
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

    try:
        if trend == "bullish":
            # Find last bearish candle before current rally
            for i in range(1, min(max_candles_back, len(candles))):
                c = candles[i]
                if c["close"] < c["open"]:  # bearish candle
                    # Check if followed by bullish move
                    if candles[i-1]["close"] > c["high"]:
                        return {
                            "type": "bullish_ob",
                            "high": round(c["high"], 5),
                            "low": round(c["low"], 5),
                            "mid": round((c["high"] + c["low"]) / 2, 5),
                            "datetime": c["datetime"],
                            "strength": "strong" if (c["high"] - c["low"]) > (candles[0]["high"] - candles[0]["low"]) else "normal",
                        }

        elif trend == "bearish":
            # Find last bullish candle before current drop
            for i in range(1, min(max_candles_back, len(candles))):
                c = candles[i]
                if c["close"] > c["open"]:  # bullish candle
                    # Check if followed by bearish move
                    if candles[i-1]["close"] < c["low"]:
                        return {
                            "type": "bearish_ob",
                            "high": round(c["high"], 5),
                            "low": round(c["low"], 5),
                            "mid": round((c["high"] + c["low"]) / 2, 5),
                            "datetime": c["datetime"],
                            "strength": "strong" if (c["high"] - c["low"]) > (candles[0]["high"] - candles[0]["low"]) else "normal",
                        }
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


def score_setup(structure: dict, ob: dict, fvg: dict, atr_data: dict, htf_bias: dict = None, candles: list = None, symbol: str = None, candles_1h: list = None, candles_4h: list = None, daily_bias: dict = None) -> dict:
    """
    Score the overall setup quality. Returns score 0-10 and recommendation.
    """
    score = 0
    factors = []

    trend = structure.get("trend", "unclear")

    if trend in ("bullish", "bearish"):
        score += 2
        factors.append(f"Clear {trend} trend")

    if structure.get("bos"):
        score += 2
        factors.append("Break of structure confirmed")

    if structure.get("choch"):
        score += 1
        factors.append("Change of character detected")

    if ob:
        score += 2
        ob_type = "Bullish" if ob["type"] == "bullish_ob" else "Bearish"
        factors.append(f"{ob_type} order block at {ob['mid']}")

        # MTF OB confluence — check 1H and 4H alignment with the 15M OB
        if symbol is not None:
            _direction_str = "BUY" if trend == "bullish" else "SELL"
            _mtf_found, _mtf_desc = detect_mtf_ob_confluence(
                symbol, ob, _direction_str, candles_1h=candles_1h, candles_4h=candles_4h
            )
            if _mtf_found:
                if "Triple" in _mtf_desc:
                    score += 3
                elif "4H" in _mtf_desc:
                    score += 2
                else:
                    score += 1
                factors.append(_mtf_desc)

    if fvg:
        score += 2
        fvg_type = "Bullish" if fvg["type"] == "bullish_fvg" else "Bearish"
        _fvg_disp_bot = fvg.get("display_bottom", fvg["bottom"])
        _fvg_disp_top = fvg.get("display_top", fvg["top"])
        factors.append(f"{fvg_type} FVG {_fvg_disp_bot} - {_fvg_disp_top}")

    if atr_data and not atr_data.get("is_low_volatility"):
        score += 1
        factors.append("Healthy volatility")

    # HTF confluence — biggest edge multiplier
    if htf_bias:
        h1 = htf_bias.get("h1_trend", "unclear")
        h4 = htf_bias.get("h4_trend", "unclear")
        d1 = htf_bias.get("d1_trend", "unclear")
        bias = htf_bias.get("bias", "unclear")
        aligned = htf_bias.get("aligned", False)
        if aligned and bias == trend and d1 == trend:
            score += 4
            factors.append(f"STRONG HTF alignment — Daily, 4H, 1H all {bias}")
        elif aligned and bias == trend:
            score += 3
            factors.append(f"HTF aligned — 1H and 4H both {bias}")
        elif d1 == trend and h4 == trend:
            score += 3
            factors.append(f"Daily and 4H both {trend}")
        elif h4 == trend:
            score += 2
            factors.append(f"4H bias {h4} aligns with setup")
        elif d1 == trend:
            score += 2
            factors.append(f"Daily bias {d1} aligns with setup")
        elif h1 == trend:
            score += 1
            factors.append(f"1H bias {h1} aligns with setup")
        elif bias == "mixed":
            score -= 1
            factors.append("HTF mixed — timeframes disagreeing")

    # Direction alignment
    ob_aligned = ob and (
        (ob["type"] == "bullish_ob" and trend == "bullish") or
        (ob["type"] == "bearish_ob" and trend == "bearish")
    )
    fvg_aligned = fvg and (
        (fvg["type"] == "bullish_fvg" and trend == "bullish") or
        (fvg["type"] == "bearish_fvg" and trend == "bearish")
    )

    if ob_aligned and fvg_aligned:
        score += 1
        factors.append("OB and FVG both aligned with trend")

    # Session quality bonus
    try:
        from scanner_improvements import get_session_score_bonus
        hour = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).hour
        if 7 <= hour < 10:
            _session = "London Open"
        elif 10 <= hour < 13:
            _session = "London"
        elif 13 <= hour < 16:
            _session = "NY Open"
        elif 16 <= hour < 21:
            _session = "NY"
        else:
            _session = "Asian"
        score += get_session_score_bonus(_session)
    except Exception:
        pass
    # Candle-based confluence — liquidity sweep, rejection, previous day levels
    if candles is not None:
        direction_str = "BUY" if trend == "bullish" else "SELL"
        ob_mid = ob["mid"] if ob else (candles[0]["close"] if candles else 0.0)

        _factor_offset = FUTURES_SPOT_OFFSET.get(symbol.upper() if symbol else "", 0)

        sweep_ok, swept_level = detect_liquidity_sweep(candles, direction_str)
        if sweep_ok:
            score += 2
            _swept_disp = round(swept_level + _factor_offset, 3) if _factor_offset else swept_level
            factors.append(f"Liquidity sweep confirmed at {_swept_disp} — institutional entry signal")

        rej_ok, candle_type = detect_rejection_candle(candles, direction_str, ob_mid)
        if rej_ok:
            score += 2
            factors.append(f"Rejection candle at OB zone: {candle_type}")

        pd_levels = get_previous_day_levels(candles)
        pdh = pd_levels.get("pdh")
        pdl = pd_levels.get("pdl")
        current_p = pd_levels.get("current_price")
        if pdh and pdl and current_p:
            if direction_str == "BUY" and current_p > pdh:
                score += 1
                factors.append("Breaking previous day high — strong breakout confirmation")
            elif direction_str == "SELL" and current_p < pdl:
                score += 1
                factors.append("Breaking previous day low — strong breakout confirmation")

        mom_ok, mom_desc = detect_momentum(candles, direction_str)
        if mom_ok:
            score += 2 if "Strong" in mom_desc else 1
            factors.append(mom_desc)

        # Equal highs/lows — liquidity pools at institutional levels
        eq_ok, eq_desc = detect_equal_highs_lows(candles, direction_str, symbol or "")
        if eq_ok:
            score += 2
            factors.append(eq_desc)

        # Market structure shift — full reversal confirmation
        mss_ok, mss_desc = detect_market_structure_shift(candles, direction_str)
        if mss_ok:
            score += 2
            factors.append(mss_desc)
        elif mss_desc:
            score = max(0, score - 2)
            factors.append(f"⚠️ {mss_desc}")

        # Premium/discount zone filter
        _entry_px = candles[0]["close"]
        pd_ok, pd_desc = check_premium_discount_zone(candles, _entry_px, direction_str)
        if pd_desc:
            if pd_ok:
                score += 1
            else:
                score = max(0, score - 1)
            factors.append(pd_desc)

    # Kill zone timing bonus — peak institutional activity windows
    _kz_ok, _kz_desc = is_kill_zone(symbol or "")
    if _kz_ok:
        score += 1
        factors.append(_kz_desc)

    # Daily bias alignment check — uses pre-fetched daily_bias if provided (no extra API call)
    _bias_unknown = False
    if symbol is not None and trend in ("bullish", "bearish"):
        try:
            if daily_bias is None:
                from scanner_improvements import get_daily_bias as _get_db
                daily_bias = _get_db(symbol)
            _bias_unknown = (daily_bias.get("bias") == "unknown")
            _trade_dir = "BUY" if trend == "bullish" else "SELL"
            _bias_ok, _bias_msg = check_daily_bias_alignment(symbol, _trade_dir, _prefetched=daily_bias)
            if not _bias_ok:
                if daily_bias.get("strength") == "strong":
                    score -= 2
                else:
                    score -= 1
                factors.append(_bias_msg or "⚠️ Daily bias conflict — proceed with caution")
            elif _bias_msg:
                score += 1
                factors.append(_bias_msg)
        except Exception:
            pass

    recommendation = "STRONG" if score >= 8 else "MODERATE" if score >= 5 else "WEAK"

    return {
        "score": min(score, 10),
        "recommendation": recommendation,
        "factors": factors,
        "direction": "BUY" if trend == "bullish" else "SELL" if trend == "bearish" else None,
        "bias_unknown": _bias_unknown,
    }


def build_auto_signal(symbol: str, direction: str, price: float,
                      ob: dict, fvg: dict, structure: dict,
                      score_data: dict, htf_bias: dict) -> str:
    """
    Auto-build a complete formatted signal from scanner data.
    This is what gets sent to the grader when user taps Grade button.
    """
    trend = structure.get("trend", "bullish")
    score = score_data.get("score", 0)
    factors = score_data.get("factors", [])

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

    factor_str = "; ".join(factors[:3]) if factors else "confluence confirmed"
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
        f"Confirmation: Yes — {score}/10 score, {htf_str}, {factor_str}"
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
        f"📈 Trend: {trend.capitalize()}",
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


def check_signal_contradictions(direction: str, factors: list) -> tuple[int, list]:
    contradictions = 0
    warnings = []

    factor_text = ' '.join(factors).lower()

    if direction == 'BUY':
        if 'bearish fvg' in factor_text:
            contradictions += 1
            warnings.append('⚠️ Bearish FVG against BUY direction')
        if 'bearish engulfing' in factor_text:
            contradictions += 1
            warnings.append('⚠️ Bearish engulfing candle against BUY direction')
        if 'shooting star' in factor_text:
            contradictions += 1
            warnings.append('⚠️ Shooting star against BUY direction')
        if 'consecutive bearish' in factor_text:
            contradictions += 1
            warnings.append('⚠️ Bearish momentum against BUY direction')
        if 'selling at premium' in factor_text:
            contradictions += 1
            warnings.append('⚠️ Selling in premium zone against BUY')

    if direction == 'SELL':
        if 'bullish fvg' in factor_text:
            contradictions += 1
            warnings.append('⚠️ Bullish FVG against SELL direction')
        if 'bullish engulfing' in factor_text or 'hammer' in factor_text:
            contradictions += 1
            warnings.append('⚠️ Bullish rejection candle against SELL direction')
        if 'consecutive bullish' in factor_text:
            contradictions += 1
            warnings.append('⚠️ Bullish momentum against SELL direction')
        if 'buying in discount' in factor_text:
            contradictions += 1
            warnings.append('⚠️ Buying in discount zone against SELL')

    return contradictions, warnings


async def scan_symbol(symbol: str, active_signals: list = None) -> dict | None:
    """
    Run full scan on one symbol. Returns alert dict if setup found, None otherwise.

    Tiered validation:
      TIER 1 — Hard blocks (news, low volatility, market closed)
      TIER 2 — Score penalties (ranging, off-session, bias conflict)
      TIER 3 — Score bonuses (applied inside score_setup)
      TIER 4 — Post-score blocks (score < 7, RR, entry missed, correlation)
      Guarantee: score >= 9 always sends regardless of range/session flags
    """
    try:
        # ── UNIFIED DATA FETCH ───────────────────────────────────────────────
        # Single call fetches all timeframes (cached 4 min) — every component
        # below reads from this bundle instead of fetching independently.
        _data = fetch_all_timeframes(symbol)
        candles = _data.get("candles_15m", [])
        if not candles or len(candles) < 10:
            return None

        # Flag ranging candles early — penalty applied after scoring, not a hard block
        is_ranging_candles = is_ranging_market(candles)

        price_data = {"price": _data["price"]} if _data.get("price") else None
        atr_data   = _data.get("atr") or None

        # ── TIER 1: HARD BLOCKS ──────────────────────────────────────────────
        news_blocked, news_reason = is_news_window()
        if news_blocked:
            logger.info(f"[scanner] {symbol} BLOCKED — {news_reason}")
            return None

        if atr_data and atr_data.get("is_low_volatility"):
            logger.info(f"[scanner] {symbol} BLOCKED — low volatility")
            return None

        # Structure and setup detection
        structure = detect_structure(candles)
        trend = structure.get("trend", "unclear")
        is_ranging_structure = (trend == "ranging")

        # Completely unclear: no data to score at all
        if trend == "unclear":
            return None

        ob = detect_order_block(candles, trend)
        fvg = detect_fvg(candles, symbol)

        # Single daily_bias fetch — reused for direction fallback, TIER 3.5 block, and score_setup
        from scanner_improvements import get_daily_bias as _get_daily_bias
        daily_bias = _get_daily_bias(symbol, candles=_data.get("candles_daily"))

        htf_bias = get_htf_bias(
            symbol,
            candles_1h=_data.get("candles_1h"),
            candles_4h=_data.get("candles_4h"),
            candles_daily=_data.get("candles_daily"),
        )
        score_data = score_setup(
            structure, ob, fvg, atr_data, htf_bias,
            candles=candles, symbol=symbol,
            candles_1h=_data.get("candles_1h"),
            candles_4h=_data.get("candles_4h"),
            daily_bias=daily_bias,
        )

        # ── TIER 2: SCORE PENALTIES ──────────────────────────────────────────
        if is_ranging_candles:
            logger.info(f"[scanner] {symbol} ranging candles — applying -1 penalty")
            score_data["score"] = max(0, score_data["score"] - 1)
            score_data["factors"] = score_data.get("factors", []) + ["Ranging candle pattern — reduced confidence"]

        if is_ranging_structure:
            logger.info(f"[scanner] {symbol} ranging structure — applying -1 penalty")
            score_data["score"] = max(0, score_data["score"] - 1)
            score_data["factors"] = score_data.get("factors", []) + ["Ranging price structure — no clear 15M trend"]

        is_optimal, optimal_reason = is_optimal_time_for_pair(symbol)
        if not is_optimal:
            logger.info(f"[scanner] {symbol} outside optimal hours — applying -1 penalty")
            score_data["score"] = max(0, score_data["score"] - 1)
            score_data["factors"] = score_data.get("factors", []) + [f"Outside optimal session — {optimal_reason}"]

        # Recalculate recommendation after all penalties
        final_score = score_data["score"]
        score_data["recommendation"] = "STRONG" if final_score >= 8 else "MODERATE" if final_score >= 5 else "WEAK"

        # Resolve trade direction — for ranging 15M fall back to daily bias
        direction = score_data.get("direction")
        if direction is None and daily_bias:
            _db = daily_bias.get("bias", "neutral")
            if _db == "bullish":
                direction = "BUY"
                score_data["direction"] = "BUY"
                score_data["factors"] = score_data.get("factors", []) + ["Direction from daily bias — bullish (15M ranging)"]
                logger.info(f"[scanner] {symbol} 15M ranging — direction BUY from daily bias")
            elif _db == "bearish":
                direction = "SELL"
                score_data["direction"] = "SELL"
                score_data["factors"] = score_data.get("factors", []) + ["Direction from daily bias — bearish (15M ranging)"]
                logger.info(f"[scanner] {symbol} 15M ranging — direction SELL from daily bias")

        if not direction:
            logger.info(f"[scanner] {symbol} no actionable direction — skipping")
            return None

        # ── TIER 3.5: HARD BIAS FILTER ────────────────────────────────────────
        # Reuses already-fetched daily_bias — no extra API call
        try:
            _bias_dir       = daily_bias.get("bias", "neutral")
            _bias_confirmed = daily_bias.get("confirmed", False)
            _bias_strength  = daily_bias.get("strength", "weak")
            _conflict = (
                (direction == "BUY"  and _bias_dir == "bearish") or
                (direction == "SELL" and _bias_dir == "bullish")
            )
            if _conflict and _bias_confirmed:
                if _bias_strength in ("moderate", "strong"):
                    logger.info(
                        f"[scanner] {symbol} {direction} blocked — confirmed {_bias_dir} bias conflict"
                    )
                    return None
                else:  # weak confirmed bias — penalty but allow if score >= 9
                    score_data["score"] = max(0, score_data["score"] - 2)
                    final_score = score_data["score"]
                    score_data["recommendation"] = (
                        "STRONG" if final_score >= 8 else
                        "MODERATE" if final_score >= 5 else "WEAK"
                    )
                    score_data["factors"] = score_data.get("factors", []) + [
                        "⚠️ Confirmed bias conflict (weak) — -2 penalty"
                    ]
        except Exception:
            pass

        # ── TIER 3.7: CONTRADICTION CHECK ────────────────────────────────────
        # FIX 3: Strip contradictory factors before checking
        if direction == 'BUY':
            score_data["factors"] = [
                f for f in score_data.get("factors", [])
                if 'bearish' not in f.lower() and 'selling' not in f.lower()
            ]
        elif direction == 'SELL':
            score_data["factors"] = [
                f for f in score_data.get("factors", [])
                if 'bullish' not in f.lower() and 'buying' not in f.lower()
            ]

        _contradictions, _contra_warnings = check_signal_contradictions(
            direction, score_data.get("factors", [])
        )
        if _contradictions >= 3:
            logger.info(
                f"[scanner] {symbol} signal blocked — too many contradicting factors ({_contradictions})"
            )
            return None
        if _contradictions > 0:
            score_data["score"] = max(0, score_data["score"] - _contradictions)
            final_score = score_data["score"]
            score_data["recommendation"] = (
                "STRONG" if final_score >= 8 else
                "MODERATE" if final_score >= 5 else "WEAK"
            )
            score_data["factors"] = score_data.get("factors", []) + _contra_warnings

        # ── TIER 4: POST-SCORE BLOCKS ─────────────────────────────────────────
        # Score threshold — 9+ guarantee overrides range/session penalties
        if final_score < 7:
            logger.info(f"[scanner] {symbol} score {final_score}/10 — below threshold")
            return None

        # OB proximity check — only fire if price is at or near the OB zone.
        # XAUUSD: price_data["price"] is already spot (GC=F + offset).
        # OB levels come from GC=F candles (futures domain) — convert to spot for apples-to-apples.
        if ob:
            _ob_domain_offset = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)
            _ob_low_cmp  = ob["low"]  + _ob_domain_offset
            _ob_high_cmp = ob["high"] + _ob_domain_offset
            _ob_mid_cmp  = ob["mid"]  + _ob_domain_offset
            _fallback_price = (candles[0]["close"] + _ob_domain_offset) if candles else 0
            _cur = float(price_data.get("price", 0) if price_data else _fallback_price)
            _ob_tol = 8.0 if (symbol.upper() == "XAUUSD" or symbol.upper() in YFINANCE_FUTURES_MAP) else 0.0008
            if direction == "BUY":
                _too_far = _cur < _ob_low_cmp - _ob_tol or _cur > _ob_high_cmp + (_ob_high_cmp - _ob_low_cmp) * 2
            else:
                _too_far = _cur > _ob_high_cmp + _ob_tol or _cur < _ob_low_cmp - (_ob_high_cmp - _ob_low_cmp) * 2
            if _too_far:
                logger.info(f"[scanner] {symbol} OB at {_ob_mid_cmp:.2f} (spot) but price {_cur} — too far from OB zone, skipping")
                return None

        alert_text = format_scan_alert(symbol, structure, ob, fvg, score_data, price_data, htf_bias, candles=candles)

        current_price = price_data.get("price", 0) if price_data else 0
        # OB/FVG levels are in futures domain; convert spot price back to futures
        # domain so build_auto_signal's zone comparisons and rp() offset are consistent.
        _build_cp = float(current_price) - FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)
        auto_signal = build_auto_signal(
            symbol, direction, _build_cp,
            ob, fvg, structure, score_data, htf_bias or {}
        )
        signal_key = _cache_signal(auto_signal, score=score_data.get('score'))

        # Verify RR >= 1.5 from the actual built signal prices
        import re as _re_sig
        _sig_entry_m = _re_sig.search(r"Entry Zone:\s*([\d.]+)", auto_signal)
        _sig_sl_m    = _re_sig.search(r"Stop Loss:\s*([\d.]+)", auto_signal)
        _sig_tp1_m   = _re_sig.search(r"TP1:\s*([\d.]+)", auto_signal)
        if _sig_entry_m and _sig_sl_m and _sig_tp1_m:
            _sig_entry = float(_sig_entry_m.group(1))
            _sig_sl    = float(_sig_sl_m.group(1))
            _sig_tp1   = float(_sig_tp1_m.group(1))
            _sig_rr_valid, _sig_actual_rr = validate_risk_reward(_sig_entry, _sig_sl, _sig_tp1)
            if not _sig_rr_valid:
                logger.info(f"[scanner] {symbol} blocked — TP1 RR {_sig_actual_rr:.2f} below minimum 1.5")
                return None

        # Direction validation — block structurally invalid limit orders before sending
        if _sig_entry_m and current_price:
            _spot_entry_for_dir = float(_sig_entry_m.group(1))
            _spot_price_for_dir = float(current_price)
            if direction == "SELL" and _spot_entry_for_dir <= _spot_price_for_dir:
                logger.info(f"[scanner] {symbol} SELL entry {_spot_entry_for_dir} not above price {_spot_price_for_dir} — invalid Sell Limit")
                return None
            if direction == "BUY" and _spot_entry_for_dir >= _spot_price_for_dir:
                logger.info(f"[scanner] {symbol} BUY entry {_spot_entry_for_dir} not below price {_spot_price_for_dir} — invalid Buy Limit")
                return None

        # Entry zone
        if ob:
            entry_check = ob["mid"]
        elif fvg:
            entry_check = fvg.get("mid", current_price)
        else:
            entry_check = current_price

        # OB/FVG levels are in futures domain; current_price from fetch_all_timeframes is spot.
        # Convert current_price back to futures domain so validate_entry compares like-for-like.
        _spot_offset = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)
        if _spot_offset != 0:
            price_for_ob_check = float(current_price) - _spot_offset
        else:
            price_for_ob_check = float(current_price)
        entry_valid, deviation = validate_entry(symbol, entry_check, price_for_ob_check)

        # TIER 4: Entry missed — block unless score >= 9
        if not entry_valid:
            if final_score < 9:
                logger.info(f"[scanner] {symbol} entry missed by {deviation} — blocking (score {final_score})")
                return None
            logger.info(f"[scanner] {symbol} entry missed by {deviation} — score {final_score}/10 overrides entry check")

        # TIER 4: RR check — hard block regardless of score
        _min_sl = _min_sl_dist(symbol)
        if direction == "BUY":
            if ob and ob.get("type") == "bullish_ob":
                _sl_rr = round(ob["low"] - (ob["high"] - ob["low"]) * 0.1, 5)
            elif fvg:
                _sl_rr = round(fvg["bottom"] - (fvg["top"] - fvg["bottom"]) * 0.5, 5)
            else:
                _sl_rr = round(float(current_price) * 0.998, 5)
            _sl_dist_rr = max(abs(float(entry_check) - _sl_rr), _min_sl)
            _sl_rr = round(float(entry_check) - _sl_dist_rr, 5)
            _tp1_rr = round(float(entry_check) + _sl_dist_rr * 1.5, 5)
        else:
            if ob and ob.get("type") == "bearish_ob":
                _sl_rr = round(ob["high"] + (ob["high"] - ob["low"]) * 0.1, 5)
            elif fvg:
                _sl_rr = round(fvg["top"] + (fvg["top"] - fvg["bottom"]) * 0.5, 5)
            else:
                _sl_rr = round(float(current_price) * 1.002, 5)
            _sl_dist_rr = max(abs(_sl_rr - float(entry_check)), _min_sl)
            _sl_rr = round(float(entry_check) + _sl_dist_rr, 5)
            _tp1_rr = round(float(entry_check) - _sl_dist_rr * 1.5, 5)

        _rr_valid, _actual_rr = validate_risk_reward(float(entry_check), _sl_rr, _tp1_rr)
        if not _rr_valid:
            logger.info(f"[scanner] {symbol} blocked — TP1 RR {_actual_rr:.2f} below minimum 1.5")
            return None

        # TIER 4: Correlation duplicate hard block
        corr_warning = ""
        if active_signals:
            corr_ok, corr_reason, corr_warning = check_pair_correlation(symbol, direction, active_signals)
            if not corr_ok:
                logger.info(f"[scanner] {symbol} correlation skip — {corr_reason}")
                return None
            if corr_warning:
                logger.info(f"[scanner] {symbol} correlation warning — {corr_warning}")

        return {
            "symbol": symbol,
            "score": final_score,
            "recommendation": score_data["recommendation"],
            "direction": direction,
            "alert_text": alert_text,
            "signal_key": signal_key,
            "auto_signal": auto_signal,
            "entry_valid": entry_valid,
            "deviation": deviation,
            "correlation_warning": corr_warning,
            "bias_unknown": score_data.get("bias_unknown", False),
            # Stored for fresh signal regeneration at grade time
            "ob": ob,
            "fvg": fvg,
            "structure": structure,
            "htf_bias": htf_bias or {},
            "score_data": score_data,
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

        signal_text = result.get("auto_signal", "")
        if not signal_text:
            return False

        # Entry validation — reject if price has moved outside strict tolerance since signal fired
        import re as _re
        _limit_note = None
        _symbol = result.get("symbol", "")
        _direction = result.get("direction", "")
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
            # Direction validation — block structurally invalid limit orders
            if _live_price is not None:
                if _direction == "SELL" and _entry_price <= _live_price:
                    logger.info(f"[scanner] {_symbol} SELL entry {_entry_price} not above price {_live_price} — invalid Sell Limit")
                    return False
                if _direction == "BUY" and _entry_price >= _live_price:
                    logger.info(f"[scanner] {_symbol} BUY entry {_entry_price} not below price {_live_price} — invalid Buy Limit")
                    return False

            if _live_price is not None and abs(_live_price - _entry_price) > _tolerance:
                _score = int(result.get("score", 0))
                logger.info(f"[grade_block] score={_score} type={type(_score)} direction={_direction} live={_live_price} entry={_entry_price}")
                if _score == 10:
                    if _direction == "BUY" and _live_price < _entry_price:
                        _limit_note = (
                            f"⚠️ Entry zone passed — price already moved in your direction.\n"
                            f"📌 LIMIT ORDER SUGGESTION: Set a Buy Limit at {_entry_price} if price retraces back to the OB zone.\n"
                            f"Cancel limit order before 8:00 AM EDT if unfilled."
                        )
                    elif _direction == "SELL" and _live_price > _entry_price:
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

        # Apply firm risk cap
        if profile:
            max_daily_pct = profile.max_daily_loss_pct * 100
            signal_risk = analysis.get("risk_percent", 0) or 0
            if max_daily_pct > 0 and signal_risk > (max_daily_pct / 5):
                analysis["risk_percent"] = round(max_daily_pct / 5, 2)
            priority_header = f"⚡ AUTO-GRADED — {profile.name}\n🏆 Score {result['score']}/10 — {result['recommendation']}\n"
        else:
            priority_header = f"⚡ AUTO-GRADED — Score {result['score']}/10\n"

        decision = analysis.get("decision", "").upper()
        grade = analysis.get("grade", "")

        if decision == "BLOCK" or grade in ["C", "D", "F"]:
            # Send block message silently — no need to alert for blocked auto-grades
            logger.info(f"[auto-grade] {result['symbol']} blocked — {grade}")
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
                        # Filter alerts per user watchlist
                        from database import get_user_by_chat_id, get_user_watchlist
                        _chat_user = get_user_by_chat_id(str(chat_id))
                        if _chat_user:
                            _user_wl = get_user_watchlist(_chat_user.id)
                            if _user_wl:
                                _user_symbols = [s.strip().upper() for s in _user_wl.split(",")]
                                if symbol.upper() not in _user_symbols:
                                    logger.info(f"[scanner] Skipping {symbol} for {chat_id} — not in watchlist")
                                    continue
                        # Auto-grade scores 9-10.
                        # Require 10/10 when outside optimal session OR bias data unavailable.
                        _outside_session = "Outside optimal session" in result.get("alert_text", "")
                        _bias_unknown = result.get("bias_unknown", False)
                        _needs_10 = (_outside_session or _bias_unknown) and score < 10
                        if score >= 9 and not _needs_10:
                            from database import get_user_by_chat_id
                            user = get_user_by_chat_id(str(chat_id))
                            if user and user.is_active:
                                # Send alert first so they see what was found
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=result["alert_text"] + "\n\n⚡ *Score 9+/10 — Auto-grading now...*",
                                    parse_mode="Markdown"
                                )
                                # Then immediately grade and send report
                                graded = await auto_grade_and_send(result, bot, chat_id, user)
                                if not graded:
                                    # If auto-grade failed send manual button as fallback
                                    grade_label = f"⚡ Grade This Signal ({symbol} {direction})"
                                    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(grade_label, callback_data=f"autograde_{signal_key}")]])
                                    await bot.send_message(chat_id=chat_id, text="Tap to grade manually:", reply_markup=keyboard)
                            else:
                                grade_label = f"⚡ Grade This Signal ({symbol} {direction})"
                                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(grade_label, callback_data=f"autograde_{signal_key}")]])
                                await bot.send_message(chat_id=chat_id, text=result["alert_text"], parse_mode="Markdown", reply_markup=keyboard)
                        else:
                            # Score 4-8 — show manual grade button
                            grade_label = f"⚡ Grade This Signal ({symbol} {direction})"
                            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(grade_label, callback_data=f"autograde_{signal_key}")]])
                            await bot.send_message(chat_id=chat_id, text=result["alert_text"], parse_mode="Markdown", reply_markup=keyboard)
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
    global _last_bias
    from scanner_improvements import get_daily_bias

    for symbol in symbols:
        try:
            b = get_daily_bias(symbol)
            new_bias = b.get("bias", "neutral")
            intraday_override = b.get("intraday_override", False)
            intraday_move_pct = b.get("intraday_move_pct", 0.0)

            old_bias = _last_bias.get(symbol)

            if (old_bias is not None
                    and old_bias != new_bias
                    and new_bias != "neutral"
                    and intraday_override):
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
                logger.info(f"[bias_shift] {symbol}: {old_bias} → {new_bias} (intraday {intraday_move_pct:.2f}%)")

            _last_bias[symbol] = new_bias

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
            interval = get_session_interval()
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
            logger.error(f"[scanner] Loop error: {e}")
            await asyncio.sleep(60)
