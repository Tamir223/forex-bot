# APFEE Complete Setup Guide + Claude Code Prompt

---

# PART 1 — ACCOUNTS TO CREATE BEFORE ANYTHING ELSE

Create these five accounts first. All have free tiers. Takes about 30 minutes total.

---

## 1. Telegram Bot

1. Open Telegram and search @BotFather
2. Send /newbot
3. Name: APFEE Signal Engine
4. Username: APFEESignalBot (must end in bot)
5. Copy the bot token. Format: 1234567890:ABCDef...
6. Save it. This goes in TELEGRAM_BOT_TOKEN in your .env

---

## 2. Anthropic API Key

1. Go to console.anthropic.com
2. Sign in or create account
3. Go to API Keys
4. Create new key
5. Copy it. This goes in ANTHROPIC_API_KEY in your .env

---

## 3. Twelve Data API Key (live price feed)

1. Go to twelvedata.com
2. Sign up free
3. Go to your dashboard and copy your API key
4. Free tier gives 800 requests per day which is more than enough
5. This goes in TWELVE_DATA_API_KEY in your .env

---

## 4. Stripe Account

1. Go to stripe.com and create an account
2. Complete business verification
3. Go to Products and create three subscription products:
   - APFEE Basic at $47 per month
   - APFEE Pro at $97 per month
   - APFEE Elite at $197 per month
4. For each product copy the Price ID (starts with price_)
5. Go to Developers > API Keys and copy your Secret key (starts with sk_live_)
6. These go in STRIPE_SECRET_KEY, STRIPE_PRICE_BASIC, STRIPE_PRICE_PRO, STRIPE_PRICE_ELITE

---

## 5. Resend Email Account

1. Go to resend.com and create account
2. Add and verify your domain (e.g. apfee.io) or use their test domain to start
3. Go to API Keys and create one
4. The API key is your SMTP password
5. SMTP settings:
   - Host: smtp.resend.com
   - Port: 587
   - User: resend
   - Pass: your API key
6. These go in SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS in your .env

---

# PART 2 — AWS SETUP

---

## Step 1 — Launch EC2 Instance

1. Log into AWS Console at console.aws.amazon.com
2. Go to EC2 > Instances > Launch Instance
3. Settings:
   - Name: apfee-production
   - AMI: Ubuntu Server 22.04 LTS (free tier eligible)
   - Instance type: t2.micro (free tier)
   - Key pair: Create new key pair, name it apfee-key, download the .pem file
   - Security group: Create new with these inbound rules:
     - SSH port 22 from your IP only
     - HTTP port 80 from anywhere
     - HTTPS port 443 from anywhere
     - Custom TCP port 8000 from anywhere
4. Click Launch Instance
5. Save the .pem file somewhere safe. You cannot download it again.

---

## Step 2 — Assign Elastic IP

1. In EC2 dashboard go to Elastic IPs
2. Click Allocate Elastic IP address
3. Click Allocate
4. Select the new IP, click Actions > Associate Elastic IP
5. Select your apfee-production instance
6. Click Associate
7. Note your Elastic IP address. This never changes.

---

## Step 3 — Launch RDS PostgreSQL Database

1. Go to AWS RDS > Create database
2. Settings:
   - Engine: PostgreSQL
   - Template: Free tier
   - DB instance identifier: apfee-db
   - Master username: apfee
   - Master password: create a strong password and save it
   - Instance type: db.t3.micro
   - Storage: 20 GB
   - VPC: same as your EC2 instance
   - Public access: No
   - VPC security group: Create new, name it apfee-db-sg
3. Click Create database
4. Wait 5-10 minutes for it to be available
5. Once available, click on the database and copy the Endpoint
6. Your DATABASE_URL will be: postgresql://apfee:YOUR_PASSWORD@ENDPOINT:5432/postgres

---

## Step 4 — Allow EC2 to Connect to RDS

1. Go to your RDS database > Connectivity and security
2. Click the VPC security group (apfee-db-sg)
3. Click Edit inbound rules
4. Add rule:
   - Type: PostgreSQL
   - Port: 5432
   - Source: the security group of your EC2 instance
5. Save rules

---

## Step 5 — SSH Into Your Server

On your Mac terminal:

```bash
chmod 400 ~/Downloads/apfee-key.pem
ssh -i ~/Downloads/apfee-key.pem ubuntu@YOUR_ELASTIC_IP
```

You are now inside your AWS server.

---

