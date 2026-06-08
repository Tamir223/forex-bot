"""
TNL Trader Report Module - SaaS version
"""

from filter import calculate_confluence


def fmt(value, fallback="--"):
    if value is None or value == "" or str(value).lower() == "null":
        return fallback
    return str(value)


def confluence_bar(score):
    return f"{'█' * score}{'░' * (6 - score)} {score}/6"


def _sl_dist_display(analysis: dict) -> str:
    pair = (analysis.get('pair') or "").upper()
    futures = {'ES', 'MES', 'NQ', 'MNQ', 'CL', 'MCL', 'GC', 'MGC', 'RTY', 'YM', 'NG'}

    def _format(raw_dist):
        if pair in futures or pair == 'XAUUSD':
            return f"{round(raw_dist, 1)} pts"
        elif 'JPY' in pair:
            return f"{round(raw_dist * 100, 1)} pips"
        else:
            return f"{round(raw_dist * 10000, 1)} pips"

    # Compute from actual entry/SL prices — always raw price distance (e.g. 0.0012 for EURUSD)
    try:
        entry = float(analysis.get('entry_zone') or 0)
        sl = float(analysis.get('stop_loss') or 0)
        if entry and sl:
            return _format(abs(entry - sl))
    except (TypeError, ValueError):
        pass

    # Fallback: AI-provided stop_loss_pts (treat as raw price distance)
    raw = analysis.get('stop_loss_pts')
    if raw is None or str(raw).lower() in ("", "null", "--"):
        return "--"
    try:
        return _format(float(raw))
    except (TypeError, ValueError):
        return fmt(raw)


def _risk_line(analysis: dict) -> str:
    raw = analysis.get('risk_percent')
    pct = f"{float(raw):.2f}" if raw is not None else "--"
    if raw is None:
        return f"Risk %:       {pct}\n"
    conf = analysis.get('confidence', 9)
    risk_rounded = round(float(raw), 2)
    if risk_rounded == 0.75:
        label = "Premium — full confluence (10/10)"
    elif risk_rounded == 0.50:
        label = "Standard (9/10)"
    elif risk_rounded == 0.35:
        label = "Conservative (8/10) — below minimum"
    elif risk_rounded == 0.25:
        label = "Minimal (7/10) — skip this trade"
    else:
        label = ""
    suffix = f" ({label})" if label else ""
    return f"Risk %:       {pct}%{suffix}\n"


def _action_line(analysis: dict) -> str:
    """Build the execution action line at the bottom of the grade report."""
    _is_tc = "TREND CONTINUATION" in str(analysis.get("setup_type", "")).upper()
    _direction = str(analysis.get("direction", "")).upper()
    _entry = fmt(analysis.get("entry_zone"))
    _sl = fmt(analysis.get("stop_loss"))
    _tp1 = fmt(analysis.get("tp1"))
    _lots = analysis.get("lot_size_suggestion")

    if _is_tc:
        _dir_word = "BUY" if _direction == "BUY" else "SELL"
        _lots_line = f"\nLots: {fmt(_lots)}" if _lots else ""
        _body = (
            f"⚡ MARKET ORDER — Open {_dir_word} NOW at current price\n"
            f"SL: {_sl}\n"
            f"TP1: {_tp1}"
            + _lots_line
        )
    else:
        _dir_word = "Buy" if _direction == "BUY" else "Sell"
        _body = f"Set {_dir_word} Limit at {_entry}"

    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + _body + "\n"
        + f"━━━━━━━━━━━━━━━━━━━━"
    )


def execute_report(analysis: dict) -> str:
    confluence = calculate_confluence(analysis)
    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 TRADE SIGNAL REPORT\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"SOURCE:     {fmt(analysis.get('signal_source'))}\n"
        f"PAIR:       {fmt(analysis.get('pair'))} | {fmt(analysis.get('direction'))}\n"
        f"TIMEFRAME:  {fmt(analysis.get('timeframe'))}\n"
        f"TYPE:       {fmt(analysis.get('trade_type'))}\n"
        f"SETUP:      {fmt(analysis.get('setup_type'))}\n"
        f"GRADE:      {fmt(analysis.get('grade'))}\n"
        f"CONFIDENCE: {fmt(analysis.get('confidence'))}/10\n"
        f"CONFLUENCE: {confluence_bar(confluence)}\n"
        f"DECISION:   ✅ EXECUTE\n\n"
        f"📍 LEVELS\n"
        f"Entry:         {fmt(analysis.get('entry_zone'))}\n"
        f"Stop Loss:     {fmt(analysis.get('stop_loss'))} ({_sl_dist_display(analysis)})\n"
        f"Invalidation:  {fmt(analysis.get('invalidation_level'))}\n"
        f"TP1:           {fmt(analysis.get('tp1'))} ({fmt(analysis.get('tp1_rr'))})\n"
        f"TP2:           {fmt(analysis.get('tp2'))} ({fmt(analysis.get('tp2_rr'))})\n"
        f"TP3:           {fmt(analysis.get('tp3'))} ({fmt(analysis.get('tp3_rr'))})\n"
        f"TP4:           {fmt(analysis.get('tp4'))}\n\n"
        f"📈 ANALYSIS\n"
        f"Trend:        {fmt(analysis.get('trend'))}\n"
        f"Structure:    {fmt(analysis.get('structure'))}\n"
        f"Zone:         {fmt(analysis.get('zone'))}\n"
        f"Liquidity:    {fmt(analysis.get('liquidity'))}\n"
        f"Confirmation: {fmt(analysis.get('confirmation'))}\n"
        f"Win Rate:     {fmt(analysis.get('win_rate_context'))}\n\n"
        f"💰 RISK\n"
        + _risk_line(analysis)
        + (f"Contracts:    {fmt(analysis.get('lot_size_suggestion'))}\n"
           if analysis.get('is_futures') and analysis.get('pair', '') in ['ES','MES','NQ','MNQ','CL','MCL','GC','MGC','RTY','YM','NG']
           else
           f"Lot Size:     {fmt(analysis.get('lot_size_suggestion'))}\n")
        + (f"⚠️ CORRELATION: Multiple correlated USD-long pairs open\n"
           if analysis.get('correlation_warning') else "")
        + f"\n📝 REASON\n"
        f"{fmt(analysis.get('reason'))}\n"
        + _action_line(analysis)
    )


