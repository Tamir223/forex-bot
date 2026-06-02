"""
trade_monitor.py — Active Trade Monitor
TNL Trader

Monitors open trades after the user taps YES on a grade report.
Sends smart alerts while the trade is running: TP1 approach, SL warning,
breakeven suggestion, momentum shift, and news warnings.
"""

import collections
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# yFinance ticker map for futures (mirrors scanner.py)
YFINANCE_FUTURES_MAP = {
    'ES': 'ES=F', 'MES': 'MES=F', 'NQ': 'NQ=F', 'MNQ': 'MNQ=F',
    'RTY': 'RTY=F', 'YM': 'YM=F', 'CL': 'CL=F', 'MCL': 'MCL=F',
    'GC': 'GC=F', 'MGC': 'MGC=F', 'NG': 'NG=F',
}


def _parse_float(val) -> float | None:
    """Safely parse a float from various input types."""
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


class TradeMonitor:
    def __init__(self):
        # Keyed by user_id (int or str)
        self._trades: dict = {}

    @property
    def trades(self) -> dict:
        return self._trades

    def add_trade(self, user_id, chat_id, symbol, direction, entry, sl, tp1, tp2, firm_code=None):
        """Add a trade to active monitoring."""
        entry_f = _parse_float(entry)
        sl_f = _parse_float(sl)
        tp1_f = _parse_float(tp1)
        tp2_f = _parse_float(tp2)

        if not entry_f or not sl_f:
            logger.info(f"[monitor] Cannot monitor {symbol} — missing entry/SL")
            return

        sl_dist = abs(entry_f - sl_f) if entry_f and sl_f else 0

        self._trades[str(user_id)] = {
            "chat_id": str(chat_id),
            "symbol": str(symbol).upper(),
            "direction": str(direction).upper(),
            "entry": entry_f,
            "sl": sl_f,
            "tp1": tp1_f,
            "tp2": tp2_f,
            "sl_dist": sl_dist,
            "firm_code": firm_code,
            "started_at": datetime.now(timezone.utc),
            "breakeven_alerted": False,
            "tp1_alerted": False,
            "sl_alerted": False,
            "news_alerted": False,
            "momentum_alerted": False,
            "price_history": collections.deque(maxlen=3),
            # Store recent close prices for momentum shift check
            "_recent_closes": [],
        }
        logger.info(f"[monitor] Monitoring {symbol} {direction} for user {user_id} — entry={entry_f} SL={sl_f} TP1={tp1_f}")

    def remove_trade(self, user_id):
        """Stop monitoring a trade (called on WIN/LOSS)."""
        uid = str(user_id)
        if uid in self._trades:
            symbol = self._trades[uid].get("symbol", "?")
            del self._trades[uid]
            logger.info(f"[monitor] Stopped monitoring {symbol} for user {uid}")

    def get_current_price(self, symbol: str) -> float | None:
        """Fetch live price — yFinance for futures, Twelve Data for forex."""
        sym = symbol.upper()
        if sym in YFINANCE_FUTURES_MAP:
            try:
                import yfinance as yf
                ticker = YFINANCE_FUTURES_MAP[sym]
                data = yf.Ticker(ticker).history(period="1d", interval="1m")
                if data.empty:
                    return None
                price = float(data["Close"].iloc[-1])
                # Also capture recent closes for momentum (last 3 candles)
                return price, list(data["Close"].tail(4).values)
            except Exception as e:
                logger.error(f"[monitor] yFinance price error for {symbol}: {e}")
                return None, []
        else:
            try:
                from market import get_live_price
                result = get_live_price(symbol)
                if result:
                    return result["price"], []
                return None, []
            except Exception as e:
                logger.error(f"[monitor] Twelve Data price error for {symbol}: {e}")
                return None, []

    @staticmethod
    def _pip_info(symbol: str):
        """Return (pip_multiplier, dist_unit, tp1_threshold, sl_threshold) for a symbol.

        Forex non-JPY : pip_mult=10000, thresholds in raw price (0.0003=3 pips, 0.0005=5 pips)
        Forex JPY     : pip_mult=100,   thresholds scaled (0.03=3 pips, 0.05=5 pips)
        XAUUSD/futures: pip_mult=1,     thresholds None (caller uses sl_dist fallback)
        """
        sym = symbol.upper()
        forex_currencies = {"USD", "EUR", "GBP", "AUD", "NZD", "CAD", "CHF"}
        is_xau_or_futures = "XAU" in sym or sym in {
            "ES", "MES", "NQ", "MNQ", "RTY", "YM", "CL", "MCL", "GC", "MGC", "NG",
        }
        if not is_xau_or_futures:
            is_forex = any(sym.startswith(c) or sym.endswith(c) for c in forex_currencies)
            if is_forex:
                if "JPY" in sym:
                    return 100, "pips", 0.03, 0.05
                return 10000, "pips", 0.0003, 0.0005
        return 1, "pts", None, None

    def check_trade(self, user_id) -> list:
        """Check a monitored trade against current price. Returns list of alert strings."""
        uid = str(user_id)
        trade = self._trades.get(uid)
        if not trade:
            return []

        symbol = trade["symbol"]
        direction = trade["direction"]
        entry = trade["entry"]
        sl = trade["sl"]
        tp1 = trade["tp1"]
        sl_dist = trade["sl_dist"]
        started_at = trade["started_at"]

        price_result = self.get_current_price(symbol)
        if price_result is None or (isinstance(price_result, tuple) and price_result[0] is None):
            return []

        if isinstance(price_result, tuple):
            current, recent_closes = price_result
        else:
            current, recent_closes = price_result, []

        if current is None:
            return []

        # Update stored recent closes and price history
        if recent_closes:
            trade["_recent_closes"] = list(recent_closes)
        trade["price_history"].append(current)

        pip_mult, dist_unit, tp1_thresh, sl_thresh = self._pip_info(symbol)
        # Fall back to sl_dist-based thresholds for non-forex (XAUUSD, futures)
        if tp1_thresh is None:
            tp1_thresh = max(sl_dist * 0.15, 3.0)
            sl_thresh = max(sl_dist * 0.25, 5.0)

        alerts = []

        # --- TP1 approaching ---
        if tp1 and not trade["tp1_alerted"]:
            dist_to_tp1 = abs(current - tp1)
            if dist_to_tp1 <= tp1_thresh:
                display_dist = dist_to_tp1 * pip_mult
                alerts.append(
                    f"🎯 *TP1 APPROACHING* — {symbol} only {display_dist:.1f} {dist_unit} away. "
                    f"Consider partial close or let MT5 auto-close."
                )
                trade["tp1_alerted"] = True

        # --- SL warning ---
        if not trade["sl_alerted"]:
            dist_to_sl = abs(current - sl)
            if dist_to_sl <= sl_thresh:
                display_dist = dist_to_sl * pip_mult
                alerts.append(
                    f"⚠️ *SL WARNING* — {symbol} price at {current:.5f}, "
                    f"only {display_dist:.1f} {dist_unit} from stop loss at {sl:.5f}. "
                    f"Prepare to accept the loss."
                )
                trade["sl_alerted"] = True

        # --- Breakeven suggestion ---
        if not trade["breakeven_alerted"]:
            if direction == "BUY":
                profit = current - entry
            else:
                profit = entry - current
            breakeven_threshold = max(sl_dist * 0.5, 15.0 / pip_mult)
            if profit >= breakeven_threshold:
                display_profit = profit * pip_mult
                alerts.append(
                    f"💡 *MOVE SL TO BREAKEVEN* — {symbol} is +{display_profit:.1f} {dist_unit} in profit. "
                    f"Move your SL to {entry:.5f} on MT5 now. "
                    f"This makes the trade free — zero risk."
                )
                trade["breakeven_alerted"] = True

        # --- Momentum shift (3 consecutive candles against trade) ---
        closes = trade.get("_recent_closes", [])
        if len(closes) >= 3:
            last3 = closes[-3:]
            if direction == "BUY":
                # 3 bearish candles = each close lower than the previous
                if last3[0] > last3[1] > last3[2]:
                    alerts.append(
                        f"⚠️ *MOMENTUM SHIFT* — {symbol} showing 3 consecutive candles "
                        f"against your {direction}. Still above SL — hold but watch closely."
                    )
            else:
                # SELL: 3 bullish candles
                if last3[0] < last3[1] < last3[2]:
                    alerts.append(
                        f"⚠️ *MOMENTUM SHIFT* — {symbol} showing 3 consecutive candles "
                        f"against your {direction}. Still above SL — hold but watch closely."
                    )

        # --- Rapid momentum alert (price moving strongly in trade direction) ---
        price_history = trade["price_history"]
        if len(price_history) >= 3 and not trade["momentum_alerted"]:
            is_gold_or_futures = "XAU" in symbol or symbol in YFINANCE_FUTURES_MAP
            threshold = 8.0 if is_gold_or_futures else 0.0008
            oldest = price_history[0]
            if direction == "BUY":
                move = current - oldest
            else:
                move = oldest - current
            if move >= threshold:
                pips_display = move * pip_mult
                alerts.append(
                    f"🚀 *MOMENTUM ALERT* — {symbol} moving strongly in your favor\n"
                    f"Price: {current} (+{pips_display:.1f} pips in 30 min)\n"
                    f"Entry: {entry} | TP1: {tp1}\n"
                    f"Consider moving SL to breakeven if not already done"
                )
                trade["momentum_alerted"] = True

        # --- News warning ---
        if not trade["news_alerted"]:
            elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
            if elapsed >= 120:  # trade open 2+ minutes
                try:
                    from scanner_improvements import check_upcoming_news
                    news_approaching, news_reason, minutes_until = check_upcoming_news(45)
                    if news_approaching and news_reason:
                        alerts.append(
                            f"⏰ *URGENT NEWS WARNING* — {symbol} open trade. {news_reason}. "
                            f"Close or move SL to breakeven NOW — {minutes_until} minutes until high impact news."
                        )
                        trade["news_alerted"] = True
                except Exception as e:
                    logger.error(f"[monitor] News check error: {e}")

        return alerts

    async def check_all_trades(self, bot):
        """Check all monitored trades and send any alerts via bot."""
        if not self._trades:
            return

        for user_id, trade in list(self._trades.items()):
            try:
                alerts = self.check_trade(user_id)
                chat_id = trade.get("chat_id")
                if alerts and chat_id:
                    for alert in alerts:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=alert,
                            parse_mode="Markdown"
                        )
            except Exception as e:
                logger.error(f"[monitor] check_all_trades error for user {user_id}: {e}")


# Global singleton — import this everywhere
trade_monitor = TradeMonitor()