## Step 6 — Run the Setup Script

```bash
# Clone your repo
git clone https://github.com/YOUR_GITHUB_USERNAME/prop-firm-engine.git /home/ubuntu/apfee
cd /home/ubuntu/apfee

# Run setup
bash aws-setup.sh
```

This installs everything automatically.

---

## Step 7 — Fill In Your .env File

```bash
nano /home/ubuntu/apfee/.env
```

Fill in every value using the credentials you collected in Part 1 and the RDS endpoint from Step 3.

Save with Ctrl+X then Y then Enter.

---

## Step 8 — Start APFEE

```bash
sudo systemctl start apfee
sudo systemctl status apfee
```

You should see active (running) in green.

To watch live logs:
```bash
sudo journalctl -u apfee -f
```

---

## Step 9 — Register Stripe Webhook

1. Go to Stripe Dashboard > Developers > Webhooks
2. Click Add endpoint
3. Endpoint URL: http://YOUR_ELASTIC_IP/webhook/stripe
4. Events to listen to:
   - checkout.session.completed
   - customer.subscription.deleted
   - invoice.payment_failed
   - customer.subscription.updated
5. Click Add endpoint
6. Click the endpoint you just created
7. Copy the Signing secret (starts with whsec_)
8. Add this to your .env as STRIPE_WEBHOOK_SECRET
9. Restart APFEE: sudo systemctl restart apfee

---

## Step 10 — Set Up GitHub Auto-Deploy

1. Go to your GitHub repo > Settings > Secrets and variables > Actions
2. Add two secrets:
   - EC2_HOST: your Elastic IP address
   - EC2_KEY: the full contents of your apfee-key.pem file
3. From now on every git push to main automatically deploys to AWS

---

# PART 3 — CLAUDE CODE PROMPT

Open Claude Code inside your repo folder in VS Code and paste everything below this line.

---

You are building APFEE v3.0 — a multi-user SaaS trading signal filter.
The repo is connected to GitHub via VS Code. All files from the APFEE-final
folder need to be added to the repo.

## What APFEE does

One central Telegram bot serves all paying subscribers. A user pays on a
website, receives an activation email with a bot link, messages the bot,
and is instantly live. You run all infrastructure. They never touch any
configuration.

## Step 1 — Add all source files to the repo

Add these files exactly as provided:

main.py — runs bot, webhook server, and weekly reset together
database.py — PostgreSQL models and all data functions
webhooks.py — FastAPI Stripe webhook handler
notifications.py — email and Telegram onboarding messages
bot.py — multi-user Telegram bot
claude.py — Claude API with per-user provider tracking
filter.py — gate logic with confluence scoring
market.py — live price and ATR from Twelve Data
calendar.py — economic calendar news check
report.py — all message templates
config.py — all settings and system prompt
reset.py — weekly loss reset scheduler
requirements.txt — all dependencies
aws-setup.sh — EC2 server setup script
deploy.sh — update and restart script
.env.template — credential template
.gitignore — keeps credentials out of git
.github/workflows/deploy.yml — GitHub Actions auto-deploy

## Step 2 — Add activation token table to database.py

Add this to the SCHEMA string inside database.py after the provider_stats table:

CREATE TABLE IF NOT EXISTS activation_tokens (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token       TEXT UNIQUE NOT NULL,
    used        BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

Then add this function to database.py:

import secrets

def create_activation_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=48)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO activation_tokens (user_id, token, expires_at) VALUES (%s, %s, %s)",
                    (user_id, token, expires)
                )
            conn.commit()
        return token
    except Exception as e:
        logger.error(f"create_activation_token error: {e}")
        return None

## Step 3 — Add email sending to notifications.py

Add these imports at the top of notifications.py:
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

Add these env vars below the existing ones:
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.resend.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@apfee.io")

Add this function:

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

Update the send_welcome_message function to call create_activation_token and
send_activation_email after a user is created:

async def send_welcome_message(user):
    from database import create_activation_token
    token = create_activation_token(user.id)
    if token:
        onboarding_url = os.getenv("ONBOARDING_URL", "https://apfee.io/setup")
        activation_link = f"https://t.me/{os.getenv('APFEE_BOT_USERNAME')}?start={token}"
        send_activation_email(user.email, activation_link, user.plan_tier)
        logger.info(f"Activation sent to {user.email} with token {token[:8]}...")

## Step 4 — Fix all imports and check for errors

