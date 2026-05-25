import asyncio
"""
TNL Trader Multi-User Bot
One central bot serving all subscribers.
Each message is routed by chat_id to the correct user context.
"""

import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from database import (
    get_user_by_chat_id, get_state, link_telegram,
    log_trade_opened, log_trade_win, log_trade_loss,
    get_provider_stats, update_provider_result,
    log_trade, get_user_trades, Trade, get_user_firm
)
from prop_firm_profiles import get_profile
from notifications import send_subscription_confirmed
from filter import is_signal_message, is_approval_message, run_fast_gates, run_enforcement_filter
from claude import analyze_signal
from report import (
    execute_report, blocked_report, fast_gate_blocked,
    trade_executed, trade_skipped, trade_logged_win,
    trade_logged_loss, not_subscribed_message, status_report
)
from trading_calendar import is_friday_close_warning
import os
from bot_commands_phase1 import (
    cmd_firmlist, cmd_setfirm, cmd_firm,
    cmd_challenge, cmd_status, cmd_logtrade,
    cmd_history, cmd_resetfirm, callback_reset,
    cmd_watch, cmd_scan
)

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

last_analysis = {}
last_trade_id = {}
signal_queue: asyncio.Queue = asyncio.Queue()

# Deduplication cache — prevents same message being processed twice
_seen_message_ids = set()
MAX_SEEN_IDS = 500  # keep memory bounded


async def send(context, chat_id: str, text: str):
    try:
        await context.bot.send_message(chat_id=int(chat_id), text=text)
    except Exception as e:
        logger.error(f"Send error to {chat_id}: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    args = context.args
    if not args:
        await update.message.reply_text(
            "Welcome to TNL Trader.\n\nTo activate your account, use the link sent to your email after purchase.\nVisit tnltrader.com to subscribe."
        )
        return
    token = args[0]
    user = _verify_activation_token(token)
    if not user:
        await update.message.reply_text("Invalid or expired activation link.\nPlease check your email or visit tnltrader.com for support.")
        return
    link_telegram(user.id, chat_id)
    await send_subscription_confirmed(chat_id, user.plan_tier, user.email)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text(not_subscribed_message())
        return
    plan = user.plan_tier
    if plan == "basic":
        trades = _get_recent_trades(user.id, days=30, limit=10)
    else:
        trades = get_user_trades(user.id, limit=10)
    if not trades:
        await update.message.reply_text("No trades logged yet. Start forwarding signals.")
        return
    lines = ["📊 RECENT TRADES\n"]
    for t in trades:
        result_emoji = {"WIN": "✅", "LOSS": "❌", "PENDING": "⏳", "SKIPPED": "⏭"}.get(t["result"], "")
        lines.append(f"{result_emoji} {t['pair']} {t['direction']} | {t['grade']} | {t['confidence']}/10 | {t['signal_source']}")
    if plan in ("pro", "elite"):
        providers = list({t["signal_source"] for t in trades
                          if t.get("signal_source") and t["signal_source"] != "UNKNOWN"})
        if providers:
            lines.append("\n📈 PROVIDER PERFORMANCE\n")
            for p in sorted(providers):
                stats = get_provider_stats(user.id, p)
                if stats.get("total_trades", 0) > 0:
                    lines.append(f"{p}: {stats.get('win_rate', 0)}% WR | {stats.get('total_trades', 0)} trades")
    else:
        lines.append(
            "\n📊 Provider performance stats are available on Pro and Elite plans. "
            "Upgrade at tnltrader.com"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "TNL Trader Commands\n\n"
        "/status — account state and limits\n"
        "/stats — recent trades and performance\n"
        "/upgrade — upgrade your plan\n"
        "/cancel — manage or cancel your subscription\n"
        "/help — this menu\n\n"
        "Replies after a report:\n"
        "YES — execute the trade\n"
        "NO — skip the trade\n"
        "WIN — mark last trade as win\n"
        "LOSS — mark last trade as loss"
    )


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text(not_subscribed_message())
        return
    plan = user.plan_tier
    if plan == "elite":
        await update.message.reply_text(
            "✅ You are already on our highest plan — Elite.\n"
            "You have access to every feature TNL Trader offers."
        )
        return

    plan_labels = {"basic": "Basic ($47/mo)", "pro": "Pro ($97/mo)"}
    current_label = plan_labels.get(plan, plan.capitalize())

    has_stripe_sub = (
        user.stripe_subscription_id
        and user.stripe_subscription_id != "founder"
    )

    if has_stripe_sub:
        await update.message.reply_text(
            "📈 UPGRADE YOUR PLAN\n\n"
            f"Current plan: {current_label}\n\n"
            "To upgrade your existing subscription visit your billing portal — "
            "you can switch plans there without being charged again:\n\n"
            "https://billing.stripe.com/p/login/fZu3cwesK8NEflccqOfjG00\n\n"
            "Or start a new subscription:\n"
            "Pro: https://tnltrader.com/checkout?plan=pro\n"
            "Elite: https://tnltrader.com/checkout?plan=elite"
        )
    else:
        if plan == "basic":
            await update.message.reply_text(
                "📈 UPGRADE YOUR PLAN\n\n"
                "Current plan: Basic ($47/mo)\n\n"
                "Upgrade options:\n"
                "- Pro — $97/mo — Unlimited signals + Provider stats\n"
                "- Elite — $197/mo — Everything + Priority analysis\n\n"
                "To upgrade:\n"
                "Pro: https://tnltrader.com/checkout?plan=pro\n"
                "Elite: https://tnltrader.com/checkout?plan=elite\n\n"
                "Your billing will be updated automatically."
            )
        elif plan == "pro":
            await update.message.reply_text(
                "📈 UPGRADE YOUR PLAN\n\n"
                "Current plan: Pro ($97/mo)\n\n"
                "Upgrade to Elite — $197/mo\n"
                "- Priority analysis on every signal\n"
                "- Highest confidence threshold\n"
                "- ⚡ Elite badge on every report\n\n"
                "Upgrade here: https://tnltrader.com/checkout?plan=elite"
            )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "To cancel your TNL Trader subscription, visit your billing portal below. "
        "You will keep full access until the end of your current billing period.\n\n"
        "https://billing.stripe.com/p/login/fZu3cwesK8NEflccqOfjG00\n\n"
        "If you have any issues email support@tnltrader.com"
    )


