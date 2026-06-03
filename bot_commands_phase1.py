"""
bot_commands_phase1.py — New Telegram Bot Commands
TNL Trader — Phase 1
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from prop_firm_profiles import get_profile, get_profile_menu, get_profile_summary, PROFILES
from drawdown_tracker import new_state, state_to_json, state_from_json, record_trade, get_status_report
from database import get_user_by_chat_id, set_user_firm, get_user_firm, save_challenge_state, load_challenge_state, reset_challenge_state, get_recent_trades

logger = logging.getLogger(__name__)


async def cmd_firmlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_profile_menu(), parse_mode="Markdown")


async def cmd_setfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ You don't have an active subscription. Visit tnltrader.com to sign up.")
        return
    if not context.args:
        await update.message.reply_text(get_profile_menu(), parse_mode="Markdown")
        return
    code = context.args[0].lower()
    profile = get_profile(code)
    if not profile:
        await update.message.reply_text(f"❌ Unknown firm code: `{code}`\n\nUse /firmlist to see all options.", parse_mode="Markdown")
        return
    set_user_firm(user.id, code)
    await update.message.reply_text(f"✅ *Firm set: {profile.name}*\n\n{get_profile_summary(profile)}\n\nUse /challenge to start tracking a new challenge.", parse_mode="Markdown")


async def cmd_firm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ No active subscription.")
        return
    firm_code = get_user_firm(user.id)
    profile = get_profile(firm_code)
    if not profile:
        await update.message.reply_text("❌ No firm set. Use /setfirm to choose your prop firm.")
        return
    await update.message.reply_text(get_profile_summary(profile), parse_mode="Markdown")


async def cmd_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ No active subscription.")
        return
    firm_code = get_user_firm(user.id)
    profile = get_profile(firm_code)
    if not profile:
        await update.message.reply_text("❌ No firm set. Use /setfirm first.\n\nExample: `/setfirm apex150`", parse_mode="Markdown")
        return
    reset_challenge_state(user.id)
    state = new_state(user.id, profile)
    save_challenge_state(user.id, firm_code, state_to_json(state))
    await update.message.reply_text(
        f"🚀 *Challenge Started!*\n\n📋 Firm: {profile.name}\n💰 Account: ${profile.account_size:,.0f}\n🎯 Target: ${profile.profit_target:,.0f}\n🛡 Max Loss: ${profile.max_total_loss:,.0f}\n📅 Min Days: {profile.min_trading_days}\n\nUse /status to check progress.\nUse /logtrade to record outcomes.\n\nGood luck! 🎯",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ No active subscription.")
        return
    state_json = load_challenge_state(user.id)
    if not state_json:
        await update.message.reply_text("❌ No challenge running. Use /challenge to start one.")
        return
    state = state_from_json(state_json)
    profile = get_profile(state.firm_code)
    if not profile:
        await update.message.reply_text("❌ Invalid firm profile. Use /challenge to restart.")
        return
    from drawdown_tracker import DrawdownTracker
    dt = DrawdownTracker()
    losses_today = dt.get_losses_today(user.id)
    paused, pause_reason = dt.is_signals_paused(user.id)
    status_text = get_status_report(state, profile)
    extra = [f"\n📊 Trades Today: {state.trades_today}"]
    if losses_today > 0:
        extra.append(f"❌ Losses Today: {losses_today}")
    if paused:
        extra.append(f"⏸ Signals Paused: Yes — {pause_reason}")
    status_text += "\n" + "\n".join(extra)
    await update.message.reply_text(status_text, parse_mode="Markdown")


async def cmd_logtrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ No active subscription.")
        return
    state_json = load_challenge_state(user.id)
    if not state_json:
        await update.message.reply_text("❌ No challenge running. Use /challenge to start one.")
        return
    if not context.args:
        await update.message.reply_text("Usage:\n`/logtrade WIN 250.50`\n`/logtrade LOSS 150`\n`/logtrade BREAKEVEN`", parse_mode="Markdown")
        return
    result = context.args[0].upper()
    if result not in ("WIN", "LOSS", "BREAKEVEN"):
        await update.message.reply_text("❌ Result must be WIN, LOSS, or BREAKEVEN.")
        return
    pnl = 0.0
    pair = None
    # Parse amount and optional pair: /logtrade WIN 37.98 XAUUSD
    if len(context.args) >= 2:
        try:
            amount = float(context.args[1])
            pnl = amount if result == "WIN" else (-amount if result == "LOSS" else 0.0)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Example: `/logtrade WIN 250 XAUUSD`", parse_mode="Markdown")
            return
    if len(context.args) >= 3:
        pair = context.args[2].upper()
    state = state_from_json(state_json)
    profile = get_profile(state.firm_code)
    state, warnings = record_trade(state, profile, pnl)
    save_challenge_state(user.id, state.firm_code, state_to_json(state))
    # Phase 4 — log with pair if provided
    if pair and result in ("WIN", "LOSS"):
        try:
            from phase4_learning import log_trade_insight, get_session
            import datetime
            log_trade_insight(user.id, {
                'pair': pair,
                'direction': '',
                'setup_type': 'OB FVG',
                'session': get_session(datetime.datetime.utcnow().hour),
                'score': 9,
                'grade': 'A+',
                'result': result,
                'pnl': pnl,
                'firm_code': state.firm_code or 'ftmo',
            })
        except Exception as e:
            logger.error(f"Phase4 log error in /logtrade: {e}")
    # Stop trade monitoring when WIN or LOSS is logged via /logtrade
    if result in ("WIN", "LOSS"):
        try:
            from trade_monitor import trade_monitor
            trade_monitor.remove_trade(user.id)
        except Exception:
            pass
    pair_str = f" | Pair: {pair}" if pair else ""
    emoji = "✅" if result == "WIN" else ("❌" if result == "LOSS" else "➖")
    msg = f"{emoji} *Trade Logged*\nResult: {result}{pair_str} | P&L: ${pnl:+,.2f}\nChallenge P&L: ${state.total_pnl:+,.2f}\nToday: ${state.today_pnl:+,.2f}"
    if warnings:
        msg += "\n\n" + "\n\n".join(warnings)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ No active subscription.")
        return
    trades = get_recent_trades(user.id, limit=10)
    if not trades:
        await update.message.reply_text("No trades logged yet. Use /logtrade after each trade.")
        return
    lines = ["📊 *Recent Trades*\n"]
    for t in trades:
        trade_id, pair, direction, grade, score, pnl, result, firm, created = t
        emoji = "✅" if result == "WIN" else ("❌" if result == "LOSS" else "➖")
        pnl_str = f"${pnl:+,.2f}" if pnl is not None else "—"
        date_str = created.strftime("%b %d") if created else "—"
        lines.append(f"{emoji} {date_str} | {pair or '—'} {direction or ''} | Grade: {grade or '—'} | {pnl_str} | {result or 'Pending'}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_resetfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ No active subscription.")
        return
    keyboard = [[InlineKeyboardButton("✅ Yes, reset my challenge", callback_data="reset_confirm"), InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel")]]
    await update.message.reply_text("⚠️ *Are you sure?*\n\nThis will erase all challenge progress including P&L, days, and drawdown tracking.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def callback_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "reset_cancel":
        await query.edit_message_text("✅ Reset cancelled.")
        return
    user = get_user_by_chat_id(str(query.from_user.id))
    if not user:
        await query.edit_message_text("❌ No active subscription.")
        return
    firm_code = get_user_firm(user.id)
    profile = get_profile(firm_code)
    reset_challenge_state(user.id)
    if profile:
        state = new_state(user.id, profile)
        save_challenge_state(user.id, firm_code, state_to_json(state))
        await query.edit_message_text(f"✅ Challenge reset for *{profile.name}*.\n\nUse /status to see your fresh start.", parse_mode="Markdown")
    else:
        await query.edit_message_text("✅ Cleared. Use /setfirm then /challenge to start.")


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ No active subscription.")
        return
    if not context.args:
        await update.message.reply_text(
            "Set your watchlist:\n`/watch XAUUSD EURUSD NQ ES`\n\nThe scanner will alert you when setups form on these instruments.",
            parse_mode="Markdown"
        )
        return
    symbols = [s.upper() for s in context.args]
    watchlist_str = ",".join(symbols)
    from database import set_user_watchlist
    set_user_watchlist(user.id, watchlist_str)
    await update.message.reply_text(
        f"✅ Watchlist updated:\n" + "\n".join(f"  • {s}" for s in symbols) +
        "\n\nYou'll get alerts every 15 minutes when setups form on these.",
        parse_mode="Markdown"
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ No active subscription.")
        return
    await update.message.reply_text("🔍 Scanning your watchlist now...")
    from database import get_user_watchlist
    from scanner import run_scan, DEFAULT_WATCHLIST
    watchlist_str = get_user_watchlist(user.id)
    watchlist = [s.strip() for s in watchlist_str.split(",")] if watchlist_str else DEFAULT_WATCHLIST
    bot = context.bot
    alerts = await run_scan(watchlist, bot, [str(update.effective_user.id)], force=True)
    if alerts == 0:
        await update.message.reply_text("✅ Scan complete — no strong setups found right now. Markets may be ranging or in low volatility.")


async def cmd_bias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ No active subscription.")
        return
    await update.message.reply_text("📊 Fetching daily bias for your watchlist...")
    from database import get_user_watchlist
    from scanner import build_bias_report, DEFAULT_WATCHLIST
    watchlist_str = get_user_watchlist(user.id)
    watchlist = [s.strip() for s in watchlist_str.split(",")] if watchlist_str else DEFAULT_WATCHLIST
    report = build_bias_report(watchlist)
    await update.message.reply_text(report, parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user_by_chat_id(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ No active subscription.")
        return
    from drawdown_tracker import DrawdownTracker
    dt = DrawdownTracker()
    dt.set_resume_override(user.id)
    await update.message.reply_text("✅ Signals resumed. Trade carefully — you have consecutive losses today.")
