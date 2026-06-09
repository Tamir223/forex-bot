"""
TNL Trader Claude Module - SaaS version
Passes market context and per-user provider stats to Claude.
"""

import json
import logging
import re
import time
import anthropic
import groq
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS, SYSTEM_PROMPT
from phase4_learning import get_confidence_modifier, get_session, log_trade_insight
from market import get_live_price, get_atr, check_entry_validity
from trading_calendar import check_news_window

logger = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

GROQ_API_KEY = "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"
groq_client = groq.Groq(api_key=GROQ_API_KEY)

CORRELATED_USD_LONGS = {"GBPUSD", "EURUSD", "AUDUSD", "NZDUSD"}

RISK_TIERS = {
    10: 0.0075,  # 0.75% — Premium
    9:  0.0050,  # 0.50% — Standard
    8:  0.0035,  # 0.35% — Conservative — below minimum, skip
    7:  0.0025,  # 0.25% — Minimal — always skip
}

_PIP_SIZE = {
    "USDJPY": 0.01, "EURJPY": 0.01, "GBPJPY": 0.01,
    "XAUUSD": 1.0, "US30": 1.0, "NAS100": 1.0,
}
_DEFAULT_PIP_SIZE = 0.0001

PIP_VALUES = {
    "EURUSD": 10.0,
    "GBPUSD": 10.0,
    "USDJPY": 9.30,
    "AUDUSD": 10.0,
    "USDCAD": 7.50,
    "NZDUSD": 10.0,
    "USDCHF": 10.50,
    "XAUUSD": 100.0,  # OANDA: $100/pt per lot
    "US30":   1.0,
    "NAS100": 1.0,
    "default": 10.0,
}

USER_PIP_VALUES = {
    8647323622: {  # Tamir - OANDA
        "EURUSD": 10.0, "GBPUSD": 10.0, "USDJPY": 9.3,
        "USDCAD": 7.5, "AUDUSD": 10.0, "NZDUSD": 10.0,
        "USDCHF": 10.5, "XAUUSD": 100.0,  # OANDA specific
    },
    5803919273: {  # Donald - 5ers standard
        "EURUSD": 10.0, "GBPUSD": 10.0, "USDJPY": 9.3,
        "USDCAD": 7.5, "AUDUSD": 10.0, "NZDUSD": 10.0,
        "USDCHF": 10.5, "XAUUSD": 10.0,  # Standard
    },
}


