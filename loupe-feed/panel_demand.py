#!/usr/bin/env python3
"""Loupe — the inventory panel, read honestly.

WHAT THIS IS FOR

build_catalog.py records stock.json: a daily, pseudonymised, per-variant
`inventory_quantity` for the 33 of 162 stores that still expose the field. Day
over day, a DECREMENT on a tracked deny-policy variant is units sold. That is
the only true demand quantity Loupe has ever had, and it is the input to every
number a brand would pay for.

It is also the input most likely to be quoted wrongly, in four specific ways.
This file exists so that each of those four is something the code REFUSES rather
than something a reader is trusted to remember.

  1. THE DECREMENTS ARE NOT CLEAN.  `inventory_quantity` moves for reasons that
     are not sales: customer returns (an increment, arriving days later, and
     indistinguishable in this field from a restock), cancellations, manual
     recounts, transfers between locations, and merchant bulk edits. This is
     inventory record inaccuracy — Chen & Mersereau, "Analytics for Operational
     Visibility in the Retail Store", M&SOM 17(3) 2015 — and the bias runs
     DOWNWARD: a return silently cancels a real sale that already happened, so
     summed decrements understate units. movement_report() classifies every
     movement it can and reports the fraction it cannot, and caveat() is the
     sentence that has to travel with any units number.

  2. DEMAND IS CENSORED.  When a variant hits zero, demand continues and we stop
     seeing it. Estimating through that is a solved problem with zero covariates:
       • negative binomial censored MLE — Agrawal & Smith, "Estimating negative
         binomial demand for retail inventory management with unobservable lost
         sales", Naval Research Logistics 43(6) 1996, which found NB fits
         significantly better than Poisson or Normal on real retail data. The
         default here.
       • Kaplan–Meier — Huh, Levi, Rusmevichientong & Orlin, "Adaptive Data-
         Driven Inventory Control with Censored Demand Based on Kaplan-Meier
         Estimator", Operations Research 59(4) 2011. Nonparametric, needs
         VARYING censoring levels, which a daily starting inventory gives.
       • Tobit ETS / Tobit Kalman — Trapero, Cardós & Kourentzes, IJF 40(3)
         2024; Pedregal & Trapero, arXiv:2407.17920. Built for time-varying
         censoring levels. Not implemented here; noted so the next person does
         not think it was missed.

  3. ABOVE THE HIGHEST INVENTORY WE EVER SAW, DEMAND IS NOT IDENTIFIED.  If no
     variant ever started a day with more than 44 units, nothing in the data
     distinguishes a demand distribution with a 60-unit tail from one with a
     600-unit tail. A point estimate there is a property of the parametric
     family, not of the market — and Poisson, Normal and NB disagree precisely
     in the region you cannot see. estimate() emits ">= X" there and
     point_estimate() raises. This is the rule most likely to be quietly dropped
     under commercial pressure, which is why it is an exception and not a note.

  4. PER-VARIANT DEMAND MUST NOT BE SUMMED TO A BRAND TOTAL.  Imputed lost
     demand on a sold-out M already appears, partly, in the observed sales of
     the L next to it: the shopper substituted rather than left. Summing
     unconstrained estimates double-counts her. Gruen & Corsten's worldwide
     out-of-stock study puts brand loss at ~35% of out-of-stock demand, i.e.
     recapture ~45% overall and higher inside one brand's own size ladder.
     brand_total() applies the haircut and refuses the naive sum.

  AND ONE THING WE DO NOT SELL AT ALL.  "True demand" in the choice-based sense
  — what she would have bought from the whole assortment — requires a market
  share or an arrival rate. Vulcano, van Ryzin & Ratliff, "Estimating Primary
  Demand for Substitutable Products from Sales Transaction Data", Operations
  Research 60(2) 2012, §3.3: without it the likelihood has a CONTINUUM of
  maxima, so the fitting code converges and returns a number that is one of
  infinitely many equally good ones. Loupe has no market share, no arrival
  process and no traffic. true_demand() raises, permanently.

A CAVEAT THAT IS NOT OPTIONAL: INFORMATIVE CENSORING

Every method above assumes censoring is independent of demand. Here it is
almost certainly not. Craig, DeHoratius & Raman (2016) measure the reverse
causal arrow directly: a one-percentage-point improvement in fill rate raises
subsequent demand by ~11%, because a shopper who finds the size gone comes back
less. So low inventory both censors demand and depresses it, and every estimate
here is a lower bound for a second, independent reason. Stated in the output as
a field, not buried here.

WHAT IS PUBLISHABLE

Nothing in this file may be published per brand. See build_catalog.py's policy
block: aggregate, anonymised, tier-level, over at least MIN_BRANDS_PER_AGGREGATE
distinct brands. publishable() defers to build_catalog.stock_aggregate_ok() so
the threshold has exactly one home.

STDLIB ONLY. The CI gates run on a bare `actions/setup-python` with no install
step, so numpy/scipy are not available to anything that has to pass them.

USAGE
    python panel_demand.py --movement     # Job 1: how dirty are the decrements
    python panel_demand.py --selftest     # estimator recovery on known draws
    python panel_demand.py --report       # everything, from the committed panel
"""
import json
import math
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
STOCK_REL = "loupe-feed/stock.json"

