"""
Fetch odds from the bookmakers' public odds feeds and turn every bet into a comparable Offer.

Books (all Ontario-licensed, all read without logging in):
    Pinnacle             guest.api.arcadia.pinnacle.com  - every market of a sport in 2 requests
    FanDuel (Ontario)    sbapi.on.sportsbook.fanduel.ca  - sport pages + one request per game tab
    LeoVegas, BetRivers  Kambi offering API              - sport lists + one request per game
DraftKings blocks automated reading ("Access Denied"), so it is not used.

The same bet looks different at every book ("Total Goals - Regular Time", "Over/Under 2.5 Goals",
a Pinnacle total with period 0 ...). Each one is parsed into a canonical key

    (period, scope, stat, kind, line)   + outcome

    period   game (full game incl. overtime where the sport has it; soccer = 90 min),
             reg (hockey/basketball regulation only), h1, h2, q1-q4, p1-p3, f5 (baseball
             first 5 innings), set1 ...
    scope    game, home, away (team totals) or player:<name>
    stat     main (goals / points / runs / tennis games), corners, cards, sets, or a player
             stat: points, rebounds, assists, threes, pra, goals, sog, hits, tb, hr, k ...
    kind     ml2 (home/away), ml3 (home/draw/away), ou (over/under), hcp (home/away at a
             handicap - line is the home team's), yn (yes/no)

Two offers are the same bet only if the whole key is equal. Anything that can't be parsed with
confidence is skipped - a missed arb costs nothing, a wrongly matched one is a fake arb.
Handicap and total lines must end in .5 (no push possible, so every rule system agrees),
except whole-number lines in basketball and American football, which push the same way everywhere.
Player "N+ points" yes/no bets are treated as over/under N-0.5.
"""
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0", "Accept": "application/json"}
REQUEST_GAP = 0.3           # seconds between two requests to the same book
FULL_WINDOW_H = 6           # every market is fetched for games starting within this many hours
FULL_REFRESH_MIN = 10       # ... and refreshed when older than this (main markets: every scan)
OUTCOMES = {"ml2": ("home", "away"), "ml3": ("home", "draw", "away"),
            "ou": ("over", "under"), "hcp": ("home", "away"), "yn": ("yes", "no")}
WHOLE_LINES_OK = {"basketball", "football"}
# sports where an unlabelled spread/total is ambiguous (games or sets? legs or sets? maps or rounds?):
# only compared when the label says the unit explicitly (stat "games" / "sets")
UNIT_UNCLEAR = {"tennis", "darts", "esports", "volleyball", "snooker", "table_tennis", "mma", "boxing", "cricket"}
GENERIC_WORDS = {"state", "city", "united", "university", "college", "town", "county", "athletic", "sporting",
                 "real", "rovers", "wanderers", "albion", "fc", "sc", "club", "team", "women", "u21", "u23", "ii"}


@dataclass
class Event:
    book: str
    id: str
    sport: str
    start: datetime
    home: str
    away: str


@dataclass
class Offer:
    book: str
    event_id: str
    period: str
    scope: str
    stat: str
    kind: str
    line: float
    outcome: str
    odds: float          # decimal
    ref: str = ""        # what to re-fetch to re-check this price (FanDuel tab, Pinnacle sport)

    @property
    def key(self):
        return (self.period, self.scope, self.stat, self.kind, self.line)


# --- helpers --------------------------------------------------------------
def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def american_to_decimal(a):
    a = float(a)
    return round(1 + (a / 100 if a > 0 else 100 / -a), 4)


