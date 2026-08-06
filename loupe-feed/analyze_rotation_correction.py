#!/usr/bin/env python3
"""Loupe — how much of the published market data was our own scraper?

WHY THIS EXISTS

The Loupe Index is sold as market data for the independent tier. Roughly half of
one of its headline mechanisms turned out to be an artefact of how we sample.

`brands.json` sets perBrand = 60 and Shopify's /products.json returns
published_at DESCENDING, so what the archive tracks is not a brand's catalogue,
it is a brand's PUBLISHING FRONT: the 60 most recently listed pieces. For a small
store those are the same thing. For a store with 400 products they are not, and
the tracked shelf ROTATES — a new arrival pushes an old piece off the front and
the old piece reads, in every absence-based metric, as "gone from the market".

Measured (analyze_demand_signal.py, 2026-08-03): whole-brand rotation accounts
for 981 of 2,041 disappearances between 2026-07-16 and 2026-08-01 — 48%. Bec +
Bridge "lost" 60 of 60 products and finished the window holding 60. Nothing sold
out. The sampler turned over.

So every metric built on product ABSENCE — turnover, "selling through fastest",
arrivals, refresh rate — is roughly half artefact, and every metric built on the
store's own `available` flag is not. This script re-derives both, side by side,
and labels which is which.

WHAT IT DOES

  1. INPUT VERIFICATION. The archive is `git log`, so it is exactly as long as
     the clone AND as long as the checked-out ref. Both are checked, loudly.
  2. CATALOGUE SIZE TRUTH. Reads probe_results.json (live /products.json walk of
     the whole roster) plus the archive's own union-of-ids lower bound, and
     reports the distribution that should set perBrand.
  3. ROTATION. Reproduces the 48% figure, and measures the FALSE-NEW rate: how
     many "new arrivals" are pieces the archive had already seen and lost.
  4. CORRECTED METRICS. Every published Index headline, computed the way it is
     published and the way it survives rotation, as a before/after table.
  5. RISK MODEL. Re-fits the out-of-time sell-out model. Its strongest feature
     was brand prior CHURN — an absence measure, i.e. the contaminated one — so
     the honest version replaces it with a prior built on the availability flag.

USAGE
    python analyze_rotation_correction.py                    # full run
    python analyze_rotation_correction.py --ref main         # a different ref
    python analyze_rotation_correction.py --refresh          # re-extract
    python analyze_rotation_correction.py --probe PATH.json
"""

import argparse
import collections
import csv
import datetime as dt
import json
import math
import os
import pathlib
import statistics
import subprocess
import sys
import tempfile

import numpy as np
from scipy import stats

# Redirected stdout on Windows defaults to cp1252, which cannot encode the box
# characters and the en-dashes below; the run then dies two thirds of the way
# through with a UnicodeEncodeError and everything already printed looks fine.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
CATALOG_REL = "loupe-feed/catalog.json"
CACHE = pathlib.Path(tempfile.gettempdir()) / "loupe_rotation_cache"

# The catalog's `available` flag ships in the 2026-07-16 snapshot (f9c0658).
# Nothing before that date can be asked whether it was in stock, so every
# availability-based number below starts there. This is a hard floor, not a
# choice.
AVAIL_START = "2026-07-16"
# The Index's own longitudinal era: the roster stopped growing on 2026-07-01.
ERA_START = "2026-07-01"

PER_BRAND_CAP = 60              # brands.json perBrand at the time of measurement
NEAR_CAP = PER_BRAND_CAP - 2    # 58+ counts as at the cap (junk/variant jitter)

MIN_MEANINGFUL_MOVE = 0.02
PRICE_EPOCHS = ["2026-07-15"]
EPOCH_SETTLE_DAYS = 3

FIELDS = ["id", "brand", "price", "category", "available", "addedAt", "retailer", "name"]


# ══════════════════════════════════════════════════════════════════════════
# git / archive
# ══════════════════════════════════════════════════════════════════════════

def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def history_is_truncated():
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        return True
    git_dir = git("rev-parse", "--git-dir").strip()
    if not git_dir:
        return False
    return (REPO / git_dir / "shallow").exists() or pathlib.Path(git_dir, "shallow").exists()


def daily_snapshots(ref):
    """(day, sha) for the LAST catalog commit of each day on `ref`, oldest first."""
    out = {}
    for line in git("log", "--format=%H|%ad", "--date=short", ref, "--", CATALOG_REL).splitlines():
        if "|" in line:
            sha, day = line.split("|", 1)
            out.setdefault(day.strip(), sha.strip())
    return sorted(out.items())