async def process_signal_queue():
    while True:
        item = await signal_queue.get()
        try:
            text = item["text"]
            chat_id = item["chat_id"]
            bot = item["bot"]
            user = item["user"]
            state_dict = item["state_dict"]

            analysis = analyze_signal(text, state_dict, user.id)
            if not analysis:
                await bot.send_message(chat_id=int(chat_id), text="⚠️ Analysis failed. Please try again.")
                continue

            if user.plan_tier == "elite" and analysis.get("confidence") is not None:
                analysis["confidence"] = min(10, analysis["confidence"] + 1)

            # Firm-aware risk cap
            firm_code = get_user_firm(user.id)
            profile = get_profile(firm_code)
            if profile:
                max_daily_pct = profile.max_daily_loss_pct * 100  # e.g. 5.0 for FTMO
                signal_risk = analysis.get("risk_percent", 0) or 0
                firm_label = profile.name
                if max_daily_pct > 0 and signal_risk > (max_daily_pct / 5):
                    analysis["risk_percent"] = round(max_daily_pct / 5, 2)
                priority_header = f"⚡ ELITE PRIORITY ANALYSIS\n🏆 {firm_label} MODE ACTIVE — Risk capped at {analysis.get('risk_percent', 1.0)}%\n"
            else:
                priority_header = "⚡ ELITE PRIORITY ANALYSIS\n"

            last_analysis[chat_id] = analysis
            trade = Trade(
                user_id=user.id,
                pair=analysis.get("pair", ""),
                direction=analysis.get("direction", ""),
                grade=analysis.get("grade", ""),
                confidence=analysis.get("confidence", 0),
                risk_percent=analysis.get("risk_percent", 0),
                signal_source=analysis.get("signal_source", "UNKNOWN"),
                entry_zone=analysis.get("entry_zone"),
                stop_loss=analysis.get("stop_loss"),
                result="PENDING"
            )
            trade_id = log_trade(trade)
            last_trade_id[chat_id] = trade_id

            if analysis.get("decision") == "BLOCK":
                await bot.send_message(chat_id=int(chat_id), text=blocked_report(analysis, "claude_block"))
                from database import update_trade_result
                if trade_id:
                    update_trade_result(trade_id, "BLOCKED")
            else:
                passed, enforce_reason = run_enforcement_filter(analysis)
                if not passed:
                    await bot.send_message(chat_id=int(chat_id), text=blocked_report(analysis, enforce_reason))
                    from database import update_trade_result
                    if trade_id:
                        update_trade_result(trade_id, "BLOCKED")
                else:
                    report = execute_report(analysis)
                    if user.plan_tier == "elite":
                        report = priority_header + report
                    await bot.send_message(chat_id=int(chat_id), text=report)
        except Exception as e:
            logger.error(f"Signal queue worker error: {e}")
        finally:
            signal_queue.task_done()
            await asyncio.sleep(0.5)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    chat_id = str(update.message.chat_id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text(not_subscribed_message())
        return
    if is_approval_message(text):
        reply = text.upper()
        analysis = last_analysis.get(chat_id, {})
        risk = analysis.get("risk_percent", 0.35)
        provider = analysis.get("signal_source", "UNKNOWN")
        trade_id = last_trade_id.get(chat_id)
        if reply == "YES":
            log_trade_opened(user.id, risk)
            if trade_id:
                from database import update_trade_result
                update_trade_result(trade_id, "PENDING")
            await send(context, chat_id, trade_executed())
            if is_friday_close_warning():
                await send(context, chat_id, "⚠️ FRIDAY WARNING: FTMO does not allow holding positions over the weekend. Make sure to close this trade before market close at 21:00 UTC tonight.")
        elif reply == "NO":
            if trade_id:
                from database import update_trade_result
                update_trade_result(trade_id, "SKIPPED")
            await send(context, chat_id, trade_skipped())
        elif reply == "WIN":
            log_trade_win(user.id, risk)
            update_provider_result(user.id, provider, won=True)
            if trade_id:
                from database import update_trade_result
                update_trade_result(trade_id, "WIN")
            # Auto-update challenge tracker
            from database import load_challenge_state, save_challenge_state
            from prop_firm_profiles import get_profile as gp
            from drawdown_tracker import state_from_json, state_to_json, record_trade
            _state_json = load_challenge_state(user.id)
            if _state_json:
                _state = state_from_json(_state_json)
                _profile = gp(_state.firm_code)
                pnl_est = analysis.get("risk_percent", 0.35) / 100 * (_profile.account_size if _profile else 10000) * 2
                _state, _warns = record_trade(_state, _profile, pnl_est)
                save_challenge_state(user.id, _state.firm_code, state_to_json(_state))
                if _warns:
                    for w in _warns:
                        await send(context, chat_id, w)
            await send(context, chat_id, trade_logged_win())
        elif reply == "LOSS":
            log_trade_loss(user.id, risk)
            update_provider_result(user.id, provider, won=False)
            if trade_id:
                from database import update_trade_result
                update_trade_result(trade_id, "LOSS")
            # Auto-update challenge tracker
            from database import load_challenge_state, save_challenge_state
            from prop_firm_profiles import get_profile as gp
            from drawdown_tracker import state_from_json, state_to_json, record_trade
            _state_json = load_challenge_state(user.id)
            if _state_json:
                _state = state_from_json(_state_json)
                _profile = gp(_state.firm_code)
                pnl_est = -(analysis.get("risk_percent", 0.35) / 100 * (_profile.account_size if _profile else 10000))
                _state, _warns = record_trade(_state, _profile, pnl_est)
                save_challenge_state(user.id, _state.firm_code, state_to_json(_state))
                if _warns:
                    for w in _warns:
                        await send(context, chat_id, w)
            await send(context, chat_id, trade_logged_loss())
        elif reply == "BREAKEVEN":
            if trade_id:
                from database import update_trade_result
                update_trade_result(trade_id, "BREAKEVEN")
            await send(context, chat_id, "➖ Breakeven logged.")
        return
    if not is_signal_message(text):
        return
    plan_limits = {"basic": 10, "pro": 999, "elite": 999}
    state = get_state(user.id)
    daily_limit = plan_limits.get(user.plan_tier, 10)
    if state.trades_today >= daily_limit:
        await send(context, chat_id, f"Daily signal limit reached for your {user.plan_tier} plan.\nUpgrade at tnltrader.com for more signals.")
        return
    passed, gate_reason = run_fast_gates(state.__dict__ if hasattr(state, "__dict__") else state)
    if not passed:
        await send(context, chat_id, fast_gate_blocked(gate_reason))
        return
    firm_code = get_user_firm(user.id)
    profile = get_profile(firm_code)
    state_dict = {
        "trades_today": state.trades_today,
        "open_trades": state.open_trades,
        "live_exposure": state.live_exposure,
        "session_losses": state.session_losses,
        "weekly_losses": state.weekly_losses,
        "daily_pnl": state.daily_pnl,
        "account_size": profile.account_size if profile else 10000.0,
        "risk_percent": 1.0,
        "max_contracts": profile.max_contracts if profile else None,
    }
    # Deduplicate — skip if we've seen this message ID before
    msg_id = update.message.message_id
    if msg_id in _seen_message_ids:
        logger.info(f"Duplicate message {msg_id} — skipping")
        return
    _seen_message_ids.add(msg_id)
    if len(_seen_message_ids) > MAX_SEEN_IDS:
        _seen_message_ids.pop()

    await signal_queue.put({
        "text": text,
        "chat_id": chat_id,
        "bot": context.bot,
        "user": user,
        "state_dict": state_dict,
    })
    qsize = signal_queue.qsize()
    if qsize > 10:
        await send(context, chat_id, "⚠️ High demand right now — your signal is queued. You will receive your report within 2 minutes.")
    elif qsize > 2:
        await send(context, chat_id, "⏳ Signal queued for analysis...")
    else:
        await send(context, chat_id, "⏳ Analyzing signal...")


def _get_recent_trades(user_id: int, days: int = 30, limit: int = 10) -> list:
    try:
        from datetime import datetime, timedelta
        from database import get_conn
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT pair, direction, grade, confidence, signal_source, result
                       FROM trades
                       WHERE user_id = %s AND created_at > %s
                       ORDER BY created_at DESC
                       LIMIT %s""",
                    (user_id, cutoff, limit)
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Recent trades query failed: {e}")
    return []


def _verify_activation_token(token: str):
    try:
        from database import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT u.* FROM users u
                       JOIN activation_tokens t ON t.user_id = u.id
                       WHERE t.token = %s AND t.used = FALSE
                       AND t.expires_at > NOW()""",
                    (token,)
                )
                row = cur.fetchone()
                if row:
                    cur.execute("UPDATE activation_tokens SET used = TRUE WHERE token = %s", (token,))
                    conn.commit()
                    from database import User
                    return User(**row)
    except Exception as e:
        logger.error(f"Token verification error: {e}")
    return None


