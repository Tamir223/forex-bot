"""
TNL Trader Reset Module
Resets weekly_losses every Monday at midnight UTC.
Sends end-of-day report to all active users at 21:00 UTC daily.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, date
from database import reset_weekly_losses_all, get_all_active_users, get_state, get_conn

logger = logging.getLogger(__name__)


async def send_eod_report(bot_token: str):
    """Send end-of-day challenge report to all active users"""
    from telegram import Bot
    bot = Bot(token=bot_token)
    today = date.today()
    users = get_all_active_users()

    for user in users:
        if not user.telegram_chat_id:
            continue
        try:
            state = get_state(user.id)

            blocked_today = 0
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """SELECT COUNT(*) AS cnt FROM trades
                               WHERE user_id = %s AND result = 'BLOCKED'
                               AND created_at::date = %s""",
                            (user.id, today)
                        )
                        row = cur.fetchone()
                        blocked_today = row["cnt"] if row else 0
            except Exception as e:
                logger.error(f"Blocked count query error for user {user.id}: {e}")

            daily_pnl = state.daily_pnl
            challenge_pnl = getattr(state, "challenge_pnl", 0.0) or 0.0
            challenge_target = getattr(state, "challenge_target", 10.0) or 10.0

            daily_sign = "+" if daily_pnl >= 0 else ""
            ch_sign = "+" if challenge_pnl >= 0 else ""
            daily_loss_used = abs(min(0.0, daily_pnl))
            target_remaining = max(0.0, challenge_target - challenge_pnl)

            msg = (
                f"📋 END OF DAY REPORT — {today.strftime('%Y-%m-%d')}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Trades taken:    {state.trades_today}\n"
                f"Trades blocked:  {blocked_today}\n"
                f"Daily P&L:       {daily_sign}{daily_pnl:.1f}%\n"
                f"Challenge P&L:   {ch_sign}{challenge_pnl:.1f}%\n"
                f"Daily limit used: {daily_loss_used:.1f}% of 5%\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Target remaining: {target_remaining:.1f}% to {challenge_target:.0f}%\n"
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
