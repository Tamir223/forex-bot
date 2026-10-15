import sys
import math
import datetime
import statistics

MIN = datetime.timedelta(minutes=1)
NL = chr(10)
SIM_TICKER = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X", "USDCHF": "USDCHF=X"}
FILL_HOURS = 6
HOLD_HOURS = 12
LATENCY_MIN = 2
SWEEP_LOOKBACK_S = 75
SWEPT_LOOKBACK_S = 75
ASIA_LOOKBACK_S = 14400
DEMOTE_LOOKBACK_S = 120
EPISODE_MIN = 60
MIN_GROUP = 8
ASIA_REL_TOL = 3e-5
KINDS = ("asia_range", "weekly", "round_number", "liquidity_run", "recent_swing", "unknown")
REACH = (1.0, 1.5, 2.0, 3.0)
KIND_NOTE = {
    "asia_range": "wick through the Asia high or low, close back inside",
    "weekly": "wick through the previous week high or low, close back inside",
    "round_number": "wick through a round number, close back inside",
    "liquidity_run": "closes THROUGH the level (needs OB and FVG together)",
    "recent_swing": "wick through a 7 or 20 candle swing, close back inside",
    "unknown": "no label in the logs and no way to work it out",
}


def split_line(line):
    parts = line.split(" - ", 3)
    if len(parts) < 4 or parts[2].strip() != "INFO":
        return None
    try:
        t = datetime.datetime.strptime(parts[0][:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return t, parts[3]


def kv(text):
    out = {}
    for tok in text.replace(",", " ").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def numeric(d, keys):
    return all(k in d and isinstance(d[k], float) and math.isfinite(d[k]) for k in keys)


def parse(lines):
    """Pulls out the line types the audit needs. Journal format: journalctl -o cat."""
    out = {"dispatch": [], "sweep": [], "swept": [], "asia": [], "demote": []}
    for ln in lines:
        if "[" not in ln:
            continue
        r = split_line(ln)
        if r is None:
            continue
        t, msg = r
        tok = msg.split()
        if len(tok) < 2:
            continue
        if msg.startswith("[signal_dispatch] ") and len(tok) >= 3 and tok[2] in ("BUY", "SELL"):
            d = kv(msg)
            if numeric(d, ("entry", "sl", "tp1")):
                grade = d.get("grade")
                token = d.get("sweep")
                out["dispatch"].append({
                    "t": t, "sym": tok[1], "dir": tok[2], "entry": d["entry"], "sl": d["sl"], "tp1": d["tp1"],
                    "grade": grade if isinstance(grade, str) else "",
                    "token": token if isinstance(token, str) and token in KINDS else None,
                })
        elif msg.startswith("[sweep] "):
            text = msg.split(None, 2)[2] if len(tok) >= 3 else ""
            kind = None
            if text.startswith("Weekly "):
                kind = "weekly"
            elif text.startswith("Round number sweep"):
                kind = "round_number"
            elif text.startswith("liquidity run"):
                kind = "liquidity_run"
            if kind:
                out["sweep"].append({"t": t, "sym": tok[1], "kind": kind})
        elif msg.startswith("[scanner] ") and " swept-level SL:" in msg:
            d = kv(msg)
            if numeric(d, ("swept",)):
                out["swept"].append({"t": t, "sym": tok[1], "level": d["swept"]})
        elif msg.startswith("[asia] ") and " levels set" in msg:
            d = kv(msg.split("levels set", 1)[1])
            if numeric(d, ("high", "low")):
                out["asia"].append({"t": t, "sym": tok[1], "high": d["high"], "low": d["low"]})
        elif msg.startswith("[ob_demote] ") and len(tok) >= 3 and tok[2] in ("BUY", "SELL"):
            out["demote"].append({"t": t, "sym": tok[1], "dir": tok[2]})
    return out


def latest_before(rows, sym, t, lookback_s):
    best = None
    for r in rows:
        if r["sym"] != sym:
            continue
        age = (t - r["t"]).total_seconds()
        if 0 <= age <= lookback_s and (best is None or r["t"] >= best["t"]):
            best = r
    return best


def classify(sig, parsed):
    """Returns (kind, source). An explicit sweep= token wins. Otherwise the kind is worked out from the log lines
    of the same scan: a [sweep] line names weekly, round number or liquidity run; with none of those, a swept
    level equal to the Asia high or low means the Asia range; any other swept level is a recent swing."""
    if sig["token"]:
        return sig["token"], "token"
    s = latest_before(parsed["sweep"], sig["sym"], sig["t"], SWEEP_LOOKBACK_S)
    if s:
        return s["kind"], "inferred"
    w = latest_before(parsed["swept"], sig["sym"], sig["t"], SWEPT_LOOKBACK_S)
    if w is None:
        return "unknown", "none"
    a = latest_before(parsed["asia"], sig["sym"], sig["t"], ASIA_LOOKBACK_S)
    if a is None:
        # Without the Asia levels there is no telling an Asia sweep from a recent swing (this happens with archived lines,
        # which keep the swept-level line but not the Asia or sweep lines), so the type is not guessed.
        return "unknown", "none"
    tol = max(abs(w["level"]) * ASIA_REL_TOL, 1e-9)
    if abs(w["level"] - a["high"]) <= tol or abs(w["level"] - a["low"]) <= tol:
        return "asia_range", "inferred"
    return "recent_swing", "inferred"


def entry_path(sig, parsed):
    d = latest_before([x for x in parsed["demote"] if x["dir"] == sig["dir"]], sig["sym"], sig["t"], DEMOTE_LOOKBACK_S)
    return "demoted" if d else "normal"


def episodes(sigs):
    """The same symbol, direction and entry inside an hour is one signal (older code could repeat it)."""
    by = {}
    for s in sigs:
        by.setdefault((s["sym"], s["dir"], round(s["entry"], 6)), []).append(s)
    out = []
    for rows in by.values():
        rows.sort(key=lambda x: x["t"])
        last_t = None
        for r in rows:
            if last_t is None or (r["t"] - last_t).total_seconds() > EPISODE_MIN * 60:
                out.append(r)
            last_t = r["t"]
    out.sort(key=lambda x: x["t"])
    return out


def find_start(bars, t):
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] >= t:
            hi = mid
        else:
            lo = mid + 1
    return lo if lo < len(bars) else None


def simulate(bars, sig):
    """bars: sorted (start_utc, open, high, low, close) of 5 minutes. A limit order fills on the first bar that trades
    through the entry. In the fill bar only the stop is checked, and the stop wins any same-bar tie.
    Also returns the deepest dip against the entry before the target (mae_r) and the best move in the trade's
    favor before the stop was touched (reach_r), both in R."""
    sell = sig["dir"] == "SELL"
    e, sl, tp1 = sig["entry"], sig["sl"], sig["tp1"]
    risk = abs(sl - e)
    ok = (sl > e and tp1 < e) if sell else (sl < e and tp1 > e)
    if risk <= 0 or not ok:
        return {"outcome": "bad geometry"}
    r1 = abs(tp1 - e) / risk
    i0 = find_start(bars, sig["t"] + LATENCY_MIN * MIN)
    if i0 is None:
        return {"outcome": "no data", "r1": r1}
    t_end = sig["t"] + FILL_HOURS * 60 * MIN
    fill_i = None
    for i in range(i0, len(bars)):
        if bars[i][0] > t_end:
            break
        if (bars[i][2] >= e) if sell else (bars[i][3] <= e):
            fill_i = i
            break
    if fill_i is None:
        return {"outcome": "no fill", "r1": r1}
    t_fill = bars[fill_i][0]
    fb = bars[fill_i]
    if (sell and fb[2] >= sl) or ((not sell) and fb[3] <= sl):
        return {"outcome": "SL", "r1": r1, "r": -1.0, "mae_r": 1.0, "reach_r": 0.0, "t_fill": t_fill}
    adverse = max(0.0, ((fb[2] - e) if sell else (e - fb[3])) / risk)
    fav = 0.0
    result = None
    mae_at_tp = None
    for i in range(fill_i + 1, len(bars)):
        b = bars[i]
        if (b[0] - t_fill).total_seconds() > HOLD_HOURS * 3600:
            break
        if (sell and b[2] >= sl) or ((not sell) and b[3] <= sl):
            if result is None:
                result = "SL"
                adverse = 1.0
            break
        b_adv = ((b[2] - e) if sell else (e - b[3])) / risk
        b_fav = ((e - b[3]) if sell else (b[2] - e)) / risk
        if result is None:
            adverse = max(adverse, b_adv)
        fav = max(fav, b_fav)
        if result is None and ((sell and b[3] <= tp1) or ((not sell) and b[2] >= tp1)):
            result = "TP1"
            mae_at_tp = adverse
    if result == "TP1":
        return {"outcome": "TP1", "r1": r1, "r": r1, "mae_r": mae_at_tp, "reach_r": fav, "t_fill": t_fill}
    if result == "SL":
        return {"outcome": "SL", "r1": r1, "r": -1.0, "mae_r": 1.0, "reach_r": fav, "t_fill": t_fill}
    return {"outcome": "unresolved", "r1": r1, "mae_r": adverse, "reach_r": fav, "t_fill": t_fill}


def wilson(k, n, z=1.96):
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def group_stats(rows):
    sim = [r for r in rows if r.get("outcome") in ("TP1", "SL", "unresolved", "no fill")]
    filled = [r for r in rows if r.get("outcome") in ("TP1", "SL", "unresolved")]
    res = [r for r in filled if r["outcome"] in ("TP1", "SL")]
    wins = [r for r in res if r["outcome"] == "TP1"]
    st = {"signals": len(rows), "simulated": len(sim), "filled": len(filled), "resolved": len(res), "wins": len(wins)}
    if res:
        st["rate"] = len(wins) / len(res)
        st["lo"], st["hi"] = wilson(len(wins), len(res))
        st["be"] = 1.0 / (1.0 + statistics.mean(r["r1"] for r in res))
        st["exp"] = statistics.mean(r["r"] for r in res)
    if wins:
        st["mae_med"] = statistics.median(r["mae_r"] for r in wins)
    if filled:
        for th in REACH:
            st["reach_%s" % th] = sum(1 for r in filled if r["reach_r"] >= th) / len(filled)
    if len(res) < MIN_GROUP:
        st["verdict"] = "too few to judge"
    elif st["lo"] > st["be"]:
        st["verdict"] = "above break-even"
    elif st["hi"] < st["be"]:
        st["verdict"] = "below break-even"
    else:
        st["verdict"] = "unclear"
    return st


def pct(x):
    return "%3d%%" % round(x * 100)


def fmt_row(label, st, width=22):
    if st["resolved"]:
        rate = "%s (%s-%s)" % (pct(st["rate"]), pct(st["lo"]).strip(), pct(st["hi"]).strip())
        be = pct(st["be"])
        ex = "%+.2f" % st["exp"]
    else:
        rate, be, ex = "  -", "  -", "  -"
    mae = "%.2fR" % st["mae_med"] if "mae_med" in st else "-"
    r15 = pct(st["reach_1.5"]) if "reach_1.5" in st else "-"
    return "  %-*s %4d %6d %5d %5d  %-14s %6s %7s %7s %6s  %s" % (
        width, label, st["signals"], st["filled"], st["resolved"], st["wins"], rate, be, ex, mae, r15, st["verdict"])


HEAD = "  %-*s %4s %6s %5s %5s  %-14s %6s %7s %7s %6s  %s" % (22, "", "sigs", "filled", "done", "wins", "win rate (95%)", "b/even", "avg R", "dip", "R1.5", "verdict")


def report(rows, not_simulated, window):
    out = []
    A = out.append
    A("SWEEP AUDIT: %d signals, %s" % (len(rows), window))
    A("Times are the journal clock. Outcomes are simulated on 5-minute bars: limit order fills when price trades through the entry,")
    A("the stop wins any same-bar tie, target is TP1, trades are given %d hours to fill and %d hours to finish." % (FILL_HOURS, HOLD_HOURS))
    A("done = filled and reached TP1 or the stop. b/even = win rate needed to break even at that group's TP1 distance.")
    A("dip = median deepest move against entry before TP1 on winners (in R). R1.5 = share of filled trades that reached 1.5R before the stop.")
    A("verdict needs %d finished trades and compares the 95%% range with break-even. It is evidence, not proof." % MIN_GROUP)
    if not_simulated:
        A("No price simulation for: %s (no verified price feed)." % ", ".join(sorted(not_simulated)))
    A("")
    A("BY SWEEP TYPE, ALL PAIRS")
    A(HEAD)
    for k in KINDS:
        sub = [r for r in rows if r["kind"] == k]
        if sub:
            A(fmt_row(k, group_stats(sub)))
    A(fmt_row("all", group_stats(rows)))
    A("")
    A("BY PAIR AND SWEEP TYPE")
    A(HEAD)
    for sym in sorted({r["sym"] for r in rows}):
        for k in KINDS:
            sub = [r for r in rows if r["sym"] == sym and r["kind"] == k]
            if sub:
                A(fmt_row("%s %s" % (sym, k), group_stats(sub)))
    A("")
    A("BY ENTRY PATH (demoted = a failed-tier order block was replaced by the FVG)")
    A(HEAD)
    for p in ("normal", "demoted"):
        sub = [r for r in rows if r["path"] == p]
        if sub:
            A(fmt_row(p, group_stats(sub)))
    A("")
    A("EACH SIGNAL")
    A("  %-16s %-7s %-4s %-14s %-9s %-8s %-10s %6s %6s %6s" % ("time", "pair", "dir", "sweep type", "label by", "path", "outcome", "R", "dip", "reach"))
    for r in rows:
        A("  %-16s %-7s %-4s %-14s %-9s %-8s %-10s %6s %6s %6s" % (
            r["t"].strftime("%m-%d %H:%M"), r["sym"], r["dir"], r["kind"], r["src"], r["path"], r.get("outcome", "-"),
            ("%+.2f" % r["r"]) if r.get("r") is not None else "-",
            ("%.2f" % r["mae_r"]) if r.get("mae_r") is not None else "-",
            ("%.2f" % r["reach_r"]) if r.get("reach_r") is not None else "-"))
    A("")
    A("WHAT EACH TYPE IS MEANT TO BE")
    for k in KINDS:
        A("  %-14s %s" % (k, KIND_NOTE[k]))
    A("The bot gives the first, second, third and fifth the same Judas Swing label and the same pass at Gate 4.")
    unk = sum(1 for r in rows if r["kind"] == "unknown")
    if unk:
        A("%d signals are unknown: their log lines are gone (the journal keeps 4 to 5 days) or predate the sweep= label." % unk)
    return NL.join(out)


def csv_text(rows):
    cols = ("time", "pair", "dir", "kind", "label_by", "path", "grade", "entry", "sl", "tp1", "outcome", "r", "mae_r", "reach_r")
    out = [",".join(cols)]
    for r in rows:
        vals = [r["t"].strftime("%Y-%m-%d %H:%M:%S"), r["sym"], r["dir"], r["kind"], r["src"], r["path"], r["grade"],
                repr(r["entry"]), repr(r["sl"]), repr(r["tp1"]), r.get("outcome", ""),
                "" if r.get("r") is None else repr(r["r"]), "" if r.get("mae_r") is None else repr(r["mae_r"]),
                "" if r.get("reach_r") is None else repr(r["reach_r"])]
        out.append(",".join(str(v) for v in vals))
    return NL.join(out) + NL


def build_rows(lines, bars_by_sym):
    parsed = parse(lines)
    sigs = episodes(parsed["dispatch"])
    rows = []
    not_sim = set()
    for s in sigs:
        kind, src = classify(s, parsed)
        row = dict(s)
        row.update(kind=kind, src=src, path=entry_path(s, parsed))
        bars = bars_by_sym.get(s["sym"]) if bars_by_sym is not None else None
        if bars is None:
            if bars_by_sym is not None:
                not_sim.add(s["sym"])
        elif len(bars) == 0:
            row["outcome"] = "no data"
        else:
            row.update(simulate(bars, s))
        rows.append(row)
    return rows, not_sim


def bars_from_frame(df):
    out = []
    idx = list(df.index)
    o, h, lo, c = df["Open"].tolist(), df["High"].tolist(), df["Low"].tolist(), df["Close"].tolist()
    for k in range(len(idx)):
        if any(math.isnan(v) for v in (o[k], h[k], lo[k], c[k])):
            continue
        ts = idx[k].to_pydatetime()
        if ts.tzinfo is not None:
            ts = ts.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        out.append((ts, o[k], h[k], lo[k], c[k]))
    out.sort(key=lambda b: b[0])
    clean = []
    for b in out:
        if clean and clean[-1][0] == b[0]:
            continue
        clean.append(b)
    return clean


def window_text(rows):
    if not rows:
        return "no signals found"
    return "%s to %s" % (rows[0]["t"].strftime("%Y-%m-%d %H:%M"), rows[-1]["t"].strftime("%Y-%m-%d %H:%M"))


def selftest():
    bad = []
    n = [0]

    def check(name, cond):
        n[0] += 1
        if not cond:
            bad.append(name)

    T0 = datetime.datetime(2026, 10, 15, 8, 0, 0)

    def L(t, msg):
        return "%s - scanner - INFO - %s" % (t.strftime("%Y-%m-%d %H:%M:%S,000"), msg)

    def disp(t, sym="EURUSD", d="SELL", entry=1.1, sl=1.101, tp1=1.0985, extra=""):
        return L(t, "[signal_dispatch] %s %s grade=B-tier entry=%s sl=%s tp1=%s tp2=1.097 tp3=1.095%s" % (sym, d, entry, sl, tp1, extra))

    def sec(s):
        return datetime.timedelta(seconds=s)

    # ---- parsing
    p = parse([disp(T0), "garbage", L(T0, "[sweep] EURUSD Round number sweep at 1.1"), L(T0, "[sweep] EURUSD Weekly high sweep at 1.12"),
               L(T0, "[sweep] EURUSD liquidity run through 1.10100"), L(T0, "[sweep] EURUSD something else"),
               L(T0, "[asia] USDCHF levels set " + chr(8212) + " high=0.8353099822998047 low=0.8277599811553955"),
               L(T0, "[scanner] USDCHF swept-level SL: swept=0.83531 buffer=0.0005 sl=0.8358 entry=0.834 sl_dist=0.0018 tp1=0.83"),
               L(T0, "[ob_demote] EURUSD SELL OB 1-2 (C-tier) is below the minimum tier; x")])
    check("one dispatch parsed", len(p["dispatch"]) == 1 and p["dispatch"][0]["grade"] == "B-tier")
    check("three sweep lines parsed", [x["kind"] for x in p["sweep"]] == ["round_number", "weekly", "liquidity_run"])
    check("asia levels parsed past the dash", len(p["asia"]) == 1 and abs(p["asia"][0]["high"] - 0.83530998) < 1e-7)
    check("swept level parsed", len(p["swept"]) == 1 and p["swept"][0]["level"] == 0.83531)
    check("demote parsed", len(p["demote"]) == 1)
    check("non-INFO and short lines ignored", parse(["2026-10-15 08:00:00,000 - scanner - WARNING - [sweep] EURUSD Round number sweep at 1", "x"])["sweep"] == [])
    check("dispatch without numbers ignored", parse([L(T0, "[signal_dispatch] EURUSD SELL grade=B-tier entry=abc sl=1 tp1=1")])["dispatch"] == [])
    check("dispatch needs a direction", parse([L(T0, "[signal_dispatch] EURUSD HOLD grade=B-tier entry=1 sl=2 tp1=0.5")])["dispatch"] == [])
    check("token kept when valid", parse([disp(T0, extra=" sweep=weekly")])["dispatch"][0]["token"] == "weekly")
    check("token ignored when invalid", parse([disp(T0, extra=" sweep=banana")])["dispatch"][0]["token"] is None)

    # ---- classification
    def kind_of(extra_lines, extra=""):
        pp = parse([disp(T0, extra=extra)] + extra_lines)
        return classify(pp["dispatch"][0], pp)

    check("token wins over logs", kind_of([L(T0 - sec(10), "[sweep] EURUSD Round number sweep at 1.1")], " sweep=asia_range") == ("asia_range", "token"))
    check("round number inferred", kind_of([L(T0 - sec(30), "[sweep] EURUSD Round number sweep at 1.1")]) == ("round_number", "inferred"))
    check("weekly inferred", kind_of([L(T0 - sec(30), "[sweep] EURUSD Weekly low sweep at 1.1")]) == ("weekly", "inferred"))
    check("liquidity run inferred", kind_of([L(T0 - sec(30), "[sweep] EURUSD liquidity run through 1.1")]) == ("liquidity_run", "inferred"))
    check("stale sweep line not used", kind_of([L(T0 - sec(200), "[sweep] EURUSD Round number sweep at 1.1")]) == ("unknown", "none"))
    check("other symbol sweep line not used", kind_of([L(T0 - sec(10), "[sweep] USDJPY Round number sweep at 1.1")]) == ("unknown", "none"))
    check("a sweep line after the dispatch is not used", kind_of([L(T0 + sec(10), "[sweep] EURUSD Round number sweep at 1.1")]) == ("unknown", "none"))
    sw = L(T0 - sec(20), "[scanner] EURUSD swept-level SL: swept=1.1005 buffer=0.0005 sl=1.101 entry=1.1 sl_dist=0.001 tp1=1.0985")
    check("asia high inferred", kind_of([sw, L(T0 - sec(40), "[asia] EURUSD levels set - high=1.1005000001 low=1.09")]) == ("asia_range", "inferred"))
    check("asia low inferred", kind_of([sw, L(T0 - sec(40), "[asia] EURUSD levels set - high=1.12 low=1.1005")]) == ("asia_range", "inferred"))
    check("recent swing when level is not an asia level", kind_of([sw, L(T0 - sec(40), "[asia] EURUSD levels set - high=1.12 low=1.09")]) == ("recent_swing", "inferred"))
    check("no asia levels: not guessed", kind_of([sw]) == ("unknown", "none"))
    check("asia tolerance: just outside is not asia", kind_of([sw, L(T0 - sec(40), "[asia] EURUSD levels set - high=1.1006 low=1.09")]) == ("recent_swing", "inferred"))
    check("old asia line is not used", kind_of([sw, L(T0 - sec(20000), "[asia] EURUSD levels set - high=1.1005 low=1.09")]) == ("unknown", "none"))
    check("sweep line beats the swept-level comparison", kind_of([sw, L(T0 - sec(40), "[asia] EURUSD levels set - high=1.1005 low=1.09"), L(T0 - sec(10), "[sweep] EURUSD Round number sweep at 1.1")]) == ("round_number", "inferred"))
    pp = parse([disp(T0), L(T0 - sec(60), "[ob_demote] EURUSD SELL OB 1-2 (C-tier) is below the minimum tier; x")])
    check("demoted path", entry_path(pp["dispatch"][0], pp) == "demoted")
    pp = parse([disp(T0), L(T0 - sec(60), "[ob_demote] EURUSD BUY OB 1-2 (C-tier) is below the minimum tier; x")])
    check("other direction is not demoted", entry_path(pp["dispatch"][0], pp) == "normal")
    pp = parse([disp(T0), L(T0 - sec(300), "[ob_demote] EURUSD SELL OB 1-2 (C-tier) is below the minimum tier; x")])
    check("old demote line is not used", entry_path(pp["dispatch"][0], pp) == "normal")

    check("demote line needs a direction", parse([L(T0, "[ob_demote] EURUSD HOLD OB 1-2 (C-tier) is below the minimum tier; x")])["demote"] == [])
    check("swept line 200s old is not used", kind_of([L(T0 - sec(200), "[scanner] EURUSD swept-level SL: swept=1.1005 buffer=0.0005 sl=1.101 entry=1.1 sl_dist=0.001 tp1=1.0985"), L(T0 - sec(210), "[asia] EURUSD levels set - high=1.1005 low=1.09")]) == ("unknown", "none"))
    check("asia tolerance allows a rounding difference", kind_of([sw, L(T0 - sec(40), "[asia] EURUSD levels set - high=1.10051 low=1.09")]) == ("asia_range", "inferred"))
    check("asia line from two hours earlier is used", kind_of([sw, L(T0 - sec(7200), "[asia] EURUSD levels set - high=1.1005 low=1.09")]) == ("asia_range", "inferred"))
    check("the latest sweep line wins", kind_of([L(T0 - sec(50), "[sweep] EURUSD Round number sweep at 1.1"), L(T0 - sec(10), "[sweep] EURUSD Weekly low sweep at 1.1")]) == ("weekly", "inferred"))
    check("the latest sweep line wins, other order", kind_of([L(T0 - sec(10), "[sweep] EURUSD Weekly low sweep at 1.1"), L(T0 - sec(50), "[sweep] EURUSD Round number sweep at 1.1")]) == ("weekly", "inferred"))

    # ---- repeats
    ds = parse([disp(T0), disp(T0 + sec(30)), disp(T0 + sec(1800)), disp(T0 + sec(3600)), disp(T0 + sec(3600 + 3601)),
                disp(T0 + sec(40), entry=1.2, sl=1.201, tp1=1.1985), disp(T0 + sec(50), d="BUY", entry=1.1, sl=1.099, tp1=1.1015)])["dispatch"]
    eps = episodes(ds)
    check("repeats collapse, new entry and other direction stay", len(eps) == 4)
    check("a gap over an hour starts a new signal", sum(1 for e in eps if e["entry"] == 1.1 and e["dir"] == "SELL") == 2)

    # ---- simulation
    def bars(spec, start=None):
        s = start or T0 + MIN * 5
        out = []
        for k, (o, h, lo, c) in enumerate(spec):
            out.append((s + datetime.timedelta(minutes=5 * k), o, h, lo, c))
        return out

    sell = {"t": T0, "sym": "EURUSD", "dir": "SELL", "entry": 100.0, "sl": 101.0, "tp1": 98.5}
    buy = {"t": T0, "sym": "EURUSD", "dir": "BUY", "entry": 100.0, "sl": 99.0, "tp1": 101.5}
    flat = (99.5, 99.6, 99.4, 99.5)
    check("bad geometry", simulate(bars([flat]), dict(sell, sl=99.0))["outcome"] == "bad geometry")
    check("no fill", simulate(bars([flat] * 10), sell)["outcome"] == "no fill")
    r = simulate(bars([flat, (99.9, 100.2, 99.8, 100.0), (100.0, 100.5, 99.9, 100.3), (100.2, 100.4, 98.4, 98.5)]), sell)
    check("sell wins", r["outcome"] == "TP1" and abs(r["r"] - 1.5) < 1e-9)
    check("dip is the deepest move against entry", abs(r["mae_r"] - 0.5) < 1e-9)
    check("reach counts the whole run", abs(r["reach_r"] - 1.6) < 1e-9)
    r = simulate(bars([(99.9, 100.2, 99.8, 100.0), (100.0, 101.2, 99.9, 101.1)]), sell)
    check("sell loses", r["outcome"] == "SL" and r["r"] == -1.0 and r["mae_r"] == 1.0)
    r = simulate(bars([(99.9, 101.5, 99.8, 100.0), (100.0, 100.4, 98.0, 98.2)]), sell)
    check("stop in the fill bar loses", r["outcome"] == "SL" and r["reach_r"] == 0.0)
    r = simulate(bars([(99.9, 100.2, 99.8, 100.0), (100.0, 101.2, 98.0, 99.0)]), sell)
    check("same bar stop and target: stop wins", r["outcome"] == "SL")
    r = simulate(bars([(99.9, 100.2, 99.8, 100.0)] + [(99.5, 99.9, 99.2, 99.5)] * 3), sell)
    check("unresolved", r["outcome"] == "unresolved" and abs(r["reach_r"] - 0.8) < 1e-9)
    r = simulate(bars([(100.1, 100.2, 99.8, 100.0), (100.0, 100.3, 99.7, 100.1), (100.1, 101.6, 100.0, 101.5), (101.5, 101.6, 98.9, 99.0)]), buy)
    check("buy wins", r["outcome"] == "TP1" and abs(r["r"] - 1.5) < 1e-9 and abs(r["mae_r"] - 0.3) < 1e-9)
    r = simulate(bars([(100.1, 100.2, 99.8, 100.0), (100.0, 100.3, 98.9, 99.0)]), buy)
    check("buy loses", r["outcome"] == "SL")
    r = simulate(bars([(99.9, 100.2, 99.8, 100.0), (100.0, 100.1, 98.4, 98.6), (98.6, 101.2, 98.5, 101.1)]), sell)
    check("a win is kept when the stop is hit after the target", r["outcome"] == "TP1" and r["reach_r"] >= 1.5)
    check("reach stops at the stop", abs(r["reach_r"] - 1.6) < 1e-9)
    late = bars([(99.9, 100.2, 99.8, 100.0)], start=T0 + datetime.timedelta(hours=7))
    check("a fill after the window is no fill", simulate(late, sell)["outcome"] == "no fill")
    check("no data after the last bar", simulate(bars([flat]), dict(sell, t=T0 + datetime.timedelta(days=1)))["outcome"] == "no data")
    slow = bars([(99.9, 100.2, 99.8, 100.0)] + [(99.5, 99.9, 99.2, 99.5)] * 200)
    check("hold limit ends the trade", simulate(slow, sell)["outcome"] == "unresolved")
    first_bar_before = bars([(99.9, 100.2, 99.8, 100.0)], start=T0)
    check("latency: a bar starting at the signal time is not used", simulate(first_bar_before, sell)["outcome"] == "no data")

    r = simulate(bars([(99.9, 100.0, 99.8, 99.95), (100.0, 100.0, 98.5, 98.6)]), sell)
    check("an exact touch fills and an exact touch of the target wins", r["outcome"] == "TP1")
    r = simulate(bars([(99.9, 100.2, 99.8, 100.0), (100.0, 100.1, 98.4, 98.6), (98.6, 100.9, 98.5, 100.8)]), sell)
    check("the dip stops growing once the target is hit", r["outcome"] == "TP1" and abs(r["mae_r"] - 0.2) < 1e-9)
    hold_ok = bars([(99.9, 100.2, 99.8, 100.0)] + [(99.5, 99.9, 99.2, 99.5)] * 130 + [(99.0, 99.2, 98.4, 98.5)])
    check("a target inside 12 hours counts", simulate(hold_ok, sell)["outcome"] == "TP1")
    hold_late = bars([(99.9, 100.2, 99.8, 100.0)] + [(99.5, 99.9, 99.2, 99.5)] * 160 + [(99.0, 99.2, 98.4, 98.5)])
    check("a target after 12 hours does not count", simulate(hold_late, sell)["outcome"] == "unresolved")
    row = {"outcome": "TP1", "r1": 1.5, "r": 1.5, "mae_r": 0.2, "reach_r": 1.5}
    check("reach counts an exact hit", group_stats([row])["reach_1.5"] == 1.0 and group_stats([row])["reach_2.0"] == 0.0)

    # ---- statistics
    lo, hi = wilson(5, 10)
    check("wilson 5/10", abs(lo - 0.2366) < 0.001 and abs(hi - 0.7634) < 0.001)
    check("wilson 0/0", wilson(0, 0) == (0.0, 1.0))
    check("wilson 10/10 top is 1", abs(wilson(10, 10)[1] - 1.0) < 1e-9 and wilson(10, 10)[0] > 0.7)

    def mk(outs, r1=1.5):
        rows = []
        for o in outs:
            rows.append({"outcome": o, "r1": r1, "r": r1 if o == "TP1" else -1.0 if o == "SL" else None,
                         "mae_r": 0.4 if o == "TP1" else 1.0, "reach_r": 1.6 if o == "TP1" else 0.2})
        return rows

    st = group_stats(mk(["TP1"] * 2 + ["SL"] * 3))
    check("small group is too few", st["verdict"] == "too few to judge" and st["resolved"] == 5 and abs(st["be"] - 0.4) < 1e-9)
    check("expectancy", abs(st["exp"] - (2 * 1.5 - 3) / 5) < 1e-9)
    st = group_stats(mk(["TP1"] * 40 + ["SL"] * 10))
    check("clear winner", st["verdict"] == "above break-even")
    st = group_stats(mk(["TP1"] * 3 + ["SL"] * 40))
    check("clear loser", st["verdict"] == "below break-even")
    st = group_stats(mk(["TP1"] * 4 + ["SL"] * 6))
    check("in between is unclear", st["verdict"] == "unclear")
    st = group_stats(mk(["TP1", "SL", "unresolved", "no fill"]))
    check("unresolved and no fill are not counted as done", st["resolved"] == 2 and st["filled"] == 3 and st["simulated"] == 4)
    check("reach share", abs(st["reach_1.5"] - 1 / 3) < 1e-9)
    check("median dip of winners", abs(st["mae_med"] - 0.4) < 1e-9)
    check("break-even moves with the target", abs(group_stats(mk(["TP1"] * 5 + ["SL"] * 5, r1=2.0))["be"] - 1 / 3) < 1e-9)
    empty = group_stats([])
    check("empty group", empty["verdict"] == "too few to judge" and empty["signals"] == 0)

    # ---- end to end
    log = [L(T0 - sec(20), "[sweep] EURUSD Round number sweep at 1.1"), disp(T0, entry=100.0, sl=101.0, tp1=98.5),
           disp(T0 + sec(7200), sym="XAUUSD", entry=4500.0, sl=4530.0, tp1=4440.0, extra=" sweep=asia_range")]
    rows, ns = build_rows(log, {"EURUSD": bars([(99.9, 100.2, 99.8, 100.0), (100.0, 100.1, 98.4, 98.6)])})
    check("two rows", len(rows) == 2)
    check("eurusd simulated", rows[0]["outcome"] == "TP1" and rows[0]["kind"] == "round_number")
    check("gold not simulated", "outcome" not in rows[1] and ns == {"XAUUSD"} and rows[1]["kind"] == "asia_range" and rows[1]["src"] == "token")
    txt = report(rows, ns, window_text(rows))
    check("report mentions the pieces", "BY SWEEP TYPE" in txt and "BY PAIR AND SWEEP TYPE" in txt and "EACH SIGNAL" in txt and "XAUUSD" in txt and "WHAT EACH TYPE IS MEANT TO BE" in txt)
    check("report is ASCII", all(ord(ch) < 128 for ch in txt))
    check("empty report works", "0 signals" in report([], set(), window_text([])))
    c = csv_text(rows).split(NL)
    check("csv has a header and two rows", len(c) == 4 and c[0].startswith("time,pair,dir,kind") and c[3] == "")
    rows2, ns2 = build_rows(log, None)
    check("no prices: no outcomes and nothing listed as not simulated", all("outcome" not in r for r in rows2) and ns2 == set())
    rows3, ns3 = build_rows(log, {"EURUSD": []})
    check("empty bars: no data", rows3[0]["outcome"] == "no data")
    check("order by time", [r["t"] for r in rows] == sorted(r["t"] for r in rows))

    print("SELFTEST: %d passed, %d failed" % (n[0] - len(bad), len(bad)))
    for b in bad:
        print("  FAILED:", b)
    return not bad


def main(argv, stdin=None):
    if "--selftest" in argv:
        sys.exit(0 if selftest() else 1)
    if not selftest():
        print("Self-test failed, not running the audit.")
        sys.exit(1)
    archives = [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--archive"]
    csv_path = next((argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--csv"), None)
    lines = []
    for a in archives:
        with open(a, encoding="utf-8", errors="replace") as fh:
            lines.extend(fh.read().splitlines())
    lines.extend((stdin if stdin is not None else sys.stdin).read().splitlines())
    lines = list(dict.fromkeys(lines))
    if "--no-prices" in argv:
        bars = None
    else:
        parsed = parse(lines)
        syms = sorted({s["sym"] for s in parsed["dispatch"] if s["sym"] in SIM_TICKER})
        bars = {}
        if syms:
            import yfinance as yf
            for s in syms:
                df = yf.Ticker(SIM_TICKER[s]).history(period="59d", interval="5m")
                bars[s] = bars_from_frame(df) if df is not None and len(df) else []
    rows, not_sim = build_rows(lines, bars)
    print(report(rows, not_sim, window_text(rows)))
    if csv_path:
        with open(csv_path, "w", encoding="utf-8") as fh:
            fh.write(csv_text(rows))
        print(NL + "Per-signal rows written to " + csv_path)


if __name__ == "__main__":
    main(sys.argv[1:])
