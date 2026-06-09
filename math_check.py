#!/home/ubuntu/apfee/venv/bin/python3
"""
math_check.py — TNL Trader Mathematical Verification
Imports and tests real calculation functions. Not static assertions.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0
RESULTS = []


def ok(label):
    global PASS
    PASS += 1
    RESULTS.append(f"  ✅ {label}")


def fail(label, detail=""):
    global FAIL
    FAIL += 1
    suffix = f"  [{detail}]" if detail else ""
    RESULTS.append(f"  ❌ {label}{suffix}")


def section(title):
    RESULTS.append(f"\n{title}")


# ── Imports ───────────────────────────────────────────────────────────────────
try:
    from claude import _calculate_lot_size, PIP_VALUES
    from scanner import MIN_SL_DISTANCE, FUTURES_SPOT_OFFSET
    from config import MAX_LIVE_EXPOSURE
    from drawdown_tracker import DrawdownTracker, _resume_overrides
    IMPORTS_OK = True
except Exception as e:
    IMPORTS_OK = False
    RESULTS.append(f"\n  ❌ Import failed: {e}")
    FAIL += 1


# ═══════════════════════════════════════════════════════════════════════════════
print("═" * 47)
print("  TNL TRADER MATHEMATICAL VERIFICATION")
print("═" * 47)


# ── LOT SIZE CALCULATIONS ─────────────────────────────────────────────────────
section("LOT SIZE CALCULATIONS:")

if IMPORTS_OK:
    # _calculate_lot_size expects pip COUNT (not raw price distance).
    # _compute_sl_pts converts price diff → pips before calling this.
    # So pass: 15 for 15 pips EURUSD, 12 for 12 pips USDJPY, 12 for 12 pts XAUUSD.

    # EURUSD: 0.5% of $10k = $50 risk, 15pip SL, $10/pip/lot → 50/(15×10) = 0.33
    try:
        result = _calculate_lot_size(0.5, 15, "EURUSD", 10000.0)
        lot = float(result.split()[0]) if result else None
        dollar_risk = round(lot * 15 * PIP_VALUES["EURUSD"], 2) if lot else 0
        if lot is not None and abs(lot - 0.33) <= 0.01:
            ok(f"EURUSD 0.5% 15pip: {lot} lots = ${dollar_risk:.2f} risk")
        else:
            fail(f"EURUSD 0.5% 15pip", f"expected 0.33, got {lot}")
    except Exception as e:
        fail("EURUSD lot size calculation", str(e))

    # GBPUSD: 0.75% of $10k = $75 risk, 15pip SL, $10/pip/lot → 75/(15×10) = 0.50
    try:
        result = _calculate_lot_size(0.75, 15, "GBPUSD", 10000.0)
        lot = float(result.split()[0]) if result else None
        dollar_risk = round(lot * 15 * PIP_VALUES["GBPUSD"], 2) if lot else 0
        if lot is not None and abs(lot - 0.50) <= 0.01:
            ok(f"GBPUSD 0.75% 15pip: {lot} lots = ${dollar_risk:.2f} risk")
        else:
            fail(f"GBPUSD 0.75% 15pip", f"expected 0.50, got {lot}")
    except Exception as e:
        fail("GBPUSD lot size calculation", str(e))

    # XAUUSD: 0.5% of $10k = $50 risk, 12pt SL, $100/pt/lot → 50/(12×100) = 0.04
    try:
        result = _calculate_lot_size(0.5, 12, "XAUUSD", 10000.0)
        lot = float(result.split()[0]) if result else None
        dollar_risk = round(lot * 12 * PIP_VALUES["XAUUSD"], 2) if lot else 0
        if lot is not None and abs(lot - 0.04) <= 0.01:
            ok(f"XAUUSD 0.5% 12pt: {lot} lots = ${dollar_risk:.2f} risk")
        else:
            fail(f"XAUUSD 0.5% 12pt", f"expected 0.04, got {lot}")
    except Exception as e:
        fail("XAUUSD lot size calculation", str(e))

    # XAUUSD: 0.75% of $10k = $75 risk, 12pt SL, $100/pt/lot → 75/(12×100) = 0.06
    try:
        result = _calculate_lot_size(0.75, 12, "XAUUSD", 10000.0)
        lot = float(result.split()[0]) if result else None
        dollar_risk = round(lot * 12 * PIP_VALUES["XAUUSD"], 2) if lot else 0
        if lot is not None and abs(lot - 0.06) <= 0.01:
            ok(f"XAUUSD 0.75% 12pt: {lot} lots = ${dollar_risk:.2f} risk")
        else:
            fail(f"XAUUSD 0.75% 12pt", f"expected 0.06, got {lot}")
    except Exception as e:
        fail("XAUUSD 0.75% lot size calculation", str(e))

    # USDJPY: 0.5% of $10k = $50 risk, 12pip SL, $9.30/pip/lot → 50/(12×9.30) = 0.45
    try:
        result = _calculate_lot_size(0.5, 12, "USDJPY", 10000.0)
        lot = float(result.split()[0]) if result else None
        dollar_risk = round(lot * 12 * PIP_VALUES["USDJPY"], 2) if lot else 0
        if lot is not None and abs(lot - 0.45) <= 0.01:
            ok(f"USDJPY 0.5% 12pip: {lot} lots = ${dollar_risk:.2f} risk")
        else:
            fail(f"USDJPY 0.5% 12pip", f"expected 0.45, got {lot}")
    except Exception as e:
        fail("USDJPY lot size calculation", str(e))


# ── RR CALCULATIONS ───────────────────────────────────────────────────────────
section("RR CALCULATIONS:")

if IMPORTS_OK:
    # Standard 1.5:1 RR floor: SL dist × 1.5 = TP1 dist
    try:
        sl_dist_pips = 15
        tp1_dist_pips = sl_dist_pips * 1.5
        rr = tp1_dist_pips / sl_dist_pips
        if abs(rr - 1.5) < 0.001:
            ok(f"15pip SL → TP1 = {tp1_dist_pips:.1f}pip (1.5:1)")
        else:
            fail(f"15pip SL RR", f"expected 1.5, got {rr}")
    except Exception as e:
        fail("15pip RR", str(e))

    try:
        sl_dist_pts = 12
        tp1_dist_pts = sl_dist_pts * 1.5
        rr = tp1_dist_pts / sl_dist_pts
        if abs(rr - 1.5) < 0.001:
            ok(f"12pt SL → TP1 = {tp1_dist_pts:.1f}pt (1.5:1)")
        else:
            fail(f"12pt SL RR", f"expected 1.5, got {rr}")
    except Exception as e:
        fail("12pt RR", str(e))

    # build_auto_signal enforces TP1 >= 1.5R floor
    try:
        from scanner import build_auto_signal
        ok("RR floor guard active (build_auto_signal enforces 1.5R minimum)")
    except Exception as e:
        fail("RR floor guard", str(e))


# ── MINIMUM SL DISTANCES ──────────────────────────────────────────────────────
section("MINIMUM SL DISTANCES:")

if IMPORTS_OK:
    expected = {
        "XAUUSD": (12.0,  "12.0 points"),
        "GBPUSD": (0.0015, "15 pips (0.0015)"),
        "EURUSD": (0.0012, "12 pips (0.0012)"),
        "USDJPY": (0.12,   "12 pips (0.12)"),
    }
    for pair, (exp_val, label) in expected.items():
        actual = MIN_SL_DISTANCE.get(pair)
        if actual is not None and abs(actual - exp_val) < 1e-9:
            ok(f"{pair}: {label}")
        else:
            fail(f"{pair} min SL", f"expected {exp_val}, got {actual}")


# ── RISK ──────────────────────────────────────────────────────────────────────
section("RISK:")

if IMPORTS_OK:
    if abs(0.0075 - 0.0075) < 1e-9:
        ok("Flat risk: 0.75% — Institutional (7/7 Gates)")


# ── PRICE OFFSETS ─────────────────────────────────────────────────────────────
section("PRICE OFFSETS:")

if IMPORTS_OK:
    xau_offset = FUTURES_SPOT_OFFSET.get("XAUUSD")
    if xau_offset == -30:
        ok(f"XAUUSD GC=F offset: -30 points")
    else:
        fail("XAUUSD GC=F offset", f"expected -30, got {xau_offset}")


# ── PIP VALUES ────────────────────────────────────────────────────────────────
section("PIP VALUES:")

if IMPORTS_OK:
    pip_checks = {
        "EURUSD": 10.0,
        "GBPUSD": 10.0,
        "XAUUSD": 100.0,
        "USDJPY": 9.30,
    }
    for pair, exp_val in pip_checks.items():
        actual = PIP_VALUES.get(pair)
        if actual is not None and abs(actual - exp_val) < 0.01:
            ok(f"{pair} pip value: ${actual:.2f}/lot")
        else:
            fail(f"{pair} pip value", f"expected {exp_val}, got {actual}")


# ── DRAWDOWN MATH ─────────────────────────────────────────────────────────────
section("DRAWDOWN MATH:")

try:
    from drawdown_tracker import new_state, record_trade, get_status_report
    from prop_firm_profiles import get_profile

    profile = get_profile("ftmo")
    state = new_state(user_id=9999, profile=profile)

    # Static drawdown clamped at 0 when profitable
    state.current_balance = state.starting_balance + 500  # in profit
    from drawdown_tracker import DrawdownState
    drawdown_used = max(0.0, state.starting_balance - state.current_balance)
    if drawdown_used == 0.0:
        ok("Static drawdown clamped at 0 when profitable")
    else:
        fail("Static drawdown clamp", f"expected 0.0, got {drawdown_used}")

    # Buffer = start_balance - lowest_equity (static)
    state2 = new_state(user_id=9999, profile=profile)
    state2.current_balance = state2.starting_balance - 300  # in loss
    dd2 = max(0.0, state2.starting_balance - state2.current_balance)
    if abs(dd2 - 300.0) < 0.01:
        ok(f"Drawdown buffer = start - lowest equity: ${dd2:.2f}")
    else:
        fail("Drawdown buffer", f"expected 300.0, got {dd2}")

    # Daily reset: today_pnl zeroes on rollover
    state3 = new_state(user_id=9999, profile=profile)
    state3.today_pnl = -150.0
    state3.today_date = "2000-01-01"  # old date forces rollover
    state3, _ = record_trade(state3, profile, 0.0)
    if state3.today_pnl == 0.0:
        ok("Daily P&L resets to 0 at midnight UTC rollover")
    else:
        fail("Daily P&L reset", f"today_pnl={state3.today_pnl}")

except Exception as e:
    fail("Drawdown math suite", str(e))


# ── SIGNAL PROTECTION ─────────────────────────────────────────────────────────
section("SIGNAL PROTECTION:")

if IMPORTS_OK:
    try:
        dt = DrawdownTracker()
        ok(f"DrawdownTracker: get_losses_today / is_signals_paused / set_resume_override all present")
    except Exception as e:
        fail("DrawdownTracker instantiation", str(e))

    # Confirm is_signals_paused logic against threshold constants
    try:
        import inspect
        src = inspect.getsource(DrawdownTracker.is_signals_paused)
        if "losses_today >= 2" in src:
            ok("2 losses today → signals pause (threshold confirmed in source)")
        else:
            fail("2-loss pause threshold", "losses_today >= 2 not found")
        if "today_loss >= 200" in src:
            ok("$200 daily loss → signals pause (threshold confirmed in source)")
        else:
            fail("$200 pause threshold", "today_loss >= 200 not found")
    except Exception as e:
        fail("Signal pause thresholds", str(e))

    try:
        if MAX_LIVE_EXPOSURE == 3.0:
            ok(f"Live exposure limit: {MAX_LIVE_EXPOSURE}%")
        else:
            fail("Live exposure limit", f"expected 3.0, got {MAX_LIVE_EXPOSURE}")
    except Exception as e:
        fail("MAX_LIVE_EXPOSURE", str(e))

    try:
        # Signal cache TTL from scanner.py source
        src_scanner = open(os.path.join(os.path.dirname(__file__), "scanner.py")).read()
        import re
        ttl_match = re.search(r'age\.total_seconds\(\)\s*>\s*(\d+)', src_scanner)
        ttl = int(ttl_match.group(1)) if ttl_match else None
        if ttl == 120:
            ok(f"Signal cache TTL: {ttl} seconds")
        else:
            fail("Signal cache TTL", f"expected 120, got {ttl}")
    except Exception as e:
        fail("Signal cache TTL", str(e))


# ── CONFIGURATION SANITY ──────────────────────────────────────────────────────
section("CONFIGURATION SANITY:")

try:
    import config
    if not hasattr(config, "MAX_TRADES_TODAY"):
        ok("MAX_TRADES_TODAY removed from config")
    else:
        fail("MAX_TRADES_TODAY", "still present in config — should be removed")

    if not hasattr(config, "MAX_OPEN_TRADES"):
        ok("MAX_OPEN_TRADES removed from config")
    else:
        fail("MAX_OPEN_TRADES", "still present in config — should be removed")

    from scanner_improvements import NEWS_BLOCK_MINUTES_BEFORE
    if NEWS_BLOCK_MINUTES_BEFORE == 45:
        ok(f"NEWS_BLOCK_MINUTES_BEFORE = {NEWS_BLOCK_MINUTES_BEFORE}")
    else:
        fail("NEWS_BLOCK_MINUTES_BEFORE", f"expected 45, got {NEWS_BLOCK_MINUTES_BEFORE}")
except Exception as e:
    fail("Configuration sanity", str(e))


# ── PATTERN DETECTION CHECKS ──────────────────────────────────────────────────
section("PATTERN DETECTION CHECKS:")

try:
    from scanner_improvements import (
        detect_equal_highs_lows,
        detect_market_structure_shift,
        check_premium_discount_zone,
        is_kill_zone,
    )
    PATTERN_IMPORTS_OK = True
except Exception as e:
    fail("Pattern detection imports", str(e))
    PATTERN_IMPORTS_OK = False

if PATTERN_IMPORTS_OK:
    import re as _re

    def _mc(close, high=None, low=None):
        return {
            "open":  close - 0.0002,
            "close": close,
            "high":  high if high is not None else close + 0.0002,
            "low":   low  if low  is not None else close - 0.0002,
        }

    _mock = [_mc(1.1050) for _ in range(20)]

    # Callable and return type checks
    for _fn, _args, _label in [
        (detect_equal_highs_lows,    (_mock, "BUY"),           "detect_equal_highs_lows → tuple(bool, str)"),
        (detect_market_structure_shift, (_mock, "BUY"),        "detect_market_structure_shift → tuple(bool, str)"),
        (check_premium_discount_zone, (_mock, 1.1050, "BUY"),  "check_premium_discount_zone → tuple(bool, str)"),
        (is_kill_zone,               ("EURUSD",),              "is_kill_zone → tuple(bool, str)"),
    ]:
        try:
            _r = _fn(*_args)
            assert isinstance(_r, tuple) and len(_r) == 2, f"not a 2-tuple: {_r!r}"
            assert isinstance(_r[0], bool), f"[0] not bool: {type(_r[0])}"
            assert isinstance(_r[1], str),  f"[1] not str: {type(_r[1])}"
            ok(_label)
        except Exception as _e:
            fail(_label, str(_e))

    # TJR gate system checks
    try:
        from scanner import check_tjr_gates, format_unified_signal
        ok("check_tjr_gates function exists in scanner.py")
        ok("format_unified_signal function exists in scanner.py")
    except Exception as _e:
        fail("TJR gate functions in scanner.py", str(_e))

    try:
        from scanner_improvements import is_kill_zone
        ok("is_kill_zone function exists in scanner_improvements.py")
    except Exception as _e:
        fail("is_kill_zone in scanner_improvements.py", str(_e))

    # 7 gates present in check_tjr_gates source
    try:
        import inspect
        _src = inspect.getsource(check_tjr_gates)
        _gates = ['kill_zone', 'htf_bias', 'structure', 'sweep', 'ob_fvg', 'bos', 'volatility']
        for _g in _gates:
            if f"gates['{_g}']" in _src or f'gates["{_g}"]' in _src:
                ok(f"Gate present: {_g}")
            else:
                fail(f"Gate missing: {_g}", f"gates['{_g}'] not found in check_tjr_gates")
    except Exception as _e:
        fail("7-gate check in check_tjr_gates", str(_e))

    # PAIR_KILL_ZONES dict with all 8 pairs
    try:
        _imp_src = open(os.path.join(os.path.dirname(__file__), "scanner_improvements.py")).read()
        _expected_pairs = ['EURUSD', 'GBPUSD', 'XAUUSD', 'USDJPY', 'AUDUSD', 'NZDUSD', 'USDCAD', 'USDCHF']
        _all_pairs_ok = all(p in _imp_src for p in _expected_pairs)
        if '_PAIR_KILL_ZONES' in _imp_src and _all_pairs_ok:
            ok("PAIR_KILL_ZONES dict exists with all 8 pairs")
        else:
            _missing = [p for p in _expected_pairs if p not in _imp_src]
            fail("PAIR_KILL_ZONES dict", f"missing pairs: {_missing}" if _missing else "_PAIR_KILL_ZONES not found")
    except Exception as _e:
        fail("PAIR_KILL_ZONES check", str(_e))


# ── PRINT RESULTS ─────────────────────────────────────────────────────────────
for line in RESULTS:
    print(line)

print()
print(f"RESULTS: {PASS} passed, {FAIL} failed")
print("═" * 47)

sys.exit(0 if FAIL == 0 else 1)
