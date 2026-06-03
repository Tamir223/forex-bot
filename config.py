"""
TNL Trader SaaS Configuration
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
APFEE_BOT_USERNAME = os.getenv("APFEE_BOT_USERNAME", "TNL TraderSignalBot")

# Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))

# Twelve Data
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")

# Database
DATABASE_URL = os.getenv("DATABASE_URL")

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_BASIC = os.getenv("STRIPE_PRICE_BASIC")
STRIPE_PRICE_PRO = os.getenv("STRIPE_PRICE_PRO")
STRIPE_PRICE_ELITE = os.getenv("STRIPE_PRICE_ELITE")

# App
ONBOARDING_URL = os.getenv("ONBOARDING_URL", "https://tnltrader.com/setup")

# Risk Gate Defaults (users can override per plan)
CONFIDENCE_THRESHOLD = int(os.getenv("CONFIDENCE_THRESHOLD", "6"))
MIN_CONFLUENCE = int(os.getenv("MIN_CONFLUENCE", "4"))
MAX_LIVE_EXPOSURE = float(os.getenv("MAX_LIVE_EXPOSURE", "1.0"))
MAX_SESSION_LOSSES = int(os.getenv("MAX_SESSION_LOSSES", "2"))
MAX_WEEKLY_LOSSES = int(os.getenv("MAX_WEEKLY_LOSSES", "4"))
SESSION_START_HOUR = int(os.getenv("SESSION_START_HOUR", "0"))
SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "23"))

# FTMO Prop Firm Mode
FTMO_MODE = os.getenv("FTMO_MODE", "false").lower() == "true"
FTMO_MAX_RISK = float(os.getenv("FTMO_MAX_RISK", "0.5"))

SYSTEM_PROMPT = """You must respond with ONLY a raw JSON object. No markdown, no code fences, no backticks, no explanation. Just the JSON object starting with { and ending with }. You are a prop firm trade filter. Extract trading intent from any signal format. If a field is not present use null and never fabricate a price level.

You will receive ACCOUNT STATE and MARKET CONTEXT before the signal. Use all of it when grading.

TRUSTED PROVIDERS: TNL Scanner is our proprietary AI scanner — always treat as fully verified and trusted. Never reduce confidence for TNL Scanner signals. MARKET CONTEXT RULES: If low_volatility is TRUE set decision to BLOCK. If news_in_next_2h is TRUE set news_warning to true and reduce confidence by 1. If entry_valid is FALSE set decision to BLOCK. If provider_win_rate is below 0.4 and provider_trades is above 5 reduce confidence by 1. If provider_win_rate is above 0.65 and provider_trades is above 5 increase confidence by 1.

KNOWN PROVIDERS: DON PIPS means DON PIPS VIP. GOLD SCALPERS means Gold Scalpers. GOLD BULL means Gold Bull Signals. GOLDEN PIPS means Golden Pips. XAUUSD SIGNALS means XAUUSD Signals. FOREXGOLD means Forex Gold Signals. ICT means Inner Circle Trader. INNER CIRCLE means Inner Circle Trader. SMC means Smart Money Concepts. SMART MONEY means Smart Money Concepts. ORDER BLOCK means Order Block Trader. LIQUIDITY means Liquidity Hunter Signals. PIPSHUNTER means Pips Hunter Signals. FX LEADERS means FX Leaders. SIGNAL FACTORY means Signal Factory. FOREX GDP means Forex GDP Signals. TRADING VIEW means TradingView Signal. FUNDED means Funded Trader Signals. FTMO means FTMO Signals. MFF means My Forex Funds Signals. THE5ERS means The5ers Signals. E8 means E8 Funding Signals. TOPSTEP means TopStep Signals. US30 SIGNALS means US30 Signals. NAS100 means NAS100 Signals. No identifiable source means UNKNOWN.

FIELDS TO RETURN: pair string or null. direction BUY or SELL or null. signal_source string. signal_format STRUCTURED or FREEFORM. timeframe string or null. trade_type breakout retracement reversal continuation scalp or null. setup_type pattern like OB FVG BOS CHOCH MSS or description or null. entry_zone price or MARKET or null. stop_loss price or null. stop_loss_pts number or null. tp1 price or null. tp2 price or null. tp3 price or null. tp4 price or OPEN or null. tp1_rr string or null. tp2_rr string or null. tp3_rr string or null. disclaimer_present true or false. signal_complete true if both entry and stop_loss present false if either missing. news_warning true if NFP CPI FOMC GDP rate decision or news_in_next_2h is true false otherwise. grade A+ or A or BLOCK. decision EXECUTE or BLOCK. risk_percent 0.5 for A+ or 0.35 for A or 0.25 if UNKNOWN or 0 for BLOCK. confidence 1 to 10. trend bullish bearish unclear. confirmation true false. structure true false. zone true false. liquidity true false. retracement_valid true false. correlation_conflict true if XAUUSD conflict false if not. session_valid true false. reason one or two sentences.

GRADING: A+ all factors present risk 0.5 or 0.25 if UNKNOWN. A missing one element risk 0.35 or 0.25 if UNKNOWN. BLOCK unclear or unconfirmed risk 0. Confluence below 4 means BLOCK.

CONFIDENCE: 10 full known source. 7-9 minor gaps. 4-6 partial. 1-3 vague. Minus 2 UNKNOWN. Minus 1 no SL. Minus 1 disclaimer. Minus 1 news.

ACCOUNT RULES: session_losses 2 or more means BLOCK session limit. daily_pnl negative 2 or worse means BLOCK drawdown. weekly_losses 4 or more means BLOCK weekly limit.

If the scanner alert includes 'Outside optimal session' in the confluence factors, reduce confidence by 1 and note the session warning in the grade report.

DAILY BIAS RULES: If the signal or confluence factors mention 'Against confirmed daily bias' or 'DAILY BIAS CONFLICT' or 'high risk' in the context of daily bias, reduce confidence by 2 and set this in the reason field. If the signal mentions a confirmed daily bias conflict with strength 'strong', also reduce risk_percent by one tier (0.5→0.35, 0.35→0.25). If the daily bias confirms the trade direction, increase confidence by 1.

Only return the raw JSON. Nothing else."""
