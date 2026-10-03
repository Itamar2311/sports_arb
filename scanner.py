"""
One scanner, two paper traders. Every scan fetches all four books once (books.py) and hands the
same data to both bots, each with its own trades file, dashboard and GitHub repo:

    arb_bot.py     sure bets  -> ARB_DIR     (sports_arb):    arb_trades.json, arb_dashboard.html, arb_log.txt
    middle_bot.py  middles    -> MIDDLE_DIR  (middle_trades): middle_trades.json, middle_dashboard.html, middle_log.txt

The same files (books.py, arb_bot.py, middle_bot.py, scanner.py) are in both repos. Run the scanner
from one of them only: two scanners double the requests to every book, and LeoVegas answers that
with "429 Too Many Requests".

Run:  python scanner.py            scan forever, one scan every CYCLE_MIN minutes
      python scanner.py --once     one scan
      python scanner.py --arb-dir PATH --middle-dir PATH
"""
import argparse
import os
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import arb_bot
import books
import middle_bot
from arb_bot import stamp

CYCLE_MIN = 5
PROJECTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARB_DIR = os.path.join(PROJECTS, "sports_arb")
MIDDLE_DIR = os.path.join(PROJECTS, "middle_trades")


def collect(book_list):
    """Fetch every book (in parallel, one thread per book) and match the games across books."""
    t0 = time.time()
    books.counts.clear()
    with ThreadPoolExecutor(len(book_list)) as ex:
        results = list(ex.map(lambda b: (b, b.scan()), book_list))
    events, offers, errors = [], [], []
    for b, (e, o, err) in results:
        events += e
        offers += o
        errors += [f"{b.name} {x}" for x in err]
    by_event = defaultdict(list)
    for o in offers:
        by_event[(o.book, o.event_id)].append(o)
    stats = ", ".join(f"{b.name}: {sum(e.book == b.name for e in events)} games/"
                      f"{sum(o.book == b.name for o in offers)} bets" for b in book_list)
    return SimpleNamespace(events=events, offers=offers, by_event=by_event, errors=errors, t0=t0,
                           games=arb_bot.match_games(events), by_book={b.name: b for b in book_list},
                           stats=stats)


def shown_errors(errors):
    """A few fetch errors, plus every failed re-check (those decide whether a trade was placed)."""
    confirms = [e for e in errors if "confirm" in e]
    return [e for e in errors if "confirm" not in e][:3] + confirms


def main():
    p = argparse.ArgumentParser(description="One odds scanner feeding the arb and middle paper traders")
    p.add_argument("--once", action="store_true", help="one scan, then exit")
    p.add_argument("--arb-dir", default=ARB_DIR, help="where the arb bot writes its files")
    p.add_argument("--middle-dir", default=MIDDLE_DIR, help="where the middle bot writes its files")
    args = p.parse_args()
    bots = ((arb_bot, args.arb_dir), (middle_bot, args.middle_dir))
    for bot, folder in bots:
        os.makedirs(folder, exist_ok=True)
        bot.configure(folder)
    book_list = books.all_books()
    trades = {bot: bot.load() for bot, _ in bots}
    while True:
        start = time.time()
        try:
            data = collect(book_list)
            results = {}
            for bot, _ in bots:
                try:
                    results[bot] = bot.process(data, trades[bot])
                except Exception:
                    results[bot] = "failed:\n" + traceback.format_exc()
            head = f"{stamp()}  {time.time() - data.t0:.0f}s  {data.stats}  | {len(data.games)} matched games, "
            tail = f", requests {dict(books.counts)}" + \
                   (f"  | {len(data.errors)} errors: {shown_errors(data.errors)}" if data.errors else "")
            for bot, _ in bots:
                bot.log(head + results[bot] + tail)
        except Exception:
            for bot, _ in bots:
                bot.log(f"{stamp()}  scan failed:\n{traceback.format_exc()}")
        if args.once:
            break
        time.sleep(max(30, CYCLE_MIN * 60 - (time.time() - start)))


if __name__ == "__main__":
    main()
