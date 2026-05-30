"""
Phase 4 — Self-Learning System
Analyzes trade history to improve signal confidence and provide insights.
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv('/home/ubuntu/apfee/.env')
logger = logging.getLogger(__name__)


def get_conn():
    return psycopg2.connect(os.getenv('DATABASE_URL'))


# ─── TRADE INSIGHT LOGGING ────────────────────────────────────────────────────

def log_trade_insight(user_id: int, trade_data: dict):
    """Log detailed trade data for self-learning analysis."""
    try:
        now = datetime.now()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO trade_insights
                    (user_id, pair, direction, setup_type, session,
                     hour_of_day, day_of_week, score, grade, result,
                     pnl, hold_minutes, firm_code)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                """, (
                    user_id,
                    trade_data.get('pair', ''),
                    trade_data.get('direction', ''),
                    trade_data.get('setup_type', ''),
                    trade_data.get('session', get_session(now.hour)),
                    now.hour,
                    now.weekday(),
                    trade_data.get('score', 0),
                    trade_data.get('grade', ''),
                    trade_data.get('result', ''),
                    trade_data.get('pnl', 0),
                    trade_data.get('hold_minutes', 0),
                    trade_data.get('firm_code', 'ftmo'),
                ))
            conn.commit()
    except Exception as e:
        logger.error(f"log_trade_insight error: {e}")


def get_session(hour_utc: int) -> str:
    """Determine trading session from UTC hour."""
    if 7 <= hour_utc < 10:
        return 'London Open'
    elif 10 <= hour_utc < 13:
        return 'London'
    elif 13 <= hour_utc < 16:
        return 'NY Open'
    elif 16 <= hour_utc < 20:
        return 'NY'
    elif 20 <= hour_utc < 23:
        return 'NY Close'
    else:
        return 'Asian'


# ─── LEARNING ANALYSIS ────────────────────────────────────────────────────────

def get_pair_performance(user_id: int, pair: str, days: int = 30) -> dict:
    """Get win rate and avg PnL for a specific pair."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        COUNT(*) as total,
                        SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
                        AVG(pnl) as avg_pnl,
                        SUM(pnl) as total_pnl
                    FROM trade_insights
                    WHERE user_id = %s 
                    AND pair = %s
                    AND created_at > NOW() - INTERVAL '%s days'
                    AND result IN ('WIN', 'LOSS')
                """, (user_id, pair, days))
                row = cur.fetchone()
                if row and row[0] > 0:
                    total, wins, losses, avg_pnl, total_pnl = row
                    return {
                        'total': total,
                        'wins': wins or 0,
                        'losses': losses or 0,
                        'win_rate': round((wins or 0) / total * 100, 1),
                        'avg_pnl': round(float(avg_pnl or 0), 2),
                        'total_pnl': round(float(total_pnl or 0), 2),
                    }
    except Exception as e:
        logger.error(f"get_pair_performance error: {e}")
    return {'total': 0, 'wins': 0, 'losses': 0, 'win_rate': 0, 'avg_pnl': 0, 'total_pnl': 0}


def get_session_performance(user_id: int, days: int = 30) -> dict:
    """Get win rate by trading session."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        session,
                        COUNT(*) as total,
                        SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                        AVG(pnl) as avg_pnl
                    FROM trade_insights
                    WHERE user_id = %s
                    AND created_at > NOW() - INTERVAL '%s days'
                    AND result IN ('WIN', 'LOSS')
                    GROUP BY session
                    ORDER BY wins DESC
                """, (user_id, days))
                rows = cur.fetchall()
                result = {}
                for row in rows:
                    session, total, wins, avg_pnl = row
                    if total > 0:
                        result[session] = {
                            'total': total,
                            'wins': wins or 0,
                            'win_rate': round((wins or 0) / total * 100, 1),
                            'avg_pnl': round(float(avg_pnl or 0), 2),
                        }
                return result
    except Exception as e:
        logger.error(f"get_session_performance error: {e}")
    return {}


