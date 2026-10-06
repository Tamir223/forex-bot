"""
drawdown_tracker.py — Multi-Firm Drawdown Engine
TNL Trader — Phase 1
"""

import copy
import json
import logging
import math
from datetime import datetime, date, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, asdict, field

from prop_firm_profiles import PropFirmProfile, DrawdownType

logger = logging.getLogger(__name__)


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
    daily_loss_warn_sent: dict = field(default_factory=dict)


def _last_sunday(year, month):
    last_day = date(year, month + 1, 1) - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() + 1) % 7)


def _cet_offset_hours(now_utc):
    """Central European Time is UTC+1; summer time (UTC+2) runs from the last Sunday of March 01:00 UTC
    to the last Sunday of October 01:00 UTC. Used only if the system time zone database is missing."""
    y = now_utc.year
    start = datetime(y, 3, _last_sunday(y, 3).day, 1, tzinfo=timezone.utc)
    end = datetime(y, 10, _last_sunday(y, 10).day, 1, tzinfo=timezone.utc)
    return 2 if start <= now_utc < end else 1


def profile_today(profile, now=None):
    """The firm's current trading day. FTMO resets its daily loss at 00:00 CE(S)T, so its day is the Prague
    calendar day. Profiles without a day_tz keep the previous behavior (the server date).
    `now` is for tests; production callers leave it out."""
    tz = getattr(profile, "day_tz", "UTC") or "UTC"
    if now is None and tz == "UTC":
        return date.today()
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if tz == "UTC":
        return now.date()
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo(tz)).date()
    except Exception:
        if tz == "Europe/Prague":
            return (now + timedelta(hours=_cet_offset_hours(now))).date()
        return now.date()

def new_state(user_id: int, profile: PropFirmProfile, now=None) -> DrawdownState:
    today = profile_today(profile, now).isoformat()
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


def record_trade(state: DrawdownState, profile: PropFirmProfile, pnl: float, now=None):
    warnings = []
    today = profile_today(profile, now).isoformat()
    if state.today_date != today:
        _rollover_day(state, profile, now)
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


def _rollover_day(state, profile, now=None):
    # FTMO STATIC: daily loss limit is calculated from previous day's closing balance.
    # Update today_start_balance on every day rollover so the daily cap tracks correctly
    # as your balance grows or shrinks through the challenge.
    if profile.drawdown_type in (DrawdownType.EOD, DrawdownType.STATIC):
        state.today_start_balance = state.current_balance
    state.today_date = profile_today(profile, now).isoformat()
    state.today_pnl = 0.0
    state.trades_today = 0


