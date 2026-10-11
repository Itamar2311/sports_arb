"""
Arbitrage paper trading across Ontario sportsbooks (Pinnacle, FanDuel, LeoVegas, BetRivers).
No real money and no real bets - it only reads public odds.

Every scan (books.py does the fetching and parsing):
1. Collect every upcoming game and every bet each book offers.
2. Match the same game across books (same sport, start within 20 min, both teams match;
   home/away is flipped when two books list the teams the other way round).
3. For every bet with identical conditions at 2+ books (same period, line, team/player,
   stat), take the best price per outcome. If sum(1 / best odds) < 1 it is an arb.
4. Re-check: fetch each leg's book again. Only if the arb is still there with whole-dollar
   stakes is a paper trade "placed" - like checking the prices before betting for real.
5. $STAKE is split across the legs so every outcome pays the same, then every leg is rounded
   to whole dollars (the rounding that keeps the most profit is picked).

Probabilities: each leg's implied probability is 1 / odds; the legs add up to under 100%.
ROI = guaranteed profit / stake. The profit is locked whichever side wins; with whole-dollar
stakes the payouts differ by a few cents to dollars, so a trade is booked at its worst payout.

Tracking until the game ends - would a book cancel the bet?
    before kickoff, every scan looks at each leg's price at its book again:
      odds cut by CUT_FLAG_PTS+ points of implied probability -> flagged: a sharp correction is
          what a pricing error ("palpable error") looks like, and books void those bets
      the bet disappears / the game disappears at one book while others still list it -> flagged
      the game disappears everywhere or kickoff moves 12h+ -> void (postponed), stake returned
    after kickoff + the sport's usual length -> "awaiting result".

Settling, as with real money: the final score (ESPN, results.py) settles every leg - the winning leg
pays, and a leg flagged before kickoff (line pulled, odds cut sharply, game removed - see
results.VOIDED_FLAGS) is gone: stake back, while the other legs stand on the result. With no flag and no result found within NO_RESULT_HOURS it settles at the
locked minimum; a flagged arb without a result becomes "needs result" - look it up and add it to
arb_manual_results.json
({"12": 3, "15": "void"}: trade id -> home margin for moneylines/spreads, the total for
over/unders, "yes"/"no" for both teams to score).

Files:
    arb_trades.json     every paper trade (the source of truth)
    arb_trades.csv      view of it, rewritten every scan
    arb_dashboard.html  open in a browser; reloads itself every minute
    arb_log.txt         one line per scan
A Windows notification pops up for every paper trade placed, flagged, voided or settled.

Run through scanner.py, which fetches the books once per scan for this bot and middle_bot:
      python scanner.py            scan forever        python scanner.py --once     one scan
"""
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import product
from math import ceil, floor

import books
import results
from books import OUTCOMES, team_score

# --- settings -------------------------------------------------------------
STAKE = 100                 # dollars per arb, split across the legs
MIN_PROFIT_PCT = 0.3        # after rounding to whole dollars
MAX_PROFIT_PCT = 8.0        # bigger is almost always two different bets matched by mistake
MATCH_MINUTES = 20          # same game if kickoff times are this close
CUT_FLAG_PTS = 0.08         # leg's implied probability up 8+ points before kickoff = flag
POSTPONE_HOURS = 12
CLOSING_MIN = 15            # books close pre-game betting around kickoff: no pulled/removed flags this close to it
GAME_HOURS = {"soccer": 2.5, "basketball": 3, "hockey": 3.5, "football": 4, "baseball": 4,
              "tennis": 5, "cricket": 9}     # kickoff -> settle; anything else 4h
NO_RESULT_HOURS = 24        # no final score this long after kickoff -> fall back / needs result
NOTIFY = True

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "arb_trades.json")
CSV_FILE = os.path.join(HERE, "arb_trades.csv")
DASHBOARD = os.path.join(HERE, "arb_dashboard.html")
LOG = os.path.join(HERE, "arb_log.txt")
MANUAL = os.path.join(HERE, "arb_manual_results.json")
CSV_FIELDS = ["id", "status", "opened", "sport", "game", "start", "market", "legs", "implied_pct",
              "stake", "payout_min", "payout_max", "roi_pct", "profit", "flags", "closed", "close_reason"]

