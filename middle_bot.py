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

Two tiers of paper trades:
    qualified   EV >= MIN_EV and worst >= -MAX_RISK, re-checked at the books, notified
    candidate   the best middles that just miss (EV >= CANDIDATE_MIN_EV, legs' 1/odds adding up to at
                most CANDIDATE_MAX_IMPLIED = 103%, i.e. a worst case of about -$3),
                CANDIDATES_PER_SCAN per scan, no re-check and no pop-up - placed to collect data
                for a model of which middles pay off. Each trade keeps its inputs in "features".

Tracking: before kickoff each leg is re-checked every scan (odds cut / bet pulled / game
removed or postponed), like the arb bot. After the game it is settled by the final score from
ESPN (results.py): each leg wins, loses or is void (player didn't play -> stake back). Games
ESPN doesn't cover (tennis, darts, esports, small leagues...) become "needs result" after
NO_RESULT_HOURS; their result can be looked up by hand and put in manual_results.json:
    {"12": 7, "15": "void"}     trade id -> the number the bet settles on (total, home margin
                                for a spread, or player stat), or "void"
and the next scan settles them.

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
import results
from arb_bot import match_games, notify, now_utc, oriented, stamp   # shared with the arb bot

# --- settings -------------------------------------------------------------
STAKE = 100
MAX_RISK = 10.0             # worst case (only one bet wins) may lose at most this many dollars ...
MIN_EV = 0.5                # ... and only if the expected profit is at least this
MAX_RISK_NO_EV = 1.0        # when the chance of the middle can't be estimated: lose at most this
CANDIDATES_PER_SCAN = 25    # also paper-trade the best middles that just miss, for data ...
CANDIDATE_MIN_EV = -2.0     # ... expected profit at least this
CANDIDATE_MAX_IMPLIED = 1.03  # ... legs' implied probabilities (1/odds) add up to at most 103%
MIN_LEG_ODDS = 1.25         # ... and no lopsided pairs (a 1.01 favourite + a long shot isn't a middle)
NO_RESULT_HOURS = 24        # no ESPN result this long after kickoff -> "needs result" (manual_results.json)
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
MANUAL = os.path.join(HERE, "manual_results.json")
CSV_FIELDS = ["id", "tier", "status", "opened", "sport", "game", "start", "market", "legs", "middle",
              "p_middle_pct", "stake", "worst", "expected", "best", "result", "hit", "profit",
              "flags", "closed", "close_reason", "features"]

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
         "p_middle": None, "expected": None, "p1": p1, "p2": p2}
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


def open_trade(g, fam, low, high, n, trades, tier="qualified"):
    ref = g["ref"]
    kind = fam[3]
    legs = []
    for leg, stake in ((low, n["s_low"]), (high, n["s_high"])):
        e, flipped = g["books"][leg.book]
        legs.append({"book": leg.book, "event_id": leg.event_id, "flipped": flipped, "outcome": leg.outcome,
                     "line": leg.line, "label": side_label(kind, leg.outcome, leg.line, ref.home, ref.away),
                     "ref": leg.ref, "odds": leg.odds, "prob": round(1 / leg.odds, 4), "stake": stake,
                     "payout": round(stake * leg.odds, 2), "now_odds": leg.odds, "status": "ok"})
    features = {"hours_to_start": round((ref.start - now_utc()).total_seconds() / 3600, 2),
                "line_gap": abs(high.line - low.line), "low_odds": low.odds, "high_odds": high.odds,
                "low_book": low.book, "high_book": high.book, "p_low_fair": n["p1"], "p_high_fair": n["p2"],
                "books_on_game": len(g["books"]), "period": fam[0],
                "scope": "player" if fam[1].startswith("player:") else fam[1], "stat": fam[2], "kind": fam[3]}
    key = trade_key(g, fam) if tier == "qualified" else f"candidate|{trade_key(g, fam)}"
    t = {"id": len(trades) + 1, "key": key, "tier": tier, "family": list(fam), "status": "open",
         "opened": stamp(), "sport": ref.sport, "game": f"{ref.home} vs {ref.away}",
         "home": ref.home, "away": ref.away, "start": ref.start.isoformat(),
         "market": family_label(fam, ref.home, ref.away),
         "middle": middle_label(kind, low.line, high.line, ref.home), "legs": legs,
         "p_middle_pct": None if n["p_middle"] is None else round(n["p_middle"] * 100, 1),
         "stake": n["staked"], "worst": round(n["worst"], 2), "best": round(n["best"], 2),
         "expected": None if n["expected"] is None else round(n["expected"], 2),
         "flags": [], "closed": "", "close_reason": "", "missing_runs": 0,
         "result": "", "hit": None, "profit": None, "features": features}
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