After all files are in place:
1. Check every import statement in every file
2. Make sure there are no circular imports
3. Make sure all function calls reference functions that exist
4. Make sure database.py timedelta is imported from datetime

## Step 5 — Verify these specific things

In database.py confirm that timedelta is imported:
from datetime import datetime, timezone, date, timedelta

In bot.py confirm that the _verify_activation_token function
uses the activation_tokens table we added to the schema

In webhooks.py confirm that send_welcome_message is awaited correctly
and that create_activation_token is called after user creation

In main.py confirm that init_db() is called before start_bot()

## Step 6 — Create a simple health check

In webhooks.py the /health endpoint already exists. Make sure it returns:
{"status": "ok", "version": "3.0", "service": "APFEE"}

## Step 7 — Commit and push to GitHub

git add .
git commit -m "APFEE v3.0 - full SaaS with AWS deployment, Stripe payments, PostgreSQL, auto-onboarding"
git push origin main
git tag -a v3.0 -m "APFEE v3.0"
git push origin v3.0

## What the final user flow looks like

User visits your website and clicks buy
Stripe charges them and fires checkout.session.completed webhook
webhooks.py receives it, creates user in PostgreSQL database
create_activation_token generates a 48-hour token
send_activation_email sends them an email with their bot link
User clicks the link which opens Telegram with /start TOKEN pre-filled
bot.py receives /start TOKEN, verifies it, links their chat_id to their account
User gets a welcome message confirming they are live
User forwards a signal, bot checks database, finds active subscription, processes it
Everything runs on your AWS EC2 server automatically

---

# PART 4 — AFTER DEPLOYMENT CHECKLIST

Run through this after your server is live:

1. Test Stripe webhook
   Go to Stripe > Webhooks > your endpoint > Send test webhook
   Select checkout.session.completed
   Check your server logs: sudo journalctl -u apfee -f
   You should see user creation logged

2. Test activation email
   Complete a test purchase on Stripe test mode
   Check the email arrives
   Click the activation link
   Message your bot
   Confirm the welcome message appears

3. Test signal processing
   Send a DON PIPS signal to your bot
   Confirm the analysis report comes back
   Reply YES
   Check /status shows trades_today incremented

4. Test subscription cancellation
   Cancel the test subscription in Stripe
   Confirm the bot replies with not subscribed message

5. Confirm auto-deploy works
   Make a small change to README
   git add . && git commit -m "test deploy" && git push origin main
   Watch GitHub Actions tab — should show green checkmark
   SSH in and confirm the change is live

---

# PART 5 — ONGOING OPERATIONS

## Checking if the bot is running
```bash
sudo systemctl status apfee
```

## Viewing live logs
```bash
sudo journalctl -u apfee -f
```

## Restarting manually
```bash
sudo systemctl restart apfee
```

## Deploying an update
Just push to main. GitHub Actions handles the rest.
Or SSH in and run: bash deploy.sh

## Checking database
```bash
psql postgresql://apfee:PASSWORD@RDS_ENDPOINT:5432/postgres
SELECT count(*) FROM users WHERE is_active = TRUE;
SELECT * FROM users ORDER BY created_at DESC LIMIT 10;
SELECT * FROM trades ORDER BY created_at DESC LIMIT 20;
```

## Adding a new signal provider
Edit config.py and add to the KNOWN PROVIDERS section in SYSTEM_PROMPT
Push to main and it auto-deploys

## Changing risk gate defaults
Edit config.py and change the default values
Push to main and it auto-deploys

---

# PART 6 — COST SUMMARY

## AWS (after free tier, roughly month 13+)
- EC2 t2.micro: $8.50/month
- RDS db.t3.micro: $13/month
- Elastic IP: $0 while instance running
- Data transfer: $1-2/month
- Total AWS: roughly $23/month

## Other services
- Stripe: 2.9% per transaction (no monthly fee)
- Resend: free up to 3,000 emails/month, then $20/month
- Twelve Data: free tier 800 requests/day
- Anthropic Claude API: roughly $0.002 per signal analyzed (haiku model)
- Total other: $0-5/month to start

## Revenue vs cost at different subscriber counts
- 10 subscribers at $97: $970/month revenue, $25/month costs
- 50 subscribers at $97: $4,850/month revenue, $25/month costs
- 100 subscribers at $97: $9,700/month revenue, $25/month costs
- 500 subscribers at $97: $48,500/month revenue, $50/month costs

The infrastructure cost is essentially flat. You add users without adding cost.
