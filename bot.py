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
    filters, ContextTypes
)
from database import (
    get_user_by_chat_id, get_state, link_telegram,
    log_trade_opened, log_trade_win, log_trade_loss,
    get_provider_stats, update_provider_result,
    log_trade, get_user_trades, Trade
)
from notifications import send_subscription_confirmed
from filter import is_signal_message, is_approval_message, run_fast_gates, run_enforcement_filter
from claude import analyze_signal
from report import (
    execute_report, blocked_report, fast_gate_blocked,
    trade_executed, trade_skipped, trade_logged_win,
    trade_logged_loss, not_subscribed_message, status_report
)
import os

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

last_analysis = {}
last_trade_id = {}


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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text(not_subscribed_message())
        return
    state = get_state(user.id)
    await update.message.reply_text(status_report(state, user.plan_tier))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text(not_subscribed_message())
        return
    trades = get_user_trades(user.id, limit=10)
    if not trades:
        await update.message.reply_text("No trades logged yet. Start forwarding signals.")
        return
    lines = ["📊 RECENT TRADES\n"]
    for t in trades:
        result_emoji = {"WIN": "✅", "LOSS": "❌", "PENDING": "⏳", "SKIPPED": "⏭"}.get(t["result"], "")
        lines.append(f"{result_emoji} {t['pair']} {t['direction']} | {t['grade']} | {t['confidence']}/10 | {t['signal_source']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "TNL Trader Commands\n\n/status — account state and limits\n/stats — last 10 trades\n/help — this menu\n\nReplies after a report:\nYES — execute the trade\nNO — skip the trade\nWIN — mark last trade as win\nLOSS — mark last trade as loss"
    )


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
            await send(context, chat_id, trade_logged_win())
        elif reply == "LOSS":
            log_trade_loss(user.id, risk)
            update_provider_result(user.id, provider, won=False)
            if trade_id:
                from database import update_trade_result
                update_trade_result(trade_id, "LOSS")
            await send(context, chat_id, trade_logged_loss())
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
    await send(context, chat_id, "⏳ Analyzing signal...")
    state_dict = {
        "trades_today": state.trades_today,
        "open_trades": state.open_trades,
        "live_exposure": state.live_exposure,
        "session_losses": state.session_losses,
        "weekly_losses": state.weekly_losses,
        "daily_pnl": state.daily_pnl
    }
    analysis = analyze_signal(text, state_dict, user.id)
    if not analysis:
        await send(context, chat_id, "⚠️ Analysis failed. Please try again.")
        return
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
        await send(context, chat_id, blocked_report(analysis, "claude_block"))
        from database import update_trade_result
        if trade_id:
            update_trade_result(trade_id, "BLOCKED")
        return
    passed, enforce_reason = run_enforcement_filter(analysis)
    if not passed:
        await send(context, chat_id, blocked_report(analysis, enforce_reason))
        from database import update_trade_result
        if trade_id:
            update_trade_result(trade_id, "BLOCKED")
        return
    report = execute_report(analysis)
    await send(context, chat_id, report)


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


async def start_bot():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("TNL Trader multi-user bot started")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.sleep(float("inf"))
        await app.updater.stop()
        await app.stop()
