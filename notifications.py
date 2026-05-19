"""
APFEE Notifications Module
Sends onboarding, welcome, and system messages to users via Telegram.
"""

import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import httpx

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
APFEE_BOT_USERNAME = os.getenv("APFEE_BOT_USERNAME", "APFEESignalBot")
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

ONBOARDING_URL = os.getenv("ONBOARDING_URL", "https://apfee.io/setup")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.resend.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@apfee.io")


async def send_telegram(chat_id: str, text: str):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/sendMessage",
                json={"chat_id": chat_id, "text": text}
            )
    except Exception as e:
        logger.error(f"Telegram send error: {e}")


def send_activation_email(to_email: str, activation_link: str, plan: str):
    plan_labels = {"basic": "Basic", "pro": "Pro", "elite": "Elite"}
    plan_name = plan_labels.get(plan, plan.title())

    body = f"""Welcome to APFEE.

Your {plan_name} subscription is now active.

To connect your Telegram account and start filtering signals, click below:

{activation_link}

This link expires in 48 hours.

Once connected, forward any trading signal to your APFEE bot
and it will analyze it instantly.

Replies to send after each signal report:
YES - execute the trade
NO - skip the trade
WIN - mark trade as winner
LOSS - mark trade as loser

If you need help visit apfee.io

The APFEE Team"""

    try:
        msg = MIMEMultipart()
        msg["Subject"] = "Your APFEE account is ready"
        msg["From"] = FROM_EMAIL
        msg["To"] = to_email
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_EMAIL, to_email, msg.as_string())

        logger.info(f"Activation email sent to {to_email}")
    except Exception as e:
        logger.error(f"Email send error: {e}")


async def send_welcome_message(user):
    from database import create_activation_token
    token = create_activation_token(user.id)
    if token:
        activation_link = f"https://t.me/{APFEE_BOT_USERNAME}?start={token}"
        send_activation_email(user.email, activation_link, user.plan_tier)
        logger.info(f"Activation sent to {user.email} with token {token[:8]}...")
    else:
        logger.error(f"Failed to create activation token for {user.email}")


async def send_cancellation_message(user):
    if not user.telegram_chat_id:
        return
    await send_telegram(
        user.telegram_chat_id,
        "Your APFEE subscription has ended.\n"
        "Your account has been deactivated.\n\n"
        "To resubscribe visit apfee.io\n"
        "We hope to see you back soon."
    )


async def send_subscription_confirmed(chat_id: str, plan: str, email: str):
    plan_labels = {
        "basic": "Basic ($47/mo)",
        "pro": "Pro ($97/mo)",
        "elite": "Elite ($197/mo)"
    }
    label = plan_labels.get(plan, plan)
    await send_telegram(
        chat_id,
        f"✅ APFEE is now active!\n\n"
        f"Plan: {label}\n"
        f"Email: {email}\n\n"
        f"Your account is live. Forward any trading signal to this chat "
        f"and APFEE will analyze it.\n\n"
        f"Commands:\n"
        f"/status — view your account state\n"
        f"/stats — view your trade history\n"
        f"/help — full command list\n\n"
        f"Reply YES, NO, WIN, or LOSS after each signal report."
    )
