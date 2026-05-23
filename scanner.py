"""
scanner.py — Autonomous Market Scanner
TNL Trader — Phase 3

Scans watchlist instruments every 15 minutes during trading hours.
Detects SMC setups: order blocks, FVGs, structure breaks.
Sends proactive alerts to subscribed users via Telegram.
"""

import logging
import asyncio
from datetime import datetime, timezone
from market import get_live_price, get_atr, normalize_symbol
from config import TWELVE_DATA_API_KEY
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com"

# Default watchlist — users can customize with /watch command
DEFAULT_WATCHLIST = ["XAUUSD", "EURUSD", "GBPUSD", "NQ", "ES"]

# Scan interval in seconds
SCAN_INTERVAL = 900  # 15 minutes

# Trading hours UTC — scanner only runs during active sessions
SCANNER_START_HOUR = 7   # 7 AM UTC = London open
SCANNER_END_HOUR = 21    # 9 PM UTC = NY close


def is_scan_window() -> bool:
    hour = datetime.now(timezone.utc).hour
    day = datetime.now(timezone.utc).weekday()
    if day >= 5:  # Saturday/Sunday
        return False
    return SCANNER_START_HOUR <= hour <= SCANNER_END_HOUR


def get_candles(symbol: str, interval: str = "15min", outputsize: int = 50) -> list | None:
    """Fetch recent OHLCV candles from Twelve Data."""
    if not TWELVE_DATA_API_KEY:
        return None
    td_symbol = normalize_symbol(symbol)
    if not td_symbol:
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/time_series",
            params={
                "symbol": td_symbol,
                "interval": interval,
                "outputsize": outputsize,
                "apikey": TWELVE_DATA_API_KEY,
            },
            timeout=8
        )
        data = resp.json()
        if data.get("status") == "error" or "values" not in data:
            logger.warning(f"Candle fetch failed for {symbol}: {data.get('message', 'unknown')}")
            return None
        candles = data["values"]
        # Convert to floats
        result = []
        for c in candles:
            result.append({
                "datetime": c["datetime"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume", 0)),
            })
        return result  # newest first
    except Exception as e:
        logger.error(f"Candle fetch error for {symbol}: {e}")
        return None


def detect_structure(candles: list) -> dict:
    """
    Detect market structure: Higher Highs, Lower Lows, BOS, CHOCH.
    Uses last 20 candles.
    """
    if not candles or len(candles) < 10:
        return {"trend": "unclear", "bos": False, "choch": False}

    recent = candles[:20]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]

    # Simple HH/HL or LH/LL detection
    hh = highs[0] > highs[2] > highs[4]  # recent highs rising
    hl = lows[0] > lows[2] > lows[4]     # recent lows rising
    lh = highs[0] < highs[2] < highs[4]  # recent highs falling
    ll = lows[0] < lows[2] < lows[4]     # recent lows falling

    if hh and hl:
        trend = "bullish"
    elif lh and ll:
        trend = "bearish"
    else:
        trend = "ranging"

    # Break of structure — current close beyond recent swing
    current_close = candles[0]["close"]
    prev_high = max(highs[1:6])
    prev_low = min(lows[1:6])

    bos_bull = current_close > prev_high and trend == "bullish"
    bos_bear = current_close < prev_low and trend == "bearish"
    bos = bos_bull or bos_bear

    # Change of character — close beyond structure in opposite direction
    choch = (current_close > prev_high and trend == "bearish") or \
            (current_close < prev_low and trend == "bullish")

    return {
        "trend": trend,
        "bos": bos,
        "choch": choch,
        "prev_high": round(prev_high, 5),
        "prev_low": round(prev_low, 5),
        "current": round(current_close, 5),
    }


def detect_order_block(candles: list, trend: str) -> dict | None:
    """
    Detect the most recent order block.
    Bullish OB: last bearish candle before a strong bullish move.
    Bearish OB: last bullish candle before a strong bearish move.
    """
    if not candles or len(candles) < 5:
        return None

    try:
        if trend == "bullish":
            # Find last bearish candle before current rally
            for i in range(1, min(15, len(candles))):
                c = candles[i]
                if c["close"] < c["open"]:  # bearish candle
                    # Check if followed by bullish move
                    if candles[i-1]["close"] > c["high"]:
                        return {
                            "type": "bullish_ob",
                            "high": round(c["high"], 5),
                            "low": round(c["low"], 5),
                            "mid": round((c["high"] + c["low"]) / 2, 5),
                            "datetime": c["datetime"],
                            "strength": "strong" if (c["high"] - c["low"]) > (candles[0]["high"] - candles[0]["low"]) else "normal",
                        }

        elif trend == "bearish":
            # Find last bullish candle before current drop
            for i in range(1, min(15, len(candles))):
                c = candles[i]
                if c["close"] > c["open"]:  # bullish candle
                    # Check if followed by bearish move
                    if candles[i-1]["close"] < c["low"]:
                        return {
                            "type": "bearish_ob",
                            "high": round(c["high"], 5),
                            "low": round(c["low"], 5),
                            "mid": round((c["high"] + c["low"]) / 2, 5),
                            "datetime": c["datetime"],
                            "strength": "strong" if (c["high"] - c["low"]) > (candles[0]["high"] - candles[0]["low"]) else "normal",
                        }
    except Exception as e:
        logger.error(f"OB detection error: {e}")

    return None


