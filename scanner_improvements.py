"""
Scanner Improvements — 6 upgrades for better market reading
1. News Filter — auto-suppress signals during red folder events
2. Session Quality Score — boost London/NY open signals
3. Entry Validation — skip auto-grade if entry already missed
4. 1H Candle Confirmation — verify direction before alerting
5. Spread Check — block signals during wide spread conditions
6. Consecutive Loss Protection — warn after 2 losses in a row
"""

import logging
import requests
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ─── PIP SPECIFICATION SYSTEM ────────────────────────────────────────────────
# Single source of truth for pip size, minimum SL distance, and ATR threshold.
# All functions that need pair-specific tolerances should call get_pip_spec().

PIP_SPECS = {
    "EURUSD": {"pip": 0.0001, "min_sl": 0.0012, "min_atr": 0.0007},  # 12 pips
    "GBPUSD": {"pip": 0.0001, "min_sl": 0.0015, "min_atr": 0.0008},  # 15 pips — most volatile forex
    "AUDUSD": {"pip": 0.0001, "min_sl": 0.0010, "min_atr": 0.0006},  # 10 pips
    "NZDUSD": {"pip": 0.0001, "min_sl": 0.0010, "min_atr": 0.0006},  # 10 pips
    "USDCAD": {"pip": 0.0001, "min_sl": 0.0012, "min_atr": 0.0007},  # 12 pips
    "USDCHF": {"pip": 0.0001, "min_sl": 0.0010, "min_atr": 0.0007},  # 10 pips
    "USDJPY": {"pip": 0.01,   "min_sl": 0.12,   "min_atr": 0.05},    # 12 pips JPY
    "EURJPY": {"pip": 0.01,   "min_sl": 0.15,   "min_atr": 0.08},
    "GBPJPY": {"pip": 0.01,   "min_sl": 0.15,   "min_atr": 0.10},
    "XAUUSD": {"pip": 0.01,   "min_sl": 12.0,   "min_atr": 3.0},     # 12 points — gold needs room
    "XAGUSD": {"pip": 0.001,  "min_sl": 0.05,   "min_atr": 0.03},
}

_FOREX_PIP_SPEC_PAIRS = {k for k, v in PIP_SPECS.items() if v["pip"] <= 0.01 and k not in ("XAUUSD", "XAGUSD")}


def get_pip_spec(symbol: str) -> dict:
    """Return pip spec for symbol, defaulting to standard 4dp forex if unknown."""
    return PIP_SPECS.get(symbol.upper(), {"pip": 0.0001, "min_sl": 0.0010, "min_atr": 0.0007})


# ─── 1. NEWS FILTER ───────────────────────────────────────────────────────────

# High impact news times UTC — updated weekly
# Format: (hour, minute, description)
HIGH_IMPACT_NEWS = [
    # Last-resort fallback only — ForexFactory live feed is the primary source.
    # Only include times that are nearly always high-impact regardless of the calendar.
    (8, 30, "US Core PCE / GDP / NFP / CPI"),
    (13, 30, "US economic data"),
    (14, 0, "US economic data — 10AM EDT"),
    (7, 0, "BOE / UK data"),
    (9, 0, "ECB / EUR data"),
]

NEWS_BLOCK_MINUTES_BEFORE = 45
NEWS_BLOCK_MINUTES_AFTER = 30

def is_news_window() -> tuple[bool, str]:
    """
    Check if current time is within a news window.
    Returns (is_blocked, reason).
    Uses ForexFactory RSS if available, falls back to hardcoded times.
    """
    now = datetime.now(timezone.utc)
    current_minutes = now.hour * 60 + now.minute

    # Try ForexFactory RSS feed first
    try:
        resp = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=5
        )
        if resp.status_code == 200:
            events = resp.json()
            today = now.strftime("%Y-%m-%d")
            high_today = []
            for event in events:
                if event.get("impact") != "High":
                    continue
                event_date = event.get("date", "")
                if today not in event_date:
                    continue
                event_time = event.get("time", "")
                if not event_time or event_time == "Tentative":
                    continue
                try:
                    from datetime import datetime as dt
                    t = dt.strptime(event_time.upper(), "%I:%M%p")
                    event_minutes = t.hour * 60 + t.minute
                    # Convert ET to UTC (+4 EDT)
                    event_minutes_utc = event_minutes + 4 * 60
                    if event_minutes_utc > 24 * 60:
                        event_minutes_utc -= 24 * 60
                    utc_h, utc_m = divmod(event_minutes_utc, 60)
                    high_today.append((utc_h, utc_m, event.get("title", "?"), event_time))

                    diff = current_minutes - event_minutes_utc
                    if -NEWS_BLOCK_MINUTES_BEFORE <= diff <= NEWS_BLOCK_MINUTES_AFTER:
                        logging.debug(
                            "[NEWS] FF today high-impact events: %s",
                            [(h, m, d, et) for h, m, d, et in high_today]
                        )
                        return True, f"High impact news: {event.get('title', 'USD event')}"
                except Exception:
                    continue
            logging.debug(
                "[NEWS] FF today high-impact events (no block): %s",
                [(h, m, d, et) for h, m, d, et in high_today]
            )
    except Exception:
        pass

    # Fall back to hardcoded news times
    for hour, minute, desc in HIGH_IMPACT_NEWS:
        event_minutes = hour * 60 + minute
        diff = current_minutes - event_minutes
        if -NEWS_BLOCK_MINUTES_BEFORE <= diff <= NEWS_BLOCK_MINUTES_AFTER:
            return True, f"News window: {desc} at {hour:02d}:{minute:02d} UTC"

    return False, ""


