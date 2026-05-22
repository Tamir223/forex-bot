"""
TNL Trader Database Module
PostgreSQL via Supabase. Handles all user, state, trade, and provider data.
"""

import os
import logging
import secrets
from datetime import datetime, timezone, date, timedelta
from dataclasses import dataclass, field
from typing import Optional
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# ─── DATA CLASSES ────────────────────────────────────────────────────────────

@dataclass
class User:
    id: int
    email: str
    telegram_chat_id: str
    plan_tier: str          # basic, pro, elite
    is_active: bool
    created_at: datetime
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    firm_code: str = "ftmo"


@dataclass
class UserState:
    user_id: int
    trades_today: int = 0
    open_trades: int = 0
    live_exposure: float = 0.0
    session_losses: int = 0
    weekly_losses: int = 0
    daily_pnl: float = 0.0
    win_streak: int = 0
    last_trade_date: Optional[date] = None
    last_reset_date: Optional[date] = None
    challenge_target: float = 10.0
    challenge_start_balance: float = 10000.0
    challenge_pnl: float = 0.0
    challenge_start_date: Optional[date] = None


@dataclass
class Trade:
    user_id: int
    pair: str
    direction: str
    grade: str
    confidence: int
    risk_percent: float
    signal_source: str
    entry_zone: Optional[str] = None
    stop_loss: Optional[str] = None
    result: str = "PENDING"   # PENDING, WIN, LOSS, SKIPPED
    id: Optional[int] = None
    created_at: Optional[datetime] = None


# ─── SCHEMA ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                      SERIAL PRIMARY KEY,
    email                   TEXT UNIQUE NOT NULL,
    telegram_chat_id        TEXT UNIQUE,
    plan_tier               TEXT NOT NULL DEFAULT 'basic',
    is_active               BOOLEAN NOT NULL DEFAULT FALSE,
    stripe_customer_id      TEXT UNIQUE,
    stripe_subscription_id  TEXT UNIQUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_state (
    user_id                 INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    trades_today            INTEGER NOT NULL DEFAULT 0,
    open_trades             INTEGER NOT NULL DEFAULT 0,
    live_exposure           FLOAT NOT NULL DEFAULT 0.0,
    session_losses          INTEGER NOT NULL DEFAULT 0,
    weekly_losses           INTEGER NOT NULL DEFAULT 0,
    daily_pnl               FLOAT NOT NULL DEFAULT 0.0,
    win_streak              INTEGER NOT NULL DEFAULT 0,
    last_trade_date         DATE,
    last_reset_date         DATE,
    challenge_target        FLOAT NOT NULL DEFAULT 10.0,
    challenge_start_balance FLOAT NOT NULL DEFAULT 10000.0,
    challenge_pnl           FLOAT NOT NULL DEFAULT 0.0,
    challenge_start_date    DATE
);

CREATE TABLE IF NOT EXISTS trades (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pair            TEXT,
    direction       TEXT,
    grade           TEXT,
    confidence      INTEGER,
    risk_percent    FLOAT,
    signal_source   TEXT,
    entry_zone      TEXT,
    stop_loss       TEXT,
    result          TEXT NOT NULL DEFAULT 'PENDING',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS provider_stats (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider_name   TEXT NOT NULL,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    win_rate        FLOAT NOT NULL DEFAULT 0.0,
    UNIQUE(user_id, provider_name)
);

CREATE TABLE IF NOT EXISTS activation_tokens (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token       TEXT UNIQUE NOT NULL,
    used        BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def init_db():
    """Create all tables if they don't exist"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            # Add challenge columns to existing tables
            cur.execute("ALTER TABLE user_state ADD COLUMN IF NOT EXISTS challenge_target FLOAT DEFAULT 10.0")
            cur.execute("ALTER TABLE user_state ADD COLUMN IF NOT EXISTS challenge_start_balance FLOAT DEFAULT 10000.0")
            cur.execute("ALTER TABLE user_state ADD COLUMN IF NOT EXISTS challenge_pnl FLOAT DEFAULT 0.0")
            cur.execute("ALTER TABLE user_state ADD COLUMN IF NOT EXISTS challenge_start_date DATE")
        conn.commit()
    logger.info("Database initialized")


# ─── USER FUNCTIONS ───────────────────────────────────────────────────────────

def get_user_by_chat_id(chat_id: str) -> Optional[User]:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM users WHERE telegram_chat_id = %s",
                    (str(chat_id),)
                )
                row = cur.fetchone()
                if row:
                    return User(**row)
    except Exception as e:
        logger.error(f"get_user_by_chat_id error: {e}")
    return None


def get_user_by_email(email: str) -> Optional[User]:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE email = %s", (email,))
                row = cur.fetchone()
                if row:
                    return User(**row)
    except Exception as e:
        logger.error(f"get_user_by_email error: {e}")
    return None


def get_user_by_stripe_customer(customer_id: str) -> Optional[User]:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM users WHERE stripe_customer_id = %s",
                    (customer_id,)
                )
                row = cur.fetchone()
                if row:
                    return User(**row)
    except Exception as e:
        logger.error(f"get_user_by_stripe_customer error: {e}")
    return None


def create_user(email: str, stripe_customer_id: str = None,
                stripe_subscription_id: str = None, plan_tier: str = "pro") -> Optional[User]:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO users (email, plan_tier, is_active,
                       stripe_customer_id, stripe_subscription_id)
                       VALUES (%s, %s, TRUE, %s, %s)
                       RETURNING *""",
                    (email, plan_tier, stripe_customer_id, stripe_subscription_id)
                )
                row = cur.fetchone()
                user = User(**row)

                # Create default state record
                cur.execute(
                    "INSERT INTO user_state (user_id) VALUES (%s)", (user.id,)
                )
            conn.commit()
            logger.info(f"Created user {email}")
            return user
    except Exception as e:
        logger.error(f"create_user error: {e}")
    return None


def link_telegram(user_id: int, chat_id: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET telegram_chat_id = %s WHERE id = %s",
                    (str(chat_id), user_id)
                )
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"link_telegram error: {e}")
    return False


