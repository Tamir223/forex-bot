"""
TNL Trader SaaS - Main Entry Point
Runs the Telegram bot, Stripe webhook server, and weekly reset together.
"""

import logging
import asyncio
import uvicorn
from bot import start_bot
from webhooks import app as stripe_app
from reset import start_weekly_reset
from database import init_db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def run_webhook_server():
    config = uvicorn.Config(
        stripe_app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    logger.info("Starting TNL Trader SaaS...")
    init_db()
    logger.info("Database ready")

    await asyncio.gather(
        start_bot(),
        run_webhook_server(),
        start_weekly_reset()
    )


if __name__ == "__main__":
    asyncio.run(main())