def get_confidence_modifier(user_id: int, pair: str, session: str) -> float:
    """
    Calculate confidence modifier based on personal performance.
    Returns value between -2 and +2 to adjust bot confidence score.
    """
    try:
        pair_perf = get_pair_performance(user_id, pair, days=60)
        
        if pair_perf['total'] < 5:
            return 0.0  # Not enough data yet
        
        win_rate = pair_perf['win_rate']
        modifier = 0.0
        
        # Strong performer on this pair
        if win_rate >= 70:
            modifier += 1.5
        elif win_rate >= 60:
            modifier += 1.0
        elif win_rate >= 50:
            modifier += 0.5
        elif win_rate < 40:
            modifier -= 1.0
        elif win_rate < 30:
            modifier -= 2.0
            
        # Session bonus
        session_perf = get_session_performance(user_id, days=60)
        if session in session_perf:
            s = session_perf[session]
            if s['total'] >= 3:
                if s['win_rate'] >= 70:
                    modifier += 0.5
                elif s['win_rate'] < 40:
                    modifier -= 0.5
                    
        return round(max(-2.0, min(2.0, modifier)), 1)
        
    except Exception as e:
        logger.error(f"get_confidence_modifier error: {e}")
    return 0.0


def get_best_trading_hours(user_id: int, days: int = 30) -> list:
    """Find the UTC hours with highest win rate."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        hour_of_day,
                        COUNT(*) as total,
                        SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins
                    FROM trade_insights
                    WHERE user_id = %s
                    AND created_at > NOW() - INTERVAL '%s days'
                    AND result IN ('WIN', 'LOSS')
                    GROUP BY hour_of_day
                    HAVING COUNT(*) >= 2
                    ORDER BY (SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END)::float / COUNT(*)) DESC
                    LIMIT 5
                """, (user_id, days))
                rows = cur.fetchall()
                return [{'hour': r[0], 'total': r[1], 'wins': r[2],
                         'win_rate': round((r[2] / r[1]) * 100, 1)} for r in rows]
    except Exception as e:
        logger.error(f"get_best_trading_hours error: {e}")
    return []


# ─── MULTI-ACCOUNT MANAGEMENT ─────────────────────────────────────────────────

def get_user_accounts(user_id: int) -> list:
    """Get all active challenge accounts for a user."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT firm_code, account_label, balance, challenge_pnl,
                           challenge_days, start_balance, is_active, created_at
                    FROM user_accounts
                    WHERE user_id = %s AND is_active = TRUE
                    ORDER BY created_at ASC
                """, (user_id,))
                rows = cur.fetchall()
                accounts = []
                for row in rows:
                    accounts.append({
                        'firm_code': row[0],
                        'label': row[1] or row[0],
                        'balance': float(row[2] or 0),
                        'challenge_pnl': float(row[3] or 0),
                        'challenge_days': row[4] or 0,
                        'start_balance': float(row[5] or 0),
                        'is_active': row[6],
                    })
                return accounts
    except Exception as e:
        logger.error(f"get_user_accounts error: {e}")
    return []


def upsert_account(user_id: int, firm_code: str, start_balance: float,
                   label: str = None) -> bool:
    """Create or update a challenge account."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_accounts
                    (user_id, firm_code, account_label, balance,
                     start_balance, peak_balance, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (user_id, firm_code) DO UPDATE SET
                        is_active = TRUE,
                        account_label = EXCLUDED.account_label,
                        updated_at = NOW()
                """, (user_id, firm_code, label or firm_code,
                      start_balance, start_balance, start_balance))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"upsert_account error: {e}")
    return False


