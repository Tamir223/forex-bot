"""
TNL Trader Market Data Module
Fetches live price and ATR via Yahoo Finance (forex/futures) and Coinbase (XAUUSD).
"""

import logging
import datetime as _dt
import requests
import yfinance as yf

# yFinance forex tickers
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
    'US100': 'NQ=F', 'US30': 'YM=F',
}
# XAUUSD is handled via yFinance using the gold futures ticker
_XAUUSD_YF_TICKER = "GC=F"
# Mirrors scanner.FUTURES_SPOT_OFFSET — GC=F trades ~30 pts above MT5 XAUUSD spot
_FUTURES_SPOT_OFFSET = {
    "XAUUSD": -30,
}

logger = logging.getLogger(__name__)

# Per-symbol cache for Yahoo Finance direct API — avoids yfinance internal request cache
# {ticker: {"price": float, "ts": float}}
_forex_price_cache: dict = {}
_FOREX_CACHE_TTL = 30  # seconds


def _get_yahoo_direct_price(yf_ticker: str) -> float | None:
    """
    Fetch regularMarketPrice directly from Yahoo Finance v8 chart API.
    Bypasses yfinance's internal cache — returns data typically <60s old.
    Results cached per-ticker for 30s to avoid rate limits.
    """
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    cached = _forex_price_cache.get(yf_ticker)
    if cached and now - cached["ts"] < _FOREX_CACHE_TTL:
        return cached["price"]
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_ticker}?interval=1m&range=1d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=3,
        )
        meta = r.json()["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
        _forex_price_cache[yf_ticker] = {"price": price, "ts": now}
        return price
    except Exception:
        return None

# Map common signal pair names to canonical symbols (used by scanner)
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
    """Convert signal pair name to canonical symbol"""
    if not pair:
        return None
    upper = pair.upper().strip()
    return SYMBOL_MAP.get(upper, upper)


def _get_live_price_yfinance(pair: str) -> dict | None:
    """Real-time forex price. Yahoo direct API first, yFinance fast_info fallback."""
    ticker_sym = YFINANCE_FOREX_MAP.get(pair.upper())
    if not ticker_sym:
        return None

    # Method 1: Yahoo Finance v8 direct HTTP — bypasses yfinance cache, ~1-60s fresh
    price = _get_yahoo_direct_price(ticker_sym)
    if price is not None:
        price = round(price, 5)
        logger.info(f"[market] Yahoo direct price for {pair}: {price}")
        return {"price": price, "symbol": pair, "source": "yahoo-direct"}

    # Method 2: yFinance fast_info fallback
    try:
        t = yf.Ticker(ticker_sym)
        try:
            price = float(t.fast_info['last_price'])
        except Exception:
            hist = t.history(period="1d", interval="1m")
            if hist.empty:
                return None
            price = float(hist['Close'].iloc[-1])
        price = round(price, 5)
        logger.info(f"[market] yFinance fallback price for {pair}: {price}")
        return {"price": price, "symbol": pair, "source": "yfinance"}
    except Exception as e:
        logger.error(f"yFinance live price error for {pair}: {e}")
        return None


def get_live_price(pair: str) -> dict:
    """
    Get current price for a pair.
    Forex: Yahoo Direct primary, yFinance fallback. XAUUSD: Coinbase. Futures: yFinance.
    Returns: { price, symbol } or None
    """
    if not pair:
        return None

    upper = pair.upper()

    # XAUUSD: Coinbase spot. Futures: yFinance.
    if upper == "XAUUSD" or upper in YFINANCE_FUTURES_MAP:
        yf_ticker = "GC=F" if upper == "XAUUSD" else YFINANCE_FUTURES_MAP[upper]
        try:
            t = yf.Ticker(yf_ticker)

            if upper == "XAUUSD":
                # Method 1: Coinbase spot price — free, no key, real-time XAU/USD
                try:
                    _cb = requests.get(
                        "https://api.coinbase.com/v2/prices/XAU-USD/spot",
                        timeout=3,
                    )
                    _cb_price = float(_cb.json()["data"]["amount"])
                    logger.info(f"[market] XAUUSD coinbase spot: {_cb_price}")
                    return {"price": round(_cb_price, 2), "symbol": pair, "source": "coinbase"}
                except Exception as _e0:
                    logger.warning(f"[market] XAUUSD coinbase failed: {_e0} — trying yfinance")

                # Method 2: yf.download() with prepost=True — forces a fresh network request
                try:
                    dl = yf.download("GC=F", period="1d", interval="1m",
                                     progress=False, prepost=True)
                    if not dl.empty:
                        # yfinance 1.3+ download() returns MultiIndex — ('Close', 'GC=F')
                        _cl = dl['Close'].iloc[-1]
                        raw1m = float(_cl.iloc[0] if hasattr(_cl, 'iloc') else _cl)
                        last_time = dl.index[-1]
                        age = (_dt.datetime.now(_dt.timezone.utc) - last_time).total_seconds() \
                              if hasattr(last_time, 'tzinfo') and last_time.tzinfo else 0
                        logger.info(f"[market] XAUUSD yf download: {raw1m} age={age:.0f}s")
                        if age < 300:
                            price = round(raw1m + _FUTURES_SPOT_OFFSET.get(upper, 0), 2)
                            return {"price": price, "symbol": pair}
                        logger.warning(f"[market] XAUUSD yf download stale ({age:.0f}s) — trying fast_info")
                except Exception as _e1:
                    logger.warning(f"[market] XAUUSD yf download failed: {_e1} — trying fast_info")

                # Method 3: fast_info fallback
                try:
                    raw = float(t.fast_info['last_price'])
                    price = round(raw + _FUTURES_SPOT_OFFSET.get(upper, 0), 2)
                    return {"price": price, "symbol": pair}
                except Exception as _e2:
                    logger.error(f"[market] XAUUSD all methods failed: {_e2}")
                    return None

            # Non-XAUUSD futures: fast_info primary, 1m candle fallback
            try:
                raw = float(t.fast_info['last_price'])
            except Exception:
                hist = t.history(period="1d", interval="1m")
                if hist.empty:
                    return None
                raw = float(hist['Close'].iloc[-1])
            price = round(raw + _FUTURES_SPOT_OFFSET.get(upper, 0), 2)
            return {"price": price, "symbol": pair}
        except Exception as e:
            logger.error(f"yFinance price error for {pair}: {e}")
        return None

    # Forex — Yahoo Direct primary, yFinance fallback
    return _get_live_price_yfinance(pair)


def _get_atr_yfinance(pair: str, yf_ticker: str, interval: str = "1h", period: int = 14) -> dict | None:
    """Calculate ATR from yFinance candles as average of (high-low) over last `period` candles."""
    ATR_MINIMUMS_YF = {
        "GC=F": 3.0,   # XAUUSD / GC
        "ES=F": 8.0, "MES=F": 8.0,
        "NQ=F": 30.0, "MNQ=F": 30.0,
        "YM=F": 80.0,
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

    if upper == "XAUUSD":
        return _get_atr_yfinance(pair, _XAUUSD_YF_TICKER, interval, period)
    if upper in YFINANCE_FUTURES_MAP:
        return _get_atr_yfinance(pair, YFINANCE_FUTURES_MAP[upper], interval, period)

    if upper in YFINANCE_FOREX_MAP:
        return _get_atr_yfinance(pair, YFINANCE_FOREX_MAP[upper], interval, period)
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
