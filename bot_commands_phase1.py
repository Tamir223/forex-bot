"""
bot_commands_phase1.py — New Telegram Bot Commands
TNL Trader — Phase 1
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from prop_firm_profiles import get_profile, get_profile_menu, get_profile_summary, PROFILES
from datetime import date
from drawdown_tracker import new_state, state_to_json, state_from_json, record_trade, get_status_report, _rollover_day
from database import get_user_by_chat_id, set_user_firm, get_user_firm, save_challenge_state, load_challenge_state, reset_challenge_state, get_recent_trades, get_conn

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

    # Roll over daily stats if this is a new day so today's values start at zero
    if state.today_date != date.today().isoformat():
        _rollover_day(state, profile)
        save_challenge_state(user.id, state.firm_code, state_to_json(state))

    # Trades taken today — queried directly from DB filtered by CURRENT_DATE
    trades_today_count = 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) AS cnt FROM trades
                       WHERE user_id = %s
                         AND result IN ('WIN', 'LOSS')
                         AND DATE(created_at AT TIME ZONE 'UTC') = CURRENT_DATE""",
                    (user.id,)
                )
                row = cur.fetchone()
                trades_today_count = row["cnt"] if row else 0
    except Exception:
        trades_today_count = state.trades_today

    from drawdown_tracker import DrawdownTracker
    dt = DrawdownTracker()
    losses_today = dt.get_losses_today(user.id)
    status_text = get_status_report(state, profile)
    extra = [f"\n📊 Trades Today: {trades_today_count}"]
    if losses_today > 0:
        extra.append(f"❌ Losses Today: {losses_today}")
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
    amount = 0.0
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
    # INSERT into trades table
    if result in ("WIN", "LOSS"):
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO trades (user_id, result, pair, pnl, pnl_amount, created_at)
                           VALUES (%s, %s, %s, %s, %s, NOW())""",
                        (user.id, result, pair, pnl, amount)
                    )
                conn.commit()
            logger.info(f"[logtrade] Saved {result} {pair} pnl=${amount} for user {user.id}")
        except Exception as e:
            logger.error(f"[logtrade] DB insert error: {e}")
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
    await update.message.reply_text("📊 Fetching market intelligence...")
    from database import get_user_watchlist
    from scanner import DEFAULT_WATCHLIST, fetch_all_timeframes
    from scanner_improvements import analyze_market_structure, get_trade_direction, get_daily_bias
    watchlist_str = get_user_watchlist(user.id)
    watchlist = [s.strip() for s in watchlist_str.split(",")] if watchlist_str else DEFAULT_WATCHLIST

    lines = [
        "📊 *MARKET INTELLIGENCE*",
        "━━━━━━━━━━━━━━━━━━━━",
        "`PAIR      BIAS        STRUCTURE     ACTION`",
    ]

    for sym in watchlist:
        try:
            data = fetch_all_timeframes(sym)
            candles = data.get("candles_15m", [])
            if not candles:
                lines.append(f"`{sym:<9}` -- -- ⛔ NO DATA")
                continue

            b = get_daily_bias(sym)
            bias = b.get("bias", "neutral")
            ms = analyze_market_structure(candles)
            structure = ms["structure"]
            choch = ms.get("choch", False)
            direction, strength = get_trade_direction(sym, candles)

            bias_str = ("🟢 Bullish " if bias == "bullish"
                        else "🔴 Bearish " if bias == "bearish"
                        else "⚪ Neutral  ")

            struct_str = ("📈 Uptrend   " if structure == "uptrend"
                          else "📉 Downtrend " if structure == "downtrend"
                          else "↔️ Ranging   ")

            if direction == "BUY" and strength == "strong":
                action = "✅ BUY"
            elif direction == "SELL" and strength == "strong":
                action = "✅ SELL"
            elif direction == "BUY" and strength == "weak":
                action = "⚠️ WEAK BUY"
            elif direction == "SELL" and strength == "weak":
                action = "⚠️ WEAK SELL"
            else:
                action = "⛔ SKIP"

            choch_tag = " 🔄" if choch else ""
            pair_col = f"{sym}{choch_tag}"
            lines.append(f"`{pair_col:<9}` {bias_str} {struct_str} {action}")
        except Exception:
            lines.append(f"`{sym:<9}` -- -- ⛔ ERROR")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        "🔄 = CHoCH detected — potential trend reversal",
        "✅ = bias + structure aligned  ⚠️ = structure only  ⛔ = skip",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