def ascii_lower(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def norm_team(s):
    s = ascii_lower(s)
    s = re.sub(r"\(.*?\)|\b(fc|cf|sc|afc|the|club|de|ac|women|w)\b|[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def team_score(a, b):
    """How sure are we that two team names are the same team: 0 (no) .. 1 (identical)."""
    from difflib import SequenceMatcher
    a, b = norm_team(a), norm_team(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if re.search(rf"\b{re.escape(a)}\b", b) or re.search(rf"\b{re.escape(b)}\b", a):
        return 0.9
    last = a.split()[-1]
    if last == b.split()[-1] and len(last) > 3 and last not in GENERIC_WORDS:   # "TOR Maple Leafs"
        return 0.85
    r = SequenceMatcher(None, a, b).ratio()
    return r if r > 0.8 else 0.0


def same_team(a, b):
    return team_score(a, b) > 0


def player_key(name):
    """'Villen, Pablo Tamba' and 'Pablo Tamba Villen' -> the same key."""
    name = ascii_lower(name)
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first} {last}"
    words = [w for w in re.sub(r"[^a-z ]", " ", name).split() if w not in ("jr", "sr", "ii", "iii", "iv")]
    return "player:" + " ".join(sorted(words)) if words else None


def line_ok(line, sport):
    if line is None:
        return False
    frac = round(abs(line) % 1, 2)
    return frac == 0.5 or (frac == 0 and sport in WHOLE_LINES_OK)


def period_of(text, sport):
    """Period from a market label; None = a time slice we don't compare (innings, minutes...)."""
    t = text.lower()
    if re.search(r"\d+:\d+\s*-\s*\d+:\d+|minutes|\d(st|nd|rd|th) inning|\binning \d", t):
        return None
    m = re.search(r"first (\d) innings|(\d)(?:st|nd|rd|th) 5 innings", t)
    if m:
        return f"f{m.group(1) or 5}"
    if re.search(r"1st half|first half|half[- ]time", t):
        return "h1"
    if re.search(r"2nd half|second half", t):
        return "h2"
    m = re.search(r"quarter (\d)|(\d)(?:st|nd|rd|th) quarter", t)
    if m:
        return f"q{m.group(1) or m.group(2)}"
    m = re.search(r"period (\d)|(\d)(?:st|nd|rd|th) period", t)
    if m:
        return f"p{m.group(1) or m.group(2)}"
    m = re.search(r"\bset (\d)|(\d)(?:st|nd|rd|th) set", t)
    if m:
        return f"set{m.group(1) or m.group(2)}"
    if re.search(r"regular time|regulation|60 min|excludes ot", t) and sport != "soccer":
        return "reg"
    return "game"


# basketball combined stats ("Points & Assists by the Player") - checked before the single stats,
# or "Points + Assists 27.5" would be read as assists 27.5
BASKETBALL_PARTS = (("points", r"\bpoints?\b|\bpts\b"), ("rebounds", r"\brebounds?\b|\brebs?\b"),
                    ("assists", r"\bassists?\b|\basts?\b"))
COMBOS = {frozenset({"points", "rebounds"}): "pr", frozenset({"points", "assists"}): "pa",
          frozenset({"rebounds", "assists"}): "ra", frozenset({"points", "rebounds", "assists"}): "pra"}
# player stat words, most specific first
PLAYER_STATS = [
    (r"hits,? runs,? ?(&|and|\+) ?rbis|hits \+ runs \+ rbis", "hrr"),
    (r"stolen base", "sb"), (r"home run", "hr"), (r"total bases|bases recorded", "tb"),
    (r"\brbi", "rbi"), (r"strikeout", "k"), (r"outs recorded", "outs"), (r"\bhits?\b", "hits"),
    (r"\bruns?\b", "runs"), (r"three|3-point|threes", "threes"), (r"rebound", "rebounds"),
    (r"assist", "assists"), (r"\bpoints?\b|\bpts\b", "points"), (r"shots on goal", "sog"),
    (r"shots on target", "sot"), (r"\bshots?\b", "shots"), (r"saves", "saves"),
    (r"goal|to score|goalscorer|goal scorer", "goals"),
]
PLAYER_SKIP = r"first|last|power play|outside|or give an assist|card|laser|moonshot|triple|double|single|parlay|/"


def player_stat(text):
    t = text.lower()
    if re.search(PLAYER_SKIP, t):
        return None
    no_threes = re.sub(r"three[- ]point", "", t)          # "Three-Point Field Goals" isn't points
    parts = frozenset(name for name, pattern in BASKETBALL_PARTS if re.search(pattern, no_threes))
    if len(parts) >= 2:
        return COMBOS[parts]
    for pattern, key in PLAYER_STATS:
        if re.search(pattern, t):
            return key
    return None


def game_stat(text):
    t = re.sub(r"(including|inc\.?) (overtime|ot|extra innings).*$", "", text.lower())   # "...and Penalty Shootout"
    if re.search(r"odd|even|race|first to|winning margin|bands|exact|correct|both halves|/|&| or more", t):
        return None
    if "corner" in t:
        return "corners"
    # stats that aren't the score: yards, touchdowns, shots... ("Team Total Passing Yards" is not team points)
    if re.search(r"booking|shot|foul|save|\baces?\b|yard|touchdown|\btds?\b|sack|reception|field goal|"
                 r"interception|turnover|punt|kick|completion|attempt|fumble|first down|penalt|possession|"
                 r"timeout|rebound|assist|steal|block|three|strikeout|\bhits?\b|walk|home run|error|"
                 r"tackle|offside|throw|\btr(y|ies)\b|break point|tiebreak|double fault|checkout|\blegs?\b", t):
        return None
    if re.search(r"\bcards?\b", t):
        return "cards"
    if "set handicap" in t or "total sets" in t:
        return "sets"
    if re.search(r"\bgames?\b", t):
        return "games"
    return "main"


def offer(book, event_id, period, scope, stat, kind, line, outcome, odds, sport, ref=""):
    """Build an Offer, or None if the line isn't one we compare."""
    if period is None or scope is None or stat is None or not odds or odds <= 1.0:
        return None
    if kind in ("ou", "hcp"):
        if stat == "main" and sport in UNIT_UNCLEAR or stat == "games" and sport != "tennis":
            return None
        if not line_ok(line, sport):
            return None
        line = float(line)
    else:
        line = 0.0
    if kind == "ml3" and period == "game" and sport in ("hockey", "basketball", "football", "baseball"):
        period = "reg"            # a 3-way result in these sports is always regulation time
    return Offer(book, str(event_id), period, scope, stat, kind, line, outcome, float(odds), ref)


# --- http -----------------------------------------------------------------
_locks, _last = {}, {}
counts = {}


def get(book, url, tries=3):
    lock = _locks.setdefault(book, threading.Lock())
    for i in range(tries):
        with lock:
            wait = _last.get(book, 0) + (REQUEST_GAP if i == 0 else 2) - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[book] = time.time()
        counts[book] = counts.get(book, 0) + 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.load(r)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            if i == tries - 1:
                raise


# --- Pinnacle -------------------------------------------------------------
PIN_SPORTS = {"soccer": "soccer", "basketball": "basketball", "hockey": "hockey", "football": "football",
              "baseball": "baseball", "tennis": "tennis", "mixed martial arts": "mma", "boxing": "boxing",
              "handball": "handball", "volleyball": "volleyball", "rugby union": "rugby_union",
              "rugby league": "rugby_league", "darts": "darts", "e sports": "esports", "cricket": "cricket",
              "snooker": "snooker", "aussie rules": "aussie_rules", "table tennis": "table_tennis"}
PIN_PERIODS = {"soccer": {0: "game", 1: "h1"},
               "basketball": {0: "game", 1: "h1", 2: "h2", 3: "q1", 4: "q2", 5: "q3", 6: "q4"},
               "football": {0: "game", 1: "h1", 2: "h2", 3: "q1", 4: "q2", 5: "q3", 6: "q4"},
               "hockey": {0: "game", 1: "p1", 2: "p2", 3: "p3"},
               "baseball": {0: "game", 1: "f5"},
               "tennis": {0: "game", 1: "set1"}}


def parse_pinnacle(sport, matchups, markets, ref=""):
    """Pinnacle sport snapshot -> (events, offers). Upcoming games only."""
    now = datetime.now(timezone.utc)
    book = "Pinnacle"
    events, offers = {}, []
    owner = {}        # matchup id -> (main event id, stat, player scope or None, {participantId: name})
    for m in matchups:
        if m["type"] == "matchup" and not m.get("parentId"):
            if m.get("isLive") or parse_time(m["startTime"]) <= now:
                continue
            p = {x["alignment"]: x["name"] for x in m["participants"]}
            if "home" in p and "away" in p:
                events[m["id"]] = Event(book, str(m["id"]), sport, parse_time(m["startTime"]), p["home"], p["away"])
                owner[m["id"]] = (m["id"], "main", None, {})
    for m in matchups:
        parent = m.get("parentId")
        if parent not in events:
            continue
        units = (m.get("units") or "").lower()
        if m["type"] == "matchup":
            stat = {"corners": "corners", "sets": "sets"}.get(units)
            if stat:
                owner[m["id"]] = (parent, stat, None, {})
        elif m["type"] == "special" and (m.get("special") or {}).get("category") == "Player Props":
            desc = m["special"]["description"]
            name = re.sub(r"\s+(Total\s+.*|\(.*\))$", "", desc)
            stat, pk = player_stat(units or desc), player_key(name)
            names = {x["id"]: x["name"].lower() for x in m["participants"]}
            if stat and pk and name != desc and set(names.values()) == {"over", "under"}:
                owner[m["id"]] = (parent, stat, pk, names)
    periods = PIN_PERIODS.get(sport, {0: "game"})
    for mk in markets:
        own = owner.get(mk["matchupId"])
        if not own or mk.get("status") != "open":
            continue
        eid, stat, pk, names = own
        period = periods.get(mk["period"])
        if stat == "corners" and period != "game":
            period = None              # Pinnacle corner periods don't line up with other books
        t = mk["type"]
        for pr in mk["prices"]:
            odds = american_to_decimal(pr["price"])
            d = pr.get("designation")
            if pk:                                   # player prop: over/under by participant
                o = offer(book, eid, period, pk, stat, "ou", pr.get("points"),
                          names.get(pr.get("participantId")), odds, sport, ref)
            elif t == "moneyline":
                kind = "ml3" if any(x.get("designation") == "draw" for x in mk["prices"]) else "ml2"
                o = offer(book, eid, period, "game", stat, kind, None, d, odds, sport, ref)
            elif t == "spread":
                line = pr["points"] if d == "home" else -pr["points"]
                o = offer(book, eid, period, "game", stat, "hcp", line, d, odds, sport, ref)
            elif t == "total":
                o = offer(book, eid, period, "game", stat, "ou", pr.get("points"), d, odds, sport, ref)
            elif t == "team_total" and mk.get("side") in ("home", "away"):
                o = offer(book, eid, period, mk["side"], stat, "ou", pr.get("points"), d, odds, sport, ref)
            else:
                o = None
            if o and o.outcome in OUTCOMES[o.kind]:
                offers.append(o)
    return list(events.values()), offers


class Pinnacle:
    name = "Pinnacle"
    base = "https://guest.api.arcadia.pinnacle.com/0.1"

    def __init__(self):
        self.sport_of_event = {}

    def fetch_sport(self, sid, sport):
        mu = get(self.name, f"{self.base}/sports/{sid}/matchups?withSpecials=true&brandId=0")
        mk = get(self.name, f"{self.base}/sports/{sid}/markets/straight?primaryOnly=false&withSpecials=true")
        events, offers = parse_pinnacle(sport, mu, mk, ref=str(sid))
        for e in events:
            self.sport_of_event[e.id] = (sid, sport)
        return events, offers

    def scan(self):
        events, offers, errors = [], [], []
        for s in get(self.name, f"{self.base}/sports"):
            sport = PIN_SPORTS.get(s["name"].lower())
            if not sport or s["matchupCount"] == 0:
                continue
            try:
                e, o = self.fetch_sport(s["id"], sport)
                events += e
                offers += o
            except Exception as ex:
                errors.append(f"{s['name']}: {ex}")
        return events, offers, errors

    def refetch(self, event_id, ref):
        sid, sport = self.sport_of_event[event_id]
        events, offers = self.fetch_sport(sid, sport)
        return [o for o in offers if o.event_id == event_id], any(e.id == event_id for e in events)


# --- Kambi (LeoVegas, BetRivers) ------------------------------------------
KAMBI_SPORTS = {"FOOTBALL": "soccer", "AMERICAN_FOOTBALL": "football", "ICE_HOCKEY": "hockey",
                "BASKETBALL": "basketball", "BASEBALL": "baseball", "TENNIS": "tennis",
                "MARTIAL_ARTS": "mma", "UFC_MMA": "mma", "BOXING": "boxing", "DARTS": "darts",
                "RUGBY_LEAGUE": "rugby_league", "RUGBY_UNION": "rugby_union",
                "AUSTRALIAN_RULES": "aussie_rules", "HANDBALL": "handball", "VOLLEYBALL": "volleyball",
                "TABLE_TENNIS": "table_tennis", "CRICKET": "cricket", "SNOOKER": "snooker",
                "LEAGUE_OF_LEGENDS": "esports", "COUNTER_STRIKE": "esports", "DOTA_2": "esports",
                "ESPORTS": "esports"}       # E_BASKETBALL, E_FOOTBALL etc. are virtual games: skipped
KAMBI_ML = ("full time", "match", "moneyline", "match odds", "half time", "1st half", "2nd half",
            "period", "quarter", "set ")


def kambi_team(text, ev):
    m = re.search(r"\bby (.+?)(?: - |$)", text, re.I)        # "Total Corners By Spain - 1st Half"
    team = m.group(1) if m else text.split(" - ")[-1] if " - " in text else None   # "Total Cards - Spain"
    if team is None or (not m and period_of(team, "") != "game") or \
            (not m and re.search(r"overtime|regular|time|half|quarter|period|innings|set", team, re.I)):
        return "game"
    h, a = team_score(team, ev["homeName"]), team_score(team, ev["awayName"])
    if h > a:
        return "home"
    if a > h:
        return "away"
    return None                    # a team we can't place, or an unknown suffix: skip, don't guess


def parse_kambi(book, ev, bet_offers):
    sport = KAMBI_SPORTS.get(ev.get("sport"))
    if not sport:
        return []
    eid = ev["id"]
    out = []
    for bo in bet_offers:
        label = bo["criterion"]["englishLabel"]
        low = label.lower()
        typ = bo["betOfferType"]["englishName"]
        period = period_of(label, sport)
        outs = [o for o in bo["outcomes"] if o.get("status") == "OPEN" and o.get("odds")]

        def add(scope, stat, kind, line, outcome, o):
            x = offer(book, eid, period, scope, stat, kind, line, outcome, o["odds"] / 1000, sport)
            if x and x.outcome in OUTCOMES[x.kind]:
                out.append(x)

        if typ == "Match":
            if not low.startswith(KAMBI_ML) or re.search(r"draw no bet|first|next|most|corner", low):
                continue
            types = {o["type"] for o in outs}
            kind = "ml3" if "OT_CROSS" in types else "ml2"
            for o in outs:
                add("game", "main", kind, None, {"OT_ONE": "home", "OT_TWO": "away", "OT_CROSS": "draw"}.get(o["type"]), o)
        elif typ in ("Over/Under", "Asian Over/Under"):
            scope, stat = kambi_team(label, ev), game_stat(re.split(r" by ", label, flags=re.I)[0])
            if stat == "main" and not re.search(r"total (goals|points|runs|games)|asian total|^total$", low):
                continue
            for o in outs:
                add(scope, stat, "ou", o.get("line", 0) / 1000, {"OT_OVER": "over", "OT_UNDER": "under"}.get(o["type"]), o)
        elif typ in ("Handicap", "Asian Handicap"):
            if not re.match(r"(handicap|point spread|puck line|run line|asian handicap|game handicap|set handicap)( - |$)", low):
                continue
            stat = game_stat(label)
            for o in outs:
                if o["type"] == "OT_ONE" or (o["type"] == "OT_UNTYPED" and same_team(o.get("participant", ""), ev["homeName"])):
                    add("game", stat, "hcp", o["line"] / 1000, "home", o)
                elif o["type"] == "OT_TWO" or (o["type"] == "OT_UNTYPED" and same_team(o.get("participant", ""), ev["awayName"])):
                    add("game", stat, "hcp", -o["line"] / 1000, "away", o)
        elif typ == "Yes/No" and low.startswith("both teams to score"):
            for o in outs:
                add("game", "btts", "yn", None, {"OT_YES": "yes", "OT_NO": "no"}.get(o["type"]), o)
        elif typ == "Player Occurrence Line":
            stat = player_stat(re.sub(r"\(.*?\)", "", label))
            if not stat:
                continue
            for o in outs:
                pk = player_key(o.get("participant", ""))
                if not pk:
                    continue
                if o["type"] in ("OT_OVER", "OT_UNDER"):
                    add(pk, stat, "ou", o["line"] / 1000, o["type"][3:].lower(), o)
                elif o["type"] in ("OT_YES", "OT_NO"):       # "N+ ..." yes/no  ==  over/under N-0.5
                    add(pk, stat, "ou", o["line"] / 1000 - 0.5, "over" if o["type"] == "OT_YES" else "under", o)
    return out


class Kambi:
    def __init__(self, brand, name):
        self.brand, self.name = brand, name
        self.base = f"https://eu-offering-api.kambicdn.com/offering/v2018/{brand}"
        self.q = "lang=en_US&market=CA"
        self.full = {}            # event id -> (fetched at, offers)
        self.raw = {}             # event id -> event json

    def scan(self):
        now = datetime.now(timezone.utc)
        events, offers, errors = [], [], []
        groups = get(self.name, f"{self.base}/group.json?{self.q}")["group"]["groups"]
        for g in groups:
            if not g.get("termKey") or KAMBI_SPORTS.get(g.get("sport")) is None and g.get("sport"):
                continue
            try:
                d = get(self.name, f"{self.base}/listView/{g['termKey']}/all/all/all/matches.json?{self.q}")
            except Exception as ex:
                errors.append(f"{g['termKey']}: {ex}")
                continue
            for e in d.get("events", []):
                ev = e["event"]
                sport = KAMBI_SPORTS.get(ev.get("sport"))
                if not sport or ev.get("state") != "NOT_STARTED" or "homeName" not in ev:
                    continue
                start = parse_time(ev["start"])
                events.append(Event(self.name, str(ev["id"]), sport, start, ev["homeName"], ev["awayName"]))
                self.raw[str(ev["id"])] = ev
                cached = self.full.get(str(ev["id"]))
                if start - now <= timedelta(hours=FULL_WINDOW_H):
                    if not cached or now - cached[0] > timedelta(minutes=FULL_REFRESH_MIN):
                        try:
                            self.refetch(str(ev["id"]), "")
                        except Exception as ex:
                            errors.append(f"event {ev['id']}: {ex}")
                    if str(ev["id"]) in self.full:
                        offers += self.full[str(ev["id"])][1]
                        continue
                offers += parse_kambi(self.name, ev, e.get("betOffers", []))
        return events, offers, errors

    def refetch(self, event_id, ref):
        d = get(self.name, f"{self.base}/betoffer/event/{event_id}.json?{self.q}&includeParticipants=true")
        ev = self.raw.get(event_id) or (d.get("events") or [{}])[0]
        offers = parse_kambi(self.name, ev, d.get("betOffers", []))
        self.full[event_id] = (datetime.now(timezone.utc), offers)
        listed = any(str(e.get("id")) == event_id and e.get("state") == "NOT_STARTED" for e in d.get("events", []))
        return offers, listed


# --- FanDuel Ontario ------------------------------------------------------
FD_SPORTS = {1: "soccer", 2: "tennis", 7511: "baseball", 7522: "basketball", 6423: "football",
             7524: "hockey", 26420387: "mma", 6: "boxing", 4: "cricket", 5: "rugby_union",
             1477: "rugby_league", 61420: "aussie_rules", 3503: "darts", 998917: "volleyball",
             468328: "handball", 2593174: "table_tennis", 6422: "snooker", 27454571: "esports"}
FD_API = "https://sbapi.on.sportsbook.fanduel.ca/api"
FD_Q = "_ak=FhMFpcPWXMeyZxOx&timezone=America%2FToronto"
# tabs whose bets never match another book (FanDuel-only props, parlays...) - not fetched
FD_SKIP_TABS = ("same-game", "sgp", "quick", "special", "popular", "parlay", "race", "margin", "odd-even",
                "laser", "moonshot", "method-of-victory", "round", "strikes", "knockdown", "time-props",
                "team-yards", "passing", "receiving", "rushing", "td-scorer", "game-props", "featured",
                "overtime", "penalties", "scoring", "lead", "innings-")
FD_SKIP_TAB_EXACT = {"innings", "all", "all-markets"}
FD_SKIP = r"/| & |parlay|double|race|margin|odd|even|correct|bands|each|special|first (goal|score|team|touchdown|basket|home run)|last|next|most|exact|both halves|to win|lead|multigol|combination|will there|overtime goal|no-hitter|reach|either|tie|win to nil|clean sheet|score in|anytime.*\bor\b"


def fd_number(text):
    m = re.findall(r"[+-]?\d+(?:\.\d+)?", text)
    return float(m[-1]) if m else None


def fd_side(name, home, away):
    h, a = team_score(name, home), team_score(name, away)
    if h > a:
        return "home"
    if a > h:
        return "away"
    return None


def parse_fanduel(sport, event, markets, ref=""):
    names = re.split(r" @ | v | vs\.? ", event["name"])
    if len(names) != 2:
        return []
    away, home = names if " @ " in event["name"] else names[::-1]
    eid, book = event["eventId"], "FanDuel"
    out = []

    def add(scope, stat, kind, line, outcome, r):
        try:
            odds = r["winRunnerOdds"]["trueOdds"]["decimalOdds"]["decimalOdds"]
        except (KeyError, TypeError):
            return
        if r.get("runnerStatus", "ACTIVE") != "ACTIVE":
            return
        x = offer(book, eid, period, scope, stat, kind, line, outcome, round(odds, 4), sport, ref)
        if x and x.outcome in OUTCOMES[x.kind]:
            out.append(x)

    for m in markets:
        if m.get("eventId") != eid or m.get("inPlay") or m.get("marketStatus") != "OPEN":
            continue
        name = m["marketName"]
        low = name.lower()
        period = period_of(name, sport)
        rest = re.sub(r"(1st|2nd|3rd|4th|first|second) (half|quarter|period|5 innings)|first \d innings|set \d|"
                      r"half[- ]time|\(60 min\)|60 min|inc(luding)? overtime|regulation", "", low).strip(" -")
        runners = m.get("runners", [])
        ptype = m.get("marketType", "")

        # --- player props -------------------------------------------------
        if "milestones" in low:                       # "Points Milestones - Name": "Name to Score 5+ Points"
            stat = player_stat(low.split("milestones")[0])
            for r in runners:
                mm = re.match(r"(.+?) to (?:score|record) (\d+)\+", r["runnerName"], re.I)
                if stat and mm and player_key(mm.group(1)):
                    add(player_key(mm.group(1)), stat, "ou", int(mm.group(2)) - 0.5, "over", r)
            continue
        if re.match(r"(player )?to (record|hit|score)|anytime goal ?scorer|to score a hat-trick", rest) \
                and not re.search(r"first|last|parlay|/|laser|moonshot|triple|single|double", rest):
            stat = player_stat(rest)
            mm = re.search(r"(\d+)\+|(\d+) or more", rest)
            n = 3 if "hat-trick" in rest else int(mm.group(1) or mm.group(2)) if mm else 1
            for r in runners:
                pk = player_key(r["runnerName"])
                if stat and pk and "no goalscorer" not in r["runnerName"].lower():
                    add(pk, stat, "ou", n - 0.5, "over", r)
            continue
        if " - " in name and not fd_side(name.split(" - ")[0], home, away):   # "Player - Total Points"
            who, what = name.split(" - ", 1)
            stat, pk = player_stat(what), player_key(who)
            if not stat or not pk or re.search(FD_SKIP, what.lower()):
                continue
            for r in runners:
                rn = r["runnerName"].lower()
                mm = re.search(r"(\d+)\+", rn)
                if "alt" in what.lower() and mm:
                    add(pk, stat, "ou", int(mm.group(1)) - 0.5, "over", r)
                elif rn.endswith("over") or rn.endswith("under") or re.search(r"\b(over|under) \d", rn):
                    line = r.get("handicap") or fd_number(rn)
                    add(pk, stat, "ou", line, "over" if "over" in rn else "under", r)
            continue

        if re.search(FD_SKIP, rest):
            continue
        # --- team / game markets ------------------------------------------
        if rest.startswith("both teams to score"):
            for r in runners:
                add("game", "btts", "yn", None, r["runnerName"].lower(), r)
        elif re.fullmatch(r"(moneyline|money line|match betting|match odds|match result|result|winner)"
                          r"( \(?3[- ]way\)?)?", rest) or ptype in ("WIN-DRAW-WIN", "MATCH_BETTING", "MONEY_LINE"):
            kind = "ml3" if any(r["runnerName"].lower() in ("draw", "tie") for r in runners) else "ml2"
            for r in runners:
                side = "draw" if r["runnerName"].lower() in ("draw", "tie") else \
                    {"HOME": "home", "AWAY": "away"}.get((r.get("result") or {}).get("type")) or \
                    fd_side(r["runnerName"], home, away)
                add("game", "main", kind, None, side, r)
        elif re.search(r"spread|run line|puck line|handicap", rest) and not re.search(r"3[- ]way", rest):
            stat = game_stat(rest)
            for r in runners:
                team = re.sub(r"\s*[+-]\d+(\.\d+)?.*$", "", r["runnerName"])
                side = fd_side(team, home, away)
                h = r.get("handicap") or fd_number(r["runnerName"])
                if side and h is not None:
                    add("game", stat, "hcp", h if side == "home" else -h, side, r)
        elif re.search(r"total|over/under", rest):
            scope, who = "game", rest.split(" total")[0].split(" over/under")[0].strip()
            if re.match(r"home\b", rest):
                scope = "home"
            elif re.match(r"away\b", rest):
                scope = "away"
            elif who and who not in ("", "alternative", "alternate", "alt"):
                scope = fd_side(who, home, away)       # "Dodgers Total Runs"; None = unknown -> skipped
            stat = game_stat(rest)
            for r in runners:
                rn = r["runnerName"].lower()
                side = "over" if re.search(r"\bover\b", rn) else "under" if re.search(r"\bunder\b", rn) else None
                line = r.get("handicap") or fd_number(rn) or fd_number(rest)
                if side:
                    add(scope, stat, "ou", line, side, r)
    return out


def fd_slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


class FanDuel:
    name = "FanDuel"

    def __init__(self):
        self.full = {}        # event id -> (fetched at, offers)
        self.ev = {}          # event id -> (sport, event json)

    def fetch_tab(self, event_id, tab):
        sport, ev = self.ev[event_id]
        url = f"{FD_API}/event-page?eventId={event_id}&{FD_Q}" + (f"&tab={tab}" if tab else "")
        d = get(self.name, url)
        markets = list(d.get("attachments", {}).get("markets", {}).values())
        return d, parse_fanduel(sport, ev, markets, ref=tab)

    def fetch_event(self, event_id):
        first, offers = self.fetch_tab(event_id, "")
        for t in first.get("layout", {}).get("tabs", {}).values():
            slug = fd_slug(t["title"])
            if slug and slug not in FD_SKIP_TAB_EXACT and not any(s in slug for s in FD_SKIP_TABS):
                offers += self.fetch_tab(event_id, slug)[1]
        self.full[event_id] = (datetime.now(timezone.utc), offers)

    def scan(self):
        now = datetime.now(timezone.utc)
        events, offers, errors = [], [], []
        for tid, sport in FD_SPORTS.items():
            try:
                d = get(self.name, f"{FD_API}/content-managed-page?page=SPORT&eventTypeId={tid}&{FD_Q}")
            except Exception as ex:
                errors.append(f"{sport}: {ex}")
                continue
            a = d.get("attachments", {})
            markets = list(a.get("markets", {}).values())
            games = {m["eventId"] for m in markets if m.get("marketType") in
                     ("MONEY_LINE", "MATCH_BETTING", "WIN-DRAW-WIN", "MATCH_ODDS")}
            for e in a.get("events", {}).values():
                if e["eventId"] not in games or parse_time(e["openDate"]) <= now:
                    continue
                names = re.split(r" @ | v | vs\.? ", e["name"])
                if len(names) != 2:
                    continue
                away, home = names if " @ " in e["name"] else names[::-1]
                eid = str(e["eventId"])
                start = parse_time(e["openDate"])
                events.append(Event(self.name, eid, sport, start, home, away))
                self.ev[eid] = (sport, e)
                if start - now <= timedelta(hours=FULL_WINDOW_H):
                    cached = self.full.get(eid)
                    if not cached or now - cached[0] > timedelta(minutes=FULL_REFRESH_MIN):
                        try:
                            self.fetch_event(eid)
                        except Exception as ex:
                            errors.append(f"event {eid}: {ex}")
                    if eid in self.full:
                        offers += self.full[eid][1]
                        continue
                offers += parse_fanduel(sport, e, markets)
        return events, offers, errors

    def refetch(self, event_id, ref):
        d, offers = self.fetch_tab(event_id, ref)
        return offers, bool(d.get("attachments", {}).get("markets"))


def all_books():
    return [Pinnacle(), FanDuel(), Kambi("leocaon", "LeoVegas"), Kambi("rsicaon", "BetRivers")]
