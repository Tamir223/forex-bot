"""
TNL Trader Market Data Module
Fetches live price and ATR from Twelve Data free tier API.
Free tier: 800 requests/day, 8 requests/minute.
"""

import logging
import requests
import yfinance as yf
from config import TWELVE_DATA_API_KEY

# yFinance forex tickers — used as fallback when Twelve Data is rate-limited
YFINANCE_FOREX_MAP = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
}

# Mirrors scanner.YFINANCE_FUTURES_MAP — kept here to avoid a circular import
# (scanner.py imports from market.py).  Keep in sync when the map changes.
YFINANCE_FUTURES_MAP = {
    'ES': 'ES=F', 'MES': 'MES=F', 'NQ': 'NQ=F', 'MNQ': 'MNQ=F',
    'RTY': 'RTY=F', 'YM': 'YM=F', 'CL': 'CL=F', 'MCL': 'MCL=F',
    'GC': 'GC=F', 'MGC': 'MGC=F', 'NG': 'NG=F',
}
# XAUUSD is handled via yFinance using the gold futures ticker
_XAUUSD_YF_TICKER = "GC=F"
# Mirrors scanner.FUTURES_SPOT_OFFSET — GC=F trades ~30 pts above MT5 XAUUSD spot
_FUTURES_SPOT_OFFSET = {
    "XAUUSD": -30,
}

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


def _get_live_price_yfinance(pair: str) -> dict | None:
    """yFinance fallback for live forex price using 1m candles."""
    ticker = YFINANCE_FOREX_MAP.get(pair.upper())
    if not ticker:
        return None
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="1m")
        if hist.empty:
            return None
        price = round(float(hist["Close"].iloc[-1]), 5)
        logger.info(f"[market] yFinance fallback price for {pair}: {price}")
        return {"price": price, "symbol": pair, "source": "yfinance"}
    except Exception as e:
        logger.error(f"yFinance live price error for {pair}: {e}")
        return None


def get_live_price(pair: str) -> dict:
    """
    Get current price for a pair.
    Tries Twelve Data first; falls back to yFinance on failure or rate limit.
    Returns: { price, symbol } or None
    """
    if not pair:
        return None

    upper = pair.upper()

    # XAUUSD and futures always use yFinance — no Twelve Data credits
    if upper == "XAUUSD" or upper in YFINANCE_FUTURES_MAP:
        yf_ticker = "GC=F" if upper == "XAUUSD" else YFINANCE_FUTURES_MAP[upper]
        try:
            hist = yf.Ticker(yf_ticker).history(period="1d", interval="1m")
            if not hist.empty:
                raw = round(float(hist["Close"].iloc[-1]), 2)
                price = round(raw + _FUTURES_SPOT_OFFSET.get(upper, 0), 2)
                return {"price": price, "symbol": pair}
        except Exception as e:
            logger.error(f"yFinance price error for {pair}: {e}")
        return None

    # Forex — use yFinance directly
    # (Twelve Data removed — rate-limited on free tier)
    # if TWELVE_DATA_API_KEY:
    #     symbol = normalize_symbol(pair)
    #     if symbol:
    #         try:
    #             resp = requests.get(f"{BASE_URL}/price",
    #                 params={"symbol": symbol, "apikey": TWELVE_DATA_API_KEY}, timeout=5)
    #             data = resp.json()
    #             if data.get("status") != "error" and "price" in data:
    #                 return {"price": float(data["price"]), "symbol": symbol}
    #             logger.warning(f"Price fetch failed for {symbol}: {data.get('message', 'unknown error')}")
    #         except Exception as e:
    #             logger.error(f"Live price error for {pair}: {e}")
    return _get_live_price_yfinance(pair)


def _get_atr_yfinance(pair: str, yf_ticker: str, interval: str = "1h", period: int = 14) -> dict | None:
    """Calculate ATR from yFinance candles as average of (high-low) over last `period` candles."""
    ATR_MINIMUMS_YF = {
        "GC=F": 3.0,   # XAUUSD / GC
        "ES=F": 8.0, "MES=F": 8.0,
        "NQ=F": 30.0, "MNQ=F": 30.0,
        "CL=F": 0.4,
    }
    try:
        # yfinance interval: "1h" is valid; fetch enough days for `period` hourly candles
        hist = yf.Ticker(yf_ticker).history(period="5d", interval=interval)
        if hist.empty or len(hist) < period:
            logger.warning(f"yFinance ATR: insufficient data for {yf_ticker}")
            return None
        recent = hist.tail(period)
        atr_value = float((recent["High"] - recent["Low"]).mean())
        minimum = ATR_MINIMUMS_YF.get(yf_ticker, 0)
        return {
            "atr": atr_value,
            "is_low_volatility": atr_value < minimum,
            "minimum": minimum,
            "symbol": pair,
        }
    except Exception as e:
        logger.error(f"yFinance ATR error for {pair} ({yf_ticker}): {e}")
        return None


def get_atr(pair: str, interval: str = "1h", period: int = 14) -> dict:
    """
    Get Average True Range to measure session volatility.
    Returns: { atr, is_low_volatility } or None
    Low volatility = ATR below historical average for that pair
    """
    if not pair:
        return None

    upper = pair.upper()

    # Use yFinance (free) for XAUUSD and all futures — no Twelve Data credits consumed
    if upper == "XAUUSD":
        return _get_atr_yfinance(pair, _XAUUSD_YF_TICKER, interval, period)
    if upper in YFINANCE_FUTURES_MAP:
        return _get_atr_yfinance(pair, YFINANCE_FUTURES_MAP[upper], interval, period)

    # Forex — use yFinance directly
    if upper in YFINANCE_FOREX_MAP:
        return _get_atr_yfinance(pair, YFINANCE_FOREX_MAP[upper], interval, period)
    return None
    # (Twelve Data removed — rate-limited on free tier)
    # if not TWELVE_DATA_API_KEY:
    #     if upper in YFINANCE_FOREX_MAP:
    #         return _get_atr_yfinance(pair, YFINANCE_FOREX_MAP[upper], interval, period)
    #     return None
    # symbol = normalize_symbol(pair)
    # ATR_MINIMUMS = { "XAU/USD": 3.0, "GBP/USD": 0.0008, ... }
    # try:
    #     resp = requests.get(f"{BASE_URL}/atr", params={...}, timeout=5)
    #     ...
    # except Exception as e:
    #     logger.error(f"ATR error for {pair}: {e}")


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
