#!/usr/bin/env python3
"""Loupe — demand PERSISTENCE, stockout TIMING, the size run, and the noise floor.

WHY THIS EXISTS

Three predictive claims have been built on this archive and all three died:

  • "what users save predicts what sells out"      — analyze_demand_signal.py
    brand-stratified RR ~1.00, and adding the user signal to an archive-only
    model moved out-of-time AUC by ~0.000.
  • "the direction of the size curve is demand"    — analyze_size_curve.py
    a restock predicts sell-out as strongly as a depletion. It measures liveness.
  • "we can rank sell-out risk"                    — the archive-only model
    in-window it looked fine; out of time it returned AUC 0.519 and a top decile
    WORSE than random.

Each death was investigated as a modelling failure. None of them was ever
checked against the one quantity that decides whether ranking sell-out risk is
possible AT ALL, which is how much of a product's demand rate carries from one
period to the next.

Call it ρ. It is the autocorrelation of a variant's underlying demand rate
between two windows. It is not a modelling choice; it is a property of the
market. Simulate an ORACLE that knows a piece's true demand rate and rank
sell-out risk with it: at ρ = 1 that oracle scores ~0.94 AUC and at ρ = 0 it
scores 0.50, because at ρ = 0 the past contains literally nothing about the
future and no feature, no vendor, no extra data source can change that.

So ρ is the ceiling. Every model ever built here has been fitted under it
without anyone knowing where it was. That is what section 3 measures, and
section 4 converts into the AUC that is physically available.

Sections 5-7 then answer the three follow-ons that the same panel supports:

  5.  STOCKOUT TIMING. Jain, Rudi & Wang (2015, Operations Research 63(1)) show
      that WHEN in a period a variant went out is a sufficient statistic for
      demand under Poisson/Normal assumptions, and that observing it rather than
      the bare stockout event eliminates 76.01% of the expected-profit loss.
      Timing is latent in every day of this archive and has never been extracted.

  6.  THE SIZE RUN. The "XL depletes first" finding died on a brand-level sign
      test, 26 above expectation to 21 below, p = 0.560. That test had about a
      quarter of the power it needed. A 25%-power null establishes nothing in
      either direction, so the question is reopened here with the unit it needed
      (the brand), a statistic that uses magnitude instead of throwing it away
      (a cluster-robust z), the variance bug fixed, and the power printed next
      to the answer instead of after it.

  7.  THE ARITHMETIC FLOOR. Demand is a counting process, so forecast accuracy
      has a floor set by Poisson noise alone: at ~10 units per SKU-week the best
      achievable WAPE is ~25%, which is roughly what Zara achieves with full
      internal data. Below that the floor rises fast. This section computes what
      volume these brands actually run at, at every level of aggregation, and
      states the floor at each — so the founder knows which claims are physically
      available to him before he tries to build one.

────────────────────────────────────────────────────────────────────────────
PRE-REGISTRATION — WRITTEN BEFORE THE NUMBERS EXISTED
────────────────────────────────────────────────────────────────────────────

The whole point of measuring ρ is that it can come back small. Interpreting it
afterwards is how a company talks itself out of an inconvenient measurement, so
the interpretation is fixed here, in code, as RHO_BANDS, and printed by section
1 BEFORE section 3 computes anything.

  ρ ≥ 0.50   THE PREDICTION BUSINESS WORKS. Past demand carries. An oracle
             ceiling around 0.80+ AUC, top-decile lift ≥ 2x, which is enough for
             a brand to act on. The three deaths above were modelling failures
             and are worth another pass.

  0.25 ≤ ρ < 0.50  MARGINAL, AND ONLY IN AGGREGATE. Ceiling roughly 0.62-0.78.
             Per-SKU risk scores are not defensible; brand- or category-level
             statements might be, because averaging over many SKUs recovers
             signal that no individual SKU carries.

  ρ < 0.25   THE PREDICTION BUSINESS DOES NOT WORK, AND NOT BECAUSE OF US.
             Ceiling under ~0.62. No feature set and no additional data source
             fixes it: there is nothing in the past of an individual piece to
             find. That is a fact about fashion — short lifecycles, one-off
             assortments, tiny buys — not about this company's instrumentation.
             The correct response is to stop buying data to fix it and sell what
             the archive describes rather than what it predicts.

A second pre-registered comparison, because it is the one that actually settles
whether the prior failures were real: the archive-only model achieved 0.519
out of time. Section 4 computes the ceiling implied by the measured ρ.

  • ceiling ≈ 0.52   -> 0.519 was NOT a bad model. It was at the ceiling. Every
                       subsequent effort spent on features was spent under a lid.
  • ceiling ≥ 0.70   -> 0.519 WAS a bad model, the signal exists, and the right
                       response is better modelling rather than a pivot.

Section 5 pre-registers: timing beats the binary indicator if it produces a
strictly higher out-of-time rank correlation at the brand level, with a
brand-clustered bootstrap CI on the DIFFERENCE that excludes zero.

Section 6 pre-registers: the ordinal size claim survives if the mean within-
brand Kendall tau between the window-A and window-B size-hazard rankings is
positive with a brand-level CI excluding zero, in the presence of a positive
control on the same code path that returns tau near 1.

────────────────────────────────────────────────────────────────────────────
WHAT WILL FOOL YOU HERE — GUARDS, EACH ENFORCED RATHER THAN DOCUMENTED
────────────────────────────────────────────────────────────────────────────

1.  THE CLONE, NOT THE DATA, DECIDES HOW LONG THE ARCHIVE IS.
    Everything is reconstructed from `git log`. A shallow clone yields a shorter
    dataset, a well-formed answer, and no error; on 2026-08-01 that silently
    cost a sibling script 14 of 42 days. Hard stop, checked first.

2.  DISAPPEARANCE IS ~HALF OUR OWN SCRAPER.
    Until 2026-08-06 build_catalog.py took the 60 most recently published items
    per store and 88 of 173 brands sat at that cap, so their tracked shelf
    ROTATED. Whole-brand rotation accounted for 981 of 2,041 disappearances in
    one measured window. NOTHING here is measured on absence. Every state is
    read off the store's own in-stock size list for a product that is physically
    present in the snapshot, which no amount of slice rotation can fake.

3.  THE SAMPLING EPOCH.
    2026-08-06 raised the walk depth. build_price_history.py declares it and
    gives it a 3-day settle window. Every window here stops at the last
    pre-epoch snapshot, and the boundary is DETECTED from that module's own
    constants rather than retyped.

4.  THE GRACE WINDOW FREEZES SIZES.
    A failed scrape carries yesterday's row forward with `stale: true` and the
    sizes frozen. A frozen size set is not evidence of no depletion. Stale rows
    are dropped from the panel entirely — not just at endpoints, which is what a
    day-by-day panel requires.

5.  A SIZE THAT VANISHES COMES BACK 22.6% OF THE TIME.
    Ten brands hold 63% of the short-gap flicker. A depletion is only counted
    when the size stays absent for RESTOCK_MIN_ABSENCE consecutive snapshots (or
    to the end of the record). A restock likewise.

6.  THE ABSORBING STATE IMPERSONATES PERSISTENCE.
    This is the specific trap for THIS question and it is the reason the answer
    is reported three ways. A variant that sold out in June and never came back
    is out of stock in window A and out of stock in window B, and a naive
    correlation of "days out of stock" reads that as perfect demand persistence
    when it is one event that never reversed. The headline estimator is
    therefore the FLOW (depletion hazard per at-risk day), which a permanently
    sold-out variant cannot contribute to because it has no at-risk days. The
    naive state version is reported alongside, labelled as the upper bound it is.

7.  POISSON NOISE MASQUERADES AS AN ABSENT SIGNAL.
    An observed between-window correlation is the true ρ multiplied by the
    reliability of each window's estimate. At these volumes reliability is not
    close to 1, so the raw correlation UNDERSTATES ρ, possibly by a lot. Both
    are reported: the raw figure is what a predictor could actually achieve from
    one window of history, the disattenuated figure is the property of the
    market. Reliability is estimated by method of moments off the Poisson /
    binomial sampling variance, not by an assumption.

8.  A COUNT THAT LANDS ON A ROUND NUMBER IS SUSPECTED TRUNCATION.
    A query here once returned exactly 100 rows for a 1,581-row answer and
    produced a confident, wrong 2x2. Every population count is checked.

9.  A NULL IS ONLY WORTH READING IF THE SAME CODE PATH CAN FIND SOMETHING.
    Every section carries a positive control that must fire. Section 2 pushes
    synthetic panels with KNOWN ρ = 1 and ρ = 0 through the exact estimator used
    on the real data; sections 5 and 6 carry their own.

WHAT THIS CANNOT BE

An availability flag is not units. A piece stocked one-deep that sells out is
one unit; a piece stocked forty-deep that sells out is forty. Every count below
is a LOWER BOUND on units, and section 7 says so in the direction it matters:
the true unit volumes are higher than the table shows, so the true accuracy
floors are slightly better than the table shows. True per-variant units exist on
33 stores via `inventory_quantity` and section 0 reports exactly how many days
of them are on disk.

USAGE
    python analyze_persistence.py                 # full run
    python analyze_persistence.py --refresh       # ignore the cache, re-extract
    python analyze_persistence.py --quick         # fewer bootstrap/sim draws
    python analyze_persistence.py --allow-shallow
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

# Non-ASCII brand names are the rule here (SIEDRÉS, DémodéMODÉ, With Jéan) and a
# Windows console defaults to cp1252, which dies on the first accented character
# AFTER the expensive part of the run. Reconfigure up front.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
CATALOG_REL = "loupe-feed/catalog.json"
SHELF_REL = "loupe-feed/shelf.json"
STOCK_REL = "loupe-feed/stock.json"

# Outside the repo on purpose: ~15 MB of extracted snapshots, 100% re-derivable
# from git in a couple of minutes, and a public repo does not need them.
CACHE = pathlib.Path(tempfile.gettempdir()) / "loupe_persistence_cache"
SEP = "\x1f"

ALPHA = 0.05
POWER = 0.80

# Guard 5. A size is only "gone" once it has stayed gone this many consecutive
# snapshots — or to the end of the record, which is the same statement about a
# variant we simply have not watched long enough to see return.
RESTOCK_MIN_ABSENCE = 3

# Guard 6/7. A variant contributes to the flow estimator only with enough
# exposure for its rate to mean anything. Both windows, both thresholds.
MIN_AT_RISK_DAYS = 7        # calendar days in stock, per window
MIN_OBS_DAYS = 5            # snapshots on which the variant's state was readable

LETTER_SCALE = ["XXS", "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL"]
LETTER_IDX = {s: i for i, s in enumerate(LETTER_SCALE)}

# ── THE PRE-REGISTRATION, AS CODE ────────────────────────────────────────────
# Printed by section 1 before section 3 runs. Changing a band after seeing the
# estimate would be visible in `git diff`, which is the entire point of putting
# it here rather than in a paragraph someone writes afterwards.
RHO_BANDS = [
    (0.50, 1.01, "THE PREDICTION BUSINESS WORKS",
     "past demand carries; oracle ceiling ~0.80+ AUC; per-SKU risk scores are "
     "defensible and the three prior deaths deserve another pass"),
    (0.25, 0.50, "MARGINAL — AGGREGATE ONLY",
     "ceiling ~0.62-0.78; brand/category statements may survive, per-SKU ones "
     "do not"),
    (-1.00, 0.25, "THE PREDICTION BUSINESS DOES NOT WORK",
     "ceiling under ~0.62; no feature set and no data purchase fixes it; this is "
     "a fact about fashion, not about Loupe's instrumentation"),
]

# The number the ceiling has to be compared against. Measured, out of time, by
# analyze_demand_signal.py section 7 on the archive-only model. Quoted here as
# the thing under test, never as a result of this file.
PRIOR_OUT_OF_TIME_AUC = 0.519

ROUND_NUMBERS = {100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000}


# ══════════════════════════════════════════════════════════════════════════
# git / archive
# ══════════════════════════════════════════════════════════════════════════

def git(*args):
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


def history_is_truncated():
    """True when this clone cannot see the repo's whole history (guard 1)."""
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        return True
    git_dir = git("rev-parse", "--git-dir").strip()
    if not git_dir:
        return False
    return (REPO / git_dir / "shallow").exists() or pathlib.Path(git_dir, "shallow").exists()


