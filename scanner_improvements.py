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

# ─── 1. NEWS FILTER ───────────────────────────────────────────────────────────

# High impact news times UTC — updated weekly
# Format: (hour, minute, description)
HIGH_IMPACT_NEWS = [
    # Monday
    (0, 0, "Weekly open"),
    # Tuesday-Friday common slots
    (8, 30, "US Core PCE / GDP / NFP / CPI"),
    (13, 30, "US economic data"),
    (14, 0, "US economic data"),
    (15, 0, "US economic data"),
    (18, 0, "FOMC minutes"),
    (19, 0, "Fed speeches"),
    # GBP events
    (7, 0, "BOE / UK data"),
    (9, 0, "ECB / EUR data"),
]

NEWS_BLOCK_MINUTES_BEFORE = 30
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
                    # Parse time like "8:30am"
                    from datetime import datetime as dt
                    t = dt.strptime(event_time.upper(), "%I:%M%p")
                    event_minutes = t.hour * 60 + t.minute
                    # Convert ET to UTC (+4 or +5 depending on DST)
                    event_minutes_utc = event_minutes + 4 * 60
                    if event_minutes_utc > 24 * 60:
                        event_minutes_utc -= 24 * 60

                    diff = current_minutes - event_minutes_utc
                    if -NEWS_BLOCK_MINUTES_BEFORE <= diff <= NEWS_BLOCK_MINUTES_AFTER:
                        return True, f"High impact news: {event.get('title', 'USD event')}"
                except Exception:
                    continue
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

    symbol_upper = symbol.upper()

    if symbol_upper in ("XAUUSD", "GC", "MGC"):
        max_dev = ENTRY_MAX_POINTS_GOLD
        is_valid = deviation <= max_dev
    elif symbol_upper in ("ES", "MES", "NQ", "MNQ", "RTY", "YM"):
        max_dev = ENTRY_MAX_POINTS_FUTURES
        is_valid = deviation <= max_dev
    else:
        # Forex — convert to pips
        max_dev = ENTRY_MAX_PIPS_FOREX * 0.0001
        is_valid = deviation <= max_dev

    return is_valid, round(deviation, 5)


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
    max_spread = MAX_SPREADS.get(symbol.upper(), 0.001)
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