def get_next_news_event() -> str:
    """Return description of next news event today."""
    now = datetime.now(timezone.utc)
    current_minutes = now.hour * 60 + now.minute

    for hour, minute, desc in sorted(HIGH_IMPACT_NEWS, key=lambda x: x[0] * 60 + x[1]):
        event_minutes = hour * 60 + minute
        if event_minutes > current_minutes:
            mins_until = event_minutes - current_minutes
            return f"{desc} in {mins_until} minutes ({hour:02d}:{minute:02d} UTC)"

    return "No more events today"


def check_upcoming_news(lookahead_minutes: int = 45) -> tuple[bool, str, int]:
    """
    Check if high-impact news is within lookahead_minutes in the future.
    Returns (news_approaching, reason, minutes_until).
    Only looks at future events — does not fire for events already passed.
    """
    now = datetime.now(timezone.utc)
    current_minutes = now.hour * 60 + now.minute

    # Try ForexFactory RSS feed first
    try:
        resp = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=5
        )
        if resp.status_code == 200:
            events = resp.json()
            today = now.strftime("%Y-%m-%d")
            high_today = []
            for event in events:
                if event.get("impact") != "High":
                    continue
                event_date = event.get("date", "")
                if today not in event_date:
                    continue
                event_time = event.get("time", "")
                if not event_time or event_time == "Tentative":
                    continue
                try:
                    from datetime import datetime as dt
                    t = dt.strptime(event_time.upper(), "%I:%M%p")
                    event_minutes = t.hour * 60 + t.minute
                    event_minutes_utc = event_minutes + 4 * 60
                    if event_minutes_utc > 24 * 60:
                        event_minutes_utc -= 24 * 60
                    utc_h, utc_m = divmod(event_minutes_utc, 60)
                    minutes_until = event_minutes_utc - current_minutes
                    high_today.append((utc_h, utc_m, event.get("title", "?"), event_time, minutes_until))
                    if 0 < minutes_until <= lookahead_minutes:
                        logging.debug(
                            "[NEWS upcoming] FF today high-impact events: %s",
                            [(h, m, d, et, mu) for h, m, d, et, mu in high_today]
                        )
                        return True, f"High impact news: {event.get('title', 'USD event')}", minutes_until
                except Exception:
                    continue
            logging.debug(
                "[NEWS upcoming] FF today high-impact events (none blocking): %s",
                [(h, m, d, et, mu) for h, m, d, et, mu in high_today]
            )
    except Exception:
        pass

    # Fall back to hardcoded news times
    for hour, minute, desc in HIGH_IMPACT_NEWS:
        event_minutes = hour * 60 + minute
        minutes_until = event_minutes - current_minutes
        if 0 < minutes_until <= lookahead_minutes:
            return True, f"News window: {desc} at {hour:02d}:{minute:02d} UTC", minutes_until

    return False, "", 0


# ─── 2. SESSION QUALITY SCORE ─────────────────────────────────────────────────

SESSION_MULTIPLIERS = {
    "London Open": 1.5,   # 7-10 UTC — best session
    "NY Open": 1.3,       # 13-16 UTC — second best
    "London": 1.1,        # 10-13 UTC — decent
    "NY": 1.0,            # 16-21 UTC — normal
    "Asian": 0.7,         # 0-7 UTC — avoid
    "Off-Session": 0.5,   # Outside hours
}

SESSION_SCORE_BONUS = {
    "London Open": 1,
    "NY Open": 1,
    "London": 0,
    "NY": 0,
    "Asian": -1,
    "Off-Session": -2,
}

def get_session_score_bonus(session: str) -> int:
    """Return score bonus/penalty based on session quality."""
    return SESSION_SCORE_BONUS.get(session, 0)

def get_session_quality(session: str) -> str:
    """Return human readable session quality."""
    multiplier = SESSION_MULTIPLIERS.get(session, 1.0)
    if multiplier >= 1.3:
        return "PRIME"
    elif multiplier >= 1.0:
        return "GOOD"
    elif multiplier >= 0.7:
        return "SLOW"
    else:
        return "AVOID"


# ─── 3. ENTRY VALIDATION ──────────────────────────────────────────────────────

ENTRY_MAX_PIPS_FOREX = 10      # 10 pips max deviation for forex
ENTRY_MAX_POINTS_GOLD = 15     # 15 points max deviation for gold
ENTRY_MAX_POINTS_FUTURES = 20  # 20 points max deviation for futures

def validate_entry(symbol: str, entry_price: float, current_price: float) -> tuple[bool, float]:
    """
    Check if current price is still within acceptable range of entry.
    Returns (is_valid, deviation).
    """
    deviation = abs(current_price - entry_price)
    sym = symbol.upper()

    if sym in ("XAUUSD", "GC", "MGC", "XAGUSD"):
        max_dev = ENTRY_MAX_POINTS_GOLD
    elif sym in ("ES", "MES", "NQ", "MNQ", "RTY", "YM", "CL", "MCL", "NG"):
        max_dev = ENTRY_MAX_POINTS_FUTURES
    else:
        # Forex (standard and JPY) — use pip spec so JPY pairs get correct scaling
        max_dev = get_pip_spec(sym)["pip"] * ENTRY_MAX_PIPS_FOREX

    return deviation <= max_dev, round(deviation, 5)


