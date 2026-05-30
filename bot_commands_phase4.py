"""
Phase 4 Bot Commands — Multi-Account + Self-Learning
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import get_user_by_chat_id, get_user_firm
from phase4_learning import (
    get_all_accounts_summary, get_performance_insights,
    get_user_accounts, upsert_account, get_confidence_modifier,
    get_session, get_best_trading_hours, record_daily_performance
)
from prop_firm_profiles import get_profile

logger = logging.getLogger(__name__)


async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all active challenge accounts."""
    chat_id = str(update.effective_chat.id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text("❌ No active subscription.")
        return
    summary = get_all_accounts_summary(user.id)
    await update.message.reply_text(summary, parse_mode="Markdown")


async def cmd_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show personalized performance insights."""
    chat_id = str(update.effective_chat.id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text("❌ No active subscription.")
        return
    insights = get_performance_insights(user.id)
    await update.message.reply_text(insights, parse_mode="Markdown")


async def cmd_best_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show best trading hours based on personal history."""
    chat_id = str(update.effective_chat.id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text("❌ No active subscription.")
        return

    hours = get_best_trading_hours(user.id)
    if not hours:
        await update.message.reply_text(
            "📊 *Best Trading Hours*\n\n"
            "Not enough data yet. Need at least 10 completed trades.\n"
            "Keep trading and logging WIN/LOSS after each trade.",
            parse_mode="Markdown"
        )
        return

    lines = ["⏰ *YOUR BEST TRADING HOURS (UTC)*\n"]
    for i, h in enumerate(hours, 1):
        edt_hour = (h['hour'] - 4) % 24
        ampm = "AM" if edt_hour < 12 else "PM"
        edt_display = edt_hour if edt_hour <= 12 else edt_hour - 12
        if edt_display == 0:
            edt_display = 12
        lines.append(
            f"{i}. {h['hour']:02d}:00 UTC ({edt_display} {ampm} EDT) — "
            f"{h['win_rate']}% win rate ({h['wins']}/{h['total']} trades)"
        )

    lines.append("\n💡 Focus your trading on these hours for best results.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new challenge account to track."""
    chat_id = str(update.effective_chat.id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text("❌ No active subscription.")
        return

    args = context.args
    if not args or len(args) < 1:
        await update.message.reply_text(
            "Usage: /addaccount [firm_code]\n\n"
            "Example: /addaccount fundednext50\n\n"
            "Send /firmlist to see all available firms."
        )
        return

    firm_code = args[0].lower()
    profile = get_profile(firm_code)
    if not profile:
        await update.message.reply_text(
            f"❌ Unknown firm code: {firm_code}\n"
            "Send /firmlist to see all available firms."
        )
        return

    success = upsert_account(
        user_id=user.id,
        firm_code=firm_code,
        start_balance=profile.account_size,
        label=profile.name
    )

    if success:
        await update.message.reply_text(
            f"✅ *{profile.name}* added to your accounts!\n\n"
            f"Starting balance: ${profile.account_size:,.0f}\n"
            f"Profit target: ${profile.profit_target:,.0f} ({profile.profit_target_pct*100:.0f}%)\n"
            f"Max daily loss: ${profile.max_daily_loss:,.0f}\n"
            f"Max total loss: ${profile.max_total_loss:,.0f}\n\n"
            f"Use /accounts to see all your challenges.\n"
            f"Use /setfirm {firm_code} to make this your active firm.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Failed to add account. Try again.")


async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed performance breakdown."""
    chat_id = str(update.effective_chat.id)
    user = get_user_by_chat_id(chat_id)
    if not user or not user.is_active:
        await update.message.reply_text("❌ No active subscription.")
        return

    from phase4_learning import get_pair_performance, get_session_performance
    
    pairs = ['XAUUSD', 'EURUSD', 'GBPUSD', 'NQ', 'ES']
    lines = ["📈 *PAIR PERFORMANCE — Last 30 Days*\n"]
    
    has_data = False
    for pair in pairs:
        perf = get_pair_performance(user.id, pair)
        if perf['total'] >= 2:
            has_data = True
            emoji = "🟢" if perf['win_rate'] >= 50 else "🔴"
            lines.append(
                f"{emoji} *{pair}*: {perf['win_rate']}% win rate "
                f"({perf['wins']}W/{perf['losses']}L) | "
                f"P&L: ${perf['total_pnl']:,.2f}"
            )

    if not has_data:
        await update.message.reply_text(
            "📈 *Pair Performance*\n\n"
            "Not enough data yet. Need at least 2 trades per pair.\n"
            "Keep trading and this will populate automatically.",
            parse_mode="Markdown"
        )
        return

    lines.append("\n⏰ *SESSION PERFORMANCE*\n")
    sessions = get_session_performance(user.id)
    for session, data in sessions.items():
        if data['total'] >= 2:
            emoji = "🟢" if data['win_rate'] >= 50 else "🔴"
            lines.append(
                f"{emoji} *{session}*: {data['win_rate']}% "
                f"({data['total']} trades) | Avg: ${data['avg_pnl']:,.2f}"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
