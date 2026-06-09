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
    "EURUSD": {"pip": 0.0001, "min_sl": 0.0012, "min_atr": 0.0005},  # 12 pips
    "GBPUSD": {"pip": 0.0001, "min_sl": 0.0015, "min_atr": 0.0006},  # 15 pips — most volatile forex
    "AUDUSD": {"pip": 0.0001, "min_sl": 0.0010, "min_atr": 0.0005},  # 10 pips
    "NZDUSD": {"pip": 0.0001, "min_sl": 0.0010, "min_atr": 0.0005},  # 10 pips
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

NEWS_BLOCK_MINUTES_BEFORE = 45
NEWS_BLOCK_MINUTES_AFTER  = 30

# Minimal fallback — only used when ALL live feeds fail
FALLBACK_NEWS = []

# 60-minute cache for today's event list
_FF_CACHE: dict = {"events": None, "fetched_at": 0.0}
_FF_CACHE_TTL = 3600


def _et_to_utc_minutes(hour: int, minute: int) -> int:
    """Convert Eastern Time (EDT=UTC-4, EST=UTC-5) to UTC minutes-since-midnight."""
    month = datetime.now(timezone.utc).month
    offset = 4 if 3 <= month <= 11 else 5   # EDT Mar-Nov, EST Dec-Feb
    utc = hour * 60 + minute + offset * 60
    return utc % (24 * 60)


def is_nfp_friday() -> bool:
    """Return True if today is the first Friday of the month (NFP release day)."""
    from datetime import date, timedelta
    today = date.today()
    if today.weekday() != 4:  # 4 = Friday
        return False
    first_day = today.replace(day=1)
    days_to_friday = (4 - first_day.weekday()) % 7
    first_friday = first_day + timedelta(days=days_to_friday)
    return today == first_friday


# Known FOMC announcement dates (second day of each meeting, 2:00 PM ET = 18:00/19:00 UTC)
_FOMC_DATES_2026 = {
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-10",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
}