def _money(text):
    try:
        v = float(str(text).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def parse_setbalance_args(args):
    """/setbalance 10489.11 [midnight=10563.35] [days=18] -> (balance, midnight, days, error)"""
    balance = midnight = days = None
    for raw in args:
        tok = str(raw).strip()
        low = tok.lower()
        if not tok:
            continue
        if low.startswith("midnight="):
            if midnight is not None:
                return None, None, None, "midnight was given twice"
            midnight = _money(tok.split("=", 1)[1])
            if midnight is None:
                return None, None, None, "midnight must be a number, for example midnight=10563.35"
        elif low.startswith("days="):
            if days is not None:
                return None, None, None, "days was given twice"
            try:
                days = int(tok.split("=", 1)[1])
            except ValueError:
                return None, None, None, "days must be a whole number, for example days=18"
            if days < 0 or days > 1000:
                return None, None, None, "days must be between 0 and 1000"
        elif "=" in tok:
            return None, None, None, "unknown option %s (use midnight= or days=)" % tok.split("=", 1)[0]
        else:
            if balance is not None:
                return None, None, None, "give only one balance"
            balance = _money(tok)
            if balance is None:
                return None, None, None, "%s is not a number" % tok
    if balance is None:
        return None, None, None, "give your account balance, for example /setbalance 10489.11"
    return balance, midnight, days, None


def balance_problem(profile, value, label="balance"):
    """None when `value` is a plausible balance for this profile's account size, otherwise a short reason."""
    if value is None or not math.isfinite(value):
        return "that %s is not a number" % label
    lo, hi = 0.5 * profile.account_size, 3.0 * profile.account_size
    if value < lo or value > hi:
        return ("a %s of $%s looks wrong for a $%s account (I accept $%s to $%s). Check for a typo."
                % (label, "{:,.2f}".format(value), "{:,.0f}".format(profile.account_size), "{:,.0f}".format(lo), "{:,.0f}".format(hi)))
    return None


def loss_floor(state, profile):
    """The balance at which the max-loss rule is breached (same formulas as _check_max_loss)."""
    dd = profile.drawdown_type
    if dd == DrawdownType.STATIC:
        return state.starting_balance - profile.max_total_loss
    if dd == DrawdownType.TRAILING:
        return state.peak_equity - profile.max_total_loss
    if dd == DrawdownType.EOD:
        return state.today_start_balance - profile.max_total_loss
    return state.starting_balance - state.starting_balance * profile.max_total_loss_pct


def apply_balance(state, profile, balance, midnight_balance=None, trading_days=None, now=None):
    """Set the tracker to the real account balance. Without midnight_balance the day's start balance is
    assumed to equal the current balance (nothing traded yet today). Breach and target flags are recomputed
    from the new numbers, so a stale or mistyped value can be corrected by running the command again."""
    today = profile_today(profile, now).isoformat()
    if state.today_date != today:
        state.trades_today = 0
    base = balance if midnight_balance is None else midnight_balance
    state.current_balance = round(balance, 2)
    state.total_pnl = round(balance - state.starting_balance, 2)
    if balance > state.peak_equity:
        state.peak_equity = round(balance, 2)
    state.today_date = today
    state.today_start_balance = round(base, 2)
    state.today_pnl = round(balance - base, 2)
    if trading_days is not None:
        state.trading_days = int(trading_days)
    state.daily_pnl_history[today] = state.today_pnl
    state.is_breached = False
    state.breach_reason = ""
    state.profit_hit = False
    warnings = []
    warnings += _check_max_loss(state, profile)
    warnings += _check_daily_loss(state, profile)
    warnings += _check_profit_target(state, profile)
    return state, warnings


def _signed(x):
    return ("+" if x >= 0 else "-") + "$" + "{:,.2f}".format(abs(x))


def setbalance_summary(state, profile):
    floor = loss_floor(state, profile)
    target = profile.profit_target
    pct = (state.total_pnl / target * 100.0) if target else 0.0
    lines = [
        "✅ *Tracker synced to your real balance*",
        "",
        "Balance: $%s (started at $%s)" % ("{:,.2f}".format(state.current_balance), "{:,.0f}".format(state.starting_balance)),
        "Profit so far: %s of $%s target (%d%%), $%s to go"
        % (_signed(state.total_pnl), "{:,.0f}".format(target), int(round(pct)), "{:,.2f}".format(max(target - state.total_pnl, 0.0))),
        "Max loss floor: $%s, $%s of room" % ("{:,.2f}".format(floor), "{:,.2f}".format(state.current_balance - floor)),
    ]
    if profile.max_daily_loss > 0:
        used = max(0.0, -state.today_pnl)
        lines.append("Daily loss limit: $%s from today's start balance $%s, $%s of room left"
                     % ("{:,.0f}".format(profile.max_daily_loss), "{:,.2f}".format(state.today_start_balance),
                        "{:,.2f}".format(max(profile.max_daily_loss - used, 0.0))))
    lines.append("Trading days: %d (minimum %d)" % (state.trading_days, profile.min_trading_days))
    lines.append("")
    lines.append("This tracks closed results only. Prop firms usually count open trades, commissions and swaps in the "
                 "daily limit too. If you have traded since the day started, add midnight=<your balance at the start of the day>.")
    return "\n".join(lines)


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


def check_daily_loss_warnings(state: DrawdownState, profile: PropFirmProfile) -> list:
    """Informational Telegram warnings at 60% and 80% of the daily loss limit.
    Fires once per threshold per day. Completely separate from the compliance
    hard-block in _check_daily_loss / check_signal_allowed — those are untouched."""
    if profile.max_daily_loss <= 0 or state.today_pnl >= 0:
        return []

    today = state.today_date
    daily_loss = -state.today_pnl
    pct_used = daily_loss / profile.max_daily_loss

    already_sent = state.daily_loss_warn_sent.get(today, [])
    messages = []

    if pct_used >= 0.80 and "80pct" not in already_sent:
        state.daily_loss_warn_sent.setdefault(today, []).extend(
            k for k in ("60pct", "80pct") if k not in already_sent
        )
        messages.append(
            f"⚠️ Daily loss tracking: ${daily_loss:,.0f} of ${profile.max_daily_loss:,.0f} "
            f"daily limit used today. Consider stopping for the day."
        )
    elif pct_used >= 0.60 and "60pct" not in already_sent:
        state.daily_loss_warn_sent.setdefault(today, []).append("60pct")
        messages.append(
            f"⚠️ Daily loss tracking: ${daily_loss:,.0f} of ${profile.max_daily_loss:,.0f} "
            f"daily limit used today. 2 more losing trades at current sizing would approach "
            f"the limit. This is informational only — no signals are being blocked."
        )

    return messages


def check_signal_allowed(state: DrawdownState, profile: PropFirmProfile, is_news: bool = False, is_overnight: bool = False, is_weekend: bool = False, now=None):
    # A new trading day resets today's P&L. Judge on a rolled copy so yesterday's loss cannot block today's
    # signals just because nothing has triggered the rollover yet. The stored state is left untouched.
    if state.today_date != profile_today(profile, now).isoformat():
        state = copy.deepcopy(state)
        _rollover_day(state, profile, now)
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