def blocked_report(analysis: dict, reason: str) -> str:
    pair = fmt(analysis.get("pair"))
    direction = fmt(analysis.get("direction"))
    msgs = {
        "incomplete_signal": (
            f"⚠️ INCOMPLETE SIGNAL\n{pair} {direction} missing key levels.\n"
            f"Entry: {fmt(analysis.get('entry_zone'))}\n"
            f"Stop Loss: {fmt(analysis.get('stop_loss'))}\n"
            f"Wait for the full signal."
        ),
        "news_warning": (
            f"📰 NEWS WARNING — BLOCKED\n{pair} {direction}\n"
            f"High impact news detected in next 2 hours.\n"
            f"Reason: {fmt(analysis.get('reason'))}"
        ),
        "low_confidence": (
            f"⛔ {pair} {direction} — LOW CONFIDENCE ({fmt(analysis.get('confidence'))}/10)\n"
            f"Reason: {fmt(analysis.get('reason'))}"
        ),
        "low_confluence": (
            f"⛔ {pair} {direction} — LOW CONFLUENCE\n"
            f"Not enough technical factors aligned.\n"
            f"Reason: {fmt(analysis.get('reason'))}"
        ),
        "correlation_conflict": (
            f"⛔ {pair} {direction} — CORRELATION CONFLICT\n"
            f"Reason: {fmt(analysis.get('reason'))}"
        ),
        "claude_block": (
            f"⛔ {pair} {direction} — BLOCKED\n"
            f"Reason: {fmt(analysis.get('reason'))}"
        ),
    }
    return msgs.get(reason, f"⛔ {pair} {direction} — BLOCKED\n{fmt(analysis.get('reason'))}")


def fast_gate_blocked(reason: str) -> str:
    return f"🚫 SIGNAL BLOCKED\n{reason}"


def not_subscribed_message() -> str:
    return (
        "You don't have an active TNL Trader subscription.\n\n"
        "Subscribe at tnltrader.com to get started.\n"
        "Plans from $47/month."
    )


def status_report(state, plan_tier: str) -> str:
    from datetime import date as _date
    daily_pnl = state.daily_pnl
    challenge_pnl = getattr(state, "challenge_pnl", 0.0) or 0.0
    challenge_target = getattr(state, "challenge_target", 10.0) or 10.0
    challenge_start_date = getattr(state, "challenge_start_date", None)

    daily_sign = "+" if daily_pnl >= 0 else ""
    ch_sign = "+" if challenge_pnl >= 0 else ""
    daily_limit_remaining = 5.0 - abs(min(0.0, daily_pnl))
    target_remaining = max(0.0, challenge_target - challenge_pnl)

    if challenge_start_date:
        days_elapsed = (_date.today() - challenge_start_date).days
        days_remaining = max(0, 30 - days_elapsed)
    else:
        days_remaining = 30

    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 ACCOUNT STATUS\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Plan:           {plan_tier.upper()}\n"
        f"Trades today:   {state.trades_today}\n"
        f"Open trades:    {state.open_trades}\n"
        f"Live exposure:  {state.live_exposure}%\n"
        f"Session losses: {state.session_losses}\n"
        f"Weekly losses:  {state.weekly_losses}\n"
        f"Win streak:     {state.win_streak}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 CHALLENGE STATUS\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Daily P&L:     {daily_sign}{daily_pnl:.1f}% (limit: -5%)\n"
        f"Challenge P&L: {ch_sign}{challenge_pnl:.1f}% (target: {challenge_target:.0f}%)\n"
        f"Remaining:     {target_remaining:.1f}% to target\n"
        f"Daily limit:   {daily_limit_remaining:.1f}% remaining today\n"
        f"Days left:     {days_remaining} of 30\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


def trade_executed() -> str:
    return "✅ Trade logged.\nGood luck! 💰"


def trade_skipped() -> str:
    return "⏭ Trade skipped."


def trade_logged_win() -> str:
    return "✅ WIN logged. Keep going! 💰"


def trade_logged_loss() -> str:
    return "❌ LOSS logged.\nReview before next signal."


def weekly_reset() -> str:
    return "🔄 Weekly reset complete. New week, clean slate."
