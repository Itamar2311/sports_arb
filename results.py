"""
Final scores and box scores from ESPN's public site API (no key needed), to settle paper trades.

    find_game(sport, home, away, start) -> Game or None
    game.value(period, scope, stat)     -> the number a bet settles on, None (not available), or VOID

Covered:
    scores by period   NHL (periods, regulation, OT/shootout in the final), NBA / WNBA / NBL / NFL /
                       college incl. FCS (quarters or halves), MLB (innings -> first 3/5/7), soccer (all
                       leagues; 1st half from goal times; 90-minute score only)
    soccer corners     full game, both teams and each team
    player stats       basketball points/rebounds/assists/threes/pts+reb+ast, hockey goals/assists/
                       points/shots on goal/saves, baseball hits/runs/RBIs/home runs/H+R+RBI/strikeouts
A player missing from the box score didn't play -> VOID (books return the stake).
Not covered (no free source): tennis, darts, esports, MMA, soccer player shots/cards, football props.
"""
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta

from books import player_key, team_score

API = "https://site.api.espn.com/apis/site/v2/sports"
LEAGUES = {"hockey": ["hockey/nhl"],
           "basketball": ["basketball/nba", "basketball/wnba", "basketball/nbl", "basketball/mens-college-basketball",
                          "basketball/womens-college-basketball"],
           "football": ["football/nfl", "football/college-football"],
           "baseball": ["baseball/mlb"],
           "soccer": ["soccer/all"]}
QUERY = {"football/college-football": ["&groups=80", "&groups=81"],          # FBS and FCS (small colleges)
         "basketball/mens-college-basketball": ["&groups=50"]}
PLAYER_STATS = {   # our stat -> ESPN box-score columns that add up to it
    "basketball": {"points": ["PTS"], "rebounds": ["REB"], "assists": ["AST"], "threes": ["3PT"],
                   "pra": ["PTS", "REB", "AST"], "pr": ["PTS", "REB"], "pa": ["PTS", "AST"], "ra": ["REB", "AST"]},
    "hockey": {"goals": ["G"], "assists": ["A"], "points": ["G", "A"], "sog": ["SOG"], "saves": ["SV"]},
    "baseball": {"hits": ["H"], "runs": ["R"], "rbi": ["RBI"], "hr": ["HR"], "hrr": ["H", "R", "RBI"], "k": ["K"]},
}
PITCHING = {"k"}             # baseball stats read from the pitching table, the rest from batting
VOID = "void"
CACHE_MIN = 10
_cache = {}


def get(url):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_MIN * 60:
        return hit[1]
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
        data = json.load(r)
    _cache[url] = (time.time(), data)
    return data


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def names(c):
    t = c["team"]
    return [x for x in (t.get("displayName"), t.get("shortDisplayName"), t.get("name"), t.get("location")) if x]


def match(name, competitor):
    return max(team_score(name, n) for n in names(competitor))


def find_game(sport, home, away, start):
    """The ESPN game for a trade: same teams (either way round), start within 3 hours."""
    for path in LEAGUES.get(sport, []):
        days = sorted({start.date(), (start - timedelta(hours=10)).date()})     # ESPN dates are US time
        for day, query in ((d, q) for d in days for q in QUERY.get(path, [""])):
            try:
                board = get(f"{API}/{path}/scoreboard?dates={day:%Y%m%d}&limit=500{query}")
            except Exception:
                continue
            for e in board.get("events", []):
                if abs(parse_time(e["date"]) - start) > timedelta(hours=3):
                    continue
                comp = e["competitions"][0]
                side = {c["homeAway"]: c for c in comp["competitors"]}
                if "home" not in side or "away" not in side:
                    continue
                straight = min(match(home, side["home"]), match(away, side["away"]))
                crossed = min(match(home, side["away"]), match(away, side["home"]))
                if straight > 0 and straight >= crossed:
                    return Game(sport, path, e, side["home"], side["away"])
                if crossed > 0:
                    return Game(sport, path, e, side["away"], side["home"])
    return None


