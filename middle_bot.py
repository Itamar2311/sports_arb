"""
Middle paper trading across Ontario sportsbooks (Pinnacle, FanDuel, LeoVegas, BetRivers).
No real money and no real bets - it only reads public odds (books.py, shared with sports_arb).

A middle: an Over at a low line at one book + an Under at a higher line at another
(or home -2.5 at one book + away +4.5 at another). If the result lands between the
lines, both bets win; otherwise one wins and roughly pays back the stake.

    Curry over 19.5 @ 1.91 (FanDuel) + under 21.5 @ 1.91 (LeoVegas), $50 / $50
    scores 19 or less -> only the under wins   -> -$4.50
    scores 20 or 21   -> both win              -> +$91.00
    scores 22 or more -> only the over wins    -> -$4.50

Every scan:
1. scanner.py collects every bet at every book and matches the same game across books.
2. For every family of bets (same period, team/player, stat - e.g. "Curry points, full game")
   take the best price for each side at each line, and pair an over/home side at line L1 with
   an under/away side further along, so both can win.
3. Stakes: $STAKE split so either single win pays the same, rounded to whole dollars.
       worst = profit if only one bet wins      best = profit if both win
   Kept only if worst >= -MAX_RISK (and worst <= MAX_WORST: bigger means two different bets
   were matched by mistake).
4. Chance of landing in the middle, from the books' own prices without their margin (Pinnacle
   when it has both lines, otherwise the other books' average):
       P(middle) = P(over L1) - P(over L2)
       EV = P(under only) * payout + P(middle) * both payouts + P(over only) * payout - stake
5. One trade per family per game - the pair with the best EV. Re-checked at the books before
   it is "placed", like arb_bot.
Half-point lines only (whole lines can push, which this math doesn't cover).
The dashboard also lists the closest candidates of the latest scan - the best middle per
family by expected profit, placed or not - to show how near the books come.

Tracking: before kickoff each leg is re-checked every scan (odds cut / bet pulled / game
removed or postponed), like the arb bot. After the game the trade waits for the result:
settling by the final score is not built yet, so profit is shown as worst / expected / best.

Files: middle_trades.json (source of truth), middle_trades.csv, middle_dashboard.html, middle_log.txt
Run through scanner.py, which fetches the books once per scan for this bot and arb_bot:
       python scanner.py           scan forever        python scanner.py --once    one scan
"""
import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import product
from math import ceil, floor
from statistics import mean

import books
from arb_bot import match_games, notify, now_utc, oriented, stamp   # shared with the arb bot

# --- settings -------------------------------------------------------------
STAKE = 100
MAX_RISK = 10.0             # worst case (only one bet wins) may lose at most this many dollars ...
MIN_EV = 0.5                # ... and only if the expected profit is at least this
MAX_RISK_NO_EV = 1.0        # when the chance of the middle can't be estimated: lose at most this
SHOW_CANDIDATES = 25        # dashboard also lists the best middles that didn't qualify ...
MIN_LEG_ODDS = 1.25         # ... skipping lopsided pairs (a 1.01 favourite + a long shot isn't a middle)
MAX_WORST = 8.0             # worst case better than +8% = almost surely two different bets matched
CUT_FLAG_PTS = 0.08
POSTPONE_HOURS = 12
GAME_HOURS = {"soccer": 2.5, "basketball": 3, "hockey": 3.5, "football": 4, "baseball": 4,
              "tennis": 5, "cricket": 9}
NOTIFY = True
NOTIFY_SUMMARY_OVER = 5     # more new middles than this in one scan -> one summary pop-up

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "middle_trades.json")
CSV_FILE = os.path.join(HERE, "middle_trades.csv")
DASHBOARD = os.path.join(HERE, "middle_dashboard.html")
LOG = os.path.join(HERE, "middle_log.txt")
CSV_FIELDS = ["id", "status", "opened", "sport", "game", "start", "market", "legs", "middle",
              "p_middle_pct", "stake", "worst", "expected", "best", "flags", "closed", "close_reason"]