PERIOD_NAMES = {"game": "", "reg": "regulation", "h1": "1st half", "h2": "2nd half", "f5": "first 5 innings",
                "f3": "first 3 innings", "f7": "first 7 innings"}
STAT_NAMES = {"main": "", "corners": "corners", "cards": "cards", "sets": "sets", "btts": "both teams to score",
              "pra": "pts+reb+ast", "pr": "pts+reb", "pa": "pts+ast", "ra": "reb+ast", "hrr": "hits+runs+RBIs", "sb": "stolen bases", "hr": "home runs",
              "tb": "total bases", "rbi": "RBIs", "k": "strikeouts", "outs": "outs", "hits": "hits",
              "runs": "runs", "threes": "threes", "rebounds": "rebounds", "assists": "assists",
              "points": "points", "sog": "shots on goal", "sot": "shots on target", "shots": "shots",
              "saves": "saves", "goals": "goals"}


def now_utc():
    return datetime.now(timezone.utc)


def stamp(dt=None):
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M")


# --- matching -------------------------------------------------------------
def match_games(events):
    """Group the same game across books -> list of games, each {book: (event, flipped)}."""
    games = []
    for e in sorted(events, key=lambda e: e.start):
        for g in games:
            ref = g["ref"]
            if e.book in g["books"] or e.sport != ref.sport or abs(e.start - ref.start) > timedelta(minutes=MATCH_MINUTES):
                continue
            straight = min(team_score(e.home, ref.home), team_score(e.away, ref.away))
            crossed = min(team_score(e.home, ref.away), team_score(e.away, ref.home))
            if straight > 0 and straight >= crossed:
                g["books"][e.book] = (e, False)
                break
            if crossed > 0:
                g["books"][e.book] = (e, True)
                break
        else:
            games.append({"ref": e, "books": {e.book: (e, False)}})
    return [g for g in games if len(g["books"]) >= 2]


def flip(o):
    """An offer seen from the other team's side (when two books list home/away swapped)."""
    swap = {"home": "away", "away": "home"}
    scope = swap.get(o.scope, o.scope)
    outcome = swap.get(o.outcome, o.outcome) if o.kind in ("ml2", "ml3", "hcp") else o.outcome
    line = -o.line if o.kind == "hcp" else o.line
    return books.Offer(o.book, o.event_id, o.period, scope, o.stat, o.kind, line, outcome, o.odds, o.ref)


def oriented(offers, flipped):
    return [flip(o) for o in offers] if flipped else offers


# --- arb math -------------------------------------------------------------
def whole_dollar_stakes(odds, total=STAKE):
    """Whole-dollar stakes adding up to `total` that keep the most guaranteed profit."""
    s = sum(1 / o for o in odds)
    ideal = [total * (1 / o) / s for o in odds]
    best = None
    for stakes in product(*[range(max(1, floor(x) - 1), ceil(x) + 2) for x in ideal]):
        staked = sum(stakes)
        payouts = [st * o for st, o in zip(stakes, odds)]
        profit = min(payouts) - staked
        score = (-abs(staked - total), round(profit, 2))     # exactly `total` first, then most profit
        if best is None or score > best[0]:
            best = (score, list(stakes), staked, min(payouts), max(payouts))
    _, stakes, staked, pmin, pmax = best
    return stakes, staked, pmin, pmax


def evaluate(best):
    """best = {outcome: Offer}. Arb numbers with whole-dollar stakes, or None."""
    odds = [o.odds for o in best.values()]
    implied = sum(1 / x for x in odds)
    if implied >= 1 or len({o.book for o in best.values()}) < 2:
        return None
    stakes, staked, pmin, pmax = whole_dollar_stakes(odds)
    profit_pct = (pmin - staked) / staked * 100
    if not MIN_PROFIT_PCT <= profit_pct <= MAX_PROFIT_PCT:
        return None
    return {"implied": implied, "stakes": dict(zip(best, stakes)), "staked": staked,
            "payout_min": pmin, "payout_max": pmax, "profit_pct": profit_pct}


