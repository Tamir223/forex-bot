"""
TNL Trader Reset Module
Resets weekly_losses every Monday at midnight UTC
"""

import asyncio
import logging
from datetime import datetime, timezone
from database import reset_weekly_losses_all

logger = logging.getLogger(__name__)


async def start_weekly_reset():
    """Runs forever, checks every hour if it is Monday midnight UTC"""
    while True:
        now = datetime.now(timezone.utc)
        if now.weekday() == 0 and now.hour == 0 and now.minute < 5:
            logger.info("Monday midnight - running weekly reset")
            reset_weekly_losses_all()
        await asyncio.sleep(300)
