"""
signal_bridge.py — MT5 EA signal file writer
Writes confirmed scanner signals to /tmp/tnl_signal.json for EA pickup.
File auto-clears after 5 minutes to prevent stale execution.
"""

import json
import logging
import asyncio
from datetime import datetime

logger = logging.getLogger(__name__)

SIGNAL_FILE = "/tmp/tnl_signal.json"
SIGNAL_TTL  = 300  # seconds before auto-clear

# Pip size in raw price units (same mapping as claude.py)
_PIP_SIZE = {
    "USDJPY": 0.01, "EURJPY": 0.01, "GBPJPY": 0.01, "CADJPY": 0.01,
    "XAUUSD": 1.0,
    "US30": 1.0, "US100": 1.0, "NAS100": 1.0, "US500": 1.0,
}
_DEFAULT_PIP_SIZE = 0.0001

# Dollar pip value per standard lot
_PIP_VALUE = {
    "EURUSD": 10.0, "GBPUSD": 10.0, "USDJPY": 9.30,
    "AUDUSD": 10.0, "USDCAD": 7.50, "NZDUSD": 10.0,
    "USDCHF": 10.50, "XAUUSD": 100.0,
    "US30": 1.0, "US100": 1.0, "NAS100": 1.0, "US500": 1.0,
    "default": 10.0,
}


def _calc_lots(symbol: str, entry: float, sl: float,
               risk_pct: float = 0.75, account_size: float = 10000.0) -> float:
    try:
        sl_dist = abs(entry - sl)
        pip_size = _PIP_SIZE.get(symbol.upper(), _DEFAULT_PIP_SIZE)
        sl_pips = sl_dist / pip_size
        if sl_pips <= 0:
            return 0.10
        risk_dollar = account_size * (risk_pct / 100)
        pip_val = _PIP_VALUE.get(symbol.upper(), _PIP_VALUE["default"])
        # JPY: pip_val varies with rate — approximate with standard value (close enough for lot sizing)
        lot = round(risk_dollar / (sl_pips * pip_val), 2)
        if symbol.upper() == "USDJPY":
            if lot > 0.50:
                logger.warning(f"[signal_bridge] USDJPY lot cap applied: {lot:.2f} → 0.50 (prevents MT5 margin rejection)")
            lot = min(lot, 0.50)
        return max(lot, 0.01)
    except Exception:
        return 0.10


async def _clear_signal():
    """
    Auto-expire signal after SIGNAL_TTL (300s = 5 minutes).
    After 5 minutes the EA polls and sees fired=False, preventing stale execution
    of signals that were never filled (e.g. price moved away from limit entry).
    The asyncio.create_task() in write_signal() schedules this concurrently so
    the bot loop is never blocked.
    """
    await asyncio.sleep(SIGNAL_TTL)
    try:
        with open(SIGNAL_FILE, "w") as f:
            json.dump({"fired": False}, f)
        logger.info(f"[signal_bridge] Signal cleared after {SIGNAL_TTL}s")
    except Exception as e:
        logger.error(f"[signal_bridge] Failed to clear signal file: {e}")


def write_signal(result: dict, user_id: int = None, account_size: float = 10000.0) -> bool:
    """
    Write a confirmed scanner signal to /tmp/tnl_signal.json for EA execution.

    Checks the user's auto_execute preference first — if False, logs and returns
    without writing so the EA stays idle and the user places the trade manually.

    result dict expected keys: symbol, direction, entry, sl, tp1, tp2, tp3, risk_pct
    Returns True on success.
    """
    if user_id is not None:
        try:
            from database import get_auto_execute
            if not get_auto_execute(user_id):
                logger.info(f"[signal_bridge] user {user_id} auto_execute=False — Telegram only, no EA write")
                return False
            logger.info(f"[signal_bridge] user {user_id} auto_execute=True — writing signal")
        except Exception as e:
            logger.error(f"[signal_bridge] auto_execute check failed for user {user_id}: {e}")
            return False

    try:
        symbol    = result.get("symbol", "")
        direction = result.get("direction", "")
        entry     = float(result.get("entry", 0) or 0)
        sl        = float(result.get("sl",    0) or 0)
        tp1       = float(result.get("tp1",   0) or 0)
        tp2       = float(result.get("tp2",   0) or 0)
        tp3       = float(result.get("tp3",   0) or 0)
        risk_pct  = float(result.get("risk_pct", 0.75) or 0.75)

        if not symbol or not direction or not entry or not sl:
            logger.warning("[signal_bridge] Incomplete signal — skipping write")
            return False

        lots = _calc_lots(symbol, entry, sl, risk_pct, account_size)

        payload = {
            "symbol":    symbol,
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "tp1":       tp1,
            "tp2":       tp2,
            "tp3":       tp3,
            "lots":      lots,
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            "fired":     True,
        }

        with open(SIGNAL_FILE, "w") as f:
            json.dump(payload, f, indent=2)

        logger.info(
            f"[signal_bridge] {symbol} {direction} → {SIGNAL_FILE} "
            f"entry={entry} sl={sl} tp1={tp1} lots={lots}"
        )

        # Log trade as opened for trade count tracking only.
        # Do NOT record P&L here — challenge state P&L is updated only when the trade
        # closes via /logtrade. Recording a phantom win at signal time corrupts the
        # drawdown tracker's daily loss calculation used by check_signal_allowed().
        if user_id is not None:
            try:
                from database import log_trade_opened
                log_trade_opened(user_id, risk_pct)
                logger.info(f"[signal_bridge] trade opened logged for user {user_id} — P&L recorded on close via /logtrade")
            except Exception as e:
                logger.error(f"[signal_bridge] log_trade_opened error: {e}")

        # Schedule auto-clear (runs in the existing asyncio event loop)
        asyncio.create_task(_clear_signal())

        return True

    except Exception as e:
        logger.error(f"[signal_bridge] write_signal error: {e}")
        return False