class Game:
    """One ESPN game, with `ours_home` / `ours_away` = the trade's home and away team."""

    def __init__(self, sport, path, event, ours_home, ours_away):
        self.sport, self.path, self.event = sport, path, event
        self.teams = {"home": ours_home, "away": ours_away}
        st = event["status"]["type"]
        self.completed = st.get("completed", False)
        self.detail = st.get("detail") or st.get("description") or ""
        self.cancelled = bool(re.search(r"postpon|cancel|abandon|suspend", self.detail + st.get("name", ""), re.I)) \
            and not self.completed
        self._box = None

    def summary(self):
        return f"{self.teams['home']['team']['displayName']} {self.teams['home'].get('score')}-" \
               f"{self.teams['away'].get('score')} {self.teams['away']['team']['displayName']} ({self.detail})"

    def score_text(self):
        """Final score as the trade's home-away, e.g. "71-60"."""
        return f"{self.teams['home'].get('score')}-{self.teams['away'].get('score')}"

    # --- team scores -------------------------------------------------------
    def team_points(self, side, period):
        c = self.teams[side]
        final = int(float(c.get("score") or 0))
        ls = [int(float(x.get("value", 0))) for x in c.get("linescores", [])]
        sport = self.sport
        if sport == "soccer":
            if re.search(r"aet|pen|extra", self.detail, re.I):
                return None                    # score includes extra time; books settle on 90 minutes
            if period == "game":
                return final
            h1 = self.soccer_first_half(c)
            if h1 is None:
                return None
            return h1 if period == "h1" else final - h1 if period == "h2" else None
        if period == "game":
            return final
        halves_only = self.path.endswith("mens-college-basketball") and "womens" not in self.path
        n_reg = {"hockey": 3, "basketball": 2 if halves_only else 4, "football": 4}.get(sport)
        if period == "reg" and n_reg and len(ls) >= n_reg:
            return sum(ls[:n_reg])
        m = re.fullmatch(r"([qp])(\d)", period)
        if m and not halves_only and len(ls) >= int(m.group(2)):
            if (m.group(1) == "q") == (sport in ("basketball", "football")):
                return ls[int(m.group(2)) - 1]
        if period in ("h1", "h2") and sport in ("basketball", "football"):
            h1 = ls[0] if halves_only and ls else sum(ls[:2]) if len(ls) >= 2 else None
            if h1 is None:
                return None
            return h1 if period == "h1" else final - h1            # 2nd half includes overtime
        m = re.fullmatch(r"f(\d)", period)
        if m and sport == "baseball" and len(ls) >= int(m.group(1)):
            return sum(ls[:int(m.group(1))])
        return None

    def soccer_first_half(self, competitor):
        details = self.event["competitions"][0].get("details")
        if details is None:
            return None
        goals = 0
        for d in details:
            if not d.get("scoringPlay") or d.get("shootout"):
                continue
            if str(d.get("team", {}).get("id")) != str(competitor["team"]["id"]) and \
                    str(d.get("team", {}).get("id")) != str(competitor.get("id")):
                continue
            clock = d.get("clock", {}).get("displayValue", "")
            minute = re.match(r"(\d+)'", clock)
            if minute and (int(minute.group(1)) < 45 or clock.startswith("45'")):
                goals += 1
        return goals

    def corners(self, side):
        for s in self.teams[side].get("statistics", []):
            if s.get("name") == "wonCorners":
                return int(float(s.get("displayValue") or 0))
        return None

    # --- players -----------------------------------------------------------
    def box(self):
        """{player key: {"batting": {...}, "pitching": {...}, ...}} from the game summary."""
        if self._box is None:
            self._box = {}
            data = get(f"{API}/{self.path}/summary?event={self.event['id']}")
            for team in data.get("boxscore", {}).get("players", []):
                for grp in team.get("statistics", []):
                    labels = grp.get("labels") or grp.get("keys") or []
                    kind = (grp.get("name") or grp.get("type") or "").lower()
                    for a in grp.get("athletes", []):
                        pk = player_key(a["athlete"]["displayName"])
                        if pk and a.get("stats"):
                            self._box.setdefault(pk, {})[kind] = dict(zip(labels, a["stats"]))
        return self._box

    def player_value(self, pk, stat):
        cols = PLAYER_STATS.get(self.sport, {}).get(stat)
        if not cols:
            return None
        tables = self.box().get(pk)
        if not tables:
            return VOID                        # not in the box score -> didn't play
        if self.sport == "baseball":
            table = tables.get("pitching" if stat in PITCHING else "batting")
            if table is None:
                return VOID
        else:
            table = next(iter(tables.values()))
        total = 0
        for col in cols:
            raw = table.get(col)
            if raw in (None, "", "--"):
                return None
            total += int(float(str(raw).split("-")[0]))     # "3PT" is "made-attempted"
        return total

    def bet_value(self, period, scope, stat, kind):
        """value() for any bet kind: home margin for moneylines too, "yes"/"no" for both teams to score."""
        if kind in ("ml2", "ml3"):
            return self.value(period, "game", stat, "hcp")
        if kind == "yn":
            h, a = self.team_points("home", period), self.team_points("away", period)
            return None if h is None or a is None else "yes" if h > 0 and a > 0 else "no"
        return self.value(period, scope, stat, kind)

    # --- what a bet settles on ---------------------------------------------
    def value(self, period, scope, stat, kind):
        """Over/under: the total. Spread: home margin (home - away). None = can't tell."""
        if scope.startswith("player:"):
            return self.player_value(scope, stat) if period == "game" and kind == "ou" else None
        if stat == "corners":
            if period != "game":
                return None
            h, a = self.corners("home"), self.corners("away")
        elif stat == "main":
            h, a = self.team_points("home", period), self.team_points("away", period)
        else:
            return None
        if h is None or a is None:
            return None
        if kind == "hcp":
            return h - a if scope == "game" else None
        return {"game": h + a, "home": h, "away": a}.get(scope)


