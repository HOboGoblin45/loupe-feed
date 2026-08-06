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

A SECOND READING THIS FILE MUST NOT MAKE (2026-08-06)

The one above is about the price axis. This one is about the population.

Until 2026-08-06, brands.json set perBrand = 60 and Shopify's /products.json
returns published_at DESCENDING, so what this archive tracked was never a
brand's catalogue — it was the 60 most recently published pieces. 88 of 173
brands sat exactly at that cap, and for those the tracked shelf ROTATED: a new
listing pushed an old piece out of the window, and the old piece looks, in this
file and in everything built on it, exactly like a piece that left the market.

Measured over 2026-07-16 -> 2026-08-01: whole-brand rotation accounted for 981
of 2,041 disappearances, 48%. Bec + Bridge "lost" 60 of 60 products and finished
the window holding 60. Agmes, VESTIGE and The Frankie Shop show the same
signature; Christopher Esber lost 50 of 50 and ended with 55. None of them sold
out. So the "34% monthly churn" quoted at the top of this file, and every
turnover figure derived from disappearance, is roughly half our own scraper.

Raising the cap fixes the future. It cannot fix the past, and it introduces its
own artefact in the opposite direction: on the day it lands, thousands of pieces
that were always for sale become visible for the first time. SAMPLING_EPOCHS
marks that boundary, `arrivalBlackout` is emitted into the output so a consumer
cannot miss it, and build_catalog.py stamps newly-admitted products with the
STORE's own published_at instead of today so most of the spike never happens.

Note which claims survive and which do not. Anything measured on the store's own
`available` flag, for a product present at BOTH endpoints, is immune — the
sampler cannot flip a flag on a piece it is still holding. Anything measured on
ABSENCE is contaminated and has to say so.

THE SHALLOW-CLONE TRAP (2026-08-01)

This script's entire input is `git log`. That makes it silently sensitive to how
the repo was CLONED, which nothing about running it reveals.

Measured on 2026-08-01: the working clone was shallow. It could see 28 daily
catalog snapshots. The remote had 42. Every day from 2026-06-17 to 2026-06-30 —
a third of the whole dataset, and the *oldest* third, which is the part that
cannot be re-derived later — was invisible. The script did not fail. It emitted
a well-formed file stamped `windowStart: 2026-07-01`, which reads exactly like a
statement that the data begins there. It does not; that is just where the clone
began.

The asset this file exists to build is worth precisely as much as its length, so
understating the length is the one error that matters. A shallow or grafted
repository is now a hard stop rather than a quiet truncation. `--allow-shallow`
still permits a partial run for local experimentation, but stamps
`partialHistory: true` into the output so nothing downstream can mistake a
fragment for the record.

CI NOTE: actions/checkout defaults to fetch-depth: 1, so any workflow that ever
runs this MUST set `fetch-depth: 0`. Otherwise it would see a single snapshot
and exit on the two-snapshot minimum below.

USAGE
    python build_price_history.py                 # walk git, write the file
    python build_price_history.py --report        # human summary, writes nothing
    python build_price_history.py --allow-shallow # partial run, marked as partial
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