def analyze_signal(signal_text: str, account_state: dict, user_id: int = None) -> dict | None:
    try:
        # Quick parse for market data
        quick = _quick_parse(signal_text)
        pair = quick.get("pair")
        entry = quick.get("entry_zone")
        direction = quick.get("direction")
        provider = quick.get("provider")
        sl_raw = quick.get("stop_loss")

        # Market context
        market_ctx = {"pair": pair, "direction": direction}
        price_data = get_live_price(pair) if pair else None
        if price_data:
            market_ctx["current_price"] = price_data["price"]

        atr_data = get_atr(pair) if pair else None
        if atr_data:
            market_ctx["atr"] = atr_data["atr"]
            market_ctx["low_volatility"] = atr_data["is_low_volatility"]

        news_data = check_news_window(pair) if pair else {"has_news": False}
        market_ctx["news_in_window"] = news_data["has_news"]
        market_ctx["news_detail"] = news_data.get("warning_message", "")

        entry_check = check_entry_validity(pair, entry, None, direction)
        market_ctx["entry_valid"] = entry_check["valid"]
        market_ctx["entry_reason"] = entry_check.get("reason", "")
        market_ctx["pips_to_entry"] = entry_check.get("pips_away")

        # Per-user provider stats
        if user_id and provider:
            from database import get_provider_stats
            stats = get_provider_stats(user_id, provider)
            market_ctx["provider_win_rate"] = stats.get("win_rate")
            market_ctx["provider_trades"] = stats.get("total_trades", 0)
            market_ctx["provider_modifier"] = stats.get("confidence_modifier", 0)

        # Historical win rate for this pair+direction — requires 10+ trades to be meaningful
        if pair and direction:
            wr = _get_historical_win_rate(pair, direction)
            hist_total = wr.get("total", 0)
            market_ctx["hist_total"] = hist_total
            market_ctx["hist_win_rate"] = wr.get("win_rate") if hist_total >= 10 else None

        # Correlation warning (BUY on correlated USD pairs only)
        market_ctx["correlation_warning"] = _check_correlated_pairs(pair, direction, user_id)

        # Lot size suggestion based on risk % and parsed stop loss
        sl_pts = _compute_sl_pts(entry, sl_raw, pair)
        risk_pct = account_state.get("risk_percent") or account_state.get("risk")
        account_size = float(account_state.get("account_size", 10000))
        from futures_instruments import is_futures
        max_contracts = account_state.get("max_contracts") if is_futures(pair or "") else None
        market_ctx["is_futures"] = is_futures(pair or "")

        # Early exits
        if market_ctx.get("low_volatility"):
            return _block_response(pair, direction, provider,
                                   f"Low volatility session. ATR {market_ctx.get('atr')} below minimum.")

        if not market_ctx.get("entry_valid", True):
            return _block_response(pair, direction, provider,
                                   f"Entry missed. {market_ctx.get('entry_reason', '')}")

        # Build full message
        full_message = _build_message(account_state, market_ctx, signal_text)

        raw = None

        # 1. Try Groq (primary — free and fast)
        try:
            _groq_response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": full_message},
                ],
                max_tokens=1000,
            )
            raw = _groq_response.choices[0].message.content
            logger.info("[claude] Groq response received")
        except Exception as _groq_err:
            logger.warning(f"[claude] Groq failed, falling back to Anthropic: {_groq_err}")

        # 2. Anthropic fallback
        if raw is None:
            _api_retry_delay = 2
            for _api_attempt in range(3):
                try:
                    _anth_response = client.messages.create(
                        model=CLAUDE_MODEL,
                        max_tokens=CLAUDE_MAX_TOKENS,
                        system=SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": full_message}],
                    )
                    raw = _anth_response.content[0].text
                    logger.info("[claude] Anthropic fallback response received")
                    break
                except Exception as _api_err:
                    logger.error(f"[claude] Anthropic attempt {_api_attempt + 1} failed: {_api_err}")
                    if _api_attempt < 2:
                        logger.info(f"[claude] Retrying in {_api_retry_delay}s...")
                        time.sleep(_api_retry_delay)
                        _api_retry_delay *= 2
                    else:
                        logger.error("[claude] All Anthropic attempts failed")

        if raw is None:
            logger.error("[claude] Both Groq and Anthropic failed — returning None")
            return None
        cleaned = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(cleaned)

        # Apply provider modifier
        modifier = market_ctx.get("provider_modifier", 0)
        
        # Apply personal performance modifier (Phase 4 self-learning)
        if user_id and pair:
            hour_utc = __import__('datetime').datetime.utcnow().hour
            session = get_session(hour_utc)
            personal_modifier = get_confidence_modifier(user_id, pair, session)
            if personal_modifier != 0:
                modifier += personal_modifier
                logger.info(f"[phase4] Personal modifier for {pair}: {personal_modifier:+.1f}")
        if modifier and "confidence" in result:
            result["confidence"] = max(1, min(10, result["confidence"] + modifier))

        if market_ctx.get("news_in_window"):
            result["news_warning"] = True

        # Backfill new fields if Claude omitted them
        result.setdefault("correlation_warning", market_ctx.get("correlation_warning", False))
        result.setdefault("invalidation_level", None)
        result.setdefault("is_futures", market_ctx.get("is_futures", False))
        result["signal_source"] = provider or result.get("signal_source") or "UNKNOWN"
        result['lot_size_suggestion'] = None

        # Tiered risk sizing — prefer scanner score injected via account_state (reliable),
        # fall back to Claude's confidence field (may be a string like "9/10")
        _raw_conf = result.get("confidence", 9)
        if isinstance(_raw_conf, str):
            try:
                confidence_score = int(_raw_conf.split("/")[0].strip())
            except (ValueError, AttributeError):
                confidence_score = 9
        else:
            confidence_score = int(_raw_conf or 9)
        scanner_score = int(account_state.get('score', confidence_score))
        risk_val = RISK_TIERS.get(scanner_score, 0.005)
        if not risk_val or risk_val <= 0:
            risk_val = 0.005
        result['risk_percent'] = round(risk_val * 100, 4)
        logger.info(f"[risk] score={scanner_score} risk={result['risk_percent']}%")
        risk_percent = result['risk_percent']
        if not risk_percent or risk_percent <= 0:
            risk_percent = 0.005
        # Always recalculate lot size with tiered risk
        if result.get('stop_loss') and result.get('entry_zone'):
            sl_pts_tiered = _compute_sl_pts(str(result['entry_zone']), str(result['stop_loss']), pair)
            if sl_pts_tiered:
                from futures_instruments import is_futures as _is_fut
                max_c = account_state.get('max_contracts') if _is_fut(pair or "") else None
                result['lot_size_suggestion'] = _calculate_lot_size(
                    risk_percent, sl_pts_tiered, pair,
                    float(account_state.get('account_size', 10000)), max_c,
                    user_id=user_id
                )

        if "win_rate_context" not in result:
            if market_ctx.get("hist_win_rate") is not None:
                result["win_rate_context"] = (
                    f"{market_ctx['hist_win_rate']}% win rate on "
                    f"{market_ctx['hist_total']} {pair} {direction} trades"
                )
            else:
                result["win_rate_context"] = None

        # Recompute TP RR labels from actual prices — overrides Claude's often-wrong values.
        # Formula: rr = abs(tp - entry) / abs(sl - entry), direction-agnostic.
        try:
            _rr_entry = float(result.get("entry_zone") or 0)
            _rr_sl    = float(result.get("stop_loss")   or 0)
            _rr_sl_dist = abs(_rr_entry - _rr_sl)
            if _rr_sl_dist > 0:
                for _tp_key, _rr_key in (("tp1", "tp1_rr"), ("tp2", "tp2_rr"), ("tp3", "tp3_rr")):
                    _tp_val = result.get(_tp_key)
                    if _tp_val is not None:
                        _rr = round(abs(float(_tp_val) - _rr_entry) / _rr_sl_dist, 2)
                        result[_rr_key] = f"{_rr:.2f}R"
        except (TypeError, ValueError, ZeroDivisionError):
            pass

        # Grade/confidence override — reuse scanner_score already parsed by risk block above
        logger.info(f"[grade_override] raw_score={account_state.get('score')} parsed={scanner_score}")
        if scanner_score <= 7:
            result['grade'] = 'B'
        elif scanner_score == 8:
            result['grade'] = 'A'
        elif scanner_score >= 9:
            result['grade'] = 'A+'
        result['confidence'] = scanner_score
        result['confluence'] = min(scanner_score, 6)

        logger.info(f"[user:{user_id}] {result.get('decision')} {result.get('pair')} "
                    f"grade={result.get('grade')} conf={result.get('confidence')}")
        # Force source — Claude ignores the prompt instruction and returns "ICT" for SMC setups
        result['signal_source'] = 'TNL Scanner'
        result['source'] = 'TNL Scanner'
        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return None
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return None