PERIOD_NAMES = {"game": "", "reg": "regulation", "h1": "1st half", "h2": "2nd half", "f5": "first 5 innings",
                "f3": "first 3 innings", "f7": "first 7 innings"}
STAT_NAMES = {"main": "", "games": "games", "corners": "corners", "cards": "cards", "sets": "sets",
              "pra": "pts+reb+ast", "hrr": "hits+runs+RBIs", "sb": "stolen bases", "hr": "home runs",
              "tb": "total bases", "rbi": "RBIs", "k": "strikeouts", "outs": "outs", "hits": "hits",
              "runs": "runs", "threes": "threes", "rebounds": "rebounds", "assists": "assists",
              "points": "points", "sog": "shots on goal", "sot": "shots on target", "shots": "shots",
              "saves": "saves", "goals": "goals"}












# --- middle math ----------------------------------------------------------
# A middle pairs a "low" side and a "high" side of one family:
#   ou :  over at line L1  +  under at line L2 > L1   -> both win if L1 < result < L2
#   hcp:  home at line Lh  +  away at home-line La < Lh -> both win if -Lh < margin < -La
# In both cases P(low side wins) = p1, P(high side's opposite wins) = p2, P(middle) = p1 - p2.
LOW_SIDE = {"ou": "over", "hcp": "home"}
HIGH_SIDE = {"ou": "under", "hcp": "away"}


def half_line(line):
    return round(abs(line) % 1, 2) == 0.5


def whole_dollar_split(o_low, o_high, total=STAKE):
    """Whole-dollar stakes adding up to `total` with the best worst case."""
    s = 1 / o_low + 1 / o_high
    ideal = [total * (1 / o_low) / s, total * (1 / o_high) / s]
    best = None
    for a, b in product(*[range(max(1, floor(x) - 1), ceil(x) + 2) for x in ideal]):
        score = (-abs(a + b - total), round(min(a * o_low, b * o_high) - (a + b), 2))
        if best is None or score > best[0]:
            best = (score, a, b)
    return best[1], best[2]


def fair_prob(offers_at_line, side):
    """No-margin chance that `side` wins at one line: Pinnacle if it has both sides, else the average."""
    by_book = defaultdict(dict)
    for o in offers_at_line:
        by_book[o.book][o.outcome] = o.odds
    probs = {}
    for book, sides in by_book.items():
        if len(sides) == 2:
            inv = {k: 1 / v for k, v in sides.items()}
            probs[book] = inv[side] / sum(inv.values())
    if not probs:
        return None
    return probs.get("Pinnacle", mean(probs.values()))


def evaluate(low, high, p1, p2, strict=True):
    """Middle numbers, or None if it doesn't qualify (strict=False: only the sanity checks)."""
    s_low, s_high = whole_dollar_split(low.odds, high.odds)
    staked = s_low + s_high
    pay_low, pay_high = s_low * low.odds, s_high * high.odds
    worst = min(pay_low, pay_high) - staked
    best = pay_low + pay_high - staked
    if worst > STAKE * MAX_WORST / 100:
        return None
    if not strict:
        if min(low.odds, high.odds) < MIN_LEG_ODDS:
            return None
    elif worst < -MAX_RISK:
        return None
    n = {"s_low": s_low, "s_high": s_high, "staked": staked, "worst": worst, "best": best,
         "p_middle": None, "expected": None}
    if p1 is not None and p2 is not None and p1 > p2:
        pm = p1 - p2
        n["p_middle"] = pm
        n["expected"] = (1 - p1) * pay_high + pm * (pay_low + pay_high) + p2 * pay_low - staked
        return n if n["expected"] >= MIN_EV or not strict else None
    return n if worst >= -MAX_RISK_NO_EV or not strict else None


