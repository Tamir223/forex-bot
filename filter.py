"""
TNL Trader Filter Module v2
All gate logic including confluence scoring.
"""

import logging
from datetime import datetime, timezone
from config import (
    CONFIDENCE_THRESHOLD,
    MAX_LIVE_EXPOSURE, MAX_SESSION_LOSSES, MAX_WEEKLY_LOSSES,
    SESSION_START_HOUR, SESSION_END_HOUR, MIN_CONFLUENCE
)

logger = logging.getLogger(__name__)

from futures_instruments import FUTURES_SYMBOLS

FUTURES_SIGNAL_KEYWORDS = list(FUTURES_SYMBOLS)
SIGNAL_KEYWORDS = ["BUY", "buy", "SELL", "sell", "ENTRY ZONE", "entry zone", "TYPE:"] + FUTURES_SIGNAL_KEYWORDS
APPROVAL_KEYWORDS = ["YES", "NO", "WIN", "LOSS"]


def is_signal_message(text: str) -> bool:
    text_upper = text.upper()
    # Standard keywords
    if any(kw in text for kw in ["BUY", "buy", "SELL", "sell", "ENTRY ZONE", "entry zone", "TYPE:"]):
        return True
    # Futures symbols
    if any(sym in text_upper for sym in FUTURES_SYMBOLS):
        if any(kw in text_upper for kw in ["BUY", "SELL", "LONG", "SHORT", "ENTRY", "STOP"]):
            return True
    return False


def is_approval_message(text: str) -> bool:
    return text.strip().upper() in APPROVAL_KEYWORDS


def calculate_confluence(analysis: dict) -> int:
    """
    Count how many technical factors align.
    Maximum score is 6.
    """
    factors = [
        analysis.get("confirmation", False),
        analysis.get("structure", False),
        analysis.get("zone", False),
        analysis.get("liquidity", False),
        analysis.get("retracement_valid", False),
        analysis.get("session_valid", False),
    ]
    score = sum(1 for f in factors if f is True)
    return score


def run_fast_gates(state: dict) -> tuple[bool, str]:
    """
    Session time and account limit gates before calling Claude.
    Returns (passed, reason)
    """
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour

    if not (SESSION_START_HOUR <= hour <= SESSION_END_HOUR):
        return False, f"Outside trading session. Active {SESSION_START_HOUR}:00 to {SESSION_END_HOUR}:00 UTC."

    if state.get("live_exposure", 0) >= MAX_LIVE_EXPOSURE:
        return False, f"Live exposure limit of {MAX_LIVE_EXPOSURE}% reached."

    if state.get("session_losses", 0) >= MAX_SESSION_LOSSES:
        return False, f"Session loss limit of {MAX_SESSION_LOSSES} reached. Stop for today."

    if state.get("weekly_losses", 0) >= MAX_WEEKLY_LOSSES:
        return False, f"Weekly loss limit of {MAX_WEEKLY_LOSSES} reached. Wait for Monday."

    return True, "Gates passed"


def run_enforcement_filter(analysis: dict) -> tuple[bool, str]:
    """
    Run all enforcement gates on Claude analysis.
    Returns (passed, block_reason)
    """
    if not analysis.get("signal_complete", False):
        return False, "incomplete_signal"

    if analysis.get("news_warning", False):
        return False, "news_warning"

    confidence = analysis.get("confidence", 0) or 0
    if confidence < CONFIDENCE_THRESHOLD:
        return False, "low_confidence"

    confluence = calculate_confluence(analysis)
    if confluence < MIN_CONFLUENCE:
        return False, "low_confluence"

    if not analysis.get("confirmation", False):
        return False, "no_confirmation"

    if analysis.get("correlation_conflict", False):
        return False, "correlation_conflict"

    if not analysis.get("session_valid", True):
        return False, "session_invalid"

    return True, "passed"