def _block_response(pair, direction, provider, reason):
    return {
        "pair": pair, "direction": direction,
        "signal_source": provider or "UNKNOWN",
        "signal_format": "UNKNOWN",
        "grade": "BLOCK", "decision": "BLOCK",
        "risk_percent": 0, "confidence": 1,
        "news_warning": False, "signal_complete": False,
        "reason": reason, "trend": "unclear",
        "confirmation": False, "structure": False, "zone": False,
        "liquidity": False, "retracement_valid": False,
        "correlation_conflict": False, "session_valid": False,
        "disclaimer_present": False, "entry_zone": None,
        "stop_loss": None, "stop_loss_pts": None,
        "tp1": None, "tp2": None, "tp3": None, "tp4": None,
        "tp1_rr": None, "tp2_rr": None, "tp3_rr": None,
        "timeframe": None, "trade_type": None, "setup_type": None,
        "invalidation_level": None, "lot_size_suggestion": None,
        "correlation_warning": False, "win_rate_context": None,
    }


def _build_message(account_state, market_ctx, signal_text):
    pair = market_ctx.get("pair", "")
    direction = market_ctx.get("direction", "")

    lines = ["=== ACCOUNT STATE ==="]
    for k, v in account_state.items():
        lines.append(f"{k}={v}")

    lines.append("\n=== MARKET CONTEXT ===")
    if market_ctx.get("current_price"):
        lines.append(f"current_price={market_ctx['current_price']}")
    if market_ctx.get("atr"):
        lines.append(f"atr={market_ctx['atr']}")
        lines.append(f"low_volatility={market_ctx.get('low_volatility', False)}")
    lines.append(f"news_in_next_2h={market_ctx.get('news_in_window', False)}")
    if market_ctx.get("news_detail"):
        lines.append(f"news_detail={market_ctx['news_detail']}")
    if market_ctx.get("pips_to_entry") is not None:
        lines.append(f"pips_to_entry={market_ctx['pips_to_entry']}")
        lines.append(f"entry_valid={market_ctx.get('entry_valid', True)}")
    if market_ctx.get("provider_win_rate") is not None:
        lines.append(f"provider_win_rate={market_ctx['provider_win_rate']}")
        lines.append(f"provider_trades={market_ctx['provider_trades']}")

    # 1. Correlation warning
    lines.append("\n=== CORRELATION WARNING ===")
    if market_ctx.get("correlation_warning"):
        other = ", ".join(sorted(CORRELATED_USD_LONGS - {pair}))
        lines.append(
            f"WARNING: Open BUY trades exist on other correlated pairs ({other}). "
            "Stacked USD-long exposure increases correlated drawdown risk. "
            "Set correlation_warning=true in your JSON output."
        )
    else:
        lines.append("No correlated pair exposure detected. Set correlation_warning=false.")

    # 2. Historical win rate
    lines.append("\n=== HISTORICAL WIN RATE ===")
    if market_ctx.get("hist_win_rate") is not None:
        lines.append(
            f"Historical performance: {market_ctx['hist_win_rate']}% win rate across "
            f"{market_ctx['hist_total']} completed {pair} {direction} trades. "
            "Include this as win_rate_context in your JSON output."
        )
    else:
        lines.append(
            "Insufficient historical data for this pair/direction. "
            "Set win_rate_context to null in your JSON output."
        )

    # 3. Lot size suggestion
    lines.append("\n=== LOT SIZE SUGGESTION ===")
    if market_ctx.get("lot_size_suggestion"):
        lines.append(
        f"Recommended position size: {market_ctx['lot_size_suggestion']} "
        "(pre-calculated — do NOT recalculate, use this exact value as lot_size_suggestion in your JSON output)."
    )
    else:
        lines.append(
            "Stop loss distance unavailable — lot size cannot be pre-calculated. "
            "Calculate lot_size_suggestion yourself if stop loss is present in the signal, "
            "otherwise set to null."
        )

    # 4. Market context note + 5. Invalidation level instructions
    lines.append("\n=== ADDITIONAL INSTRUCTIONS ===")
    lines.append(
        "MARKET CONTEXT NOTE: In your 'reason' field, include one sentence noting whether "
        "the entry zone is near a key technical level (round number, major support/resistance, "
        "prior high/low, or psychological level)."
    )
    lines.append(
        "INVALIDATION LEVEL: Output 'invalidation_level' as a float — the exact price level "
        "at which this setup is no longer valid before entry is triggered. If price reaches "
        "this level first, the trade must be skipped. Set to null only if indeterminate."
    )
    lines.append(
        "REQUIRED JSON OUTPUT FIELDS (add to your existing schema):\n"
        "  invalidation_level: float or null\n"
        "  lot_size_suggestion: string (e.g. '0.33 lots on $10k account') or null\n"
        "  correlation_warning: bool\n"
        "  win_rate_context: string (e.g. '68% win rate on 11 GBPUSD BUY trades') or null"
    )

    lines.append("\n=== SIGNAL ===")
    lines.append(signal_text)
    return "\n".join(lines)


