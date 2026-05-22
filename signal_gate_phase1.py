"""
signal_gate_phase1.py — Multi-Firm Signal Pre-Check
TNL Trader — Phase 1
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from prop_firm_profiles import get_profile, InstrumentType
from drawdown_tracker import state_from_json, check_signal_allowed
from database import get_user_firm, load_challenge_state

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    allowed: bool
    reason: str
    warning: str = ""


async def check_signal_gate(user, signal_data: dict, is_news_time: bool = False) -> GateResult:
    try:
        firm_code = get_user_firm(user.id)
        profile = get_profile(firm_code)
        if not profile:
            return GateResult(allowed=True, warning="⚠️ No prop firm profile set. Use /setfirm to enable compliance checking.")
        state_json = load_challenge_state(user.id)
        if not state_json:
            return GateResult(allowed=True, warning=f"ℹ️ No challenge tracker active. Use /challenge to start tracking your {profile.name} challenge.")
        state = state_from_json(state_json)
        now = datetime.utcnow()
        is_weekend = now.weekday() >= 5
        is_overnight = signal_data.get("is_overnight", False)
        allowed, reason = check_signal_allowed(state=state, profile=profile, is_news=is_news_time, is_overnight=is_overnight, is_weekend=is_weekend)
        if not allowed:
            return GateResult(allowed=False, reason=reason)
        pair = signal_data.get("pair", "").upper()
        warning = _check_pair_compatibility(pair, profile)
        return GateResult(allowed=True, reason="✅ Cleared", warning=warning)
    except Exception as e:
        logger.error(f"Signal gate error: {e}")
        return GateResult(allowed=True, reason="Gate check error — passing through")


def _check_pair_compatibility(pair: str, profile) -> str:
    futures_symbols = {"ES", "NQ", "CL", "GC", "RTY", "YM", "MES", "MNQ", "MCL"}
    crypto_symbols = {"BTCUSD", "ETHUSD", "XRPUSD", "BTCUSDT", "ETHUSDT"}
    if pair in futures_symbols:
        if InstrumentType.FUTURES not in profile.instruments:
            return f"⚠️ {pair} is a futures contract but {profile.name} uses forex. Check your firm settings."
    elif pair in crypto_symbols:
        if InstrumentType.CRYPTO not in profile.instruments:
            return f"⚠️ {pair} is crypto but {profile.name} may not allow it."
    return ""


def format_compliance_block_in_report(gate_result: GateResult) -> str:
    if not gate_result.allowed:
        return f"\n{'─' * 30}\n🚫 *SIGNAL BLOCKED*\n{gate_result.reason}\n{'─' * 30}"
    lines = [f"\n{'─' * 30}", "✅ *Compliance: CLEARED*"]
    if gate_result.warning:
        lines.append(gate_result.warning)
    lines.append("─" * 30)
    return "\n".join(lines)