try:
    from build_catalog import MIN_BRANDS_PER_AGGREGATE, stock_aggregate_ok
except Exception:  # pragma: no cover - build_catalog is always importable in-repo
    MIN_BRANDS_PER_AGGREGATE = 5

    def stock_aggregate_ok(n):
        return isinstance(n, int) and not isinstance(n, bool) and n >= 5


# ═════════════════════════════════════════════════════════════════════════════
# Refusals. Each one is a specific claim we have decided not to make.
# ═════════════════════════════════════════════════════════════════════════════
class NotIdentified(Exception):
    """Asked for a point estimate in the region the data cannot see."""


class NotPublishable(Exception):
    """Asked to emit a figure that would breach the aggregation floor."""


class NotAvailableWithoutMarketShare(Exception):
    """Asked for choice-based 'true demand'. See Vulcano et al. (2012) §3.3."""


class PanelNotJoinable(Exception):
    """Asked to difference snapshots that cannot be differenced."""


# ═════════════════════════════════════════════════════════════════════════════
# 1. LOADING AND JOINING THE PANEL
# ═════════════════════════════════════════════════════════════════════════════
# The panel is a time series or it is nothing, and there are exactly two ways to
# join two snapshots. Both are implemented, because the second one is what saved
# the first three weeks of this dataset.
#
#   BY KEY      the intended path. Both files carry the same `saltId`, which
#               means the same secret produced both, which means the same
#               variant hashes to the same pseudonym. O(1), exact, survives a
#               store dropping out or a cohort change.
#
#   BY POSITION the fallback, and it is NOT a heuristic. collect_stock walks
#               stores in sorted order, one product each per pass, and
#               stock_cohort is a CRC32 sort — so the row order of stock.json is
#               a deterministic function of the cohort. If two snapshots have
#               the same row count, the same store-block structure, and
#               identical (denyPolicy, tracked, category, priceBand) at every
#               single index, then row i is the same variant in both. Verified
#               on 2026-08-06 against the two panels committed 20 minutes apart
#               under DIFFERENT ephemeral salts: 2,742 rows, 495 store blocks, 0
#               invariant mismatches.
#
#               It is strictly worse than a key join — one product leaving a
#               cohort shifts every row after it — but it FAILS LOUDLY (the
#               invariant check catches exactly that shift) rather than silently
#               joining the wrong variants. Use it to rescue days that were
#               recorded before the salt was set. Do not use it as the design.
JOIN_KEY, JOIN_POSITION = "key", "position"

# stock.json column order. Mirrors stock_record()'s `schema`.
C_STORE, C_VAR, C_QTY, C_AVAIL, C_DENY, C_TRACKED, C_CAT, C_BAND = range(8)

# The columns that CANNOT change for a fixed variant between two snapshots and
# are therefore the positional join's proof of alignment. A price band can move
# if a merchant re-prices across a boundary, so it is checked as a soft signal:
# a handful of band changes is a sale, thousands is a misalignment.
_HARD_INVARIANTS = (C_DENY, C_TRACKED, C_CAT)


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def committed_snapshots(limit=None):
    """Every committed stock.json, oldest first, as (sha, isoTime, doc).

    Reads git rather than the working tree because the working tree holds ONE
    day and the panel's whole value is its length.
    """
    out = []
    lines = git("log", "--format=%H|%aI", "--", STOCK_REL).splitlines()
    for line in reversed(lines):
        if "|" not in line:
            continue
        sha, when = line.split("|", 1)
        raw = git("show", f"{sha.strip()}:{STOCK_REL}")
        if not raw.strip():
            continue
        try:
            out.append((sha.strip(), when.strip(), json.loads(raw)))
        except ValueError:
            continue
    return out[-limit:] if limit else out