# ─── 4. 1H CANDLE CONFIRMATION ────────────────────────────────────────────────

def check_1h_candle_confirmation(candles_1h: list, direction: str) -> tuple[bool, str]:
    """
    Verify the most recent 1H candle closes in the signal direction.
    Returns (confirmed, reason).
    """
    if not candles_1h or len(candles_1h) < 2:
        return True, "Insufficient 1H data — proceeding"

    latest = candles_1h[0]
    candle_direction = "bullish" if latest["close"] > latest["open"] else "bearish"
    expected = "bullish" if direction == "BUY" else "bearish"

    if candle_direction == expected:
        body_size = abs(latest["close"] - latest["open"])
        total_range = latest["high"] - latest["low"]
        body_ratio = body_size / total_range if total_range > 0 else 0

        if body_ratio >= 0.5:
            return True, f"Strong 1H {candle_direction} confirmation ({body_ratio:.0%} body)"
        else:
            return True, f"Weak 1H {candle_direction} confirmation ({body_ratio:.0%} body)"
    else:
        return False, f"1H candle is {candle_direction} — conflicts with {direction} signal"


# ─── 5. SPREAD CHECK ──────────────────────────────────────────────────────────

MAX_SPREADS = {
    "XAUUSD": 0.50,    # 50 cents on gold
    "EURUSD": 0.00015, # 1.5 pips
    "GBPUSD": 0.00020, # 2 pips
    "USDJPY": 0.020,   # 2 pips
    "AUDUSD": 0.00020, # 2 pips
    "ES": 0.25,        # 1 tick
    "NQ": 0.25,        # 1 tick
    "CL": 0.02,        # 2 cents crude
    "GC": 0.50,        # 50 cents gold futures
}

def check_spread(symbol: str, bid: float, ask: float) -> tuple[bool, float]:
    """
    Check if spread is within acceptable range.
    Returns (is_acceptable, spread).
    """
    spread = abs(ask - bid)
    sym = symbol.upper()
    # Pip-spec pairs: max spread = 2 pips in that pair's pip units
    if sym in PIP_SPECS:
        max_spread = PIP_SPECS[sym]["pip"] * 2
    else:
        max_spread = MAX_SPREADS.get(sym, 0.001)
    is_ok = spread <= max_spread
    return is_ok, round(spread, 5)


def estimate_spread_from_candles(candles: list) -> float:
    """Estimate current spread from recent candle data."""
    if not candles:
        return 0.0
    # Use smallest recent candle body as spread estimate
    recent = candles[:5]
    bodies = [abs(c["close"] - c["open"]) for c in recent]
    return min(bodies) if bodies else 0.0


# ─── 6. CONSECUTIVE LOSS PROTECTION ──────────────────────────────────────────

