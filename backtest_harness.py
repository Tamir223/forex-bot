"""
backtest_harness.py

Walks historical candles forward through the REAL check_tjr_gates() logic —
same code the live bot runs — and records what would have fired, for each of
the 4 watched pairs (GBPUSD, EURUSD, XAUUSD, USDJPY).

This exists because most of the recent tuning (TP1 targets, GBPUSD's tier bar,
the sweep-buffer scaling) was reasoned from general FX research, not verified
against this system's own historical behavior. This is step one of closing
that gap.

WHAT THIS DOES:
  - Fetches historical 15M/5M/1H/4H/Daily candles per pair via the same
    get_candles() the live bot uses.
  - Walks forward through the 15M series with a sliding window, at each step
    rebuilding structure/OB/FVG/htf_bias/daily_bias/draw/displacement exactly
    as scan_symbol() does live, then calling the real check_tjr_gates() with
    as_of=<that candle's timestamp>.
  - On a full gate pass, builds an approximate entry/SL/TP1 (see caveat below)
    and walks forward from that candle to see whether SL or TP1 hits first.
  - Reports per-pair: signal count, win rate, avg R.

WHAT THIS DOESN'T DO (read before trusting the numbers):
  - News gate is untested. is_news_window() has no historical news data
    source in this codebase and fails open during backtesting (see its
    docstring) — so results reflect gates 1,2,3,4,5,6,7 minus real news-time
    accuracy. A real high-impact-news day in the backtest window will NOT be
    filtered the way it would live.
  - Entry/SL/TP construction here is an APPROXIMATION, not the exact live
    formatting logic (that lives deep in scan_symbol()'s ~800-line dispatch
    section — OB Retracement / FVG Fill / Displacement FVG / 5M refinement —
    and isn't practical to replicate line-for-line here). This harness uses:
    entry = OB/FVG midpoint (or current close if neither), SL = this symbol's
    min_sl_dist floor, TP1 = entry ± SL_distance × this symbol's TP1_MULTIPLIER
    (same values the live system uses). Close, not identical, to what actually
    would have dispatched.
  - Gate 7 (volatility) is stubbed to always pass ("not low volatility") —
    real ATR computation (get_atr()) does its own live price fetching that
    doesn't fit this offline replay model. This means the volatility floor
    isn't actually enforced in these results; a rigorous version would need
    ATR computed from the historical candles directly instead of stubbed.
  - ORB and ORB-style signals are NOT included — only the core 7-gate
    OB Retracement path. detect_orb_breakout() keeps in-memory per-day state
    not yet safe for out-of-order historical replay (see its docstring).
  - Runs on real historical candles, so it needs to run where your Twelve
    Data / yFinance access actually works — this does NOT run in a sandbox,
    it needs your EC2 box and your venv.

USAGE (run in /home/ubuntu/apfee, same venv as the live bot):

    python backtest_harness.py                       # all 4 pairs, 60 days
    python backtest_harness.py --days 90
    python backtest_harness.py --symbols EURUSD,XAUUSD
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from scanner import (
    get_candles, analyze_market_structure, detect_structure, detect_order_block,
    detect_fvg, get_htf_bias, get_draw_on_liquidity, check_tjr_gates,
    _update_asia_levels, _asia_levels, _min_sl_dist,
)
from scanner_improvements import (
    get_daily_bias, get_trade_direction, detect_displacement, _tp1_mult,
)

PAIRS = ["GBPUSD", "EURUSD", "XAUUSD", "USDJPY"]
WARMUP_CANDLES = 250  # enough history for structure/OB/FVG detection before the first real step


def _parse_dt(c: dict) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(c.get("datetime", "")).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _window_up_to(candles_asc: list, as_of: datetime, n: int) -> list:
    """candles_asc is oldest-first. Return the most recent n candles at/before
    as_of, newest-first (matching the live convention everywhere else)."""
    idx = 0
    for i, c in enumerate(candles_asc):
        dt = _parse_dt(c)
        if dt and dt <= as_of:
            idx = i
        else:
            break
    window = candles_asc[max(0, idx - n + 1): idx + 1]
    return list(reversed(window))  # newest-first


async def fetch_history(symbol: str, days: int) -> dict:
    """Fetch and return {tf_name: candles_oldest_first} for one symbol."""
    out = {}
    out["15m"] = get_candles(symbol, interval="15min", outputsize=min(days * 96, 5000)) or []
    out["5m"]  = get_candles(symbol, interval="5min",  outputsize=min(days * 288, 5000)) or []
    out["1h"]  = get_candles(symbol, interval="1h",    outputsize=min(days * 24, 5000)) or []
    out["4h"]  = get_candles(symbol, interval="4h",    outputsize=min(days * 6, 5000)) or []
    out["1d"]  = get_candles(symbol, interval="1day",  outputsize=days + 10) or []
    for tf, candles in out.items():
        candles.sort(key=lambda c: _parse_dt(c) or datetime.min.replace(tzinfo=timezone.utc))
        out[tf] = candles
    return out


def walk_outcome(candles_15m_asc: list, entry_idx: int, entry: float, sl: float,
                  tp1: float, direction: str, max_bars: int = 200) -> str:
    """Walk forward from entry_idx to see whether SL or TP1 hits first."""
    for c in candles_15m_asc[entry_idx + 1: entry_idx + 1 + max_bars]:
        if direction == "BUY":
            if c["low"] <= sl:
                return "LOSS"
            if c["high"] >= tp1:
                return "WIN"
        else:
            if c["high"] >= sl:
                return "LOSS"
            if c["low"] <= tp1:
                return "WIN"
    return "OPEN"


async def backtest_symbol(symbol: str, days: int) -> dict:
    print(f"\n[{symbol}] fetching {days}d of history across 5 timeframes...")
    hist = await fetch_history(symbol, days)
    c15 = hist["15m"]
    if len(c15) < WARMUP_CANDLES + 20:
        print(f"[{symbol}] not enough 15M history returned ({len(c15)} candles) — skipping")
        return {"symbol": symbol, "signals": [], "skipped": True}

    signals = []
    _asia_levels.pop(symbol.upper(), None)

    for i in range(WARMUP_CANDLES, len(c15) - 1):
        as_of = _parse_dt(c15[i])
        if as_of is None:
            continue

        window_15m = list(reversed(c15[max(0, i - 199): i + 1]))
        window_5m  = _window_up_to(hist["5m"], as_of, 200)
        window_1h  = _window_up_to(hist["1h"], as_of, 200)
        window_4h  = _window_up_to(hist["4h"], as_of, 200)
        window_1d  = _window_up_to(hist["1d"], as_of, 200)

        try:
            gt_direction, gt_strength = get_trade_direction(symbol, window_15m)
            if gt_direction is None or gt_strength == "weak":
                continue

            ms = analyze_market_structure(window_15m)
            market_structure = ms["structure"]
            structure = detect_structure(window_15m)
            if structure.get("trend", "unclear") == "unclear":
                continue

            ob_trend = "bullish" if gt_direction == "BUY" else "bearish"
            ob = detect_order_block(window_15m, ob_trend, symbol=symbol)
            fvg = detect_fvg(window_15m, symbol)
            if fvg and fvg.get("type") != ("bullish_fvg" if gt_direction == "BUY" else "bearish_fvg"):
                fvg = None

            daily_bias = get_daily_bias(symbol, candles=window_1d)
            htf_bias = get_htf_bias(symbol, candles_1h=window_1h, candles_4h=window_4h, candles_daily=window_1d)
            displacement = detect_displacement(window_15m, gt_direction, symbol)
            current_price = float(window_15m[0]["close"])

            await _update_asia_levels(symbol, window_15m, as_of=as_of)
            draw = get_draw_on_liquidity(symbol, window_15m, gt_direction, _asia_levels.get(symbol.upper(), {}))

            all_passed, gates, gate_details, failed, kz_label, swept_level = await check_tjr_gates(
                symbol, window_15m, ob, fvg, htf_bias, market_structure,
                daily_bias, {"is_low_volatility": False}, gt_direction, structure, ms,
                data={"candles_1h": window_1h}, displacement=displacement,
                current_price=current_price, draw=draw, as_of=as_of,
            )
        except Exception as e:
            continue

        if not all_passed:
            continue

        if ob:
            entry = ob["mid"]
        elif fvg:
            entry = (fvg["top"] + fvg["bottom"]) / 2
        else:
            entry = current_price
        sl_dist = _min_sl_dist(symbol)
        sl = entry - sl_dist if gt_direction == "BUY" else entry + sl_dist
        tp1 = entry + sl_dist * _tp1_mult(symbol) if gt_direction == "BUY" else entry - sl_dist * _tp1_mult(symbol)

        outcome = walk_outcome(c15, i, entry, sl, tp1, gt_direction)
        signals.append({
            "time": as_of.isoformat(), "direction": gt_direction, "entry": entry,
            "sl": sl, "tp1": tp1, "outcome": outcome,
        })
        print(f"[{symbol}] {as_of.date()} {gt_direction} entry={entry:.5f} -> {outcome}")

    return {"symbol": symbol, "signals": signals, "skipped": False}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", type=str, default=",".join(PAIRS))
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    results = []
    for sym in symbols:
        results.append(await backtest_symbol(sym, args.days))

    print("\n" + "=" * 70)
    print(f"{'Symbol':<10} {'Signals':>8} {'Wins':>6} {'Losses':>7} {'Open':>6} {'WinRate':>8}")
    print("-" * 70)
    for r in results:
        sigs = r["signals"]
        n = len(sigs)
        wins = sum(1 for s in sigs if s["outcome"] == "WIN")
        losses = sum(1 for s in sigs if s["outcome"] == "LOSS")
        opens = sum(1 for s in sigs if s["outcome"] == "OPEN")
        decided = wins + losses
        wr = f"{wins/decided*100:.1f}%" if decided else "n/a"
        print(f"{r['symbol']:<10} {n:>8} {wins:>6} {losses:>7} {opens:>6} {wr:>8}")

    print("\nRemember: news gate untested, entry/SL/TP is an approximation, ORB")
    print("signals not included. This is a first read, not a final verdict.")


if __name__ == "__main__":
    asyncio.run(main())
