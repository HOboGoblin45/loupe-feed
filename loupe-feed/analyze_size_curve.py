#!/usr/bin/env python3
"""Loupe — is the SIZE CURVE a business? Reconstructed from the catalog's git log.

WHY THIS EXISTS

Every product row Loupe has ever published carries `sizes`: the size labels that
were IN STOCK on the day of the scrape, recomputed from the store's own variant
list on every run (build_catalog.py: available_sizes() at ~line 1313, product
`available` at ~line 1440). It has been committed daily for months and nothing
has ever looked at it.

Everything measured so far used the product-level boolean instead, and it is
thin: 3.04% of products sell out in 16 days. A model built on that returned an
out-of-time AUC of 0.519 with a top decile of 0.92x — worse than random
(analyze_demand_signal.py section 7; read that file before this one, its five
guards apply here unchanged).

The intuition this script exists to test: when a dress goes from six available
sizes to two, four sizes sold. That is a denser signal, and it should LEAD the
product-level flag rather than coincide with it. If it does, Loupe can tell a
brand which piece is going and how fast, which is a sentence a brand pays for.

It is deliberately built to be able to return NO, and on the incremental
question it does.

────────────────────────────────────────────────────────────────────────────
WHAT IT FOUND, IN ONE PARAGRAPH, SO NOBODY HAS TO READ THE WHOLE OUTPUT
────────────────────────────────────────────────────────────────────────────

The density gain is real and large: ~5x more events than the product-level
flag. The DIRECTION of the size curve is worth nothing. Once the model knows
how many sizes are in stock today and whether the listing's flags move at all,
being told that the move was DOWNWARD rather than upward adds -0.003 AUC on
sell-out and +0.003 on further movement — i.e. nothing, twice, out of time. A
restock predicts sell-out as strongly as a depletion does (RR 2.66 vs 2.63).
What the size curve measures is LIVENESS, not demand.

But the level does predict, and hard. The number of sizes currently in stock —
a field that has been sitting in every row for months and has never been used —
takes the same out-of-time model from AUC 0.492 (coin flip, the prior model's
information) to 0.829, with a 4.42x top decile. One size left means a 9.15%
chance of being gone in thirteen days; six or more means 0.00%.

So: the size array IS worth building on. Just not the story anyone would have
told about it.

────────────────────────────────────────────────────────────────────────────
THE EIGHT THINGS THAT WILL FOOL YOU HERE
────────────────────────────────────────────────────────────────────────────

1.  `sizes` DOES NOT SPAN THE ARCHIVE.

    The catalog begins 2026-06-17. `sizes` first appears on 2026-06-23 (build
    34). Six of the archive's oldest days carry no size information at all, and
    a script that walked "the whole archive" would silently analyse a shorter
    period than it claimed. The era boundary is DETECTED here, not assumed, and
    printed before any result.

    `available` is younger still — first snapshot 2026-07-16 — which is why the
    outcome below is defined on the size set rather than on the flag. See guard
    3 for why that substitution is exact rather than approximate.

2.  THE PER-BRAND CAP MAKES "GONE" MEAN "NOT SAMPLED TODAY".

    Until 2026-08-06 build_catalog.py took the 60 most recently published items
    per store, and 88 of 173 brands sat at that cap, so their tracked shelf
    ROTATED. Whole-brand rotation accounted for 981 of 2,041 disappearances
    between 2026-07-16 and 2026-08-01 — 48%. A product leaving the sampling
    window is not a size change.

    GUARD: every comparison here is restricted to products present in BOTH
    endpoints, by id. A rotated-out product contributes nothing. This costs
    about a third of the catalog per window and is not negotiable.

3.  AN EMPTY SIZE ARRAY MEANS TWO DIFFERENT THINGS.

    available_sizes() returns [] both for a one-size item with no size option
    and for a sized item whose every size has sold. Roughly 37% of the catalog
    has no size option at all, so `sizes == []` on its own is not a sell-out.

    The disambiguation is exact and was verified, not assumed. Measured on
    2026-07-16 and 2026-08-05: the count of rows with a NON-EMPTY size set and
    `available == false` is zero, on both days. And of the 95 pieces that had
    sizes on 2026-07-16 and none on 2026-08-01, 95 carry `available == false`
    at the endpoint, against 0 of the 3,424 that still had sizes. So for a piece
    that HAD a size option, "the size set emptied" is not a proxy for the
    product-level sell-out — it is the same measurement, and it is available
    for the 24 days before `available` was ever written. That is what makes an
    honest out-of-time split possible at all. The check is re-run every time,
    below, because it is an empirical fact about the data and not a guarantee.

4.  THE SIZE VOCABULARY IS CHAOS, BUT NOT WHERE YOU EXPECT.

    642 distinct raw labels across the era: 'M' and 'Medium' and 'MEDIUM', 'UK8'
    and 'UK 8', 'S- PREORDER', '17 PRO' (a phone case), 'Navy' and 'Dolphin
    Blue' (a store whose colour option is named "Size"), and — the one that
    actually bites — 'OS - 1 unit left', a label that MUTATES as stock changes
    and therefore manufactures a remove-plus-add pair out of nothing.

    Normalising is still worth doing and is done below. But measure the payoff
    before believing the story: it removes only 4.0% of raw label losses, and
    only 13 products in a 16-day window had their size set replaced wholesale.
    Vocabulary chaos is loud and nearly harmless. The expensive noise is
    elsewhere — see guard 5.

5.  A SIZE THAT VANISHES COMES BACK 22.6% OF THE TIME, AND THE REVERSALS ARE
    CONCENTRATED IN A HANDFUL OF STORES.

    Of 5,974 day-to-day size-label losses, 1,349 are back within 7 snapshots.
    Ten brands hold 63% of all short-gap flicker — Orseund Iris alone produces
    10.4 flicker events per tracked piece. Any product that alerts on a
    day-to-day size disappearance will fire mostly on those stores.

    GUARD: nothing here is measured day-to-day. Every claim is endpoint to
    endpoint, and a "restock" only counts when the size was absent for at least
    3 consecutive snapshots first.

6.  THE GRACE WINDOW FREEZES SIZES.

    When a store's scrape fails, build_catalog.py carries yesterday's row
    forward and stamps `stale: true`, with price and sizes frozen at the last
    good scrape (build_catalog.py ~line 2070). A frozen size set is not
    evidence of no depletion. Rows stale at EITHER endpoint are dropped. The
    counts are small (0-32/day) but the trinket incident of 2026-08-01 is the
    precedent: the grace window silently restored everything a filter removed,
    because a rule applied only to the live path is a rule the grace window
    undoes.

7.  DIRECTION-FREE VOLATILITY BEATS THE HYPOTHESIS, AND IF YOU DO NOT TEST FOR
    IT YOU WILL SHIP THE WRONG CLAIM.

    The obvious analysis — "did it lose a size, and did it then sell out" —
    returns a crude RR of 2.63 and a brand-stratified 2.05, which reads like a
    finding. It is not. A piece that GAINED a size in the same window sells out
    at RR 2.66, and the direction-free count of daily set changes out-predicts
    the directional feature (AUC 0.577 vs 0.556). Add direction to a model that
    already has volatility and the out-of-time AUC moves by -0.003.

    So the NEGATIVE CONTROL is run every time and printed next to the estimate.
    Without it this file would say the opposite of what is true.

8.  A CRUDE NUMBER HERE IS PRESUMED WRONG.

    On 2026-08-01 an unstratified version of the sibling question produced a
    confident p<0.001 result pointing the WRONG WAY, via Simpson's paradox.
    Every association below is reported crude AND brand-stratified, every AUC
    is reported pooled AND within-brand, and the two are printed side by side so
    a disagreement is visible rather than averaged away. Row counts that land
    exactly on a round number are flagged as suspected truncation rather than
    read as data (a PostHog query once returned exactly 100 rows for a
    1,581-row answer and produced a well-formed, entirely wrong 2x2).

WHAT THIS CAN NEVER BE

An availability flag is not units sold, not revenue, and not demand. Eight
sizes selling out on a piece stocked one-deep is eight units. A made-to-order
piece may never show a stockout however well it sells — measured below, 12 of
142 brands with 5+ sized pieces changed no size set at all in 43 days, and for
those brands every number in this file is structurally blind.

USAGE
    python analyze_size_curve.py               # full run
    python analyze_size_curve.py --refresh     # ignore the cache, re-extract
    python analyze_size_curve.py --quick       # skip the 24-window sweep
    python analyze_size_curve.py --allow-shallow
"""

import argparse
import collections
import csv
import datetime as dt
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import numpy as np
from scipy import optimize, stats

# Non-ASCII brand names are the rule, not the exception (SIEDRÉS, DémodéMODÉ,
# With Jéan). A Windows console defaults to cp1252 and a print of a brand table
# dies on the first accented character AFTER the expensive part of the run.
# Reconfigure once, up front, rather than losing a five-minute walk to a
# UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
CATALOG_REL = "loupe-feed/catalog.json"

# Outside the repo on purpose: ~15 MB of extracted snapshots, 100% re-derivable
# from git in a couple of minutes, and a public repo does not need them.
CACHE = pathlib.Path(tempfile.gettempdir()) / "loupe_size_cache"

PER_BRAND_CAP = 60          # brands.json perBrand before 2026-08-06; see guard 2
ALPHA = 0.05
POWER = 0.80