async def set_bot_commands(app):
    from telegram import BotCommand
    commands = [
        BotCommand("start", "Get started with TNL Trader"),
        BotCommand("help", "Show all available commands"),
        BotCommand("setfirm", "Set your prop firm (e.g. /setfirm apex150)"),
        BotCommand("firm", "Show your current firm profile"),
        BotCommand("firmlist", "List all supported prop firms"),
        BotCommand("challenge", "Start a new challenge tracker"),
        BotCommand("status", "Check your challenge progress"),
        BotCommand("logtrade", "Log a trade result (e.g. /logtrade WIN 500)"),
        BotCommand("history", "View your last 10 trades"),
        BotCommand("resetfirm", "Reset your challenge tracker"),
        BotCommand("watch", "Set your scanner watchlist (e.g. /watch ES NQ XAUUSD)"),
        BotCommand("scan", "Trigger an instant manual scan of your watchlist"),
        BotCommand("cancel", "Cancel current operation"),
    ]
    await app.bot.set_my_commands(commands)


async def callback_trade_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle YES/NO inline buttons on grade reports."""
    query = update.callback_query
    await query.answer()
    chat_id = str(query.from_user.id)
    user = get_user_by_chat_id(chat_id)
    if not user:
        return

    action = query.data  # trade_yes or trade_no
    analysis = last_analysis.get(chat_id, {})
    risk = analysis.get("risk_percent", 0.35)
    provider = analysis.get("signal_source", "TNL Scanner")
    trade_id = last_trade_id.get(chat_id)

    await query.edit_message_reply_markup(reply_markup=None)

    if action == "trade_yes":
        if trade_id:
            from database import update_trade_result
            update_trade_result(trade_id, "PENDING")
        await context.bot.send_message(
            chat_id=chat_id,
            text=trade_executed() + "\n\nReply *WIN* or *LOSS* when you close the trade.",
            parse_mode="Markdown"
        )
        if is_friday_close_warning():
            firm_code = get_user_firm(user.id)
            profile = get_profile(firm_code) if firm_code else None
            if profile and not profile.allow_weekend:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ FRIDAY WARNING: {profile.name} does not allow weekend holding. Close before market close tonight."
                )
    else:
        if trade_id:
            from database import update_trade_result
            update_trade_result(trade_id, "SKIPPED")
        await context.bot.send_message(chat_id=chat_id, text=trade_skipped())


async def callback_autograde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle one-tap signal grading from scanner alerts."""
    query = update.callback_query
    chat_id = str(query.from_user.id)
    try:
        await query.answer()
    except Exception:
        pass

    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await context.bot.send_message(chat_id=chat_id, text="❌ No active subscription.")
        return

    # Get signal key from callback data
    signal_key = query.data.replace("autograde_", "")
    from scanner import get_cached_signal
    signal_text = get_cached_signal(signal_key)

    if not signal_text:
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Signal expired. Send /scan for fresh setups."
        )
        return

    # Remove the Grade button so it can only be tapped once
    await query.edit_message_reply_markup(reply_markup=None)

    # Show grading message
    await context.bot.send_message(
        chat_id=chat_id,
        text="🔍 Grading signal..."
    )

    # Run through full analysis pipeline
    try:
        from prop_firm_profiles import get_profile
        firm_code = get_user_firm(user.id)
        profile = get_profile(firm_code)
        state = get_state(user.id)

        state_dict = {
            "trades_today": state.trades_today,
            "open_trades": state.open_trades,
            "live_exposure": state.live_exposure,
            "session_losses": state.session_losses,
            "weekly_losses": state.weekly_losses,
            "daily_pnl": state.daily_pnl,
            "account_size": profile.account_size if profile else 10000.0,
            "risk_percent": 1.0,
            "max_contracts": profile.max_contracts if profile else None,
        }

        analysis = analyze_signal(signal_text, state_dict)

        if not analysis:
            await context.bot.send_message(chat_id=chat_id, text="❌ Could not grade signal. Try again.")
            return

        # Apply firm-aware risk cap
        if profile:
            max_daily_pct = profile.max_daily_loss_pct * 100
            signal_risk = analysis.get("risk_percent", 0) or 0
            if max_daily_pct > 0 and signal_risk > (max_daily_pct / 5):
                analysis["risk_percent"] = round(max_daily_pct / 5, 2)
            priority_header = f"⚡ ELITE PRIORITY ANALYSIS\n🏆 {profile.name} MODE ACTIVE — Risk capped at {analysis.get('risk_percent', 1.0)}%\n"
        else:
            priority_header = "⚡ ELITE PRIORITY ANALYSIS\n"

        # Check fast gates
        passed, gate_reason = run_fast_gates(state.__dict__ if hasattr(state, "__dict__") else state)
        if not passed:
            await context.bot.send_message(chat_id=chat_id, text=fast_gate_blocked(gate_reason))
            return

        # Build and send report
        from report import execute_report, blocked_report
        decision = analysis.get("decision", "").upper()

        if decision == "BLOCK" or analysis.get("grade", "") in ["C", "D", "F"]:
            report_text = priority_header + blocked_report(analysis, "Signal blocked")
            await context.bot.send_message(chat_id=chat_id, text=report_text, parse_mode="Markdown")
        else:
            report_text = priority_header + execute_report(analysis)
            # Store analysis for WIN/LOSS tracking
            last_analysis[chat_id] = analysis

            # Log trade to DB
            from database import Trade
            trade_id = log_trade(Trade(
                user_id=user.id,
                pair=analysis.get("pair", ""),
                direction=analysis.get("direction", ""),
                grade=analysis.get("grade", ""),
                confidence=analysis.get("confidence_score", 0),
                signal_source=analysis.get("signal_source", "TNL Scanner"),
                risk_percent=analysis.get("risk_percent", 0),
                entry_zone=str(analysis.get("entry_zone", "")),
                stop_loss=str(analysis.get("stop_loss", "")),
            ))
            if trade_id:
                last_trade_id[chat_id] = trade_id

            # Add YES/NO buttons
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            execute_keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ YES — Execute", callback_data="trade_yes"),
                InlineKeyboardButton("❌ NO — Skip", callback_data="trade_no"),
            ]])

            await context.bot.send_message(
                chat_id=chat_id,
                text=report_text,
                parse_mode="Markdown",
                reply_markup=execute_keyboard
            )

            # Update trade state
            log_trade_opened(user.id, analysis.get("risk_percent", 0))

    except Exception as e:
        logger.error(f"callback_autograde error: {e}")
        await context.bot.send_message(chat_id=chat_id, text="❌ Error grading signal. Try again.")


async def start_bot():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.post_init = set_bot_commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("firmlist", cmd_firmlist))
    app.add_handler(CommandHandler("setfirm", cmd_setfirm))
    app.add_handler(CommandHandler("firm", cmd_firm))
    app.add_handler(CommandHandler("challenge", cmd_challenge))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logtrade", cmd_logtrade))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("resetfirm", cmd_resetfirm))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(callback_reset, pattern="^reset_"))
    app.add_handler(CallbackQueryHandler(callback_autograde, pattern="^autograde_"))
    app.add_handler(CallbackQueryHandler(callback_trade_button, pattern="^trade_(yes|no)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("TNL Trader multi-user bot started")
    asyncio.create_task(process_signal_queue())
    from scanner import start_scanner
    from database import get_active_users
    asyncio.create_task(start_scanner(app.bot, get_active_users))
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.sleep(float("inf"))
        await app.updater.stop()
        await app.stop()