def extract(ref, refresh=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    snaps = daily_snapshots(ref)
    for day, sha in snaps:
        dest = CACHE / f"{day}.tsv"
        if dest.exists() and not refresh:
            continue
        raw = git("show", f"{sha}:{CATALOG_REL}")
        try:
            doc = json.loads(raw)
        except ValueError:
            print(f"  {day}: unparseable, SKIPPED", file=sys.stderr)
            continue
        tmp = dest.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(FIELDS)
            for p in doc.get("products", []):
                av = p.get("available")
                w.writerow([
                    p.get("id", ""), p.get("brand", ""), p.get("price", ""),
                    p.get("category", ""),
                    "" if av is None else ("1" if av else "0"),
                    (p.get("addedAt") or "")[:10],
                    p.get("retailer") or "",
                    (p.get("name") or "").replace("\t", " ")[:90],
                ])
        os.replace(tmp, dest)
        print(f"  extracted {day}  {len(doc.get('products', [])):>5} products", file=sys.stderr)
    return [d for d, _ in snaps]


def load_day(day):
    out = {}
    with open(CACHE / f"{day}.tsv", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            out[row["id"]] = row
    return out


# ══════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════

def wilson(k, n, z=1.96):
    if not n:
        return (float("nan"),) * 3
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * p, 100 * (c - m), 100 * (c + m)


def auc(scores, labels):
    scores, labels = np.asarray(scores, float), np.asarray(labels, bool)
    pos, neg = scores[labels], scores[~labels]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    r = stats.rankdata(np.concatenate([pos, neg]))
    return (r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def logistic_fit(X, y, l2=2.0):
    from scipy import optimize
    X = np.column_stack([np.ones(len(X)), X])
    y = np.asarray(y, float)

    def nll(w):
        z = X @ w
        return float(np.logaddexp(0, z).sum() - y @ z + l2 * (w[1:] ** 2).sum())

    def grad(w):
        p = 1 / (1 + np.exp(-(X @ w)))
        g = X.T @ (p - y)
        g[1:] += 2 * l2 * w[1:]
        return g

    return optimize.minimize(nll, np.zeros(X.shape[1]), jac=grad, method="L-BFGS-B").x


def logistic_predict(w, X):
    return 1 / (1 + np.exp(-(np.column_stack([np.ones(len(X)), X]) @ w)))


def rule(t=""):
    print("\n" + "=" * 78)
    if t:
        print(t)
        print("=" * 78)


def pct(k, n):
    return f"{100*k/n:5.2f}%" if n else "  n/a"


# ══════════════════════════════════════════════════════════════════════════
# outcome definitions — lifted verbatim from analyze_demand_signal.py so the
# two cannot drift. FLIP is the only rotation-immune one.
# ══════════════════════════════════════════════════════════════════════════

def outcomes(B, E, kind):
    if kind == "GONE":            # CONTAMINATED — ~48% whole-brand rotation
        risk = set(B)
        return risk, {p for p in risk if p not in E}
    if kind == "SOLDOUT":         # inherits the rotation problem
        risk = {p for p, r in B.items() if r["available"] == "1"}
        return risk, {p for p in risk if p not in E or E[p]["available"] == "0"}
    if kind == "FLIP":            # CLEAN — the store's own flag, both endpoints
        risk = {p for p, r in B.items()
                if r["available"] == "1" and p in E and E[p]["available"] in ("0", "1")}
        return risk, {p for p in risk if E[p]["available"] == "0"}
    raise ValueError(kind)


def rotation_brands(B, E, thresh=0.8, min_n=3):
    """Brands that lost >=80% of their baseline shelf. Flagged ROTATION when they
    ended the window holding roughly as many pieces as they started with."""
    endcount = collections.Counter(r["brand"] for r in E.values())
    bybrand = collections.defaultdict(list)
    for p, r in B.items():
        bybrand[r["brand"]].append(p)
    out = []
    for b, ps in bybrand.items():
        g = sum(1 for p in ps if p not in E)
        if len(ps) >= min_n and g / len(ps) >= thresh:
            out.append((b, len(ps), g, endcount.get(b, 0)))
    out.sort(key=lambda t: -t[1])
    return out


# ══════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="origin/main",
                    help="git ref to read the archive from (default origin/main: "
                         "a local branch behind the remote is a shorter archive)")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--probe", default=None, help="probe_results.json from the live roster walk")
    ap.add_argument("--allow-shallow", action="store_true")
    args = ap.parse_args()

    # ── 0. INPUT VERIFICATION ─────────────────────────────────────────────
    rule("0. INPUT VERIFICATION")
    if history_is_truncated() and not args.allow_shallow:
        sys.exit("REFUSING TO RUN: shallow/grafted clone. `git fetch --unshallow` first.")
    print(f"  clone complete (not shallow)   : True")

    head_days = [d for d, _ in daily_snapshots("HEAD")]
    ref_days = [d for d, _ in daily_snapshots(args.ref)]
    print(f"  snapshots on HEAD              : {len(head_days)}  ({head_days[0]} -> {head_days[-1]})")
    print(f"  snapshots on {args.ref:18}: {len(ref_days)}  ({ref_days[0]} -> {ref_days[-1]})")
    if len(ref_days) > len(head_days):
        print(f"  !! HEAD IS BEHIND {args.ref} BY {len(ref_days)-len(head_days)} SNAPSHOT DAY(S).")
        print("     The shallow-clone guard does not catch this: the clone is complete,")
        print("     the checked-out branch is simply older. Reading from the ref.")

    days = extract(args.ref, refresh=args.refresh)
    span = (dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days + 1
    have = set(days)
    missing = [(dt.date.fromisoformat(days[0]) + dt.timedelta(d)).isoformat()
               for d in range(span)]
    missing = [d for d in missing if d not in have]
    print(f"  calendar span                  : {span} days, {len(missing)} missing -> {missing}")

    snaps = {d: load_day(d) for d in days}
    counts = {d: len(snaps[d]) for d in days}
    print(f"  products per snapshot          : min {min(counts.values()):,} "
          f"max {max(counts.values()):,} last {counts[days[-1]]:,}")
    round_days = [d for d, n in counts.items() if n in (100, 500, 1000, 5000, 10000)]
    print(f"  suspiciously round row counts  : {round_days or 'none'}")

    avail_days = [d for d in days if d >= AVAIL_START]
    print(f"  days carrying `available`      : {len(avail_days)}  "
          f"({avail_days[0]} -> {avail_days[-1]})")
    for d in avail_days[:1] + avail_days[-1:]:
        known = sum(1 for r in snaps[d].values() if r["available"] in ("0", "1"))
        print(f"    {d}: {known:,}/{len(snaps[d]):,} rows carry a known flag")

    BASELINE = AVAIL_START
    ENDPOINT_OLD = "2026-08-01"      # the endpoint every published figure used
    ENDPOINT = days[-1]              # the newest snapshot available now
    for d in (BASELINE, ENDPOINT_OLD, ENDPOINT):
        if d not in snaps:
            sys.exit(f"REFUSING TO RUN: no snapshot for {d}")
    print(f"  windows                        : baseline {BASELINE}, "
          f"published endpoint {ENDPOINT_OLD}, newest endpoint {ENDPOINT}")

    # ── 1. WHAT THE STORES ACTUALLY PUBLISH ───────────────────────────────
    rule("1. THE REAL CATALOGUE SIZES — what should set perBrand")

    # (a) archive lower bound: distinct ids ever seen per brand. A brand whose
    #     union over 50 days is 300 while its daily shelf is 60 has a store at
    #     least 5x the window, and this needs no network at all.
    # The Index's published era ends at ENDPOINT_OLD. Reproducing a published
    # figure means using the window it was published on — extending it silently
    # would make the "before" column something nobody ever published.
    era = [d for d in days if ERA_START <= d <= ENDPOINT_OLD]
    era_now = [d for d in days if d >= ERA_START]
    union = collections.defaultdict(set)
    shelf_max = collections.Counter()
    for d in era_now:          # every day we have, for the tightest lower bound
        c = collections.Counter()
        for p, r in snaps[d].items():
            if r["retailer"]:
                continue
            union[r["brand"]].add(p)
            c[r["brand"]] += 1
        for b, n in c.items():
            shelf_max[b] = max(shelf_max[b], n)
    last_shelf = collections.Counter(r["brand"] for r in snaps[ENDPOINT].values()
                                     if not r["retailer"])
    print(f"  ARCHIVE LOWER BOUND ({len(era_now)} days, {era_now[0]} -> {era_now[-1]})")
    print(f"    brands measured                     : {len(union)}")
    at_cap = [b for b in union if shelf_max[b] >= NEAR_CAP]
    print(f"    brands whose shelf reached the cap  : {len(at_cap)} "
          f"({100*len(at_cap)/max(len(union),1):.0f}%)")
    ratios = sorted((len(union[b]) / max(shelf_max[b], 1)) for b in at_cap)
    if ratios:
        print(f"    for those, distinct-ids-ever / shelf : median {statistics.median(ratios):.2f}x  "
              f"p75 {ratios[int(.75*(len(ratios)-1))]:.2f}x  max {ratios[-1]:.2f}x")
    big = sorted(((len(union[b]), b, shelf_max[b]) for b in union), reverse=True)[:12]
    print("    largest observed footprints (union of ids ever seen vs daily shelf):")
    for n, b, s in big:
        print(f"      {b[:30]:30} {n:5,} ever   shelf {s:3}")

    # (b) the live walk — the authoritative number
    probe = args.probe or str(HERE / "probe_results.json")
    if pathlib.Path(probe).exists():
        pr = json.loads(pathlib.Path(probe).read_text(encoding="utf-8"))
        rows = pr["results"]
        ok = [r for r in rows if r["pages"] > 0 and not r["brand"].startswith("[retailer]")]
        bad = [r for r in rows if r["pages"] == 0]
        print(f"\n  LIVE WALK ({pr['probedAt']}, ~{pr['approx_requests']} requests, "
              f"{pr['seconds']:.0f}s)")
        print(f"    stores answering /products.json     : {len(ok)} of "
              f"{len(rows)}  ({len(bad)} refused/blocked)")
        elig = sorted(r["eligible"] for r in ok)
        raw = sorted(r["raw"] for r in ok)

        def q(v, f):
            return v[int(f * (len(v) - 1))] if v else 0
        print(f"    RAW products published   median {q(raw,.5):5,}  p75 {q(raw,.75):5,}  "
              f"p90 {q(raw,.90):5,}  p95 {q(raw,.95):5,}  max {q(raw,1):5,}")
        print(f"    ELIGIBLE after our filters median {q(elig,.5):5,}  p75 {q(elig,.75):5,}  "
              f"p90 {q(elig,.90):5,}  p95 {q(elig,.95):5,}  max {q(elig,1):5,}")
        print(f"    stores larger than the 60 cap       : "
              f"{sum(1 for v in elig if v > PER_BRAND_CAP)} of {len(elig)} "
              f"({100*sum(1 for v in elig if v > PER_BRAND_CAP)/max(len(elig),1):.0f}%)")
        print("\n    COVERAGE AT CANDIDATE CAPS (share of answering stores fully covered,")
        print("    and the total items the daily build would then hold):")
        print(f"      {'cap':>5} {'stores fully covered':>22} {'items held':>12} "
              f"{'pages/run':>10}")
        for cand in (60, 100, 150, 200, 250, 300, 400, 500, 750, 1000):
            covered = sum(1 for v in elig if v <= cand)
            held = sum(min(v, cand) for v in elig)
            # PAGE_LIMIT is clamped to Shopify's 250 max, so pages ~= cap/250
            pages = sum(math.ceil(min(v, cand) / 250) or 1 for v in elig)
            print(f"      {cand:>5} {covered:>10}/{len(elig):<11} {held:>12,} {pages:>10,}")
        print("\n    the 12 biggest stores on the roster:")
        for r in sorted(ok, key=lambda r: -r["eligible"])[:12]:
            print(f"      {r['brand'][:30]:30} raw {r['raw']:5,}  eligible {r['eligible']:5,}"
                  f"  sold out now {pct(r['raw']-r['available'] if False else 0,1) if False else ''}")

        # robots / agents posture
        allowed = sum(1 for r in rows if r.get("products_json_allowed") is True)
        blocked = [r["domain"] for r in rows if r.get("products_json_allowed") is False]
        noro = sum(1 for r in rows if r.get("robots") not in (None, "ok"))
        agents = [r["domain"] for r in rows if r.get("agents_md")]
        print(f"\n    robots.txt readable                 : {len(rows)-noro} of {len(rows)}")
        print(f"    robots.txt ALLOWS /products.json    : {allowed}")
        print(f"    robots.txt DISALLOWS /products.json : {len(blocked)} -> {blocked or 'none'}")
        print(f"    stores publishing an agents.md/llms.txt: {len(agents)} -> {agents[:6] or 'none'}")

        # sell-out prevalence bias: the front vs the whole store
        front = {}
        for b, n in last_shelf.items():
            front[b] = n
        pairs = []
        for r in ok:
            b = r["brand"]
            if b in last_shelf and r["eligible"] >= 20:
                shelf_rows = [x for x in snaps[ENDPOINT].values()
                              if x["brand"] == b and not x["retailer"]]
                kn = [x for x in shelf_rows if x["available"] in ("0", "1")]
                if len(kn) >= 20:
                    oos_front = sum(1 for x in kn if x["available"] == "0") / len(kn)
                    oos_store = 1 - (r["available"] / max(r["eligible"], 1))
                    pairs.append((b, len(kn), oos_front, r["eligible"], oos_store))
        if pairs:
            capped = [p for p in pairs if p[3] > PER_BRAND_CAP]
            print(f"\n    SELL-OUT PREVALENCE: the tracked front vs the WHOLE store")
            print(f"      brands comparable                 : {len(pairs)} "
                  f"({len(capped)} of them larger than the cap)")
            for lbl, grp in (("all comparable", pairs), ("only stores > cap", capped)):
                if not grp:
                    continue
                f_ = 100 * statistics.mean(p[2] for p in grp)
                s_ = 100 * statistics.mean(p[4] for p in grp)
                print(f"      {lbl:33}: front {f_:5.2f}%  whole store {s_:5.2f}%  "
                      f"({s_-f_:+.2f} pts)")
    else:
        print(f"\n  (no probe_results.json at {probe} — live sizes unmeasured)")

    # ── 2. ROTATION ───────────────────────────────────────────────────────
    rule("2. ROTATION — how much of 'disappeared' is our own sampler")
    for endpoint in (ENDPOINT_OLD, ENDPOINT):
        B, E = snaps[BASELINE], snaps[endpoint]
        gone = {p for p in B if p not in E}
        later = [d for d in days if BASELINE < d <= endpoint]
        ever_gap = {p for p in B if any(p not in snaps[d] for d in later)}
        rot = rotation_brands(B, E)
        lost = sum(g for _, _, g, _ in rot)
        true_rot = [t for t in rot if t[3] >= t[1] * 0.8]
        lost_true = sum(g for _, _, g, _ in true_rot)
        print(f"\n  {BASELINE} -> {endpoint}  ({(dt.date.fromisoformat(endpoint)-dt.date.fromisoformat(BASELINE)).days}d)")
        print(f"    present at baseline               : {len(B):,}")
        print(f"    absent at the endpoint            : {len(gone):,}")
        print(f"    absent on ANY later snapshot      : {len(ever_gap):,}  "
              f"-> {100*(len(ever_gap)-len(gone))/max(len(ever_gap),1):.1f}% of gaps reverse")
        print(f"    brands losing >=80% of the shelf  : {len(rot)}  "
              f"({lost:,} of {len(gone):,} disappearances = {100*lost/max(len(gone),1):.0f}%)")
        print(f"    ...and ending it just as full     : {len(true_rot)}  "
              f"({lost_true:,} = {100*lost_true/max(len(gone),1):.0f}%)  <- pure rotation")
        if endpoint == ENDPOINT_OLD:
            print("      brand                          lost   ended holding")
            for b, n, g, k in rot[:6]:
                flag = "  <- ROTATION, not sell-out" if k >= n * 0.8 else ""
                print(f"      {b[:28]:28} {g:4}/{n:<4} {k:4}{flag}")

    # FALSE NEW: a piece the archive already knew, re-entering with a fresh
    # addedAt because build_catalog only carries addedAt forward from the
    # PREVIOUS catalog. Rotation therefore manufactures newness as well as death.
    E = snaps[ENDPOINT_OLD]
    seen_before = collections.defaultdict(set)
    acc = set()
    for d in days:
        seen_before[d] = set(acc)
        acc |= set(snaps[d])
    newish, false_new = 0, 0
    for p, r in E.items():
        if not r["addedAt"]:
            continue
        age = (dt.date.fromisoformat(ENDPOINT_OLD) - dt.date.fromisoformat(r["addedAt"])).days
        if age <= 14:
            newish += 1
            if p in seen_before.get(r["addedAt"], set()):
                false_new += 1
    print(f"\n  FALSE NEWNESS at {ENDPOINT_OLD}")
    print(f"    pieces flagged new (addedAt <=14d): {newish:,}")
    print(f"    ...that the archive had ALREADY seen before that addedAt: "
          f"{false_new:,} = {pct(false_new, newish)}")
    print("    (build_catalog carries addedAt forward from the previous catalog only,")
    print("     so a piece that rotates off the front and returns is stamped brand new)")

    # ── 3. THE PUBLISHED FIGURES, BEFORE AND AFTER ────────────────────────
    rule("3. PUBLISHED INDEX FIGURES — as published vs. rotation-immune")
    B0, E0 = snaps[ERA_START], snaps[ENDPOINT_OLD]
    roster = ({r["brand"] for r in B0.values()} & {r["brand"] for r in E0.values()})

    def direct(rows):
        return {k: v for k, v in rows.items() if not v["retailer"]}

    cohort = {p: r for p, r in direct(B0).items() if r["brand"] in roster}
    gone = {p for p in cohort if p not in E0}
    print(f"\n  A. 31-DAY TURNOVER  (published: 32.7% of {len(cohort):,})")
    p_, lo, hi = wilson(len(gone), len(cohort))
    print(f"     as published (absence)          : {p_:5.1f}%  [{lo:.1f}, {hi:.1f}]  "
          f"n={len(cohort):,}")
    # The uncontaminated stratum: brands whose shelf never reached the cap, so
    # nothing could be pushed off the front. Absence there really is delisting.
    uncapped = {b for b in roster if shelf_max[b] < NEAR_CAP}
    sub = {p: r for p, r in cohort.items() if r["brand"] in uncapped}
    gsub = {p for p in sub if p not in E0}
    p2, lo2, hi2 = wilson(len(gsub), len(sub))
    print(f"     brands never at the cap ONLY    : {p2:5.1f}%  [{lo2:.1f}, {hi2:.1f}]  "
          f"n={len(sub):,}  ({len(uncapped)} brands)")
    capped_b = roster - uncapped
    sub2 = {p: r for p, r in cohort.items() if r["brand"] in capped_b}
    g2 = {p for p in sub2 if p not in E0}
    p3, lo3, hi3 = wilson(len(g2), len(sub2))
    print(f"     brands AT the cap               : {p3:5.1f}%  [{lo3:.1f}, {hi3:.1f}]  "
          f"n={len(sub2):,}  ({len(capped_b)} brands)")
    print(f"     -> the two strata differ by {p3-p2:+.1f} points, and NOT in the")
    print(f"        direction the artefact alone predicts. The contamination is not")
    print(f"        spread evenly across capped brands, it is CONCENTRATED:")
    rotb = {b for b, _, _, k in rotation_brands(snaps[BASELINE], E0)}
    sub3 = {p: r for p, r in cohort.items() if r["brand"] not in rotb}
    g3 = {p for p in sub3 if p not in E0}
    p4, lo4, hi4 = wilson(len(g3), len(sub3))
    print(f"     the {len(rotb)} rotation brands removed : {p4:5.1f}%  "
          f"[{lo4:.1f}, {hi4:.1f}]  n={len(sub3):,}")
    print(f"     -> {len(cohort)-len(sub3):,} of the {len(cohort):,} cohort pieces sit on "
          f"{len(rotb)} brands that")
    print(f"        contribute {len(gone)-len(g3):,} of the {len(gone):,} disappearances "
          f"({100*(len(gone)-len(g3))/max(len(gone),1):.0f}%)")

    print(f"\n  B. SELL-OUT ON THE CURRENT SHELF  (published: 13.2%)")
    kn = [r for r in direct(E0).values() if r["available"] in ("0", "1")]
    oos = sum(1 for r in kn if r["available"] == "0")
    p_, lo, hi = wilson(oos, len(kn))
    print(f"     as published (availability flag): {p_:5.1f}%  [{lo:.1f}, {hi:.1f}]  n={len(kn):,}")
    print("     CLEAN in kind — this is the store's own flag, not absence. It is")
    print("     still a PREVALENCE ON THE TRACKED FRONT, so for a store bigger than")
    print("     the cap it describes the newest 60 pieces, not the catalogue.")

    print(f"\n  C. SELL-OUT AS AN INCIDENCE (not published — the honest version)")
    for endpoint in (ENDPOINT_OLD, ENDPOINT):
        risk, out = outcomes(snaps[BASELINE], snaps[endpoint], "FLIP")
        p_, lo, hi = wilson(len(out), len(risk))
        d_ = (dt.date.fromisoformat(endpoint) - dt.date.fromisoformat(BASELINE)).days
        print(f"     {BASELINE} -> {endpoint} ({d_:2d}d): {p_:5.2f}% "
              f"[{lo:.2f}, {hi:.2f}]  n={len(risk):,}  in-stock pieces that went out of stock")
        r2, o2 = outcomes(snaps[BASELINE], snaps[endpoint], "SOLDOUT")
        p2_, _, _ = wilson(len(o2), len(r2))
        r3, o3 = outcomes(snaps[BASELINE], snaps[endpoint], "GONE")
        p3_, _, _ = wilson(len(o3), len(r3))
        print(f"       (same window, contaminated definitions: SOLDOUT {p2_:5.2f}%, "
              f"GONE {p3_:5.2f}%)")

    print(f"\n  D. SELL-OUT BY PRICE BAND  (published as a barbell, "
          f"$200-349 the dead band)")
    BANDS = [(0, 60, "under $60"), (60, 120, "$60-119"), (120, 200, "$120-199"),
             (200, 350, "$200-349"), (350, 600, "$350-599"), (600, 10 ** 9, "$600+")]

    def band_of(v):
        for lo_, hi_, name in BANDS:
            if lo_ <= v < hi_:
                return name
        return None
    print("     as published (prevalence on the shelf at 2026-08-01):")
    tot, k = collections.Counter(), collections.Counter()
    for r in kn:
        b = band_of(float(r["price"] or 0))
        if b:
            tot[b] += 1
            k[b] += (r["available"] == "0")
    for _, _, name in BANDS:
        if tot[name] >= 150:
            p_, lo, hi = wilson(k[name], tot[name])
            print(f"       {name:12} n={tot[name]:5,}  {p_:5.1f}%  [{lo:.1f}, {hi:.1f}]")
    print("     rotation-immune INCIDENCE (in-stock at 07-16 -> out of stock later):")
    for endpoint in (ENDPOINT_OLD, ENDPOINT):
        risk, out = outcomes(snaps[BASELINE], snaps[endpoint], "FLIP")
        tot, k = collections.Counter(), collections.Counter()
        for p in risk:
            b = band_of(float(snaps[BASELINE][p]["price"] or 0))
            if b:
                tot[b] += 1
                k[b] += p in out
        print(f"       endpoint {endpoint}:")
        for _, _, name in BANDS:
            if tot[name] >= 150:
                p_, lo, hi = wilson(k[name], tot[name])
                print(f"         {name:12} n={tot[name]:5,}  {p_:5.2f}%  [{lo:.2f}, {hi:.2f}]")

    print(f"\n  E. NEWNESS / REFRESH  (published: median label refreshed 23%; "
          f"22 of 139 dormant)")
    first_seen = {}
    for d in era:
        for p, r in direct(snaps[d]).items():
            first_seen.setdefault(p, d)
    arrivals = collections.Counter()
    arrivals_true = collections.Counter()
    for p, d in first_seen.items():
        if d > ERA_START:
            b = snaps[d][p]["brand"]
            arrivals[b] += 1
            # A TRUE arrival also has to be new to the whole archive, not just to
            # the era — and must not be a re-entry of a piece we lost.
            if p not in seen_before.get(d, set()):
                arrivals_true[b] += 1
    shelf_now = collections.Counter(r["brand"] for r in direct(E0).values())
    news, news_true = [], []
    for b, n in shelf_now.items():
        if b in roster and n >= 20:
            news.append(100 * arrivals[b] / n)
            news_true.append(100 * arrivals_true[b] / n)
    dormant = sum(1 for b, n in shelf_now.items()
                  if b in roster and n >= 20 and arrivals[b] == 0)
    measured = sum(1 for b, n in shelf_now.items() if b in roster and n >= 20)
    print(f"     labels measured                 : {measured}")
    print(f"     median refresh, as published    : {statistics.median(news):5.0f}%")
    print(f"     median refresh, re-entries removed: {statistics.median(news_true):5.0f}%")
    print(f"     dormant (0 arrivals)            : {dormant} of {measured} "
          f"= {100*dormant/max(measured,1):.0f}%")
    print("     (dormancy is a FLOOR and survives rotation: rotation can only ADD")
    print("      spurious arrivals, never remove real ones)")
    top = sorted(((100 * arrivals[b] / shelf_now[b], b, shelf_now[b], arrivals[b],
                   arrivals_true[b]) for b in shelf_now if b in roster and shelf_now[b] >= 20),
                 reverse=True)[:8]
    print("     'refreshing fastest', as published vs with re-entries removed:")
    for r_, b, n, a, at in top:
        print(f"       {b[:26]:26} shelf {n:3}  arrivals {a:4} = {r_:5.0f}%   "
              f"true-new {at:4} = {100*at/n:5.0f}%")

    print(f"\n  F. PRICE DISCIPLINE  (published: 98.4% held full price, "
          f"1.6% cut, n=8,219)")
    # Same construction as build_loupe_index: one clean epoch, tracked = enough
    # observations in the longest clean run. The published version requires 5.
    clean = [d for d in days
             if sum(1 for e in PRICE_EPOCHS if d >= e) == sum(1 for e in PRICE_EPOCHS
                                                              if ENDPOINT_OLD >= e)
             and not any(e <= d < (dt.date.fromisoformat(e)
                                   + dt.timedelta(EPOCH_SETTLE_DAYS)).isoformat()
                         for e in PRICE_EPOCHS)
             and d <= ENDPOINT_OLD]
    series = collections.defaultdict(list)
    for d in clean:
        for p, r in direct(snaps[d]).items():
            v = float(r["price"] or 0)
            if v > 0:
                series[p].append((d, v))
    print(f"     clean comparison days           : {len(clean)} "
          f"({clean[0]} -> {clean[-1]})")
    print("     NOT a restatement of 98.4%. The published figure additionally runs")
    print("     detect_uniform_steps(), which voids brand-days where a currency")
    print("     correction moved a whole line by one ratio (Stine Goya x0.134 etc).")
    print("     This block does not, so its ABSOLUTE rate is higher for a reason that")
    print("     has nothing to do with rotation. Only the two rows below are")
    print("     comparable to each other, and that comparison is the whole point:")
    print("     it asks whether the pieces the cap DROPS are marked down differently")
    print("     from the pieces it keeps.")
    buckets = {"2-4 observations (rotates off)": (2, 4),
               "5+ observations (as published)": (5, 10 ** 6)}
    for lbl, (lo_, hi_) in buckets.items():
        n = k = 0
        for p, pts in series.items():
            if not (lo_ <= len(pts) <= hi_):
                continue
            n += 1
            if pts[-1][1] < pts[0][1] * (1 - MIN_MEANINGFUL_MOVE):
                k += 1
        p_, l_, h_ = wilson(k, n)
        print(f"     {lbl:32}: n={n:6,}  cut {k:4} = {p_:5.2f}% "
              f"[{l_:.2f}, {h_:.2f}]  -> held {100-p_:5.2f}%")
    tracked5 = [p for p, pts in series.items() if len(pts) >= 5]
    capped_now = {b for b in shelf_max if shelf_max[b] >= NEAR_CAP}
    inc = sum(1 for p in tracked5 if snaps[ENDPOINT_OLD].get(p, {}).get("brand") in capped_now)
    everseen = len({p for d in clean for p in direct(snaps[d])})
    print(f"     pieces excluded by the 5-observation rule: "
          f"{everseen - len(tracked5):,} of {everseen:,} "
          f"({100*(everseen-len(tracked5))/max(everseen,1):.0f}%)")
    print(f"     of the tracked set, share on capped brands: "
          f"{100*inc/max(len(tracked5),1):.0f}%")

    print(f"\n  G. WHY RANKINGS BY 'REFRESH' ARE DISTORTED")
    newflag = collections.Counter()
    tot_b = collections.Counter()
    for p, r in direct(E0).items():
        if not r["addedAt"]:
            continue
        age = (dt.date.fromisoformat(ENDPOINT_OLD)
               - dt.date.fromisoformat(r["addedAt"])).days
        tot_b[r["brand"] in capped_now] += 1
        newflag[r["brand"] in capped_now] += age <= 14
    for is_cap in (True, False):
        p_, l_, h_ = wilson(newflag[is_cap], tot_b[is_cap])
        print(f"     brands {'AT' if is_cap else 'never at'} the cap: "
              f"{newflag[is_cap]:,}/{tot_b[is_cap]:,} flagged new = {p_:5.1f}% "
              f"[{l_:.1f}, {h_:.1f}]")
    print("     A cluster or category whose brands sit at the cap therefore scores")
    print("     'newer' for a reason that is about our sampler. Any RANK by refresh")
    print("     rate — 'the 2nd-lowest refresh' — inherits that distortion.")

    # ── 4. RISK MODEL ─────────────────────────────────────────────────────
    rule("4. RISK MODEL — re-fit, strictly out of time")
    print("  The published model's strongest single feature was BRAND PRIOR CHURN,")
    print("  which is an ABSENCE measure — i.e. the contaminated one. The corrected")
    print("  model swaps it for a prior built on the availability flag. Everything")
    print("  is trained on one window and tested on a strictly later one.\n")

    def brand_prior_churn(day, prev):
        S, P = snaps[day], snaps[prev]
        cnt = collections.Counter(r["brand"] for r in P.values())
        gonec = collections.Counter(r["brand"] for p, r in P.items() if p not in S)
        return {b: gonec.get(b, 0) / n for b, n in cnt.items()}

    def brand_prior_flip(day, prev):
        """Rotation-immune brand prior: of that brand's pieces in stock at `prev`
        and still present at `day`, what share went out of stock?"""
        risk, out = outcomes(snaps[prev], snaps[day], "FLIP")
        tot, k = collections.Counter(), collections.Counter()
        for p in risk:
            b = snaps[prev][p]["brand"]
            tot[b] += 1
            k[b] += p in out
        # shrink toward the global rate so a 3-piece brand cannot dominate
        g = sum(k.values()) / max(sum(tot.values()), 1)
        return {b: (k[b] + 10 * g) / (tot[b] + 10) for b in tot}

    def features(day, prev, prior, use_cap_feats):
        S = snaps[day]
        sizes = collections.Counter(r["brand"] for r in S.values())
        gp = statistics.mean(prior.values()) if prior else 0.0
        rows, ids = [], []
        for p, r in S.items():
            pr = float(r["price"] or 0)
            if pr <= 0:
                continue
            added = r["addedAt"]
            age = ((dt.date.fromisoformat(day) - dt.date.fromisoformat(added)).days
                   if added else 45)
            f = [math.log(pr), min(age, 60) / 60.0, 1.0 if age <= 14 else 0.0,
                 prior.get(r["brand"], gp),
                 1.0 if r["category"] == "dresses" else 0.0,
                 1.0 if r["category"] == "accessories" else 0.0]
            if use_cap_feats:
                f += [1.0 if sizes[r["brand"]] >= NEAR_CAP else 0.0,
                      math.log1p(sizes[r["brand"]])]
            rows.append(f)
            ids.append(p)
        return np.array(rows), ids

    def xy(bd, ed, pd_, prior_fn, use_cap_feats):
        prior = prior_fn(bd, pd_)
        X, ids = features(bd, pd_, prior, use_cap_feats)
        risk, out = outcomes(snaps[bd], snaps[ed], "FLIP")
        keep = [i for i, p in enumerate(ids) if p in risk]
        return X[keep], np.array([ids[i] in out for i in keep]), [ids[i] for i in keep]

    SPLITS = [
        ("published split", ("2026-07-16", "2026-07-23", "2026-07-09"),
         ("2026-07-23", "2026-08-01", "2026-07-16")),
    ]
    if "2026-08-05" in snaps and "2026-07-30" in snaps:
        SPLITS.append(("fresh out-of-time", ("2026-07-16", "2026-07-23", "2026-07-09"),
                       ("2026-07-30", "2026-08-05", "2026-07-23")))

    NAMES6 = ["log price", "age/60", "is new (<=14d)", "brand prior",
              "is dress", "is accessory"]
    NAMES8 = NAMES6 + ["brand at cap", "log brand size"]

    for split_name, (tb, te_, tp), (vb, ve, vp) in SPLITS:
        for d in (tb, te_, tp, vb, ve, vp):
            if d not in snaps:
                print(f"  {split_name}: missing snapshot {d}, skipped")
                break
        else:
            print(f"\n  ── {split_name}: train {tb}->{te_}, test {vb}->{ve} ──")
            for mlabel, prior_fn, capf in (
                    ("AS PUBLISHED (churn prior + cap features)", brand_prior_churn, True),
                    ("CORRECTED  (availability prior, no cap features)", brand_prior_flip, False)):
                Xtr, ytr, idtr = xy(tb, te_, tp, prior_fn, capf)
                Xte, yte, idte = xy(vb, ve, vp, prior_fn, capf)
                names = NAMES8 if capf else NAMES6
                print(f"\n    {mlabel}")
                print(f"      train n={len(ytr):,} events={int(ytr.sum())} "
                      f"({100*ytr.mean():.2f}%)   test n={len(yte):,} "
                      f"events={int(yte.sum())} ({100*yte.mean():.2f}%)")
                for j, nm in enumerate(names):
                    v = auc(Xte[:, j], yte)
                    arrow = "" if abs(v - .5) < .02 else (" (higher -> sells out)" if v > .5
                                                          else " (LOWER -> sells out)")
                    print(f"        {nm:22} AUC {v:.3f}{arrow}")
                mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
                w = logistic_fit((Xtr - mu) / sd, ytr)
                pte = logistic_predict(w, (Xte - mu) / sd)
                a_all = auc(pte, yte)
                order = np.argsort(pte)[::-1]
                k10 = int(len(pte) * .10)
                k20 = int(len(pte) * .20)
                l10 = yte[order[:k10]].mean() / max(yte.mean(), 1e-9)
                l20 = yte[order[:k20]].mean() / max(yte.mean(), 1e-9)
                print(f"        ridge logistic OUT-OF-TIME AUC = {a_all:.3f}")
                print(f"        top 10% by predicted risk: "
                      f"{100*yte[order[:k10]].mean():.2f}% vs base "
                      f"{100*yte.mean():.2f}% = {l10:.2f}x lift")
                print(f"        top 20%:                   "
                      f"{100*yte[order[:k20]].mean():.2f}% = {l20:.2f}x lift")

    # sell-out by price quintile, the sellable table
    print("\n  SELL-OUT BY PRICE QUINTILE (test window, FLIP outcome)")
    for bd, ed in (("2026-07-23", "2026-08-01"), ("2026-07-30", ENDPOINT)):
        if bd not in snaps or ed not in snaps:
            continue
        risk, out = outcomes(snaps[bd], snaps[ed], "FLIP")
        pr = {p: float(snaps[bd][p]["price"] or 0) for p in risk}
        vals = [v for v in pr.values() if v > 0]
        qs = np.quantile(vals, [.2, .4, .6, .8])
        buckets = collections.defaultdict(lambda: [0, 0])
        for p in risk:
            if pr[p] <= 0:
                continue
            i = int(np.searchsorted(qs, pr[p]))
            buckets[i][0] += 1
            buckets[i][1] += p in out
        edges = [0] + list(qs) + [max(vals)]
        print(f"    {bd} -> {ed}")
        for i in sorted(buckets):
            n, ev = buckets[i]
            p_, lo, hi = wilson(ev, n)
            print(f"      ${edges[i]:>6,.0f}-${edges[i+1]:>6,.0f}  n={n:5,}  "
                  f"{p_:5.2f}%  [{lo:.2f}, {hi:.2f}]")

    rule("DONE")


if __name__ == "__main__":
    main()
