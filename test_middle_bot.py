"""Offline tests on made-up odds. Run: python -m unittest"""
import unittest
from datetime import datetime, timedelta, timezone

import books as b
import middle_bot as m

START = datetime.now(timezone.utc) + timedelta(hours=2)


def o(book, kind, line, outcome, odds, scope="game"):
    return b.Offer(book, book.lower(), "game", scope, "main", kind, line, outcome, odds)


def game(*offers):
    events = {x.book: b.Event(x.book, x.book.lower(), "basketball", START, "Home", "Away") for x in offers}
    g = {"ref": next(iter(events.values())), "books": {k: (e, False) for k, e in events.items()}}
    by_event = {}
    for x in offers:
        by_event.setdefault((x.book, x.event_id), []).append(x)
    return [g], by_event


def pinnacle(line, p_over, margin=1.10):
    """Pinnacle over/under at `line` whose no-margin chance of the over is p_over."""
    return [o("Pinnacle", "ou", line, "over", 1 / (p_over * margin)),
            o("Pinnacle", "ou", line, "under", 1 / ((1 - p_over) * margin))]


class MiddleMath(unittest.TestCase):
    def test_middle_without_probability_needs_small_risk(self):
        games, by_event = game(o("A", "ou", 19.5, "over", 1.91), o("B", "ou", 21.5, "under", 1.91))
        self.assertEqual(m.find_middles(games, by_event), [])   # -4.50 worst and no way to price the middle
        games, by_event = game(o("A", "ou", 19.5, "over", 1.99), o("B", "ou", 21.5, "under", 1.99))
        [(g, fam, low, high, n)] = m.find_middles(games, by_event)
        self.assertEqual((low.line, high.line, n["staked"]), (19.5, 21.5, 100))
        self.assertAlmostEqual(n["worst"], -0.5, delta=0.01)

    def test_too_risky_is_skipped(self):
        games, by_event = game(o("A", "ou", 19.5, "over", 1.70), o("B", "ou", 21.5, "under", 1.70),
                               *pinnacle(19.5, 0.64), *pinnacle(21.5, 0.52))
        self.assertEqual(m.find_middles(games, by_event), [])   # worst about -10.40 to -15, beyond MAX_RISK

    def test_lines_must_leave_a_gap(self):
        games, by_event = game(o("A", "ou", 21.5, "over", 2.2), o("B", "ou", 19.5, "under", 2.2))
        self.assertEqual(m.find_middles(games, by_event), [])   # over 21.5 + under 19.5 can both lose

    def test_spread_middle_direction(self):
        # home -2.5 (line -2.5) + away +4.5 (home line -4.5): both win if home wins by 3 or 4
        games, by_event = game(o("A", "hcp", -2.5, "home", 2.0), o("B", "hcp", -4.5, "away", 2.0))
        [(g, fam, low, high, n)] = m.find_middles(games, by_event)
        self.assertEqual(m.middle_label("hcp", low.line, high.line, "Home"), "Home margin 3 to 4")
        self.assertGreaterEqual(n["worst"], 0)
        games, by_event = game(o("A", "hcp", -4.5, "home", 2.0), o("B", "hcp", -2.5, "away", 2.0))
        self.assertEqual(m.find_middles(games, by_event), [])

    def test_probability_and_ev(self):
        # Pinnacle no-vig: P(over 19.5) = 64%, P(over 21.5) = 52%  ->  P(middle) = 12%
        games, by_event = game(o("A", "ou", 19.5, "over", 1.91), o("B", "ou", 21.5, "under", 1.91),
                               *pinnacle(19.5, 0.64), *pinnacle(21.5, 0.52))
        [(g, fam, low, high, n)] = m.find_middles(games, by_event)
        self.assertAlmostEqual(n["p_middle"], 0.12, places=3)
        self.assertAlmostEqual(n["expected"], 0.36 * 95.5 + 0.12 * 191 + 0.52 * 95.5 - 100, places=1)

    def test_labels(self):
        self.assertEqual(m.middle_label("ou", 19.5, 21.5, "H"), "result 20-21")
        self.assertEqual(m.middle_label("ou", 2.5, 3.5, "H"), "result 3")


class FakeGame:
    completed, cancelled, detail = True, False, "Final"

    def __init__(self, value):
        self.v = value

    def value(self, period, scope, stat, kind):
        return self.v

    def summary(self):
        return "fake final"

    def score_text(self):
        return "5-3"


def settled_trade(kind, legs, value, monkey_id=None):
    """A finished middle with the given legs [(outcome, line, odds, stake)], settled against `value`."""
    t = {"id": 1, "tier": "qualified", "status": "awaiting result", "sport": "baseball", "home": "H", "away": "A",
         "start": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(), "family": ["game", "game", "main", kind],
         "game": "H vs A", "market": "spread", "stake": sum(l[3] for l in legs), "flags": [],
         "legs": [{"outcome": o, "line": li, "odds": od, "stake": st} for o, li, od, st in legs]}
    old_find, old_notify = m.results.find_game, m.notify
    m.results.find_game = lambda *a: FakeGame(value)
    m.notify = lambda *a: None
    try:
        m.settle([t])
    finally:
        m.results.find_game, m.notify = old_find, old_notify
    return t


class Settling(unittest.TestCase):
    BREWERS = [("home", -1.5, 2.5, 37), ("away", -2.5, 1.4878, 63)]   # Brewers -1.5 + Padres +2.5

    def test_spread_middle_hits_on_margin_two(self):
        t = settled_trade("hcp", self.BREWERS, 2)
        self.assertTrue(t["hit"])
        self.assertAlmostEqual(t["profit"], 37 * 2.5 + 63 * 1.4878 - 100, places=2)

    def test_spread_middle_misses(self):
        t = settled_trade("hcp", self.BREWERS, 4)            # Brewers by 4: only the -1.5 wins
        self.assertEqual([l["result"] for l in t["legs"]], ["win", "lose"])
        self.assertAlmostEqual(t["profit"], 37 * 2.5 - 100, places=2)
        t = settled_trade("hcp", self.BREWERS, -3)           # Padres win: only the +2.5 wins
        self.assertEqual([l["result"] for l in t["legs"]], ["lose", "win"])

    def test_total_and_void(self):
        legs = [("over", 19.5, 1.91, 50), ("under", 21.5, 1.91, 50)]
        self.assertTrue(settled_trade("ou", legs, 21)["hit"])
        self.assertFalse(settled_trade("ou", legs, 25)["hit"])
        t = settled_trade("ou", legs, m.results.VOID)        # player didn't play
        self.assertEqual((t["profit"], t["close_reason"]), (0.0, "void - stakes back"))

    def test_not_found_waits_then_needs_result(self):
        legs = [("over", 19.5, 1.91, 50), ("under", 21.5, 1.91, 50)]
        t = settled_trade("ou", legs, None)
        self.assertEqual(t["status"], "awaiting result")      # 5h after kickoff: keep waiting


if __name__ == "__main__":
    unittest.main()