def check_consecutive_losses(user_id: int, max_losses: int = 2) -> tuple[bool, int]:
    """
    Check if user has hit consecutive loss limit.
    Returns (should_warn, consecutive_count).
    """
    try:
        import psycopg2, os
        from dotenv import load_dotenv
        load_dotenv('/home/ubuntu/apfee/.env')
        conn = psycopg2.connect(os.getenv('DATABASE_URL'))
        cur = conn.cursor()

        cur.execute("""
            SELECT result FROM trade_insights
            WHERE user_id = %s
            AND created_at > NOW() - INTERVAL '24 hours'
            AND result IN ('WIN', 'LOSS')
            ORDER BY created_at DESC
            LIMIT 5
        """, (user_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return False, 0

        consecutive = 0
        for row in rows:
            if row[0] == 'LOSS':
                consecutive += 1
            else:
                break

        return consecutive >= max_losses, consecutive

    except Exception as e:
        logger.error(f"Consecutive loss check error: {e}")
        return False, 0


def get_loss_warning_message(consecutive: int) -> str:
    """Generate warning message for consecutive losses."""
    if consecutive >= 3:
        return (
            f"🚨 *3 consecutive losses today*\n\n"
            f"Stop trading for the day. Protect your challenge buffer.\n"
            f"Come back tomorrow with fresh eyes.\n\n"
            f"This is the system protecting you. Trust it."
        )
    elif consecutive >= 2:
        return (
            f"⚠️ *2 consecutive losses*\n\n"
            f"Take a 30-minute break before next trade.\n"
            f"Only take 10/10 A+ signals for the rest of today.\n\n"
            f"Consider calling it a day — protecting your buffer is more important than chasing losses."
        )
    return ""


# ─── COMBINED PRE-SCAN VALIDATION ─────────────────────────────────────────────

def run_pre_scan_checks(symbol: str, entry_price: float,
                         current_price: float, direction: str,
                         session: str, candles_1h: list = None) -> dict:
    """
    Run all pre-scan validation checks.
    Returns dict with pass/fail for each check.
    """
    results = {
        "passed": True,
        "blocks": [],
        "warnings": [],
        "session_bonus": 0,
        "news_blocked": False,
        "entry_valid": True,
        "candle_confirmed": True,
    }

    # 1. News filter
    news_blocked, news_reason = is_news_window()
    if news_blocked:
        results["news_blocked"] = True
        results["passed"] = False
        results["blocks"].append(f"🚨 {news_reason}")

    # 2. Session quality bonus
    session_bonus = get_session_score_bonus(session)
    results["session_bonus"] = session_bonus
    quality = get_session_quality(session)
    if quality == "AVOID":
        results["warnings"].append(f"⚠️ Asian/off-session — lower probability setup")
    elif quality == "PRIME":
        results["warnings"].append(f"✅ Prime session — highest probability window")

    # 3. Entry validation
    entry_valid, deviation = validate_entry(symbol, entry_price, current_price)
    results["entry_valid"] = entry_valid
    if not entry_valid:
        results["passed"] = False
        results["blocks"].append(f"❌ Entry missed — price moved {deviation} from zone")

    # 4. 1H candle confirmation
    if candles_1h:
        confirmed, reason = check_1h_candle_confirmation(candles_1h, direction)
        results["candle_confirmed"] = confirmed
        if not confirmed:
            results["warnings"].append(f"⚠️ {reason}")

    return results


# ─── 7. LIQUIDITY SWEEP DETECTION ────────────────────────────────────────────

def detect_liquidity_sweep(candles: list, direction: str, symbol: str = "") -> tuple[bool, float]:
    """
    Check last 5 candles for a liquidity sweep.
    BUY: candle low pierced below previous 3-candle low but closed back above it.
    SELL: candle high pierced above previous 3-candle high but closed back below it.
    Returns (sweep_detected, swept_level).
    """
    if not candles or len(candles) < 6:
        return False, 0.0

    # Minimum pierce distance — 1 pip in the pair's units — filters wicks that barely graze the level
    sym = symbol.upper() if symbol else ""
    min_pierce = PIP_SPECS[sym]["pip"] if sym in _FOREX_PIP_SPEC_PAIRS else 0.0

    recent = candles[:5]

    if direction == "BUY":
        for i, c in enumerate(recent):
            if i + 3 >= len(candles):
                break
            prev_low = min(candles[i+1]["low"], candles[i+2]["low"], candles[i+3]["low"])
            if c["low"] < prev_low - min_pierce and c["close"] > prev_low:
                return True, round(prev_low, 5)
    else:  # SELL
        for i, c in enumerate(recent):
            if i + 3 >= len(candles):
                break
            prev_high = max(candles[i+1]["high"], candles[i+2]["high"], candles[i+3]["high"])
            if c["high"] > prev_high + min_pierce and c["close"] < prev_high:
                return True, round(prev_high, 5)

    return False, 0.0


# ─── 8. REJECTION CANDLE DETECTION ───────────────────────────────────────────

def detect_rejection_candle(candles: list, direction: str, ob_zone_mid: float, symbol: str = "") -> tuple[bool, str]:
    """
    Check most recent 3 candles for rejection patterns near an OB zone.
    BUY: hammer, bullish engulfing, pin bar (lower wick >60% of range).
    SELL: shooting star, bearish engulfing, pin bar (upper wick >60% of range).
    Must be within 15 pips of ob_zone_mid.
    Returns (found, candle_type).
    """
    if not candles or len(candles) < 2:
        return False, ""

    # Proximity threshold: 15 pips via pip spec for forex pairs; price-level fallback for gold/futures
    sym = symbol.upper() if symbol else ""
    if sym in _FOREX_PIP_SPEC_PAIRS:
        threshold = PIP_SPECS[sym]["pip"] * 15
    else:
        threshold = 0.0015 if ob_zone_mid < 100 else 15.0

    for i in range(min(3, len(candles))):
        c = candles[i]
        candle_mid = (c["high"] + c["low"]) / 2
        if abs(candle_mid - ob_zone_mid) > threshold:
            continue

        body = abs(c["close"] - c["open"])
        total_range = c["high"] - c["low"]
        if total_range == 0:
            continue

        upper_wick = c["high"] - max(c["close"], c["open"])
        lower_wick = min(c["close"], c["open"]) - c["low"]

        if direction == "BUY":
            if body > 0 and lower_wick >= 2 * body:
                return True, "hammer"
            if lower_wick / total_range > 0.6:
                return True, "pin bar"
            if i + 1 < len(candles):
                prev = candles[i + 1]
                if (c["close"] > c["open"] and prev["close"] < prev["open"] and
                        c["close"] >= prev["open"] and c["open"] <= prev["close"]):
                    return True, "bullish engulfing"
        else:  # SELL
            if body > 0 and upper_wick >= 2 * body:
                return True, "shooting star"
            if upper_wick / total_range > 0.6:
                return True, "pin bar"
            if i + 1 < len(candles):
                prev = candles[i + 1]
                if (c["close"] < c["open"] and prev["close"] > prev["open"] and
                        c["close"] <= prev["low"] and c["open"] >= prev["high"]):
                    return True, "bearish engulfing"

    return False, ""


# ─── 9. RANGE FILTER ──────────────────────────────────────────────────────────

def is_ranging_market(candles: list) -> bool:
    """
    Returns True only when ALL three conditions are met simultaneously:
    1. 4-candle range < 0.15% of price
    2. 10-candle range < 0.5% of price
    3. No liquidity sweep in the last 5 candles

    A liquidity sweep is a candle whose wick pierced beyond the prior 3-candle
    high/low but whose body closed back inside — a sign of institutional activity
    that never occurs in a truly ranging market.
    """
    if not candles or len(candles) < 4:
        return False

    current_price = candles[0]["close"]
    if current_price == 0:
        return False

    # Condition 1: 4-candle range < 0.15%
    recent4 = candles[:4]
    range4 = (max(c["high"] for c in recent4) - min(c["low"] for c in recent4)) / current_price
    if range4 >= 0.0015:
        return False

    # Condition 2: 10-candle range < 0.5%
    if len(candles) >= 10:
        lookback10 = candles[:10]
        range10 = (max(c["high"] for c in lookback10) - min(c["low"] for c in lookback10)) / current_price
        if range10 >= 0.005:
            return False

    # Condition 3: no liquidity sweep in the last 5 candles
    # A sweep: wick breaks the prior-3-candle high or low, but candle closes back inside
    if len(candles) >= 5:
        for i in range(5):
            candle = candles[i]
            # need at least 3 candles before this one for the reference window
            if i + 3 >= len(candles):
                break
            prior3 = candles[i + 1: i + 4]
            prior_high = max(c["high"] for c in prior3)
            prior_low  = min(c["low"]  for c in prior3)
            close = candle["close"]
            # Bullish sweep: wick below prior low but close back above it
            if candle["low"] < prior_low and close > prior_low:
                return False
            # Bearish sweep: wick above prior high but close back below it
            if candle["high"] > prior_high and close < prior_high:
                return False

    return True


# ─── 10. PREVIOUS DAY HIGH/LOW ────────────────────────────────────────────────

def get_previous_day_levels(candles: list) -> dict:
    """
    Find previous day high and low from candles list (newest first).
    Returns dict with pdh, pdl, current_price.
    """
    if not candles:
        return {"pdh": None, "pdl": None, "current_price": None}

    current_price = candles[0]["close"]
    current_day = datetime.now(timezone.utc).date()
    prev_day_candles = []
    prev_day_date = None

    for c in candles:
        dt_str = c.get("datetime", "")
        if not dt_str:
            continue
        try:
            if "T" in str(dt_str):
                c_dt = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
            else:
                c_dt = datetime.strptime(str(dt_str)[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            c_day = c_dt.date()
            if c_day < current_day:
                if prev_day_date is None:
                    prev_day_date = c_day
                if c_day == prev_day_date:
                    prev_day_candles.append(c)
        except Exception:
            continue

    if prev_day_candles:
        pdh = max(c["high"] for c in prev_day_candles)
        pdl = min(c["low"] for c in prev_day_candles)
        return {"pdh": round(pdh, 5), "pdl": round(pdl, 5), "current_price": round(current_price, 5)}

    # Fallback: older half of available candles
    mid = max(len(candles) // 2, 2)
    older = candles[mid:]
    if not older:
        return {"pdh": None, "pdl": None, "current_price": round(current_price, 5)}

    pdh = max(c["high"] for c in older)
    pdl = min(c["low"] for c in older)
    return {"pdh": round(pdh, 5), "pdl": round(pdl, 5), "current_price": round(current_price, 5)}


# ─── 11. TIME OF DAY FILTER PER PAIR ─────────────────────────────────────────

PAIR_OPTIMAL_HOURS = {
    "EURUSD": [(7, 16)],            # London open through NY session
    "GBPUSD": [(7, 16)],            # London open through NY session
    "XAUUSD": [(7, 16)],            # London open through NY session
    "USDJPY": [(0, 3), (7, 16)],    # Asian session + London/NY
    "AUDUSD": [(7, 14)],            # London open through early NY
    "NZDUSD": [(7, 14)],            # London open through early NY
    "USDCAD": [(12, 20)],           # NY session (most active for CAD)
    "USDCHF": [(7, 16)],            # London open through NY session
    "EURJPY": [(0, 3), (7, 16)],
    "GBPJPY": [(0, 3), (7, 16)],
    "ES":     [(12, 16)],           # NY open 8 AM EDT = 12:00 UTC
    "MES":    [(12, 16)],
    "NQ":     [(12, 16)],
    "MNQ":    [(12, 16)],
    "CL":     [(12, 16)],
    "MCL":    [(12, 16)],
}

def is_optimal_time_for_pair(symbol: str) -> tuple[bool, str]:
    """
    Check if current UTC hour is within optimal trading window for the pair.
    Returns (is_optimal, reason_string).
    """
    hour = datetime.now(timezone.utc).hour
    sym = symbol.upper()
    windows = PAIR_OPTIMAL_HOURS.get(sym)

    if not windows:
        return True, ""

    for start, end in windows:
        if start <= hour < end:
            return True, f"{sym} in optimal window {start:02d}:00–{end:02d}:00 UTC"

    window_strs = [f"{s:02d}:00–{e:02d}:00" for s, e in windows]
    return False, f"{sym} outside optimal hours — optimal: {', '.join(window_strs)} UTC"


# ─── 12. RISK REWARD MINIMUM FILTER ──────────────────────────────────────────

def validate_risk_reward(entry: float, sl: float, tp1: float, min_rr: float = 1.5) -> tuple[bool, float]:
    """
    Validate actual risk/reward ratio meets minimum threshold.
    Returns (is_valid, actual_rr).
    """
    sl_dist = abs(entry - sl)
    if sl_dist == 0:
        return False, 0.0

    actual_rr = round(abs(tp1 - entry) / sl_dist, 2)
    if actual_rr < min_rr:
        logger.warning(
            f"[RR_check] FAILED — entry={entry} sl={sl} tp1={tp1} "
            f"sl_dist={round(sl_dist, 5)} actual_rr={actual_rr} min={min_rr}"
        )
    return actual_rr >= min_rr, actual_rr


# ─── 13. CORRELATION FILTER ───────────────────────────────────────────────────

CORRELATED_SAME_DIRECTION = {
    "EURUSD": ["GBPUSD", "AUDUSD", "NZDUSD"],
    "GBPUSD": ["EURUSD", "AUDUSD", "NZDUSD"],
    "AUDUSD": ["EURUSD", "GBPUSD", "NZDUSD"],
    "NZDUSD": ["EURUSD", "GBPUSD", "AUDUSD"],
    "USDJPY": ["USDCAD", "USDCHF"],
    "USDCAD": ["USDJPY", "USDCHF"],
    "USDCHF": ["USDJPY", "USDCAD"],
    "ES": ["NQ", "YM"],
    "NQ": ["ES", "YM"],
    "YM": ["ES", "NQ"],
    "XAUUSD": ["GC", "MGC"],
    "GC": ["XAUUSD"],
}

INVERSE_CORRELATED = {
    "EURUSD": ["USDJPY", "USDCAD", "USDCHF"],
    "GBPUSD": ["USDJPY", "USDCAD", "USDCHF"],
    "AUDUSD": ["USDJPY", "USDCAD", "USDCHF"],
    "NZDUSD": ["USDJPY", "USDCAD", "USDCHF"],
    "USDJPY": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"],
    "USDCAD": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"],
    "USDCHF": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"],
}

def check_pair_correlation(symbol: str, direction: str, active_signals: list) -> tuple[bool, str, str]:
    """
    Block signals that double USD exposure via same-direction correlation.
    Warn (but allow) on inverse correlation — USD theme conflict.

    Same-direction: e.g. EURUSD SELL + GBPUSD SELL — both express EUR/GBP weakness vs USD. Hard block.
    Inverse: e.g. EURUSD BUY active, USDJPY BUY fires — conflicting USD themes. Warn, don't block.

    active_signals: list of dicts with 'symbol' and 'direction' keys.
    Returns (is_ok_to_trade, block_reason, correlation_warning).
    """
    if not active_signals:
        return True, "", ""

    sym = symbol.upper()
    new_dir = direction.upper()

    active_map = {
        s.get("symbol", "").upper(): s.get("direction", "").upper()
        for s in active_signals
    }

    # Same-direction block — hard stop, doubling exposure
    for corr_sym in CORRELATED_SAME_DIRECTION.get(sym, []):
        if corr_sym in active_map and active_map[corr_sym] == new_dir:
            return False, f"Correlated pair {corr_sym} already has a {new_dir} signal — skip to avoid doubling exposure", ""

    # Inverse-correlation: same USD theme via opposite pair directions — warn but allow
    for inv_sym in INVERSE_CORRELATED.get(sym, []):
        if inv_sym in active_map:
            active_dir = active_map[inv_sym]
            if active_dir != new_dir:
                usd_theme = "USD strengthening" if new_dir == "BUY" and "JPY" in sym else "USD conflict"
                warning = (
                    f"⚠️ {usd_theme} — {sym} {new_dir} signal while {inv_sym} {active_dir} active. "
                    f"USD theme conflict detected."
                )
                return True, "", warning

    return True, "", ""


# ─── 14. MULTI-TIMEFRAME OB CONFLUENCE ───────────────────────────────────────

# Mirrors YFINANCE_FUTURES_MAP from scanner.py — kept in sync manually
_MTF_FUTURES_MAP = {
    'ES': 'ES=F', 'MES': 'MES=F', 'NQ': 'NQ=F', 'MNQ': 'MNQ=F',
    'RTY': 'RTY=F', 'YM': 'YM=F', 'CL': 'CL=F', 'MCL': 'MCL=F',
    'GC': 'GC=F', 'MGC': 'MGC=F', 'NG': 'NG=F',
}


def _fetch_htf_candles(symbol: str, interval_yf: str, period_yf: str,
                       interval_td: str, outputsize: int) -> list | None:
    """Fetch candles for MTF analysis — yFinance for futures, Twelve Data for forex."""
    sym = symbol.upper()
    ticker = _MTF_FUTURES_MAP.get(sym)
    if ticker:
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period=period_yf, interval=interval_yf)
            if hist.empty:
                return None
            result = []
            for _, row in hist.iloc[::-1].iterrows():
                result.append({
                    "open": float(row["Open"]), "high": float(row["High"]),
                    "low": float(row["Low"]), "close": float(row["Close"]),
                })
            return result[:outputsize]
        except Exception as e:
            logger.debug(f"MTF yFinance fetch error {symbol} {interval_yf}: {e}")
            return None
    else:
        try:
            from config import TWELVE_DATA_API_KEY
            from market import normalize_symbol
            td_symbol = normalize_symbol(symbol)
            if not td_symbol or not TWELVE_DATA_API_KEY:
                return None
            resp = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": td_symbol, "interval": interval_td,
                        "outputsize": outputsize, "apikey": TWELVE_DATA_API_KEY},
                timeout=8,
            )
            data = resp.json()
            if "values" not in data:
                return None
            return [
                {"open": float(c["open"]), "high": float(c["high"]),
                 "low": float(c["low"]), "close": float(c["close"])}
                for c in data["values"]
            ]
        except Exception as e:
            logger.debug(f"MTF Twelve Data fetch error {symbol} {interval_td}: {e}")
            return None


def _detect_ob_htf(candles: list, trend: str) -> dict | None:
    """Minimal OB detection for MTF confluence — same logic as detect_order_block in scanner.py."""
    if not candles or len(candles) < 5:
        return None
    if trend == "bullish":
        for i in range(1, min(15, len(candles))):
            c = candles[i]
            if c["close"] < c["open"] and candles[i - 1]["close"] > c["high"]:
                return {"high": c["high"], "low": c["low"],
                        "mid": round((c["high"] + c["low"]) / 2, 5)}
    elif trend == "bearish":
        for i in range(1, min(15, len(candles))):
            c = candles[i]
            if c["close"] > c["open"] and candles[i - 1]["close"] < c["low"]:
                return {"high": c["high"], "low": c["low"],
                        "mid": round((c["high"] + c["low"]) / 2, 5)}
    return None


def detect_mtf_ob_confluence(symbol: str, current_ob: dict, direction: str) -> tuple[bool, str]:
    """
    Check if the 15M order block also aligns with a 1H or 4H order block at the same level.
    Triple timeframe OB confluence is the highest probability SMC setup.
    Returns (confluence_found, description).
    Scoring applied by caller: triple→+3, 4H only→+2, 1H only→+1.
    """
    if not current_ob:
        return False, ""

    ob_low = current_ob["low"]
    ob_high = current_ob["high"]
    trend = "bullish" if direction == "BUY" else "bearish"
    mid_price = current_ob.get("mid", ob_high)

    # Tolerance: 10 pips for forex, 10 points for gold/futures
    tol_1h = 0.001 if mid_price < 100 else 10.0
    # Tolerance: 20 pips for forex, 20 points for gold/futures
    tol_4h = 0.002 if mid_price < 100 else 20.0

    def _overlaps(low_a, high_a, low_b, high_b, tol):
        return low_a <= high_b + tol and low_b <= high_a + tol

    try:
        # 1H: 5-day history at 1h interval, 50 candles
        candles_1h = _fetch_htf_candles(symbol, "1h", "5d", "1h", 50)
        ob_1h = _detect_ob_htf(candles_1h, trend) if candles_1h else None

        # 4H: 20-day history at 4h interval, 30 candles
        candles_4h = _fetch_htf_candles(symbol, "4h", "20d", "4h", 30)
        ob_4h = _detect_ob_htf(candles_4h, trend) if candles_4h else None

        h1_conf = bool(ob_1h and _overlaps(ob_low, ob_high, ob_1h["low"], ob_1h["high"], tol_1h))
        h4_conf = bool(ob_4h and _overlaps(ob_low, ob_high, ob_4h["low"], ob_4h["high"], tol_4h))

        if h1_conf and h4_conf:
            return True, "Triple timeframe OB confluence (15M+1H+4H) — highest probability setup"
        elif h4_conf:
            return True, "4H OB confluence confirmed — strong institutional level"
        elif h1_conf:
            return True, "1H OB confluence confirmed — solid structural level"

    except Exception as e:
        logger.debug(f"MTF OB confluence error for {symbol}: {e}")

    return False, ""


# ─── 15. MOMENTUM DETECTION ──────────────────────────────────────────────────

def detect_momentum(candles: list, direction: str) -> tuple[bool, str]:
    """
    Detect if price is moving with strong momentum in signal direction.
    Strong momentum = 3+ consecutive candles closing in same direction
    with increasing body sizes.
    Returns (momentum_detected, description).
    """
    if not candles or len(candles) < 3:
        return False, ""

    expected_bullish = direction.upper() == "BUY"
    consecutive = 0
    prev_body = None
    bodies_increasing = True

    for c in candles[:5]:
        is_bullish = c["close"] > c["open"]
        if is_bullish == expected_bullish:
            body = abs(c["close"] - c["open"])
            if prev_body is not None and body <= prev_body:
                bodies_increasing = False
            prev_body = body
            consecutive += 1
        else:
            break

    if consecutive >= 3:
        dir_label = "bullish" if expected_bullish else "bearish"
        if bodies_increasing:
            return True, f"Strong momentum — 3 consecutive {dir_label} candles with increasing volume"
        return True, f"Momentum confirmed — consecutive {dir_label} candles"

    return False, ""


# ─── 16. DAILY BIAS ───────────────────────────────────────────────────────────

_DAILY_BIAS_FUTURES_MAP = {
    'ES': 'ES=F', 'MES': 'MES=F', 'NQ': 'NQ=F', 'MNQ': 'MNQ=F',
    'RTY': 'RTY=F', 'YM': 'YM=F', 'CL': 'CL=F', 'MCL': 'MCL=F',
    'GC': 'GC=F', 'MGC': 'MGC=F', 'NG': 'NG=F', 'XAUUSD': 'GC=F',
}

_FOREX_YFINANCE_MAP = {
    'EURUSD': 'EURUSD=X', 'GBPUSD': 'GBPUSD=X', 'USDJPY': 'USDJPY=X',
    'AUDUSD': 'AUDUSD=X', 'USDCAD': 'USDCAD=X', 'NZDUSD': 'NZDUSD=X',
    'USDCHF': 'USDCHF=X',
}


def get_daily_bias(symbol: str) -> dict:
    """
    Get the daily candle bias for a symbol.
    Returns dict with bias, strength, confirmed flag, and reason.
    """
    _default = {"bias": "unknown", "strength": "weak", "today_candle": "neutral",
                "confirmed": False, "reason": "Insufficient data",
                "intraday_override": False, "intraday_move_pct": 0.0}
    sym = symbol.upper()
    try:
        candles = []
        ticker = _DAILY_BIAS_FUTURES_MAP.get(sym)
        if ticker:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="10d", interval="1d")
            if hist.empty or len(hist) < 3:
                return _default
            for _, row in hist.iloc[-5:].iterrows():
                candles.append({
                    "open": float(row["Open"]), "high": float(row["High"]),
                    "low": float(row["Low"]),  "close": float(row["Close"]),
                })
        else:
            td_success = False
            try:
                from config import TWELVE_DATA_API_KEY
                from market import normalize_symbol
                td_sym = normalize_symbol(sym)
                if td_sym and TWELVE_DATA_API_KEY:
                    resp = requests.get(
                        "https://api.twelvedata.com/time_series",
                        params={"symbol": td_sym, "interval": "1day",
                                "outputsize": 5, "apikey": TWELVE_DATA_API_KEY},
                        timeout=8,
                    )
                    data = resp.json()
                    if "values" in data:
                        for c in reversed(data["values"]):
                            candles.append({
                                "open": float(c["open"]), "high": float(c["high"]),
                                "low":  float(c["low"]),  "close": float(c["close"]),
                            })
                        td_success = True
            except Exception:
                pass
            if not td_success:
                yf_ticker = _FOREX_YFINANCE_MAP.get(sym)
                if not yf_ticker:
                    return _default
                try:
                    import yfinance as yf
                    hist = yf.Ticker(yf_ticker).history(period="5d", interval="1d")
                    if hist.empty or len(hist) < 3:
                        return _default
                    for _, row in hist.iloc[-5:].iterrows():
                        candles.append({
                            "open": float(row["Open"]), "high": float(row["High"]),
                            "low":  float(row["Low"]),  "close": float(row["Close"]),
                        })
                except Exception:
                    return _default

        if len(candles) < 3:
            return _default

        def _candle_dir(c):
            return "bullish" if c["close"] > c["open"] else "bearish"

        def _body_ratio(c):
            rng = c["high"] - c["low"]
            return abs(c["close"] - c["open"]) / rng if rng else 0

        def _score(c):
            return 1 if c["close"] > c["open"] else -1

        # candles are oldest→newest; last entry is today
        today       = candles[-1]
        yesterday   = candles[-2]
        two_days    = candles[-3]

        today_dir   = _candle_dir(today)
        today_ratio = _body_ratio(today)

        intraday_move_pct = ((today["close"] - today["open"]) / today["open"] * 100) if today["open"] else 0.0
        intraday_override = today_ratio > 0.6

        # Strong today candle overrides everything
        if today_ratio > 0.7:
            bias      = today_dir
            strength  = "strong"
            confirmed = True
            reason    = f"Strong today candle ({today_dir}, body {today_ratio:.0%}) overrides history"
            return {
                "bias": bias, "strength": strength,
                "today_candle": today_dir, "confirmed": confirmed, "reason": reason,
                "intraday_override": True, "intraday_move_pct": round(intraday_move_pct, 2),
            }

        # Weighted score: today×3, yesterday×2, two_days_ago×1
        weighted = _score(today) * 3 + _score(yesterday) * 2 + _score(two_days) * 1

        if weighted >= 3:
            bias = "bullish"
        elif weighted >= 1:
            bias = "bullish"
        elif weighted <= -3:
            bias = "bearish"
        elif weighted <= -1:
            bias = "bearish"
        else:
            bias = "neutral"

        confirmed = abs(weighted) >= 2

        # Strength
        if today_ratio > 0.6 and _candle_dir(today) == bias:
            strength = "strong"
        elif abs(weighted) >= 3:
            strength = "moderate"
        else:
            strength = "weak"

        reason = (
            f"Weighted score {weighted:+d}/6 "
            f"(today {_candle_dir(today)}, yday {_candle_dir(yesterday)}, "
            f"2d {_candle_dir(two_days)})"
        )

        return {
            "bias": bias,
            "strength": strength,
            "today_candle": today_dir,
            "confirmed": confirmed,
            "reason": reason,
            "intraday_override": intraday_override,
            "intraday_move_pct": round(intraday_move_pct, 2),
        }

    except Exception as e:
        logger.error(f"[daily_bias] {symbol}: {e}")
        return _default


def check_daily_bias_alignment(symbol: str, direction: str, _prefetched: dict = None) -> tuple[bool, str]:
    """
    Check if trade direction aligns with the daily bias.
    Returns (aligned, message).
    Pass _prefetched to reuse an already-fetched get_daily_bias() result.
    """
    bias = _prefetched if _prefetched is not None else get_daily_bias(symbol)
    b = bias["bias"]
    confirmed = bias["confirmed"]

    if b == "unknown":
        return False, "⚠️ No bias data available — proceed with extra caution"

    if direction.upper() == "BUY" and b == "bearish" and confirmed:
        return False, "⚠️ DAILY BIAS CONFLICT — Trading BUY against confirmed bearish daily bias. High risk."
    if direction.upper() == "SELL" and b == "bullish" and confirmed:
        return False, "⚠️ DAILY BIAS CONFLICT — Trading SELL against confirmed bullish daily bias. High risk."

    if b not in ("neutral", "unknown") and confirmed:
        return True, f"✅ Daily bias {b} confirms {direction.upper()} direction"

    return True, ""
