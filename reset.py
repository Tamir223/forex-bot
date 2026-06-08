"""
TNL Trader Reset Module
Resets weekly_losses every Monday at midnight UTC.
Sends end-of-day report to all active users at 21:00 UTC daily.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, date
from database import reset_weekly_losses_all, get_all_active_users, get_conn

logger = logging.getLogger(__name__)

DAILY_LOSS_LIMIT_DOLLARS = 500.0


async def send_eod_report(bot_token: str):
    """Send end-of-day challenge report to all active users"""
    from telegram import Bot
    from database import load_challenge_state, save_challenge_state, get_user_firm
    from drawdown_tracker import state_from_json, state_to_json, _rollover_day
    from prop_firm_profiles import get_profile

    bot = Bot(token=bot_token)
    today = date.today()
    users = get_all_active_users()

    for user in users:
        if not user.telegram_chat_id:
            continue
        try:
            # Load challenge state — the only reliable source of P&L in dollars
            firm_code = get_user_firm(user.id)
            profile = get_profile(firm_code)
            challenge_json = load_challenge_state(user.id)

            if challenge_json:
                ch = state_from_json(challenge_json)
                # Roll over daily stats if this is a new day
                today_str = today.isoformat()
                if ch.today_date != today_str:
                    _rollover_day(ch, profile)
                    save_challenge_state(user.id, firm_code, state_to_json(ch))
                daily_pnl_dollars = ch.today_pnl
                challenge_pnl_dollars = ch.total_pnl
                account_size = ch.account_size or (profile.account_size if profile else 10000.0)
                current_balance = ch.current_balance
                profit_target_dollars = profile.profit_target if profile else 1000.0
                target_balance = ch.starting_balance + profit_target_dollars
            else:
                account_size = profile.account_size if profile else 10000.0
                daily_pnl_dollars = 0.0
                challenge_pnl_dollars = 0.0
                current_balance = account_size
                profit_target_dollars = profile.profit_target if profile else 1000.0
                target_balance = account_size + profit_target_dollars

            # Trades taken and blocked today — queried directly from DB filtered by CURRENT_DATE
            trades_taken_today = 0
            blocked_today = 0
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """SELECT
                                 COUNT(*) FILTER (WHERE result != 'BLOCKED') AS taken,
                                 COUNT(*) FILTER (WHERE result = 'BLOCKED')  AS blocked
                               FROM trades
                               WHERE user_id = %s
                                 AND DATE(created_at AT TIME ZONE 'UTC') = CURRENT_DATE""",
                            (user.id,)
                        )
                        row = cur.fetchone()
                        if row:
                            trades_taken_today = row["taken"] or 0
                            blocked_today = row["blocked"] or 0
            except Exception as e:
                logger.error(f"Daily trade count query error for user {user.id}: {e}")

            daily_pnl_pct = (daily_pnl_dollars / account_size * 100) if account_size else 0.0
            challenge_pnl_pct = (challenge_pnl_dollars / account_size * 100) if account_size else 0.0
            daily_loss_dollars = abs(min(0.0, daily_pnl_dollars))
            daily_limit_used_pct = (daily_loss_dollars / DAILY_LOSS_LIMIT_DOLLARS * 100)
            target_remaining = max(0.0, target_balance - current_balance)

            daily_sign = "+" if daily_pnl_dollars >= 0 else ""
            ch_sign = "+" if challenge_pnl_dollars >= 0 else ""

            msg = (
                f"📋 END OF DAY REPORT — {today.strftime('%Y-%m-%d')}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Trades taken:     {trades_taken_today}\n"
                f"Trades blocked:   {blocked_today}\n"
                f"Daily P&L:        {daily_sign}${daily_pnl_dollars:,.2f} ({daily_sign}{daily_pnl_pct:.2f}%)\n"
                f"Challenge P&L:    {ch_sign}${challenge_pnl_dollars:,.2f} ({ch_sign}{challenge_pnl_pct:.2f}%)\n"
                f"Daily limit used: ${daily_loss_dollars:,.2f} / ${DAILY_LOSS_LIMIT_DOLLARS:,.0f} ({daily_limit_used_pct:.1f}%)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Target remaining: ${target_remaining:,.2f} remaining\n"
                f"See you tomorrow. Stay disciplined."
            )
            await bot.send_message(chat_id=int(user.telegram_chat_id), text=msg)
        except Exception as e:
            logger.error(f"EOD report error for user {user.id}: {e}")


async def start_weekly_reset():
    """Runs forever — weekly reset on Monday midnight UTC, EOD report at 21:00 UTC daily"""
    eod_sent_date = None
    while True:
        now = datetime.now(timezone.utc)
        today = now.date()

        if now.weekday() == 0 and now.hour == 0 and now.minute < 5:
            logger.info("Monday midnight - running weekly reset")
            reset_weekly_losses_all()

        if now.hour == 21 and now.minute < 5 and eod_sent_date != today:
            logger.info("21:00 UTC - sending EOD reports")
            eod_sent_date = today
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            if bot_token:
                await send_eod_report(bot_token)

        await asyncio.sleep(300)