def find_middles(games, offers_by_event, strict=True):
    """Best middle per family per matched game -> list of (game, family, low, high, numbers)."""
    found = []
    for g in games:
        families = defaultdict(list)
        for book, (e, flipped) in g["books"].items():
            for o in oriented(offers_by_event.get((book, e.id), []), flipped):
                if o.kind in LOW_SIDE and half_line(o.line):
                    families[(o.period, o.scope, o.stat, o.kind)].append(o)
        for fam, offers in families.items():
            kind = fam[3]
            at_line = defaultdict(list)
            for o in offers:
                at_line[o.line].append(o)
            best_low, best_high = {}, {}
            for line, os_ in at_line.items():
                lows = [o for o in os_ if o.outcome == LOW_SIDE[kind]]
                highs = [o for o in os_ if o.outcome == HIGH_SIDE[kind]]
                if lows:
                    best_low[line] = max(lows, key=lambda o: o.odds)
                if highs:
                    best_high[line] = max(highs, key=lambda o: o.odds)
            top = None
            for l1, low in best_low.items():
                for l2, high in best_high.items():
                    # over L1 + under L2 needs L2 > L1; home Lh + away La needs La < Lh
                    if (kind == "ou" and l2 <= l1) or (kind == "hcp" and l2 >= l1) or low.book == high.book:
                        continue
                    p1 = fair_prob(at_line[l1], LOW_SIDE[kind])
                    p2 = fair_prob(at_line[l2], LOW_SIDE[kind])
                    n = evaluate(low, high, p1, p2, strict)
                    if not n:
                        continue
                    rank = (n["expected"] is not None, n["expected"] if n["expected"] is not None else n["worst"])
                    if top is None or rank > top[0]:
                        top = (rank, low, high, n)
            if top:
                found.append((g, fam, top[1], top[2], top[3]))
    return found


# --- labels ---------------------------------------------------------------
def family_label(fam, home, away):
    period, scope, stat, kind = fam
    parts = []
    if scope.startswith("player:"):
        parts.append(scope[7:].title())
    elif scope in ("home", "away"):
        parts.append(home if scope == "home" else away)
    parts.append("total" if kind == "ou" else "spread")
    if STAT_NAMES.get(stat):
        parts.append(STAT_NAMES[stat])
    if PERIOD_NAMES.get(period, period):
        parts.append(f"({PERIOD_NAMES.get(period, period)})")
    return " ".join(parts)


def side_label(kind, outcome, line, home, away):
    if kind == "ou":
        return f"{outcome.title()} {line:g}"
    return f"{home} {line:+g}" if outcome == "home" else f"{away} {-line:+g}"


def middle_label(kind, l1, l2, home):
    if kind == "ou":
        lo, hi = int(l1 + 0.5), int(l2 - 0.5)
        return f"result {lo}" if lo == hi else f"result {lo}-{hi}"
    lo, hi = int(-l1 + 0.5), int(-l2 - 0.5)     # home margin strictly between -Lh and -La
    rng = f"{lo}" if lo == hi else f"{lo} to {hi}"
    return f"{home} margin {rng}"


# --- paper trades ---------------------------------------------------------
def load():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save(trades):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=1)
    os.replace(tmp, STATE)


def trade_key(g, fam):
    ref = g["ref"]
    return f"{ref.sport}|{books.norm_team(ref.home)}|{books.norm_team(ref.away)}|{ref.start:%Y-%m-%d}|{fam}"


def confirm(g, fam, low, high, by_book, probs):
    """Re-fetch both legs; return (low, high, numbers) if the middle is still there."""
    fresh = {}
    for leg in (low, high):
        e, flipped = g["books"][leg.book]
        offers, _ = by_book[leg.book].refetch(e.id, leg.ref)
        same = [o for o in oriented(offers, flipped)
                if (o.period, o.scope, o.stat, o.kind) == fam and o.line == leg.line and o.outcome == leg.outcome]
        if not same:
            return None
        fresh[leg.outcome] = max(same, key=lambda o: o.odds)
    new_low, new_high = fresh[low.outcome], fresh[high.outcome]
    n = evaluate(new_low, new_high, *probs)
    return (new_low, new_high, n) if n else None


