"""
trade_monitor.py — Active Trade Monitor
TNL Trader

Monitors open trades after the user taps YES on a grade report.
Sends smart alerts while the trade is running: TP1 approach, SL warning,
breakeven suggestion, momentum shift, and news warnings.
"""

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
            # Store recent close prices for momentum check
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

        # Update stored recent closes
        if recent_closes:
            trade["_recent_closes"] = list(recent_closes)

        alerts = []
        threshold = max(sl_dist * 0.15, 3.0)  # 15% of SL dist, minimum 3 pts/pips

        # --- TP1 approaching ---
        if tp1 and not trade["tp1_alerted"]:
            dist_to_tp1 = abs(current - tp1)
            if dist_to_tp1 <= threshold:
                alerts.append(
                    f"🎯 *TP1 APPROACHING* — {symbol} only {dist_to_tp1:.2f} away. "
                    f"Consider partial close or let MT5 auto-close."
                )
                trade["tp1_alerted"] = True

        # --- SL warning ---
        if not trade["sl_alerted"]:
            dist_to_sl = abs(current - sl)
            sl_warn_threshold = max(sl_dist * 0.25, 5.0)
            if dist_to_sl <= sl_warn_threshold:
                alerts.append(
                    f"⚠️ *SL WARNING* — {symbol} price at {current:.5f}, "
                    f"only {dist_to_sl:.2f} from stop loss at {sl:.5f}. "
                    f"Prepare to accept the loss."
                )
                trade["sl_alerted"] = True

        # --- Breakeven suggestion ---
        if not trade["breakeven_alerted"]:
            if direction == "BUY":
                profit = current - entry
            else:
                profit = entry - current
            breakeven_threshold = max(sl_dist * 0.5, 15.0)
            if profit >= breakeven_threshold:
                alerts.append(
                    f"💡 *MOVE SL TO BREAKEVEN* — {symbol} is +{profit:.2f} in profit. "
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

        # --- News warning ---
        if not trade["news_alerted"]:
            elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
            if elapsed > 300:  # trade open more than 5 minutes
                try:
                    from scanner_improvements import is_news_window
                    news_blocked, news_reason = is_news_window()
                    if news_blocked and news_reason:
                        alerts.append(
                            f"⏰ *NEWS WARNING* — {symbol} open trade. {news_reason}. "
                            f"Consider closing or moving SL to breakeven before news hits."
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
