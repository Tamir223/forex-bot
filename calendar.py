"""
APFEE Calendar Module
Checks for high impact news events in the next 2 hours.
Uses MyFXBook economic calendar (no API key required).
Falls back gracefully if unavailable.
"""

import logging
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Pairs affected by each currency's news
CURRENCY_PAIRS = {
    "USD": ["XAUUSD", "GBPUSD", "EURUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD", "US30", "NAS100"],
    "GBP": ["GBPUSD", "GBPJPY", "EURGBP"],
    "EUR": ["EURUSD", "EURJPY", "EURGBP"],
    "JPY": ["USDJPY", "GBPJPY", "EURJPY"],
    "CAD": ["USDCAD"],
    "AUD": ["AUDUSD"],
    "NZD": ["NZDUSD"],
    "CHF": ["USDCHF"],
    "XAU": ["XAUUSD"],
}

HIGH_IMPACT_KEYWORDS = [
    "nfp", "non-farm", "interest rate", "rate decision", "fomc", "cpi",
    "inflation", "gdp", "unemployment", "fed", "powell", "draghi",
    "ecb", "boe", "boj", "rba", "rbnz", "snb", "jackson hole",
    "payroll", "pmi", "retail sales", "balance of trade"
]


def get_affected_currencies(pair: str) -> list:
    """Get currencies involved in a pair"""
    if not pair:
        return []
    pair = pair.upper().replace("/", "").replace("_", "")
    currencies = []
    for currency in CURRENCY_PAIRS.keys():
        if currency in pair:
            currencies.append(currency)
    return currencies


def check_news_window(pair: str, hours_ahead: int = 2) -> dict:
    """
    Check if there is high impact news for this pair in the next N hours.
    Returns: { has_news, events, warning_message }
    """
    if not pair:
        return {"has_news": False, "events": [], "warning_message": None}

    try:
        now_utc = datetime.now(timezone.utc)
        window_end = now_utc + timedelta(hours=hours_ahead)

        # MyFXBook calendar endpoint - no auth required
        resp = requests.get(
            "https://www.myfxbook.com/api/get-economic-calendar.json",
            params={
                "start": now_utc.strftime("%Y-%m-%d %H:%M"),
                "end": window_end.strftime("%Y-%m-%d %H:%M"),
            },
            timeout=5
        )

        if resp.status_code != 200:
            logger.warning(f"Calendar API returned {resp.status_code}")
            return _fallback_check(pair)

        data = resp.json()
        if not data.get("error") is False:
            return _fallback_check(pair)

        events = data.get("calendar", [])
        affected_currencies = get_affected_currencies(pair)
        matching_events = []

        for event in events:
            impact = str(event.get("impact", "")).lower()
            currency = str(event.get("currency", "")).upper()
            title = str(event.get("title", "")).lower()

            if impact not in ["high", "3"]:
                continue

            if currency not in affected_currencies:
                continue

            matching_events.append({
                "title": event.get("title"),
                "currency": currency,
                "time": event.get("date"),
                "impact": impact
            })

        if matching_events:
            titles = ", ".join([e["title"] for e in matching_events[:3]])
            return {
                "has_news": True,
                "events": matching_events,
                "warning_message": f"High impact news in next {hours_ahead}h: {titles}"
            }

        return {"has_news": False, "events": [], "warning_message": None}

    except Exception as e:
        logger.error(f"Calendar check error: {e}")
        return _fallback_check(pair)


def _fallback_check(pair: str) -> dict:
    """
    Fallback when API is unavailable.
    Checks current UTC time against known high-impact windows.
    NFP is first Friday of month at 13:30 UTC.
    FOMC is roughly every 6 weeks at 19:00 UTC.
    """
    now = datetime.now(timezone.utc)
    warnings = []

    # Friday 13:00-14:30 UTC = NFP window (first Friday of month)
    if now.weekday() == 4 and 13 <= now.hour <= 14:
        if now.day <= 7:
            warnings.append("Possible NFP window (first Friday of month)")

    # Wednesday 18:30-19:30 UTC = FOMC window
    if now.weekday() == 2 and 18 <= now.hour <= 20:
        warnings.append("Possible FOMC window (Wednesday evening UTC)")

    if warnings:
        return {
            "has_news": True,
            "events": [{"title": w} for w in warnings],
            "warning_message": " | ".join(warnings)
        }

    return {"has_news": False, "events": [], "warning_message": None}