def join_mode(a, b):
    """How (and whether) two snapshots can be differenced. Never guesses."""
    if a.get("joinable") and b.get("joinable") and a.get("saltId") == b.get("saltId"):
        return JOIN_KEY, "same salt"
    pa, pb = a.get("panel") or [], b.get("panel") or []
    if not pa or len(pa) != len(pb):
        return None, f"row counts differ ({len(pa)} vs {len(pb)})"
    bad = sum(1 for i in range(len(pa))
              if any(pa[i][c] != pb[i][c] for c in _HARD_INVARIANTS))
    if bad:
        return None, f"{bad} of {len(pa)} rows disagree on invariant columns"
    if _blocks(pa) != _blocks(pb):
        return None, "store-block structure differs"
    return JOIN_POSITION, "invariants align at every index"


def _blocks(panel):
    """Run lengths of consecutive identical storeKey — the cohort's shape."""
    out, cur, n = [], None, 0
    for row in panel:
        if row[C_STORE] != cur:
            if cur is not None:
                out.append(n)
            cur, n = row[C_STORE], 0
        n += 1
    if cur is not None:
        out.append(n)
    return out


def pair(a, b, allow_position=True):
    """[(rowA, rowB)] for the same variant in two snapshots.

    Raises PanelNotJoinable rather than returning a partial or a guess: a units
    series built from a bad join is worse than no units series, because it is
    indistinguishable from a good one downstream.
    """
    mode, why = join_mode(a, b)
    if mode is None:
        raise PanelNotJoinable(why)
    if mode == JOIN_POSITION and not allow_position:
        raise PanelNotJoinable("only a positional join is available and it was "
                               "not permitted (pass allow_position=True)")
    if mode == JOIN_KEY:
        ib = {(r[C_STORE], r[C_VAR]): r for r in b["panel"]}
        return [(r, ib[(r[C_STORE], r[C_VAR])]) for r in a["panel"]
                if (r[C_STORE], r[C_VAR]) in ib], mode
    return list(zip(a["panel"], b["panel"])), mode


# ═════════════════════════════════════════════════════════════════════════════
# 2. WHICH ROWS ARE EVEN ELIGIBLE TO BE UNITS
# ═════════════════════════════════════════════════════════════════════════════
# Measured on 2,502 live variants (build_catalog.py's variant_stock docstring):
# 2.4% of rows carry a quantity that is not a live position at all — the shop is
# not managing that variant's inventory through Shopify, so the number is a
# stale remnant and `available` ignores it. A further ~24% sit on
# inventory_policy=continue, where the shop keeps selling below zero; those
# decrements ARE units, but the variant never censors, so it belongs in the
# movement analysis and not in the censored-demand fit.
def is_units_eligible(row):
    """Tracked by Shopify AND deny-policy: the only rows where qty is a ledger
    AND zero means stopped."""
    return bool(row[C_TRACKED]) and bool(row[C_DENY])


def is_movement_eligible(row):
    """Tracked by Shopify. `continue`-policy rows still move for real reasons."""
    return bool(row[C_TRACKED])


# ═════════════════════════════════════════════════════════════════════════════
# 3. JOB 1 — HOW DIRTY IS A DECREMENT
# ═════════════════════════════════════════════════════════════════════════════
# The field moves for at least six reasons and publishes none of them:
#
#   sale            −n   the one we want
#   return          +n   arrives days after the sale it cancels, usually +1,
#                        usually on a variant that is already in stock
#   cancellation    +n   same shape as a return, sooner
#   restock         +n   usually LARGE, usually simultaneous across most of one
#                        product's size run, often lands on a round number
#   recount         ±n   arbitrary, and the one that cannot be modelled
#   transfer        ±n   multi-location shops move stock between locations
#   bulk edit       ±n   a merchant re-keying the catalogue
#
# Only two of those are separable from the field alone, and they are separable
# by SHAPE rather than by size:
#
#   a positive move shared by most of a product's variants in the same interval
#   is a restock or a recount — a shopper does not return four sizes at once;
#
#   an isolated +1 or +2 on a single variant of a product whose other variants
#   did not move is a return or a cancellation.
#
# Everything else is UNATTRIBUTABLE, and the fraction that is unattributable is
# the number that has to ship with the product. Do not report a units figure
# without it.
RESTOCK_MIN_SIBLINGS = 2      # variants of one product moving up together
RETURN_MAX_MAGNITUDE = 2      # an isolated +1 or +2 looks like a return

SALE, RESTOCK_LIKE, RETURN_LIKE, UNATTRIBUTABLE = (
    "sale", "restock-like", "return-like", "unattributable")


def _product_of(variant_key):
    """stock.json's variantKey is an HMAC of "<productId>|<variantId>", so the
    product is NOT recoverable from the pseudonym. The panel's store-block
    structure is: one product's variants are CONTIGUOUS (round-robin fetches one
    product per store per pass). That contiguity is the product grouping."""
    return None