# The window the published 3.04%/16d product-level figure was measured on. Pinned
# so this file's density claim is checkable against the number it is arguing
# with, rather than against a differently-cut window that happens to look better.
PRIOR_WINDOW = ("2026-07-16", "2026-08-01")

# The out-of-time split. Geometry is identical on both sides — 7 days of feature,
# 13 days of outcome — so the two halves are comparable, and the train outcome
# window ENDS on the day the test feature window BEGINS, so nothing leaks.
# 13 rather than 14 because the refresh workflow did not run on 2026-07-25..28
# and every window has to land on days that exist.
TRAIN_SPLIT = ("2026-06-26", "2026-07-03", "2026-07-16", "2026-06-24")
TEST_SPLIT = ("2026-07-16", "2026-07-23", "2026-08-05", "2026-07-12")
#              t0 (feature   t1 (feature   t2 (outcome   prior day, used only
#               start)        end / index)  end)         for the brand-churn feature

FEATURE_DAYS = 7
OUTCOME_DAYS = 13

# A size is only "restocked" if it was absent this many consecutive snapshots
# first. Below it, guard 5: 22.6% of day-to-day losses reverse and ten stores
# own most of them.
RESTOCK_MIN_ABSENCE = 3

LETTER_SCALE = ["XXS", "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL"]
LETTER_IDX = {s: i for i, s in enumerate(LETTER_SCALE)}


# ══════════════════════════════════════════════════════════════════════════
# git / archive
# ══════════════════════════════════════════════════════════════════════════

def git(*args):
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


def history_is_truncated():
    """True when this clone cannot see the repo's whole history.

    Guard 3 of analyze_demand_signal.py, restated because it is the one failure
    with no symptom: on 2026-08-01 a shallow clone cost build_price_history.py
    14 of 42 days and emitted a well-formed file that read as a statement about
    the data. It was a statement about the clone.
    """
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        return True
    git_dir = git("rev-parse", "--git-dir").strip()
    if not git_dir:
        return False
    return (REPO / git_dir / "shallow").exists() or pathlib.Path(git_dir, "shallow").exists()


def daily_snapshots():
    """(day, sha) for the LAST catalog commit of each day, oldest first.

    Same implementation as build_price_history.daily_snapshots(); duplicated
    rather than imported because that module writes price_history.json on
    import-time constants and this script must never be able to touch it.
    """
    out = {}
    for line in git("log", "--format=%H|%ad", "--date=short", "--", CATALOG_REL).splitlines():
        if "|" not in line:
            continue
        sha, day = line.split("|", 1)
        out.setdefault(day.strip(), sha.strip())   # log is newest-first
    return sorted(out.items())


FIELDS = ["id", "brand", "price", "category", "available", "addedAt",
          "stale", "retailer", "sizes", "hasSizesKey"]
SEP = "\x1f"    # size labels contain commas, slashes, spaces and dashes; a unit
                # separator is the only delimiter none of them can be


def _cache_is_current(dest):
    """True when a cached TSV was written by THIS version of the extractor.

    A cache file from an older column layout loads without error and then fails
    a thousand lines later on a missing key — or worse, silently reads the wrong
    column. The header is the cheapest possible version check, so it is checked
    rather than assumed.
    """
    try:
        with open(dest, encoding="utf-8", newline="") as fh:
            head = next(csv.reader(fh, delimiter="\t"), None)
    except OSError:
        return False
    return head == FIELDS