def set_user_active(user_id: int, active: bool) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET is_active = %s WHERE id = %s",
                    (active, user_id)
                )
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"set_user_active error: {e}")
    return False


def get_all_active_users() -> list:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE is_active = TRUE")
                return [User(**row) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"get_all_active_users error: {e}")
    return []


# ─── STATE FUNCTIONS ──────────────────────────────────────────────────────────

def get_state(user_id: int) -> UserState:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM user_state WHERE user_id = %s", (user_id,)
                )
                row = cur.fetchone()
                if row:
                    return UserState(**row)
                # Create if missing
                cur.execute(
                    "INSERT INTO user_state (user_id) VALUES (%s) RETURNING *",
                    (user_id,)
                )
                row = cur.fetchone()
                conn.commit()
                return UserState(**row)
    except Exception as e:
        logger.error(f"get_state error: {e}")
        return UserState(user_id=user_id)


def _reset_daily_if_needed(user_id: int, state: UserState):
    """Reset daily counters if it's a new day"""
    today = date.today()
    if state.last_reset_date != today:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE user_state SET
                       trades_today = 0,
                       session_losses = 0,
                       daily_pnl = 0.0,
                       last_reset_date = %s
                       WHERE user_id = %s""",
                    (today, user_id)
                )
            conn.commit()


def log_trade_opened(user_id: int, risk_percent: float):
    try:
        state = get_state(user_id)
        _reset_daily_if_needed(user_id, state)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE user_state SET
                       trades_today = trades_today + 1,
                       open_trades = open_trades + 1,
                       live_exposure = live_exposure + %s,
                       last_trade_date = %s
                       WHERE user_id = %s""",
                    (risk_percent, date.today(), user_id)
                )
            conn.commit()
    except Exception as e:
        logger.error(f"log_trade_opened error: {e}")


def log_trade_win(user_id: int, risk_percent: float):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE user_state SET
                       open_trades = GREATEST(0, open_trades - 1),
                       live_exposure = GREATEST(0, live_exposure - %s),
                       win_streak = win_streak + 1
                       WHERE user_id = %s""",
                    (risk_percent, user_id)
                )
            conn.commit()
    except Exception as e:
        logger.error(f"log_trade_win error: {e}")


def log_trade_loss(user_id: int, risk_percent: float):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE user_state SET
                       open_trades = GREATEST(0, open_trades - 1),
                       live_exposure = GREATEST(0, live_exposure - %s),
                       session_losses = session_losses + 1,
                       weekly_losses = weekly_losses + 1,
                       win_streak = 0
                       WHERE user_id = %s""",
                    (risk_percent, user_id)
                )
            conn.commit()
    except Exception as e:
        logger.error(f"log_trade_loss error: {e}")


def reset_weekly_losses_all():
    """Reset weekly losses for all users - runs every Monday midnight UTC"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE user_state SET weekly_losses = 0")
            conn.commit()
        logger.info("Weekly losses reset for all users")
    except Exception as e:
        logger.error(f"reset_weekly_losses_all error: {e}")


def get_challenge_pnl(user_id: int) -> float:
    """Returns the current cumulative challenge P&L for a user"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT challenge_pnl FROM user_state WHERE user_id = %s",
                    (user_id,)
                )
                row = cur.fetchone()
                if row:
                    return float(row["challenge_pnl"] or 0.0)
    except Exception as e:
        logger.error(f"get_challenge_pnl error: {e}")
    return 0.0