def find_arbs(games, offers_by_event):
    """Every arb among matched games -> list of (game, key, best offers, numbers)."""
    found = []
    for g in games:
        by_key = defaultdict(lambda: defaultdict(list))
        for book, (e, flipped) in g["books"].items():
            for o in oriented(offers_by_event.get((book, e.id), []), flipped):
                by_key[o.key][o.outcome].append(o)
        for key, outs in by_key.items():
            kind = key[3]
            if set(outs) != set(OUTCOMES[kind]):
                continue
            if len({o.book for os_ in outs.values() for o in os_}) < 2:
                continue
            best = {k: max(outs[k], key=lambda o: o.odds) for k in OUTCOMES[kind]}
            numbers = evaluate(best)
            if numbers:
                found.append((g, key, best, numbers))
    return found


# --- labels ---------------------------------------------------------------
def market_label(key, home, away):
    period, scope, stat, kind, line = key
    parts = []
    if scope.startswith("player:"):
        parts.append(scope[7:].title())
    elif scope in ("home", "away"):
        parts.append(home if scope == "home" else away)
    parts.append({"ml2": "winner", "ml3": "result 1X2", "ou": f"over/under {line:g}",
                  "hcp": "spread", "yn": "yes/no"}[kind])
    if STAT_NAMES.get(stat):
        parts.append(STAT_NAMES[stat])
    if PERIOD_NAMES.get(period, period):
        parts.append(f"({PERIOD_NAMES.get(period, period)})")
    return " ".join(parts)


def outcome_label(key, outcome, home, away):
    line = key[4]
    if key[3] == "ou":
        return f"{outcome.title()} {line:g}"
    if key[3] == "hcp":
        return f"{home} {line:+g}" if outcome == "home" else f"{away} {-line:+g}"
    return {"home": home, "away": away, "draw": "Draw", "yes": "Yes", "no": "No"}[outcome]


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


def trade_key(g, key):
    ref = g["ref"]
    return f"{ref.sport}|{books.norm_team(ref.home)}|{books.norm_team(ref.away)}|{ref.start:%Y-%m-%d}|{key}"


def open_trade(g, key, best, n, trades):
    ref = g["ref"]
    legs = []
    for outcome, o in best.items():
        e, flipped = g["books"][o.book]
        legs.append({"book": o.book, "event_id": o.event_id, "flipped": flipped, "outcome": outcome,
                     "label": outcome_label(key, outcome, ref.home, ref.away), "ref": o.ref,
                     "odds": o.odds, "prob": round(1 / o.odds, 4), "stake": n["stakes"][outcome],
                     "payout": round(n["stakes"][outcome] * o.odds, 2), "now_odds": o.odds,
                     "status": "ok", "listed": True})
    t = {"id": len(trades) + 1, "key": trade_key(g, key), "market_key": list(key), "status": "open",
         "opened": stamp(), "sport": ref.sport, "game": f"{ref.home} vs {ref.away}",
         "home": ref.home, "away": ref.away, "start": ref.start.isoformat(),
         "market": market_label(key, ref.home, ref.away), "legs": legs,
         "implied_pct": round(n["implied"] * 100, 2), "stake": n["staked"],
         "payout_min": round(n["payout_min"], 2), "payout_max": round(n["payout_max"], 2),
         "roi_pct": round(n["profit_pct"], 2), "profit": round(n["payout_min"] - n["staked"], 2),
         "flags": [], "closed": "", "close_reason": "", "missing_runs": 0}
    trades.append(t)
    notify(f"Arb placed: +{t['roi_pct']:.2f}% (${t['profit']:.2f})",
           f"{t['game']} - {t['market']}\n" +
           "\n".join(f"${l['stake']} {l['label']} @ {l['odds']:g} ({l['book']})" for l in legs))
    return t


def confirm(g, key, best, by_book):
    """Re-fetch each leg's book; return (best, numbers) if the arb is still there."""
    fresh = {}
    for book in {o.book for o in best.values()}:
        e, flipped = g["books"][book]
        refs = {o.ref for o in best.values() if o.book == book}
        for ref in refs:
            offers, _ = by_book[book].refetch(e.id, ref)
            for o in oriented(offers, flipped):
                if o.key == key:
                    fresh.setdefault(o.outcome, []).append(o)
    # other books' prices for the same bet may still beat the legs - keep the original legs only
    new_best = {}
    for outcome, o in best.items():
        same = [x for x in fresh.get(outcome, []) if x.book == o.book]
        if not same:
            return None
        new_best[outcome] = max(same, key=lambda x: x.odds)
    n = evaluate(new_best)
    return (new_best, n) if n else None


