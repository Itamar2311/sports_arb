"""Offline tests on made-up odds. Run: python -m unittest"""
import unittest
from datetime import datetime, timedelta, timezone

import arb_bot as a
import books as b

SOON = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()


def fd_runner(name, odds, handicap=0, kind=None):
    r = {"runnerName": name, "handicap": handicap, "runnerStatus": "ACTIVE",
         "winRunnerOdds": {"trueOdds": {"decimalOdds": {"decimalOdds": odds}}}}
    if kind:
        r["result"] = {"type": kind}
    return r


def fd_market(name, runners, mtype="X"):
    return {"eventId": 1, "marketName": name, "marketType": mtype, "marketStatus": "OPEN",
            "inPlay": False, "runners": runners}


FD_EVENT = {"eventId": 1, "name": "Fresno State @ Washington State", "openDate": SOON}


def kambi_offer(label, typ, outcomes):
    return {"criterion": {"englishLabel": label}, "betOfferType": {"englishName": typ},
            "outcomes": [dict(status="OPEN", **o) for o in outcomes]}


KAMBI_EVENT = {"id": 7, "sport": "ICE_HOCKEY", "homeName": "Toronto Maple Leafs", "awayName": "Ottawa Senators"}


class Stakes(unittest.TestCase):
    def test_whole_dollars_and_profit(self):
        stakes, staked, pmin, pmax = a.whole_dollar_stakes([1.92, 2.14])
        self.assertTrue(all(isinstance(s, int) for s in stakes))
        self.assertEqual(staked, 100)
        self.assertGreater(pmin, staked)                  # still an arb after rounding
        self.assertLess(pmax - pmin, 2.2)                 # payouts nearly equal

    def test_evaluate_rejects_same_book_and_no_arb(self):
        o = lambda book, out, odds: b.Offer(book, "1", "game", "game", "main", "ml2", 0.0, out, odds)
        self.assertIsNone(a.evaluate({"home": o("X", "home", 2.1), "away": o("X", "away", 2.1)}))
        self.assertIsNone(a.evaluate({"home": o("X", "home", 1.9), "away": o("Y", "away", 1.9)}))
        n = a.evaluate({"home": o("X", "home", 1.92), "away": o("Y", "away", 2.14)})
        self.assertAlmostEqual(n["implied"], 1 / 1.92 + 1 / 2.14)

    def test_rejects_absurd_profit(self):
        o = lambda book, out, odds: b.Offer(book, "1", "game", "game", "main", "ml2", 0.0, out, odds)
        self.assertIsNone(a.evaluate({"home": o("X", "home", 3.0), "away": o("Y", "away", 3.0)}))


class Matching(unittest.TestCase):
    def test_state_teams_are_different(self):
        self.assertFalse(b.same_team("Fresno State", "Washington State"))
        self.assertTrue(b.same_team("TOR Maple Leafs", "Toronto Maple Leafs"))
        self.assertTrue(b.same_team("Almere City", "Almere City FC"))

    def test_flip_spread(self):
        o = b.Offer("X", "1", "game", "game", "main", "hcp", -1.5, "home", 2.0)
        f = a.flip(o)
        self.assertEqual((f.outcome, f.line), ("away", 1.5))     # home -1.5 seen from the other side
        t = a.flip(b.Offer("X", "1", "game", "home", "main", "ou", 2.5, "over", 2.0))
        self.assertEqual((t.scope, t.outcome, t.line), ("away", "over", 2.5))

    def test_games_matched_with_swapped_home_away(self):
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        e1 = b.Event("P", "1", "tennis", start, "Hunter Heck", "Ryota Tanuma")
        e2 = b.Event("Q", "2", "tennis", start + timedelta(minutes=5), "Ryota Tanuma", "Hunter Heck")
        [g] = a.match_games([e1, e2])
        self.assertEqual(g["books"]["Q"][1], True)

    def test_lines(self):
        self.assertTrue(b.line_ok(2.5, "soccer"))
        self.assertFalse(b.line_ok(2.0, "soccer"))        # push possible -> skip
        self.assertFalse(b.line_ok(2.25, "soccer"))       # asian quarter line -> skip
        self.assertTrue(b.line_ok(-3.0, "football"))


class Parsers(unittest.TestCase):
    def test_fanduel_spread_sides(self):
        [home, away] = b.parse_fanduel("football", FD_EVENT, [fd_market(
            "Spread", [fd_runner("Fresno State", 1.94, 1.5), fd_runner("Washington State", 1.88, -1.5)])])
        self.assertEqual((home.outcome, home.line), ("away", -1.5))   # Fresno is away, +1.5
        self.assertEqual((away.outcome, away.line), ("home", -1.5))

    def test_fanduel_team_corners_not_game_total(self):
        ev = {"eventId": 1, "name": "North Macedonia v Scotland", "openDate": SOON}
        [o] = b.parse_fanduel("soccer", ev, [fd_market("Home Total Corners 8.5", [fd_runner("Over 8.5 Corners", 33)])])
        self.assertEqual((o.scope, o.stat, o.line), ("home", "corners", 8.5))

    def test_fanduel_milestone_is_over(self):
        ev = {"eventId": 1, "name": "Elan Chalon @ JL Bourg Basket", "openDate": SOON}
        [o] = b.parse_fanduel("basketball", ev, [fd_market("Points Milestones - Pablo Tamba Villen",
                                                            [fd_runner("Villen, Pablo Tamba to Score 5+ Points", 1.5)])])
        self.assertEqual((o.scope, o.stat, o.line, o.outcome), (b.player_key("Pablo Tamba Villen"), "points", 4.5, "over"))

    def test_kambi_regulation_vs_overtime(self):
        offers = b.parse_kambi("LeoVegas", KAMBI_EVENT, [
            kambi_offer("Total Goals - Regular Time", "Over/Under",
                        [{"type": "OT_OVER", "odds": 1900, "line": 5500}, {"type": "OT_UNDER", "odds": 1900, "line": 5500}]),
            kambi_offer("Total Goals - Including Overtime and Penalty Shootout", "Over/Under",
                        [{"type": "OT_OVER", "odds": 1800, "line": 5500}, {"type": "OT_UNDER", "odds": 2000, "line": 5500}])])
        self.assertEqual({o.period for o in offers}, {"reg", "game"})

    def test_kambi_three_way_hockey_is_regulation(self):
        offers = b.parse_kambi("LeoVegas", KAMBI_EVENT, [kambi_offer("Match Odds - Regular Time", "Match", [
            {"type": "OT_ONE", "odds": 2200}, {"type": "OT_CROSS", "odds": 4000}, {"type": "OT_TWO", "odds": 3000}])])
        self.assertEqual({(o.period, o.kind) for o in offers}, {("reg", "ml3")})

    def test_kambi_player_yes_is_over(self):
        [o] = b.parse_kambi("LeoVegas", KAMBI_EVENT, [kambi_offer(
            "Player to score at least 2 points - Including Overtime", "Player Occurrence Line",
            [{"type": "OT_YES", "odds": 2550, "line": 2000, "participant": "Auston Matthews"}])])
        self.assertEqual((o.stat, o.line, o.outcome), ("points", 1.5, "over"))

    def test_tennis_unlabelled_spread_skipped(self):
        self.assertIsNone(b.offer("P", 1, "game", "game", "main", "hcp", -1.5, "home", 1.9, "tennis"))
        self.assertIsNotNone(b.offer("P", 1, "game", "game", "games", "hcp", -1.5, "home", 1.9, "tennis"))


if __name__ == "__main__":
    unittest.main()