def extract_snapshots(refresh=False, verbose=True):
    """Materialise one compact TSV per snapshot day (~25x smaller than the JSON)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    snaps = daily_snapshots()
    for day, sha in snaps:
        dest = CACHE / f"{day}.tsv"
        if dest.exists() and not refresh and _cache_is_current(dest):
            continue
        raw = git("show", f"{sha}:{CATALOG_REL}")
        try:
            doc = json.loads(raw)
        except ValueError:
            print(f"  {day}: unparseable snapshot, SKIPPED", file=sys.stderr)
            continue
        tmp = dest.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(FIELDS)
            for p in doc.get("products", []):
                av = p.get("available")
                sz = p.get("sizes")
                w.writerow([
                    p.get("id", ""), p.get("brand", ""), p.get("price", ""),
                    p.get("category", ""),
                    "" if av is None else ("1" if av else "0"),
                    (p.get("addedAt") or "")[:10],
                    "1" if p.get("stale") else "",
                    p.get("retailer") or "",
                    SEP.join(str(s) for s in (sz or [])),
                    "1" if sz is not None else "",
                ])
        os.replace(tmp, dest)
        if verbose:
            print(f"  extracted {day}  {len(doc.get('products', [])):>5} products",
                  file=sys.stderr)
    return [d for d, _ in snaps]


# ══════════════════════════════════════════════════════════════════════════
# size normalisation (guard 4)
# ══════════════════════════════════════════════════════════════════════════

# Spelled-out sizes and the X-count spellings. Deliberately small: the goal is
# to make 'Medium' and 'M' the same label, NOT to invent a universal size
# ontology. Mapping 'IT 40' to 'S' would be a fashion opinion, and a wrong one
# for half the brands here, so numeric and regional scales are left alone —
# they only ever have to match themselves, within one product, across two days.
_SPELLED = {
    "XXSMALL": "XXS", "EXTRAEXTRASMALL": "XXS",
    "XSMALL": "XS", "EXTRASMALL": "XS",
    "SMALL": "S", "MEDIUM": "M", "LARGE": "L",
    "XLARGE": "XL", "EXTRALARGE": "XL",
    "XXLARGE": "2XL", "XXL": "2XL",
    "XXXL": "3XL", "XXXXL": "4XL",
}

# Merchandising text stores append to a size value. 'OS - 1 unit left' is the
# dangerous one: it changes as stock changes, so leaving it in makes a size
# appear to vanish and a new size appear, on a piece where nothing happened
# except that the store sold one.
_NOISE_RE = re.compile(
    r"(-?\s*PRE-?ORDER)"
    r"|(\d+\s*UNITS?\s*LEFT)"
    r"|(SOLDOUT)|(LOWSTOCK)|(FINALSALE)|(BACKORDER)"
)


def norm_size(value):
    """One size label, normalised. Returns '' for a label that normalises away."""
    t = str(value or "").upper()
    t = t.replace("–", "-").replace("—", "-").replace("’", "'")
    t = re.sub(r"\s+", "", t)          # 'UK 8' and 'UK8' are one size
    t = _NOISE_RE.sub("", t)
    t = t.strip(" -/.,")
    return _SPELLED.get(t, t)


def size_set(row):
    """The normalised set of in-stock size labels for one product row."""
    raw = row["sizes"]
    if not raw:
        return set()
    return {n for n in (norm_size(x) for x in raw.split(SEP) if x) if n}


def raw_sizes(row):
    return [x for x in row["sizes"].split(SEP) if x] if row["sizes"] else []


# ══════════════════════════════════════════════════════════════════════════
# loading
# ══════════════════════════════════════════════════════════════════════════

_DAYS = {}


def load_day(day):
    """id -> row, with the normalised size set precomputed. Memoised."""
    if day in _DAYS:
        return _DAYS[day]
    out = {}
    with open(CACHE / f"{day}.tsv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            r["ns"] = size_set(r)
            out[r["id"]] = r
    _DAYS[day] = out
    return out


def usable(row_b, row_e):
    """Both endpoints readable: neither is a grace-carried freeze (guard 6)."""
    return not row_b["stale"] and not row_e["stale"]


# ══════════════════════════════════════════════════════════════════════════
# statistics
# ══════════════════════════════════════════════════════════════════════════

def rr_ci(a, n1, c, n0, alpha=ALPHA):
    p1 = a / n1 if n1 else float("nan")
    p0 = c / n0 if n0 else float("nan")
    if not a or not c:
        return p1, p0, float("nan"), float("nan"), float("nan")
    z = stats.norm.ppf(1 - alpha / 2)
    se = math.sqrt(1 / a - 1 / n1 + 1 / c - 1 / n0)
    rr = p1 / p0
    return p1, p0, rr, math.exp(math.log(rr) - z * se), math.exp(math.log(rr) + z * se)


def mh_rr(strata, alpha=ALPHA):
    """Mantel-Haenszel pooled risk ratio, Greenland-Robins variance.

    Strata with no exposed OR no unexposed contribute nothing. That is the point:
    a brand whose store never changes a variant flag (12 of them, see section 3)
    carries no within-brand information and must not be allowed to move the
    estimate by being large.
    """
    R = S = V = 0.0
    used = 0
    for a, b, c, d in strata:
        n1, n0 = a + b, c + d
        N = n1 + n0
        if N == 0 or n1 == 0 or n0 == 0:
            continue
        used += 1
        R += a * n0 / N
        S += c * n1 / N
        V += (n1 * n0 * (a + c) - a * c * N) / (N * N)
    if R == 0 or S == 0:
        return float("nan"), float("nan"), float("nan"), used
    rr = R / S
    se = math.sqrt(V / (R * S))
    z = stats.norm.ppf(1 - alpha / 2)
    return rr, math.exp(math.log(rr) - z * se), math.exp(math.log(rr) + z * se), used


def mde_rr(n1, n0, p0, alpha=ALPHA, power=POWER):
    """Smallest risk ratio (>1) this cell could detect at 80% power.

    Printed BEFORE the estimate wherever the cell is small. If the design cannot
    see anything short of a tripling, that sentence outranks any p-value.
    """
    if n1 < 1 or n0 < 1 or not 0 < p0 < 1:
        return float("nan")
    za, zb = stats.norm.ppf(1 - alpha / 2), stats.norm.ppf(power)

    def f(p1):
        se = math.sqrt(p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
        return (p1 - p0) / se - (za + zb)

    hi = min(0.999, p0 * 25 + 1e-6)
    if f(hi) < 0:
        return float("inf")
    return optimize.brentq(f, p0 + 1e-9, hi) / p0


def auc(scores, labels):
    """Rank-based AUC (Mann-Whitney). NaN when one class is empty."""
    s = np.asarray(scores, float)
    l = np.asarray(labels, bool)
    pos, neg = s[l], s[~l]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    r = stats.rankdata(np.concatenate([pos, neg]))
    return (r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def auc_ci(scores, labels, nboot=500, seed=0):
    """Bootstrap percentile CI. 500 resamples: enough to see whether 0.50 is
    inside, not enough to quote a third decimal, and the numbers here do not
    deserve a third decimal."""
    a = auc(scores, labels)
    s = np.asarray(scores, float)
    l = np.asarray(labels, bool)
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(nboot):
        k = rng.integers(0, len(s), len(s))
        v = auc(s[k], l[k])
        if not math.isnan(v):
            vals.append(v)
    if not vals:
        return a, float("nan"), float("nan")
    return a, float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def stratified_auc(scores, labels, groups):
    """Within-stratum AUC, pooled with n_pos*n_neg weights.

    Guard 8. The crude AUC compares a product at one brand against a product at
    another, so it can be driven entirely by brand composition. This one only
    ever compares two products from the SAME brand.
    """
    by = collections.defaultdict(lambda: ([], []))
    for s, l, g in zip(scores, labels, groups):
        by[g][0].append(s)
        by[g][1].append(l)
    num = den = 0.0
    used = 0
    for _, (s, l) in by.items():
        s = np.asarray(s, float)
        l = np.asarray(l, bool)
        npos, nneg = int(l.sum()), int((~l).sum())
        if npos == 0 or nneg == 0:
            continue
        used += 1
        num += auc(s, l) * npos * nneg
        den += npos * nneg
    return (num / den if den else float("nan")), used


def decile_lift(pred, y, frac=0.10, nboot=500, seed=0):
    pred = np.asarray(pred, float)
    y = np.asarray(y, float)
    k = max(1, int(len(pred) * frac))
    order = np.argsort(pred)[::-1]
    base = y.mean()
    lift = y[order[:k]].mean() / base if base > 0 else float("nan")
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(nboot):
        i = rng.integers(0, len(y), len(y))
        m = y[i].mean()
        if m <= 0:
            continue
        o = np.argsort(pred[i])[::-1][:k]
        vals.append(y[i][o].mean() / m)
    if not vals:
        return lift, float("nan"), float("nan"), y[order[:k]].mean(), base
    return (lift, float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5)),
            y[order[:k]].mean(), base)


def logistic_fit(X, y, l2=2.0):
    """Ridge logistic regression, numpy + scipy only (statsmodels is not
    installed on the founder's machine and this has to run there)."""
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


ROUND_NUMBERS = {100, 200, 250, 500, 1000, 2000, 5000, 10000, 50000}


def check_not_round(n, label):
    """Guard 8. A count landing exactly on a page-size boundary is treated as
    suspected truncation, not as data. A HogQL query once returned exactly 100
    rows for a 1,581-row answer and produced a confident, wrong 2x2."""
    if n in ROUND_NUMBERS:
        print(f"  !! {label} = {n} lands exactly on a round number. Verify this is "
              "not a truncated read before quoting it.")


# ══════════════════════════════════════════════════════════════════════════
# reporting helpers
# ══════════════════════════════════════════════════════════════════════════

def rule(title=""):
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def fmt_ci(rr, lo, hi):
    if any(math.isnan(x) for x in (rr, lo, hi)):
        return "  n/a (a cell is empty)"
    return f"{rr:5.2f}  95% CI [{lo:4.2f}, {hi:5.2f}]"


# ══════════════════════════════════════════════════════════════════════════
# study construction
# ══════════════════════════════════════════════════════════════════════════

def brand_strata(rows, treated_key, outcome_key, extra_key=None):
    """(a, b, c, d) cells per stratum, for mh_rr."""
    cells = collections.defaultdict(lambda: [0, 0, 0, 0])
    for r in rows:
        k = r["brand"] if extra_key is None else (r["brand"], extra_key(r))
        t = bool(r[treated_key])
        y = bool(r[outcome_key])
        cells[k][(0 if t else 2) + (0 if y else 1)] += 1
    return [tuple(v) for v in cells.values()]


def build_window(t0, t1, t2, prior_day, all_days):
    """One (feature window -> outcome window) design matrix.

    RISK SET: a product present and readable on all three days, with a non-empty
    normalised size set at t0 AND at t1. Present-at-all-three is guard 2 (the
    sampler cannot manufacture an event on a piece it is still holding at both
    ends). Non-empty at t1 is the risk-set definition: a piece already at zero
    sizes cannot sell out again.

    This conditions on surviving to t2, which is a collider. The sibling script
    established that treatment does not predict disappearance (brand-stratified
    RR ~1.00), so the conditioning is not differential on the exposure and the
    bias is not material — but it is a conditioning, and it is stated.
    """
    A, B, C = load_day(t0), load_day(t1), load_day(t2)
    Pv = load_day(prior_day)

    # Brand churn measured BEFORE t1 only, so the feature is observable on the
    # day the prediction would be made.
    cnt = collections.Counter(r["brand"] for r in Pv.values())
    gone = collections.Counter(r["brand"] for p, r in Pv.items() if p not in B)
    churn = {b: gone.get(b, 0) / n for b, n in cnt.items()}
    bsize = collections.Counter(r["brand"] for r in B.values())

    mids = [d for d in all_days if t0 <= d <= t1]

    rows = []
    for pid, r1 in B.items():
        if pid not in A or pid not in C:
            continue
        if A[pid]["stale"] or r1["stale"] or C[pid]["stale"]:
            continue
        s0, s1, s2 = A[pid]["ns"], r1["ns"], C[pid]["ns"]
        if not s0 or not s1:
            continue

        # Direction-free volatility inside the feature window: how many days the
        # set CHANGED at all, and how many labels moved in either direction.
        # This is the negative control of guard 7 and it has to be built from the
        # same window as the directional feature or the comparison is unfair.
        prev = None
        n_changes = n_moved = 0
        for d in mids:
            S = load_day(d)
            if pid not in S or S[pid]["stale"]:
                prev = None
                continue
            if prev is not None and prev != S[pid]["ns"]:
                n_changes += 1
                n_moved += len(prev ^ S[pid]["ns"])
            prev = S[pid]["ns"]

        added = r1["addedAt"]
        try:
            age = (dt.date.fromisoformat(t1) - dt.date.fromisoformat(added)).days if added else 45
        except ValueError:
            age = 45

        rows.append({
            "id": pid,
            "brand": r1["brand"],
            "n0": len(s0),
            "n1": len(s1),
            "lost": 1 if (s0 - s1) else 0,
            "frac_lost": len(s0 - s1) / len(s0),
            "gained": 1 if (s1 - s0) else 0,
            "vol_days": n_changes,
            "vol_labels": n_moved,
            "price": float(r1["price"] or 0) or 1.0,
            "age": age,
            "is_new": 1 if age <= 14 else 0,
            "brand_churn": churn.get(r1["brand"], 0.0),
            "brand_size": bsize[r1["brand"]],
            "cat": r1["category"],
            # OUTCOMES. y_soldout is the product-level sell-out, expressed on the
            # size set so it exists for the whole sizes era (guard 3).
            "y_soldout": 1 if not s2 else 0,
            "y_deplete": 1 if (s1 - s2) else 0,
        })
    return rows


# Model tiers. The point of the ladder is to price each layer of information
# separately: a +0.34 AUC step and a -0.003 step are different businesses.
TIERS = [
    (0, "M0  price / age / brand churn / category      (the prior model's information)"),
    (1, "M1  = M0 + how many sizes are in stock NOW"),
    (2, "M2  = M1 + DIRECTION-FREE volatility          (the negative control)"),
    (3, "M3  = M2 + the DIRECTION of the move          (the hypothesis)"),
]


def features(rows, tier):
    X = []
    for r in rows:
        v = [math.log(r["price"]),
             min(r["age"], 60) / 60.0,
             float(r["is_new"]),
             r["brand_churn"],
             math.log1p(r["brand_size"]),
             1.0 if r["cat"] == "dresses" else 0.0]
        if tier >= 1:
            v += [float(r["n1"])]
        if tier >= 2:
            v += [float(r["vol_days"]), float(r["vol_labels"])]
        if tier >= 3:
            v += [r["frac_lost"], float(r["lost"]), float(r["gained"])]
        X.append(v)
    return np.array(X)


# ══════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="ignore caches, re-extract")
    ap.add_argument("--quick", action="store_true", help="skip the 24-window sweep")
    ap.add_argument("--allow-shallow", action="store_true")
    args = ap.parse_args()

    # ── 0. VERIFY THE INPUT BEFORE TRUSTING THE OUTPUT ────────────────────
    rule("0. INPUT VERIFICATION")
    truncated = history_is_truncated()
    if truncated and not args.allow_shallow:
        sys.exit(
            "REFUSING TO RUN: shallow/grafted clone. `git fetch --unshallow` first.\n"
            "  On 2026-08-01 a shallow clone silently hid 14 of 42 days from a sibling\n"
            "  script, which then emitted a clean file describing the clone rather than\n"
            "  the data. The whole value of this analysis is its length."
        )
    print(f"  clone complete (not shallow)        : {not truncated}")

    days = extract_snapshots(refresh=args.refresh, verbose=False)
    print(f"  catalog snapshots in git            : {len(days)}  ({days[0]} -> {days[-1]})")

    # GUARD 1: find where `sizes` actually starts. Never assume it spans the archive.
    era_start = None
    coverage = {}
    for d in days:
        S = load_day(d)
        n_key = sum(1 for r in S.values() if r["hasSizesKey"])
        coverage[d] = (len(S), n_key)
        if era_start is None and len(S) and n_key / len(S) > 0.5:
            era_start = d
    if era_start is None:
        sys.exit("REFUSING TO RUN: no snapshot carries `sizes`.")
    sz_days = [d for d in days if d >= era_start]

    pre = [d for d in days if d < era_start]
    print(f"  snapshots with NO `sizes` field     : {len(pre)}"
          + (f"  ({pre[0]} .. {pre[-1]})" if pre else ""))
    print(f"  >>> TRUE USABLE WINDOW              : {sz_days[0]} -> {sz_days[-1]}"
          f"  ({len(sz_days)} snapshots)")
    span = (dt.date.fromisoformat(sz_days[-1]) - dt.date.fromisoformat(sz_days[0])).days + 1
    have = set(sz_days)
    missing = [(dt.date.fromisoformat(sz_days[0]) + dt.timedelta(i)).isoformat()
               for i in range(span)]
    missing = [d for d in missing if d not in have]
    print(f"      calendar days spanned           : {span}   MISSING {len(missing)}: {missing}")
    print("      (the refresh workflow did not run on those days; every window below is")
    print("       measured between snapshot ENDPOINTS, so a gap shortens the ladder but")
    print("       cannot bias a comparison)")

    av_days = [d for d in days if any(r["available"] in ("0", "1") for r in load_day(d).values())]
    print(f"  first snapshot carrying `available` : {av_days[0] if av_days else 'never'}"
          f"   ({len(av_days)} snapshots)")
    print("      -> the product-level flag covers less than half the size era, which is")
    print("         why the outcome below is defined on the size set. Section 1 proves")
    print("         the substitution is exact rather than approximate.")

    # SAMPLING EPOCH. perBrand was raised on 2026-08-06; a window spanning it
    # would compare two different populations.
    if sz_days[-1] >= "2026-08-06":
        print("  !! THIS ARCHIVE NOW SPANS THE 2026-08-06 SAMPLING EPOCH (perBrand raise).")
        print("     Windows crossing it compare two different populations. See")
        print("     build_price_history.SAMPLING_EPOCHS before reading anything below.")
    else:
        print(f"  sampling epoch 2026-08-06           : not yet in the archive "
              f"(ends {sz_days[-1]}), so every window is inside one regime")
    print(f"  per-brand sampling cap in force     : {PER_BRAND_CAP} items/store"
          "  -> every comparison below is restricted to")
    print("                                        products present at BOTH endpoints (guard 2)")

    for d in (*PRIOR_WINDOW, *TRAIN_SPLIT, *TEST_SPLIT):
        if d not in have:
            sys.exit(f"REFUSING TO RUN: no snapshot for {d}, which a pinned window needs.")

    # ── 1. WHAT A SIZE ARRAY IS ───────────────────────────────────────────
    rule("1. WHAT THE SIZE ARRAY IS, AND WHAT AN EMPTY ONE MEANS (guards 3 and 4)")
    end = sz_days[-1]
    E = load_day(end)
    card = collections.Counter(len(r["ns"]) for r in E.values())
    n_end = len(E)
    check_not_round(n_end, f"products on {end}")
    sized_end = sum(v for k, v in card.items() if k >= 1)
    print(f"  {end}: {n_end:,} products, {sized_end:,} carry >=1 in-stock size "
          f"= {100*sized_end/n_end:.1f}%")
    print(f"    no size option at all (or fully gone): {card[0]:,} = {100*card[0]/n_end:.1f}%")
    print("    size-set cardinality:")
    for k in sorted(card):
        if k == 0 or card[k] < 10:
            continue
        print(f"      {k:2d} sizes  {card[k]:6,}  {100*card[k]/n_end:5.1f}%")
    print(f"    >=2 sizes: {sum(v for k,v in card.items() if k>=2):,}"
          f"   >=3 sizes: {sum(v for k,v in card.items() if k>=3):,}")

    raw_vocab, norm_vocab = set(), set()
    for d in sz_days:
        for r in load_day(d).values():
            raw_vocab.update(raw_sizes(r))
            norm_vocab.update(r["ns"])
    print(f"\n  VOCABULARY across the era: {len(raw_vocab)} raw labels -> "
          f"{len(norm_vocab)} after normalisation")
    print("    The tail contains real garbage — phone models ('17 PRO'), colour names")
    print("    from a store whose colour option is called \"Size\", and 'OS - 1 unit left',")
    print("    a label that mutates as stock changes. Measured payoff below, because a")
    print("    scary-looking tail is not the same as a costly one.")

    # Does an empty set really equal the product-level sell-out? (guard 3)
    print("\n  IS 'the size set emptied' THE SAME EVENT AS 'available flipped to false'?")
    for d in (av_days[0], end) if av_days else ():
        S = load_day(d)
        known = [r for r in S.values() if r["available"] in ("0", "1")]
        bad = sum(1 for r in known if r["ns"] and r["available"] == "0")
        oneofsize = sum(1 for r in known if not r["ns"] and r["available"] == "1")
        print(f"    {d}: rows with a NON-EMPTY size set and available=false : {bad}"
              + ("   <- must be 0" if bad == 0 else "   <- NOT 0, the substitution is unsafe"))
        print(f"           rows with an EMPTY set and available=true        : {oneofsize:,}"
              "   (one-size pieces; correctly NOT sell-outs)")
        if bad:
            sys.exit("REFUSING TO CONTINUE: a non-empty size set with available=false means "
                     "the two measurements have diverged and every outcome below is wrong.")
    b0, e0 = PRIOR_WINDOW
    B, Ew = load_day(b0), load_day(e0)
    pairs = [p for p in B if p in Ew and usable(B[p], Ew[p])]
    emptied = [p for p in pairs if B[p]["ns"] and not Ew[p]["ns"]]
    still = [p for p in pairs if B[p]["ns"] and Ew[p]["ns"]]
    agree = sum(1 for p in emptied if Ew[p]["available"] == "0")
    dis = sum(1 for p in still if Ew[p]["available"] == "0")
    print(f"    {b0} -> {e0}: of {len(emptied)} pieces whose size set emptied, "
          f"{agree} ({100*agree/max(len(emptied),1):.0f}%) carry available=false")
    print(f"                            of {len(still):,} that still have sizes, "
          f"{dis} ({100*dis/max(len(still),1):.2f}%) do")
    print("    -> the two are the same event. The size set is simply the version of it")
    print("       that exists for the 24 days before `available` was ever written, which")
    print("       is what makes an out-of-time split possible at all.")

    # ── 2. DENSITY ────────────────────────────────────────────────────────
    rule("2. DENSITY — how much more signal is there? (the whole point)")
    print("  Restricted to products present at BOTH endpoints and not grace-frozen at")
    print("  either, so nothing here can be manufactured by the 60-item sampler.\n")
    dens = {}
    flip_sets = {}
    windows = [PRIOR_WINDOW]
    alt_b = sz_days[max(0, len(sz_days) - 17)]
    if (alt_b, end) != PRIOR_WINDOW:
        windows.append((alt_b, end))
    for wb, we in windows:
        Bw, Ewin = load_day(wb), load_day(we)
        nd = (dt.date.fromisoformat(we) - dt.date.fromisoformat(wb)).days
        both = [p for p in Bw if p in Ewin and usable(Bw[p], Ewin[p])]
        check_not_round(len(both), f"both-endpoint products {wb}->{we}")
        risk_av = [p for p in both if Bw[p]["available"] == "1"
                   and Ewin[p]["available"] in ("0", "1")]
        flip_set = {p for p in risk_av if Ewin[p]["available"] == "0"}
        flip_sets[(wb, we)] = flip_set
        flips = len(flip_set)
        sized = [p for p in both if Bw[p]["ns"]]
        lost_any = [p for p in sized if Bw[p]["ns"] - Ewin[p]["ns"]]
        gain_any = [p for p in sized if Ewin[p]["ns"] - Bw[p]["ns"]]
        both_dir = [p for p in sized if (Bw[p]["ns"] - Ewin[p]["ns"]) and (Ewin[p]["ns"] - Bw[p]["ns"])]
        n_lab_lost = sum(len(Bw[p]["ns"] - Ewin[p]["ns"]) for p in sized)
        n_lab_gain = sum(len(Ewin[p]["ns"] - Bw[p]["ns"]) for p in sized)
        slots = sum(len(Bw[p]["ns"]) for p in sized)
        print(f"  {wb} -> {we}  (+{nd}d)   both-endpoint products {len(both):,}"
              f"   of which sized {len(sized):,} = {100*len(sized)/len(both):.1f}%")
        if risk_av:
            print(f"    PRODUCT-level sell-outs (available 1->0) : {flips:5,}/{len(risk_av):,}"
                  f" = {100*flips/len(risk_av):5.2f}%")
        print(f"    pieces losing >=1 size                   : {len(lost_any):5,}/{len(sized):,}"
              f" = {100*len(lost_any)/len(sized):5.2f}%"
              f"   (of ALL both-endpoint pieces {100*len(lost_any)/len(both):5.2f}%)")
        print(f"    pieces GAINING >=1 size (restock)        : {len(gain_any):5,}"
              f" = {100*len(gain_any)/len(sized):5.2f}%")
        print(f"    pieces doing both                        : {len(both_dir):5,}"
              f" = {100*len(both_dir)/len(sized):5.2f}%")
        print(f"    SIZE-LEVEL depletion EVENTS              : {n_lab_lost:5,}"
              f"   against {slots:,} baseline size slots = {100*n_lab_lost/max(slots,1):.2f}%")
        print(f"    size-level RESTOCK events                : {n_lab_gain:5,}")
        if risk_av and flips:
            print(f"    >>> DENSITY GAIN: {n_lab_lost:,} size events vs {flips} product events"
                  f" = {n_lab_lost/flips:.1f}x")
            print(f"        pieces touched: {100*len(lost_any)/len(sized):.2f}% vs "
                  f"{100*flips/len(risk_av):.2f}% = "
                  f"{(len(lost_any)/len(sized))/(flips/len(risk_av)):.1f}x")
            dens[(wb, we)] = dict(events=n_lab_lost, flips=flips,
                                  gain=n_lab_gain,
                                  ratio=n_lab_lost / flips,
                                  touched=(len(lost_any) / len(sized)) / (flips / len(risk_av)))
        print()

    # Two windows of different length landing on the SAME event count looks like a
    # copy-paste bug and is worth two seconds to rule out. Rather than assert a
    # number that will go stale as the archive grows, show the overlap whenever it
    # happens: identical COUNTS with a partial intersection is chance, identical
    # counts with an identical SET is the same window computed twice.
    keys = list(flip_sets)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a_, b_ = flip_sets[keys[i]], flip_sets[keys[j]]
            if len(a_) == len(b_) and a_ and len(a_ & b_) != len(a_):
                print(f"  (coincidence check: {keys[i][0]}->{keys[i][1]} and "
                      f"{keys[j][0]}->{keys[j][1]} both report {len(a_)} sell-outs, but")
                print(f"   the two sets share only {len(a_ & b_)} pieces — different events, "
                      "equal counts, not a bug)\n")
            elif len(a_) == len(b_) and a_ and a_ == b_:
                print(f"  !! {keys[i]} and {keys[j]} report the SAME {len(a_)} sell-outs on "
                      "the same pieces.\n     Two different windows should not agree exactly. "
                      "Check the window definitions.\n")

    # per product-day, the honest rate-based version
    print("  Per product-day, over consecutive snapshots (the same restriction):")
    prev = None
    pd_sized = pd_lost = pd_gain = 0
    pd_flip = pd_risk = 0
    for d in sz_days:
        cur = load_day(d)
        if prev is not None:
            _, D = prev
            for p, r in cur.items():
                if p not in D or D[p]["stale"] or r["stale"]:
                    continue
                if D[p]["ns"]:
                    pd_sized += 1
                    if D[p]["ns"] - r["ns"]:
                        pd_lost += 1
                    if r["ns"] - D[p]["ns"]:
                        pd_gain += 1
                if D[p]["available"] == "1" and r["available"] in ("0", "1"):
                    pd_risk += 1
                    if r["available"] == "0":
                        pd_flip += 1
        prev = (d, cur)
    print(f"    sized product-days                : {pd_sized:,}")
    print(f"    lost >=1 size                     : {pd_lost:,} = {100*pd_lost/pd_sized:.3f}%/day")
    print(f"    gained >=1 size                   : {pd_gain:,} = {100*pd_gain/pd_sized:.3f}%/day")
    print(f"    product-level flip 1->0           : {pd_flip:,}/{pd_risk:,} = "
          f"{100*pd_flip/max(pd_risk,1):.3f}%/day")
    print(f"    >>> {100*pd_lost/pd_sized:.3f} / {100*pd_flip/max(pd_risk,1):.3f} = "
          f"{(pd_lost/pd_sized)/(pd_flip/max(pd_risk,1)):.1f}x denser per day")
    print("\n  READ IT WITH THE CAVEAT ATTACHED: the multiplier applies to the ~63% of the")
    print("  catalog that has a size option at all. For a bag, a one-size top or a piece")
    print("  of jewellery there is no size curve and never will be.")

    # ── 3. IS IT DEPLETION OR IS IT NOISE? ────────────────────────────────
    rule("3. SEPARATING DEPLETION FROM NOISE (guards 4, 5, 6)")

    # (a) how much does normalisation actually buy
    Bw, Ewin = load_day(b0), load_day(e0)
    pr = [p for p in Bw if p in Ewin and usable(Bw[p], Ewin[p]) and Bw[p]["ns"]]
    raw_l = sum(len(set(raw_sizes(Bw[p])) - set(raw_sizes(Ewin[p]))) for p in pr)
    nrm_l = sum(len(Bw[p]["ns"] - Ewin[p]["ns"]) for p in pr)
    disjoint = [p for p in pr if Ewin[p]["ns"] and not (Bw[p]["ns"] & Ewin[p]["ns"])]
    print(f"  (a) VOCABULARY. {b0} -> {e0}: {raw_l:,} raw label losses become {nrm_l:,}")
    print(f"      after normalisation — it removes {raw_l-nrm_l} = "
          f"{100*(raw_l-nrm_l)/max(raw_l,1):.1f}% of them.")
    print(f"      size sets replaced WHOLESALE (disjoint at the two ends): {len(disjoint)}")
    print("      Those are re-uploads and scale changes (a store moving from UK sizing to")
    print("      letters) and they are excluded from nothing — at this count they cannot")
    print("      move a result. The vocabulary is chaotic and nearly harmless.")
    for p in disjoint[:5]:
        print(f"        {p[:46]:46} {sorted(Bw[p]['ns'])} -> {sorted(Ewin[p]['ns'])}")

    # (b) reversion
    print("\n  (b) REVERSION. Of every day-to-day size loss, is the label back a week later?")
    n_loss = n_back = 0
    for i in range(len(sz_days) - 1):
        A, C = load_day(sz_days[i]), load_day(sz_days[i + 1])
        fut = [load_day(d) for d in sz_days[i + 2:i + 9]]
        for p, r in A.items():
            if p not in C or r["stale"] or C[p]["stale"]:
                continue
            for s in (r["ns"] - C[p]["ns"]):
                n_loss += 1
                if any(p in F and s in F[p]["ns"] for F in fut):
                    n_back += 1
    print(f"      day-to-day size-label losses : {n_loss:,}")
    print(f"      back within 7 snapshots      : {n_back:,} = {100*n_back/max(n_loss,1):.1f}%")
    print(f"      durably gone                 : {n_loss-n_back:,} = "
          f"{100*(n_loss-n_back)/max(n_loss,1):.1f}%")
    print("      A fifth of it reverses, so day-to-day is not a claim. Endpoint to")
    print("      endpoint is. Nothing in this file is measured day-to-day.")

    # (c) where the flicker lives
    print("\n  (c) WHERE THE NOISE LIVES. Flicker = a size gone 1-2 snapshots, then back.")
    hist = collections.defaultdict(dict)
    seen = collections.Counter()
    brand_of = {}
    for i, d in enumerate(sz_days):
        S = load_day(d)
        for p, r in S.items():
            seen[p] += 1
            brand_of[p] = r["brand"]
            if r["stale"]:
                continue
            for s in r["ns"]:
                hist[p].setdefault(s, []).append(i)
    tracked = [p for p, n in seen.items() if n >= 20]
    check_not_round(len(tracked), "tracked products")
    flick = collections.Counter()
    durable = collections.Counter()
    for p in tracked:
        for s, ix in hist[p].items():
            for a, b in zip(ix, ix[1:]):
                gap = b - a - 1
                if gap >= RESTOCK_MIN_ABSENCE:
                    durable[p] += 1
                elif gap >= 1:
                    flick[p] += 1
    bf = collections.Counter()
    bn = collections.Counter()
    for p in tracked:
        bf[brand_of[p]] += flick[p]
        bn[brand_of[p]] += 1
    tot_f = sum(bf.values())
    top = bf.most_common(8)
    print(f"      tracked pieces (>=20 snapshots): {len(tracked):,}"
          f"   total flicker events: {tot_f:,}")
    print(f"      the top 8 brands hold {sum(n for _, n in top):,} = "
          f"{100*sum(n for _,n in top)/max(tot_f,1):.0f}% of it:")
    for b, n in top:
        print(f"        {b[:30]:30} {n:5,} events / {bn[b]:4} pieces = {n/bn[b]:5.1f} per piece")
    print("      A product that alerted on a single-day size disappearance would fire")
    print("      mostly on those stores and would be wrong most of the time.")

    # (d) brands with no signal at all
    print("\n  (d) BRANDS THE SIGNAL IS STRUCTURALLY BLIND TO.")
    movers = collections.Counter()
    prev = None
    for d in sz_days:
        cur = load_day(d)
        if prev is not None:
            _, D = prev
            for p, r in cur.items():
                if p in D and not D[p]["stale"] and not r["stale"] and D[p]["ns"] != r["ns"]:
                    movers[r["brand"]] += 1
        prev = (d, cur)
    sized_by_brand = collections.Counter(r["brand"] for r in load_day(end).values() if r["ns"])
    elig = [b for b, n in sized_by_brand.items() if n >= 5]
    dead = sorted(b for b in elig if movers[b] == 0)
    print(f"      brands with >=5 sized pieces at {end}: {len(elig)}")
    print(f"      with ZERO size-set changes across the whole era: {len(dead)} = "
          f"{100*len(dead)/max(len(elig),1):.0f}%")
    print(f"        {', '.join(dead)}")
    print("      Made-to-order, print-on-demand, or a store that simply never marks a")
    print("      variant unavailable. For those brands every number in this file is a")
    print("      structural zero, not a finding, and they must never be sold a report")
    print("      that reads their silence as 'nothing sold'.")

    # ── 4. PREDICTION ─────────────────────────────────────────────────────
    rule("4. DOES IT PREDICT? — out of time, always")
    tr = build_window(*TRAIN_SPLIT, sz_days)
    te = build_window(*TEST_SPLIT, sz_days)
    check_not_round(len(tr), "train rows")
    check_not_round(len(te), "test rows")
    print(f"  TRAIN  feature {TRAIN_SPLIT[0]} -> {TRAIN_SPLIT[1]}"
          f"   outcome -> {TRAIN_SPLIT[2]}   n = {len(tr):,}"
          f"  brands {len(set(r['brand'] for r in tr))}")
    print(f"  TEST   feature {TEST_SPLIT[0]} -> {TEST_SPLIT[1]}"
          f"   outcome -> {TEST_SPLIT[2]}   n = {len(te):,}"
          f"  brands {len(set(r['brand'] for r in te))}")
    print("  Identical geometry both sides (7d feature, 13d outcome). The train outcome")
    print("  window ENDS on the day the test feature window BEGINS, so nothing leaks.")
    print("  The prior model looked fine in-window and collapsed out of it; that is the")
    print("  failure this split exists to make impossible.\n")

    OUTCOMES = [("y_soldout", "the piece is entirely gone (== available flipped to false)"),
                ("y_deplete", "the piece loses at least one MORE size")]
    for key, label in OUTCOMES:
        ytr = np.array([r[key] for r in tr])
        yte = np.array([r[key] for r in te])
        print(f"  OUTCOME {key}: {label}")
        print(f"    base rate  train {int(ytr.sum()):4} / {len(ytr):,} = {100*ytr.mean():5.2f}%"
              f"    test {int(yte.sum()):4} / {len(yte):,} = {100*yte.mean():5.2f}%")

    # 4a. POWER FIRST
    rule("4a. POWER, BEFORE ANY ESTIMATE")
    treated = [r for r in te if r["lost"]]
    for key, _ in OUTCOMES:
        a = sum(r[key] for r in treated)
        n1 = len(treated)
        c = sum(r[key] for r in te if not r["lost"])
        n0 = len(te) - n1
        p0 = c / n0
        print(f"  {key:10} treated n={n1:,} control n={n0:,} base {100*p0:5.2f}%"
              f"  -> detectable RR >= {mde_rr(n1, n0, p0):.2f}x")
    print("\n  Read that literally. On the sell-out outcome this design cannot see")
    print("  anything short of roughly a tripling, so a null there is indicative and")
    print("  nothing more. On the further-depletion outcome it can see a 1.5x, which is")
    print("  why that is where the real test happens.")

    # 4b. POSITIVE CONTROL
    rule("4b. POSITIVE CONTROL — can this machinery detect anything at all?")
    print("  A known-true effect on the same risk set through the same code path. If")
    print("  this is null, everything below is broken plumbing rather than a")
    print("  measurement. Two of them: one mechanical (a piece with one size left is")
    print("  closer to zero than a piece with six) and one about the world (newness).\n")
    g_te = [r["brand"] for r in te]
    for feat, lbl, expect in (("n1", "sizes in stock now -> sells out", "LOWER"),
                              ("is_new", "arrived <=14d ago -> sells out", "higher")):
        s = [r[feat] for r in te]
        y = [r["y_soldout"] for r in te]
        a, lo, hi = auc_ci(s, y, seed=1)
        sa, nb = stratified_auc(s, y, g_te)
        arrow = "  (LOWER -> sells out)" if a < 0.5 else "  (higher -> sells out)"
        print(f"    {lbl:44} AUC {a:.3f} [{lo:.3f},{hi:.3f}]"
              f"  within-brand {sa:.3f} ({nb} brands){arrow}")
    print("    Expected: the first fires hard and INVERTED, the second weakly. If the")
    print("    first is near 0.50 the join or the outcome is wrong — stop and fix it.")

    # 4c. NEGATIVE CONTROL — the one that decides the answer
    rule("4c. NEGATIVE CONTROL — is it depletion, or just a listing that moves?")
    print("  The hypothesis is directional: sizes DRAINING means the piece is selling.")
    print("  The alternative is that any listing whose availability flags move at all is")
    print("  a listing more likely to move again, in which case a restock should predict")
    print("  a sell-out just as well as a depletion does. Same plumbing, same window.\n")
    for key, _ in OUTCOMES:
        y = [r[key] for r in te]
        print(f"    OUTCOME {key}  ({sum(y)} events, {100*sum(y)/len(y):.2f}%)")
        for feat, lbl in (("lost", "DIRECTIONAL   lost >=1 size          (the hypothesis)"),
                          ("gained", "NEG CONTROL   GAINED >=1 size        (a restock)"),
                          ("vol_days", "NEG CONTROL   n days the set changed (direction-free)")):
            s = [r[feat] for r in te]
            a, lo, hi = auc_ci(s, y, seed=2)
            sa, _ = stratified_auc(s, y, g_te)
            print(f"      {lbl:56} AUC {a:.3f} [{lo:.3f},{hi:.3f}]  within-brand {sa:.3f}")
        for feat, lbl in (("lost", "lost a size"), ("gained", "was restocked")):
            t = [r for r in te if r[feat]]
            a_ = sum(r[key] for r in t)
            n1 = len(t)
            c_ = sum(r[key] for r in te if not r[feat])
            n0 = len(te) - n1
            p1, p0, rr, lo, hi = rr_ci(a_, n1, c_, n0)
            mr, ml, mh, used = mh_rr(brand_strata(te, feat, key))
            print(f"      {lbl:16} {a_:4}/{n1:5,} = {100*p1:5.2f}%  vs "
                  f"{c_:4}/{n0:5,} = {100*p0:5.2f}%   crude RR {fmt_ci(rr, lo, hi)}")
            print(f"                       brand-stratified RR {fmt_ci(mr, ml, mh)} "
                  f"({used} brands)")
        print()
    print("    If the restock row looks like the depletion row, the direction is not")
    print("    what is being measured. Section 4d settles it inside a model.")

    # 4d. THE LADDER
    rule("4d. WHAT EACH LAYER OF INFORMATION IS ACTUALLY WORTH (out of time)")
    print("  Each tier adds one idea to the tier above it. The size of each step is the")
    print("  price of that idea. Ridge logistic, fit on TRAIN only, scored on TEST only.\n")
    ladder = {}
    for key, label in OUTCOMES:
        ytr = np.array([r[key] for r in tr], float)
        yte = np.array([r[key] for r in te], float)
        print(f"  OUTCOME {key}  ({label})   test base rate {100*yte.mean():.2f}%")
        prev_auc = None
        for tier, nm in TIERS:
            Xtr, Xte = features(tr, tier), features(te, tier)
            mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
            w = logistic_fit((Xtr - mu) / sd, ytr)
            pte = logistic_predict(w, (Xte - mu) / sd)
            a, lo, hi = auc_ci(pte, yte, seed=3)
            sa, nb = stratified_auc(pte, yte, g_te)
            lift, llo, lhi, top_rate, base = decile_lift(pte, yte, seed=4)
            delta = "" if prev_auc is None else f"  delta {a-prev_auc:+.3f}"
            print(f"    {nm}")
            print(f"        AUC {a:.3f} [{lo:.3f},{hi:.3f}]   within-brand {sa:.3f} ({nb} br)"
                  f"   top decile {lift:.2f}x [{llo:.2f},{lhi:.2f}]"
                  f"  ({100*top_rate:.2f}% vs {100*base:.2f}%){delta}")
            ladder[(key, tier)] = (a, sa, lift)
            prev_auc = a
        print()
    print("  The two numbers that decide the business are the M0->M1 step and the")
    print("  M2->M3 step: what the size LEVEL is worth, and what the size DIRECTION is")
    print("  worth once you already know the listing moves.")

    # 4e. the sellable version of the level effect
    rule("4e. THE LEVEL EFFECT, AS A BRAND WOULD READ IT")
    print(f"  Sell-out rate over the {OUTCOME_DAYS}-day TEST outcome window, by how many")
    print("  sizes were in stock when the clock started:\n")
    buckets = collections.defaultdict(lambda: [0, 0])
    for r in te:
        k = min(r["n1"], 6)
        buckets[k][0] += 1
        buckets[k][1] += r["y_soldout"]
    for k in sorted(buckets):
        n, ev = buckets[k]
        lbl = f"{k}" if k < 6 else "6+"
        print(f"    {lbl:>3} sizes in stock   n={n:5,}   entirely gone {ev:3} = {100*ev/n:5.2f}%")
    print("\n  That is a forward statement, not a definition: none of these pieces was")
    print("  sold out when it was scored. It is also the single most useful thing in the")
    print("  file, and it needs no user, no model and no fitting.")

    # 4f. does the direction survive stratification
    rule("4f. THE DIRECTIONAL CLAIM, STRATIFIED UNTIL IT STOPS MOVING")
    print("  Guard 8: an unstratified number here is presumed wrong. The same 2x2, with")
    print("  brand added, then with 'how many sizes are left' added on top of brand —")
    print("  because a piece that just lost a size has, by arithmetic, fewer left.\n")
    for key, _ in OUTCOMES:
        t = [r for r in te if r["lost"]]
        a_ = sum(r[key] for r in t)
        n1 = len(t)
        c_ = sum(r[key] for r in te if not r["lost"])
        n0 = len(te) - n1
        p1, p0, rr, lo, hi = rr_ci(a_, n1, c_, n0)
        mr, ml, mh, used = mh_rr(brand_strata(te, "lost", key))
        mr2, ml2, mh2, used2 = mh_rr(
            brand_strata(te, "lost", key, extra_key=lambda r: min(r["n1"], 6)))
        print(f"    OUTCOME {key}")
        print(f"      crude                            RR {fmt_ci(rr, lo, hi)}")
        print(f"      + brand                          RR {fmt_ci(mr, ml, mh)}  ({used} brands)")
        print(f"      + brand + sizes remaining        RR {fmt_ci(mr2, ml2, mh2)}  ({used2} cells)")
        print(f"      MDE for this cell                {mde_rr(n1, n0, p0):.2f}x")

    # 4g. stability
    if not args.quick:
        rule("4g. STABILITY — the same frozen rule on every window it fits in")
        print("  No fitting at all: the a-priori rule 'lost >=1 size in the feature window'")
        print("  scored against both outcomes, on every 7d+13d window the archive admits.")
        print("  A signal that only exists in the window you chose is not a signal.\n")
        print(f"  {'t0':>10} {'t1':>11} {'t2':>11} {'n':>7} {'base%':>7}"
              f" {'AUC_sold':>9} {'wb':>6} {'AUC_depl':>9} {'wb':>6}")

        def nearest_on_or_after(day, delta):
            tgt = (dt.date.fromisoformat(day) + dt.timedelta(days=delta)).isoformat()
            later = [d for d in sz_days if d >= tgt]
            return later[0] if later else None

        sweep = []
        for t0 in sz_days:
            t1 = nearest_on_or_after(t0, FEATURE_DAYS)
            t2 = nearest_on_or_after(t1, OUTCOME_DAYS) if t1 else None
            if not t1 or not t2 or t2 > sz_days[-1] or t1 >= t2:
                continue
            A, Bm, C = load_day(t0), load_day(t1), load_day(t2)
            rows = []
            for p, r1 in Bm.items():
                if p not in A or p not in C or A[p]["stale"] or r1["stale"] or C[p]["stale"]:
                    continue
                s0, s1, s2 = A[p]["ns"], r1["ns"], C[p]["ns"]
                if not s0 or not s1:
                    continue
                rows.append((1 if (s0 - s1) else 0, 1 if not s2 else 0,
                             1 if (s1 - s2) else 0, r1["brand"]))
            if len(rows) < 500:
                continue
            x = [r[0] for r in rows]
            g = [r[3] for r in rows]
            ys = [r[1] for r in rows]
            yd = [r[2] for r in rows]
            a1, a2 = auc(x, ys), auc(x, yd)
            s1_, _ = stratified_auc(x, ys, g)
            s2_, _ = stratified_auc(x, yd, g)
            sweep.append((a1, s1_, a2, s2_))
            print(f"  {t0} {t1} {t2} {len(rows):7,} {100*sum(ys)/len(ys):6.2f}%"
                  f" {a1:9.3f} {s1_:6.3f} {a2:9.3f} {s2_:6.3f}")
        if sweep:
            arr = np.array(sweep)
            print(f"\n  {len(sweep)} windows. medians: AUC_sold {np.median(arr[:,0]):.3f} "
                  f"(within-brand {np.median(arr[:,1]):.3f})"
                  f"   AUC_depl {np.median(arr[:,2]):.3f} "
                  f"(within-brand {np.median(arr[:,3]):.3f})")
            for j, nm in ((0, "AUC_sold"), (1, "within-brand sold"),
                          (2, "AUC_depl"), (3, "within-brand depl")):
                print(f"    windows above 0.500 on {nm:22}: "
                      f"{int((arr[:,j] > 0.5).sum())}/{len(sweep)}")
            print("\n  The rule beats a coin flip in essentially every window, which rules")
            print("  out 'we got lucky with the split'. It does NOT rule out section 4c —")
            print("  a liveness signal is also stable.")

    # ── 5. SIZE-CURVE SHAPE ───────────────────────────────────────────────
    rule("5. WHICH SIZES GO FIRST — and whether the tier under-buys the middle")
    print("  Restricted to pieces whose whole size set is on the letter scale, with >=2")
    print("  sizes. The test is WITHIN PRODUCT: given a piece lost k of its n sizes,")
    print("  every size it offered had an equal k/n chance of being one of them. That")
    print("  makes each product its own stratum, so brand, price and style are all")
    print("  controlled by construction — the strongest available answer to guard 8.\n")
    shape_windows = [(sz_days[0], sz_days[-1]), PRIOR_WINDOW, (TEST_SPLIT[0], TEST_SPLIT[2])]
    seen_w = set()
    zrows = {}
    for wb, we in shape_windows:
        if (wb, we) in seen_w:
            continue
        seen_w.add((wb, we))
        Bw, Ewin = load_day(wb), load_day(we)
        oe = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
        oe_brand = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0]))
        nmixed = 0
        for p, r in Bw.items():
            if p not in Ewin or not usable(r, Ewin[p]):
                continue
            s0, s1 = r["ns"], Ewin[p]["ns"]
            if len(s0) < 2 or not all(x in LETTER_IDX for x in s0):
                continue
            L = s0 - s1
            if not L or not (s0 - L):     # all-or-nothing carries no within-product info
                continue
            nmixed += 1
            k, n = len(L), len(s0)
            for x in s0:
                oe[x][0] += 1 if x in L else 0
                oe[x][1] += k / n
                oe[x][2] += (k / n) * (1 - k / n) * (n - k) / max(n - 1, 1)
                d = oe_brand[r["brand"]][x]
                d[0] += 1 if x in L else 0
                d[1] += k / n
                d[2] += (k / n) * (1 - k / n) * (n - k) / max(n - 1, 1)
        cells = []
        for s in LETTER_SCALE:
            o, ex, v = oe[s]
            if ex < 5:
                cells.append("      .")
                continue
            z = (o - ex) / math.sqrt(v) if v > 0 else float("nan")
            cells.append(f"{z:+7.2f}")
            zrows.setdefault(s, []).append(z)
        print(f"  {wb} -> {we}  ({nmixed:4} mixed-outcome pieces)")
        print("      " + "  ".join(f"{s:>7}" for s in LETTER_SCALE[:7]))
        print("      " + "  ".join(cells[:7]) + "     <- z, + means it goes FIRST")
        if (wb, we) == (TEST_SPLIT[0], TEST_SPLIT[2]):
            # leave-one-brand-out on the winner, so the claim is not one store
            worst_label, worst_z = None, None
            for s in LETTER_SCALE:
                o, ex, v = oe[s]
                if ex < 5 or v <= 0:
                    continue
                z = (o - ex) / math.sqrt(v)
                if worst_z is None or z > worst_z:
                    worst_label, worst_z = s, z
            lo_z = None
            drop = None
            for b in oe_brand:
                d = oe_brand[b][worst_label]
                o2, e2, v2 = (oe[worst_label][0] - d[0], oe[worst_label][1] - d[1],
                              oe[worst_label][2] - d[2])
                if v2 <= 0:
                    continue
                z2 = (o2 - e2) / math.sqrt(v2)
                if lo_z is None or z2 < lo_z:
                    lo_z, drop = z2, b
            print(f"      leave-one-brand-out on {worst_label}: z falls from {worst_z:+.2f}"
                  f" to at worst {lo_z:+.2f} (dropping {drop}) — not one store")
    print("\n  What the tier OFFERS, for comparison (share of letter-scale pieces with each")
    print("  size in stock). A size can only run out if it was bought in the first place:")
    for d in (sz_days[0], PRIOR_WINDOW[0], end):
        S = load_day(d)
        c = collections.Counter()
        tot = 0
        for r in S.values():
            if r["ns"] and all(x in LETTER_IDX for x in r["ns"]):
                tot += 1
                for x in r["ns"]:
                    c[x] += 1
        print(f"    {d}  n={tot:5,}   "
              + "  ".join(f"{s}:{100*c[s]/max(tot,1):4.1f}%" for s in LETTER_SCALE[:7]))

    # ── 6. RESTOCKS ───────────────────────────────────────────────────────
    rule("6. RESTOCKS — the event nobody else can see")
    print(f"  A restock counts only when the size was absent for >= "
          f"{RESTOCK_MIN_ABSENCE} consecutive snapshots")
    print("  first. Anything shorter is the flicker of section 3c and would make the")
    print("  ten noisiest stores look like the ten best-managed ones.\n")
    hd = collections.Counter(durable[p] for p in tracked)
    print(f"  tracked pieces: {len(tracked):,}")
    for k in sorted(hd)[:5]:
        print(f"    {k} durable restocks: {hd[k]:6,} = {100*hd[k]/len(tracked):5.2f}%")
    n_ge1 = sum(v for k, v in hd.items() if k >= 1)
    n_ge2 = sum(v for k, v in hd.items() if k >= 2)
    print(f"    >=1: {n_ge1:,} = {100*n_ge1/len(tracked):.2f}%"
          f"    >=2: {n_ge2:,} = {100*n_ge2/len(tracked):.2f}%")
    print(f"  discarded as flicker: {sum(flick.values()):,} events across "
          f"{sum(1 for p in tracked if flick[p]):,} pieces")
    bb = collections.defaultdict(lambda: [0, 0])
    for p in tracked:
        bb[brand_of[p]][0] += 1
        if durable[p] >= 2:
            bb[brand_of[p]][1] += 1
    big = {b: v for b, v in bb.items() if v[0] >= 20}
    n_named = sum(1 for v in big.values() if 1 <= v[1] <= 3)
    n_zero = sum(1 for v in big.values() if v[1] == 0)
    print(f"\n  brands with >=20 tracked pieces: {len(big)}")
    print(f"    where 1-3 pieces have been restocked twice or more (a NAMEABLE outlier,")
    print(f"    which is the form the insight has to take to be worth anything): {n_named}")
    print(f"    where MORE than 3 qualify (so it is not an outlier): "
          f"{sum(1 for v in big.values() if v[1] > 3)}")
    print(f"    where none qualify (nothing to say): {n_zero}")

    # ── 7. THE SENTENCES ──────────────────────────────────────────────────
    rule("7. THE FOUR SENTENCES A BRAND MIGHT PAY FOR")
    print("  For each: is it TRUE from this data, how often does it apply, and what is")
    print("  the caveat that has to travel with it.\n")

    # S1
    print('  S1  "four of the six sizes in this dress went in eleven days, and it has')
    print('       not been restocked"')
    print("      Counted as: >=3 sizes offered at the start, >=half of them gone by the")
    print("      end, and NOT ONE size added back anywhere in between.")
    for wb, we, lbl in ((PRIOR_WINDOW[0], PRIOR_WINDOW[1], "16d"),
                        (sz_days[0], end, "whole era")):
        Bw, Ewin = load_day(wb), load_day(we)
        sized = [p for p in Bw if p in Ewin and usable(Bw[p], Ewin[p]) and Bw[p]["ns"]]
        q = [p for p in sized
             if len(Bw[p]["ns"]) >= 3
             and len(Bw[p]["ns"] - Ewin[p]["ns"]) >= math.ceil(0.5 * len(Bw[p]["ns"]))
             and not (Ewin[p]["ns"] - Bw[p]["ns"])]
        byb = collections.Counter(Bw[p]["brand"] for p in q)
        sb = collections.Counter(Bw[p]["brand"] for p in sized)
        el = [b for b, n in sb.items() if n >= 20]
        print(f"      {lbl:10} ({wb}->{we}): {len(q):4,} pieces = "
              f"{100*len(q)/max(len(sized),1):5.2f}% of the sized shelf;"
              f"  of {len(el)} brands with >=20 sized pieces, "
              f"{sum(1 for b in el if byb[b])} have >=1 and "
              f"{sum(1 for b in el if byb[b] >= 3)} have >=3")
    print("      VERDICT: TRUE, and directly checkable against the brand's own store.")
    print("      CAVEAT: it is a count of SIZES, not units. A piece stocked one-deep")
    print("      loses three sizes on three sales. Say 'three of five sizes are gone',")
    print("      never 'this sold well'. And in any two-week window most brands have no")
    print("      such piece at all, so this is a highlight, not a report.")

    # S2
    print('\n  S2  "your size curve empties from the middle — M and L go first"')
    for s in ("S", "M", "L", "XL"):
        if s in zrows:
            zs = zrows[s]
            print(f"      {s:3}: within-product z across {len(zs)} windows = "
                  + ", ".join(f"{z:+.2f}" for z in zs))
    print("      VERDICT: FALSE as usually stated, and the true version is better. The")
    print("      middle does not go first — S is consistently the size that SURVIVES.")
    print("      XL is consistently over-represented among the sizes that go, in every")
    print("      window, and it survives leave-one-brand-out. Combine that with the")
    print("      offer curve above (XL stocked on under half of pieces, and falling) and")
    print("      the honest sentence is: the independent tier under-buys the LARGE end,")
    print("      and that is where its stock runs out first.")
    print("      CAVEAT: letter-scale pieces only. A brand on IT/FR/UK numbering is not")
    print("      in this analysis at all, and roughly a third of sized pieces are not on")
    print("      the letter scale.")

    # S3
    print('\n  S3  "this piece has been restocked twice, which no other piece in your')
    print('       line has"')
    print(f"      TRUE for {n_ge2:,} pieces = {100*n_ge2/len(tracked):.2f}% of tracked stock,")
    print(f"      and it is a genuine OUTLIER (1-3 such pieces) for {n_named} of {len(big)}")
    print("      brands large enough to say it about.")
    print("      CAVEAT: a restock is a re-listing, not a re-order. A store fixing an")
    print("      oversold variant looks identical. And the >=3-snapshot rule means a")
    print("      genuine 2-day restock is invisible; that is the deliberate trade for")
    print("      not shipping the flicker of section 3c as insight.")

    # S4
    print('\n  S4  "this piece is going — half its sizes went in a week, expect it gone"')
    a_soldout = ladder.get(("y_soldout", 3), (float("nan"),))[0]
    a_novol = ladder.get(("y_soldout", 2), (float("nan"),))[0]
    d_soldout = ladder.get(("y_deplete", 3), (float("nan"),))[0]
    d_novol = ladder.get(("y_deplete", 2), (float("nan"),))[0]
    print(f"      VERDICT: NOT SUPPORTED as a causal read. Adding the direction of the")
    print(f"      move to a model that already knows the level and the volatility moves")
    print(f"      the out-of-time AUC by {a_soldout-a_novol:+.3f} on sell-out and "
          f"{d_soldout-d_novol:+.3f} on further")
    print("      depletion. Section 4c is why: a RESTOCK predicts a sell-out about as")
    print("      well as a depletion does. What the size curve tracks is whether the")
    print("      listing is alive, not which way it is moving.")
    print("      The version that IS supported is section 4e — 'one size left' is a real")
    print("      13-day forecast, and it needs no time series at all.")

    # ── 8. VERDICT ────────────────────────────────────────────────────────
    rule("8. VERDICT")
    d = dens.get(PRIOR_WINDOW)
    print("  Q: is the size curve, collected daily for months and never once analysed,")
    print("     strong enough to build a business on?")
    print("  A: the ARRAY is. The CURVE is not. Those are different products.\n")
    if d:
        print(f"    DENSITY — yes, and by a lot. In {PRIOR_WINDOW[0]} -> {PRIOR_WINDOW[1]} the")
        print(f"    archive holds {d['events']:,} size-depletion events against {d['flips']} "
              f"product-level sell-outs,")
        print(f"    {d['ratio']:.1f}x more, touching {d['touched']:.1f}x as many pieces. It also holds "
              f"{d['gain']:,} size")
        print("    RESTOCK events, a class of event the product-level flag cannot express")
        print("    at all. The signal is real, it is dense, and it is ours.")
    print(f"\n    PREDICTION — replicates out of time, but not the part anyone expected.")
    for key, _ in OUTCOMES:
        a0 = ladder[(key, 0)][0]
        a1 = ladder[(key, 1)][0]
        a2 = ladder[(key, 2)][0]
        a3 = ladder[(key, 3)][0]
        print(f"      {key:10}  M0 {a0:.3f}  ->  +level {a1:.3f} ({a1-a0:+.3f})"
              f"  ->  +volatility {a2:.3f} ({a2-a1:+.3f})"
              f"  ->  +DIRECTION {a3:.3f} ({a3-a2:+.3f})")
    print("      The prior model's information is a coin flip out of time, exactly as")
    print("      it was when measured on the product-level flag. Adding a field that has")
    print("      been in every row for weeks — how many sizes are in stock — is worth")
    print(f"      {ladder[('y_soldout',1)][0]-ladder[('y_soldout',0)][0]:+.3f} AUC and a "
          f"{ladder[('y_soldout',1)][2]:.2f}x top decile. Adding the DIRECTION of the")
    print("      curve, after volatility, is worth nothing on either outcome.")
    print("\n    WHAT TO BUILD: the level, the restock, and the shape. Not the velocity.")
    print("      • 'one size left' is a real 13-day forecast and needs no model.")
    print("      • 'restocked twice' is an outlier for a third of eligible brands and no")
    print("        competitor can see it, because it requires having watched every day.")
    print("      • 'you under-buy XL' is stable in every window and survives dropping any")
    print("        single brand. It is the only claim here that is about the TIER rather")
    print("        than about one piece, which is also the only one that scales to a")
    print("        report you can sell before you have the customer's own data.")
    print("\n    WHAT WOULD CHANGE THE ANSWER: variant-level inventory quantity. Shopify")
    print("    publishes it on some stores. With quantity this stops being an")
    print("    availability flag and becomes units, and every 'not units sold' caveat in")
    print("    this file disappears. Without it, no amount of modelling gets there —")
    print("    that is instrumentation, not analysis.")

    rule("DONE")


if __name__ == "__main__":
    main()