def fetch_forexfactory_today() -> list:
    """
    Fetch today's high-impact events. Caches result for 60 minutes.
    Returns list of dicts: {time_utc, currency, event, impact}.
    Four-strategy fallback: FF HTML → Investing.com → FF JSON → smart hardcoded.
    """
    import time as _t
    now = datetime.now(timezone.utc)

    if _FF_CACHE["events"] is not None and (_t.time() - _FF_CACHE["fetched_at"]) < _FF_CACHE_TTL:
        return _FF_CACHE["events"]

    events: list = []

    def _cache_and_return(evs: list, source: str) -> list:
        logger.info(f"[news] {source}: {len(evs)} high-impact events today")
        for e in evs:
            logger.info(f"[news]  {e['time_utc'].strftime('%H:%M UTC')}  {e['currency']}  {e['event']}")
        _FF_CACHE.update({"events": evs, "fetched_at": _t.time()})
        return evs

    def _parse_ff_html(html: str) -> list:
        from bs4 import BeautifulSoup
        evs = []
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="calendar__table")
        if not table:
            return evs
        today_date = now.date()
        in_today = False
        current_time_str = ""
        for row in table.find_all("tr"):
            row_classes = row.get("class", [])
            if "calendar__row--new-day" in row_classes:
                date_td = row.find("td", class_="calendar__date")
                if date_td:
                    raw = date_td.get_text(strip=True).replace("\xa0", "")
                    try:
                        row_date = datetime.strptime(
                            f"{raw} {today_date.year}", "%a%b %d %Y"
                        ).date()
                        in_today = (row_date == today_date)
                        current_time_str = ""
                    except Exception:
                        in_today = False
                time_td = row.find("td", class_="calendar__time")
                if time_td and in_today:
                    t_text = time_td.get_text(strip=True).replace("\xa0", "").strip()
                    if t_text and t_text not in ("Tentative", "All Day", ""):
                        current_time_str = t_text
            if not in_today or "calendar__row" not in row_classes:
                continue
            impact_td = row.find("td", class_="calendar__impact")
            if not impact_td:
                continue
            if not impact_td.find(class_=lambda c: c and "red" in str(c).lower()):
                continue
            time_td = row.find("td", class_="calendar__time")
            if time_td:
                t_text = time_td.get_text(strip=True).replace("\xa0", "").strip()
                if t_text and t_text not in ("Tentative", "All Day", ""):
                    current_time_str = t_text
            if not current_time_str or current_time_str in ("Tentative", "All Day"):
                continue
            cur_td = row.find("td", class_="calendar__currency")
            currency = cur_td.get_text(strip=True) if cur_td else ""
            ev_td = row.find("td", class_="calendar__event")
            event_title = ""
            if ev_td:
                title_el = ev_td.find(class_="calendar__event-title")
                event_title = (title_el or ev_td).get_text(strip=True)
            try:
                t = datetime.strptime(current_time_str.upper(), "%I:%M%p")
                utc_min = _et_to_utc_minutes(t.hour, t.minute)
                utc_h, utc_m = divmod(utc_min, 60)
                evs.append({
                    "time_utc": now.replace(hour=utc_h, minute=utc_m, second=0, microsecond=0),
                    "currency": currency,
                    "event": event_title,
                    "impact": "high",
                })
            except Exception:
                continue
        return evs

    # ── Strategy 1: ForexFactory HTML (2 attempts, 5s apart) ─────────────────
    for _attempt in range(2):
        try:
            resp = requests.get(
                "https://www.forexfactory.com/calendar.php?day=today",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "keep-alive",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                events = _parse_ff_html(resp.text)
            if events:
                return _cache_and_return(events, f"ForexFactory HTML (attempt {_attempt + 1})")
            break  # succeeded but no events — skip retry, fall to Strategy 2
        except Exception as _e:
            logger.debug(f"[news] FF HTML fetch failed (attempt {_attempt + 1}): {_e}")
            if _attempt == 0:
                _t.sleep(5)

    # ── Strategy 2: Investing.com economic calendar API ───────────────────────
    try:
        today_str = now.strftime("%Y-%m-%d")
        resp = requests.get(
            "https://api.investing.com/api/financialdata/calendar/economic",
            params={"from": today_str, "to": today_str, "importance": "high"},
            headers={
                "User-Agent": "Mozilla/5.0",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in (data.get("data") or data if isinstance(data, list) else []):
                ev_time = item.get("time") or item.get("event_time", "")
                currency = item.get("currency") or item.get("country", "")
                title = item.get("event") or item.get("name", "?")
                if not ev_time:
                    continue
                try:
                    t = datetime.strptime(str(ev_time).upper(), "%I:%M%p")
                    utc_min = _et_to_utc_minutes(t.hour, t.minute)
                    utc_h, utc_m = divmod(utc_min, 60)
                    events.append({
                        "time_utc": now.replace(hour=utc_h, minute=utc_m, second=0, microsecond=0),
                        "currency": currency,
                        "event": title,
                        "impact": "high",
                    })
                except Exception:
                    continue
        if events:
            return _cache_and_return(events, "Investing.com API")
    except Exception as _e:
        logger.debug(f"[news] Investing.com API failed: {_e}")

    # ── Strategy 3: nfs.faireconomy.media JSON feed ───────────────────────────
    try:
        resp = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=6,
        )
        if resp.status_code == 200:
            today_str = now.strftime("%Y-%m-%d")
            for item in resp.json():
                if item.get("impact") != "High":
                    continue
                if today_str not in item.get("date", ""):
                    continue
                ev_time = item.get("time", "")
                if not ev_time or ev_time == "Tentative":
                    continue
                try:
                    t = datetime.strptime(ev_time.upper(), "%I:%M%p")
                    utc_min = _et_to_utc_minutes(t.hour, t.minute)
                    utc_h, utc_m = divmod(utc_min, 60)
                    events.append({
                        "time_utc": now.replace(hour=utc_h, minute=utc_m, second=0, microsecond=0),
                        "currency": item.get("country", ""),
                        "event": item.get("title", "?"),
                        "impact": "high",
                    })
                except Exception:
                    continue
        if events:
            return _cache_and_return(events, "FF JSON feed")
    except Exception as _e:
        logger.debug(f"[news] FF JSON feed failed: {_e}")

    # ── Strategy 4: Smart hardcoded fallback — NFP and FOMC only ─────────────
    logger.warning("[news] All live feeds failed — using smart hardcoded fallback")
    today_iso = now.date().isoformat()
    if is_nfp_friday():
        # NFP releases at 8:30 AM ET = 12:30 UTC (EDT) / 13:30 UTC (EST)
        nfp_utc_h = 12 if (3 <= now.month <= 11) else 13
        events.append({
            "time_utc": now.replace(hour=nfp_utc_h, minute=30, second=0, microsecond=0),
            "currency": "USD",
            "event": "Non-Farm Payrolls",
            "impact": "high",
        })
        logger.info(f"[news] Hardcoded: NFP Friday detected — blocking {nfp_utc_h}:30 UTC")
    if today_iso in _FOMC_DATES_2026:
        events.append({
            "time_utc": now.replace(hour=18, minute=0, second=0, microsecond=0),
            "currency": "USD",
            "event": "FOMC Rate Decision",
            "impact": "high",
        })
        logger.info("[news] Hardcoded: FOMC date detected — blocking 18:00 UTC")
    _FF_CACHE.update({"events": events, "fetched_at": _t.time()})
    return events


def is_news_window() -> tuple[bool, str]:
    """
    Check if current time is within a news window.
    Returns (is_blocked, reason).
    """
    now = datetime.now(timezone.utc)
    cur = now.hour * 60 + now.minute
    try:
        for e in fetch_forexfactory_today():
            ev = e["time_utc"].hour * 60 + e["time_utc"].minute
            if -NEWS_BLOCK_MINUTES_BEFORE <= (cur - ev) <= NEWS_BLOCK_MINUTES_AFTER:
                return True, (
                    f"High impact news: {e['currency']} {e['event']} "
                    f"at {e['time_utc'].strftime('%H:%M')} UTC"
                )
    except Exception:
        pass
    return False, ""


def get_next_news_event() -> str:
    """Return description of next high-impact event today."""
    now = datetime.now(timezone.utc)
    cur = now.hour * 60 + now.minute
    try:
        upcoming = sorted(
            (e for e in fetch_forexfactory_today()
             if e["time_utc"].hour * 60 + e["time_utc"].minute > cur),
            key=lambda e: e["time_utc"].hour * 60 + e["time_utc"].minute,
        )
        if upcoming:
            e = upcoming[0]
            ev_min = e["time_utc"].hour * 60 + e["time_utc"].minute
            return (
                f"{e['currency']} {e['event']} in {ev_min - cur} minutes "
                f"({e['time_utc'].strftime('%H:%M')} UTC)"
            )
    except Exception:
        pass
    return "No more high-impact events today"


def check_upcoming_news(lookahead_minutes: int = 45) -> tuple[bool, str, int]:
    """
    Check if high-impact news is within lookahead_minutes in the future.
    Returns (news_approaching, reason, minutes_until).
    """
    now = datetime.now(timezone.utc)
    cur = now.hour * 60 + now.minute
    try:
        for e in fetch_forexfactory_today():
            ev = e["time_utc"].hour * 60 + e["time_utc"].minute
            mins_until = ev - cur
            if 0 < mins_until <= lookahead_minutes:
                return (
                    True,
                    f"High impact news: {e['currency']} {e['event']} "
                    f"at {e['time_utc'].strftime('%H:%M')} UTC",
                    mins_until,
                )
    except Exception:
        pass
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

    # Early exit: 20-candle range > 0.5% means the market is NOT ranging
    if len(candles) >= 20:
        lookback20 = candles[:20]
        range20 = (max(c["high"] for c in lookback20) - min(c["low"] for c in lookback20)) / current_price
        if range20 > 0.005:
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
    Find PDH/PDL across last 5 trading days from candles list (newest first).
    Returns dict with pdh, pdl, current_price.
    """
    if not candles:
        return {"pdh": None, "pdl": None, "current_price": None}

    current_price = candles[0]["close"]
    current_day = datetime.now(timezone.utc).date()
    past_days: dict = {}

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
                past_days.setdefault(c_day, []).append(c)
        except Exception:
            continue

    # Use up to the 5 most recent past trading days
    sorted_days = sorted(past_days.keys(), reverse=True)[:5]
    multi_day_candles = [c for day in sorted_days for c in past_days[day]]

    if multi_day_candles:
        pdh = max(c["high"] for c in multi_day_candles)
        pdl = min(c["low"] for c in multi_day_candles)
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
    "EURUSD": [(7, 21)],            # London open through NY close
    "GBPUSD": [(7, 21)],            # London open through NY close
    "XAUUSD": [(7, 21)],            # London open through NY close
    "USDJPY": [(0, 3), (7, 21)],    # Asian session + London/NY close
    "AUDUSD": [(7, 17)],            # London open through mid NY
    "NZDUSD": [(7, 17)],            # London open through mid NY
    "USDCAD": [(12, 21)],           # NY session close (most active for CAD)
    "USDCHF": [(7, 21)],            # London open through NY close
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
    passes = round(actual_rr, 1) >= min_rr
    if not passes:
        logger.warning(
            f"[RR_check] FAILED — entry={entry} sl={sl} tp1={tp1} "
            f"sl_dist={round(sl_dist, 5)} actual_rr={actual_rr} min={min_rr}"
        )
    return passes, actual_rr


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
        # TD primary for forex HTF candles
        try:
            from config import TWELVE_DATA_API_KEY
            if TWELVE_DATA_API_KEY:
                from market import normalize_symbol as _norm_sym
                td_symbol = _norm_sym(sym)
                resp = requests.get("https://api.twelvedata.com/time_series",
                    params={"symbol": td_symbol, "interval": interval_td,
                            "outputsize": outputsize, "apikey": TWELVE_DATA_API_KEY}, timeout=8)
                data = resp.json()
                if data.get("status") != "error" and "values" in data:
                    return [{"open": float(v["open"]), "high": float(v["high"]),
                             "low": float(v["low"]), "close": float(v["close"])}
                            for v in data["values"]][:outputsize]
        except Exception as e:
            logger.debug(f"MTF TD fetch error {symbol} {interval_td}: {e}")

        # yFinance fallback
        try:
            import yfinance as yf
            _MTF_FOREX_MAP = {
                'EURUSD': 'EURUSD=X', 'GBPUSD': 'GBPUSD=X', 'USDJPY': 'USDJPY=X',
                'AUDUSD': 'AUDUSD=X', 'USDCAD': 'USDCAD=X', 'NZDUSD': 'NZDUSD=X',
                'USDCHF': 'USDCHF=X', 'EURGBP': 'EURGBP=X', 'EURJPY': 'EURJPY=X',
                'GBPJPY': 'GBPJPY=X', 'XAUUSD': 'GC=F',
            }
            yf_ticker = _MTF_FOREX_MAP.get(sym)
            if not yf_ticker:
                return None
            hist = yf.Ticker(yf_ticker).history(period=period_yf, interval=interval_yf)
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
            logger.debug(f"MTF yFinance forex fetch error {symbol} {interval_yf}: {e}")
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


def detect_mtf_ob_confluence(symbol: str, current_ob: dict, direction: str,
                              candles_1h: list = None, candles_4h: list = None) -> tuple[bool, str]:
    """
    Check if the 15M order block also aligns with a 1H or 4H order block at the same level.
    Triple timeframe OB confluence is the highest probability SMC setup.
    Returns (confluence_found, description).
    Scoring applied by caller: triple→+3, 4H only→+2, 1H only→+1.
    Pass candles_1h/candles_4h (newest-first) to skip API fetches.
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
        # Use pre-fetched candles if provided, else fetch from API
        _c1h = candles_1h if candles_1h else _fetch_htf_candles(symbol, "1h", "14d", "1h", 100)
        ob_1h = _detect_ob_htf(_c1h, trend) if _c1h else None

        _c4h = candles_4h if candles_4h else _fetch_htf_candles(symbol, "4h", "30d", "4h", 60)
        ob_4h = _detect_ob_htf(_c4h, trend) if _c4h else None

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

_previous_bias: dict = {}    # {symbol: bias_string} — last known bias per symbol
_last_bias_shift: dict = {}  # {symbol: timestamp}   — last bias-shift alert per symbol


def get_daily_bias(symbol: str, candles: list = None) -> dict:
    """
    Get the daily candle bias for a symbol.
    Pass candles (newest-first) to skip the API fetch and use pre-fetched data.
    Returns dict with bias, strength, confirmed flag, and reason.
    """
    _default = {"bias": "unknown", "strength": "weak", "today_candle": "neutral",
                "confirmed": False, "reason": "Insufficient data",
                "intraday_override": False, "intraday_move_pct": 0.0}
    sym = symbol.upper()
    try:
        if candles and len(candles) >= 3:
            # Pre-fetched candles are newest-first; reverse to oldest→newest for computation
            _daily_candles = list(reversed(candles))
        else:
            _daily_candles = None

        # Build raw_candles (oldest→newest) either from pre-fetched data or API
        raw_candles = []
        ticker = _DAILY_BIAS_FUTURES_MAP.get(sym)
        if _daily_candles is not None:
            raw_candles = _daily_candles
        elif ticker:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="30d", interval="1d")
            if hist.empty or len(hist) < 3:
                return _default
            for _, row in hist.iloc[-20:].iterrows():
                raw_candles.append({
                    "open": float(row["Open"]), "high": float(row["High"]),
                    "low": float(row["Low"]),  "close": float(row["Close"]),
                })
        else:
            yf_ticker = _FOREX_YFINANCE_MAP.get(sym)
            td_success = False
            try:
                from config import TWELVE_DATA_API_KEY
                if TWELVE_DATA_API_KEY:
                    from market import normalize_symbol as _norm_sym
                    td_sym = _norm_sym(sym)
                    resp = requests.get(
                        "https://api.twelvedata.com/time_series",
                        params={"symbol": td_sym, "interval": "1day",
                                "outputsize": 20, "apikey": TWELVE_DATA_API_KEY},
                        timeout=8
                    )
                    data = resp.json()
                    if data.get("status") != "error" and "values" in data:
                        for v in reversed(data["values"]):
                            raw_candles.append({
                                "open": float(v["open"]), "high": float(v["high"]),
                                "low":  float(v["low"]),  "close": float(v["close"]),
                            })
                        td_success = True
            except Exception:
                pass
            if not td_success:
                if not yf_ticker:
                    return _default
                try:
                    import yfinance as yf
                    hist = yf.Ticker(yf_ticker).history(period="30d", interval="1d")
                    if hist.empty or len(hist) < 3:
                        return _default
                    for _, row in hist.iloc[-20:].iterrows():
                        raw_candles.append({
                            "open": float(row["Open"]), "high": float(row["High"]),
                            "low":  float(row["Low"]),  "close": float(row["Close"]),
                        })
                except Exception:
                    return _default

        if len(raw_candles) < 3:
            return _default

        def _candle_dir(c):
            return "bullish" if c["close"] > c["open"] else "bearish"

        def _body_ratio(c):
            rng = c["high"] - c["low"]
            return abs(c["close"] - c["open"]) / rng if rng else 0

        def _score(c):
            return 1 if c["close"] > c["open"] else -1

        # raw_candles are oldest→newest; last entry is today
        today       = raw_candles[-1]
        yesterday   = raw_candles[-2]
        two_days    = raw_candles[-3]

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


# ─── 17. EQUAL HIGHS/LOWS DETECTION ──────────────────────────────────────────

def detect_equal_highs_lows(candles: list, direction: str, symbol: str = "") -> tuple[bool, str]:
    """
    Detect equal highs (SELL) or equal lows (BUY) liquidity pools in last 20 candles.
    Swing high/low: candle is higher/lower than 2 candles on each side.
    Equal = within 3 pips/points of each other.
    Returns (detected, description).
    """
    if not candles or len(candles) < 5:
        return False, ""

    recent = candles[:20]
    n = len(recent)
    if n < 5:
        return False, ""

    current_price = recent[0]["close"]
    if current_price > 500:
        tol = 3.0       # gold/indices: 3 points
    elif current_price > 20:
        tol = 0.03      # JPY pairs: 3 pips
    else:
        tol = 0.0003    # forex: 3 pips

    # Offset futures prices to spot domain for display (avoids circular import from scanner)
    _SPOT_DISPLAY_OFFSETS = {"XAUUSD": -30, "XAGUSD": -0.30}
    _disp_off = _SPOT_DISPLAY_OFFSETS.get(symbol.upper(), 0) if symbol else 0

    if direction.upper() == "SELL":
        swing_highs = []
        for i in range(2, n - 2):
            h = recent[i]["high"]
            if (h > recent[i-1]["high"] and h > recent[i-2]["high"] and
                    h > recent[i+1]["high"] and h > recent[i+2]["high"]):
                swing_highs.append(h)
        for j in range(len(swing_highs)):
            cluster = [swing_highs[j]]
            for k in range(j + 1, len(swing_highs)):
                if abs(swing_highs[k] - swing_highs[j]) <= tol:
                    cluster.append(swing_highs[k])
            if len(cluster) >= 2:
                level = sum(cluster) / len(cluster)
                display_level = round(level + _disp_off, 3)
                return True, f"Equal highs liquidity pool at {display_level} — banks will sweep this"
    else:  # BUY
        swing_lows = []
        for i in range(2, n - 2):
            l = recent[i]["low"]
            if (l < recent[i-1]["low"] and l < recent[i-2]["low"] and
                    l < recent[i+1]["low"] and l < recent[i+2]["low"]):
                swing_lows.append(l)
        for j in range(len(swing_lows)):
            cluster = [swing_lows[j]]
            for k in range(j + 1, len(swing_lows)):
                if abs(swing_lows[k] - swing_lows[j]) <= tol:
                    cluster.append(swing_lows[k])
            if len(cluster) >= 2:
                level = sum(cluster) / len(cluster)
                display_level = round(level + _disp_off, 3)
                return True, f"Equal lows liquidity pool at {display_level} — banks will sweep this"

    return False, ""


# ─── 18. MARKET STRUCTURE SHIFT DETECTION ────────────────────────────────────

def detect_market_structure_shift(candles: list, direction: str) -> tuple[bool, str]:
    """
    Detect a full market structure shift (MSS) — stronger than BOS, confirms reversal.
    Bullish MSS: prior lower highs + lower lows, current close breaks above most recent swing high.
    Bearish MSS: prior higher highs + higher lows, current close breaks below most recent swing low.
    Returns (detected_in_direction, description).
    If detected against direction: returns (False, 'MSS against signal direction').
    """
    if not candles or len(candles) < 10:
        return False, ""

    recent = candles[:20]
    n = len(recent)
    if n < 8:
        return False, ""

    current_close = recent[0]["close"]

    # Analyse structure in candles[2:] — skip the 2 most recent (possible MSS candles)
    structure = recent[2:]
    sc_n = len(structure)
    if sc_n < 6:
        return False, ""

    swing_highs = []  # (index, price) — index within structure[], newest-first
    swing_lows = []

    for i in range(2, sc_n - 2):
        h = structure[i]["high"]
        l = structure[i]["low"]
        if (h > structure[i-1]["high"] and h > structure[i-2]["high"] and
                h > structure[i+1]["high"] and h > structure[i+2]["high"]):
            swing_highs.append((i, h))
        if (l < structure[i-1]["low"] and l < structure[i-2]["low"] and
                l < structure[i+1]["low"] and l < structure[i+2]["low"]):
            swing_lows.append((i, l))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return False, ""

    # Smallest index = most recent; largest index = oldest (newest-first array)
    most_recent_sh = min(swing_highs, key=lambda x: x[0])[1]
    oldest_sh = max(swing_highs, key=lambda x: x[0])[1]
    most_recent_sl = min(swing_lows, key=lambda x: x[0])[1]
    oldest_sl = max(swing_lows, key=lambda x: x[0])[1]

    lower_highs = most_recent_sh < oldest_sh
    lower_lows = most_recent_sl < oldest_sl
    higher_highs = most_recent_sh > oldest_sh
    higher_lows = most_recent_sl > oldest_sl

    if direction.upper() == "BUY":
        if lower_highs and lower_lows and current_close > most_recent_sh:
            return True, "Market structure shift confirmed — full BUY reversal"
        if higher_highs and higher_lows and current_close < most_recent_sl:
            return False, "MSS against signal direction"
    else:  # SELL
        if higher_highs and higher_lows and current_close < most_recent_sl:
            return True, "Market structure shift confirmed — full SELL reversal"
        if lower_highs and lower_lows and current_close > most_recent_sh:
            return False, "MSS against signal direction"

    return False, ""


# ─── 19. PREMIUM/DISCOUNT ZONE FILTER ────────────────────────────────────────

def check_premium_discount_zone(candles: list, entry: float, direction: str) -> tuple[bool, str]:
    """
    Filter entries by whether they sit in a premium or discount zone.
    Range of last 20 candles split at equilibrium (midpoint).
    BUY in discount (below equilibrium) → favourable.
    SELL in premium (above equilibrium) → favourable.
    Returns (is_favourable, description). Empty description = neutral/no data.
    """
    if not candles or len(candles) < 5:
        return True, ""

    recent = candles[:20]
    range_high = max(c["high"] for c in recent)
    range_low = min(c["low"] for c in recent)

    if range_high == range_low:
        return True, ""

    equilibrium = (range_high + range_low) / 2

    if direction.upper() == "BUY":
        if entry <= equilibrium:
            return True, "Entry in discount zone — buying at value"
        return False, "Buying in premium zone — unfavorable"
    else:  # SELL
        if entry >= equilibrium:
            return True, "Entry in premium zone — selling at premium"
        return False, "Selling in discount zone — unfavorable"


# ─── 20. KILL ZONE TIMING BONUS ──────────────────────────────────────────────

def is_kill_zone(symbol: str) -> tuple[bool, str]:
    """
    Check if current UTC time falls within a high-probability institutional kill zone.
    London Kill Zone: 07:00-09:00 UTC
    NY Kill Zone:     12:00-14:00 UTC
    NY Lunch Kill Zone: 14:00-16:00 UTC
    Returns (in_kill_zone, description).
    """
    hour = datetime.now(timezone.utc).hour

    if 7 <= hour < 9:
        return True, "Kill zone active — London Kill Zone — peak institutional activity"
    if 12 <= hour < 14:
        return True, "Kill zone active — NY Kill Zone — peak institutional activity"
    if 14 <= hour < 16:
        return True, "Kill zone active — NY Lunch Kill Zone — peak institutional activity"

    return False, ""


# ─── 21. MARKET STRUCTURE ANALYSIS ───────────────────────────────────────────

def analyze_market_structure(candles: list) -> dict:
    """
    Identify market structure using swing highs/lows on the last 20 candles.
    Candles must be newest-first.

    Returns dict with keys:
      structure: 'uptrend' | 'downtrend' | 'ranging'
      choch:     True if Change of Character (reversal) detected
      bos:       True if Break of Structure (continuation) detected
      last_swing_high: float | None
      last_swing_low:  float | None
    """
    _default = {
        "structure": "ranging", "choch": False, "bos": False,
        "last_swing_high": None, "last_swing_low": None,
    }

    if len(candles) < 20:
        return _default

    # MOMENTUM OVERRIDE — strong directional moves bypass swing structure check
    recent = candles[:10]  # newest first
    bullish_count = sum(1 for c in recent if c['close'] > c['open'])
    bearish_count = sum(1 for c in recent if c['close'] < c['open'])

    if bullish_count >= 8:
        return {
            "structure": "uptrend",
            "choch": False,
            "bos": True,
            "last_swing_high": candles[0]['high'],
            "last_swing_low": candles[9]['low'],
        }
    elif bearish_count >= 8:
        return {
            "structure": "downtrend",
            "choch": False,
            "bos": True,
            "last_swing_high": candles[9]['high'],
            "last_swing_low": candles[0]['low'],
        }

    swing_highs = []
    swing_lows = []
    chron = list(reversed(candles[:20]))

    for i in range(2, len(chron) - 2):
        if (chron[i]['high'] > chron[i-1]['high'] and
                chron[i]['high'] > chron[i-2]['high'] and
                chron[i]['high'] > chron[i+1]['high'] and
                chron[i]['high'] > chron[i+2]['high']):
            swing_highs.append(chron[i]['high'])

        if (chron[i]['low'] < chron[i-1]['low'] and
                chron[i]['low'] < chron[i-2]['low'] and
                chron[i]['low'] < chron[i+1]['low'] and
                chron[i]['low'] < chron[i+2]['low']):
            swing_lows.append(chron[i]['low'])

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return _default

    last_highs = swing_highs[-2:]
    last_lows = swing_lows[-2:]
    current_close = chron[-1]['close']

    higher_highs = last_highs[1] > last_highs[0]
    higher_lows = last_lows[1] > last_lows[0]
    lower_highs = last_highs[1] < last_highs[0]
    lower_lows = last_lows[1] < last_lows[0]

    if higher_highs and higher_lows:
        structure = "uptrend"
    elif lower_highs and lower_lows:
        structure = "downtrend"
    else:
        structure = "ranging"

    # CHoCH — Change of Character (trend reversal: price breaks last swing extreme)
    choch = False
    if structure == "uptrend" and current_close < last_lows[-1]:
        choch = True
        structure = "downtrend"
    elif structure == "downtrend" and current_close > last_highs[-1]:
        choch = True
        structure = "uptrend"

    # BOS — Break of Structure (trend continuation: price extends beyond last swing)
    bos = False
    if structure == "uptrend" and current_close > last_highs[-1]:
        bos = True
    elif structure == "downtrend" and current_close < last_lows[-1]:
        bos = True

    return {
        "structure": structure,
        "choch": choch,
        "bos": bos,
        "last_swing_high": last_highs[-1],
        "last_swing_low": last_lows[-1],
    }


# ─── 22. UNIFIED TRADE DIRECTION ─────────────────────────────────────────────

def get_trade_direction(symbol: str, candles_15m: list) -> tuple:
    """
    Single source of truth for trade direction — used by scanner and /bias.
    Combines daily bias with 15M market structure (swing high/low analysis).

    Returns (direction, strength):
      ('BUY',  'strong')  — bias bullish  + market structure uptrend
      ('SELL', 'strong')  — bias bearish  + market structure downtrend
      ('BUY',  'weak')    — bias neutral  + market structure uptrend
      ('SELL', 'weak')    — bias neutral  + market structure downtrend
      (None,   'ranging') — market structure ranging → skip
      (None,   'conflict')— bias conflicts with market structure → skip
    """
    daily_bias = get_daily_bias(symbol)
    ms = analyze_market_structure(candles_15m)
    structure = ms["structure"]
    bias = daily_bias.get('bias', 'neutral')

    if structure == 'ranging':
        return None, 'ranging'
    if bias == 'bullish' and structure == 'uptrend':
        return 'BUY', 'strong'
    elif bias == 'bearish' and structure == 'downtrend':
        return 'SELL', 'strong'
    elif bias == 'neutral' and structure == 'uptrend':
        return 'BUY', 'weak'
    elif bias == 'neutral' and structure == 'downtrend':
        return 'SELL', 'weak'
    elif bias == 'bullish' and structure == 'downtrend':
        return None, 'conflict'
    elif bias == 'bearish' and structure == 'uptrend':
        return None, 'conflict'
    else:
        return None, 'ranging'