def open_trade(g, fam, low, high, n, trades):
    ref = g["ref"]
    kind = fam[3]
    legs = []
    for leg, stake in ((low, n["s_low"]), (high, n["s_high"])):
        e, flipped = g["books"][leg.book]
        legs.append({"book": leg.book, "event_id": leg.event_id, "flipped": flipped, "outcome": leg.outcome,
                     "line": leg.line, "label": side_label(kind, leg.outcome, leg.line, ref.home, ref.away),
                     "ref": leg.ref, "odds": leg.odds, "prob": round(1 / leg.odds, 4), "stake": stake,
                     "payout": round(stake * leg.odds, 2), "now_odds": leg.odds, "status": "ok"})
    t = {"id": len(trades) + 1, "key": trade_key(g, fam), "family": list(fam), "status": "open",
         "opened": stamp(), "sport": ref.sport, "game": f"{ref.home} vs {ref.away}",
         "home": ref.home, "away": ref.away, "start": ref.start.isoformat(),
         "market": family_label(fam, ref.home, ref.away),
         "middle": middle_label(kind, low.line, high.line, ref.home), "legs": legs,
         "p_middle_pct": None if n["p_middle"] is None else round(n["p_middle"] * 100, 1),
         "stake": n["staked"], "worst": round(n["worst"], 2), "best": round(n["best"], 2),
         "expected": None if n["expected"] is None else round(n["expected"], 2),
         "flags": [], "closed": "", "close_reason": "", "missing_runs": 0}
    trades.append(t)
    return t




def describe(t):
    ev = "" if t["expected"] is None else f", EV {t['expected']:+.2f}"
    return (f"{t['game']} - {t['market']}: middle {t['middle']} "
            f"({t['p_middle_pct'] if t['p_middle_pct'] is not None else '?'}%{ev})\n" +
            "\n".join(f"${l['stake']} {l['label']} @ {l['odds']:g} ({l['book']})" for l in t["legs"]))


def flag(t, text):
    if text not in t["flags"]:
        t["flags"].append(text)
        notify(f"Middle at risk: {t['game']}", f"{t['market']}\n{text}")


def track(trades, events, offers_by_event):
    """Follow open trades until kickoff (cancellation signs); after the game, await the result."""
    listed = {(e.book, e.id): e for e in events}
    now = now_utc()
    for t in trades:
        if t["status"] not in ("open", "started"):
            continue
        start = datetime.fromisoformat(t["start"])
        fam = tuple(t["family"])
        if t["status"] == "open" and now < start:
            gone_everywhere = True
            for l in t["legs"]:
                e = listed.get((l["book"], l["event_id"]))
                if e is None:
                    if l["status"] == "ok":
                        l["status"] = "game removed"
                    continue
                gone_everywhere = False
                if abs(e.start - start) > timedelta(hours=POSTPONE_HOURS):
                    t["status"], t["closed"], t["close_reason"] = "void", stamp(), "kickoff moved (postponed)"
                    notify(f"Middle void: {t['game']}", "Kickoff moved - bets void, stakes back")
                    break
                cur = [o for o in oriented(offers_by_event.get((l["book"], l["event_id"]), []), l["flipped"])
                       if (o.period, o.scope, o.stat, o.kind) == fam and o.line == l["line"] and o.outcome == l["outcome"]]
                if cur:
                    l["now_odds"], l["missing"] = cur[0].odds, 0
                    if 1 / cur[0].odds - l["prob"] >= CUT_FLAG_PTS and l["status"] == "ok":
                        l["status"] = "odds cut"
                        flag(t, f"{l['book']} cut {l['label']} from {l['odds']:g} to {cur[0].odds:g} "
                                f"- looks like a pricing error the book may void")
                elif offers_by_event.get((l["book"], l["event_id"])):
                    l["now_odds"] = None
                    l["missing"] = l.get("missing", 0) + 1
                    if l["missing"] >= 2 and l["status"] == "ok":
                        l["status"] = "pulled"
                        flag(t, f"{l['book']} no longer offers {l['label']} - line moved or pulled")
            if t["status"] == "void":
                continue
            if gone_everywhere:
                t["missing_runs"] += 1
                if t["missing_runs"] >= 3:
                    t["status"], t["closed"], t["close_reason"] = "void", stamp(), "game removed by every book"
                    notify(f"Middle void: {t['game']}", "Game removed everywhere (postponed/cancelled)")
                continue
            t["missing_runs"] = 0
        elif t["status"] == "open":
            t["status"] = "started"
        if t["status"] == "started" and now >= start + timedelta(hours=GAME_HOURS.get(t["sport"], 4)):
            t["status"], t["closed"], t["close_reason"] = "awaiting result", stamp(), "game over - result not settled yet"


