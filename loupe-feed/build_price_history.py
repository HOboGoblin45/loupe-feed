#!/usr/bin/env python3
"""Loupe — price & availability history, reconstructed from the catalog's own git log.

WHY THIS EXISTS

Loupe's problem is not discovery. Measured on live telemetry, the feed's approval
rate is 21.6% and people save constantly — 1,701 saves in 45 days. What they do
NOT do is buy: 113 outbound clicks against those 1,701 saves, roughly fifteen
saves per click. The gap is not "she couldn't find anything she liked". She found
plenty. She would not commit to a $180 dress from a label she has never heard of.

The independent tier is where that hesitation is worst, because every reassurance
a shopper leans on is missing: no reviews, no brand recognition, no idea whether
the price is fair, no idea whether it will still be there next week.

Two of those four are answerable from data Loupe already generates and nobody
else has for these brands:

  • "Is this a fair price?"  — we have published a full snapshot of every product
    every single day. Git kept all of them.
  • "Will it still be here?" — measured churn over July 2026 was 34%: 2,568 of
    7,591 pieces disappeared in one month. Scarcity in this tier is real, and can
    be stated honestly instead of faked with a countdown timer.

The dataset costs nothing. It has been accumulating since 2026-07-01 as a side
effect of the daily refresh commit, and it cannot be bought or backfilled — a
competitor starting today is however many months behind and cannot catch up by
spending money. That is the most durable asset in the whole product.

WHAT IT EMITS

price_history.json, published beside catalog.json:

  { "generatedAt", "windowStart", "windowEnd", "days",
    "products": { "<id>": [firstDay, daysSeen, minPrice, maxPrice, lastPrice,
                           lastChangeDay] },
    "brands":   { "<brand>": { "tracked", "everDiscounted", "medianHold" } } }

Day values are integer offsets from windowStart, so the file stays small (~8,200
entries at ~40 bytes ≈ 350 KB, vs 8.8 MB for one catalog snapshot).

A READING THIS FILE MUST NOT MAKE

On 2026-07-15 the scrape was pinned to country=US and 49 geo-priced brands were
flipped to USD (commit 10b4c79). Prices legitimately moved for a large part of
the catalog on that date — that is the pipeline getting MORE accurate, not a
sale. Any window spanning it will show spurious "increases" of roughly 8-45%.
`priceEpoch` below marks the boundary; anything claiming a price DROP must be
computed inside a single epoch. The guard is enforced, not merely documented.

USAGE
    python build_price_history.py                 # walk git, write the file
    python build_price_history.py --report        # human summary, writes nothing
"""

import argparse
import collections
import datetime as dt
import json
import pathlib
import statistics
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
CATALOG_REL = "loupe-feed/catalog.json"
OUT = HERE / "price_history.json"

# Dates on or after which the pricing METHODOLOGY changed. A price move that
# straddles one of these is an artefact of our own pipeline, not the brand's
# decision, and must never be shown to a shopper as a discount.
PRICE_EPOCHS = [
    "2026-07-15",  # 10b4c79 — pinned scrape to country=US, 49 brands flipped to USD
]

# A methodology change does not land in one clean day. The country=US pin rolled
# through as each brand's next scrape ran and caches expired, so prices kept
# stepping for a couple of days afterwards. Comparing across that tail produces
# exactly the false "discount" this file exists to avoid: on the first run,
# six brands showed 100% of their catalog "discounted" — Rat and Boa, Attega,
# Susamusa, Martine Rose, Musier Paris, C'est Nous — all of which were simply
# repricing into the new regime. Days inside the buffer are dropped from
# comparison entirely. A claim we are not sure of is a claim we do not make.
EPOCH_SETTLE_DAYS = 3

# A price that moves by less than this is rounding/FX noise, not a real change.
MIN_MEANINGFUL_MOVE = 0.02


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


def daily_snapshots():
    """(day, sha) for the LAST commit of each day that touched the catalog."""
    out = {}
    for line in git("log", "--format=%H|%ad", "--date=short", "--", CATALOG_REL).splitlines():
        if "|" not in line:
            continue
        sha, day = line.split("|", 1)
        out.setdefault(day.strip(), sha.strip())   # log is newest-first → last of day wins
    return sorted(out.items())


def epoch_of(day: str) -> int:
    """Which pricing regime a day belongs to. Comparisons across regimes are void."""
    return sum(1 for e in PRICE_EPOCHS if day >= e)