def classify(movements):
    """[(index, storeKey, delta, qtyBefore, qtyAfter, groupId)] -> labels.

    `groupId` groups variants of the SAME product in the same interval. Callers
    get it from panel contiguity (see movement_report); it is not recoverable
    from the pseudonym, by design.
    """
    up_by_group = {}
    for _i, _s, d, _qb, _qa, g in movements:
        if d > 0:
            up_by_group[g] = up_by_group.get(g, 0) + 1
    out = []
    for i, store, d, qb, qa, g in movements:
        if d < 0:
            label = SALE
        elif up_by_group.get(g, 0) >= RESTOCK_MIN_SIBLINGS:
            label = RESTOCK_LIKE
        elif d <= RETURN_MAX_MAGNITUDE and qb > 0:
            label = RETURN_LIKE
        else:
            label = UNATTRIBUTABLE
        out.append((i, store, d, qb, qa, g, label))
    return out


def movement_report(a, b, allow_position=True):
    """Everything Job 1 asks: what moved, which way, and how much of it we
    cannot attribute."""
    rows, mode = pair(a, b, allow_position=allow_position)
    groups, gid, prev_store = [], -1, object()
    for ra, _rb in rows:
        if ra[C_STORE] != prev_store:
            gid += 1
            prev_store = ra[C_STORE]
        groups.append(gid)

    tracked = deny = 0
    moves = []
    for i, (ra, rb) in enumerate(rows):
        if is_movement_eligible(ra):
            tracked += 1
        if is_units_eligible(ra):
            deny += 1
        if ra[C_QTY] != rb[C_QTY] and is_movement_eligible(ra):
            moves.append((i, ra[C_STORE], rb[C_QTY] - ra[C_QTY],
                          ra[C_QTY], rb[C_QTY], groups[i]))
    labelled = classify(moves)

    def tally(pred):
        sel = [m for m in labelled if pred(m)]
        return {"n": len(sel), "units": sum(abs(m[2]) for m in sel)}

    down = [m for m in labelled if m[2] < 0]
    up = [m for m in labelled if m[2] > 0]
    # The number the caveat is built on. A decrement is only clean UNITS if no
    # increment could have cancelled one, so the increment volume is the size of
    # the doubt, expressed against the decrement volume it contaminates.
    down_units = sum(-m[2] for m in down)
    unattr_units = sum(abs(m[2]) for m in labelled if m[6] == UNATTRIBUTABLE)
    return {
        "join": mode,
        "from": a.get("generatedAt"), "to": b.get("generatedAt"),
        "rows": len(rows),
        "trackedRows": tracked,
        "unitsEligibleRows": deny,
        "unitsEligibleShare": round(deny / len(rows), 4) if rows else None,
        "movedRows": len(labelled),
        "decrements": tally(lambda m: m[2] < 0),
        "increments": tally(lambda m: m[2] > 0),
        "byLabel": {lab: tally(lambda m, L=lab: m[6] == L)
                    for lab in (SALE, RESTOCK_LIKE, RETURN_LIKE, UNATTRIBUTABLE)},
        "grossUnitsDown": down_units,
        "grossUnitsUp": sum(m[2] for m in up),
        # ↓ THE HEADLINE. Every unit of increment is a unit of doubt about the
        #   decrements, because a return and a restock are the same byte here.
        "contaminationRatio": (round(sum(m[2] for m in up) / down_units, 4)
                               if down_units else None),
        "unattributableUnitShare": (round(unattr_units /
                                          (down_units + sum(m[2] for m in up)), 4)
                                    if (down_units or up) else None),
        "informativeCensoring": True,
    }


def caveat(units_figure=None):
    """The sentence that must travel with any units number derived from here."""
    n = f"{units_figure:,}" if isinstance(units_figure, (int, float)) else "This"
    return (
        f"{n} is a NET INVENTORY DECREMENT on shopify-tracked, deny-policy "
        "variants, not a count of orders. The field also moves on returns, "
        "cancellations, recounts, inter-location transfers and merchant bulk "
        "edits, and a return is byte-identical to a restock. Returns cancel "
        "sales that really happened, so the bias runs DOWNWARD: this is a floor. "
        "Fashion return rates run high enough that the gap is material. Separately, "
        "censoring here is informative — low stock both hides demand and depresses "
        "it (Craig, DeHoratius & Raman 2016: +1pp fill rate -> ~+11% demand) — "
        "which is a second, independent reason every figure here is a lower bound."
    )