# --- output ---------------------------------------------------------------
def write_csv(trades):
    rows = []
    for t in trades:
        r = {k: t.get(k, "") for k in CSV_FIELDS}
        r["legs"] = " | ".join(f"${l['stake']} {l['label']} @ {l['odds']:g} {l['book']} ({l['status']})" for l in t["legs"])
        r["flags"] = " | ".join(t["flags"])
        rows.append(r)
    tmp = CSV_FILE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    try:
        os.replace(tmp, CSV_FILE)
    except PermissionError:
        os.remove(tmp)


def candidate_row(g, fam, low, high, n):
    ref = g["ref"]
    kind = fam[3]
    return {"sport": ref.sport, "game": f"{ref.home} vs {ref.away}", "start": ref.start.isoformat(),
            "market": family_label(fam, ref.home, ref.away),
            "middle": middle_label(kind, low.line, high.line, ref.home),
            "legs": [{"stake": st, "label": side_label(kind, x.outcome, x.line, ref.home, ref.away),
                      "odds": x.odds, "book": x.book, "prob": 1 / x.odds}
                     for x, st in ((low, n["s_low"]), (high, n["s_high"]))],
            "p_middle_pct": n["p_middle"] * 100, "worst": n["worst"], "expected": n["expected"], "best": n["best"]}


