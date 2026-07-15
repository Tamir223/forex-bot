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
    log_trade, get_user_trades, Trade, get_user_firm,
    update_trade_result, get_auto_execute, set_auto_execute
)
from prop_firm_profiles import get_profile
from notifications import send_subscription_confirmed
from claude import analyze_signal
from report import (
    execute_report, blocked_report,
    trade_executed, trade_skipped, trade_logged_win,
    trade_logged_loss, not_subscribed_message, status_report
)
from trading_calendar import is_friday_close_warning
from trade_monitor import trade_monitor
import os
from bot_commands_phase1 import (
    cmd_firmlist, cmd_setfirm, cmd_firm,
    cmd_challenge, cmd_status, cmd_logtrade,
    cmd_history, cmd_resetfirm, callback_reset,
    cmd_watch, cmd_scan, cmd_bias
)

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

last_analysis = {}
last_trade_id = {}
signal_queue: asyncio.Queue = asyncio.Queue()


async def _cancel_limit_not_filled(user_id: int, chat_id: str, bot):
    """Cancel PENDING trade(s) from last 2 hours and reverse exposure tracking."""
    try:
        from database import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE trades SET result='CANCELLED'
                       WHERE user_id=%s AND result='PENDING'
                       AND created_at > NOW() - INTERVAL '2 hours'
                       RETURNING risk_percent""",
                    (user_id,)
                )
                rows = cur.fetchall()
                if rows:
                    total_risk = sum(float(r[0] or 0) for r in rows)
                    cur.execute(
                        """UPDATE user_state SET
                           live_exposure = GREATEST(0, live_exposure - %s),
                           open_trades = GREATEST(0, open_trades - %s)
                           WHERE user_id = %s""",
                        (total_risk, len(rows), user_id)
                    )
            conn.commit()
    except Exception as e:
        logger.error(f"_cancel_limit_not_filled error: {e}")
    trade_monitor.remove_trade(user_id)
    await bot.send_message(
        chat_id=int(chat_id),
        text="✅ Limit order cancelled — no trade recorded. Buffer unchanged."
    )

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

    try:
        from database import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT UPPER(pair) AS pair, direction, result, pnl_amount, created_at "
                    "FROM trades "
                    "WHERE user_id = %s "
                    "  AND result IS NOT NULL "
                    "  AND UPPER(result) NOT IN ('BLOCKED','SKIPPED','EXPIRED','CANCELLED') "
                    "ORDER BY created_at DESC",
                    (user.id,)
                )
                rows = [dict(r) for r in cur.fetchall()]
                logger.info(f"[stats] raw sample (first 3): {rows[:3]}")
                # Active trades: result IS NULL or PENDING
                cur.execute(
                    "SELECT COUNT(*) as count FROM trades "
                    "WHERE user_id = %s "
                    "  AND (result IS NULL OR UPPER(result) = 'PENDING')",
                    (user.id,)
                )
                active_count = cur.fetchone()["count"]
    except Exception as e:
        logger.error(f"[stats] DB query failed: {e}")
        await update.message.reply_text("Could not load stats. Try again later.")
        return

    if not rows:
        await update.message.reply_text("No trades logged yet. Start forwarding signals.")
        return

    EST_WIN  =  112.50  # 1.5R on $75 risk
    EST_LOSS =  -75.00

    def _pnl(t):
        try:
            raw = float(t["pnl_amount"] or 0)
        except (TypeError, ValueError):
            raw = 0.0
        if raw != 0:
            return raw
        r = str(t.get("result", "")).upper()
        if r == "WIN":
            return EST_WIN
        if r == "LOSS":
            return EST_LOSS
        return 0.0

    def _is_win(t):
        return str(t.get("result", "")).upper() == "WIN"

    def _is_loss(t):
        return str(t.get("result", "")).upper() == "LOSS"

    wins   = [t for t in rows if _is_win(t)]
    losses = [t for t in rows if _is_loss(t)]
    logger.info(f"[stats] total={len(rows)} wins={len(wins)} losses={len(losses)}")
    total  = len(rows)
    win_rate = round(len(wins) / total * 100) if total else 0

    win_pnls  = [_pnl(t) for t in wins]
    loss_pnls = [abs(_pnl(t)) for t in losses]
    total_pnl = sum(win_pnls) - sum(loss_pnls)
    avg_win   = (sum(win_pnls)  / len(win_pnls))  if win_pnls  else 0.0
    avg_loss  = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0
    pf        = round(sum(win_pnls) / sum(loss_pnls), 2) if sum(loss_pnls) > 0 else float("inf")
    pf_str    = f"{pf:.2f}" if pf != float("inf") else "∞"

    # Per-pair breakdown
    from collections import defaultdict
    pair_trades: dict = defaultdict(list)
    for t in rows:
        if t.get("pair"):
            pair_trades[t["pair"]].append(t)

    pair_stats = []
    for pair, pts in pair_trades.items():
        p_wins  = sum(1 for t in pts if _is_win(t))
        p_total = len(pts)
        p_wr    = round(p_wins / p_total * 100) if p_total else 0
        p_pnl   = sum(_pnl(t) for t in pts if _is_win(t)) \
                + sum(_pnl(t) for t in pts if _is_loss(t))
        pair_stats.append((pair, p_total, p_wr, p_pnl))

    pair_stats.sort(key=lambda x: x[2], reverse=True)
    pair_lines = []
    for pair, p_total, p_wr, p_pnl in pair_stats:
        sign = "+" if p_pnl >= 0 else "-"
        pair_lines.append(f"{pair}: {p_total} trades | {p_wr}% | {sign}${abs(p_pnl):.2f}")

    qualified  = [s for s in pair_stats if s[1] >= 3]
    best_pair  = qualified[0][0]  if qualified else "N/A"
    worst_pair = qualified[-1][0] if qualified else "N/A"

    # Per-direction breakdown
    buys  = [t for t in rows if str(t.get("direction","")).upper() == "BUY"]
    sells = [t for t in rows if str(t.get("direction","")).upper() == "SELL"]
    buy_wr  = round(sum(1 for t in buys  if _is_win(t)) / len(buys)  * 100) if buys  else 0
    sell_wr = round(sum(1 for t in sells if _is_win(t)) / len(sells) * 100) if sells else 0

    sep = "━━━━━━━━━━━━━━━━━━━━"
    lines = [
        sep,
        "📊 TNL TRADER STATS",
        sep,
        f"Total Trades: {total}",
        f"Active Trades: {active_count}",
        f"Win Rate: {win_rate}%",
        f"Profit Factor: {pf_str}",
        f"Total PnL: ${total_pnl:+.2f}",
        "",
        f"Avg Win:  ${avg_win:.2f}",
        f"Avg Loss: ${avg_loss:.2f}",
        "",
        "BY PAIR:",
    ] + pair_lines + [
        "",
        "BY DIRECTION:",
        f"BUY:  {len(buys)} trades | {buy_wr}%",
        f"SELL: {len(sells)} trades | {sell_wr}%",
        sep,
    ]

    await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *TNL Trader Commands*\n\n"
        "📊 *Challenge:*\n"
        "/stats — your full trading stats & P&L\n"
        "/status — challenge P&L and drawdown\n"
        "/challenge — start a new challenge\n"
        "/setfirm — set your prop firm\n"
        "/firmlist — all supported firms\n"
        "/firm — your current firm rules\n"
        "/logtrade — log a trade manually\n"
        "/history — last 10 trades\n"
        "/resetfirm — reset challenge tracker\n\n"
        "📡 *Scanner:*\n"
        "/scan — instant market scan\n"
        "/watch — set your watchlist\n"
        "/asia — Asia session high/low levels\n"
        "/autoexecute on|off — EA auto-execution toggle\n\n"
        "💬 *After a report:*\n"
        "YES — execute the trade\n"
        "NO — skip the trade\n"
        "WIN — quick win log (estimated P&L)\n"
        "LOSS — quick loss log (estimated P&L)\n"
        "/logtrade WIN 146.20 — exact dollar amount\n"
        "/logtrade LOSS 12.07 — exact dollar amount",
        parse_mode="Markdown"
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


async def cmd_asia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from scanner import _asia_levels

    lines = ["📊 ASIA SESSION LEVELS", "━━━━━━━━━━━━━━━━━━━━"]

    pairs = ['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'NZDUSD', 'USDCHF', 'US100', 'US30', 'US500']

    for pair in pairs:
        levels = _asia_levels.get(pair)
        if levels:
            lines.append(f"{pair}  H: {levels['high']:.5f}  L: {levels['low']:.5f}")
        else:
            lines.append(f"{pair}  — not set")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🎯 Watch for price to sweep H or L then reverse")

    await update.message.reply_text("\n".join(lines))


async def cmd_autoexecute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text(not_subscribed_message())
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        current = get_auto_execute(user.id)
        status = "ON — EA places trades automatically" if current else "OFF — Telegram signal only, you place manually"
        await update.message.reply_text(
            f"⚡ Auto Execute is currently: {status}\n\n"
            "/autoexecute on  — EA places trades automatically\n"
            "/autoexecute off — Telegram signal only (default)"
        )
        return

    enable = args[0].lower() == "on"
    set_auto_execute(user.id, enable)

    if enable:
        await update.message.reply_text(
            "✅ Auto Execute ON\n\n"
            "When a signal fires, it will be written to the EA signal file automatically.\n"
            "The EA will place the order — no manual action needed."
        )
    else:
        await update.message.reply_text(
            "✅ Auto Execute OFF\n\n"
            "Signals will be sent to Telegram only.\n"
            "You place the trade manually."
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
                if trade_id:
                    update_trade_result(trade_id, "BLOCKED")
            else:
                report = execute_report(analysis)
                if user.plan_tier == "elite":
                    report = priority_header + report
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                _grade_keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ YES — Execute", callback_data="trade_yes"),
                        InlineKeyboardButton("❌ NO — Skip", callback_data="trade_no"),
                    ],
                    [InlineKeyboardButton("❌ Limit Not Filled", callback_data="trade_limit_not_filled")],
                ])
                await bot.send_message(chat_id=int(chat_id), text=report, reply_markup=_grade_keyboard)
                from discord_bridge import send_to_discord as _send_discord_sq, strip_lots_for_discord as _strip_discord_sq
                asyncio.create_task(_send_discord_sq(_strip_discord_sq(report)))
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
    if text.strip().upper() in {"YES", "NO", "WIN", "LOSS", "BREAKEVEN", "LIMIT NOT FILLED", "LIMIT_NOT_FILLED"}:
        reply = text.upper()
        analysis = last_analysis.get(chat_id, {})
        risk = analysis.get("risk_percent", 0.35)
        provider = analysis.get("signal_source", "UNKNOWN")
        trade_id = last_trade_id.get(chat_id)
        if reply == "YES":
            log_trade_opened(user.id, risk)
            if trade_id:
                update_trade_result(trade_id, "PENDING")
            await send(context, chat_id, trade_executed())
            if is_friday_close_warning():
                await send(context, chat_id, "⚠️ FRIDAY WARNING: FTMO does not allow holding positions over the weekend. Make sure to close this trade before market close at 21:00 UTC tonight.")
        elif reply == "NO":
            if trade_id:
                update_trade_result(trade_id, "SKIPPED")
            await send(context, chat_id, trade_skipped())
        elif reply == "WIN":
            log_trade_win(user.id, risk)
            update_provider_result(user.id, provider, won=True)
            trade_monitor.remove_trade(user.id)
            from database import load_challenge_state, save_challenge_state
            from prop_firm_profiles import get_profile as gp
            from drawdown_tracker import state_from_json, state_to_json, record_trade
            _state_json = load_challenge_state(user.id)
            _state = state_from_json(_state_json) if _state_json else None
            _profile = gp(_state.firm_code if _state else get_user_firm(user.id))
            _account = _profile.account_size if _profile else 10000
            pnl_est = round(analysis.get("risk_percent", 0.35) / 100 * _account * 2, 2)
            if trade_id:
                update_trade_result(trade_id, "WIN", pnl_amount=pnl_est)
            if _state and _profile:
                _state, _warns = record_trade(_state, _profile, pnl_est)
                save_challenge_state(user.id, _state.firm_code, state_to_json(_state))
                if _warns:
                    for w in _warns:
                        await send(context, chat_id, w)
            await send(context, chat_id, trade_logged_win())
        elif reply == "LOSS":
            log_trade_loss(user.id, risk)
            try:
                from scanner_improvements import check_consecutive_losses, get_loss_warning_message
                should_warn, consecutive = check_consecutive_losses(user.id)
                if should_warn:
                    warning_msg = get_loss_warning_message(consecutive)
                    if warning_msg:
                        await update.message.reply_text(warning_msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Consecutive loss check error: {e}")
            update_provider_result(user.id, provider, won=False)
            trade_monitor.remove_trade(user.id)
            from database import load_challenge_state, save_challenge_state
            from prop_firm_profiles import get_profile as gp
            from drawdown_tracker import state_from_json, state_to_json, record_trade, check_daily_loss_warnings
            _state_json = load_challenge_state(user.id)
            _state = state_from_json(_state_json) if _state_json else None
            _profile = gp(_state.firm_code if _state else get_user_firm(user.id))
            _account = _profile.account_size if _profile else 10000
            pnl_est = -round(analysis.get("risk_percent", 0.35) / 100 * _account, 2)
            if trade_id:
                update_trade_result(trade_id, "LOSS", pnl_amount=pnl_est)
            if _state and _profile:
                _state, _warns = record_trade(_state, _profile, pnl_est)
                _loss_warns = check_daily_loss_warnings(_state, _profile)
                save_challenge_state(user.id, _state.firm_code, state_to_json(_state))
                if _warns:
                    for w in _warns:
                        await send(context, chat_id, w)
                if _loss_warns:
                    for w in _loss_warns:
                        await send(context, chat_id, w)
            await send(context, chat_id, trade_logged_loss())
        elif reply == "BREAKEVEN":
            if trade_id:
                update_trade_result(trade_id, "BREAKEVEN")
            await send(context, chat_id, "➖ Breakeven logged.")
        elif reply in ("LIMIT NOT FILLED", "LIMIT_NOT_FILLED"):
            await _cancel_limit_not_filled(user.id, chat_id, context.bot)
        return
    if not any(kw in text for kw in ("BUY", "buy", "SELL", "sell", "ENTRY ZONE", "entry zone", "TYPE:")):
        return
    plan_limits = {"basic": 10, "pro": 999, "elite": 999}
    state = get_state(user.id)
    daily_limit = plan_limits.get(user.plan_tier, 10)
    if state.trades_today >= daily_limit:
        await send(context, chat_id, f"Daily signal limit reached for your {user.plan_tier} plan.\nUpgrade at tnltrader.com for more signals.")
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
        BotCommand(command="start", description="Get started with TNL Trader"),
        BotCommand(command="help", description="Show all available commands"),
        BotCommand(command="stats", description="Your full trading stats and P&L breakdown"),
        BotCommand(command="setfirm", description="Set your prop firm (e.g. /setfirm apex150)"),
        BotCommand(command="status", description="Check your challenge progress"),
        BotCommand(command="logtrade", description="Log a trade result (e.g. /logtrade WIN 500)"),
        BotCommand(command="watch", description="Set your scanner watchlist (e.g. /watch ES NQ XAUUSD)"),
        BotCommand(command="scan", description="Trigger an instant manual scan of your watchlist"),
        BotCommand(command="bias", description="Daily bias report for your watchlist pairs"),
        BotCommand(command="asia", description="Asia session highs and lows for all pairs"),
        BotCommand(command="autoexecute", description="Toggle EA auto-execution (on/off)"),
    ]
    logger.info(f"[bot] Registering {len(commands)} commands: {[c.command for c in commands]}")
    await app.bot.set_my_commands(commands)
    logger.info("[bot] set_my_commands completed successfully")


async def callback_trade_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle YES/NO inline buttons on grade reports."""
    query = update.callback_query
    await query.answer()
    chat_id = str(query.from_user.id)
    user = get_user_by_chat_id(chat_id)
    if not user:
        return

    action = query.data
    analysis = last_analysis.get(chat_id, {})
    risk = analysis.get("risk_percent", 0.35)
    trade_id = last_trade_id.get(chat_id)

    await query.edit_message_reply_markup(reply_markup=None)

    if action == "trade_yes":
        if trade_id:
            update_trade_result(trade_id, "PENDING")
        log_trade_opened(user.id, risk)
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        cancel_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel Limit", callback_data="trade_cancel_limit"),
        ]])
        await context.bot.send_message(
            chat_id=chat_id,
            text=trade_executed() + "\n\nReply *WIN* or *LOSS* when you close the trade.\nIf your limit order was never filled, tap Cancel Limit below.",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard,
        )
        # Start trade monitoring
        try:
            firm_code = get_user_firm(user.id)
            trade_monitor.add_trade(
                user_id=user.id,
                chat_id=chat_id,
                symbol=analysis.get("pair", ""),
                direction=analysis.get("direction", ""),
                entry=analysis.get("entry_zone"),
                sl=analysis.get("stop_loss"),
                tp1=analysis.get("tp1"),
                tp2=analysis.get("tp2"),
                firm_code=firm_code,
            )
        except Exception as _e:
            logger.error(f"[monitor] add_trade error: {_e}")
        if is_friday_close_warning():
            firm_code = get_user_firm(user.id)
            profile = get_profile(firm_code) if firm_code else None
            if profile and not profile.allow_weekend:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ FRIDAY WARNING: {profile.name} does not allow weekend holding. Close before market close tonight."
                )
    elif action == "trade_no":
        if trade_id:
            update_trade_result(trade_id, "SKIPPED")
        try:
            from database import get_conn
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE trades SET result='CANCELLED' WHERE user_id=%s AND result='PENDING' AND created_at > NOW() - INTERVAL '1 hour'",
                        (user.id,)
                    )
                conn.commit()
        except Exception as _e:
            logger.error(f"PENDING cleanup error on NO: {_e}")
        await context.bot.send_message(chat_id=chat_id, text=trade_skipped())
    elif action in ("trade_limit_not_filled", "trade_cancel_limit"):
        await _cancel_limit_not_filled(user.id, chat_id, context.bot)


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
    from scanner import get_cached_signal, get_cached_score
    signal_text = get_cached_signal(signal_key)
    cached_score = get_cached_score(signal_key)

    if not signal_text:
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Signal expired. Send /scan for fresh setups."
        )
        try:
            from database import get_conn
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE trades SET result='CANCELLED' WHERE user_id=%s AND result='PENDING' AND created_at > NOW() - INTERVAL '1 hour'",
                        (user.id,)
                    )
                conn.commit()
        except Exception as _e:
            logger.error(f"PENDING cleanup error on signal expiry: {_e}")
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
            "score": cached_score,
        }

        analysis = analyze_signal(signal_text, state_dict)

        if not analysis:
            await asyncio.sleep(5)
            analysis = analyze_signal(signal_text, state_dict)

        if not analysis:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ Grading temporarily unavailable. Tap the button again in 10 seconds.")
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

            # Add YES/NO/LNF buttons
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            execute_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ YES — Execute", callback_data="trade_yes"),
                    InlineKeyboardButton("❌ NO — Skip", callback_data="trade_no"),
                ],
                [InlineKeyboardButton("❌ Limit Not Filled", callback_data="trade_limit_not_filled")],
            ])

            await context.bot.send_message(
                chat_id=chat_id,
                text=report_text,
                parse_mode="Markdown",
                reply_markup=execute_keyboard
            )
            from discord_bridge import send_to_discord as _send_discord_ag, strip_lots_for_discord as _strip_discord_ag
            asyncio.create_task(_send_discord_ag(_strip_discord_ag(report_text)))

    except Exception as e:
        logger.error(f"callback_autograde error: {e}")
        await context.bot.send_message(chat_id=chat_id, text="❌ Error grading signal. Try again.")


async def start_bot():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
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
    app.add_handler(CommandHandler("bias", cmd_bias))
    app.add_handler(CommandHandler("asia", cmd_asia))
    app.add_handler(CommandHandler("autoexecute", cmd_autoexecute))
    app.add_handler(CallbackQueryHandler(callback_reset, pattern="^reset_"))
    app.add_handler(CallbackQueryHandler(callback_autograde, pattern="^autograde_"))
    app.add_handler(CallbackQueryHandler(callback_trade_button, pattern="^trade_(yes|no|limit_not_filled|cancel_limit)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("TNL Trader multi-user bot started")
    _signal_queue_task = asyncio.create_task(process_signal_queue())
    from scanner import start_scanner
    from database import get_all_active_users
    _scanner_task = asyncio.create_task(start_scanner(app.bot, get_all_active_users))
    async with app:
        await app.start()
        await set_bot_commands(app)
        await app.updater.start_polling()
        await asyncio.sleep(float("inf"))
        await app.updater.stop()
        await app.stop()