def _get_historical_win_rate(pair: str, direction: str) -> dict:
    try:
        from database import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE result = 'WIN') AS wins,
                COUNT(*) FILTER (WHERE result = 'LOSS') AS losses
            FROM trades
            WHERE pair = %s AND direction = %s AND result IN ('WIN', 'LOSS')
            """,
            (pair, direction)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            wins, losses = row['wins'], row['losses']
            total = wins + losses
            if total > 0:
                return {"win_rate": round((wins / total) * 100), "total": total}
    except Exception as e:
        logger.warning(f"Win rate query failed: {e}")
    return {}


def _check_correlated_pairs(pair: str, direction: str, user_id: int) -> bool:
    if not pair or direction != "BUY" or pair not in CORRELATED_USD_LONGS:
        return False
    try:
        from database import get_conn
        conn = get_conn()
        cur = conn.cursor()
        other = list(CORRELATED_USD_LONGS - {pair})
        cur.execute(
            "SELECT COUNT(*) FROM trades WHERE user_id = %s AND pair = ANY(%s) AND result IS NULL",
            (user_id, other)
        )
        count = cur.fetchone()['count']
        cur.close()
        conn.close()
        return count > 0
    except Exception as e:
        logger.warning(f"Correlation check failed: {e}")
    return False


def _compute_sl_pts(entry_str, sl_str, pair) -> float | None:
    try:
        entry_f = float(entry_str)
        sl_f = float(sl_str)
        diff = abs(entry_f - sl_f)

        from futures_instruments import is_futures
        if is_futures(pair or ""):
            return round(diff, 2)  # futures use raw point difference

        pip_size = _PIP_SIZE.get(pair, _DEFAULT_PIP_SIZE)
        return round(diff / pip_size, 1)
    except (TypeError, ValueError):
        return None


def _calculate_lot_size(risk_percent: float, sl_pts: float, pair: str,
                        account_size: float = 10000.0, max_contracts: int = None,
                        user_id: int = None) -> str | None:
    try:
        if sl_pts <= 0:
            return None
        risk_dollar = account_size * (risk_percent / 100)

        FUTURES_SYMBOLS = {"ES","MES","NQ","MNQ","CL","MCL","GC","MGC","RTY","YM","NG"}
        if pair and pair.upper() in FUTURES_SYMBOLS:
            from futures_instruments import calculate_contracts, format_sizing
            sizing = calculate_contracts(risk_dollar, sl_pts, pair, max_contracts)
            return format_sizing(sizing, account_size)

        # Forex lot calculation — use user-specific pip values when available
        _user_pips = USER_PIP_VALUES.get(user_id, {}) if user_id else {}
        pip_val = _user_pips.get(pair) or PIP_VALUES.get(pair, PIP_VALUES["default"])
        lot = round(risk_dollar / (sl_pts * pip_val), 2)
        acct_k = int(account_size / 1000)
        logger.info(f"[lots] pair={pair} user={user_id} risk=${risk_dollar:.2f} sl={sl_pts} pip_val={pip_val} lots={lot}")
        return f"{lot} lots on ${acct_k}k account"
    except Exception:
        return None


def _quick_parse(signal_text: str) -> dict:
    from futures_instruments import FUTURES_SYMBOLS
    import re

    result = {}
    text_upper = signal_text.upper()

    # Futures symbols checked in priority order (specific before generic)
    FUTURES_PRIORITY = ["MES", "MNQ", "MCL", "MGC", "ES", "NQ", "CL", "GC", "RTY", "YM", "NG"]
    FOREX_PAIRS = [
        "XAUUSD", "GBPUSD", "EURUSD", "USDJPY", "USDCAD",
        "AUDUSD", "NZDUSD", "USDCHF", "EURGBP", "EURJPY",
        "GBPJPY", "US30", "NAS100", "GBPNZD", "EURNZD",
        "AUDCAD", "AUDCHF", "AUDNZD", "CADJPY", "CHFJPY",
    ]

    # Detect symbol
    import re
    for symbol in FUTURES_PRIORITY:
        if re.search(r"\b" + symbol + r"\b", text_upper):
            result["pair"] = symbol
            result["is_futures"] = True
            break
    if "pair" not in result:
        for pair in FOREX_PAIRS:
            if pair in text_upper:
                result["pair"] = pair
                result["is_futures"] = False
                break

    # Detect direction — support BUY/SELL and LONG/SHORT
    if "BUY" in text_upper or "LONG" in text_upper:
        result["direction"] = "BUY"
    elif "SELL" in text_upper or "SHORT" in text_upper:
        result["direction"] = "SELL"

    # Entry zone
    m = re.search(r"ENTRY\s*ZONE\s*:?\s*([\d.]+)", signal_text, re.IGNORECASE)
    if m:
        result["entry_zone"] = m.group(1)

    # Stop loss
    m_sl = re.search(r"(?:STOP\s*LOSS|SL)\s*:?\s*([\d.]+)", signal_text, re.IGNORECASE)
    if m_sl:
        result["stop_loss"] = m_sl.group(1)

    # Provider detection
    for kw, name in [
        ("DON PIPS", "DON PIPS VIP"), ("ICT", "ICT"),
        ("FTMO", "FTMO Signals"), ("GOLD SCALPERS", "Gold Scalpers"),
        ("APEX", "Apex Signals"), ("TOPSTEP", "Topstep Signals"), ("TNL", "TNL Scanner"),
    ]:
        if kw in text_upper:
            result["provider"] = name
            break

    return result