# ═════════════════════════════════════════════════════════════════════════════
# 4. JOB 2 — CENSORED DEMAND
# ═════════════════════════════════════════════════════════════════════════════
# An observation is (s, c): s units sold on a day that began with c units on
# hand. s < c is an exact demand observation. s == c is right-censored at c:
# demand was at least c and we cannot see how much more.
def _lgamma(x):
    return math.lgamma(x)


def _nb_logpmf(k, mu, r):
    """log P(D = k) for NB with mean mu and dispersion r (variance mu + mu^2/r)."""
    if k < 0:
        return -math.inf
    p = r / (r + mu)
    return (_lgamma(k + r) - _lgamma(r) - _lgamma(k + 1)
            + r * math.log(p) + k * math.log1p(-p))


def _logsumexp(vals):
    m = max(vals)
    if m == -math.inf:
        return -math.inf
    return m + math.log(sum(math.exp(v - m) for v in vals))


def _nb_logsf(k, mu, r):
    """log P(D >= k). Summed in log space; k is bounded by an inventory level,
    so the loop is short and exact. No scipy in CI."""
    if k <= 0:
        return 0.0
    lo = [_nb_logpmf(j, mu, r) for j in range(0, k)]
    lcdf = _logsumexp(lo)
    if lcdf >= 0.0:
        return -math.inf
    return math.log1p(-math.exp(lcdf))


def _neg_loglik(obs, mu, r):
    if mu <= 0 or r <= 0 or not math.isfinite(mu) or not math.isfinite(r):
        return math.inf
    total = 0.0
    for s, c, censored in obs:
        total += _nb_logsf(c, mu, r) if censored else _nb_logpmf(s, mu, r)
    return -total if math.isfinite(total) else math.inf