def daily_snapshots(path=CATALOG_REL):
    """(day, sha) for the LAST commit of each day that touched `path`.

    Lifted deliberately from build_price_history.py rather than reinvented: the
    two files must agree about what a "day" is, or a boundary declared in one
    lands on a different snapshot in the other.
    """
    out = {}
    for line in git("log", "--format=%H|%ad", "--date=short", "--", path).splitlines():
        if "|" in line:
            sha, day = line.split("|", 1)
            out.setdefault(day.strip(), sha.strip())   # log is newest-first
    return sorted(out.items())


FIELDS = ["id", "brand", "price", "category", "available", "addedAt",
          "stale", "retailer", "sizes", "hasSizesKey"]


def extract_snapshots(refresh=False, verbose=True):
    """One compact TSV per snapshot day. ~25x smaller than the JSON it came from."""
    CACHE.mkdir(parents=True, exist_ok=True)
    snaps = daily_snapshots()
    for day, sha in snaps:
        dest = CACHE / f"{day}.tsv"
        if dest.exists() and not refresh:
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
            print(f"  extracted {day}  {len(doc.get('products', [])):>6} products",
                  file=sys.stderr)
    return [d for d, _ in snaps]


# ══════════════════════════════════════════════════════════════════════════
# size normalisation — identical rules to analyze_size_curve.py
# ══════════════════════════════════════════════════════════════════════════
#
# Deliberately small. The goal is to make 'Medium' and 'M' one label, NOT to
# invent a universal size ontology: mapping 'IT 40' onto 'S' would be a fashion
# opinion, and a wrong one for half the brands here. Numeric and regional scales
# are left alone — they only ever have to match themselves, within one product,
# across two days.

_SPELLED = {
    "XXSMALL": "XXS", "EXTRAEXTRASMALL": "XXS",
    "XSMALL": "XS", "EXTRASMALL": "XS",
    "SMALL": "S", "MEDIUM": "M", "LARGE": "L",
    "XLARGE": "XL", "EXTRALARGE": "XL",
    "XXLARGE": "2XL", "XXL": "2XL",
    "XXXL": "3XL", "XXXXL": "4XL",
}

# Merchandising text some stores append to a size value. 'OS - 1 unit left' is
# the dangerous one: it MUTATES as stock changes, so leaving it in manufactures
# a remove-plus-add pair out of a store selling one unit.
_NOISE_RE = re.compile(
    r"(-?\s*PRE-?ORDER)"
    r"|(\d+\s*UNITS?\s*LEFT)"
    r"|(SOLDOUT)|(LOWSTOCK)|(FINALSALE)|(BACKORDER)"
)


def norm_size(value):
    t = str(value or "").upper()
    t = t.replace("–", "-").replace("—", "-").replace("’", "'")
    t = re.sub(r"\s+", "", t)
    t = _NOISE_RE.sub("", t)
    t = t.strip(" -/.,")
    return _SPELLED.get(t, t)


# ══════════════════════════════════════════════════════════════════════════
# statistics
# ══════════════════════════════════════════════════════════════════════════

def check_not_round(n, label):
    """Guard 8. A population count landing exactly on a page-size boundary is
    treated as suspected truncation, not as data."""
    if n in ROUND_NUMBERS:
        print(f"  !! {label} = {n:,} lands exactly on a round number. Verify this "
              "is not a truncated read before quoting it.")


def auc(scores, labels):
    """Rank-based AUC (Mann-Whitney). NaN when one class is empty."""
    scores, labels = np.asarray(scores, float), np.asarray(labels, bool)
    pos, neg = scores[labels], scores[~labels]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    r = stats.rankdata(np.concatenate([pos, neg]))
    return (r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def cluster_bootstrap_corr(x, y, clusters, nboot=1000, seed=0, kind="spearman"):
    """Correlation with a CI that respects the fact that variants are not
    independent draws.

    Twenty variants of one dress share a brand, a buyer, a season and a
    photograph. Bootstrapping rows would treat them as twenty facts; this
    resamples CLUSTERS (brands, unless told otherwise) and takes every row they
    carry, which is the only resampling scheme whose CI means anything at the
    tier level. One brand supplying a quarter of an effect is how the last
    finding here died.
    """
    x, y = np.asarray(x, float), np.asarray(y, float)
    clusters = np.asarray(clusters)
    if len(x) < 4:
        return float("nan"), float("nan"), float("nan"), len(x)
    f = stats.spearmanr if kind == "spearman" else stats.pearsonr
    point = float(f(x, y)[0])

    uniq, inv = np.unique(clusters, return_inverse=True)
    by = [np.flatnonzero(inv == i) for i in range(len(uniq))]
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(nboot):
        pick = rng.integers(0, len(uniq), len(uniq))
        idx = np.concatenate([by[i] for i in pick])
        if len(idx) < 4:
            continue
        xa, ya = x[idx], y[idx]
        if xa.std() == 0 or ya.std() == 0:
            continue
        draws.append(f(xa, ya)[0])
    if len(draws) < 20:
        return point, float("nan"), float("nan"), len(x)
    lo, hi = np.percentile(draws, [100 * ALPHA / 2, 100 * (1 - ALPHA / 2)])
    return point, float(lo), float(hi), len(x)


def poisson_reliability(k, T):
    """How much of the observed spread in rates is signal rather than Poisson noise.

    Guard 7. For k ~ Poisson(λT) the estimate k/T has sampling variance λ/T, so
      Var(observed rates) = Var(λ) + E[λ/T]
    and the reliability of a single window's estimate is Var(λ)/Var(observed).
    Method of moments, no distributional assumption beyond the counting process
    itself. Returns (reliability, var_observed, var_noise).
    """
    k, T = np.asarray(k, float), np.asarray(T, float)
    rate = k / T
    v_obs = float(rate.var(ddof=1))
    v_noise = float((rate / T).mean())        # E[λ/T], λ estimated by the rate
    if v_obs <= 0:
        return float("nan"), v_obs, v_noise
    return float(np.clip(1 - v_noise / v_obs, 0.0, 1.0)), v_obs, v_noise


def binomial_reliability(k, n):
    """The same idea for a share: p̂(1-p̂)/(n-1) is unbiased for p(1-p)/n."""
    k, n = np.asarray(k, float), np.asarray(n, float)
    p = k / n
    v_obs = float(p.var(ddof=1))
    v_noise = float((p * (1 - p) / np.maximum(n - 1, 1)).mean())
    if v_obs <= 0:
        return float("nan"), v_obs, v_noise
    return float(np.clip(1 - v_noise / v_obs, 0.0, 1.0)), v_obs, v_noise


def disattenuate(r, rel_a, rel_b):
    """Kelley's correction: the correlation two perfectly-measured windows would
    have shown. Capped at 1.0 — a correction that returns 1.4 is telling you the
    reliability estimate is noisy, not that the market is more than perfectly
    persistent."""
    if not (rel_a > 0 and rel_b > 0):
        return float("nan")
    return float(min(1.0, r / math.sqrt(rel_a * rel_b)))


def sign_test_power(n, p_true, alpha=ALPHA):
    """Power of a two-sided binomial sign test at n trials against p_true.

    This is the calculation that was missing when 26-vs-21 was read as evidence
    of absence. Exact, not normal-approximated: at n = 47 the approximation is
    already loose enough to matter.
    """
    if n < 2:
        return float("nan")
    lo = stats.binom.ppf(alpha / 2, n, 0.5)
    hi = stats.binom.ppf(1 - alpha / 2, n, 0.5)
    # exact rejection region under H0 (conservative, as a real sign test is)
    while lo >= 0 and stats.binom.cdf(lo, n, 0.5) > alpha / 2:
        lo -= 1
    while hi <= n and stats.binom.sf(hi - 1, n, 0.5) > alpha / 2:
        hi += 1
    return float(stats.binom.cdf(lo, n, p_true) + stats.binom.sf(hi - 1, n, p_true))


def sign_test_n_for_power(p_true, target=POWER, alpha=ALPHA, nmax=4000):
    for n in range(4, nmax):
        if sign_test_power(n, p_true, alpha) >= target:
            return n
    return None


def mdes(sd, n, alpha=ALPHA, power=POWER):
    """Minimum detectable effect, in the units of the thing measured, for a mean
    of n independent cluster-level values with the given standard deviation."""
    if n < 2 or not sd > 0:
        return float("nan")
    z = stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power)
    return float(z * sd / math.sqrt(n))


def corr_mde(n, alpha=ALPHA, power=POWER):
    """Smallest correlation detectable with n independent clusters (Fisher z)."""
    if n < 5:
        return float("nan")
    z = (stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power)) / math.sqrt(n - 3)
    return float(math.tanh(z))


def wape_floor(lam):
    """The best WAPE any forecaster can achieve against Poisson(λ) demand.

    WAPE = E|X - f| / E[X]. The f that minimises E|X - f| is the MEDIAN, not the
    mean, so two floors are returned: the median-forecast floor (the true, lowest
    possible one) and the mean-forecast floor (what a conventional point forecast
    achieves, and the one the ~25%-at-10-units figure refers to).

    Nothing about this is a modelling choice. It is the mean absolute deviation
    of a counting process divided by its own mean, and no amount of data moves it.
    """
    if lam <= 0:
        return float("nan"), float("nan")
    kmax = int(lam + 12 * math.sqrt(lam) + 40)
    ks = np.arange(0, kmax + 1)
    pk = stats.poisson.pmf(ks, lam)
    med = stats.poisson.ppf(0.5, lam)
    return (float((pk * np.abs(ks - med)).sum() / lam),
            float((pk * np.abs(ks - lam)).sum() / lam))


def lam_for_wape(target, lo=1e-3, hi=1e7):
    """The unit volume per cell per period at which the mean-forecast floor
    reaches `target`. Answers "how big does the bucket have to be"."""
    f = lambda x: wape_floor(x)[1] - target
    if f(hi) > 0 or f(lo) < 0:
        return float("nan")
    return float(optimize.brentq(f, lo, hi, xtol=1e-6))


# ══════════════════════════════════════════════════════════════════════════
# the variant panel
# ══════════════════════════════════════════════════════════════════════════
#
# UNIT OF OBSERVATION: (product id, normalised size label) on a snapshot day.
# STATE: 1 in stock, 0 out of stock, UNOBS unreadable.
#
# The OFFERED size run of a product is reconstructed as the UNION of every size
# ever seen in stock across the whole archive. That is a LOWER BOUND — a size
# that was already sold out on 2026-06-23 and never restocked is invisible and
# the piece looks like it was never offered in that size. shelf.json now records
# the store's true `sizesOffered`, which would remove the guess entirely; section
# 0 reports how many days of it exist, because on the day this was written the
# answer was zero and using it was not an option.
#
# `available` is not used for the state. It first appears on 2026-07-16, which is
# most of the way through the archive, and it is redundant where it exists: a
# sized product whose size set has emptied carries available == false on every
# day both have been checked. Reading the size list instead buys the 24 days
# before the flag existed, which is what makes an out-of-time split possible.

