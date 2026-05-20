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
from trading_calendar import is_friday_close_warning
from config import FTMO_MODE, FTMO_MAX_RISK
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
        "/cancel — manage or cancel your subscription\n"
        "/help — this menu\n\n"
        "Replies after a report:\n"
        "YES — execute the trade\n"
        "NO — skip the trade\n"
        "WIN — mark last trade as win\n"
        "LOSS — mark last trade as loss"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "To cancel your TNL Trader subscription, visit your billing portal below. "
        "You will keep full access until the end of your current billing period.\n\n"
        "https://billing.stripe.com/p/login/fZu3cwesK8NEflccqOfjG00\n\n"
        "If you have any issues email support@tnltrader.com"
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
    # Elite priority: lower effective confidence threshold by 1
    if user.plan_tier == "elite" and analysis.get("confidence") is not None:
        analysis["confidence"] = min(10, analysis["confidence"] + 1)

    # FTMO Mode risk enforcement
    if FTMO_MODE:
        risk = analysis.get("risk_percent", 0) or 0
        if risk > 1.0:
            analysis["decision"] = "BLOCK"
            analysis["reason"] = "FTMO risk limit exceeded"
        elif risk > FTMO_MAX_RISK:
            analysis["risk_percent"] = FTMO_MAX_RISK

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
    if FTMO_MODE:
        report = "🏆 FTMO MODE ACTIVE — Risk capped at 0.5%\n" + report
    if user.plan_tier == "elite":
        report = "⚡ ELITE PRIORITY ANALYSIS\n" + report
    await send(context, chat_id, report)


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


async def start_bot():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("TNL Trader multi-user bot started")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.sleep(float("inf"))
        await app.updater.stop()
        await app.stop()