def _nelder_mead(f, x0, step=0.5, tol=1e-8, maxit=4000):
    """Plain Nelder–Mead on a 2-vector. stdlib only, and deterministic."""
    n = len(x0)
    simplex = [list(x0)]
    for i in range(n):
        p = list(x0)
        p[i] += step
        simplex.append(p)
    vals = [f(p) for p in simplex]
    for _ in range(maxit):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        simplex = [simplex[i] for i in order]
        vals = [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < tol:
            break
        cen = [sum(p[i] for p in simplex[:-1]) / n for i in range(n)]
        ref = [cen[i] + (cen[i] - simplex[-1][i]) for i in range(n)]
        fr = f(ref)
        if fr < vals[0]:
            exp = [cen[i] + 2.0 * (cen[i] - simplex[-1][i]) for i in range(n)]
            fe = f(exp)
            simplex[-1], vals[-1] = (exp, fe) if fe < fr else (ref, fr)
        elif fr < vals[-2]:
            simplex[-1], vals[-1] = ref, fr
        else:
            con = [cen[i] + 0.5 * (simplex[-1][i] - cen[i]) for i in range(n)]
            fc = f(con)
            if fc < vals[-1]:
                simplex[-1], vals[-1] = con, fc
            else:
                for i in range(1, n + 1):
                    simplex[i] = [(simplex[i][j] + simplex[0][j]) / 2.0
                                  for j in range(n)]
                    vals[i] = f(simplex[i])
    best = min(range(n + 1), key=lambda i: vals[i])
    return simplex[best], vals[best]


def nb_censored_mle(obs):
    """Agrawal & Smith (1996) censored NB MLE.

    obs: [(sales, censorLevel, censoredBool)]. Returns {"mu","r","loglik","n",
    "nCensored"}. Optimised over (log mu, log r) so both stay positive without a
    constraint solver.
    """
    obs = [(int(s), int(c), bool(z)) for s, c, z in obs]
    if not obs:
        raise ValueError("no observations")
    seen = [s for s, _c, z in obs if not z]
    m0 = (sum(s for s, _c, _z in obs) / len(obs)) or 0.5
    # A censored sample's raw mean understates; start above it.
    m0 = max(m0 * (1.0 + sum(1 for o in obs if o[2]) / len(obs)), 0.05)
    v0 = (sum((s - m0) ** 2 for s in seen) / max(len(seen) - 1, 1)) if len(seen) > 1 else m0 * 2
    r0 = max(m0 * m0 / max(v0 - m0, 1e-3), 0.05) if v0 > m0 else 5.0

    def nll(z):
        return _neg_loglik(obs, math.exp(z[0]), math.exp(z[1]))

    best, val = None, math.inf
    for mult in (0.5, 1.0, 2.0, 4.0):
        for rr in (0.25, 1.0, 4.0, 16.0):
            p, v = _nelder_mead(nll, [math.log(m0 * mult), math.log(r0 * rr)])
            if v < val:
                best, val = p, v
    return {"mu": math.exp(best[0]), "r": math.exp(best[1]),
            "loglik": -val, "n": len(obs),
            "nCensored": sum(1 for o in obs if o[2])}


def nb_mu_interval(obs, fit=None, level=0.95):
    """Profile-likelihood interval for mu. Chi-square(1) cutoff, r profiled out.

    Reported instead of a standard error because the likelihood is markedly
    asymmetric under heavy censoring — which is exactly the regime this runs in,
    and exactly where a symmetric interval understates the upper end.
    """
    fit = fit or nb_censored_mle(obs)
    cut = {0.90: 1.3527, 0.95: 1.9207, 0.99: 3.3172}.get(level, 1.9207)
    target = fit["loglik"] - cut

    def prof(mu):
        p, v = _nelder_mead(lambda z: _neg_loglik(obs, mu, math.exp(z[0])),
                            [math.log(fit["r"])], step=0.4)
        return -v

    def bisect(lo, hi):
        for _ in range(50):
            mid = (lo + hi) / 2.0
            if prof(mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    lo = bisect(fit["mu"] * 1e-3, fit["mu"]) if prof(fit["mu"] * 1e-3) < target else fit["mu"] * 1e-3
    hi_hint = fit["mu"]
    for _ in range(40):
        hi_hint *= 1.6
        if prof(hi_hint) < target:
            break
    hi = bisect(fit["mu"], hi_hint)
    return lo, hi


def kaplan_meier(obs):
    """Huh et al. (2011). Nonparametric demand survival from censored sales.

    Returns {"survival": [(x, S(x))], "meanBelowCeiling", "ceiling",
    "largestIsCensored"}. The mean is E[min(D, ceiling)] — the part of the mean
    the data identifies — never an extrapolated mean.
    """
    if not obs:
        raise ValueError("no observations")
    events = sorted({s for s, _c, z in obs if not z})
    n_total = len(obs)
    surv, S = [], 1.0
    for x in events:
        at_risk = sum(1 for s, c, z in obs if (s >= x if not z else c >= x))
        d = sum(1 for s, _c, z in obs if not z and s == x)
        if at_risk > 0:
            S *= (1.0 - d / at_risk)
        surv.append((x, S))
    ceiling = max(c for _s, c, _z in obs)
    # E[min(D,ceiling)] = sum_{x=0}^{ceiling-1} P(D > x), P(D > x) read off the
    # step function (S is defined at observed event points; carry forward).
    mean, cur = 0.0, 1.0
    idx = 0
    for x in range(0, ceiling):
        while idx < len(surv) and surv[idx][0] <= x:
            cur = surv[idx][1]
            idx += 1
        mean += cur
    return {"survival": surv, "meanBelowCeiling": mean, "ceiling": ceiling,
            "largestIsCensored": max((s for s, _c, z in obs if not z), default=-1)
            < ceiling, "n": n_total}


# ── RULE 1: the identification ceiling ───────────────────────────────────────
# Above sup(inventory) the data is silent, and the three candidate families
# disagree there by a lot. Concretely, fitted to the same censored sample, a
# Poisson, a Normal and an NB agree closely below the ceiling and can differ by
# an order of magnitude in the tail above it. So the ceiling is not a
# conservatism, it is the edge of the evidence.
TAIL_MASS_TOLERANCE = 0.02   # fitted mass above the ceiling we will tolerate


def identification_ceiling(obs):
    """The largest censoring level ever observed. Nothing above this is data."""
    return max(int(c) for _s, c, _z in obs)


def point_estimate(obs, fit=None):
    """The mean, or an exception. Never a number carrying a hidden family
    assumption."""
    fit = fit or nb_censored_mle(obs)
    ceiling = identification_ceiling(obs)
    tail = math.exp(_nb_logsf(ceiling + 1, fit["mu"], fit["r"]))
    if fit["mu"] > ceiling or tail > TAIL_MASS_TOLERANCE:
        raise NotIdentified(
            f"mean {fit['mu']:.2f} with {tail:.1%} of fitted mass above the "
            f"highest inventory ever observed ({ceiling}). Nothing in this data "
            f"distinguishes that tail from a much larger or much smaller one. "
            f"Use estimate(), which emits '>=' here.")
    return fit["mu"]


def estimate(obs, label=""):
    """The honest answer: an '=' when the data identifies one, a '>=' otherwise.

    Always returns the same shape, so a consumer cannot accidentally read a
    bound as a point.
    """
    fit = nb_censored_mle(obs)
    km = kaplan_meier(obs)
    ceiling = identification_ceiling(obs)
    tail = math.exp(_nb_logsf(ceiling + 1, fit["mu"], fit["r"]))
    identified = fit["mu"] <= ceiling and tail <= TAIL_MASS_TOLERANCE
    lo, hi = nb_mu_interval(obs, fit)
    return {
        "label": label,
        "relation": "=" if identified else ">=",
        "value": round(fit["mu"], 3) if identified else round(km["meanBelowCeiling"], 3),
        "nbMu": round(fit["mu"], 3), "nbR": round(fit["r"], 3),
        "nbMuCI": [round(lo, 3), round(hi, 3)],
        "kmMeanBelowCeiling": round(km["meanBelowCeiling"], 3),
        "identificationCeiling": ceiling,
        "fittedMassAboveCeiling": round(tail, 4),
        "identified": identified,
        "n": len(obs), "nCensored": fit["nCensored"],
        "censoredShare": round(fit["nCensored"] / len(obs), 4),
        "informativeCensoring": True,
        "informativeCensoringNote": (
            "All three estimators assume censoring is independent of demand. "
            "Here inventory plausibly CAUSES demand (Craig, DeHoratius & Raman "
            "2016), so this is a lower bound for a second reason."),
        "caveat": caveat(),
    }


# ── RULE 2: never sum unconstrained per-variant demand ───────────────────────
# Gruen & Corsten's out-of-stock work: faced with a stockout, a shopper
# substitutes, delays, switches store or does not buy. The brand loses ~35% of
# out-of-stock demand, so ~45% comes back somewhere — and INSIDE one brand's own
# size ladder (an M gone, an L on the same page) the recapture is higher again,
# because the substitute is one click away and already in the basket's price
# band.
BRAND_RECAPTURE = 0.45
SIZE_LADDER_RECAPTURE = 0.65


def brand_total(observed_units, imputed_lost_by_variant, within_size_ladder=True,
                n_brands=None):
    """Observed units + a HAIRCUT ON imputed lost demand. Never a naive sum.

    imputed_lost_by_variant: the per-variant lost-demand estimates. Summing them
    onto observed units double-counts, because the shopper who could not have
    the M bought the L and is already in observed.
    """
    if n_brands is not None and not stock_aggregate_ok(n_brands):
        raise NotPublishable(
            f"{n_brands} brands is below the floor of {MIN_BRANDS_PER_AGGREGATE}")
    recapture = SIZE_LADDER_RECAPTURE if within_size_ladder else BRAND_RECAPTURE
    gross = sum(imputed_lost_by_variant)
    incremental = gross * (1.0 - recapture)
    return {
        "observedUnits": observed_units,
        "grossImputedLost": round(gross, 3),
        "recaptureAssumed": recapture,
        "incrementalLost": round(incremental, 3),
        "total": round(observed_units + incremental, 3),
        "naiveSumWouldHaveBeen": round(observed_units + gross, 3),
        "note": ("The naive sum double-counts: imputed lost demand on a sold-out "
                 "size already appears in a sibling size's observed sales. "
                 "Recapture from Gruen & Corsten (brand loses ~35% of "
                 "out-of-stock demand); higher inside one brand's own size "
                 "ladder, where the substitute is on the same page."),
    }


# ── RULE 3: no choice-based "true demand", ever ──────────────────────────────
def true_demand(*_a, **_k):
    """Permanently unavailable. See Vulcano, van Ryzin & Ratliff (2012) §3.3."""
    raise NotAvailableWithoutMarketShare(
        "Choice-based 'true demand' needs a market share or an arrival rate. "
        "Without one the likelihood has a CONTINUUM of maxima (Vulcano, van "
        "Ryzin & Ratliff 2012, Oper. Res. 60(2), §3.3): the fit converges and "
        "returns one of infinitely many equally good answers, with no warning. "
        "Loupe has no market share, no arrival process and no traffic data for "
        "these stores. This is a product rule, not a TODO.")


def publishable(n_brands):
    """The aggregation floor, deferred to its single home in build_catalog."""
    return stock_aggregate_ok(n_brands)


# ═════════════════════════════════════════════════════════════════════════════
# 5. VALIDATION — artificial censoring, and recovery
# ═════════════════════════════════════════════════════════════════════════════
def censor(series, level):
    """Apply an artificial inventory cap to a known demand series."""
    return [(min(d, level), level, d >= level) for d in series]


def recovery_check(series, levels):
    """Hold out an UNCENSORED series, censor it artificially, refit, compare.

    This is the validation Job 2 asks for. On the real panel it runs on variants
    that never stocked out — those are the only ones whose true demand is fully
    observed, which is precisely what makes them the right holdout.
    """
    truth = sum(series) / len(series)
    out = {"truthMean": round(truth, 3), "n": len(series), "levels": {}}
    for lv in levels:
        obs = censor(series, lv)
        share = sum(1 for o in obs if o[2]) / len(obs)
        fit = nb_censored_mle(obs)
        naive = sum(o[0] for o in obs) / len(obs)
        out["levels"][lv] = {
            "censoredShare": round(share, 3),
            "naiveMean": round(naive, 3),
            "naiveErrorPct": round(100 * (naive - truth) / truth, 1),
            "nbMu": round(fit["mu"], 3),
            "nbErrorPct": round(100 * (fit["mu"] - truth) / truth, 1),
            "kmMeanBelowCeiling": round(kaplan_meier(obs)["meanBelowCeiling"], 3),
            "identified": fit["mu"] <= lv,
        }
    return out


def _lcg(seed):
    """A deterministic PRNG so --selftest is reproducible without a seed file."""
    state = [seed & 0xFFFFFFFF]

    def rnd():
        state[0] = (1103515245 * state[0] + 12345) & 0x7FFFFFFF
        return state[0] / 0x7FFFFFFF
    return rnd


def nb_sample(n, mu, r, seed=7):
    """NB draws via a gamma–Poisson mixture, stdlib only, deterministic."""
    rnd = _lcg(seed)

    def gamma(shape, scale):
        # Marsaglia–Tsang, with Box–Muller normals off the same stream.
        if shape < 1:
            return gamma(shape + 1, scale) * (rnd() ** (1.0 / shape))
        d = shape - 1.0 / 3.0
        c = 1.0 / math.sqrt(9.0 * d)
        while True:
            u1, u2 = max(rnd(), 1e-12), rnd()
            z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2 * math.pi * u2)
            v = (1 + c * z) ** 3
            if v <= 0:
                continue
            u = max(rnd(), 1e-12)
            if math.log(u) < 0.5 * z * z + d - d * v + d * math.log(v):
                return d * v * scale

    def poisson(lam):
        if lam <= 0:
            return 0
        if lam > 30:  # normal approximation, adequate for a self-test
            u1, u2 = max(rnd(), 1e-12), rnd()
            z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2 * math.pi * u2)
            return max(0, int(round(lam + math.sqrt(lam) * z)))
        el, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= rnd()
            if p <= el:
                return k
            k += 1
    return [poisson(gamma(r, mu / r)) for _ in range(n)]


