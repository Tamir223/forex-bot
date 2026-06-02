"""
scanner.py — Autonomous Market Scanner
TNL Trader — Phase 3

Scans watchlist instruments every 15 minutes during trading hours.
Detects SMC setups: order blocks, FVGs, structure breaks.
Sends proactive alerts to subscribed users via Telegram.
"""

import logging
import asyncio
from datetime import datetime, timezone
from market import get_live_price, get_atr, normalize_symbol
from config import TWELVE_DATA_API_KEY
from scanner_improvements import (
    is_news_window, get_session_score_bonus, validate_entry,
    check_1h_candle_confirmation, check_consecutive_losses,
    get_loss_warning_message, run_pre_scan_checks,
    detect_liquidity_sweep, detect_rejection_candle, is_ranging_market,
    get_previous_day_levels, is_optimal_time_for_pair, validate_risk_reward,
    check_pair_correlation, detect_mtf_ob_confluence, detect_momentum,
    check_daily_bias_alignment
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

# Rotation index for spreading Twelve Data API calls across scan cycles
_scan_rotation_index = 0

# Tracks last known bias per symbol for shift detection
_last_bias: dict = {}


def _cache_signal(signal_text: str) -> str:
    """Store a signal and return its short cache key."""
    key = _uuid.uuid4().hex[:12]
    AUTO_SIGNAL_CACHE[key] = {"signal": signal_text, "timestamp": datetime.now(timezone.utc)}
    # Keep cache bounded
    if len(AUTO_SIGNAL_CACHE) > MAX_CACHE_SIZE:
        oldest = next(iter(AUTO_SIGNAL_CACHE))
        del AUTO_SIGNAL_CACHE[oldest]
    return key


def get_cached_signal(key: str) -> str | None:
    """Retrieve a cached signal by key. Returns None if missing or older than 10 minutes."""
    entry = AUTO_SIGNAL_CACHE.get(key)
    if entry is None:
        return None
    age = datetime.now(timezone.utc) - entry["timestamp"]
    if age.total_seconds() > 600:
        del AUTO_SIGNAL_CACHE[key]
        return None
    return entry["signal"]

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
    London open (7-10 UTC) and NY open (13-16 UTC) = every 5 min.
    All other hours = every 15 min.
    """
    hour = datetime.now(timezone.utc).hour
    london_open = 7 <= hour <= 10
    ny_open = 13 <= hour <= 16
    if london_open or ny_open:
        return 300   # 5 minutes
    return 900       # 15 minutes


def get_current_session() -> str:
    """Return the name of the current trading session."""
    hour = datetime.now(timezone.utc).hour
    if 7 <= hour <= 10:
        return "London Open"
    elif 10 <= hour <= 12:
        return "London"
    elif 13 <= hour <= 16:
        return "NY Open"
    elif 16 <= hour <= 21:
        return "NY"
    elif 0 <= hour <= 3:
        return "Asian"
    return "Off-Session"


def get_candles_yfinance(symbol: str, outputsize: int = 50) -> list | None:
    """Fetch futures candles from yFinance (free, no rate limit)."""
    try:
        ticker = YFINANCE_FUTURES_MAP.get(symbol.upper())
        if not ticker:
            return None
        hist = yf.Ticker(ticker).history(period="2d", interval="15m")
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


def get_candles(symbol: str, interval: str = "15min", outputsize: int = 50) -> list | None:
    """Fetch candles — uses yFinance for futures and XAUUSD, Twelve Data for forex."""
    if symbol.upper() in YFINANCE_FUTURES_MAP:
        return get_candles_yfinance(symbol, outputsize)
    if symbol.upper() == "XAUUSD":
        # Use yFinance GC=F for gold — free, no Twelve Data credits consumed
        try:
            hist = yf.Ticker("GC=F").history(period="2d", interval="15m")
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
    if not TWELVE_DATA_API_KEY:
        return None
    td_symbol = normalize_symbol(symbol)
    if not td_symbol:
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/time_series",
            params={
                "symbol": td_symbol,
                "interval": interval,
                "outputsize": outputsize,
                "apikey": TWELVE_DATA_API_KEY,
            },
            timeout=8
        )
        data = resp.json()
        if data.get("status") == "error" or "values" not in data:
            logger.warning(f"Candle fetch failed for {symbol}: {data.get('message', 'unknown')}")
            return None
        candles = data["values"]
        result = []
        for c in candles:
            result.append({
                "datetime": c["datetime"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume", 0)),
            })
        return result
    except Exception as e:
        logger.error(f"Candle fetch error for {symbol}: {e}")
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


def detect_order_block(candles: list, trend: str) -> dict | None:
    """
    Detect the most recent order block.
    Bullish OB: last bearish candle before a strong bullish move.
    Bearish OB: last bullish candle before a strong bearish move.
    """
    if not candles or len(candles) < 5:
        return None

    try:
        if trend == "bullish":
            # Find last bearish candle before current rally
            for i in range(1, min(15, len(candles))):
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
            for i in range(1, min(15, len(candles))):
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


def detect_fvg(candles: list) -> dict | None:
    """
    Detect Fair Value Gap (imbalance between candle 1 high and candle 3 low).
    Bullish FVG: candle[2].high < candle[0].low
    Bearish FVG: candle[2].low > candle[0].high
    """
    if not candles or len(candles) < 3:
        return None

    try:
        for i in range(len(candles) - 2):
            c1 = candles[i+2]   # oldest of three
            c2 = candles[i+1]   # middle
            c3 = candles[i]     # newest

            # Bullish FVG
            if c1["high"] < c3["low"]:
                gap_size = c3["low"] - c1["high"]
                return {
                    "type": "bullish_fvg",
                    "top": round(c3["low"], 5),
                    "bottom": round(c1["high"], 5),
                    "mid": round((c3["low"] + c1["high"]) / 2, 5),
                    "size": round(gap_size, 5),
                    "datetime": c2["datetime"],
                }

            # Bearish FVG
            if c1["low"] > c3["high"]:
                gap_size = c1["low"] - c3["high"]
                return {
                    "type": "bearish_fvg",
                    "top": round(c1["low"], 5),
                    "bottom": round(c3["high"], 5),
                    "mid": round((c1["low"] + c3["high"]) / 2, 5),
                    "size": round(gap_size, 5),
                    "datetime": c2["datetime"],
                }
    except Exception as e:
        logger.error(f"FVG detection error: {e}")

    return None



def get_htf_bias(symbol: str) -> dict:
    """Get Daily, 4H, and 1H trend bias for multi-timeframe confirmation."""
    result = {"h1_trend": "unclear", "h4_trend": "unclear", "d1_trend": "unclear", "aligned": False, "bias": "unclear"}
    try:
        if symbol.upper() in YFINANCE_FUTURES_MAP:
            ticker = YFINANCE_FUTURES_MAP[symbol.upper()]
            h1 = yf.Ticker(ticker).history(period="5d", interval="1h")
            h4 = yf.Ticker(ticker).history(period="20d", interval="4h")
            d1 = yf.Ticker(ticker).history(period="60d", interval="1d")
            if h1.empty or h4.empty or d1.empty:
                return result
            h1_trend = "bullish" if h1["Close"].iloc[-1] > h1["Close"].iloc[0] else "bearish"
            h4_trend = "bullish" if h4["Close"].iloc[-1] > h4["Close"].iloc[0] else "bearish"
            d1_trend = "bullish" if d1["Close"].iloc[-1] > d1["Close"].iloc[0] else "bearish"
        else:
            if not TWELVE_DATA_API_KEY:
                return result
            td_symbol = normalize_symbol(symbol)
            if not td_symbol:
                return result
            def fetch_td(interval, outputsize=30):
                try:
                    resp = requests.get(f"{BASE_URL}/time_series",
                        params={"symbol": td_symbol, "interval": interval,
                                "outputsize": outputsize, "apikey": TWELVE_DATA_API_KEY}, timeout=8)
                    data = resp.json()
                    if "values" not in data:
                        return []
                    return [{"close": float(c["close"])} for c in data["values"]]
                except Exception:
                    return []
            h1_raw = fetch_td("1h")
            h4_raw = fetch_td("4h")
            d1_raw = fetch_td("1day", 60)
            if not h1_raw or not h4_raw or not d1_raw:
                return result
            h1_trend = "bullish" if h1_raw[0]["close"] > h1_raw[-1]["close"] else "bearish"
            h4_trend = "bullish" if h4_raw[0]["close"] > h4_raw[-1]["close"] else "bearish"
            d1_trend = "bullish" if d1_raw[0]["close"] > d1_raw[-1]["close"] else "bearish"

        result["h1_trend"] = h1_trend
        result["h4_trend"] = h4_trend
        result["d1_trend"] = d1_trend
        all_aligned = h1_trend == h4_trend == d1_trend
        result["aligned"] = all_aligned
        result["bias"] = h1_trend if all_aligned else ("mixed" if h1_trend != h4_trend else h4_trend)
    except Exception as e:
        logger.warning(f"HTF bias error for {symbol}: {e}")
    return result


def score_setup(structure: dict, ob: dict, fvg: dict, atr_data: dict, htf_bias: dict = None, candles: list = None, symbol: str = None) -> dict:
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
            _mtf_found, _mtf_desc = detect_mtf_ob_confluence(symbol, ob, _direction_str)
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
        factors.append(f"{fvg_type} FVG {fvg['bottom']} - {fvg['top']}")

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
        if 7 <= hour <= 10:
            _session = "London Open"
        elif 13 <= hour <= 16:
            _session = "NY Open"
        elif 10 <= hour <= 12:
            _session = "London"
        elif 16 <= hour <= 21:
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

        sweep_ok, swept_level = detect_liquidity_sweep(candles, direction_str)
        if sweep_ok:
            score += 2
            factors.append(f"Liquidity sweep confirmed at {swept_level} — institutional entry signal")

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

    # Daily bias alignment check
    if symbol is not None and trend in ("bullish", "bearish"):
        try:
            _trade_dir = "BUY" if trend == "bullish" else "SELL"
            _bias_ok, _bias_msg = check_daily_bias_alignment(symbol, _trade_dir)
            if not _bias_ok:
                # Confirmed conflict — penalty depends on bias strength
                from scanner_improvements import get_daily_bias as _get_db
                _db = _get_db(symbol)
                if _db.get("strength") == "strong":
                    score -= 2
                    factors.append("⚠️ Against confirmed daily bias — high risk")
                else:
                    score -= 1
                    factors.append("⚠️ Daily bias conflict — proceed with caution")
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
        # Entry: OB mid if available, else FVG top, else current price
        if ob and ob["type"] == "bullish_ob":
            entry = ob["mid"]
            sl = round(ob["low"] - (ob["high"] - ob["low"]) * 0.1, 5)
        elif fvg and fvg["type"] == "bullish_fvg":
            entry = fvg["top"]
            sl = round(fvg["bottom"] - (fvg["top"] - fvg["bottom"]) * 0.5, 5)
        else:
            entry = price
            sl = round(price * 0.998, 5) if not spec else round(price - spec["typical_sl_pts"], 2)

        sl_dist = abs(entry - sl)
        if not spec:
            if symbol.upper() in ("XAUUSD", "US30", "NAS100"):
                sl_dist = max(sl_dist, 8.0)  # min 8 points for gold
            else:
                sl_dist = max(sl_dist, 0.0015)  # min 15 pips for forex
            sl = round(entry - sl_dist, 2) if symbol.upper() in ("XAUUSD",) else round(entry - sl_dist, 5)
        tp1 = round(entry + sl_dist * 1.5, 5)
        tp2 = round(entry + sl_dist * 2.5, 5)
        tp3 = round(entry + sl_dist * 4.0, 5)

    else:  # SELL
        if ob and ob["type"] == "bearish_ob":
            entry = ob["mid"]
            sl = round(ob["high"] + (ob["high"] - ob["low"]) * 0.1, 5)
        elif fvg and fvg["type"] == "bearish_fvg":
            entry = fvg["bottom"]
            sl = round(fvg["top"] + (fvg["top"] - fvg["bottom"]) * 0.5, 5)
        else:
            entry = price
            sl = round(price * 1.002, 5) if not spec else round(price + spec["typical_sl_pts"], 2)

        sl_dist = abs(sl - entry)
        if not spec:
            if symbol.upper() in ("XAUUSD", "US30", "NAS100"):
                sl_dist = max(sl_dist, 8.0)  # min 8 points for gold
            else:
                sl_dist = max(sl_dist, 0.0015)  # min 15 pips for forex
            sl = round(entry + sl_dist, 2) if symbol.upper() in ("XAUUSD",) else round(entry + sl_dist, 5)
        tp1 = round(entry - sl_dist * 1.5, 5)
        tp2 = round(entry - sl_dist * 2.5, 5)
        tp3 = round(entry - sl_dist * 4.0, 5)

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

    # Round prices cleanly and apply spot offset for instruments where yFinance
    # returns futures prices that differ from MT5 spot price
    _spot_offset = FUTURES_SPOT_OFFSET.get(symbol.upper(), 0)
    def rp(v):
        adjusted = float(v) + _spot_offset
        if spec:
            return round(adjusted, 2)  # futures
        elif symbol.upper() in ("XAUUSD", "US30", "NAS100"):
            return round(adjusted, 2)  # gold and indices use 2dp
        else:
            return round(adjusted, 5)  # forex pairs use 5dp
    entry = rp(entry)
    sl = rp(sl)
    tp1 = rp(tp1)
    tp2 = rp(tp2)
    tp3 = rp(tp3)

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
            current_price = round(current_price, 2)
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

    if ob:
        ob_label = "🟢 Bullish OB" if ob["type"] == "bullish_ob" else "🔴 Bearish OB"
        lines.append(f"\n{ob_label}: {ob['low']} — {ob['high']} (mid: {ob['mid']})")

    if fvg:
        fvg_label = "🟢 Bullish FVG" if fvg["type"] == "bullish_fvg" else "🔴 Bearish FVG"
        lines.append(f"{fvg_label}: {fvg['bottom']} — {fvg['top']}")

    lines += [
        "",
        f"⏱ Scanned: {datetime.now(timezone.utc).strftime('%H:%M UTC')} — {get_current_session()} Session",
        f"📡 Send this signal to the bot to get a full grade and execute report.",
    ]

    return "\n".join(lines)


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
        candles = get_candles(symbol, interval="15min", outputsize=50)
        if not candles or len(candles) < 10:
            return None

        # Flag ranging candles early — penalty applied after scoring, not a hard block
        is_ranging_candles = is_ranging_market(candles)

        # Fetch price and ATR data
        if symbol.upper() in YFINANCE_FUTURES_MAP or symbol.upper() == "XAUUSD":
            yf_ticker = YFINANCE_FUTURES_MAP.get(symbol.upper(), "GC=F")
            try:
                _hist = yf.Ticker(yf_ticker).history(period="2d", interval="1h")
                if not _hist.empty and len(_hist) >= 5:
                    candles_1h = [
                        {"high": float(r["High"]), "low": float(r["Low"]),
                         "close": float(r["Close"])}
                        for _, r in _hist.iloc[::-1].iterrows()
                    ][:20]
                    # For XAUUSD use 1m candles for the most current price; fall back to 1h close
                    if symbol.upper() == "XAUUSD":
                        try:
                            _1m = yf.Ticker("GC=F").history(period="1d", interval="1m")
                            live_price = round(float(_1m["Close"].iloc[-1]), 2) if not _1m.empty else round(candles_1h[0]["close"], 2)
                        except Exception:
                            live_price = round(candles_1h[0]["close"], 2)
                        # Subtract futures basis to approximate MT5 spot price
                        live_price = round(live_price + FUTURES_SPOT_OFFSET.get(symbol.upper(), 0), 2)
                        price_data = {"price": live_price}
                    else:
                        price_data = {"price": candles_1h[0]["close"]}
                    ranges = [c["high"] - c["low"] for c in candles_1h[:14]]
                    atr_val = sum(ranges) / len(ranges)
                    thresholds = {
                        "ES": 5.0, "MES": 5.0, "NQ": 20.0, "MNQ": 20.0,
                        "CL": 0.3, "MCL": 0.3, "GC": 5.0, "MGC": 5.0,
                        "RTY": 3.0, "YM": 50.0, "XAUUSD": 3.0,
                    }
                    threshold = thresholds.get(symbol.upper(), 1.0)
                    atr_data = {"atr": atr_val, "is_low_volatility": atr_val < threshold}
                else:
                    price_data = None
                    atr_data = None
            except Exception as _yfe:
                logger.error(f"[scanner] yFinance price/ATR error for {symbol}: {_yfe}")
                price_data = None
                atr_data = None
        else:
            price_data = get_live_price(symbol)
            atr_data = get_atr(symbol)

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
        fvg = detect_fvg(candles)
        htf_bias = get_htf_bias(symbol)
        score_data = score_setup(structure, ob, fvg, atr_data, htf_bias, candles=candles, symbol=symbol)

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

        # Resolve trade direction — for ranging structure fall back to HTF bias
        direction = score_data.get("direction")
        if direction is None and htf_bias:
            htf_dir = htf_bias.get("bias", "unclear")
            if htf_dir == "bullish":
                direction = "BUY"
                score_data["direction"] = "BUY"
                score_data["factors"] = score_data.get("factors", []) + ["Direction from HTF bias (15M structure ranging)"]
            elif htf_dir == "bearish":
                direction = "SELL"
                score_data["direction"] = "SELL"
                score_data["factors"] = score_data.get("factors", []) + ["Direction from HTF bias (15M structure ranging)"]

        if not direction:
            logger.info(f"[scanner] {symbol} no actionable direction — skipping")
            return None

        # ── TIER 4: POST-SCORE BLOCKS ─────────────────────────────────────────
        # Score threshold — 9+ guarantee overrides range/session penalties
        if final_score < 7:
            logger.info(f"[scanner] {symbol} score {final_score}/10 — below threshold")
            return None

        alert_text = format_scan_alert(symbol, structure, ob, fvg, score_data, price_data, htf_bias, candles=candles)

        current_price = price_data.get("price", 0) if price_data else 0
        auto_signal = build_auto_signal(
            symbol, direction, float(current_price),
            ob, fvg, structure, score_data, htf_bias or {}
        )
        signal_key = _cache_signal(auto_signal)

        # Entry zone
        if ob:
            entry_check = ob["mid"]
        elif fvg:
            entry_check = fvg.get("mid", current_price)
        else:
            entry_check = current_price
        entry_valid, deviation = validate_entry(symbol, entry_check, float(current_price))

        # TIER 4: Entry missed — block unless score >= 9
        if not entry_valid:
            if final_score < 9:
                logger.info(f"[scanner] {symbol} entry missed by {deviation} — blocking (score {final_score})")
                return None
            logger.info(f"[scanner] {symbol} entry missed by {deviation} — score {final_score}/10 overrides entry check")

        # TIER 4: RR check — hard block regardless of score
        if direction == "BUY":
            if ob and ob.get("type") == "bullish_ob":
                _sl_rr = round(ob["low"] - (ob["high"] - ob["low"]) * 0.1, 5)
            elif fvg:
                _sl_rr = round(fvg["bottom"] - (fvg["top"] - fvg["bottom"]) * 0.5, 5)
            else:
                _sl_rr = round(float(current_price) * 0.998, 5)
            _sl_dist_rr = max(abs(float(entry_check) - _sl_rr), 0.0015)
            _tp1_rr = round(float(entry_check) + _sl_dist_rr * 1.5, 5)
        else:
            if ob and ob.get("type") == "bearish_ob":
                _sl_rr = round(ob["high"] + (ob["high"] - ob["low"]) * 0.1, 5)
            elif fvg:
                _sl_rr = round(fvg["top"] + (fvg["top"] - fvg["bottom"]) * 0.5, 5)
            else:
                _sl_rr = round(float(current_price) * 1.002, 5)
            _sl_dist_rr = max(abs(_sl_rr - float(entry_check)), 0.0015)
            _tp1_rr = round(float(entry_check) - _sl_dist_rr * 1.5, 5)

        _rr_valid, _actual_rr = validate_risk_reward(float(entry_check), _sl_rr, _tp1_rr)
        if not _rr_valid:
            logger.info(f"[scanner] {symbol} RR {_actual_rr:.1f} below minimum 1.5 — skipping")
            return None

        # 1H candle confirmation (informational)
        candles_1h = None
        try:
            if symbol.upper() in YFINANCE_FUTURES_MAP:
                ticker = YFINANCE_FUTURES_MAP[symbol.upper()]
                h1 = yf.Ticker(ticker).history(period="2d", interval="1h")
                if not h1.empty:
                    candles_1h = [{"open": float(r["Open"]), "close": float(r["Close"]),
                                   "high": float(r["High"]), "low": float(r["Low"])}
                                  for _, r in h1.iloc[::-1].iterrows()][:5]
        except Exception:
            pass

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
        signal_text = result.get("auto_signal", "")
        if not signal_text:
            return False

        # Entry validation — reject if price has moved outside tolerance since signal fired
        import re as _re
        _entry_match = _re.search(r"Entry Zone:\s*([\d.]+)", signal_text)
        if _entry_match:
            _entry_price = float(_entry_match.group(1))
            _symbol = result.get("symbol", "")
            if _symbol.upper() in YFINANCE_FUTURES_MAP:
                _candles = get_candles_yfinance(_symbol, outputsize=5)
                _live_price = float(_candles[0]["close"]) if _candles else None
                _tolerance = 12.0
            elif _symbol.upper() == "XAUUSD":
                # Use 1m GC=F candle, then subtract futures basis to match spot entry levels
                try:
                    _gc_hist = yf.Ticker("GC=F").history(period="1d", interval="1m")
                    _live_price = float(_gc_hist["Close"].iloc[-1]) if not _gc_hist.empty else None
                    if _live_price is not None:
                        _live_price = round(_live_price + FUTURES_SPOT_OFFSET.get("XAUUSD", 0), 2)
                except Exception:
                    _live_price = None
                _tolerance = 12.0
            else:
                _price_data = get_live_price(_symbol)
                _live_price = float(_price_data["price"]) if _price_data else None
                _tolerance = 0.0012
            if _live_price is not None and abs(_live_price - _entry_price) > _tolerance:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⏰ Signal expired — {_symbol} entry at {_entry_price} but price is now at {_live_price}. Entry zone missed."
                )
                return False

        from claude import analyze_signal
        from report import execute_report, blocked_report
        from database import get_user_firm, load_challenge_state
        from prop_firm_profiles import get_profile
        from drawdown_tracker import state_from_json, check_signal_allowed
        from database import get_state, log_trade_opened, log_trade
        from database import Trade

        firm_code = get_user_firm(user.id)
        profile = get_profile(firm_code)
        state = get_state(user.id)

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
        }

        analysis = analyze_signal(signal_text, state_dict)
        if not analysis:
            return False

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
            confidence=analysis.get("confidence_score", 0),
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
        logger.info(f"[auto-grade] {result['symbol']} {result['direction']} — {grade} {analysis.get('confidence_score')}/10 — sent to {chat_id}")
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

    Rate limiting strategy: yFinance pairs (XAUUSD, futures) scan every cycle.
    Twelve Data (forex) pairs rotate in groups of 2 across cycles — stays within
    the 8 credits/minute free tier limit.
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

    # yFinance pairs have no rate limit — always scan every cycle
    yfinance_pairs = [s for s in _user_symbol_union if s in YFINANCE_FUTURES_MAP or s == "XAUUSD"]
    # Twelve Data pairs rotate through in groups of 2
    td_pairs = [s for s in _user_symbol_union if s not in YFINANCE_FUTURES_MAP and s != "XAUUSD"]

    group_size = 2
    if len(td_pairs) <= group_size:
        td_group = td_pairs
    else:
        start = (_scan_rotation_index * group_size) % len(td_pairs)
        td_group = [td_pairs[(start + j) % len(td_pairs)] for j in range(group_size)]

    _scan_rotation_index += 1
    pairs_this_cycle = yfinance_pairs + td_group

    logger.info(
        f"[scanner] Cycle {_scan_rotation_index} — scanning {len(pairs_this_cycle)} symbols "
        f"(yFinance: {yfinance_pairs}, TD group: {td_group})"
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
                        # Auto-grade scores 9-10, but a 9/10 outside optimal session
                        # requires manual confirmation — only 10/10 auto-grades outside session
                        _outside_session = "Outside optimal session" in result.get("alert_text", "")
                        if score >= 9 and not (_outside_session and score < 10):
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

    logger.info(f"[scanner] Scan complete — {alerts_sent} alerts sent")
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

    while True:
        try:
            interval = get_session_interval()
            await asyncio.sleep(interval)

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