def update_account_pnl(user_id: int, firm_code: str, pnl_delta: float,
                       is_win: bool = True) -> bool:
    """Update account balance and PnL after a trade."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE user_accounts SET
                        balance = balance + %s,
                        challenge_pnl = challenge_pnl + %s,
                        peak_balance = GREATEST(peak_balance, balance + %s),
                        updated_at = NOW()
                    WHERE user_id = %s AND firm_code = %s
                """, (pnl_delta, pnl_delta, pnl_delta, user_id, firm_code))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"update_account_pnl error: {e}")
    return False


def get_all_accounts_summary(user_id: int) -> str:
    """Generate a summary of all active accounts."""
    accounts = get_user_accounts(user_id)
    if not accounts:
        return "No active challenge accounts. Use /challenge to start one."

    from prop_firm_profiles import get_profile
    lines = ["📊 *YOUR ACTIVE CHALLENGES*\n"]

    total_pnl = 0
    for i, acc in enumerate(accounts, 1):
        profile = get_profile(acc['firm_code'])
        pnl = acc['challenge_pnl']
        total_pnl += pnl
        start = acc['start_balance']
        target = profile.profit_target if profile else start * 0.10
        pct = round((pnl / target) * 100, 1) if target > 0 else 0
        pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
        emoji = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{i}. *{profile.name if profile else acc['firm_code']}*\n"
            f"   {emoji} P&L: {pnl_str} ({pct}% of target)\n"
            f"   Days: {acc['challenge_days']} | "
            f"Balance: ${acc['balance']:,.2f}\n"
        )

    total_str = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"
    lines.append(f"━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💰 *Combined P&L: {total_str}*")
    return "\n".join(lines)


# ─── PERFORMANCE INSIGHTS ─────────────────────────────────────────────────────

