"""
TNL Trader Stripe Webhook Handler
Handles subscription events from Stripe.
Automatically activates and deactivates user accounts.
"""

import os
import logging
import stripe
from fastapi import FastAPI, Request, HTTPException
from database import (
    get_user_by_email, get_user_by_stripe_customer,
    create_user, set_user_active, link_telegram
)
from notifications import send_welcome_message, send_cancellation_message

logger = logging.getLogger(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# Map Stripe price IDs to plan tiers
PRICE_TO_PLAN = {
    os.getenv("STRIPE_PRICE_BASIC"):  "basic",
    os.getenv("STRIPE_PRICE_PRO"):    "pro",
    os.getenv("STRIPE_PRICE_ELITE"):  "elite",
}

app = FastAPI()


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event["type"]
    data = event["data"]["object"]

    logger.info(f"Stripe event: {event_type}")

    # ── New subscription created ──────────────────────────────────────────────
    if event_type == "checkout.session.completed":
        email = data.get("customer_email") or data.get("customer_details", {}).get("email")
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        price_id = None

        # Get price from line items
        try:
            session = stripe.checkout.Session.retrieve(
                data["id"], expand=["line_items"]
            )
            items = session.line_items.data
            if items:
                price_id = items[0].price.id
        except Exception as e:
            logger.error(f"Could not get price from session: {e}")

        plan = PRICE_TO_PLAN.get(price_id, "pro")

        if email:
            existing = get_user_by_email(email)
            if existing:
                set_user_active(existing.id, True)
                logger.info(f"Reactivated user {email}")
            else:
                user = create_user(
                    email=email,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    plan_tier=plan
                )
                if user:
                    logger.info(f"Created new user {email} on {plan} plan")
                    await send_welcome_message(user)

    # ── Subscription cancelled or payment failed ──────────────────────────────
    elif event_type in ["customer.subscription.deleted", "invoice.payment_failed"]:
        customer_id = data.get("customer")
        user = get_user_by_stripe_customer(customer_id)
        if user:
            set_user_active(user.id, False)
            await send_cancellation_message(user)
            logger.info(f"Deactivated user {user.email}")

    # ── Subscription updated (plan change) ───────────────────────────────────
    elif event_type == "customer.subscription.updated":
        customer_id = data.get("customer")
        user = get_user_by_stripe_customer(customer_id)
        if user:
            items = data.get("items", {}).get("data", [])
            if items:
                price_id = items[0].get("price", {}).get("id")
                new_plan = PRICE_TO_PLAN.get(price_id, user.plan_tier)
                if new_plan != user.plan_tier:
                    from database import get_conn
                    with get_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE users SET plan_tier = %s WHERE id = %s",
                                (new_plan, user.id)
                            )
                        conn.commit()
                    logger.info(f"User {user.email} upgraded to {new_plan}")

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.0", "service": "TNL Trader"}
