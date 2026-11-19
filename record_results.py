"""
Record results looked up by hand for the arb and middle paper trades. Merges into
arb_manual_results.json / manual_results.json (never removes earlier entries); the running scanner
settles those trades on its next scan.

    python record_results.py arb 123=4 125=void middle 2041=181 2050=-3
    python record_results.py --file found.json      {"arb": {"123": 4}, "middle": {"2041": 181}}
    python record_results.py --tried "2026-10-04|tennis|Hunter Heck|Ryota Tanuma" ...   searched, not found

Values: see pending_results.py (total / home margin / 1 or -1 for a winner / yes / no / void).
"""
import json
import os
import sys

from pending_results import FILES, MANUAL, TRIED, load_json


def parse_value(v):
    v = str(v).strip().lower()
    if v in ("void", "yes", "no"):
        return v
    return int(float(v)) if float(v).is_integer() else float(v)


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def record(found):
    for bot, values in found.items():
        ids = {str(t["id"]) for t in json.load(open(FILES[bot], encoding="utf-8"))}
        manual = load_json(MANUAL[bot])
        added = 0
        for tid, value in values.items():
            if str(tid) not in ids:
                print(f"skipped {bot} {tid}: no such trade")
                continue
            manual[str(tid)] = parse_value(value)
            added += 1
        save(MANUAL[bot], manual)
        print(f"{bot}: {added} results recorded ({len(manual)} in {os.path.basename(MANUAL[bot])})")


def mark_tried(keys):
    tried = load_json(TRIED)
    for k in keys:
        tried[k] = tried.get(k, 0) + 1
    save(TRIED, tried)
    print(f"{len(keys)} games marked as searched without a result")


def main():
    args = sys.argv[1:]
    if args[:1] == ["--file"]:
        record(json.load(open(args[1], encoding="utf-8")))
        return
    if args[:1] == ["--tried"]:
        mark_tried(args[1:])
        return
    found, bot = {}, None
    for a in args:
        if a in FILES:
            bot = a
            found.setdefault(bot, {})
        elif "=" in a and bot:
            tid, value = a.split("=", 1)
            found[bot][tid] = value
        else:
            sys.exit(f"don't understand {a!r} - see the docstring")
    record(found)


if __name__ == "__main__":
    main()
