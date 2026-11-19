"""
List the paper trades (arbs and middles) that still need a result looked up by hand, grouped by
game, oldest first, with exactly which number each trade needs. Used by the daily results check.

    python pending_results.py [--limit N] [--json]

Games searched on MAX_TRIES days without a result are left out (record_results.py --tried KEY).

What to record for a trade (record_results.py):
    over/under            the total (or the team's / player's number) for that period
    spread, moneyline     home margin = home score - away score for that period ("home" = the first
                          team in "Game"); a tennis/darts/MMA winner bet: 1 if the first-listed player
                          won, -1 if the second did
    both teams to score   "yes" or "no"
    "void"                the book would void the bet: postponed to another day, abandoned or suspended
                          and not finished within 48h, no contest, tennis retirement before the first
                          set was completed
Periods: "game" = full game incl. overtime/shootout (soccer: 90 minutes + stoppage time), "reg" =
regulation only, h1/h2 = halves, q1-q4 = quarters, p1-p3 = hockey periods, f5 = first 5 innings,
set1 = first set.
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.dirname(HERE)
FILES = {"arb": os.path.join(PROJECTS, "sports_arb", "arb_trades.json"),
         "middle": os.path.join(PROJECTS, "middle_trades", "middle_trades.json")}
MANUAL = {"arb": os.path.join(PROJECTS, "sports_arb", "arb_manual_results.json"),
          "middle": os.path.join(PROJECTS, "middle_trades", "manual_results.json")}
TRIED = os.path.join(PROJECTS, "sports_arb", "results_tried.json")   # game key -> days searched, not found
MAX_TRIES = 3


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def needed(bot, t):
    period, scope, stat, kind = (t["market_key"][:4] if bot == "arb" else t["family"])
    who = {"game": "", "home": f"{t['home']} ", "away": f"{t['away']} "}.get(scope, scope.replace("player:", "player ") + " ")
    what = {"main": "score", "corners": "corners", "cards": "cards", "games": "games", "sets": "sets",
            "btts": "both teams to score"}.get(stat, stat)
    if kind == "ou":
        return f"{who}total {what} ({period})"
    if kind == "yn":
        return f"both teams to score yes/no ({period})"
    if t["sport"] in ("tennis", "darts", "mma", "boxing", "table_tennis", "snooker", "esports"):
        return f"winner ({period}): 1 if {t['home']} won, -1 if {t['away']} won"
    return f"home margin, {t['home']} minus {t['away']} ({period})"


def pending(include_given_up=False):
    games, attempts = {}, load_json(TRIED)
    for bot, path in FILES.items():
        manual = load_json(MANUAL[bot])
        for t in json.load(open(path, encoding="utf-8")):
            if t["status"] != "needs result" or str(t["id"]) in manual:
                continue
            key = (t["start"][:10], t["sport"], t["home"], t["away"])
            games.setdefault(key, []).append({"bot": bot, "id": t["id"], "market": t["market"],
                                              "needs": needed(bot, t), "start_utc": t["start"]})
    out = [{"date": k[0], "sport": k[1], "game": f"{k[2]} vs {k[3]}", "key": "|".join(k),
            "tries": attempts.get("|".join(k), 0), "trades": v} for k, v in sorted(games.items())]
    return out if include_given_up else [g for g in out if g["tries"] < MAX_TRIES]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, help="at most this many games")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    games = pending()
    shown = games[:args.limit] if args.limit else games
    if args.json:
        print(json.dumps(shown, indent=1))
        return
    given_up = len(pending(include_given_up=True)) - len(games)
    print(f"{len(games)} games, {sum(len(g['trades']) for g in games)} trades need a result"
          + (f" ({given_up} more given up after {MAX_TRIES} days of searching)" if given_up else ""))
    for g in shown:
        print(f"\n{g['date']}  {g['sport']}  {g['game']}  (start {g['trades'][0]['start_utc']})")
        print(f"    key: {g['key']}")
        for x in g["trades"]:
            print(f"    {x['bot']} {x['id']}: {x['market']}  ->  {x['needs']}")


if __name__ == "__main__":
    main()