def _plus(day: str, n: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()


def in_settle_window(day: str) -> bool:
    """True while a pricing-methodology change is still working through the feed."""
    return any(e <= day < _plus(e, EPOCH_SETTLE_DAYS) for e in PRICE_EPOCHS)


def build(verbose: bool = True):
    snaps = daily_snapshots()
    if len(snaps) < 2:
        sys.exit("Need at least two daily catalog snapshots in git history.")
    days = [d for d, _ in snaps]
    start, end = days[0], days[-1]
    day_index = {d: i for i, d in enumerate(days)}

    # pid -> list of (dayIndex, price); brand/meta captured from the newest sighting
    seen = collections.defaultdict(list)
    brand_of = {}

    for day, sha in snaps:
        raw = git("show", f"{sha}:{CATALOG_REL}")
        if not raw.strip():
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            if verbose:
                print(f"  {day}: unparseable snapshot, skipped", file=sys.stderr)
            continue
        i = day_index[day]
        for p in doc.get("products", []):
            pid, price = p.get("id"), p.get("price")
            if not pid or not isinstance(price, (int, float)) or price <= 0:
                continue
            seen[pid].append((i, float(price)))
            brand_of[pid] = p.get("brand") or "?"
        if verbose:
            print(f"  {day}  {len(doc.get('products', [])):>5} products", file=sys.stderr)

    products = {}
    brand_stats = collections.defaultdict(lambda: {"tracked": 0, "everDiscounted": 0, "holds": []})

    for pid, points in seen.items():
        points.sort()
        first_i = points[0][0]
        last_i, last_price = points[-1]
        prices = [pr for _, pr in points]

        # Only compare prices inside ONE pricing epoch (see the module docstring).
        cur_epoch = epoch_of(days[last_i])
        same_epoch = [(i, pr) for i, pr in points
                      if epoch_of(days[i]) == cur_epoch and not in_settle_window(days[i])]
        # Too little comparable data → make NO price claim about this piece. The
        # emitted daysSeen still reflects reality, but min==max==last so nothing
        # downstream will call it discounted or call it a low.
        if len(same_epoch) < 2:
            products[pid] = [first_i, len(points), round(last_price, 2),
                             round(last_price, 2), round(last_price, 2), first_i]
            b0 = brand_of[pid]
            brand_stats[b0]["tracked"] += 1
            brand_stats[b0]["holds"].append(len(points))
            continue
        cmp_prices = [pr for _, pr in same_epoch]

        lo, hi = min(cmp_prices), max(cmp_prices)
        last_change = first_i
        for k in range(1, len(same_epoch)):
            if abs(same_epoch[k][1] - same_epoch[k - 1][1]) / max(same_epoch[k - 1][1], 1) > MIN_MEANINGFUL_MOVE:
                last_change = same_epoch[k][0]

        products[pid] = [
            first_i,                    # first day we saw it (offset from windowStart)
            len(points),                # days observed
            round(lo, 2),               # lowest price in the current epoch
            round(hi, 2),               # highest price in the current epoch
            round(last_price, 2),       # price now
            last_change,                # day of the last meaningful move
        ]

        b = brand_of[pid]
        st = brand_stats[b]
        st["tracked"] += 1
        if hi > lo * (1 + MIN_MEANINGFUL_MOVE):
            st["everDiscounted"] += 1
        st["holds"].append(len(points))

    brands = {
        b: {
            "tracked": st["tracked"],
            "everDiscounted": st["everDiscounted"],
            "medianHold": int(statistics.median(st["holds"])) if st["holds"] else 0,
        }
        for b, st in brand_stats.items()
    }

    return {
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "windowStart": start,
        "windowEnd": end,
        "days": len(days),
        "priceEpochs": PRICE_EPOCHS,
        "schema": "[firstDayIdx, daysSeen, minPrice, maxPrice, lastPrice, lastChangeDayIdx]",
        "products": products,
        "brands": brands,
    }


def report(hist):
    """What a shopper could now be told, and what a brand could now be sold."""
    days, prods = hist["days"], hist["products"]
    print("=" * 74)
    print(f"PRICE HISTORY  {hist['windowStart']} -> {hist['windowEnd']}  ({days} daily snapshots)")
    print(f"  products with any history : {len(prods):,}")

    tracked = {k: v for k, v in prods.items() if v[1] >= 7}
    print(f"  tracked 7+ days           : {len(tracked):,}")

    never = [k for k, v in tracked.items() if v[3] <= v[2] * (1 + MIN_MEANINGFUL_MOVE)]
    lowest = [k for k, v in tracked.items()
              if v[4] <= v[2] * (1 + MIN_MEANINGFUL_MOVE) and v[3] > v[2] * (1 + MIN_MEANINGFUL_MOVE)]
    print(f"\n  CLAIMS WE CAN NOW MAKE, HONESTLY AND CHECKABLY")
    print(f"    'never discounted since we started watching' : {len(never):,} pieces")
    print(f"    'at the lowest price we have seen'           : {len(lowest):,} pieces")

    print(f"\n  BRAND PRICE DISCIPLINE (>=20 tracked) — a brand-side insight nobody else sells")
    rows = [(b, s["tracked"], s["everDiscounted"], 100.0 * s["everDiscounted"] / s["tracked"])
            for b, s in hist["brands"].items() if s["tracked"] >= 20]
    rows.sort(key=lambda r: r[3])
    print(f"    {'brand':26} {'tracked':>8} {'discounted':>11} {'%':>6}")
    for b, n, d, pct in rows[:6]:
        print(f"    {b[:26]:26} {n:>8} {d:>11} {pct:>5.0f}%   full-price label")
    print("    ...")
    for b, n, d, pct in rows[-6:]:
        print(f"    {b[:26]:26} {n:>8} {d:>11} {pct:>5.0f}%   discounts often")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="print a summary, write nothing")
    args = ap.parse_args()

    print("walking catalog history…", file=sys.stderr)
    hist = build()
    report(hist)

    if not args.report:
        OUT.write_text(json.dumps(hist, separators=(",", ":")), encoding="utf-8")
        kb = OUT.stat().st_size / 1024
        print(f"\nwrote {OUT.name}  ({kb:,.0f} KB, vs 8,800 KB for one catalog snapshot)")


if __name__ == "__main__":
    main()