def detect_fvg(candles: list) -> dict | None:
    """
    Detect Fair Value Gap (imbalance between candle 1 high and candle 3 low).
    Bullish FVG: candle[2].high < candle[0].low
    Bearish FVG: candle[2].low > candle[0].high
    """
    if not candles or len(candles) < 3:
        return None

    try:
        for i in range(len(candles) - 2):
            c1 = candles[i+2]   # oldest of three
            c2 = candles[i+1]   # middle
            c3 = candles[i]     # newest

            # Bullish FVG
            if c1["high"] < c3["low"]:
                gap_size = c3["low"] - c1["high"]
                return {
                    "type": "bullish_fvg",
                    "top": round(c3["low"], 5),
                    "bottom": round(c1["high"], 5),
                    "mid": round((c3["low"] + c1["high"]) / 2, 5),
                    "size": round(gap_size, 5),
                    "datetime": c2["datetime"],
                }

            # Bearish FVG
            if c1["low"] > c3["high"]:
                gap_size = c1["low"] - c3["high"]
                return {
                    "type": "bearish_fvg",
                    "top": round(c1["low"], 5),
                    "bottom": round(c3["high"], 5),
                    "mid": round((c1["low"] + c3["high"]) / 2, 5),
                    "size": round(gap_size, 5),
                    "datetime": c2["datetime"],
                }
    except Exception as e:
        logger.error(f"FVG detection error: {e}")

    return None


def score_setup(structure: dict, ob: dict, fvg: dict, atr_data: dict) -> dict:
    """
    Score the overall setup quality. Returns score 0-10 and recommendation.
    """
    score = 0
    factors = []

    trend = structure.get("trend", "unclear")

    if trend in ("bullish", "bearish"):
        score += 2
        factors.append(f"Clear {trend} trend")

    if structure.get("bos"):
        score += 2
        factors.append("Break of structure confirmed")

    if structure.get("choch"):
        score += 1
        factors.append("Change of character detected")

    if ob:
        score += 2
        ob_type = "Bullish" if ob["type"] == "bullish_ob" else "Bearish"
        factors.append(f"{ob_type} order block at {ob['mid']}")

    if fvg:
        score += 2
        fvg_type = "Bullish" if fvg["type"] == "bullish_fvg" else "Bearish"
        factors.append(f"{fvg_type} FVG {fvg['bottom']} - {fvg['top']}")

    if atr_data and not atr_data.get("is_low_volatility"):
        score += 1
        factors.append("Healthy volatility")

    # Direction alignment
    ob_aligned = ob and (
        (ob["type"] == "bullish_ob" and trend == "bullish") or
        (ob["type"] == "bearish_ob" and trend == "bearish")
    )
    fvg_aligned = fvg and (
        (fvg["type"] == "bullish_fvg" and trend == "bullish") or
        (fvg["type"] == "bearish_fvg" and trend == "bearish")
    )

    if ob_aligned and fvg_aligned:
        score += 1
        factors.append("OB and FVG both aligned with trend")

    recommendation = "STRONG" if score >= 8 else "MODERATE" if score >= 5 else "WEAK"

    return {
        "score": min(score, 10),
        "recommendation": recommendation,
        "factors": factors,
        "direction": "BUY" if trend == "bullish" else "SELL" if trend == "bearish" else None,
    }