# Dates on or after which the SAMPLING methodology changed — WHICH products we
# look at, rather than what we record about the ones we already had.
#
# A price epoch voids PRICE comparisons. A sampling epoch voids everything
# counted by a product's ARRIVAL, DISAPPEARANCE or TENURE, because on that day
# the population changed underneath the measurement. The two are different
# failures and need different guards; conflating them would either leave
# arrivals unprotected or needlessly destroy price history for every piece we
# already had.
#
# 2026-08-06 — perBrand raised from 60. Until that day the scrape took at most
# 60 items from each store's /products.json, which returns published_at
# DESCENDING. What we tracked was therefore a brand's PUBLISHING FRONT, not its
# catalogue, and for the 88-of-173 brands sitting exactly at the cap that front
# ROTATED: a new listing pushed an old piece out of the window and the old piece
# read, in every absence-based metric, as gone from the market.
#
# Measured before the change: whole-brand rotation accounted for 981 of 2,041
# disappearances between 2026-07-16 and 2026-08-01 — 48%. Bec + Bridge "lost"
# 60 of 60 products and ended the window holding 60. Nothing sold out.
#
# The change fixes the future and CANNOT fix the past, so it is a boundary in
# both directions:
#   • BEFORE it, "disappeared" is ~half our own sampler.
#   • ON it, thousands of pieces that were always for sale become visible for
#     the first time. That is not a drop, not a restock, and not newness.
# build_catalog.py additionally stamps newly-admitted products with the STORE's
# own published_at rather than today, so the arrival spike mostly does not
# happen at source. This list is the second net, for the pieces whose store
# publishes no usable date.
SAMPLING_EPOCHS = [
    "2026-08-06",  # perBrand 60 -> paginated whole-catalogue walk
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


def history_is_truncated() -> bool:
    """True when this clone cannot see the repo's whole history.

    Two ways that happens, both of which produce a shorter dataset with no error:
      • a shallow clone (`clone --depth N`), which git reports directly;
      • a grafted history (`.git/shallow` present after a partial unshallow).
    Either one makes windowStart a fact about the CLONE, not about the data.
    """
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        return True
    # Older gits (and some grafted states) do not answer the question above but
    # still leave the marker file behind. Ask git where it lives rather than
    # assuming .git is a directory — in a worktree or submodule it is a file.
    git_dir = git("rev-parse", "--git-dir").strip()
    if not git_dir:
        return False
    return (REPO / git_dir / "shallow").exists() or pathlib.Path(git_dir, "shallow").exists()


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


def sampling_epoch_of(day: str) -> int:
    """Which SAMPLING regime a day belongs to. Arrival / disappearance / tenure
    comparisons across two different regimes are void: the population changed,
    not the market."""
    return sum(1 for e in SAMPLING_EPOCHS if day >= e)


def in_sampling_settle_window(day: str) -> bool:
    """True while a sampling change is still working through the feed.

    A cap raise does not land in one clean day either: each store is re-walked on
    its own schedule and the grace window carries yesterday's shape forward for
    up to GRACE_DAYS. Pieces first seen inside this window have a firstDay that
    describes our scraper, so nothing may read them as arrivals.
    """
    return any(e <= day < _plus(e, EPOCH_SETTLE_DAYS) for e in SAMPLING_EPOCHS)


def crosses_sampling_epoch(day_a: str, day_b: str) -> bool:
    """True when a window spans a sampling change, i.e. when 'this piece is gone'
    and 'this piece is new' cannot be compared between its two endpoints."""
    lo, hi = sorted((day_a, day_b))
    return sampling_epoch_of(lo) != sampling_epoch_of(hi)


def arrival_blackout(days) -> list:
    """[startIdx, endIdx] day-index ranges in which firstDay is NOT an arrival.

    Emitted into price_history.json so a consumer that never read this file's
    prose still cannot mistake the boundary for a drop. Inclusive on both ends;
    empty when the archive does not span a sampling change.
    """
    out = []
    for i, d in enumerate(days):
        if in_sampling_settle_window(d):
            if out and out[-1][1] == i - 1:
                out[-1][1] = i
            else:
                out.append([i, i])
    return out


def build(verbose: bool = True, allow_shallow: bool = False):
    # Check BEFORE walking: a truncated clone still produces a perfectly
    # well-formed file, so there is no later point at which this is detectable.
    truncated = history_is_truncated()
    if truncated and not allow_shallow:
        sys.exit(
            "REFUSING TO BUILD: this clone's history is truncated (shallow/grafted).\n"
            "\n"
            "  Everything here is reconstructed from `git log`, so a shallow clone\n"
            "  silently yields a SHORTER dataset and a windowStart that describes the\n"
            "  clone rather than the data. On 2026-08-01 that cost 14 of 42 days —\n"
            "  the oldest third, which is the part that cannot be rebuilt later.\n"
            "\n"
            "  Fix it:      git fetch --unshallow\n"
            "  In CI:       actions/checkout@v4  with:  fetch-depth: 0\n"
            "  Anyway:      python build_price_history.py --allow-shallow\n"
            "               (output is stamped partialHistory: true)"
        )
    if truncated:
        print("WARNING: shallow/grafted clone — this is a FRAGMENT, not the record.",
              file=sys.stderr)

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
        # True when the clone could not see the whole history, so windowStart is a
        # lower bound rather than the real beginning. Never omit it on a partial
        # run: an absent flag is indistinguishable from a complete one.
        **({"partialHistory": True} if truncated else {}),
        "priceEpochs": PRICE_EPOCHS,
        # Sampling changes, and the day-index ranges they poison. A consumer
        # computing arrivals, turnover, refresh rate or tenure MUST skip these:
        # inside them, firstDay is a fact about the scraper. Emitted rather than
        # merely documented for the same reason partialHistory is — an absent
        # flag has to be indistinguishable from nothing to worry about, and the
        # only way to guarantee that is to always emit the flag.
        "samplingEpochs": SAMPLING_EPOCHS,
        "epochSettleDays": EPOCH_SETTLE_DAYS,
        "arrivalBlackout": arrival_blackout(days),
        "schema": "[firstDayIdx, daysSeen, minPrice, maxPrice, lastPrice, lastChangeDayIdx]",
        "products": products,
        "brands": brands,
    }


def report(hist):
    """What a shopper could now be told, and what a brand could now be sold."""
    days, prods = hist["days"], hist["products"]
    print("=" * 74)
    print(f"PRICE HISTORY  {hist['windowStart']} -> {hist['windowEnd']}  ({days} daily snapshots)")
    if hist.get("partialHistory"):
        print("  !! PARTIAL — clone is shallow; the window starts where the CLONE does,")
        print("     not where the data does. Run `git fetch --unshallow` and rebuild.")
    print(f"  products with any history : {len(prods):,}")

    tracked = {k: v for k, v in prods.items() if v[1] >= 7}
    print(f"  tracked 7+ days           : {len(tracked):,}")

    # A sampling change makes thousands of pieces appear on one day. Say so
    # before printing anything a reader could mistake for market movement.
    blackout = hist.get("arrivalBlackout") or []
    if blackout:
        n_in = sum(1 for v in prods.values()
                   if any(a <= v[0] <= b for a, b in blackout))
        print(f"\n  !! SAMPLING EPOCH IN WINDOW {hist.get('samplingEpochs')}")
        print(f"     day-index ranges where firstDay is NOT an arrival: {blackout}")
        print(f"     pieces first seen inside them : {n_in:,} "
              f"({100*n_in/max(len(prods),1):.0f}% of the file)")
        print("     Those are pieces the old 60-item cap hid, not new listings, and")
        print("     their daysSeen is a floor. No arrival, refresh-rate, turnover or")
        print("     tenure claim may be computed across this boundary.")

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
    ap.add_argument("--allow-shallow", action="store_true",
                    help="build from a truncated clone anyway (stamps partialHistory)")
    args = ap.parse_args()

    print("walking catalog history…", file=sys.stderr)
    hist = build(allow_shallow=args.allow_shallow)
    report(hist)

    if not args.report:
        OUT.write_text(json.dumps(hist, separators=(",", ":")), encoding="utf-8")
        kb = OUT.stat().st_size / 1024
        print(f"\nwrote {OUT.name}  ({kb:,.0f} KB, vs 8,800 KB for one catalog snapshot)")


if __name__ == "__main__":
    main()