# --- settling single bets ---------------------------------------------------
def leg_result(kind, outcome, line, value):
    """win / lose / void for one bet, given what the market settled on (bet_value)."""
    if value == VOID:
        return "void"
    if kind == "yn":
        return "win" if outcome == value else "lose"
    if kind == "ou":
        diff = value - line
        return "void" if diff == 0 else "win" if (diff > 0) == (outcome == "over") else "lose"
    if kind == "hcp":                    # value = home margin; home covers if margin + line > 0
        diff = value + line
        return "void" if diff == 0 else "win" if (diff > 0) == (outcome == "home") else "lose"
    if kind == "ml2":                    # a tie in a 2-way moneyline is a push
        return "void" if value == 0 else "win" if (value > 0) == (outcome == "home") else "lose"
    if kind == "ml3":
        return "win" if outcome == ("home" if value > 0 else "away" if value < 0 else "draw") else "lose"
    raise ValueError(kind)


# A placed bet stands when the book later moves or pulls the line, or takes the game off its pre-game
# list at kickoff. Books void a placed bet for a pricing error ("palpable error"), and a sharp odds
# correction right after the bet ("odds cut") is the sign of one - so only those legs count as voided.
VOIDED_FLAGS = {"odds cut"}


def real_money(legs, kind, value, line_of):
    """
    What the bets would really have paid. A leg whose odds were cut sharply before kickoff (status in
    VOIDED_FLAGS) is treated as voided by its book: stake back. The other legs stand and settle on
    the result. -> (profit, [result per leg]) or None if the result is needed but unknown (value None).
    """
    out, payout = [], 0.0
    for l in legs:
        if l.get("status", "ok") in VOIDED_FLAGS:
            r = "void (flagged)"
        elif value is None:
            return None
        else:
            r = leg_result(kind, l["outcome"], line_of(l), value)
        payout += l["stake"] * l["odds"] if r == "win" else l["stake"] if r.startswith("void") else 0
        out.append(r)
    return round(payout - sum(l["stake"] for l in legs), 2), out