def get_performance_insights(user_id: int) -> str:
    """Generate personalized performance insights from trade history."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Overall stats last 30 days
                cur.execute("""
                    SELECT 
                        COUNT(*) as total,
                        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses,
                        SUM(pnl) as total_pnl,
                        AVG(pnl) as avg_pnl
                    FROM trade_insights
                    WHERE user_id = %s
                    AND created_at > NOW() - INTERVAL '30 days'
                    AND result IN ('WIN', 'LOSS')
                """, (user_id,))
                overall = cur.fetchone()

                # Best pair
                cur.execute("""
                    SELECT pair, 
                        COUNT(*) as total,
                        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                        SUM(pnl) as total_pnl
                    FROM trade_insights
                    WHERE user_id = %s
                    AND created_at > NOW() - INTERVAL '30 days'
                    AND result IN ('WIN', 'LOSS')
                    GROUP BY pair
                    HAVING COUNT(*) >= 2
                    ORDER BY SUM(pnl) DESC
                    LIMIT 1
                """, (user_id,))
                best_pair = cur.fetchone()

                # Best session
                cur.execute("""
                    SELECT session,
                        COUNT(*) as total,
                        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins
                    FROM trade_insights
                    WHERE user_id = %s
                    AND created_at > NOW() - INTERVAL '30 days'
                    AND result IN ('WIN', 'LOSS')
                    GROUP BY session
                    HAVING COUNT(*) >= 2
                    ORDER BY (SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END)::float/COUNT(*)) DESC
                    LIMIT 1
                """, (user_id,))
                best_session = cur.fetchone()

        total = overall[0] if overall else 0

        if total < 3:
            return (
                "📊 *Performance Insights*\n\n"
                "Not enough data yet — need at least 5 completed trades.\n"
                "Keep trading and logging WIN/LOSS after each trade.\n"
                "Insights will appear after 5+ trades."
            )

        wins = overall[1] or 0
        losses = overall[2] or 0
        total_pnl = float(overall[3] or 0)
        avg_pnl = float(overall[4] or 0)
        win_rate = round((wins / total) * 100, 1)

        lines = ["📊 *PERFORMANCE INSIGHTS — Last 30 Days*\n"]
        lines.append(f"Total trades: {total} | Win rate: {win_rate}%")
        lines.append(f"Wins: {wins} | Losses: {losses}")
        pnl_str = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"
        lines.append(f"Total P&L: {pnl_str} | Avg: ${avg_pnl:,.2f}\n")

        if best_pair:
            pair_wr = round((best_pair[2] / best_pair[1]) * 100, 1) if best_pair[1] > 0 else 0
            lines.append(f"🏆 *Best Pair:* {best_pair[0]}")
            lines.append(f"   Win rate: {pair_wr}% | P&L: ${float(best_pair[3]):,.2f}\n")

        if best_session:
            sess_wr = round((best_session[2] / best_session[1]) * 100, 1) if best_session[1] > 0 else 0
            lines.append(f"⏰ *Best Session:* {best_session[0]}")
            lines.append(f"   Win rate: {sess_wr}% ({best_session[1]} trades)\n")

        # Recommendations
        lines.append("💡 *Recommendations:*")
        if win_rate >= 60:
            lines.append("✅ Strong win rate — maintain current approach")
        elif win_rate >= 50:
            lines.append("⚠️ Win rate okay — focus on A+ grades only")
        else:
            lines.append("🚨 Win rate below 50% — only take 10/10 signals")

        if best_pair:
            lines.append(f"✅ Focus on {best_pair[0]} — your strongest pair")

        if best_session:
            lines.append(f"✅ Trade {best_session[0]} — your best session")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"get_performance_insights error: {e}")
        return "Unable to generate insights. Keep logging trades and try again."


# ─── PARTIAL CLOSE GUIDANCE ───────────────────────────────────────────────────

def get_partial_close_guidance(lots: float, score: int, direction: str,
                                entry: float, tp1: float, tp2: float,
                                tp3: float) -> str:
    """Generate partial close instructions based on lot size and score."""
    
    half = round(lots / 2, 2)
    third = round(lots / 3, 2)
    remainder_half = round(lots - half, 2)
    remainder_third = round(lots - third * 2, 2)

    if score >= 9:
        # High confidence — run to TP2/TP3
        return (
            f"📊 *PARTIAL CLOSE PLAN — Score {score}/10*\n\n"
            f"At *TP1 ({tp1})*: Close {half} lots → lock profit\n"
            f"Move SL to breakeven: {entry}\n"
            f"At *TP2 ({tp2})*: Close {remainder_half} lots → full close\n\n"
            f"⭐ Or let all {lots} lots run to TP1 for guaranteed profit"
        )
    else:
        # Lower confidence — take TP1 fully
        return (
            f"📊 *CLOSE PLAN — Score {score}/10*\n\n"
            f"Close *all {lots} lots* at TP1 ({tp1})\n"
            f"Score below 9 — take guaranteed profit at TP1\n\n"
            f"Don't split — lock the full win"
        )


# ─── DAILY PERFORMANCE RECORD ─────────────────────────────────────────────────

def record_daily_performance(user_id: int, firm_code: str,
                              trades: int, wins: int, losses: int,
                              gross_pnl: float, net_pnl: float) -> bool:
    """Record end of day performance for historical tracking."""
    try:
        win_rate = round((wins / trades) * 100, 1) if trades > 0 else 0
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO daily_performance
                    (user_id, firm_code, trade_date, trades_taken,
                     wins, losses, gross_pnl, net_pnl, win_rate)
                    VALUES (%s, %s, CURRENT_DATE, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, firm_code, trade_date) DO UPDATE SET
                        trades_taken = EXCLUDED.trades_taken,
                        wins = EXCLUDED.wins,
                        losses = EXCLUDED.losses,
                        gross_pnl = EXCLUDED.gross_pnl,
                        net_pnl = EXCLUDED.net_pnl,
                        win_rate = EXCLUDED.win_rate
                """, (user_id, firm_code, trades, wins, losses,
                      gross_pnl, net_pnl, win_rate))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"record_daily_performance error: {e}")
    return False
