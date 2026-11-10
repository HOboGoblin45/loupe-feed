#!/usr/bin/env python3
"""Featured-placement fixtures -- run by CI before every catalog build.
Plain asserts, no pytest. Pins what reaches catalog.json's `featured` block."""
import json
from datetime import date

import build_catalog
from build_catalog import featured_windows

TODAY = date(2026, 11, 10)
PRESENT = {"RBK Lab", "Deiji Studios"}
checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def run(featured):
    return featured_windows({"featured": featured}, PRESENT, TODAY)


good = {"brand": "RBK Lab", "from": "2026-11-16", "to": "2026-11-22"}
out, notes = run([good])
ok(out == [good] and notes == [], f"valid window not published: {out} {notes}")

ok(featured_windows({}, PRESENT, TODAY) == ([], []), "no key must mean no block and no noise")
ok(run([]) == ([], []), "empty list must mean no block and no noise")

out, notes = run([{"brand": " RBK Lab ", "from": "2026-11-16", "to": "2026-11-16"}])
ok(out == [{"brand": "RBK Lab", "from": "2026-11-16", "to": "2026-11-16"}], "one-day window / trimmed brand")

# today is the last day: still live
out, _ = run([{"brand": "RBK Lab", "from": "2026-11-04", "to": "2026-11-10"}])
ok(len(out) == 1, "a window ending today must still be published")

# ended yesterday: dropped quietly
out, notes = run([{"brand": "RBK Lab", "from": "2026-11-03", "to": "2026-11-09"}])
ok(out == [] and notes == [], "an ended window must drop without a warning")

for bad, why in [
    ({"brand": "RBK Lab", "from": "2026-11-22", "to": "2026-11-16"}, "backwards range"),
    ({"brand": "RBK Lab", "from": "2026-02-31", "to": "2026-03-02"}, "impossible date"),
    ({"brand": "RBK Lab", "from": "11/16/2026", "to": "11/22/2026"}, "wrong date format"),
    ({"brand": "RBK Lab", "from": "2026-11-16"}, "missing end"),
    ({"brand": "", "from": "2026-11-16", "to": "2026-11-22"}, "blank brand"),
    ({"brand": "RBK Lab", "from": "2026-11-16", "to": "2027-11-16"}, "a year-long pin"),
    ({"brand": "Nobody Here", "from": "2026-11-16", "to": "2026-11-22"}, "brand not in catalog"),
    ("RBK Lab", "not an object"),
]:
    out, notes = run([bad])
    ok(out == [], f"{why}: published {out}")
    ok(len(notes) == 1, f"{why}: must leave exactly one line in the run summary, got {notes}")

out, _ = run([{"brand": "RBK Lab", "from": "2026-12-01", "to": "2026-12-31"}])
ok(len(out) == 1, "31 days is allowed")
out, _ = run([{"brand": "RBK Lab", "from": "2026-12-01", "to": "2027-01-01"}])
ok(out == [], "32 days is not")

out, notes = featured_windows({"featured": "RBK Lab"}, PRESENT, TODAY)
ok(out == [] and len(notes) == 1, "a non-list must be ignored loudly")

# one bad entry must not cost the good one
out, notes = run([{"brand": "Nobody Here", "from": "2026-11-16", "to": "2026-11-22"}, good])
ok(out == [good] and len(notes) == 1, "a bad entry took a good one down with it")

# the live roster file parses and every entry in it is well-formed
cfg = json.loads(build_catalog.BRANDS_FILE.read_text(encoding="utf-8"))
roster = {e["brand"] for e in cfg["brands"]}
ok(isinstance(cfg.get("featured", []), list), "brands.json featured must be a list")
for w in cfg.get("featured", []):
    ok(w.get("brand") in roster, f"featured names a brand not on the roster: {w.get('brand')!r}")
    live, _ = featured_windows({"featured": [w]}, roster, date(2000, 1, 1))
    ok(len(live) == 1, f"malformed featured entry in brands.json: {w}")

print(f"featured fixtures: all OK ({checks} checks)")
