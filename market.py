"""
TNL Trader Market Data Module
Fetches live price and ATR from Twelve Data free tier API.
Free tier: 800 requests/day, 8 requests/minute.
"""

import logging
import requests
from config import TWELVE_DATA_API_KEY

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com"

# Map common signal pair names to Twelve Data symbols
SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
    "GBPUSD": "GBP/USD",
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "USDCAD": "USD/CAD",
    "AUDUSD": "AUD/USD",
    "NZDUSD": "NZD/USD",
    "USDCHF": "USD/CHF",
    "EURGBP": "EUR/GBP",
    "EURJPY": "EUR/JPY",
    "GBPJPY": "GBP/JPY",
    "US30":   "DJI",
    "NAS100": "NDX",
    "SPX500": "SPX",
    "BTCUSD": "BTC/USD",
    "ETHUSD": "ETH/USD",
    "ES":     "ES1!",
    "MES":    "MES1!",
    "NQ":     "NQ1!",
    "MNQ":    "MNQ1!",
    "RTY":    "RTY1!",
    "YM":     "YM1!",
    "CL":     "CL1!",
    "MCL":    "MCL1!",
    "GC":     "GC1!",
    "MGC":    "MGC1!",
    "NG":     "NG1!",
}


def normalize_symbol(pair: str) -> str:
    """Convert signal pair name to Twelve Data symbol"""
    if not pair:
        return None
    upper = pair.upper().strip()
    return SYMBOL_MAP.get(upper, upper)


def get_live_price(pair: str) -> dict:
    """
    Get current price for a pair.
    Returns: { price, symbol, timestamp } or None
    """
    if not TWELVE_DATA_API_KEY or not pair:
        return None

    symbol = normalize_symbol(pair)
    if not symbol:
        return None

    try:
        resp = requests.get(
            f"{BASE_URL}/price",
            params={"symbol": symbol, "apikey": TWELVE_DATA_API_KEY},
            timeout=5
        )
        data = resp.json()
        if data.get("status") == "error" or "price" not in data:
            logger.warning(f"Price fetch failed for {symbol}: {data.get('message', 'unknown error')}")
            return None

        return {
            "price": float(data["price"]),
            "symbol": symbol,
        }
    except Exception as e:
        logger.error(f"Live price error for {pair}: {e}")
        return None


def get_atr(pair: str, interval: str = "1h", period: int = 14) -> dict:
    """
    Get Average True Range to measure session volatility.
    Returns: { atr, is_low_volatility } or None
    Low volatility = ATR below historical average for that pair
    """
    if not TWELVE_DATA_API_KEY or not pair:
        return None

    symbol = normalize_symbol(pair)
    if not symbol:
        return None

    # Minimum ATR thresholds per pair (in price units)
    # Below these = low volatility session, not worth trading
    ATR_MINIMUMS = {
        "XAU/USD": 3.0,
        "GBP/USD": 0.0008,
        "EUR/USD": 0.0007,
        "USD/JPY": 0.08,
        "GBP/JPY": 0.12,
        "EUR/JPY": 0.10,
        "USD/CAD": 0.0008,
        "AUD/USD": 0.0006,
        "DJI":     80.0,
        "NDX":     120.0,
        "BTC/USD": 400.0,
    }

    try:
        resp = requests.get(
            f"{BASE_URL}/atr",
            params={
                "symbol": symbol,
                "interval": interval,
                "time_period": period,
                "apikey": TWELVE_DATA_API_KEY,
                "outputsize": 1
            },
            timeout=5
        )
        data = resp.json()
        if data.get("status") == "error" or "values" not in data:
            logger.warning(f"ATR fetch failed for {symbol}: {data.get('message', 'unknown')}")
            return None

        atr_value = float(data["values"][0]["atr"])
        minimum = ATR_MINIMUMS.get(symbol, 0)
        is_low = atr_value < minimum

        return {
            "atr": atr_value,
            "is_low_volatility": is_low,
            "minimum": minimum,
            "symbol": symbol,
        }
    except Exception as e:
        logger.error(f"ATR error for {pair}: {e}")
        return None


def check_entry_validity(pair: str, entry_zone: str, stop_loss: str, direction: str) -> dict:
    """
    Compare current price to entry zone and stop loss.
    Returns assessment of whether entry is still valid.
    """
    price_data = get_live_price(pair)
    if not price_data or not entry_zone or entry_zone == "MARKET":
        return {"valid": True, "reason": "no price check", "current_price": None}

    try:
        current = price_data["price"]
        entry = float(entry_zone)
        direction = (direction or "").upper()

        # Calculate distance from current price to entry
        distance = abs(current - entry)

        # Rough pip/point calculation
        MAX_PIPS_AWAY = {
            "XAUUSD": 15, "XAU/USD": 15,
            "US30": 50, "DJI": 50,
            "NAS100": 80, "NDX": 80,
            "default": 20
        }
        from futures_instruments import is_futures, get_spec
        if is_futures(pair.upper()):
            spec = get_spec(pair.upper())
            pips_away = distance  # futures use points directly
            max_away = spec["typical_sl_pts"] * 3 if spec else 50
        elif "JPY" in pair.upper():
            pips_away = distance / 0.01
            max_away = MAX_PIPS_AWAY.get(pair.upper(), MAX_PIPS_AWAY["default"])
        elif pair.upper() in ["XAUUSD", "XAU/USD"]:
            pips_away = distance
            max_away = MAX_PIPS_AWAY.get(pair.upper(), 15)
        elif pair.upper() in ["US30", "DJI", "NAS100", "NDX"]:
            pips_away = distance
            max_away = MAX_PIPS_AWAY.get(pair.upper(), 80)
        else:
            pips_away = distance / 0.0001
            max_away = MAX_PIPS_AWAY.get(pair.upper(), MAX_PIPS_AWAY["default"])

        # Check if price already blew past stop loss
        if stop_loss and stop_loss not in ["null", "UNSPECIFIED"]:
            sl = float(stop_loss)
            if direction == "BUY" and current < sl:
                return {
                    "valid": False,
                    "reason": "Price already below stop loss",
                    "current_price": current,
                    "entry": entry,
                    "pips_away": round(pips_away, 1)
                }
            if direction == "SELL" and current > sl:
                return {
                    "valid": False,
                    "reason": "Price already above stop loss",
                    "current_price": current,
                    "entry": entry,
                    "pips_away": round(pips_away, 1)
                }

        # Check if entry is too far away (signal may be stale)

        if pips_away > max_away:
            return {
                "valid": False,
                "reason": f"Entry {round(pips_away, 1)} points away from current price {current}",
                "current_price": current,
                "entry": entry,
                "pips_away": round(pips_away, 1)
            }

        return {
            "valid": True,
            "reason": "Entry zone valid",
            "current_price": current,
            "entry": entry,
            "pips_away": round(pips_away, 1)
        }

    except Exception as e:
        logger.error(f"Entry validity check error: {e}")
        return {"valid": True, "reason": "check error", "current_price": None}