def update_challenge_pnl(user_id: int, pnl_change: float):
    """Update the running cumulative challenge P&L total"""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE user_state
                       SET challenge_pnl = challenge_pnl + %s
                       WHERE user_id = %s""",
                    (pnl_change, user_id)
                )
            conn.commit()
    except Exception as e:
        logger.error(f"update_challenge_pnl error: {e}")


# ─── TRADE LOGGING ────────────────────────────────────────────────────────────

def log_trade(trade: Trade) -> Optional[int]:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO trades
                       (user_id, pair, direction, grade, confidence,
                        risk_percent, signal_source, entry_zone, stop_loss, result)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (trade.user_id, trade.pair, trade.direction, trade.grade,
                     trade.confidence, trade.risk_percent, trade.signal_source,
                     trade.entry_zone, trade.stop_loss, trade.result)
                )
                trade_id = cur.fetchone()["id"]
            conn.commit()
            return trade_id
    except Exception as e:
        logger.error(f"log_trade error: {e}")
    return None


def update_trade_result(trade_id: int, result: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trades SET result = %s WHERE id = %s",
                    (result, trade_id)
                )
            conn.commit()
    except Exception as e:
        logger.error(f"update_trade_result error: {e}")


def get_user_trades(user_id: int, limit: int = 20) -> list:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM trades WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                    (user_id, limit)
                )
                return cur.fetchall()
    except Exception as e:
        logger.error(f"get_user_trades error: {e}")
    return []


# ─── PROVIDER STATS ───────────────────────────────────────────────────────────

def get_provider_stats(user_id: int, provider: str) -> dict:
    if not provider or provider == "UNKNOWN":
        return {"total_trades": 0, "win_rate": None, "confidence_modifier": -2}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM provider_stats WHERE user_id = %s AND provider_name = %s",
                    (user_id, provider)
                )
                row = cur.fetchone()
                if not row or row["total_trades"] < 5:
                    return {
                        "total_trades": row["total_trades"] if row else 0,
                        "win_rate": None,
                        "confidence_modifier": 0
                    }
                wr = row["win_rate"]
                modifier = 1 if wr >= 0.65 else (-2 if wr <= 0.35 else 0)
                return {
                    "total_trades": row["total_trades"],
                    "wins": row["wins"],
                    "losses": row["losses"],
                    "win_rate": wr,
                    "confidence_modifier": modifier
                }
    except Exception as e:
        logger.error(f"get_provider_stats error: {e}")
    return {"total_trades": 0, "win_rate": None, "confidence_modifier": 0}


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


def update_provider_result(user_id: int, provider: str, won: bool):
    if not provider or provider == "UNKNOWN":
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO provider_stats (user_id, provider_name, total_trades, wins, losses, win_rate)
                       VALUES (%s, %s, 1, %s, %s, %s)
                       ON CONFLICT (user_id, provider_name) DO UPDATE SET
                           total_trades = provider_stats.total_trades + 1,
                           wins = provider_stats.wins + %s,
                           losses = provider_stats.losses + %s,
                           win_rate = (provider_stats.wins + %s)::float /
                                      (provider_stats.total_trades + 1)""",
                    (user_id, provider,
                     1 if won else 0, 0 if won else 1,
                     1.0 if won else 0.0,
                     1 if won else 0, 0 if won else 1,
                     1 if won else 0)
                )
            conn.commit()
    except Exception as e:
        logger.error(f"update_provider_result error: {e}")


def set_user_firm(user_id: int, firm_code: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET firm_code = %s WHERE id = %s", (firm_code, user_id))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"set_user_firm error: {e}")
        return False

def get_user_firm(user_id: int) -> str:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT firm_code FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                return row[0] if row and row[0] else "ftmo"
    except Exception as e:
        logger.error(f"get_user_firm error: {e}")
        return "ftmo"

def save_challenge_state(user_id: int, firm_code: str, state_json: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO challenge_state (user_id, firm_code, state_json, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                    SET firm_code = EXCLUDED.firm_code,
                        state_json = EXCLUDED.state_json,
                        updated_at = NOW()
                """, (user_id, firm_code, state_json))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"save_challenge_state error: {e}")
        return False

def load_challenge_state(user_id: int):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state_json FROM challenge_state WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        logger.error(f"load_challenge_state error: {e}")
        return None

def reset_challenge_state(user_id: int) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM challenge_state WHERE user_id = %s", (user_id,))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"reset_challenge_state error: {e}")
        return False

def get_recent_trades(user_id: int, limit: int = 10) -> list:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, pair, direction, grade, score, pnl, result, firm_code, created_at
                    FROM trades
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (user_id, limit))
                return cur.fetchall()
    except Exception as e:
        logger.error(f"get_recent_trades error: {e}")
        return []