def notify(title, body):
    """Windows toast notification via PowerShell (no extra packages needed)."""
    print(f"  >> {title} | {body.splitlines()[0]}")
    if not NOTIFY or sys.platform != "win32":
        return
    from xml.sax.saxutils import escape
    xml = (f"<toast><visual><binding template='ToastGeneric'><text>{escape(title)}</text>"
           f"<text>{escape(body)}</text></binding></visual></toast>").replace("'", "''")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "$x = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]::new();"
        f"$x.LoadXml('{xml}');"
        "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id)"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    subprocess.Popen(["powershell", "-NoProfile", "-Command", script],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def flag(t, text):
    if text not in t["flags"]:
        t["flags"].append(text)
        notify(f"Arb at risk: {t['game']}", f"{t['market']}\n{text}")


def worst_if_voided(t):
    """Loss if the flagged legs were voided (stake back) and the rest of the bets lost."""
    risky = [l for l in t["legs"] if l["status"] != "ok"]
    if not risky:
        return 0.0
    return -sum(l["stake"] for l in t["legs"] if l["status"] == "ok")


def track(trades, events, offers_by_event):
    """Follow open trades until kickoff (cancellation signs), then settle after the game."""
    listed = {(e.book, e.id): e for e in events}
    now = now_utc()
    for t in trades:
        if t["status"] not in ("open", "started"):
            continue
        start = datetime.fromisoformat(t["start"])
        key = tuple(t["market_key"])
        if t["status"] == "open" and now < start:
            closing = now > start - timedelta(minutes=CLOSING_MIN)
            gone_everywhere = True
            for l in t["legs"]:
                e = listed.get((l["book"], l["event_id"]))
                l["listed"] = e is not None
                if e is None:
                    if l["status"] == "ok" and not closing:
                        l["status"] = "game removed"
                    continue
                gone_everywhere = False
                if abs(e.start - start) > timedelta(hours=POSTPONE_HOURS):
                    t["status"], t["closed"], t["close_reason"] = "void", stamp(), "kickoff moved (postponed)"
                    t["profit"] = 0.0
                    notify(f"Arb void: {t['game']}", "Kickoff moved - all bets void, stakes back")
                    break
                cur = [o for o in oriented(offers_by_event.get((l["book"], l["event_id"]), []), l["flipped"])
                       if o.key == key and o.outcome == l["outcome"]]
                if cur:
                    l["now_odds"], l["missing"] = cur[0].odds, 0
                    if 1 / cur[0].odds - l["prob"] >= CUT_FLAG_PTS and l["status"] == "ok":
                        l["status"] = "odds cut"
                        flag(t, f"{l['book']} cut {l['label']} from {l['odds']:g} to {cur[0].odds:g} "
                                f"- looks like a pricing error the book may void")
                elif offers_by_event.get((l["book"], l["event_id"])):
                    l["now_odds"] = None
                    l["missing"] = l.get("missing", 0) + 1       # 2 scans in a row, not a one-off fetch gap
                    if l["missing"] >= 2 and l["status"] == "ok" and not closing:
                        l["status"] = "pulled"
                        flag(t, f"{l['book']} no longer offers {l['label']} - line moved or pulled")
            if t["status"] == "void":
                continue
            if gone_everywhere:
                t["missing_runs"] += 1
                if t["missing_runs"] >= 3:
                    t["status"], t["closed"], t["close_reason"] = "void", stamp(), "game removed by every book"
                    t["profit"] = 0.0
                    notify(f"Arb void: {t['game']}", "Game removed everywhere (postponed/cancelled)")
                continue
            t["missing_runs"] = 0
            for l in t["legs"]:
                if not l["listed"] and l["status"] == "game removed":
                    flag(t, f"{l['book']} removed the game while other books still list it")
        elif t["status"] == "open":
            t["status"] = "started"
        if t["status"] == "started" and now >= start + timedelta(hours=GAME_HOURS.get(t["sport"], 4)):
            t["status"], t["profit"] = "awaiting result", None
            t["close_reason"] = "game over - waiting for the final score"


def manual_results():
    try:
        with open(MANUAL, encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (FileNotFoundError, ValueError):
        return {}


def settle_real(trades):
    """After the game: settle each arb on what the bets would really have paid (module docstring)."""
    now = now_utc()
    manual = manual_results()
    for t in trades:
        if t["status"] not in ("awaiting result", "needs result"):
            continue
        period, scope, stat, kind, line = t["market_key"]
        start = datetime.fromisoformat(t["start"])
        flagged = any(l["status"] in results.VOIDED_FLAGS for l in t["legs"])
        value, game = None, None
        if t["id"] in manual:
            value = manual[t["id"]]
            value = results.VOID if str(value).lower() == "void" else value
        elif all(l["status"] in results.VOIDED_FLAGS for l in t["legs"]):
            value = 0                           # every leg voided: nothing depends on the result
        else:
            try:
                game = results.find_game(t["sport"], t["home"], t["away"], start)
            except Exception:
                game = None
            if game and game.cancelled:
                value = results.VOID
            elif game and game.completed:
                value = game.bet_value(period, scope, stat, kind)
        real = results.real_money(t["legs"], kind, value, lambda l: line)
        if real is None:
            if now > start + timedelta(hours=NO_RESULT_HOURS):
                if flagged:
                    t["status"] = "needs result"
                    t["close_reason"] = "flagged leg + no result found - add it to arb_manual_results.json"
                else:                           # all bets stand: the locked minimum is what it pays at least
                    t["status"], t["closed"] = "settled", stamp()
                    t["profit"] = round(t["payout_min"] - t["stake"], 2)
                    t["close_reason"] = "no result found - settled at the locked minimum"
            continue
        t["profit"], leg_results = real
        for l, r in zip(t["legs"], leg_results):
            l["result"] = r
        won = [l["label"] for l, r in zip(t["legs"], leg_results) if r == "win"]
        t["status"], t["closed"] = "settled", stamp()
        t["close_reason"] = (f"final {game.score_text()}: " if game else "") + \
            (f"{', '.join(won)} won" if won else "no leg won") + \
            (" · flagged legs voided (stake back)" if flagged else "")
        notify(f"Arb settled: {t['profit']:+.2f} $", f"{t['game']} - {t['market']}\n{t['close_reason']}")


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
    except PermissionError:          # open in Excel - skip this scan
        os.remove(tmp)


def write_dashboard(trades, last_scan):
    from html import escape
    done = [t for t in trades if t["status"] == "settled"]
    live = [t for t in trades if t["status"] in ("open", "started", "awaiting result", "needs result")]
    void = [t for t in trades if t["status"] == "void"]
    earned = sum(t["profit"] for t in done)
    pending = sum(t["payout_min"] - t["stake"] for t in live)      # locked in, not settled yet
    staked = sum(t["stake"] for t in done)
    flagged = [t for t in done + live if t["flags"]]
    at_risk = sum(worst_if_voided(t) for t in live if t["flags"])
    locked_done = sum(t["payout_min"] - t["stake"] for t in done)

    def money(x):
        if x is None:
            return ""
        return f'<span class="{"pos" if x > 0 else "neg" if x < 0 else ""}">{x:+.2f}</span>'

    def leg_html(l):
        cls = "" if l["status"] == "ok" else ' class="warn"'
        now = "" if l.get("now_odds") in (None, l["odds"]) else f" → {l['now_odds']:g}"
        st = "" if l["status"] == "ok" else f" · {escape(l['status'])}"
        res = l.get("result")
        res = f" <b class='{'pos' if res == 'win' else 'neg' if res == 'lose' else 'muted'}'>{escape(res)}</b>" if res else ""
        return (f"<div{cls}><b>${l['stake']}</b> {escape(l['label'])} @ {l['odds']:g}{now} "
                f"<span class='muted'>{escape(l['book'])} · {l['prob'] * 100:.1f}%{st}</span>{res}</div>")

    order = {"needs result": 0, "open": 1, "started": 2, "awaiting result": 3, "settled": 4, "void": 5}
    rows = "".join(
        f"<tr><td><span class='st {t['status']}'>{t['status']}</span></td><td class='n'>{escape(t['opened'][5:])}</td>"
        f"<td>{escape(t['sport'])}</td><td>{escape(t['game'])}<div class='muted'>"
        f"{datetime.fromisoformat(t['start']).astimezone():%a %d %b %H:%M}</div></td>"
        f"<td>{escape(t['market'])}</td><td class='legs'>{''.join(leg_html(l) for l in t['legs'])}</td>"
        f"<td class='n'>{t['implied_pct']:.2f}%</td><td class='n'>${t['payout_min']:.2f}</td>"
        f"<td class='n'>{t['roi_pct']:.2f}%</td><td class='n'>{money(t['profit'])}</td>"
        f"<td class='flags'>{'<br>'.join(escape(f) for f in t['flags']) or ''}"
        f"{'<br>' if t['flags'] and t['close_reason'] else ''}{escape(t['close_reason'])}</td></tr>"
        for t in sorted(trades, key=lambda t: (order[t["status"]], -t["id"])))
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arb paper trades</title>
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
.st.settled {{ color:var(--pos); }} .st.void {{ color:var(--muted); }}
.wrap {{ overflow-x:auto; }}
</style></head><body>
<h1>Arbitrage paper trades</h1>
<div class="sub">${STAKE} imaginary per arb, whole-dollar stakes · Pinnacle, FanDuel, LeoVegas, BetRivers (Ontario)
 · last scan {escape(last_scan)} · page reloads every minute</div>
<div class="tiles">
 <div class="tile">Money earned (settled)<b>{money(earned)} $</b></div>
 <div class="tile">ROI settled<b>{(earned / staked * 100 if staked else 0):+.2f}%</b></div>
 <div class="tile" title="what the settled arbs promised when placed">Locked when placed<b>{money(locked_done)} $</b></div>
 <div class="tile">Locked in, not settled<b>{money(pending)} $</b></div>
 <div class="tile">Open / settled / void<b>{len(live)} / {len(done)} / {len(void)}</b></div>
 <div class="tile">Flagged (book may cancel)<b>{len(flagged)}</b></div>
 <div class="tile">Worst case if open flagged legs voided<b>{money(at_risk)} $</b></div>
</div>
<div class="sub" style="margin-top:12px">Settled as with real money: on the final score, the winning leg pays; a leg flagged
 before kickoff (line pulled, odds cut sharply, game removed) is gone - stake back - and the other legs stand.</div>
<h2>Trades</h2><div class="wrap"><table>
<tr><th>Status</th><th>Placed</th><th>Sport</th><th>Game</th><th>Bet</th><th>Legs (stake, pick, odds, book, probability)</th>
<th class="n">Total prob.</th><th class="n">Payout</th><th class="n">ROI</th>
<th class="n" title="settled on the real result; legs flagged before kickoff (pulled, odds cut, game removed) are voided">Profit $</th><th>Notes</th></tr>
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
    """Write this bot's files into out_dir (scanner.py points it at the sports_arb folder)."""
    global STATE, CSV_FILE, DASHBOARD, LOG, MANUAL
    STATE, CSV_FILE, DASHBOARD, LOG, MANUAL = (os.path.join(out_dir, f) for f in
                                               ("arb_trades.json", "arb_trades.csv", "arb_dashboard.html",
                                                "arb_log.txt", "arb_manual_results.json"))


def process(data, trades):
    """One scan's data (from scanner.collect) -> new paper trades, tracking, files. Returns a summary."""
    arbs = find_arbs(data.games, data.by_event)
    taken = {t["key"] for t in trades}
    placed = 0
    for g, key, best, n in sorted(arbs, key=lambda a: -a[3]["profit_pct"]):
        tk = trade_key(g, key)
        if tk in taken:
            continue
        taken.add(tk)
        try:
            ok = confirm(g, key, best, data.by_book)
        except Exception as ex:
            data.errors.append(f"arb confirm: {ex}")
            continue
        if ok:
            open_trade(g, key, ok[0], ok[1], trades)
            placed += 1
    track(trades, data.events, data.by_event)
    settle_real(trades)
    save(trades)
    write_csv(trades)
    write_dashboard(trades, stamp())
    return f"{len(arbs)} arbs seen, {placed} placed"