def format_scan_alert(symbol: str, structure: dict, ob: dict, fvg: dict, score_data: dict, price_data: dict) -> str:
    """Format a scan alert for Telegram."""
    trend = structure.get("trend", "unclear")
    direction = score_data.get("direction", "")
    score = score_data.get("score", 0)
    rec = score_data.get("recommendation", "WEAK")
    factors = score_data.get("factors", [])
    current_price = price_data.get("price", "--") if price_data else "--"

    rec_emoji = "🔥" if rec == "STRONG" else "⚡" if rec == "MODERATE" else "👀"
    dir_emoji = "📈" if direction == "BUY" else "📉" if direction == "SELL" else "↔️"

    lines = [
        f"{rec_emoji} *{symbol} SETUP DETECTED*",
        f"{'─' * 28}",
        f"{dir_emoji} Direction: {direction or 'Unclear'}",
        f"💰 Price: {current_price}",
        f"📊 Setup Score: {score}/10 — {rec}",
        f"📈 Trend: {trend.capitalize()}",
        "",
        "✅ *Confluence Factors:*",
    ]

    for f in factors:
        lines.append(f"  • {f}")

    if ob:
        ob_label = "🟢 Bullish OB" if ob["type"] == "bullish_ob" else "🔴 Bearish OB"
        lines.append(f"\n{ob_label}: {ob['low']} — {ob['high']} (mid: {ob['mid']})")

    if fvg:
        fvg_label = "🟢 Bullish FVG" if fvg["type"] == "bullish_fvg" else "🔴 Bearish FVG"
        lines.append(f"{fvg_label}: {fvg['bottom']} — {fvg['top']}")

    lines += [
        "",
        f"⏱ Scanned: {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
        f"📡 Send this signal to the bot to get a full grade and execute report.",
    ]

    return "\n".join(lines)


async def scan_symbol(symbol: str) -> dict | None:
    """
    Run full scan on one symbol. Returns alert dict if setup found, None otherwise.
    """
    try:
        candles = get_candles(symbol, interval="15min", outputsize=50)
        if not candles or len(candles) < 10:
            return None

        price_data = get_live_price(symbol)
        atr_data = get_atr(symbol)

        if atr_data and atr_data.get("is_low_volatility"):
            logger.info(f"[scanner] {symbol} low volatility — skipping")
            return None

        structure = detect_structure(candles)
        trend = structure.get("trend", "unclear")

        if trend == "ranging":
            return None  # No clear setup in ranging market

        ob = detect_order_block(candles, trend)
        fvg = detect_fvg(candles)

        score_data = score_setup(structure, ob, fvg, atr_data)

        # Only alert on moderate or strong setups
        if score_data["score"] < 5:
            logger.info(f"[scanner] {symbol} score {score_data['score']}/10 — below threshold")
            return None

        alert_text = format_scan_alert(symbol, structure, ob, fvg, score_data, price_data)

        return {
            "symbol": symbol,
            "score": score_data["score"],
            "recommendation": score_data["recommendation"],
            "direction": score_data["direction"],
            "alert_text": alert_text,
        }

    except Exception as e:
        logger.error(f"[scanner] Error scanning {symbol}: {e}")
        return None


async def run_scan(watchlist: list, bot, user_chat_ids: list, force: bool = False):
    """
    Scan all symbols in watchlist and send alerts to users.
    force=True bypasses the scan window check (for manual /scan command).
    """
    if not force and not is_scan_window():
        logger.info("[scanner] Outside scan window — skipping")
        return

    logger.info(f"[scanner] Starting scan of {len(watchlist)} symbols")
    alerts_sent = 0

    for symbol in watchlist:
        try:
            result = await scan_symbol(symbol)
            if result:
                for chat_id in user_chat_ids:
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=result["alert_text"],
                            parse_mode="Markdown"
                        )
                        alerts_sent += 1
                    except Exception as e:
                        logger.error(f"[scanner] Failed to send alert to {chat_id}: {e}")

            # Rate limit — Twelve Data free tier: 8 req/min
            await asyncio.sleep(8)

        except Exception as e:
            logger.error(f"[scanner] Symbol scan failed for {symbol}: {e}")

    logger.info(f"[scanner] Scan complete — {alerts_sent} alerts sent")


async def start_scanner(bot, get_active_users_fn):
    """
    Main scanner loop. Runs every SCAN_INTERVAL seconds.
    Call this from main.py alongside start_bot().
    """
    logger.info("[scanner] Scanner started")
    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL)

            if not is_scan_window():
                continue

            # Get active users and their watchlists
            users = get_active_users_fn()
            if not users:
                continue

            # Get all unique symbols across all user watchlists
            all_symbols = set()
            user_chat_ids = []

            for user in users:
                watchlist = getattr(user, "watchlist", None)
                if watchlist:
                    symbols = [s.strip().upper() for s in watchlist.split(",")]
                else:
                    symbols = DEFAULT_WATCHLIST
                all_symbols.update(symbols)
                user_chat_ids.append(user.telegram_chat_id)

            await run_scan(list(all_symbols), bot, user_chat_ids)

        except Exception as e:
            logger.error(f"[scanner] Loop error: {e}")
            await asyncio.sleep(60)