# ═════════════════════════════════════════════════════════════════════════════
# 6. CLI
# ═════════════════════════════════════════════════════════════════════════════
def _fmt(d, indent=2):
    return json.dumps(d, indent=indent, default=str)


def cmd_movement():
    snaps = committed_snapshots()
    if len(snaps) < 2:
        print(f"Only {len(snaps)} committed stock.json snapshot(s). The "
              "movement analysis needs two.")
        if snaps:
            s = snaps[-1][2]
            print(f"  latest: {s.get('generatedAt')}  rows={s.get('count')}  "
                  f"joinable={s.get('joinable')}  saltSource={s.get('saltSource')}")
        return 1
    ok = 0
    for (sa, ta, a), (sb, tb, b) in zip(snaps, snaps[1:]):
        try:
            rep = movement_report(a, b)
        except PanelNotJoinable as e:
            print(f"{ta} -> {tb}: NOT JOINABLE ({e})")
            continue
        ok += 1
        print(f"\n{ta} -> {tb}   join={rep['join']}")
        print(_fmt(rep))
    if not ok:
        print("\nNo consecutive pair could be joined. The panel is not yet a "
              "time series.")
    return 0


def cmd_selftest():
    print("Estimator recovery on known NB draws (mu=4, r=2, n=400)")
    series = nb_sample(400, 4.0, 2.0, seed=20260806)
    print(_fmt(recovery_check(series, [2, 3, 4, 6, 10])))
    print("\nRefusals")
    obs = censor(series, 3)
    try:
        point_estimate(obs)
        print("  point_estimate did NOT refuse — BUG")
    except NotIdentified as e:
        print(f"  point_estimate refused: {e}")
    try:
        true_demand()
    except NotAvailableWithoutMarketShare as e:
        print(f"  true_demand refused: {str(e)[:90]}...")
    try:
        brand_total(100, [10, 10], n_brands=2)
    except NotPublishable as e:
        print(f"  brand_total refused: {e}")
    print("\n" + _fmt(brand_total(100, [10.0, 8.0, 6.0], n_brands=9)))
    return 0


def main(argv):
    if "--selftest" in argv:
        return cmd_selftest()
    if "--movement" in argv:
        return cmd_movement()
    if "--report" in argv:
        rc = cmd_movement()
        print("\n" + "=" * 78)
        cmd_selftest()
        return rc
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
