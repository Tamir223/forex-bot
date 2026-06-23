"""
drawdown_tracker.py — Multi-Firm Drawdown Engine
TNL Trader — Phase 1
"""

import json
import logging
from datetime import datetime, date, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, asdict

from prop_firm_profiles import PropFirmProfile, DrawdownType

logger = logging.getLogger(__name__)

# In-memory resume overrides: user_id -> midnight UTC expiry
_resume_overrides: dict = {}


class DrawdownTracker:
    def get_losses_today(self, user_id: int) -> int:
        from database import get_conn
        try:
            today_midnight = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT COUNT(*) AS cnt FROM trades
                           WHERE user_id = %s AND result = 'LOSS'
                           AND created_at >= %s""",
                        (user_id, today_midnight)
                    )
                    row = cur.fetchone()
            return int(row['cnt']) if row else 0
        except Exception as e:
            logger.error(f"get_losses_today error: {e}")
            return 0

    def is_signals_paused(self, user_id: int) -> tuple:
        global _resume_overrides
        override_expiry = _resume_overrides.get(user_id)
        if override_expiry:
            now = datetime.now(timezone.utc)
            if now < override_expiry:
                return False, ""
            else:
                del _resume_overrides[user_id]

        losses_today = self.get_losses_today(user_id)

        # Prefer challenge_state.today_pnl — kept current by /logtrade via record_trade().
        # user_state.daily_pnl is a separate field that /logtrade never writes to.
        daily_pnl = 0.0
        try:
            from database import load_challenge_state
            _cs_json = load_challenge_state(user_id)
            if _cs_json:
                daily_pnl = state_from_json(_cs_json).today_pnl
        except Exception:
            pass
        if daily_pnl == 0.0:
            # Fallback: user_state.daily_pnl (may be stale but better than nothing)
            from database import get_state
            daily_pnl = get_state(user_id).daily_pnl

        logger.info(f"[pause_check] user={user_id} losses={losses_today} daily_pnl={daily_pnl:.2f}")

        if losses_today >= 2:
            if daily_pnl < 0:
                return True, "2 losses today — signals paused. Type /resume to continue trading."
            else:
                # Net positive despite 2 losses — warn but allow signals through
                pnl_str = f"+${daily_pnl:.2f}"
                return False, f"⚠️ 2 losses today but still net positive ({pnl_str}). Trading carefully."

        today_loss = -daily_pnl if daily_pnl < 0 else 0
        if today_loss >= 200:
            return True, "Daily loss $200 reached — signals paused. Type /resume to continue."

        return False, ""

    def set_resume_override(self, user_id: int):
        now = datetime.now(timezone.utc)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        _resume_overrides[user_id] = midnight


@dataclass
class DrawdownState:
    user_id: int
    firm_code: str
    account_size: float
    starting_balance: float
    current_balance: float
    peak_equity: float
    today_start_balance: float
    today_date: str
    total_pnl: float
    today_pnl: float
    trading_days: int
    trades_today: int
    is_breached: bool
    breach_reason: str
    profit_hit: bool
    daily_pnl_history: dict


def new_state(user_id: int, profile: PropFirmProfile) -> DrawdownState:
    today = date.today().isoformat()
    return DrawdownState(
        user_id=user_id, firm_code=profile.short_code,
        account_size=profile.account_size, starting_balance=profile.account_size,
        current_balance=profile.account_size, peak_equity=profile.account_size,
        today_start_balance=profile.account_size, today_date=today,
        total_pnl=0.0, today_pnl=0.0, trading_days=0, trades_today=0,
        is_breached=False, breach_reason="", profit_hit=False, daily_pnl_history={},
    )

def state_to_json(state: DrawdownState) -> str:
    return json.dumps(asdict(state))

def state_from_json(data: str) -> DrawdownState:
    return DrawdownState(**json.loads(data))


def record_trade(state: DrawdownState, profile: PropFirmProfile, pnl: float):
    warnings = []
    today = date.today().isoformat()
    if state.today_date != today:
        _rollover_day(state, profile)
    state.current_balance += pnl
    state.total_pnl += pnl
    state.today_pnl += pnl
    state.trades_today += 1
    if state.trades_today == 1:
        state.trading_days += 1
    if state.current_balance > state.peak_equity:
        state.peak_equity = state.current_balance
    state.daily_pnl_history[today] = state.today_pnl
    warnings += _check_max_loss(state, profile)
    warnings += _check_daily_loss(state, profile)
    warnings += _check_consistency(state, profile)
    warnings += _check_profit_target(state, profile)
    return state, warnings


def _rollover_day(state, profile):
    # FTMO STATIC: daily loss limit is calculated from previous day's closing balance.
    # Update today_start_balance on every day rollover so the daily cap tracks correctly
    # as your balance grows or shrinks through the challenge.
    if profile.drawdown_type in (DrawdownType.EOD, DrawdownType.STATIC):
        state.today_start_balance = state.current_balance
    state.today_date = date.today().isoformat()
    state.today_pnl = 0.0
    state.trades_today = 0


def _check_max_loss(state, profile):
    warnings = []
    if profile.drawdown_type == DrawdownType.STATIC:
        floor = state.starting_balance - profile.max_total_loss
        current_drawdown = state.starting_balance - state.current_balance
        pct_used = current_drawdown / profile.max_total_loss if profile.max_total_loss else 0
        if state.current_balance <= floor:
            state.is_breached = True
            state.breach_reason = f"Max drawdown breached — ${current_drawdown:,.2f} loss exceeds ${profile.max_total_loss:,.0f} limit"
            warnings.append(f"🚨 *CHALLENGE BREACHED* — Max loss exceeded!\nLoss: ${current_drawdown:,.2f} / Limit: ${profile.max_total_loss:,.0f}")
        elif pct_used >= 0.80:
            remaining = state.current_balance - floor
            warnings.append(f"⚠️ *Max Drawdown Warning* — ${remaining:,.2f} remaining (80%+ used)")
    elif profile.drawdown_type == DrawdownType.TRAILING:
        floor = state.peak_equity - profile.max_total_loss
        current_drawdown = state.peak_equity - state.current_balance
        pct_used = current_drawdown / profile.max_total_loss if profile.max_total_loss else 0
        if state.current_balance <= floor:
            state.is_breached = True
            state.breach_reason = f"Trailing drawdown breached — ${current_drawdown:,.2f} from peak ${state.peak_equity:,.2f}"
            warnings.append(f"🚨 *CHALLENGE BREACHED* — Trailing drawdown hit!\nPeak: ${state.peak_equity:,.2f} | Current: ${state.current_balance:,.2f}")
        elif pct_used >= 0.80:
            remaining = state.current_balance - floor
            warnings.append(f"⚠️ *Trailing Drawdown Warning* — ${remaining:,.2f} buffer remaining\nPeak: ${state.peak_equity:,.2f}")
    elif profile.drawdown_type == DrawdownType.EOD:
        floor = state.today_start_balance - profile.max_total_loss
        current_drawdown = state.today_start_balance - state.current_balance
        if state.current_balance <= floor:
            state.is_breached = True
            state.breach_reason = "Max drawdown breached — balance fell below EOD floor"
            warnings.append("🚨 *CHALLENGE BREACHED* — EOD drawdown breached!")
        elif profile.max_total_loss and current_drawdown / profile.max_total_loss >= 0.80:
            remaining = state.current_balance - floor
            warnings.append(f"⚠️ *Drawdown Warning* — ${remaining:,.2f} remaining today")
    elif profile.drawdown_type == DrawdownType.BALANCE_BASED:
        max_loss = state.starting_balance * profile.max_total_loss_pct
        total_drawdown = state.starting_balance - state.current_balance
        if total_drawdown >= max_loss:
            state.is_breached = True
            state.breach_reason = f"Max drawdown breached — ${total_drawdown:,.2f} loss"
            warnings.append("🚨 *CHALLENGE BREACHED* — Max drawdown exceeded!")
    return warnings


def _check_daily_loss(state, profile):
    warnings = []
    if profile.max_daily_loss <= 0:
        return warnings
    daily_loss = -state.today_pnl if state.today_pnl < 0 else 0
    pct_used = daily_loss / profile.max_daily_loss if profile.max_daily_loss else 0
    if daily_loss >= profile.max_daily_loss:
        state.is_breached = True
        state.breach_reason = f"Daily loss limit breached — ${daily_loss:,.2f} loss today"
        warnings.append(f"🚨 *DAILY LIMIT BREACHED*\nToday's loss: ${daily_loss:,.2f}\nDaily limit: ${profile.max_daily_loss:,.0f}\n⛔ Stop trading for today immediately.")
    elif pct_used >= 0.75:
        remaining = profile.max_daily_loss - daily_loss
        warnings.append(f"⚠️ *Daily Loss Warning*\nUsed: ${daily_loss:,.2f} / ${profile.max_daily_loss:,.0f}\nRemaining today: ${remaining:,.2f}")
    return warnings


def _check_consistency(state, profile):
    warnings = []
    if not profile.consistency_rule or state.total_pnl <= 0:
        return warnings
    max_allowed_today = state.total_pnl * profile.consistency_pct
    if state.today_pnl > max_allowed_today:
        warnings.append(f"⚠️ *Consistency Rule Alert*\nToday's profit: ${state.today_pnl:,.2f}\nMax allowed ({int(profile.consistency_pct * 100)}% of total): ${max_allowed_today:,.2f}\nConsider stopping for the day.")
    return warnings


def _check_profit_target(state, profile):
    warnings = []
    if not state.profit_hit and state.total_pnl >= profile.profit_target:
        state.profit_hit = True
        ready = state.trading_days >= profile.min_trading_days
        warnings.append(
            f"🎉 *PROFIT TARGET HIT!*\nP&L: ${state.total_pnl:,.2f} / Target: ${profile.profit_target:,.0f}\nTrading days: {state.trading_days} / Required: {profile.min_trading_days}\n"
            + ("✅ Challenge PASSED! Submit for review." if ready else f"⏳ Need {profile.min_trading_days - state.trading_days} more trading day(s).")
        )
    return warnings


def get_status_report(state: DrawdownState, profile: PropFirmProfile) -> str:
    target_pct = (state.total_pnl / profile.profit_target * 100) if profile.profit_target else 0
    if profile.drawdown_type == DrawdownType.TRAILING:
        drawdown_used = max(0.0, state.peak_equity - state.current_balance)
        drawdown_label = f"Trailing (peak: ${state.peak_equity:,.2f})"
    elif profile.drawdown_type == DrawdownType.EOD:
        drawdown_used = max(0.0, state.today_start_balance - state.current_balance)
        drawdown_label = "EOD (resets each day)"
    else:
        drawdown_used = max(0.0, state.starting_balance - state.current_balance)
        drawdown_label = "Static" if profile.drawdown_type == DrawdownType.STATIC else "Balance-based"
    drawdown_remaining = profile.max_total_loss - drawdown_used
    daily_loss_today = min(state.today_pnl, 0)
    daily_remaining = profile.max_daily_loss + daily_loss_today if profile.max_daily_loss > 0 else None
    status_icon = "🚨" if state.is_breached else ("🎉" if state.profit_hit else "🟢")
    lines = [
        f"{status_icon} *{profile.name} — Challenge Status*",
        f"{'─' * 32}",
        f"💰 Balance: ${state.current_balance:,.2f}",
        f"📈 Total P&L: ${state.total_pnl:+,.2f}",
        f"🎯 Target: ${state.total_pnl:,.2f} / ${profile.profit_target:,.0f} ({target_pct:.1f}%)",
        f"📉 Drawdown Used: ${drawdown_used:,.2f} ({drawdown_label})",
        f"🛡 Buffer Remaining: ${drawdown_remaining:,.2f}",
    ]
    if daily_remaining is not None:
        lines.append(f"📅 Daily Remaining: ${daily_remaining:,.2f}")
    lines += [
        f"🗓 Trading Days: {state.trading_days} / {profile.min_trading_days} required",
        f"📊 Today's P&L: ${state.today_pnl:+,.2f}",
    ]
    if state.is_breached:
        lines += [f"\n🚨 *CHALLENGE FAILED*", f"Reason: {state.breach_reason}"]
    elif state.profit_hit:
        if state.trading_days >= profile.min_trading_days:
            lines.append(f"\n✅ *Target hit — ready to submit!*")
        else:
            lines.append(f"\n⏳ Target hit — need {profile.min_trading_days - state.trading_days} more day(s)")
    return "\n".join(lines)


def check_signal_allowed(state: DrawdownState, profile: PropFirmProfile, is_news: bool = False, is_overnight: bool = False, is_weekend: bool = False):
    if state.is_breached:
        return False, f"🚫 Challenge is already breached — {state.breach_reason}"
    if is_news and not profile.news_trading_allowed:
        return False, "🚫 News trading blocked by your firm's rules"
    if is_overnight and not profile.allow_overnight:
        return False, f"🚫 Overnight holding not allowed ({profile.name})"
    if is_weekend and not profile.allow_weekend:
        return False, "🚫 Weekend holding not allowed"
    if profile.max_daily_loss > 0:
        daily_loss = -state.today_pnl if state.today_pnl < 0 else 0
        if daily_loss >= profile.max_daily_loss * 0.95:
            return False, f"🚫 Near daily limit (${daily_loss:,.2f} used of ${profile.max_daily_loss:,.0f})"
    if profile.drawdown_type == DrawdownType.TRAILING:
        floor = state.peak_equity - profile.max_total_loss
        buffer = state.current_balance - floor
        if buffer < profile.max_total_loss * 0.10:
            return False, f"🚫 Trailing drawdown buffer critically low — ${buffer:,.2f} remaining"
    return True, "✅ Signal cleared"