def manual_results():
    """{trade id: value} typed in by hand for games ESPN doesn't cover."""
    try:
        with open(MANUAL, encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (FileNotFoundError, ValueError):
        return {}


def settle(trades):
    """Settle finished middles by the final score (ESPN, or manual_results.json)."""
    now = now_utc()
    manual = manual_results()
    for t in trades:
        if t["status"] not in ("awaiting result", "needs result"):
            continue
        start = datetime.fromisoformat(t["start"])
        period, scope, stat, kind = t["family"]
        game = None
        if t["id"] in manual:
            value = manual[t["id"]]
            value = results.VOID if str(value).lower() == "void" else value
            summary = "result entered by hand"
        else:
            try:
                game = results.find_game(t["sport"], t["home"], t["away"], start)
            except Exception:
                game = None
            if game and game.cancelled:
                t["status"], t["closed"], t["profit"], t["hit"] = "void", stamp(), 0.0, None
                t["close_reason"] = f"game {game.detail.lower()} - bets void, stakes back"
                continue
            value = game.value(period, scope, stat, kind) if game and game.completed else None
            summary = game.summary() if game else ""
        if value is None:
            if t["status"] == "awaiting result" and now > start + timedelta(hours=NO_RESULT_HOURS):
                t["status"] = "needs result"
                t["close_reason"] = ("ESPN has the game but not this stat" if game else
                                     "not on ESPN") + " - look it up and add it to manual_results.json"
            continue
        payout = 0.0
        for l in t["legs"]:
            if value == results.VOID:
                l["result"] = "void"
            elif kind == "ou":
                l["result"] = "win" if (value > l["line"]) == (l["outcome"] == "over") else "lose"
            else:   # spread: value = home margin; home covers if margin + line > 0, away if margin + line < 0
                l["result"] = "win" if (value + l["line"] > 0) == (l["outcome"] == "home") else "lose"
            payout += l["stake"] * l["odds"] if l["result"] == "win" else l["stake"] if l["result"] == "void" else 0
        t["profit"] = round(payout - t["stake"], 2)
        t["hit"] = all(l["result"] == "win" for l in t["legs"])
        shown = "didn't play" if value == results.VOID else (f"margin {value:+d}" if kind == "hcp" else f"{value}")
        t["result"] = f"{summary} -> {shown}"
        t["status"], t["closed"] = "settled", stamp()
        t["close_reason"] = "middle hit - both bets won" if t["hit"] else \
            "void - stakes back" if value == results.VOID else "one bet won"
        if t["tier"] == "qualified":
            notify(f"Middle settled: {t['profit']:+.2f} $", f"{t['game']} - {t['market']}\n{t['result']}")


# --- output ---------------------------------------------------------------
def write_csv(trades):
    rows = []
    for t in trades:
        r = {k: t.get(k, "") for k in CSV_FIELDS}
        r["legs"] = " | ".join(f"${l['stake']} {l['label']} @ {l['odds']:g} {l['book']} ({l['status']})" for l in t["legs"])
        r["flags"] = " | ".join(t["flags"])
        r["features"] = json.dumps(t.get("features", {}))
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


def tier_stats(ts):
    live = [t for t in ts if t["status"] != "void"]
    done = [t for t in live if t["status"] == "settled"]
    with_ev = [t for t in live if t["expected"] is not None]
    return {"n": len(live), "settled": len(done), "hit": sum(bool(t["hit"]) for t in done),
            "earned": sum(t["profit"] for t in done), "staked_done": sum(t["stake"] for t in done),
            "expected_open": sum(t["expected"] for t in with_ev if t["status"] != "settled"),
            "avg_pm": mean(t["p_middle_pct"] for t in with_ev) if with_ev else 0,
            "expected_done": sum(t["expected"] or 0 for t in done)}


def write_dashboard(trades, last_scan):
    from html import escape
    for t in trades:
        t.setdefault("tier", "qualified")
    void = [t for t in trades if t["status"] == "void"]
    pending = [t for t in trades if t["status"] == "needs result"]
    flagged = sum(bool(t["flags"]) for t in trades if t["status"] != "void")

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

    def total_prob(t):
        return f"{sum(l['prob'] for l in t['legs']) * 100:.1f}%"

    def chance(t):
        return "?" if t["p_middle_pct"] is None else f"{t['p_middle_pct']:.1f}%"

    order = {"needs result": 0, "open": 1, "started": 2, "awaiting result": 3, "settled": 4, "void": 5}
    tier_name = {"qualified": "qualified", "candidate": "close case"}

    def tiles(name, st):
        roi = st["earned"] / st["staked_done"] * 100 if st["staked_done"] else 0
        return (f'<div class="tiles"><div class="tile">{name}<b>{st["n"]}</b></div>'
                f'<div class="tile">Money earned (settled)<b>{money(st["earned"])} $</b></div>'
                f'<div class="tile">ROI settled<b>{roi:+.1f}%</b></div>'
                f'<div class="tile">Middles hit / settled<b>{st["hit"]} / {st["settled"]}</b></div>'
                f'<div class="tile">Expected on settled<b>{money(st["expected_done"])} $</b></div>'
                f'<div class="tile">Expected, still open<b>{money(st["expected_open"])} $</b></div>'
                f'<div class="tile">Avg. chance of middle<b>{st["avg_pm"]:.1f}%</b></div></div>')
    rows = "".join(
        f"<tr><td><span class='st'>{escape(t['status'])}</span></td><td>{tier_name.get(t['tier'], t['tier'])}</td>"
        f"<td class='n'>{escape(t['opened'][5:])}</td>"
        f"<td>{escape(t['sport'])}</td><td>{escape(t['game'])}<div class='muted'>"
        f"{datetime.fromisoformat(t['start']).astimezone():%a %d %b %H:%M}</div></td>"
        f"<td>{escape(t['market'])}</td><td class='legs'>{''.join(leg_html(l) for l in t['legs'])}</td>"
        f"<td>{escape(t['middle'])}</td>"
        f"<td class='n'>{chance(t)}</td><td class='n'>{total_prob(t)}</td>"
        f"<td class='n'>{money(t['worst'])}</td><td class='n'>{money(t['expected'])}</td>"
        f"<td class='n'>{money(t['best'])}</td><td class='n'>{money(t.get('profit'))}</td>"
        f"<td class='flags'>{escape(t.get('result') or '')}{'<br>' if t.get('result') else ''}"
        f"{'<br>'.join(escape(f) for f in t['flags'])}"
        f"{'<br>' if t['flags'] and t['close_reason'] else ''}{escape(t['close_reason'])}</td></tr>"
        for t in sorted(trades, key=lambda t: (t["tier"] != "qualified", order.get(t["status"], 9),
                                               -(t["expected"] or -99), -t["id"])))
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Middle paper trades</title>
<style>
:root {{ --bg:#fff; --fg:#111; --muted:#666; --line:#e4e4e4; --pos:#0a7d32; --neg:#c62828; --warn:#b26a00; --chip:#f1f1f1; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161616; --fg:#eee; --muted:#999;
  --line:#333; --pos:#4cc777; --neg:#ff6b6b; --warn:#f0a840; --chip:#2a2a2a; }} }}
body {{ background:var(--bg); color:var(--fg); font:13px system-ui,sans-serif; margin:14px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
.sub, .muted {{ color:var(--muted); }} .sub {{ margin-bottom:16px; }}
.tiles {{ display:flex; gap:22px; flex-wrap:wrap; }} .tile b {{ display:block; font-size:19px; }}
table {{ border-collapse:collapse; width:100%; font-size:12px; }} th, td {{ padding:3px 5px; vertical-align:top;
  border-bottom:1px solid var(--line); text-align:left; white-space:normal; }}
td.n, th.n {{ white-space:nowrap; }} td.legs {{ min-width:230px; }} td.legs div {{ white-space:nowrap; }}
th {{ color:var(--muted); font-weight:500; }} td.flags {{ min-width:140px; max-width:260px; color:var(--warn); }}
.pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }} .warn {{ color:var(--warn); }}
.st {{ background:var(--chip); border-radius:4px; padding:1px 6px; }}
.wrap {{ overflow-x:auto; }}
</style></head><body>
<h1>Middle paper trades</h1>
<div class="sub">${STAKE} imaginary per middle, whole-dollar stakes · Pinnacle, FanDuel, LeoVegas, BetRivers (Ontario)
 · worst case = only one bet wins, best = both win · last scan {escape(last_scan)} · reloads every minute</div>
<h2>Qualified</h2>
<div class="sub">expected profit at least ${MIN_EV:.2f}, worst case at most -${MAX_RISK:.0f}, re-checked at the books</div>
{tiles("Middles", tier_stats([t for t in trades if t["tier"] == "qualified"]))}
<h2>Close cases (for data)</h2>
<div class="sub">the best middles that just miss: expected profit at least ${CANDIDATE_MIN_EV:+.2f}, worst case at most
 about -$3 (total probability of the two legs at most {CANDIDATE_MAX_IMPLIED * 100:.0f}%), legs at {MIN_LEG_ODDS:g}+ odds
 - placed without re-check or pop-up</div>
{tiles("Close cases", tier_stats([t for t in trades if t["tier"] == "candidate"]))}
<div class="sub" style="margin-top:12px">Flagged (book may cancel): {flagged} · void: {len(void)}
 · needs a result looked up by hand: {len(pending)}</div>
<h2>Trades</h2><div class="wrap"><table>
<tr><th>Status</th><th>Tier</th><th>Placed</th><th>Sport</th><th>Game</th><th>Bet</th><th>Legs (stake, pick, odds, book, probability)</th>
<th>Middle if</th><th class="n">Chance</th><th class="n">Total prob.</th><th class="n">Worst $</th>
<th class="n">Expected $</th><th class="n">Best $</th><th class="n">Profit $</th><th>Result / flags</th></tr>
{rows}</table></div></body></html>"""
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
    global STATE, CSV_FILE, DASHBOARD, LOG, MANUAL
    STATE, CSV_FILE, DASHBOARD, LOG, MANUAL = (os.path.join(out_dir, f) for f in
                                               ("middle_trades.json", "middle_trades.csv", "middle_dashboard.html",
                                                "middle_log.txt", "manual_results.json"))


def process(data, trades):
    """One scan's data (from scanner.collect) -> new paper trades, tracking, files. Returns a summary."""
    middles = find_middles(data.games, data.by_event)
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
    # candidates: the best of the rest, placed (unconfirmed, silently) to collect data
    near = [m for m in find_middles(data.games, data.by_event, strict=False)
            if m[4]["expected"] is not None and m[4]["expected"] >= CANDIDATE_MIN_EV
            and 1 / m[2].odds + 1 / m[3].odds <= CANDIDATE_MAX_IMPLIED]
    placed_candidates = 0
    for g, fam, low, high, n in sorted(near, key=lambda m: -m[4]["expected"]):
        if placed_candidates >= CANDIDATES_PER_SCAN:
            break
        tk = trade_key(g, fam)
        if tk in taken or f"candidate|{tk}" in taken:
            continue
        taken.add(f"candidate|{tk}")
        open_trade(g, fam, low, high, n, trades, tier="candidate")
        placed_candidates += 1
    track(trades, data.events, data.by_event)
    settle(trades)
    save(trades)
    write_csv(trades)
    write_dashboard(trades, stamp())
    return f"{len(middles)} middles seen, {len(new)} placed, {placed_candidates} candidates placed"


def probs_for(g, fam, low, high, by_event):
    """Fair P(low side wins) at both legs' lines, from this scan's prices at every book."""
    kind = fam[3]
    at = defaultdict(list)
    for book, (e, flipped) in g["books"].items():
        for o in oriented(by_event.get((book, e.id), []), flipped):
            if (o.period, o.scope, o.stat, o.kind) == fam and o.line in (low.line, high.line):
                at[o.line].append(o)
    return fair_prob(at[low.line], LOW_SIDE[kind]), fair_prob(at[high.line], LOW_SIDE[kind])