def write_dashboard(trades, last_scan, near=()):
    from html import escape
    live = [t for t in trades if t["status"] != "void"]
    void = [t for t in trades if t["status"] == "void"]
    staked = sum(t["stake"] for t in live)
    with_ev = [t for t in live if t["expected"] is not None]
    exp = sum(t["expected"] for t in with_ev)
    worst = sum(t["worst"] for t in live)
    free = sum(t["worst"] >= 0 for t in live)
    avg_pm = mean(t["p_middle_pct"] for t in with_ev) if with_ev else 0
    flagged = sum(bool(t["flags"]) for t in live)

    def money(x):
        if x is None:
            return '<span class="muted">?</span>'
        return f'<span class="{"pos" if x > 0 else "neg" if x < 0 else ""}">{x:+.2f}</span>'

    def leg_html(l):
        cls = "" if l["status"] == "ok" else ' class="warn"'
        now = "" if l.get("now_odds") in (None, l["odds"]) else f" → {l['now_odds']:g}"
        st = "" if l["status"] == "ok" else f" · {escape(l['status'])}"
        return (f"<div{cls}><b>${l['stake']}</b> {escape(l['label'])} @ {l['odds']:g}{now} "
                f"<span class='muted'>{escape(l['book'])} · {l['prob'] * 100:.1f}%{st}</span></div>")

    def chance(t):
        return "?" if t["p_middle_pct"] is None else f"{t['p_middle_pct']:.1f}%"

    order = {"open": 0, "started": 1, "awaiting result": 2, "void": 3}
    near_rows = "".join(
        f"<tr><td>{escape(c['sport'])}</td><td>{escape(c['game'])}<div class='muted'>"
        f"{datetime.fromisoformat(c['start']).astimezone():%a %d %b %H:%M}</div></td>"
        f"<td>{escape(c['market'])}</td><td>" +
        "".join(f"<div><b>${l['stake']}</b> {escape(l['label'])} @ {l['odds']:g} "
                f"<span class='muted'>{escape(l['book'])} · {l['prob'] * 100:.1f}%</span></div>" for l in c["legs"]) +
        f"</td><td>{escape(c['middle'])}</td><td>{c['p_middle_pct']:.1f}%</td><td>{money(c['worst'])}</td>"
        f"<td>{money(c['expected'])}</td><td>{money(c['best'])}</td></tr>" for c in near) \
        or "<tr><td colspan='9' class='muted'>none</td></tr>"
    rows = "".join(
        f"<tr><td><span class='st'>{escape(t['status'])}</span></td><td>{escape(t['opened'])}</td>"
        f"<td>{escape(t['sport'])}</td><td>{escape(t['game'])}<div class='muted'>"
        f"{datetime.fromisoformat(t['start']).astimezone():%a %d %b %H:%M}</div></td>"
        f"<td>{escape(t['market'])}</td><td>{''.join(leg_html(l) for l in t['legs'])}</td>"
        f"<td>{escape(t['middle'])}</td>"
        f"<td>{chance(t)}</td>"
        f"<td>${t['stake']}</td><td>{money(t['worst'])}</td><td>{money(t['expected'])}</td><td>{money(t['best'])}</td>"
        f"<td class='flags'>{'<br>'.join(escape(f) for f in t['flags'])}"
        f"{'<br>' if t['flags'] and t['close_reason'] else ''}{escape(t['close_reason'])}</td></tr>"
        for t in sorted(trades, key=lambda t: (order.get(t["status"], 9), -(t["expected"] or -99), -t["id"])))
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Middle paper trades</title>
<style>
:root {{ --bg:#fff; --fg:#111; --muted:#666; --line:#e4e4e4; --pos:#0a7d32; --neg:#c62828; --warn:#b26a00; --chip:#f1f1f1; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161616; --fg:#eee; --muted:#999;
  --line:#333; --pos:#4cc777; --neg:#ff6b6b; --warn:#f0a840; --chip:#2a2a2a; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px system-ui,sans-serif; margin:24px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
.sub, .muted {{ color:var(--muted); }} .sub {{ margin-bottom:16px; }}
.tiles {{ display:flex; gap:28px; flex-wrap:wrap; }} .tile b {{ display:block; font-size:22px; }}
table {{ border-collapse:collapse; width:100%; }} th, td {{ padding:6px 8px; vertical-align:top;
  border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
th {{ color:var(--muted); font-weight:500; }} td.flags {{ white-space:normal; min-width:200px; color:var(--warn); }}
.pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }} .warn {{ color:var(--warn); }}
.st {{ background:var(--chip); border-radius:4px; padding:1px 6px; }}
.wrap {{ overflow-x:auto; }}
</style></head><body>
<h1>Middle paper trades</h1>
<div class="sub">${STAKE} imaginary per middle, whole-dollar stakes · Pinnacle, FanDuel, LeoVegas, BetRivers (Ontario)
 · worst case = only one bet wins, best = both win · last scan {escape(last_scan)} · reloads every minute</div>
<div class="tiles">
 <div class="tile">Middles<b>{len(live)}</b></div>
 <div class="tile">Staked<b>${staked:,.0f}</b></div>
 <div class="tile">Expected profit<b>{money(exp)} $</b></div>
 <div class="tile">Worst case (no middle hits)<b>{money(worst)} $</b></div>
 <div class="tile">Avg. chance of middle<b>{avg_pm:.1f}%</b></div>
 <div class="tile">Free middles (can't lose)<b>{free}</b></div>
 <div class="tile">Flagged / void<b>{flagged} / {len(void)}</b></div>
</div>
<h2>Trades</h2><div class="wrap"><table>
<tr><th>Status</th><th>Placed</th><th>Sport</th><th>Game</th><th>Bet</th><th>Legs (stake, pick, odds, book, probability)</th>
<th>Middle if</th><th>Chance</th><th>Stake</th><th>Worst $</th><th>Expected $</th><th>Best $</th><th>Flags</th></tr>
{rows}</table></div>
<h2>Closest candidates this scan (not placed)</h2>
<div class="sub">The best middle per bet family at every book right now, by expected profit - placed only if
 expected profit is at least ${MIN_EV:.2f} and the worst case loses at most ${MAX_RISK:.0f}.
 Pairs with a leg under {MIN_LEG_ODDS:g} odds are left out.</div>
<div class="wrap"><table>
<tr><th>Sport</th><th>Game</th><th>Bet</th><th>Legs (stake, pick, odds, book, probability)</th>
<th>Middle if</th><th>Chance</th><th>Worst $</th><th>Expected $</th><th>Best $</th></tr>
{near_rows}</table></div></body></html>"""
    tmp = DASHBOARD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp, DASHBOARD)


def log(msg):
    print(msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


# --- one scan --------------------------------------------------------------
def configure(out_dir):
    """Write this bot's files into out_dir (scanner.py points it at the middle_trades folder)."""
    global STATE, CSV_FILE, DASHBOARD, LOG
    STATE, CSV_FILE, DASHBOARD, LOG = (os.path.join(out_dir, f) for f in
                                       ("middle_trades.json", "middle_trades.csv", "middle_dashboard.html",
                                        "middle_log.txt"))


def process(data, trades):
    """One scan's data (from scanner.collect) -> new paper trades, tracking, files. Returns a summary."""
    middles = find_middles(data.games, data.by_event)
    near = [m for m in find_middles(data.games, data.by_event, strict=False) if m[4]["expected"] is not None]
    near = [candidate_row(*m) for m in sorted(near, key=lambda m: -m[4]["expected"])[:SHOW_CANDIDATES]]
    taken = {t["key"] for t in trades}
    new = []
    for g, fam, low, high, n in sorted(middles, key=lambda m: -(m[4]["expected"] or -99)):
        tk = trade_key(g, fam)
        if tk in taken:
            continue
        taken.add(tk)
        try:
            # fair probabilities from this scan; they don't move in the seconds the re-check takes
            ok = confirm(g, fam, low, high, data.by_book, probs_for(g, fam, low, high, data.by_event))
        except Exception as ex:
            data.errors.append(f"middle confirm: {ex}")
            continue
        if ok:
            new.append(open_trade(g, fam, ok[0], ok[1], ok[2], trades))
    if len(new) > NOTIFY_SUMMARY_OVER:
        best = max(new, key=lambda t: t["expected"] or -99)
        notify(f"{len(new)} new middles placed", "Best: " + describe(best))
    else:
        for t in new:
            notify(f"Middle placed: {t['middle']} ({t['p_middle_pct'] or '?'}%)", describe(t))
    track(trades, data.events, data.by_event)
    save(trades)
    write_csv(trades)
    write_dashboard(trades, stamp(), near)
    return f"{len(middles)} middles seen, {len(new)} placed"


def probs_for(g, fam, low, high, by_event):
    """Fair P(low side wins) at both legs' lines, from this scan's prices at every book."""
    kind = fam[3]
    at = defaultdict(list)
    for book, (e, flipped) in g["books"].items():
        for o in oriented(by_event.get((book, e.id), []), flipped):
            if (o.period, o.scope, o.stat, o.kind) == fam and o.line in (low.line, high.line):
                at[o.line].append(o)
    return fair_prob(at[low.line], LOW_SIDE[kind]), fair_prob(at[high.line], LOW_SIDE[kind])
