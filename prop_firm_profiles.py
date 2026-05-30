"""
prop_firm_profiles.py — Universal Prop Firm Profile System
TNL Trader — Phase 1
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class DrawdownType(str, Enum):
    STATIC = "static"
    TRAILING = "trailing"
    EOD = "eod"
    BALANCE_BASED = "balance"


class InstrumentType(str, Enum):
    FOREX = "forex"
    FUTURES = "futures"
    CRYPTO = "crypto"
    OPTIONS = "options"


@dataclass
class PropFirmProfile:
    name: str
    short_code: str
    account_size: float
    profit_target: float
    profit_target_pct: float
    max_total_loss: float
    max_total_loss_pct: float
    max_daily_loss: float
    max_daily_loss_pct: float
    drawdown_type: DrawdownType
    min_trading_days: int
    max_lot_size: Optional[float]
    max_contracts: Optional[int]
    instruments: list
    allow_overnight: bool
    allow_weekend: bool
    news_trading_allowed: bool
    consistency_rule: bool
    consistency_pct: float
    two_percent_rule: bool
    trail_from_high_watermark: bool
    website: str
    phase_name: str
    notes: str = ""


PROFILES = {}

def _register(profile: PropFirmProfile):
    PROFILES[profile.short_code] = profile


_register(PropFirmProfile(
    name="FTMO $10k", short_code="ftmo",
    account_size=10000, profit_target=1000, profit_target_pct=0.10,
    max_total_loss=1000, max_total_loss_pct=0.10,
    max_daily_loss=500, max_daily_loss_pct=0.05,
    drawdown_type=DrawdownType.STATIC, min_trading_days=4,
    max_lot_size=None, max_contracts=None,
    instruments=[InstrumentType.FOREX, InstrumentType.CRYPTO],
    allow_overnight=True, allow_weekend=False, news_trading_allowed=False,
    consistency_rule=False, consistency_pct=0.0, two_percent_rule=False,
    trail_from_high_watermark=False,
    website="ftmo.com", phase_name="FTMO Challenge",
    notes="Static drawdown from starting balance. No weekend holding."
))

_register(PropFirmProfile(
    name="FTMO $25k", short_code="ftmo25",
    account_size=25000, profit_target=2500, profit_target_pct=0.10,
    max_total_loss=2500, max_total_loss_pct=0.10,
    max_daily_loss=1250, max_daily_loss_pct=0.05,
    drawdown_type=DrawdownType.STATIC, min_trading_days=4,
    max_lot_size=None, max_contracts=None,
    instruments=[InstrumentType.FOREX, InstrumentType.CRYPTO],
    allow_overnight=True, allow_weekend=False, news_trading_allowed=False,
    consistency_rule=False, consistency_pct=0.0, two_percent_rule=False,
    trail_from_high_watermark=False,
    website="ftmo.com", phase_name="FTMO Challenge",
    notes="Static drawdown from starting balance. No weekend holding."
))

_register(PropFirmProfile(
    name="Apex $50k", short_code="apex50",
    account_size=50000, profit_target=3000, profit_target_pct=0.06,
    max_total_loss=2500, max_total_loss_pct=0.05,
    max_daily_loss=0, max_daily_loss_pct=0.0,
    drawdown_type=DrawdownType.TRAILING, min_trading_days=1,
    max_lot_size=None, max_contracts=5,
    instruments=[InstrumentType.FUTURES],
    allow_overnight=False, allow_weekend=False, news_trading_allowed=True,
    consistency_rule=False, consistency_pct=0.0, two_percent_rule=False,
    trail_from_high_watermark=True,
    website="apextraderfunding.com", phase_name="Apex Evaluation",
    notes="Intraday trailing drawdown. Must flatten by 4:00 PM CT."
))

_register(PropFirmProfile(
    name="Apex $150k", short_code="apex150",
    account_size=150000, profit_target=9000, profit_target_pct=0.06,
    max_total_loss=4500, max_total_loss_pct=0.03,
    max_daily_loss=0, max_daily_loss_pct=0.0,
    drawdown_type=DrawdownType.TRAILING, min_trading_days=1,
    max_lot_size=None, max_contracts=12,
    instruments=[InstrumentType.FUTURES],
    allow_overnight=False, allow_weekend=False, news_trading_allowed=True,
    consistency_rule=False, consistency_pct=0.0, two_percent_rule=False,
    trail_from_high_watermark=True,
    website="apextraderfunding.com", phase_name="Apex Evaluation",
    notes="Trailing drawdown. Max 12 mini contracts. Flat by 4:00 PM CT daily."
))

_register(PropFirmProfile(
    name="Topstep $50k", short_code="topstep50",
    account_size=50000, profit_target=3000, profit_target_pct=0.06,
    max_total_loss=2000, max_total_loss_pct=0.04,
    max_daily_loss=1000, max_daily_loss_pct=0.02,
    drawdown_type=DrawdownType.EOD, min_trading_days=5,
    max_lot_size=None, max_contracts=5,
    instruments=[InstrumentType.FUTURES],
    allow_overnight=False, allow_weekend=False, news_trading_allowed=False,
    consistency_rule=True, consistency_pct=0.40, two_percent_rule=False,
    trail_from_high_watermark=False,
    website="topstep.com", phase_name="Trading Combine",
    notes="EOD drawdown updates daily. No news. No day > 40% of total profit."
))

_register(PropFirmProfile(
    name="Topstep $150k", short_code="topstep150",
    account_size=150000, profit_target=9000, profit_target_pct=0.06,
    max_total_loss=4500, max_total_loss_pct=0.03,
    max_daily_loss=2000, max_daily_loss_pct=0.013,
    drawdown_type=DrawdownType.EOD, min_trading_days=5,
    max_lot_size=None, max_contracts=15,
    instruments=[InstrumentType.FUTURES],
    allow_overnight=False, allow_weekend=False, news_trading_allowed=False,
    consistency_rule=True, consistency_pct=0.40, two_percent_rule=False,
    trail_from_high_watermark=False,
    website="topstep.com", phase_name="Trading Combine",
    notes="EOD drawdown. Consistency rule: no day > 40% of total profit."
))

_register(PropFirmProfile(
    name="The5%ers Hyper $10k", short_code="5ers10",
    account_size=10000, profit_target=800, profit_target_pct=0.08,
    max_total_loss=600, max_total_loss_pct=0.06,
    max_daily_loss=500, max_daily_loss_pct=0.05,
    drawdown_type=DrawdownType.BALANCE_BASED, min_trading_days=0,
    max_lot_size=3.0, max_contracts=None,
    instruments=[InstrumentType.FOREX, InstrumentType.CRYPTO],
    allow_overnight=True, allow_weekend=True, news_trading_allowed=False,
    consistency_rule=True, consistency_pct=0.50, two_percent_rule=True,
    trail_from_high_watermark=False,
    website="the5ers.com", phase_name="Hyper Growth",
    notes="Max 3 lots. Scaling plan. No news. No day > 50% of total profit."
))

_register(PropFirmProfile(
    name="The5%ers Bootcamp $100k", short_code="5ers100",
    account_size=100000, profit_target=10000, profit_target_pct=0.10,
    max_total_loss=8000, max_total_loss_pct=0.08,
    max_daily_loss=5000, max_daily_loss_pct=0.05,
    drawdown_type=DrawdownType.BALANCE_BASED, min_trading_days=0,
    max_lot_size=None, max_contracts=None,
    instruments=[InstrumentType.FOREX, InstrumentType.CRYPTO],
    allow_overnight=True, allow_weekend=True, news_trading_allowed=False,
    consistency_rule=True, consistency_pct=0.50, two_percent_rule=False,
    trail_from_high_watermark=False,
    website="the5ers.com", phase_name="Bootcamp",
    notes="Balance-based drawdown. No news. No day > 50% of total profit."
))


def get_profile(short_code: str):
    return PROFILES.get(short_code.lower())

def list_profiles():
    return list(PROFILES.values())

def get_profile_summary(profile: PropFirmProfile) -> str:
    overnight = "Yes" if profile.allow_overnight else "No (must flatten)"
    weekend = "Yes" if profile.allow_weekend else "No"
    news = "Yes" if profile.news_trading_allowed else "Blocked"
    consistency = f"No day > {int(profile.consistency_pct * 100)}% of total P&L" if profile.consistency_rule else "None"
    drawdown_labels = {
        DrawdownType.STATIC: "Static (fixed from start)",
        DrawdownType.TRAILING: "Trailing (follows peak equity)",
        DrawdownType.EOD: "End-of-day (resets daily)",
        DrawdownType.BALANCE_BASED: "Balance-based (grows with account)",
    }
    size_limit = f"{profile.max_contracts} max contracts" if profile.max_contracts else (f"{profile.max_lot_size} lots max" if profile.max_lot_size else "No limit")
    return (
        f"📋 *{profile.name}*\n"
        f"{'─' * 30}\n"
        f"💰 Account: ${profile.account_size:,.0f}\n"
        f"🎯 Target: ${profile.profit_target:,.0f} ({int(profile.profit_target_pct * 100)}%)\n"
        f"🛡 Max Loss: ${profile.max_total_loss:,.0f} ({int(profile.max_total_loss_pct * 100)}%)\n"
        f"📉 Daily Loss: ${profile.max_daily_loss:,.0f} ({int(profile.max_daily_loss_pct * 100)}%)\n"
        f"📊 Drawdown: {drawdown_labels[profile.drawdown_type]}\n"
        f"📅 Min Days: {profile.min_trading_days}\n"
        f"📦 Size Limit: {size_limit}\n"
        f"🌙 Overnight: {overnight}\n"
        f"📅 Weekend: {weekend}\n"
        f"📰 News Trading: {news}\n"
        f"📏 Consistency: {consistency}\n"
        f"🌐 {profile.website}"
    )
_register(PropFirmProfile(
    name="FundedNext Futures $50k", short_code="fundednext50",
    account_size=50000, profit_target=3000, profit_target_pct=0.06,
    max_total_loss=2500, max_total_loss_pct=0.05,
    max_daily_loss=1250, max_daily_loss_pct=0.025,
    drawdown_type=DrawdownType.EOD, min_trading_days=1,
    max_lot_size=None, max_contracts=5,
    instruments=[InstrumentType.FUTURES],
    allow_overnight=True, allow_weekend=False, news_trading_allowed=True,
    consistency_rule=False, consistency_pct=0.0, two_percent_rule=False,
    trail_from_high_watermark=False,
    website="fundednext.com", phase_name="FundedNext Evaluation",
    notes="Guaranteed 24hr payout or $1000 compensation. EOD drawdown. News trading allowed."
))

_register(PropFirmProfile(
    name="FundedNext Futures $150k", short_code="fundednext150",
    account_size=150000, profit_target=9000, profit_target_pct=0.06,
    max_total_loss=7500, max_total_loss_pct=0.05,
    max_daily_loss=3750, max_daily_loss_pct=0.025,
    drawdown_type=DrawdownType.EOD, min_trading_days=1,
    max_lot_size=None, max_contracts=15,
    instruments=[InstrumentType.FUTURES],
    allow_overnight=True, allow_weekend=False, news_trading_allowed=True,
    consistency_rule=False, consistency_pct=0.0, two_percent_rule=False,
    trail_from_high_watermark=False,
    website="fundednext.com", phase_name="FundedNext Evaluation",
    notes="Guaranteed 24hr payout or $1000 compensation. EOD drawdown. 15 contracts max."
))

_register(PropFirmProfile(
    name="Tradeify Select $50k", short_code="tradeify50",
    account_size=50000, profit_target=2500, profit_target_pct=0.05,
    max_total_loss=2500, max_total_loss_pct=0.05,
    max_daily_loss=1250, max_daily_loss_pct=0.025,
    drawdown_type=DrawdownType.EOD, min_trading_days=1,
    max_lot_size=None, max_contracts=5,
    instruments=[InstrumentType.FUTURES],
    allow_overnight=True, allow_weekend=False, news_trading_allowed=True,
    consistency_rule=False, consistency_pct=0.0, two_percent_rule=False,
    trail_from_high_watermark=False,
    website="tradeify.co", phase_name="Tradeify Select Evaluation",
    notes="Daily payouts after buffer met. No daily loss limit on Select. Path to Tradeify Elite live account after 5 payouts."
))

_register(PropFirmProfile(
    name="Tradeify Select $150k", short_code="tradeify150",
    account_size=150000, profit_target=7500, profit_target_pct=0.05,
    max_total_loss=7500, max_total_loss_pct=0.05,
    max_daily_loss=0, max_daily_loss_pct=0.0,
    drawdown_type=DrawdownType.EOD, min_trading_days=1,
    max_lot_size=None, max_contracts=15,
    instruments=[InstrumentType.FUTURES],
    allow_overnight=True, allow_weekend=False, news_trading_allowed=True,
    consistency_rule=False, consistency_pct=0.0, two_percent_rule=False,
    trail_from_high_watermark=False,
    website="tradeify.co", phase_name="Tradeify Select Evaluation",
    notes="Daily payouts. No daily loss limit. EOD trailing drawdown. Path to live CME capital."
))


def get_profile_menu() -> str:
    lines = ["🏢 *Select Your Prop Firm*\n", "Reply with the code for your firm:\n"]
    firms_grouped = {
        "Forex": ["ftmo", "ftmo25", "5ers10", "5ers100"],
        "Futures": ["apex50", "apex150", "topstep50", "topstep150", "fundednext50", "fundednext150", "tradeify50", "tradeify150"],
    }
    for category, codes in firms_grouped.items():
        lines.append(f"\n*{category}:*")
        for code in codes:
            p = PROFILES[code]
            lines.append(f"  `{code}` — {p.name}")
    lines.append("\n_Example: Send_ `/setfirm apex150`")
    return "\n".join(lines)