UNOBS = 2


class Panel:
    """The whole archive as a variant × day state matrix, plus product metadata."""

    def __init__(self, days, verbose=True):
        self.days = days
        self.dates = [dt.date.fromisoformat(d) for d in days]
        self.ndays = len(days)

        self.brand, self.category, self.price, self.retailer = {}, {}, {}, {}
        self.size_labels = []
        self._size_idx = {}
        self.stale_rows = 0
        self.present_rows = 0
        self.rows_with_size_key = 0

        # pass 1 — per-day masks of in-stock sizes, and the union per product
        day_masks = []
        offered = collections.defaultdict(int)
        present = collections.defaultdict(int)     # bitset of day indices
        for i, d in enumerate(days):
            masks = {}
            for pid, r in self._load_day(d).items():
                self.present_rows += 1
                if r["stale"]:                     # guard 4 — frozen row
                    self.stale_rows += 1
                    continue
                if r["hasSizesKey"]:
                    self.rows_with_size_key += 1
                m = 0
                for raw in (r["sizes"].split(SEP) if r["sizes"] else []):
                    if not raw:
                        continue
                    s = norm_size(raw)
                    if not s:
                        continue
                    j = self._size_idx.get(s)
                    if j is None:
                        j = self._size_idx[s] = len(self.size_labels)
                        self.size_labels.append(s)
                    m |= 1 << j
                masks[pid] = m
                offered[pid] |= m
                present[pid] |= 1 << i
                self.brand[pid] = r["brand"]
                self.category[pid] = r["category"]
                self.retailer[pid] = r["retailer"]
                try:
                    self.price[pid] = float(r["price"] or 0)
                except ValueError:
                    self.price[pid] = 0.0
            day_masks.append(masks)
            if verbose:
                print(f"  panel {d}  {len(masks):>6} readable rows", file=sys.stderr)

        # pass 2 — materialise the state series for every (product, offered size)
        self.state = {}
        self.offered = {}
        for pid, m in offered.items():
            if m == 0:
                continue                          # no size option ever: not a variant
            bits = [j for j in range(len(self.size_labels)) if m >> j & 1]
            self.offered[pid] = bits
            pres = present[pid]
            for j in bits:
                arr = bytearray([UNOBS]) * self.ndays
                for i in range(self.ndays):
                    if not (pres >> i & 1):
                        continue
                    arr[i] = 1 if (day_masks[i].get(pid, 0) >> j & 1) else 0
                self.state[(pid, j)] = arr
        del day_masks

    _cache = collections.OrderedDict()
    _CACHE_MAX = 2

    @staticmethod
    def _load_day(day):
        """One day's rows. Bounded LRU: the panel build is a single forward pass,
        so holding the whole archive as parsed dicts would cost ~1 GB to save a
        re-read that takes a tenth of a second."""
        if day in Panel._cache:
            Panel._cache.move_to_end(day)
            return Panel._cache[day]
        out = {}
        with open(CACHE / f"{day}.tsv", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                out[r["id"]] = r
        Panel._cache[day] = out
        while len(Panel._cache) > Panel._CACHE_MAX:
            Panel._cache.popitem(last=False)
        return out

    # ---- spell structure ---------------------------------------------------

    def observed(self, v):
        """Snapshot indices on which this variant's state was readable."""
        arr = self.state[v]
        return [i for i in range(self.ndays) if arr[i] != UNOBS]

    def onsets(self, v, obs=None):
        """CONFIRMED depletions: (last-in-stock index, first-out index).

        Guard 5. An in->out step counts only when the size then stays absent for
        RESTOCK_MIN_ABSENCE consecutive readable snapshots, or to the end of the
        record. 22.6% of raw day-to-day size losses reverse and ten stores own
        most of them, so without this the estimator would mostly measure Orseund
        Iris's storefront.
        """
        arr = self.state[v]
        obs = obs if obs is not None else self.observed(v)
        out, m = [], len(obs)
        for t in range(m - 1):
            if arr[obs[t]] == 1 and arr[obs[t + 1]] == 0:
                u, run = t + 1, 0
                while u < m and arr[obs[u]] == 0:
                    run += 1
                    u += 1
                if run >= RESTOCK_MIN_ABSENCE or u >= m:
                    out.append((obs[t], obs[t + 1]))
        return out

    def restocks(self, v, obs=None):
        """CONFIRMED restocks: (first-out index, first-back-in index)."""
        arr = self.state[v]
        obs = obs if obs is not None else self.observed(v)
        out, m, t = [], len(obs), 0
        while t < m:
            if arr[obs[t]] == 0:
                u = t
                while u < m and arr[obs[u]] == 0:
                    u += 1
                if u - t >= RESTOCK_MIN_ABSENCE and u < m:
                    out.append((obs[t], obs[u]))
                t = u
            else:
                t += 1
        return out

    def at_risk_days(self, v, obs=None, lo=None, hi=None):
        """Calendar days spent in stock, i.e. days on which a depletion could
        have happened. Calendar rather than snapshot count because the refresh
        workflow did not run on 2026-07-25..28 and a rate denominated in
        snapshots would silently price that gap at one day."""
        arr, D = self.state[v], self.dates
        obs = obs if obs is not None else self.observed(v)
        tot = 0.0
        for t in range(len(obs) - 1):
            i = obs[t]
            if arr[i] == 1 and (lo is None or lo <= i <= hi):
                tot += (D[obs[t + 1]] - D[i]).days
        return tot


def window_stats(panel, v, lo, hi):
    """Everything section 3 needs about one variant inside one window.

    Onsets are DETECTED on the full record (so the confirmation rule can look
    past the window edge, which is information about the world rather than about
    the outcome) and ATTRIBUTED to the window containing the last in-stock day.
    """
    arr = panel.state[v]
    obs_all = panel.observed(v)
    obs = [i for i in obs_all if lo <= i <= hi]
    if not obs:
        return None
    n_obs = len(obs)
    n_out = sum(1 for i in obs if arr[i] == 0)
    k = sum(1 for a, _ in panel.onsets(v, obs_all) if lo <= a <= hi)
    T = panel.at_risk_days(v, obs_all, lo, hi)
    return {"n_obs": n_obs, "n_out": n_out, "share_out": n_out / n_obs,
            "onsets": k, "at_risk": T,
            "in_at_start": arr[obs[0]] == 1, "in_at_end": arr[obs[-1]] == 1}


# ══════════════════════════════════════════════════════════════════════════
# reporting helpers
# ══════════════════════════════════════════════════════════════════════════

def rule(title=""):
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def fmt_ci(r, lo, hi, w=5, p=3):
    if any(math.isnan(x) for x in (r, lo, hi)):
        return f"{r:{w}.{p}f}  95% CI [   n/a ]"
    return f"{r:{w}.{p}f}  95% CI [{lo:6.3f}, {hi:6.3f}]"


def band_for(rho):
    for lo, hi, name, why in RHO_BANDS:
        if lo <= rho < hi:
            return name, why
    return "OUT OF RANGE", ""


# ══════════════════════════════════════════════════════════════════════════
# section 4 — what AUC a given ρ physically allows
# ══════════════════════════════════════════════════════════════════════════

def simulate_auc(rho, sigma, lam_units, depth_mean, n=200_000, seed=7):
    """Rank sell-out risk in window B, knowing window A. Returns (oracle, feasible).

    The generative model is the smallest one that can express the question:
      • a variant has a latent log demand rate; between windows it decays toward
        the mean at rate ρ, which is the definition being measured;
      • units sold in a window are Poisson in that rate — demand is a counting
        process, which is the whole reason section 7 exists;
      • the piece is stocked `depth` units deep and "sells out" when cumulative
        sales reach it.

    ORACLE   scores by the variant's TRUE window-A rate. Unattainable; it is the
             lid, and the only thing that lowers it is ρ itself plus the Poisson
             noise between a rate and a realised sell-out.
    FEASIBLE scores by what window A actually SHOWED — an observed count. That
             is the number a real model is fitted on, so it is the honest ceiling
             for anything buildable here.
    """
    rng = np.random.default_rng(seed)
    a = rng.normal(0.0, sigma, n)
    b = rho * a + math.sqrt(max(0.0, 1 - rho * rho)) * rng.normal(0.0, sigma, n)
    depth_a = 1 + rng.poisson(max(depth_mean - 1, 0.0), n)
    depth_b = 1 + rng.poisson(max(depth_mean - 1, 0.0), n)
    sales_a = rng.poisson(lam_units * np.exp(a))
    sales_b = rng.poisson(lam_units * np.exp(b))
    out_b = sales_b >= depth_b
    if out_b.all() or not out_b.any():
        return float("nan"), float("nan")
    return auc(a, out_b), auc(np.minimum(sales_a, depth_a), out_b)


def calibrate_lambda(sigma, depth_mean, target_rate, n=200_000, seed=7):
    """The per-window unit volume that reproduces the observed sell-out rate."""
    def f(lu):
        rng = np.random.default_rng(seed)
        b = rng.normal(0.0, sigma, n)
        depth = 1 + rng.poisson(max(depth_mean - 1, 0.0), n)
        return (rng.poisson(lu * np.exp(b)) >= depth).mean() - target_rate
    lo, hi = 1e-4, 200.0
    if f(lo) > 0 or f(hi) < 0:
        return float("nan")
    return float(optimize.brentq(f, lo, hi, xtol=1e-4))


def calibrate_sigma_for_auc(target_auc, depth_mean, base_rate, n=120_000, seed=7):
    """The rate dispersion at which a ρ = 1 oracle scores `target_auc`.

    Used only to reproduce the curve the founder was quoted (0.94 at ρ = 1) so
    that the measured ρ can be read off THAT curve as well as off the one this
    catalog's own dispersion implies. Two curves, one measured ρ, and the reader
    can see whether the conclusion depends on the assumption.
    """
    def f(sig):
        lu = calibrate_lambda(sig, depth_mean, base_rate, n=n, seed=seed)
        if math.isnan(lu):
            return -1.0
        return simulate_auc(1.0, sig, lu, depth_mean, n=n, seed=seed)[0] - target_auc
    lo, hi = 0.05, 6.0
    try:
        if f(lo) > 0 or f(hi) < 0:
            return float("nan")
        return float(optimize.brentq(f, lo, hi, xtol=1e-3))
    except (ValueError, RuntimeError):
        return float("nan")


def auc_at(rho, rhos, aucs):
    """Linear read-off of the simulated curve at the measured ρ."""
    return float(np.interp(np.clip(rho, 0, 1), rhos, aucs))


# ══════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="ignore the cache, re-extract")
    ap.add_argument("--quick", action="store_true", help="fewer bootstrap and sim draws")
    ap.add_argument("--allow-shallow", action="store_true")
    args = ap.parse_args()
    NBOOT = 200 if args.quick else 1000
    NSIM = 60_000 if args.quick else 200_000

    # ── 0. INPUT VERIFICATION ─────────────────────────────────────────────
    rule("0. INPUT VERIFICATION — verify the inputs before trusting the outputs")
    truncated = history_is_truncated()
    if truncated and not args.allow_shallow:
        sys.exit("REFUSING TO RUN: shallow/grafted clone. `git fetch --unshallow` first.\n"
                 "  Everything here is reconstructed from `git log`, so a shallow clone\n"
                 "  yields a SHORTER dataset, a well-formed answer, and no error. On\n"
                 "  2026-08-01 that silently cost a sibling script 14 of 42 days.")
    print(f"  clone complete (not shallow)          : {not truncated}")

    all_days = extract_snapshots(refresh=args.refresh, verbose=False)
    print(f"  catalog snapshots in git              : {len(all_days)}  "
          f"({all_days[0]} -> {all_days[-1]})")
    check_not_round(len(all_days), "catalog snapshots")
    span = (dt.date.fromisoformat(all_days[-1]) - dt.date.fromisoformat(all_days[0])).days + 1
    have = set(all_days)
    missing = [(dt.date.fromisoformat(all_days[0]) + dt.timedelta(d)).isoformat()
               for d in range(span)]
    missing = [d for d in missing if d not in have]
    print(f"  calendar days spanned                 : {span}   MISSING {len(missing)}: "
          f"{', '.join(missing) if missing else 'none'}")
    if missing:
        print("    (the refresh workflow did not run on those days. Every duration below is")
        print("     in CALENDAR days, so a spell crossing the gap is bracketed rather than")
        print("     counted as one day — see at_risk_days().)")

    # Guard 3 — the sampling epoch, read from build_price_history.py rather than
    # retyped, so the two files cannot drift apart about where the boundary is.
    try:
        sys.path.insert(0, str(HERE))
        import build_price_history as BPH
        SAMPLING_EPOCHS = BPH.SAMPLING_EPOCHS
        SETTLE = BPH.EPOCH_SETTLE_DAYS
        src = "build_price_history.py"
    except Exception:                                     # noqa: BLE001
        SAMPLING_EPOCHS, SETTLE, src = ["2026-08-06"], 3, "fallback constant"
    print(f"  sampling epochs ({src}) : {SAMPLING_EPOCHS}, settle {SETTLE}d")

    def poisoned(day):
        return any(e <= day < (dt.date.fromisoformat(e)
                               + dt.timedelta(SETTLE)).isoformat()
                   for e in SAMPLING_EPOCHS)

    # `sizes` first appears partway into the archive; DETECT the boundary rather
    # than assume it, because a script that walks "the whole archive" and finds
    # nothing in the first week has silently analysed a shorter period than it
    # claimed.
    size_era = []
    for d in all_days:
        with open(CACHE / f"{d}.tsv", encoding="utf-8", newline="") as fh:
            n = sum(1 for r in csv.DictReader(fh, delimiter="\t") if r["hasSizesKey"])
        if n > 0:
            size_era.append(d)
    days = [d for d in size_era if not poisoned(d)]
    print(f"  days carrying a `sizes` key           : {len(size_era)}  "
          f"({size_era[0]} -> {size_era[-1]})")
    print(f"  usable after the sampling-epoch cut   : {len(days)}  "
          f"({days[0]} -> {days[-1]})")
    check_not_round(len(days), "usable snapshot days")

    # ── the units panel, which is the thing that would settle this properly ──
    print("\n  TRUE PER-VARIANT UNITS (`inventory_quantity`, 33 stores):")
    stock_days = daily_snapshots(STOCK_REL)
    shelf_days = daily_snapshots(SHELF_REL)
    on_disk = (HERE / "stock.json").exists()
    print(f"    stock.json committed days           : {len(stock_days)}"
          f"{'  -> ' + stock_days[0][0] + '..' + stock_days[-1][0] if stock_days else ''}")
    print(f"    stock.json present in working tree  : {on_disk}")
    print(f"    shelf.json committed days           : {len(shelf_days)}"
          f"{'  -> ' + shelf_days[0][0] + '..' + shelf_days[-1][0] if shelf_days else ''}")
    offered_days = 0
    for day, sha in shelf_days:
        raw = git("show", f"{sha}:{SHELF_REL}")
        try:
            if "sizesOffered" in json.loads(raw):
                offered_days += 1
        except ValueError:
            pass
    print(f"    shelf.json days carrying sizesOffered: {offered_days}")
    UNITS_AVAILABLE = len(stock_days) >= 2
    OFFERED_AVAILABLE = offered_days >= 2
    if not UNITS_AVAILABLE:
        print("\n    !! THE UNITS REPLICATION OF SECTION 3 CANNOT BE RUN.")
        print("       collect_stock() and INVENTORY_STORES landed in build_catalog.py")
        print("       today; the daily refresh that would first exercise them has not")
        print("       run since. A rate autocorrelation needs two periods and there are")
        print("       zero. This is not a limitation of the method, it is the age of the")
        print("       instrument, and it resolves itself at the next refresh — every")
        print("       number below is therefore computed on the AVAILABILITY process,")
        print("       which is a LOWER BOUND on units and is stated as one throughout.")
    if not OFFERED_AVAILABLE:
        print("\n    !! `sizesOffered` LIKEWISE HAS ZERO USABLE DAYS, so section 6 cannot")
        print("       separate 'they never stocked XL' from 'XL sold out' at source. It")
        print("       reconstructs the offered run as the UNION of every size ever seen")
        print("       in stock, which is a LOWER BOUND: a size that had already sold out")
        print("       before the archive opens is invisible. Section 6 quantifies what")
        print("       that costs rather than waving at it.")

    # ── 1. THE PRE-REGISTRATION, PRINTED BEFORE ANY ESTIMATE ──────────────
    rule("1. PRE-REGISTERED INTERPRETATION — read this before section 3")
    print("  ρ is the autocorrelation of a variant's underlying demand RATE between two")
    print("  windows. It is a property of the market, not of any model, and it is the lid")
    print("  on every sell-out-risk ranker anyone could ever build on this catalog.\n")
    for lo, hi, name, why in RHO_BANDS:
        rng_s = f"ρ >= {lo:.2f}" if hi > 1 else (f"ρ < {hi:.2f}" if lo < 0
                                                 else f"{lo:.2f} <= ρ < {hi:.2f}")
        print(f"    {rng_s:20} {name}")
        print(f"    {'':20} {why}")
    print(f"\n  And the comparison that decides whether the prior failures were real:")
    print(f"    the archive-only model scored {PRIOR_OUT_OF_TIME_AUC:.3f} out of time. If the")
    print("    ceiling implied by the measured ρ is about that, the model was AT THE LID")
    print("    and every hour since spent on features was spent under it. If the ceiling")
    print("    is 0.70+, the model was simply bad and the signal is still there.")

    # ── 2. THE PANEL, AND A POSITIVE CONTROL ON THE ESTIMATOR ─────────────
    rule("2. THE PANEL, AND A POSITIVE CONTROL THAT MUST FIRE")
    print("  Building the variant × day state matrix…")
    panel = Panel(days, verbose=False)
    n_var = len(panel.state)
    n_prod = len(panel.offered)
    print(f"    products with a size option           : {n_prod:,}")
    print(f"    variants (product × size)             : {n_var:,}")
    print(f"    distinct normalised size labels       : {len(panel.size_labels):,}")
    print(f"    rows dropped as stale (guard 4)       : {panel.stale_rows:,} of "
          f"{panel.present_rows:,} ({100*panel.stale_rows/max(panel.present_rows,1):.2f}%)")
    for lbl, v in (("products", n_prod), ("variants", n_var)):
        check_not_round(v, lbl)

    mid = len(days) // 2
    A = (0, mid - 1)
    B = (mid, len(days) - 1)
    print(f"\n  OUT-OF-TIME SPLIT (fit on the earlier window, evaluate on the later one)")
    print(f"    window A : {days[A[0]]} -> {days[A[1]]}   {A[1]-A[0]+1} snapshots, "
          f"{(panel.dates[A[1]]-panel.dates[A[0]]).days+1} calendar days")
    print(f"    window B : {days[B[0]]} -> {days[B[1]]}   {B[1]-B[0]+1} snapshots, "
          f"{(panel.dates[B[1]]-panel.dates[B[0]]).days+1} calendar days")
    print("    The windows are disjoint and adjacent. Nothing measured in B is used to")
    print("    construct anything in A, which is the failure mode that produced a model")
    print("    scoring well in-window and 0.519 out of it.")

    # ---- the estimator, defined once and used everywhere -------------------
    def flow_pairs(lo_a, hi_a, lo_b, hi_b, min_risk=MIN_AT_RISK_DAYS):
        """Variants with real exposure in BOTH windows, and their depletion rates."""
        ks_a, ts_a, ks_b, ts_b, ids = [], [], [], [], []
        for v in panel.state:
            sa = window_stats(panel, v, lo_a, hi_a)
            if not sa or sa["at_risk"] < min_risk:
                continue
            sb = window_stats(panel, v, lo_b, hi_b)
            if not sb or sb["at_risk"] < min_risk:
                continue
            ks_a.append(sa["onsets"]); ts_a.append(sa["at_risk"])
            ks_b.append(sb["onsets"]); ts_b.append(sb["at_risk"])
            ids.append(v)
        return (np.array(ks_a, float), np.array(ts_a, float),
                np.array(ks_b, float), np.array(ts_b, float), ids)

    print("\n  POSITIVE CONTROL. The same estimator, on synthetic panels with a KNOWN ρ.")
    print("  A null in section 3 is only worth reading if this returns ~1.00 and ~0.00 on")
    print("  data where the answer is not in question. It also shows how far Poisson noise")
    print("  alone drags an observed correlation below the truth at these exposures.\n")
    rngc = np.random.default_rng(11)
    n_ctl = max(n_var, 5000)
    T_typ = float(np.median([panel.at_risk_days(v) for v in
                             list(panel.state)[:4000]]) or 14.0) / 2.0
    T_typ = max(T_typ, 7.0)
    print(f"    synthetic n = {n_ctl:,} variants, exposure {T_typ:.0f} at-risk days per window")
    print(f"    {'true ρ':>8} {'observed r':>12} {'reliability':>12} {'disattenuated':>14}")
    ctl_ok = []
    for true_rho in (1.0, 0.5, 0.0):
        sig = 1.0
        a = rngc.normal(0, sig, n_ctl)
        b = true_rho * a + math.sqrt(max(0.0, 1 - true_rho ** 2)) * rngc.normal(0, sig, n_ctl)
        lam = 0.02 * np.exp(a)
        lamb = 0.02 * np.exp(b)
        ka = rngc.poisson(lam * T_typ).astype(float)
        kb = rngc.poisson(lamb * T_typ).astype(float)
        Ta = np.full(n_ctl, T_typ)
        r_obs = float(stats.pearsonr(ka / Ta, kb / Ta)[0])
        rel_a = poisson_reliability(ka, Ta)[0]
        rel_b = poisson_reliability(kb, Ta)[0]
        dis = disattenuate(r_obs, rel_a, rel_b)
        ctl_ok.append((true_rho, r_obs, dis))
        print(f"    {true_rho:8.2f} {r_obs:12.3f} {rel_a:12.3f} {dis:14.3f}")
    print("    The middle column is why guard 7 exists: at this exposure a market with")
    print("    PERFECT persistence shows a raw correlation far below 1, purely because a")
    print("    Poisson count is a noisy reading of a rate. Disattenuation recovers it.")
    CTL_PASS = (ctl_ok[0][2] > 0.80 and abs(ctl_ok[2][2]) < 0.15)
    print(f"    positive control PASSES: {CTL_PASS}   "
          f"(ρ=1 recovered as {ctl_ok[0][2]:.3f}, ρ=0 as {ctl_ok[2][2]:+.3f})")
    if not CTL_PASS:
        print("    !! CONTROL FAILED — every number below is uninterpretable. Stop here.")

    # ── 3. JOB 1 — ρ ──────────────────────────────────────────────────────
    rule("3. DEMAND PERSISTENCE ρ — the quantity that decides all of it")

    print("  (a) THE NAIVE READ, reported first so it can be disbelieved out loud.")
    print("      Correlate each variant's share of days OUT OF STOCK in A against B.")
    print("      This is the literal request, and it is contaminated by guard 6: a piece")
    print("      that sold out in June and never returned is 100% out in both windows and")
    print("      scores as perfect persistence on the strength of one event.\n")
    ka_s, na_s, kb_s, nb_s, ids_s = [], [], [], [], []
    for v in panel.state:
        sa = window_stats(panel, v, *A)
        sb = window_stats(panel, v, *B)
        if not sa or not sb or sa["n_obs"] < MIN_OBS_DAYS or sb["n_obs"] < MIN_OBS_DAYS:
            continue
        ka_s.append(sa["n_out"]); na_s.append(sa["n_obs"])
        kb_s.append(sb["n_out"]); nb_s.append(sb["n_obs"])
        ids_s.append(v)
    ka_s, na_s = np.array(ka_s, float), np.array(na_s, float)
    kb_s, nb_s = np.array(kb_s, float), np.array(nb_s, float)
    cl_s = np.array([panel.brand[p] for p, _ in ids_s])
    check_not_round(len(ids_s), "variants in the state estimator")
    r_state, lo_s, hi_s, _ = cluster_bootstrap_corr(ka_s / na_s, kb_s / nb_s, cl_s,
                                                    nboot=NBOOT, kind="pearson")
    rs_sp = cluster_bootstrap_corr(ka_s / na_s, kb_s / nb_s, cl_s, nboot=NBOOT)[0]
    rel_as = binomial_reliability(ka_s, na_s)[0]
    rel_bs = binomial_reliability(kb_s, nb_s)[0]
    print(f"      variants (>= {MIN_OBS_DAYS} readable days in each window) : {len(ids_s):,}"
          f"   across {len(set(cl_s))} brands")
    print(f"      Pearson  r(share_out A, share_out B)  {fmt_ci(r_state, lo_s, hi_s)}"
          "   <- brand-clustered")
    print(f"      Spearman                              {rs_sp:.3f}")
    print(f"      reliability  A {rel_as:.3f}   B {rel_bs:.3f}   "
          f"disattenuated ρ {disattenuate(r_state, rel_as, rel_bs):.3f}")
    frozen = int(((ka_s == na_s) & (kb_s == nb_s)).sum())
    print(f"      of those, never in stock in EITHER window: {frozen:,} "
          f"({100*frozen/max(len(ids_s),1):.1f}%) — the absorbing state, doing the work")

    print("\n  (b) THE HEADLINE ESTIMATOR — the FLOW, not the state.")
    print("      Depletion rate = confirmed depletions per at-risk day, per window, per")
    print("      variant. A permanently sold-out variant has no at-risk days and cannot")
    print("      contribute, so the absorbing state is structurally excluded rather than")
    print("      adjusted for. This is the number the pre-registration is about.\n")
    ka, Ta, kb, Tb, ids = flow_pairs(A[0], A[1], B[0], B[1])
    cl = np.array([panel.brand[p] for p, _ in ids])
    prod_cl = np.array([p for p, _ in ids])
    check_not_round(len(ids), "variants in the flow estimator")
    ra, rb = ka / Ta, kb / Tb
    r_flow, lo_f, hi_f, _ = cluster_bootstrap_corr(ra, rb, cl, nboot=NBOOT, kind="pearson")
    r_flow_sp, lo_fs, hi_fs, _ = cluster_bootstrap_corr(ra, rb, cl, nboot=NBOOT)
    r_flow_pc = cluster_bootstrap_corr(ra, rb, prod_cl, nboot=NBOOT, kind="pearson")[0]
    rel_a, va, vna = poisson_reliability(ka, Ta)
    rel_b, vb, vnb = poisson_reliability(kb, Tb)
    RHO = disattenuate(r_flow, rel_a, rel_b)
    print(f"      variants with >= {MIN_AT_RISK_DAYS} at-risk days in BOTH windows : {len(ids):,}"
          f"   across {len(set(cl))} brands, {len(set(prod_cl)):,} products")
    print(f"      depletions in A {int(ka.sum()):,}   in B {int(kb.sum()):,}   "
          f"at-risk days A {Ta.sum():,.0f}  B {Tb.sum():,.0f}")
    print(f"      mean depletion rate  A {ra.mean():.5f}/day   B {rb.mean():.5f}/day")
    print()
    print(f"      OBSERVED  Pearson  r(rate A, rate B)   {fmt_ci(r_flow, lo_f, hi_f)}"
          "   brand-clustered")
    print(f"                                             (product-clustered {r_flow_pc:.3f})")
    print(f"      OBSERVED  Spearman                     {fmt_ci(r_flow_sp, lo_fs, hi_fs)}")
    print(f"      reliability of one window   A {rel_a:.3f}   B {rel_b:.3f}")
    print(f"        (observed rate variance {va:.3e}; Poisson noise alone {vna:.3e})")
    print(f"      DISATTENUATED  ρ = {RHO:.3f}"
          f"   <- the market property the pre-registration is about")

    # A CI for ρ has to carry the disattenuation through the resampling, or it is
    # a CI for a different quantity. Bootstrapped end to end, brands as clusters.
    uniq_b = np.unique(cl)
    by_b = [np.flatnonzero(cl == b) for b in uniq_b]
    rngb = np.random.default_rng(3)
    draws = []
    for _ in range(NBOOT):
        pick = rngb.integers(0, len(uniq_b), len(uniq_b))
        idx = np.concatenate([by_b[i] for i in pick])
        try:
            rr = float(stats.pearsonr(ra[idx], rb[idx])[0])
            d = disattenuate(rr, poisson_reliability(ka[idx], Ta[idx])[0],
                             poisson_reliability(kb[idx], Tb[idx])[0])
            if not math.isnan(d):
                draws.append(d)
        except Exception:                                 # noqa: BLE001
            continue
    RHO_LO, RHO_HI = (float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))) \
        if len(draws) > 20 else (float("nan"), float("nan"))
    print(f"      95% CI (brand-clustered bootstrap, disattenuation carried through the")
    print(f"      resampling): [{RHO_LO:.3f}, {RHO_HI:.3f}]   from {len(draws)} draws")
    print(f"      MDE: with {len(uniq_b)} brands as independent clusters the smallest")
    print(f"      correlation detectable at 80% power is {corr_mde(len(uniq_b)):.3f}")

    band, why = band_for(RHO)
    print(f"\n      PRE-REGISTERED BAND: {band}")
    print(f"        {why}")

    print("\n  (c) BY ARCHETYPE. If ρ differs by category the tier-level verdict may be")
    print("      wrong for part of the catalog, so it is checked rather than assumed.")
    print(f"      {'category':16} {'variants':>9} {'brands':>7} {'obs r':>8} {'rel A':>7}"
          f" {'rel B':>7} {'ρ':>7}")
    cats = np.array([panel.category[p] or "?" for p, _ in ids])
    RHO_BY_CAT = {}
    for c in sorted(set(cats), key=lambda z: -int((cats == z).sum())):
        m = cats == c
        if m.sum() < 200 or len(set(cl[m])) < 5:
            continue
        rr = float(stats.pearsonr(ra[m], rb[m])[0])
        r_a = poisson_reliability(ka[m], Ta[m])[0]
        r_b = poisson_reliability(kb[m], Tb[m])[0]
        d = disattenuate(rr, r_a, r_b)
        RHO_BY_CAT[c] = d
        print(f"      {c[:16]:16} {int(m.sum()):9,} {len(set(cl[m])):7} {rr:8.3f} "
              f"{r_a:7.3f} {r_b:7.3f} {d:7.3f}")
    if RHO_BY_CAT:
        sprd = max(RHO_BY_CAT.values()) - min(RHO_BY_CAT.values())
        print(f"      spread across categories: {sprd:.3f}")

    print("\n  (d) AT THE BRAND LEVEL, which is the unit any tier claim has to use.")
    print("      Pooling a brand's variants averages away the Poisson noise that dominates")
    print("      a single SKU, so if ρ is low per-SKU but high per-brand the sellable")
    print("      product is a brand-level statement and not a per-piece one.")
    bka = collections.defaultdict(float); bTa = collections.defaultdict(float)
    bkb = collections.defaultdict(float); bTb = collections.defaultdict(float)
    for i, b in enumerate(cl):
        bka[b] += ka[i]; bTa[b] += Ta[i]; bkb[b] += kb[i]; bTb[b] += Tb[i]
    bs = [b for b in bka if bTa[b] >= 200 and bTb[b] >= 200]
    if len(bs) >= 8:
        x = np.array([bka[b] / bTa[b] for b in bs])
        y = np.array([bkb[b] / bTb[b] for b in bs])
        kx = np.array([bka[b] for b in bs]); tx = np.array([bTa[b] for b in bs])
        ky = np.array([bkb[b] for b in bs]); ty = np.array([bTb[b] for b in bs])
        rb_p, rb_lo, rb_hi, _ = cluster_bootstrap_corr(x, y, np.array(bs),
                                                       nboot=NBOOT, kind="pearson")
        rb_sp = float(stats.spearmanr(x, y)[0])
        rel_ba = poisson_reliability(kx, tx)[0]
        rel_bb = poisson_reliability(ky, ty)[0]
        RHO_BRAND = disattenuate(rb_p, rel_ba, rel_bb)
        print(f"      brands with >= 200 at-risk days in both windows : {len(bs)}")
        print(f"      Pearson  {fmt_ci(rb_p, rb_lo, rb_hi)}   Spearman {rb_sp:.3f}")
        print(f"      reliability A {rel_ba:.3f}  B {rel_bb:.3f}   "
              f"disattenuated ρ_brand = {RHO_BRAND:.3f}")
        print(f"      MDE with {len(bs)} brands: {corr_mde(len(bs)):.3f}")
    else:
        RHO_BRAND = float("nan")
        print(f"      only {len(bs)} brands clear the exposure bar — not enough to test")

    # ── 4. WHAT ρ PHYSICALLY ALLOWS ───────────────────────────────────────
    rule("4. THE CEILING — the AUC that the measured ρ makes physically possible")
    print("  A simulation, because the mapping from ρ to AUC is arithmetic and not")
    print("  something this catalog can be asked. Two calibrations, so the reader can see")
    print("  whether the conclusion depends on the assumption:\n")
    base_rate = float((kb > 0).mean())
    # Dispersion of the true rates, backed out of the observed overdispersion.
    # For a Gamma-Poisson, Var(k)/E[k] = 1 + E[k]·CV²(λ), so CV² is identifiable
    # from counts alone. σ_log = sqrt(ln(1+CV²)) is the lognormal equivalent.
    mk, vk = float(kb.mean()), float(kb.var(ddof=1))
    cv2 = max((vk / mk - 1.0) / mk, 1e-6) if mk > 0 else 1e-6
    sigma_data = math.sqrt(math.log(1 + cv2))
    print(f"    observed window-B sell-out rate per variant : {100*base_rate:.2f}%")
    print(f"    observed count overdispersion Var/mean       : {vk/max(mk,1e-9):.3f}"
          f"   -> CV²(λ) = {cv2:.3f}, σ_log = {sigma_data:.3f}")
    DEPTH = 3.0
    lam_data = calibrate_lambda(sigma_data, DEPTH, base_rate, n=NSIM)
    sigma_lit = calibrate_sigma_for_auc(0.94, DEPTH, base_rate,
                                        n=max(NSIM // 2, 40_000))
    lam_lit = (calibrate_lambda(sigma_lit, DEPTH, base_rate, n=NSIM)
               if not math.isnan(sigma_lit) else float("nan"))
    print(f"    stocked depth assumed                        : mean {DEPTH:.0f} units/variant")
    print(f"    calibration 1 (this catalog's dispersion)    : σ = {sigma_data:.3f}, "
          f"λ = {lam_data:.3f} units/window")
    print(f"    calibration 2 (reproduces the quoted 0.94)   : σ = {sigma_lit:.3f}, "
          f"λ = {lam_lit:.3f} units/window")
    rhos = np.linspace(0, 1, 11)
    cur_d = [simulate_auc(r, sigma_data, lam_data, DEPTH, n=NSIM) for r in rhos]
    cur_l = ([simulate_auc(r, sigma_lit, lam_lit, DEPTH, n=NSIM) for r in rhos]
             if not math.isnan(sigma_lit) else [(float("nan"),) * 2] * len(rhos))
    o_d = np.array([c[0] for c in cur_d]); f_d = np.array([c[1] for c in cur_d])
    o_l = np.array([c[0] for c in cur_l]); f_l = np.array([c[1] for c in cur_l])
    print(f"\n    {'ρ':>5} {'oracle(data)':>13} {'feasible':>9} | "
          f"{'oracle(lit)':>12} {'feasible':>9}")
    for i, r in enumerate(rhos):
        print(f"    {r:5.1f} {o_d[i]:13.3f} {f_d[i]:9.3f} | {o_l[i]:12.3f} {f_l[i]:9.3f}")
    CEIL_ORACLE = auc_at(RHO, rhos, o_d)
    CEIL_FEAS = auc_at(RHO, rhos, f_d)
    CEIL_ORACLE_L = auc_at(RHO, rhos, o_l)
    CEIL_FEAS_L = auc_at(RHO, rhos, f_l)
    CEIL_LO = auc_at(RHO_LO, rhos, f_d)
    CEIL_HI = auc_at(RHO_HI, rhos, f_d)
    print(f"\n    AT THE MEASURED ρ = {RHO:.3f}:")
    print(f"      oracle ceiling   {CEIL_ORACLE:.3f} (this catalog)   "
          f"{CEIL_ORACLE_L:.3f} (quoted calibration)")
    print(f"      FEASIBLE ceiling {CEIL_FEAS:.3f} (this catalog)   "
          f"{CEIL_FEAS_L:.3f} (quoted calibration)")
    print(f"      feasible ceiling across ρ's own CI: "
          f"[{CEIL_LO:.3f}, {CEIL_HI:.3f}]")
    print(f"      the archive-only model actually achieved {PRIOR_OUT_OF_TIME_AUC:.3f}")
    print(f"      headroom left on the table: {CEIL_FEAS - PRIOR_OUT_OF_TIME_AUC:+.3f} AUC")

    # ── 5. JOB 2 — STOCKOUT TIMING ────────────────────────────────────────
    rule("5. STOCKOUT TIMING — the sufficient statistic nobody had computed")
    print("  Jain, Rudi & Wang (2015, Operations Research 63(1)): under Poisson/Normal")
    print("  demand, WHEN in the period a variant stocked out is a sufficient statistic")
    print("  for the demand rate, and observing it rather than the bare event eliminates")
    print("  76.01% of the expected-profit loss. The reason is mechanical — a stockout at")
    print("  day 2 and a stockout at day 40 are the same binary and completely different")
    print("  rates, and the binary throws the difference away.\n")

    print("  (a) SPELL STRUCTURE, which has never been extracted from this archive.")
    tts, in_spell, out_spell, censored, restocked = [], [], [], 0, 0
    D = panel.dates
    for v in panel.state:
        obs = panel.observed(v)
        if len(obs) < MIN_OBS_DAYS:
            continue
        arr = panel.state[v]
        ons = panel.onsets(v, obs)
        # time to first stockout from the first day we saw it IN stock
        first_in = next((i for i in obs if arr[i] == 1), None)
        if first_in is not None:
            first_out = next((b for a, b in ons if a >= first_in), None)
            if first_out is None:
                censored += 1
            else:
                # interval-censored between the two snapshots; midpoint
                prev = max(i for i in obs if i < first_out)
                tts.append(((D[first_out] - D[first_in]).days
                            + (D[prev] - D[first_in]).days) / 2.0)
        # In-stock spell length: walk BACK from the last in-stock day to the start
        # of that contiguous run. Taking the first-ever in-stock day instead would
        # silently merge every cycle a restocked variant went through into one.
        pos = {i: t for t, i in enumerate(obs)}
        for a, _b in ons:
            t = pos[a]
            while t > 0 and arr[obs[t - 1]] == 1:
                t -= 1
            in_spell.append((D[a] - D[obs[t]]).days)
        for a, b in panel.restocks(v, obs):
            out_spell.append((D[b] - D[a]).days)
            restocked += 1
    n_tts = len(tts)
    print(f"      variants with a readable history           : "
          f"{sum(1 for v in panel.state if len(panel.observed(v)) >= MIN_OBS_DAYS):,}")
    print(f"      stocked out at least once                  : {n_tts:,}")
    print(f"      never stocked out (right-censored)         : {censored:,} "
          f"({100*censored/max(n_tts+censored,1):.1f}%)")
    if n_tts:
        q = np.percentile(tts, [10, 25, 50, 75, 90])
        print(f"      time to first stockout (calendar days)     : "
              f"p10 {q[0]:.0f}  p25 {q[1]:.0f}  median {q[2]:.0f}  p75 {q[3]:.0f}  p90 {q[4]:.0f}")
    if in_spell:
        q = np.percentile(in_spell, [25, 50, 75])
        print(f"      in-stock spell before a depletion (days)   : "
              f"p25 {q[0]:.0f}  median {q[1]:.0f}  p75 {q[2]:.0f}   (n={len(in_spell):,})")
    if out_spell:
        q = np.percentile(out_spell, [25, 50, 75])
        print(f"      confirmed restocks                         : {restocked:,}")
        print(f"      time to restock (calendar days)            : "
              f"p25 {q[0]:.0f}  median {q[1]:.0f}  p75 {q[2]:.0f}")
    print("      A median time-to-stockout well inside the window is what makes timing")
    print("      informative; if almost everything went out on day 1 or never, the timing")
    print("      statistic degenerates back into the binary and cannot beat it.")

    print("\n  (b) DOES TIMING RANK BRANDS MORE STABLY OUT OF TIME THAN THE BINARY?")
    print("      Both statistics are computed on window A and correlated against the SAME")
    print("      window-B outcome (the brand's realised depletion rate), so the comparison")
    print("      is paired and the difference is bootstrapped over brands.")
    print("        binary  = share of a brand's at-risk variants that stocked out in A")
    print("                  — the indicator, exposure and timing discarded")
    print("        timing  = depletions per at-risk DAY, the Poisson rate MLE, which is")
    print("                  exactly the sufficient statistic when stockout times are")
    print("                  what you observe\n")
    bstat = collections.defaultdict(lambda: {"nA": 0, "hitA": 0, "kA": 0.0, "TA": 0.0,
                                             "kB": 0.0, "TB": 0.0})
    for i, (p, _) in enumerate(ids):
        b = panel.brand[p]
        s = bstat[b]
        s["nA"] += 1
        s["hitA"] += 1 if ka[i] > 0 else 0
        s["kA"] += ka[i]; s["TA"] += Ta[i]
        s["kB"] += kb[i]; s["TB"] += Tb[i]
    keep = [b for b, s in bstat.items() if s["nA"] >= 20 and s["TB"] >= 200]
    keep.sort()
    if len(keep) >= 10:
        binA = np.array([bstat[b]["hitA"] / bstat[b]["nA"] for b in keep])
        timA = np.array([bstat[b]["kA"] / bstat[b]["TA"] for b in keep])
        outB = np.array([bstat[b]["kB"] / bstat[b]["TB"] for b in keep])
        r_bin = float(stats.spearmanr(binA, outB)[0])
        r_tim = float(stats.spearmanr(timA, outB)[0])
        rngd = np.random.default_rng(5)
        diffs = []
        for _ in range(NBOOT):
            pick = rngd.integers(0, len(keep), len(keep))
            try:
                d = (stats.spearmanr(timA[pick], outB[pick])[0]
                     - stats.spearmanr(binA[pick], outB[pick])[0])
                if not math.isnan(d):
                    diffs.append(d)
            except Exception:                             # noqa: BLE001
                continue
        dl, dh = (np.percentile(diffs, [2.5, 97.5]) if len(diffs) > 20
                  else (float("nan"), float("nan")))
        print(f"      brands qualifying (>=20 variants in A, >=200 at-risk days in B): "
              f"{len(keep)}")
        print(f"      Spearman(binary A , outcome B) = {r_bin:+.3f}")
        print(f"      Spearman(timing A , outcome B) = {r_tim:+.3f}")
        print(f"      difference {r_tim - r_bin:+.3f}   95% CI [{dl:+.3f}, {dh:+.3f}]"
              f"   ({len(diffs)} brand-clustered draws)")
        TIMING_WINS = (not math.isnan(dl)) and dl > 0
        print(f"      PRE-REGISTERED CRITERION (CI on the difference excludes 0): "
              f"{TIMING_WINS}")
        print(f"      MDE for a correlation with {len(keep)} brands: {corr_mde(len(keep)):.3f}")
    else:
        r_bin = r_tim = float("nan"); TIMING_WINS = False
        print(f"      only {len(keep)} brands qualify — not enough for a brand-level test")

    print("\n  (c) THE SAME QUESTION AT THE VARIANT LEVEL, out of time.")
    print("      Rank window-B depletion using only window A. AUC, so it is directly")
    print("      comparable with the 0.519 the archive-only model scored.")
    yB = (kb > 0).astype(bool)
    sc_bin = (ka > 0).astype(float)
    sc_tim = ka / Ta
    sc_cnt = ka.astype(float)
    auc_bin, auc_tim, auc_cnt = auc(sc_bin, yB), auc(sc_tim, yB), auc(sc_cnt, yB)
    print(f"      binary 'stocked out in A'        AUC {auc_bin:.3f}")
    print(f"      count  'how many times in A'     AUC {auc_cnt:.3f}")
    print(f"      timing 'depletions per day at risk' AUC {auc_tim:.3f}"
          f"   delta vs binary {auc_tim - auc_bin:+.3f}")
    print(f"      simulated feasible ceiling at ρ = {RHO:.3f} : {CEIL_FEAS:.3f}")

    print("\n  (d) POSITIVE CONTROL for this section. The same brand-level plumbing, the")
    print("      same two windows, on quantities that MUST carry across them. Both are")
    print("      recomputed independently in each window off that window's own snapshots —")
    print("      nothing is joined to itself, which would return 1.00 and prove nothing.")
    # Brand median price, computed separately on the FIRST day of A and the LAST
    # day of B: different snapshots, different product populations, same brands.
    def brand_median_price(day):
        agg = collections.defaultdict(list)
        for r in Panel._load_day(day).values():
            if r["stale"]:
                continue
            try:
                v = float(r["price"] or 0)
            except ValueError:
                continue
            if v > 0:
                agg[r["brand"]].append(v)
        return {b: float(np.median(v)) for b, v in agg.items()}
    mpA, mpB = brand_median_price(days[A[0]]), brand_median_price(days[B[1]])
    both = [b for b in keep if b in mpA and b in mpB]
    ctl_price = (float(stats.spearmanr([mpA[b] for b in both], [mpB[b] for b in both])[0])
                 if len(both) >= 10 else float("nan"))
    n_exp_a = np.array([bstat[b]["TA"] for b in keep]) if keep else np.array([])
    n_exp_b = np.array([bstat[b]["TB"] for b in keep]) if keep else np.array([])
    ctl_exposure = (float(stats.spearmanr(n_exp_a, n_exp_b)[0])
                    if len(keep) >= 10 else float("nan"))
    print(f"      brand median price   {days[A[0]]} vs {days[B[1]]} : {ctl_price:.3f}"
          f"   ({len(both)} brands)")
    print(f"      brand at-risk exposure, window A vs window B  : {ctl_exposure:.3f}")
    print("      Both high means the brand key joins, the windows line up and the")
    print("      exposure denominators are sane — so a near-zero DEMAND correlation in")
    print("      (b) is a measurement about demand and not a broken pipe.")

    # ── 6. JOB 3 — THE SIZE RUN, WITH THE POWER AND THE UNIT IT NEEDED ────
    rule("6. THE SIZE RUN — reopened with the unit, the statistic and the power")
    print("  The prior finding died on a brand-level sign test: 26 above expectation, 21")
    print("  below, p = 0.560. Three things were wrong with that, and all three are fixed")
    print("  here rather than inherited:\n")
    n_prior = 47
    pw_prior = sign_test_power(n_prior, 0.60)
    n_need = sign_test_n_for_power(0.60)
    print(f"    1. POWER. A sign test on {n_prior} brands against a true 60/40 split has")
    print(f"       {100*pw_prior:.0f}% power. It needs {n_need} brands for 80%. A test with")
    print(f"       {100*pw_prior:.0f}% power returning p = 0.560 has established NOTHING, in")
    print(f"       either direction, and quoting it as a refutation was an error.")
    print(f"    2. THE STATISTIC. A sign test discards the magnitude of every brand's")
    print(f"       deviation. Replaced below with a brand-CLUSTERED z, which keeps the")
    print(f"       brand as the unit — the part that was right — while using the size of")
    print(f"       each brand's deviation, which is where the power is.")
    print(f"    3. THE VARIANCE. analyze_size_curve.py line ~1326 multiplies the")
    print(f"       hypergeometric variance by a finite-population factor (n-k)/(n-1) that")
    print(f"       does not belong there: each PRODUCT contributes one independent draw")
    print(f"       per size, so the variance of that indicator is p(1-p) with p = k/n and")
    print(f"       nothing else. The factor is < 1, so it shrank the variance and inflated")
    print(f"       every z by ~8%. Fixed below, and the size of the fix is printed.\n")

    if not OFFERED_AVAILABLE:
        print("    DATA LIMITATION, STATED BEFORE THE RESULT. `sizesOffered` has 0 usable")
        print("    days, so the OFFERED run is reconstructed as the union of every size")
        print("    ever seen in stock. A size already sold out before 2026-06-23 is")
        print("    invisible, which biases against finding depletion in exactly the sizes")
        print("    that deplete fastest — i.e. against the hypothesis under test. Any")
        print("    positive finding here is therefore conservative; a null is not.\n")

    # Letter-scale products only, offered run reconstructed from the union.
    letter_prods = [p for p, bits in panel.offered.items()
                    if len(bits) >= 2
                    and all(panel.size_labels[j] in LETTER_IDX for j in bits)]
    print(f"    letter-scale products with >= 2 offered sizes : {len(letter_prods):,}"
          f"   ({len({panel.brand[p] for p in letter_prods})} brands)")
    check_not_round(len(letter_prods), "letter-scale products")

    # ---- 6a. the corrected within-product test, brand-clustered ------------
    print("\n  (a) WITHIN-PRODUCT DEPLETION, VARIANCE FIXED, BRAND AS THE UNIT.")
    print("      Given a piece lost k of its n offered sizes over a window, every size it")
    print("      offered had an equal k/n chance of being one of them. Each product is its")
    print("      own stratum, so brand, price, season and style are controlled by")
    print("      construction. Windows are DISJOINT thirds — the prior run reported three")
    print("      OVERLAPPING windows and called them replication.\n")
    thirds = [(0, len(days) // 3), (len(days) // 3, 2 * len(days) // 3),
              (2 * len(days) // 3, len(days) - 1)]
    OE_TOT = collections.defaultdict(lambda: [0.0, 0.0, 0.0])         # o, e, var
    per_window = []
    for wi, (i0, i1) in enumerate(thirds):
        oe = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])    # o,e,var_fix,var_bug
        oe_brand = collections.defaultdict(lambda: collections.defaultdict(float))
        pos_oe = collections.defaultdict(lambda: [0.0, 0.0])
        nmixed = 0
        for p in letter_prods:
            bits = panel.offered[p]
            st0, st1 = [], []
            for j in bits:
                arr = panel.state[(p, j)]
                a0 = arr[i0] if arr[i0] != UNOBS else None
                a1 = arr[i1] if arr[i1] != UNOBS else None
                st0.append(a0); st1.append(a1)
            if any(x is None for x in st0) or any(x is None for x in st1):
                continue
            inn = [k for k, x in enumerate(st0) if x == 1]
            if len(inn) < 2:
                continue
            lost = [k for k in inn if st1[k] == 0]
            if not lost or len(lost) == len(inn):
                continue                     # all-or-nothing carries no within-product info
            nmixed += 1
            kk, nn = len(lost), len(inn)
            pr = kk / nn
            order = sorted(inn, key=lambda k: LETTER_IDX[panel.size_labels[bits[k]]])
            for rank, k in enumerate(order):
                s = panel.size_labels[bits[k]]
                hit = 1.0 if k in lost else 0.0
                oe[s][0] += hit
                oe[s][1] += pr
                oe[s][2] += pr * (1 - pr)                                   # CORRECT
                oe[s][3] += pr * (1 - pr) * (nn - kk) / max(nn - 1, 1)      # the bug
                oe_brand[panel.brand[p]][s] += hit - pr
                u = rank / max(nn - 1, 1)
                bucket = "end" if u <= 0.2 or u >= 0.8 else "middle"
                pos_oe[bucket][0] += hit
                pos_oe[bucket][1] += pr
        per_window.append((days[i0], days[i1], nmixed, oe, oe_brand, pos_oe))
        for s, (o, e, vf, vb) in oe.items():
            t = OE_TOT[s]
            t[0] += o; t[1] += e; t[2] += vf

    print(f"      {'window':26} {'pieces':>7} " +
          " ".join(f"{s:>7}" for s in LETTER_SCALE[:7]))
    for d0, d1, nmixed, oe, _, _ in per_window:
        cells = []
        for s in LETTER_SCALE[:7]:
            o, e, vf, vb = oe[s]
            cells.append(f"{(o-e)/math.sqrt(vf):+7.2f}" if e >= 5 and vf > 0 else "      .")
        print(f"      {d0}->{d1} {nmixed:7,} " + " ".join(cells))
    print(f"      {'':26} {'':>7} " +
          " ".join(f"{'':>7}" for _ in LETTER_SCALE[:7]) + "   <- z, + = goes FIRST")
    # size of the variance bug, computed rather than quoted
    infl = []
    for _, _, _, oe, _, _ in per_window:
        for s, (o, e, vf, vb) in oe.items():
            if vf > 0 and vb > 0 and e >= 5:
                infl.append(math.sqrt(vf / vb))
    if infl:
        print(f"      the removed finite-population factor was inflating every z by "
              f"{100*(np.mean(infl)-1):.1f}% on average (max {100*(max(infl)-1):.1f}%)")

    # brand-clustered z on the pooled disjoint windows
    print("\n      BRAND-CLUSTERED z (the honest tier-level test). Under the null each")
    print("      brand's summed O-E has mean zero and the brands are independent, so the")
    print("      variance of the total is estimated by the sum of the brands' squares. No")
    print("      distributional assumption and no product-level independence assumption.")
    brand_dev = collections.defaultdict(lambda: collections.defaultdict(float))
    for _, _, _, _, oeb, _ in per_window:
        for b, d in oeb.items():
            for s, v in d.items():
                brand_dev[b][s] += v
    print(f"      {'size':>6} {'brands':>7} {'sum O-E':>9} {'naive z':>9} {'cluster z':>10}"
          f" {'p':>8} {'sign +/-':>10} {'sign p':>8}")
    SIZE_RESULT = {}
    for s in LETTER_SCALE[:7]:
        devs = np.array([brand_dev[b][s] for b in brand_dev if s in brand_dev[b]])
        if len(devs) < 5:
            continue
        o, e, vf = OE_TOT[s]
        naive_z = (o - e) / math.sqrt(vf) if vf > 0 else float("nan")
        tot = devs.sum()
        se = math.sqrt((devs ** 2).sum())
        z = tot / se if se > 0 else float("nan")
        p = 2 * stats.norm.sf(abs(z))
        pos = int((devs > 0).sum()); neg = int((devs < 0).sum())
        sp = stats.binomtest(pos, pos + neg, 0.5).pvalue if pos + neg else float("nan")
        SIZE_RESULT[s] = (z, p, len(devs), tot, se, naive_z, pos, neg, sp)
        print(f"      {s:>6} {len(devs):7} {tot:9.1f} {naive_z:9.2f} {z:10.2f} "
              f"{p:8.3f} {pos:5}/{neg:<4} {sp:8.3f}")
    if SIZE_RESULT:
        sd_dev = float(np.std([brand_dev[b][s] for s in SIZE_RESULT
                               for b in brand_dev if s in brand_dev[b]], ddof=1))
        nb_avg = int(np.mean([SIZE_RESULT[s][2] for s in SIZE_RESULT]))
        print(f"\n      POWER. Brand-level deviations have sd {sd_dev:.2f}; with {nb_avg}")
        print(f"      brands the MDE on the summed deviation is {mdes(sd_dev, nb_avg)*nb_avg:.1f}")
        print(f"      (i.e. |z| >= 2.80). The sign test on the same {nb_avg} brands has")
        print(f"      {100*sign_test_power(nb_avg, 0.60):.0f}% power against a true 60/40")
        print(f"      split; the clustered z is the same data without that handicap.")

    # ---- 6b. the edge-effect alternative -----------------------------------
    print("\n  (b) THE MECHANICAL ALTERNATIVE. Recode each size by its POSITION in that")
    print("      piece's own run rather than by its letter. A bell-shaped buy depletes")
    print("      both ends faster than the middle with no demand content whatsoever, and")
    print("      that pattern would masquerade as 'XL goes first' in a letter-indexed")
    print("      table because XL is usually an end.")
    tot_end = [0.0, 0.0]; tot_mid = [0.0, 0.0]
    for _, _, _, _, _, pos in per_window:
        tot_end[0] += pos["end"][0]; tot_end[1] += pos["end"][1]
        tot_mid[0] += pos["middle"][0]; tot_mid[1] += pos["middle"][1]
    for lbl, t in (("run ENDS (first/last 20%)", tot_end), ("run MIDDLE", tot_mid)):
        if t[1] > 0:
            print(f"      {lbl:28} observed {t[0]:8.0f}  expected {t[1]:8.1f}  "
                  f"O/E {t[0]/t[1]:.3f}")
    if tot_end[1] > 0 and tot_mid[1] > 0:
        print(f"      ends-vs-middle O/E ratio {(tot_end[0]/tot_end[1])/(tot_mid[0]/tot_mid[1]):.3f}"
              "   (1.00 = no edge effect)")

    # ---- 6c. the ordinal test (Kurz et al.) --------------------------------
    print("\n  (c) THE ORDINAL TEST — the version the literature says can survive.")
    print("      Kurz et al. (2015, Annals of OR 229(1)): the branch-level size-demand")
    print("      question is hopeless, but the ORDINAL one is not — pool across products")
    print("      WITHIN a brand, rank sizes by depletion hazard, and ask whether that")
    print("      ranking is stable out of time. Their field study got ~1pp of gross yield")
    print("      from the ordinal measure alone, so a stable ranking is worth something")
    print("      even though a rate forecast is not.\n")
    def size_hazards(lo, hi):
        h = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0]))
        for p in letter_prods:
            b = panel.brand[p]
            for j in panel.offered[p]:
                s = panel.size_labels[j]
                w = window_stats(panel, (p, j), lo, hi)
                if not w:
                    continue
                h[b][s][0] += w["onsets"]
                h[b][s][1] += w["at_risk"]
        return h
    hA, hB = size_hazards(*A), size_hazards(*B)
    taus, ns_used = [], []
    MIN_SIZE_EXPOSURE = 30.0
    for b in sorted(set(hA) & set(hB)):
        common = [s for s in hA[b]
                  if s in hB[b] and hA[b][s][1] >= MIN_SIZE_EXPOSURE
                  and hB[b][s][1] >= MIN_SIZE_EXPOSURE]
        if len(common) < 3:
            continue
        x = [hA[b][s][0] / hA[b][s][1] for s in common]
        y = [hB[b][s][0] / hB[b][s][1] for s in common]
        if len(set(x)) < 2 or len(set(y)) < 2:
            continue
        t = stats.kendalltau(x, y)[0]
        if not math.isnan(t):
            taus.append(t); ns_used.append(len(common))
    taus = np.array(taus)
    if len(taus) >= 8:
        mt = float(taus.mean()); sdt = float(taus.std(ddof=1))
        ci = stats.t.interval(0.95, len(taus) - 1, loc=mt, scale=sdt / math.sqrt(len(taus)))
        pval = float(stats.ttest_1samp(taus, 0.0).pvalue)
        wp = float(stats.wilcoxon(taus).pvalue) if np.any(taus != 0) else float("nan")
        print(f"      brands with >= 3 sizes clearing {MIN_SIZE_EXPOSURE:.0f} at-risk days in")
        print(f"      BOTH windows                                : {len(taus)}"
              f"   (median {int(np.median(ns_used))} sizes each)")
        print(f"      mean within-brand Kendall tau (A rank vs B rank) : {mt:+.3f}")
        print(f"      95% CI [{ci[0]:+.3f}, {ci[1]:+.3f}]   t-test p = {pval:.3f}"
              f"   Wilcoxon p = {wp:.3f}")
        print(f"      MDE at 80% power with {len(taus)} brands            : "
              f"{mdes(sdt, len(taus)):+.3f} tau")
        ORDINAL_SURVIVES = ci[0] > 0
        print(f"      PRE-REGISTERED CRITERION (CI excludes 0)         : {ORDINAL_SURVIVES}")
    else:
        mt = float("nan"); ORDINAL_SURVIVES = False
        print(f"      only {len(taus)} brands clear the exposure bar — the ordinal test")
        print(f"      cannot be run at this scale, which is itself the answer")

    print("\n  (d) POSITIVE CONTROL for the ordinal machinery: rank each brand's sizes by")
    print("      how OFTEN they are offered instead of by hazard, through the identical")
    print("      code path. A brand offers S on more pieces than 3XL in every window, so")
    print("      this must come back near +1. If it does, a null on hazard is a fact about")
    print("      demand; if it does not, the ranking code is broken.")
    def size_offer_counts(lo, hi):
        c = collections.defaultdict(lambda: collections.defaultdict(float))
        for p in letter_prods:
            b = panel.brand[p]
            for j in panel.offered[p]:
                arr = panel.state[(p, j)]
                n = sum(1 for i in range(lo, hi + 1) if arr[i] != UNOBS)
                if n:
                    c[b][panel.size_labels[j]] += n
        return c
    cA, cB = size_offer_counts(*A), size_offer_counts(*B)
    ctaus = []
    for b in sorted(set(cA) & set(cB)):
        common = [s for s in cA[b] if s in cB[b]]
        if len(common) < 3:
            continue
        t = stats.kendalltau([cA[b][s] for s in common], [cB[b][s] for s in common])[0]
        if not math.isnan(t):
            ctaus.append(t)
    CTL_ORD = float(np.mean(ctaus)) if ctaus else float("nan")
    print(f"      mean within-brand tau on the OFFER ranking : {CTL_ORD:+.3f}   "
          f"({len(ctaus)} brands)")
    print(f"      control fires: {CTL_ORD > 0.7}")

    # ── 7. JOB 4 — THE ARITHMETIC FLOOR ───────────────────────────────────
    rule("7. THE FLOOR — what accuracy is physically available at each aggregation")
    print("  Demand is a counting process. Even a forecaster who knows the true rate")
    print("  exactly cannot beat the Poisson noise around it, and that floor depends on")
    print("  ONE thing: how many units land in a cell in a period. No model, no vendor and")
    print("  no amount of data moves it.\n")
    a25 = wape_floor(10.0)
    print(f"    ANCHOR, computed not quoted: at 10 units per SKU-week the best possible")
    print(f"    WAPE is {100*a25[1]:.1f}% against a mean forecast ({100*a25[0]:.1f}% against")
    print(f"    the median). Zara achieves ~25% with full internal data, i.e. Zara is AT")
    print(f"    the floor. Nobody beats it; the only question is which bucket you are in.\n")
    print(f"    {'units per cell per period':>26}   {'WAPE floor (mean fc)':>21}  "
          f"{'(median fc)':>12}")
    for lam in (0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 1000):
        m, mn = wape_floor(lam)
        print(f"    {lam:>26.2f}   {100*mn:>20.1f}%  {100*m:>11.1f}%")

    print("\n    NOW THIS CATALOG. The observable counting process is a CONFIRMED")
    print("    DEPLETION: a size that was in stock and stayed gone. That is a LOWER BOUND")
    print("    on units — one depletion means at least one unit sold, and a piece stocked")
    print("    forty deep produces exactly one depletion for forty units. So the true unit")
    print("    volumes are higher than this table and the true floors are therefore BETTER")
    print("    than this table. The ordering between levels is unaffected, and the")
    print("    ordering is the decision.\n")
    # counts per cell per period at each aggregation level
    ev = []          # (day_index, product, brand, category, size_idx)
    for v in panel.state:
        p, j = v
        for a, _b in panel.onsets(v):
            ev.append((a, p, panel.brand[p], panel.category[p] or "?", j))
    n_ev = len(ev)
    check_not_round(n_ev, "confirmed depletion events")
    span_days = (panel.dates[-1] - panel.dates[0]).days + 1
    n_weeks = max(span_days / 7.0, 1.0)
    week_of = {i: (panel.dates[i] - panel.dates[0]).days // 7 for i in range(panel.ndays)}
    n_week_bins = len(set(week_of.values()))
    print(f"    confirmed depletion events in the panel : {n_ev:,}"
          f"   over {span_days} days ({n_weeks:.1f} weeks)")

    def level_table(keyfn, per_week, label, universe):
        cells = collections.Counter()
        for e in ev:
            cells[keyfn(e)] += 1
        n_cells = len(universe)
        periods = n_week_bins if per_week else panel.ndays
        counts = np.array(list(cells.values()) + [0] * max(n_cells - len(cells), 0), float)
        mean_per_cell_period = counts.sum() / max(n_cells * periods, 1)
        # the DISTRIBUTION that matters is over active cells, so report both
        active = counts[counts > 0] / periods
        med = float(np.median(active)) if len(active) else 0.0
        _, fl_mean = wape_floor(max(mean_per_cell_period, 1e-9))
        _, fl_med = wape_floor(max(med, 1e-9))
        print(f"    {label:22} {n_cells:>8,} {periods:>8} {mean_per_cell_period:>12.3f} "
              f"{med:>10.3f} {100*fl_mean:>11.0f}% {100*fl_med:>11.0f}%")
        return mean_per_cell_period, fl_mean

    all_variants = set(panel.state)
    all_products = set(panel.offered)
    all_brands = {panel.brand[p] for p in all_products}
    all_cats = {panel.category[p] or "?" for p in all_products}
    print(f"    {'level':22} {'cells':>8} {'periods':>8} {'mean/cell/pd':>12} "
          f"{'median':>10} {'floor(mean)':>12} {'floor(med)':>12}")
    LEVELS = {}
    LEVELS["variant-day"] = level_table(lambda e: (e[1], e[4], e[0]), False,
                                        "variant-day", all_variants)
    LEVELS["variant-week"] = level_table(lambda e: (e[1], e[4], week_of[e[0]]), True,
                                         "variant-week", all_variants)
    LEVELS["product-week"] = level_table(lambda e: (e[1], week_of[e[0]]), True,
                                         "product-week", all_products)
    LEVELS["brand-week"] = level_table(lambda e: (e[2], week_of[e[0]]), True,
                                       "brand-week", all_brands)
    LEVELS["category-week"] = level_table(lambda e: (e[3], week_of[e[0]]), True,
                                          "category-week", all_cats)
    LEVELS["tier-week"] = level_table(lambda e: week_of[e[0]], True,
                                      "tier-week", {"tier"})
    l25 = lam_for_wape(0.25)
    l50 = lam_for_wape(0.50)
    print(f"\n    To reach a 25% WAPE floor a cell needs {l25:.1f} units per period;")
    print(f"    for 50% it needs {l50:.1f}. Read those two numbers against the column")
    print(f"    above and the answer to 'what can I sell' falls out without argument.")

    # ── 8. VERDICT ────────────────────────────────────────────────────────
    rule("8. VERDICT")
    print(f"  ρ  = {RHO:.3f}   95% CI [{RHO_LO:.3f}, {RHO_HI:.3f}]"
          f"   ({len(ids):,} variants, {len(uniq_b)} brands, brand-clustered)")
    print(f"       raw between-window correlation {r_flow:.3f}; reliability of one window")
    print(f"       A {rel_a:.3f} / B {rel_b:.3f}, so the raw figure is what a predictor")
    print(f"       could actually achieve and {RHO:.3f} is the market property.")
    print(f"  PRE-REGISTERED BAND: {band}")
    print(f"       {why}")
    print(f"  brand-level ρ {RHO_BRAND:.3f}" if not math.isnan(RHO_BRAND) else
          "  brand-level ρ not estimable")
    print(f"  CEILING at that ρ: oracle {CEIL_ORACLE:.3f}, feasible {CEIL_FEAS:.3f}")
    print(f"       the archive-only model scored {PRIOR_OUT_OF_TIME_AUC:.3f} out of time, so it was")
    print(f"       {'AT THE CEILING — the failure was the market, not the model'
                    if CEIL_FEAS - PRIOR_OUT_OF_TIME_AUC < 0.05 else
                    'BELOW the ceiling — there was headroom the model did not take'}.")
    print(f"  TIMING: brand-level Spearman against the same out-of-time outcome —")
    print(f"       binary {r_bin:+.3f} vs timing {r_tim:+.3f}; variant-level AUC "
          f"{auc_bin:.3f} -> {auc_tim:.3f}.")
    print(f"  SIZE: the ordinal test returns mean tau {mt:+.3f}"
          f" with the offer-ranking control at {CTL_ORD:+.3f}.")
    print(f"  FLOOR: {n_ev:,} observable depletions over {n_weeks:.0f} weeks; a cell needs")
    print(f"       {l25:.0f} units/period for a 25% WAPE floor.")
    print("\n  Everything above was computed in this run. Nothing was retyped.")
    rule("DONE")


if __name__ == "__main__":
    main()
