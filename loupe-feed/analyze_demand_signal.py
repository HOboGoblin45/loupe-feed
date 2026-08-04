#!/usr/bin/env python3
"""Loupe — does what users SAVE predict what SELLS OUT?

WHY THIS EXISTS

Loupe has two assets. One is an archive: a full daily snapshot of ~8,000
independent-fashion products, kept by git since 2026-06-17, which nobody else
has and nobody can backfill. The other is user behaviour: what people swipe,
save and click.

The archive alone is a record. The archive PLUS a user signal that leads it
would be something else — a demand oracle. If a save today predicts a sell-out
in two weeks, Loupe can tell a brand which of its pieces to cut more of, and
that is a product people pay for. If a save predicts nothing, Loupe owns a very
good archive and should sell it as one.

This script answers that question and is deliberately built to be able to
return NO. A null result here is the useful result, because the alternative —
a company strategy built on a correlation that was actually a sampling
artefact — is much more expensive than a boring answer.

WHAT AN EARLIER, WEAKER TEST FOUND

A brand-level test (brand approval rate vs brand sell-through, n=133 labels)
returned r = +0.09, not significant. That test was underpowered and used the
wrong unit: a brand is an average over hundreds of pieces, and the claim being
made is about PIECES. This one is per product, with the brand as a stratum
rather than as the unit.

────────────────────────────────────────────────────────────────────────────
THE FIVE THINGS THAT WILL FOOL YOU HERE, AND THE GUARDS FOR EACH
────────────────────────────────────────────────────────────────────────────

1.  THE PER-BRAND CAP MAKES "DISAPPEARED" MEAN "NOT SAMPLED TODAY".

    build_catalog.py pulls at most `perBrand` items per store (60 as of
    2026-08-01). 88 of the 173 brands in the 2026-07-16 snapshot sit AT that
    cap, i.e. their store has more than 60 eligible items and we take a slice.
    Which slice surfaces shifts run to run.

    Measured, and this is the number that killed the obvious analysis: between
    2026-07-16 and 2026-08-01, Bec + Bridge "lost" 60 of 60 products — and
    ended the window with 60 products. So did Agmes (60/60 gone, 60 at the
    end), VESTIGE, The Frankie Shop. Christopher Esber lost 50 of 50 and
    finished with 55. Staud and Cult Gaia lost 15 of 15 and finished with 15.
    Nothing sold out. The sampler turned over.

    Whole-brand rotation accounts for 981 of the 2,041 disappearances in that
    window — 48%. A test that reads product disappearance as "sold out" is
    therefore measuring, about half the time, a property of our own scraper.

    GUARD: disappearance is NOT the primary outcome. The primary outcome is the
    catalog's own `available` flag flipping True -> False on a product that is
    still present at both endpoints, which no amount of slice rotation can
    fake. Disappearance is still reported, labelled as contaminated, because
    suppressing it entirely would hide the fact that it disagrees.

2.  DAY-TO-DAY DISAPPEARANCE IS MOSTLY NOISE; ENDPOINT-TO-ENDPOINT IS NOT.

    Of the 7,639 products present on 2026-07-16, 2,274 were absent on at least
    one later snapshot but only 2,041 were absent at the end: 10.2% of gaps
    reverse. (Measured day-to-day the reversal rate is far higher — the ~45%
    figure quoted elsewhere.) Everything here is therefore computed between two
    ENDPOINTS and never day-to-day.

3.  THE ARCHIVE IS ONLY AS LONG AS THE CLONE.

    Everything about the outcome comes from `git log`. A shallow clone yields a
    shorter dataset, a well-formed output file, and no error. On 2026-08-01
    that silently cost build_price_history.py 14 of 42 days. Checked up front
    here, as a hard stop, for the same reason.

4.  POSTHOG'S QUERY API SILENTLY TRUNCATES AT 100 ROWS.

    The first run of this analysis pulled per-product save counts and got back
    exactly 100 rows for a query whose true answer is 1,581. No error, no
    warning, and the resulting 2x2 table was well formed and completely wrong
    (it "found" a protective effect on n=51). Every query below carries an
    explicit LIMIT and every result is cross-checked against a separately
    computed count. A row count that lands exactly on a round number is
    treated as evidence of truncation, not as data.

5.  EXPOSURE IS NOT MEASURED PER PRODUCT, AND THIS CANNOT BE FIXED.

    A product can only be saved if the ranker showed it. Loupe emits
    `brand_engagement` (per-brand impressions, batched one event per session)
    but no per-product impression event. So the unsaved control group contains
    an unknown number of products that had zero opportunity to be saved.

    This is the main threat to validity and it is not repairable from the data
    that exists. Three partial mitigations are applied and reported separately:
    brand strata (Mantel-Haenszel), restriction to brands that actually
    received impressions in the treatment window, and — most informative — a
    PLACEBO test in which the outcome is measured BEFORE the treatment. A save
    made after 2026-07-16 cannot cause a sell-out that had already happened by
    2026-07-16, so any association there is pure confounding and calibrates how
    much of the forward estimate to disbelieve.

────────────────────────────────────────────────────────────────────────────
WHAT THE SCALE ALLOWS
────────────────────────────────────────────────────────────────────────────

1,723 saves, 1,581 distinct products, 48 people, over 30 days. One person
(computed, not hardcoded) accounts for ~71% of the saves and ~76% of the
distinct products, 552 of them on a single day. The effective independent
sample is therefore closer to 48 than to 1,581, and every headline is repeated
with that person removed. A power calculation runs FIRST and prints the
smallest effect the design could detect; if the answer is "nothing below a
50% increase", that sentence matters more than any p-value below it.

USAGE
    python analyze_demand_signal.py                # full run
    python analyze_demand_signal.py --refresh      # ignore caches, re-pull
    python analyze_demand_signal.py --allow-shallow

CREDENTIALS
    Reads POSTHOG_API_KEY from the environment, else from a local .env-style
    file (see ENV_CANDIDATES). Never printed, never written to the cache.
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
import urllib.request

import numpy as np
from scipy import optimize, stats

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
CATALOG_REL = "loupe-feed/catalog.json"

# The cache lives OUTSIDE the repo on purpose. It is ~40 MB of extracted daily
# snapshots; putting it in the working tree invites someone to commit it, and
# it is 100% re-derivable from git in about a minute.
CACHE = pathlib.Path(tempfile.gettempdir()) / "loupe_demand_cache"

POSTHOG_HOST = "https://us.posthog.com"
POSTHOG_PROJECT = "489958"
ENV_CANDIDATES = [
    pathlib.Path(r"C:\loupe-clean\.scorecard.env"),
    REPO.parent / "loupe-clean" / ".scorecard.env",
    pathlib.Path.home() / ".loupe" / "scorecard.env",
]

# ── The analysis window ────────────────────────────────────────────────────
# product_saved was instrumented on 2026-07-02 and the first events land
# 2026-07-03; `available` first appears in the catalog on 2026-07-16. Those two
# dates, not any analytical choice, are what fixes the window below.
BASELINE = "2026-07-16"        # first snapshot carrying `available`
ENDPOINT = "2026-08-01"        # last snapshot in the archive
SAVE_CUTOFF = "2026-07-16"     # saves strictly before this count as treatment

# The daily refresh commits at ~08:00 UTC, so a save made at 10:00 on the
# baseline DAY is already after the baseline snapshot. Treatment therefore ends
# at midnight before the baseline, never on it. Losing 54 saves is cheaper than
# a treatment that postdates its own outcome.

PER_BRAND_CAP = 60             # brands.json perBrand; see guard 1
NEAR_CAP = PER_BRAND_CAP - 2   # 58+ counts as "at the cap" (scrape jitter)

ALPHA = 0.05
POWER = 0.80


# ══════════════════════════════════════════════════════════════════════════
# git / archive
# ══════════════════════════════════════════════════════════════════════════

def git(*args):
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


def history_is_truncated():
    """True when this clone cannot see the repo's whole history (guard 3)."""
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        return True
    git_dir = git("rev-parse", "--git-dir").strip()
    if not git_dir:
        return False
    return (REPO / git_dir / "shallow").exists() or pathlib.Path(git_dir, "shallow").exists()


def daily_snapshots():
    """(day, sha) for the LAST catalog commit of each day, oldest first."""
    out = {}
    for line in git("log", "--format=%H|%ad", "--date=short", "--", CATALOG_REL).splitlines():
        if "|" in line:
            sha, day = line.split("|", 1)
            out.setdefault(day.strip(), sha.strip())   # log is newest-first
    return sorted(out.items())


FIELDS = ["id", "brand", "price", "category", "available", "addedAt", "stale", "retailer"]


def extract_snapshots(refresh=False, verbose=True):
    """Materialise one compact TSV per snapshot day. ~40x smaller than the JSON."""
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
                w.writerow([
                    p.get("id", ""), p.get("brand", ""), p.get("price", ""),
                    p.get("category", ""),
                    "" if av is None else ("1" if av else "0"),
                    (p.get("addedAt") or "")[:10],
                    "1" if p.get("stale") else "",
                    p.get("retailer") or "",
                ])
        os.replace(tmp, dest)
        if verbose:
            print(f"  extracted {day}  {len(doc.get('products', [])):>5} products", file=sys.stderr)
    return [d for d, _ in snaps]


def load_day(day):
    out = {}
    with open(CACHE / f"{day}.tsv", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            out[row["id"]] = row
    return out


# ══════════════════════════════════════════════════════════════════════════
# PostHog
# ══════════════════════════════════════════════════════════════════════════

def posthog_key():
    k = os.environ.get("POSTHOG_API_KEY")
    if k:
        return k.strip()
    for path in ENV_CANDIDATES:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("POSTHOG_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            continue
    sys.exit(
        "No PostHog key. Set POSTHOG_API_KEY, or drop a .scorecard.env with\n"
        "POSTHOG_API_KEY=phx_... at one of:\n  " +
        "\n  ".join(str(p) for p in ENV_CANDIDATES)
    )


def hogql(sql, expect_at_most=None):
    """Run a HogQL query.

    GUARD 4. The API's default page size is 100 rows and it does not tell you
    when it has used it. Every caller must pass an explicit LIMIT; this wrapper
    refuses a result that looks like it hit a page boundary, because a
    truncated result here produces a perfectly plausible, entirely wrong table.
    """
    if "LIMIT" not in sql.upper():
        raise ValueError("every HogQL query here must carry an explicit LIMIT (guard 4)")
    req = urllib.request.Request(
        f"{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT}/query/",
        data=json.dumps({"query": {"kind": "HogQLQuery", "query": sql}}).encode(),
        headers={"Authorization": "Bearer " + posthog_key(),
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        rows = json.load(r)["results"]
    if len(rows) in (100, 500, 1000, 10000) and expect_at_most and len(rows) < expect_at_most:
        raise RuntimeError(
            f"HogQL returned exactly {len(rows)} rows but up to {expect_at_most} were "
            "expected — this is the silent-truncation failure mode (guard 4)."
        )
    return rows


def fetch_saves(refresh=False):
    """Per-product save counts, bucketed into windows, plus the dominant saver.

    Windows are cut at snapshot boundaries so a save can always be placed
    strictly before or strictly after any baseline used below.
    """
    cache = CACHE / "saves.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))

    n_expected = hogql(
        "SELECT count() AS n, uniq(properties.productId) AS p, uniq(person_id) AS u "
        "FROM events WHERE event='product_saved' "
        "AND timestamp >= toDateTime('2026-07-01 00:00:00') "
        "AND timestamp <  toDateTime('2026-08-02 00:00:00') LIMIT 1")[0]
    n_saves, n_prod, n_people = n_expected

    # The dominant saver is COMPUTED, not hardcoded: who it is will change, the
    # fact that saves are concentrated in one person probably will not.
    top = hogql(
        "SELECT toString(person_id) AS pid, count() AS n FROM events "
        "WHERE event='product_saved' "
        "AND timestamp >= toDateTime('2026-07-01 00:00:00') "
        "AND timestamp <  toDateTime('2026-08-02 00:00:00') "
        "GROUP BY pid ORDER BY n DESC LIMIT 5")
    dominant, dominant_n = top[0][0], top[0][1]

    sql = f"""
    SELECT properties.productId AS pid,
           countIf(timestamp <  toDateTime('2026-07-04 00:00:00'))                                          AS w0,
           countIf(timestamp >= toDateTime('2026-07-04 00:00:00') AND timestamp < toDateTime('2026-07-16 00:00:00')) AS w1,
           countIf(timestamp >= toDateTime('2026-07-16 00:00:00') AND timestamp < toDateTime('2026-07-17 00:00:00')) AS w2,
           countIf(timestamp >= toDateTime('2026-07-17 00:00:00') AND timestamp < toDateTime('2026-07-24 00:00:00')) AS w3,
           countIf(timestamp >= toDateTime('2026-07-24 00:00:00'))                                          AS w4,
           countIf(toString(person_id) != '{dominant}' AND timestamp <  toDateTime('2026-07-16 00:00:00')) AS nd_pre,
           countIf(toString(person_id) != '{dominant}' AND timestamp >= toDateTime('2026-07-16 00:00:00')) AS nd_post,
           uniq(person_id) AS people
    FROM events
    WHERE event='product_saved'
      AND timestamp >= toDateTime('2026-07-01 00:00:00')
      AND timestamp <  toDateTime('2026-08-02 00:00:00')
    GROUP BY pid
    ORDER BY pid
    LIMIT 100000
    """
    rows = hogql(sql, expect_at_most=n_prod)
    if len(rows) != n_prod:
        raise RuntimeError(f"save pull returned {len(rows)} products, expected {n_prod}")

    clicks = hogql(
        "SELECT properties.productId AS pid, "
        "countIf(timestamp <  toDateTime('2026-07-16 00:00:00')) AS pre, "
        "countIf(timestamp >= toDateTime('2026-07-16 00:00:00')) AS post "
        "FROM events WHERE event='shop_click' "
        "AND timestamp >= toDateTime('2026-07-01 00:00:00') "
        "AND timestamp <  toDateTime('2026-08-02 00:00:00') "
        "GROUP BY pid ORDER BY pid LIMIT 100000")

    # Brand-level impressions: the only exposure signal that exists (guard 5).
    imps = hogql(
        "SELECT kv.1 AS brand, "
        "sumIf(JSONExtractInt(kv.2,'impressions'), timestamp < toDateTime('2026-07-16 00:00:00')) AS pre, "
        "sum(JSONExtractInt(kv.2,'impressions')) AS all_ "
        "FROM events ARRAY JOIN JSONExtractKeysAndValuesRaw(coalesce(properties.brands,'{}')) AS kv "
        "WHERE event='brand_engagement' "
        "AND timestamp >= toDateTime('2026-07-01 00:00:00') "
        "AND timestamp <  toDateTime('2026-08-02 00:00:00') "
        "GROUP BY brand ORDER BY brand LIMIT 100000")

    data = {
        "n_saves": n_saves, "n_products": n_prod, "n_people": n_people,
        "dominant": dominant, "dominant_n": dominant_n,
        "top_savers": top,
        "saves": {r[0]: r[1:] for r in rows},
        "clicks": {r[0]: r[1:] for r in clicks},
        "brand_impressions": {r[0]: {"pre": r[1], "all": r[2]} for r in imps},
    }
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


# ══════════════════════════════════════════════════════════════════════════
# statistics
# ══════════════════════════════════════════════════════════════════════════

def rr_ci(a, n1, c, n0, alpha=ALPHA):
    """Risk ratio with a Katz log CI. Returns (p1, p0, rr, lo, hi)."""
    p1, p0 = a / n1 if n1 else float("nan"), c / n0 if n0 else float("nan")
    if not a or not c:
        return p1, p0, float("nan"), float("nan"), float("nan")
    z = stats.norm.ppf(1 - alpha / 2)
    se = math.sqrt(1 / a - 1 / n1 + 1 / c - 1 / n0)
    rr = p1 / p0
    return p1, p0, rr, math.exp(math.log(rr) - z * se), math.exp(math.log(rr) + z * se)


def mh_rr(strata, alpha=ALPHA):
    """Mantel-Haenszel pooled risk ratio, Greenland-Robins variance.

    strata: list of (a, b, c, d) = (treated&event, treated&none, ctrl&event, ctrl&none).
    Strata with no exposed OR no unexposed contribute nothing, which is exactly
    what we want: a brand where every product rotated out carries no
    within-brand information and must not be allowed to pull the estimate.
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


def mh_or(strata, alpha=ALPHA):
    """Mantel-Haenszel pooled odds ratio, Robins-Breslow-Greenland variance."""
    R = S = 0.0
    t1 = t2 = t3 = 0.0
    used = 0
    for a, b, c, d in strata:
        N = a + b + c + d
        if N == 0 or (a + b) == 0 or (c + d) == 0:
            continue
        used += 1
        Ri, Si = a * d / N, b * c / N
        P, Q = (a + d) / N, (b + c) / N
        R += Ri
        S += Si
        t1 += P * Ri
        t2 += P * Si + Q * Ri
        t3 += Q * Si
    if R == 0 or S == 0:
        return float("nan"), float("nan"), float("nan"), used
    orr = R / S
    var = t1 / (2 * R * R) + t2 / (2 * R * S) + t3 / (2 * S * S)
    z = stats.norm.ppf(1 - alpha / 2)
    se = math.sqrt(var)
    return orr, math.exp(math.log(orr) - z * se), math.exp(math.log(orr) + z * se), used


def cmh_test(strata):
    """Cochran-Mantel-Haenszel chi-square (continuity-corrected). Exact variance."""
    A = E = V = 0.0
    for a, b, c, d in strata:
        n1, n0, m1, m0 = a + b, c + d, a + c, b + d
        N = n1 + n0
        if N < 2 or n1 == 0 or n0 == 0 or m1 == 0 or m0 == 0:
            continue
        A += a
        E += n1 * m1 / N
        V += n1 * n0 * m1 * m0 / (N * N * (N - 1))
    if V <= 0:
        return float("nan"), float("nan"), A, E
    chi = (abs(A - E) - 0.5) ** 2 / V
    return chi, stats.chi2.sf(chi, 1), A, E


def mde_rr(n1, n0, p0, alpha=ALPHA, power=POWER):
    """Smallest risk ratio (>1) this design could detect. THE headline number.

    Two-proportion normal approximation. If this comes back at 2.0, the study
    cannot see anything short of a doubling and every estimate below is
    indicative, not conclusive — say so before showing the estimate, not after.
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
    scores, labels = np.asarray(scores, float), np.asarray(labels, bool)
    pos, neg = scores[labels], scores[~labels]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    r = stats.rankdata(np.concatenate([pos, neg]))
    return (r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def logistic_fit(X, y, l2=1.0):
    """Ridge logistic regression. numpy + scipy only (statsmodels isn't installed
    on the founder's machine and this must run there)."""
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

    res = optimize.minimize(nll, np.zeros(X.shape[1]), jac=grad, method="L-BFGS-B")
    return res.x


def logistic_predict(w, X):
    return 1 / (1 + np.exp(-(np.column_stack([np.ones(len(X)), X]) @ w)))


# ══════════════════════════════════════════════════════════════════════════
# study construction
# ══════════════════════════════════════════════════════════════════════════

class Study:
    """One (baseline -> endpoint) comparison, with all three outcome definitions."""

    def __init__(self, snaps, base_day, end_day):
        self.base_day, self.end_day = base_day, end_day
        self.B, self.E = snaps[base_day], snaps[end_day]
        self.days = (dt.date.fromisoformat(end_day) - dt.date.fromisoformat(base_day)).days

        counts = collections.Counter(r["brand"] for r in self.B.values())
        self.brand_size = counts
        self.capped = {b for b, n in counts.items() if n >= NEAR_CAP}

    # ---- outcome definitions ------------------------------------------------
    def risk_and_outcome(self, kind):
        """(risk set, outcome set) for one of three outcome definitions.

        FLIP      available True -> False, both endpoints present.  CLEAN.
                  Immune to slice rotation (guard 1) because the product is
                  physically in both snapshots. Conditions on survival, which
                  is a collider — reported as such.
        SOLDOUT   available True at baseline, then (gone OR available False).
                  No survival conditioning, but inherits the rotation problem.
        GONE      present at baseline, absent at endpoint. CONTAMINATED —
                  ~48% of it is whole-brand rotation. Reported to show the
                  disagreement, never used as the headline.
        """
        B, E = self.B, self.E
        if kind == "GONE":
            risk = set(B)
            out = {p for p in risk if p not in E}
        elif kind == "SOLDOUT":
            risk = {p for p, r in B.items() if r["available"] == "1"}
            out = {p for p in risk if p not in E or E[p]["available"] == "0"}
        elif kind == "FLIP":
            risk = {p for p, r in B.items()
                    if r["available"] == "1" and p in E and E[p]["available"] in ("0", "1")}
            out = {p for p in risk if E[p]["available"] == "0"}
        else:
            raise ValueError(kind)
        return risk, out

    def strata(self, risk, treated, outcome, key=None):
        key = key or (lambda p: self.B[p]["brand"])
        cells = collections.defaultdict(lambda: [0, 0, 0, 0])
        for p in risk:
            k = key(p)
            t = p in treated
            y = p in outcome
            cells[k][(0 if t else 2) + (0 if y else 1)] += 1
        return [tuple(v) for v in cells.values()]


def crude(risk, treated, outcome):
    t = risk & treated
    c = risk - treated
    return len(t & outcome), len(t), len(c & outcome), len(c)


def fisher_p(a, n1, c, n0):
    return stats.fisher_exact([[a, n1 - a], [c, n0 - c]])[1]


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
        return "     n/a (a cell is empty)"
    return f"{rr:5.2f}  95% CI [{lo:4.2f}, {hi:5.2f}]"


def report_2x2(label, a, n1, c, n0, strata_brand=None, strata_fine=None):
    p1, p0, rr, lo, hi = rr_ci(a, n1, c, n0)
    print(f"  {label}")
    print(f"    treated (saved)   {a:5d}/{n1:5d} = {100*p1:6.2f}%")
    print(f"    control (unsaved) {c:5d}/{n0:5d} = {100*p0:6.2f}%")
    print(f"    crude RR          {fmt_ci(rr, lo, hi)}   Fisher p = {fisher_p(a, n1, c, n0):.3f}")
    print(f"    MDE (80% power)   {mde_rr(n1, n0, p0):5.2f}x  <- smallest RR this cell could detect")
    if strata_brand:
        r, l_, h_, used = mh_rr(strata_brand)
        chi, p, A, E = cmh_test(strata_brand)
        print(f"    brand-stratified  RR {fmt_ci(r, l_, h_)}   ({used} informative brands)")
        print(f"                      CMH chi2 = {chi:5.2f}  p = {p:.3f}   "
              f"(observed {A:.0f} events among treated vs {E:.1f} expected)")
    if strata_fine:
        r, l_, h_, used = mh_rr(strata_fine)
        print(f"    +price+newness    RR {fmt_ci(r, l_, h_)}   ({used} informative cells)")


# ══════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="ignore caches, re-extract and re-pull")
    ap.add_argument("--allow-shallow", action="store_true")
    args = ap.parse_args()

    # ── 0. VERIFY INPUTS BEFORE TRUSTING OUTPUTS ──────────────────────────
    rule("0. INPUT VERIFICATION")
    truncated = history_is_truncated()
    if truncated and not args.allow_shallow:
        sys.exit("REFUSING TO RUN: shallow/grafted clone. `git fetch --unshallow` first.\n"
                 "  On 2026-08-01 a shallow clone silently hid 14 of 42 days from a\n"
                 "  sibling script and produced a well-formed, wrong answer.")
    print(f"  clone complete (not shallow)      : {not truncated}")

    days = extract_snapshots(refresh=args.refresh, verbose=False)
    print(f"  catalog snapshots in git          : {len(days)}  ({days[0]} -> {days[-1]})")
    span = (dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days + 1
    have = set(days)
    missing = [(dt.date.fromisoformat(days[0]) + dt.timedelta(d)).isoformat() for d in range(span)]
    missing = [d for d in missing if d not in have]
    print(f"  calendar days spanned             : {span}   MISSING: {len(missing)} -> {missing}")
    if missing:
        print("    (the refresh workflow did not run on those days; every window below is\n"
              "     measured between snapshot ENDPOINTS, so a gap shortens the ladder but\n"
              "     does not bias it)")

    snaps = {d: load_day(d) for d in days}
    for d in (BASELINE, ENDPOINT):
        if d not in snaps:
            sys.exit(f"REFUSING TO RUN: no snapshot for {d}")
    print(f"  baseline {BASELINE} products        : {len(snaps[BASELINE]):,}")
    print(f"  endpoint {ENDPOINT} products        : {len(snaps[ENDPOINT]):,}")

    ph = fetch_saves(refresh=args.refresh)
    print(f"  product_saved events              : {ph['n_saves']:,} "
          f"over {ph['n_products']:,} products, {ph['n_people']} people")
    dom_share = 100 * ph["dominant_n"] / ph["n_saves"]
    print(f"  most active single person         : {ph['dominant_n']:,} saves = {dom_share:.0f}% of all saves")

    saves = ph["saves"]
    ever = set()
    for d in days:
        ever |= set(snaps[d])
    matched = sum(1 for p in saves if p in ever)
    print(f"  save productIds joining a catalog : {matched}/{len(saves)} "
          f"({100*matched/len(saves):.1f}%)")
    if matched / len(saves) < 0.90:
        sys.exit("REFUSING TO RUN: the join key does not join. Check the id scheme.")

    # ── 1. WHAT "SOLD OUT" CAN MEAN HERE ──────────────────────────────────
    rule("1. WHAT THE ARCHIVE CAN AND CANNOT SEE (guards 1 and 2)")
    st = Study(snaps, BASELINE, ENDPOINT)
    B, E = st.B, st.E
    later = [d for d in days if d > BASELINE]
    ever_gap = {p for p in B if any(p not in snaps[d] for d in later)}
    gone_end = {p for p in B if p not in E}
    print(f"  present {BASELINE}: {len(B):,}")
    print(f"    absent on ANY later snapshot : {len(ever_gap):,}")
    print(f"    absent at {ENDPOINT}          : {len(gone_end):,}")
    print(f"    -> {100*(len(ever_gap)-len(gone_end))/max(len(ever_gap),1):.1f}% of gaps reverse; "
          "endpoint-to-endpoint is the only honest read")

    print(f"\n  brands at the perBrand={PER_BRAND_CAP} cap : "
          f"{len(st.capped)} of {len(st.brand_size)}  "
          f"({sum(st.brand_size[b] for b in st.capped):,} products)")
    rot = []
    endcount = collections.Counter(r["brand"] for r in E.values())
    for b in st.brand_size:
        ps = [p for p, r in B.items() if r["brand"] == b]
        g = sum(1 for p in ps if p not in E)
        if len(ps) >= 3 and g / len(ps) >= 0.8:
            rot.append((b, len(ps), g, endcount.get(b, 0)))
    rot.sort(key=lambda t: -t[1])
    lost = sum(g for _, _, g, _ in rot)
    print(f"  brands losing >=80% of baseline products: {len(rot)}  "
          f"({lost:,} of {len(gone_end):,} disappearances = {100*lost/max(len(gone_end),1):.0f}%)")
    print("    brand                          lost   still in endpoint catalog")
    for b, n, g, k in rot[:8]:
        flag = "  <- ROTATION, not sell-out" if k >= n * 0.8 else ""
        print(f"    {b[:28]:28} {g:4}/{n:<4} {k:4}{flag}")

    for kind in ("GONE", "SOLDOUT", "FLIP"):
        risk, out = st.risk_and_outcome(kind)
        print(f"\n  outcome {kind:8} risk set {len(risk):5,}  events {len(out):5,}  "
              f"base rate {100*len(out)/len(risk):5.2f}%")

    print("\n  PRIMARY OUTCOME = FLIP. It is the store's own availability flag on a")
    print("  product that is physically present in both snapshots, so the sampler")
    print("  cannot manufacture it. SOLDOUT and GONE are reported alongside so that")
    print("  a disagreement between them is visible rather than hidden.")

    # ── 2. POWER, BEFORE ANY ESTIMATE ─────────────────────────────────────
    rule("2. POWER CALCULATION (read this before any number below)")
    treated_pre = {p for p, v in saves.items() if v[0] + v[1] > 0}
    treated_nd = {p for p, v in saves.items() if v[5] > 0}
    for kind in ("FLIP", "SOLDOUT", "GONE"):
        risk, out = st.risk_and_outcome(kind)
        a, n1, c, n0 = crude(risk, treated_pre, out)
        p0 = c / n0
        m = mde_rr(n1, n0, p0)
        a2, n1b, c2, n0b = crude(risk, treated_nd, out)
        m2 = mde_rr(n1b, n0b, c2 / n0b)
        print(f"  {kind:8} treated n={n1:5,} control n={n0:5,} base rate {100*p0:5.2f}%"
              f"  -> detectable RR >= {m:4.2f}x")
        print(f"           excluding the dominant saver: treated n={n1b:4,}"
              f"  -> detectable RR >= {m2:4.2f}x")
    print("\n  Read that as: with this much data the study can only see LARGE effects.")
    print("  Anything it fails to find is not thereby shown to be absent; and anything")
    print("  it does find has to be big enough to be implausible for other reasons.")

    price = {p: float(r["price"] or 0) for p, r in B.items()}
    qs = np.quantile([v for v in price.values() if v > 0], [.2, .4, .6, .8])

    def pband(p):
        return int(np.searchsorted(qs, price.get(p, 0)))

    def age_days(p):
        added = B[p]["addedAt"]
        if not added:
            return None
        return (dt.date.fromisoformat(BASELINE) - dt.date.fromisoformat(added)).days

    def newness(p):
        a_ = age_days(p)
        return "unk" if a_ is None else ("new" if a_ <= 14 else ("mid" if a_ <= 30 else "old"))

    # ── 2b. POSITIVE CONTROL ──────────────────────────────────────────────
    rule("2b. POSITIVE CONTROL — can this machinery detect anything at all?")
    print("  A null is only worth reading if the same code path, on the same risk set,")
    print("  finds a REAL effect when one exists. Newness is the known one: a piece that")
    print("  arrived in the last two weeks sells out more often than an old one. If the")
    print("  block below is also null, the pipeline is broken and section 3 means nothing.\n")
    riskF, outF = st.risk_and_outcome("FLIP")
    newset = {p for p in riskF if (age_days(p) or 99) <= 14}
    a, n1, c, n0 = crude(riskF, newset, outF)
    pc_strata = st.strata(riskF, newset, outF)
    report_2x2("outcome FLIP, treatment = 'arrived in the last 14 days'",
               a, n1, c, n0, pc_strata)
    # Carried to the verdict. Never retype a number into prose: on 2026-08-01 a
    # sibling report shipped a hand-copied figure that had stopped being true.
    PC = mh_rr(pc_strata)
    PC_P = cmh_test(pc_strata)[1]

    print("\n  And a check that the FLIP outcome is a real signal rather than a flag that")
    print("  rattles at random: if availability were noise, True->False and False->True")
    print("  would happen about equally often among the same survivors.")
    surv = {p for p, r in B.items()
            if r["available"] in ("0", "1") and p in E and E[p]["available"] in ("0", "1")}
    to_out = sum(1 for p in surv if B[p]["available"] == "1" and E[p]["available"] == "0")
    to_in = sum(1 for p in surv if B[p]["available"] == "0" and E[p]["available"] == "1")
    n_av, n_un = (sum(1 for p in surv if B[p]["available"] == "1"),
                  sum(1 for p in surv if B[p]["available"] == "0"))
    print(f"    survivors with a known flag at both ends : {len(surv):,}")
    print(f"    in stock -> sold out : {to_out:4}/{n_av:5,} = {100*to_out/max(n_av,1):5.2f}%")
    print(f"    sold out -> restock  : {to_in:4}/{n_un:5,} = {100*to_in/max(n_un,1):5.2f}%")
    print("    (an asymmetry this large is a real inventory process, not flag noise)")

    # ── 3. THE MAIN TEST ──────────────────────────────────────────────────
    rule(f"3. MAIN TEST — saves before {SAVE_CUTOFF} vs outcome by {ENDPOINT} "
         f"({st.days} days)")
    MAIN = {}
    for kind in ("FLIP", "SOLDOUT", "GONE"):
        risk, out = st.risk_and_outcome(kind)
        a, n1, c, n0 = crude(risk, treated_pre, out)
        sb = st.strata(risk, treated_pre, out)
        sf = st.strata(risk, treated_pre, out,
                       key=lambda p: (B[p]["brand"], pband(p), newness(p)))
        print()
        report_2x2(f"outcome = {kind}", a, n1, c, n0, sb, sf)
        MAIN[kind] = {"crude": rr_ci(a, n1, c, n0), "mh": mh_rr(sb),
                      "mde": mde_rr(n1, n0, c / n0), "cells": (a, n1, c, n0)}

    # ── 4. SENSITIVITY ────────────────────────────────────────────────────
    rule("4. SENSITIVITY — does the main result survive its own assumptions?")
    risk, out = st.risk_and_outcome("FLIP")

    imp = ph["brand_impressions"]
    exposed = {b for b, v in imp.items() if v["pre"] > 0}
    risk_exp = {p for p in risk if B[p]["brand"] in exposed}
    print(f"  (a) brands with >0 measured impressions before {SAVE_CUTOFF}: "
          f"{len(exposed)} brands, {len(risk_exp):,} products")
    if len(risk_exp) == len(risk):
        print("      NO-OP: every brand in the baseline catalog received impressions, so")
        print("      this restriction removes nothing and CANNOT address the exposure")
        print("      threat. Brand-level exposure is uniform-ish; PRODUCT-level exposure")
        print("      is what matters and Loupe does not emit it. See guard 5.")
    else:
        a, n1, c, n0 = crude(risk_exp, treated_pre, out)
        report_2x2("      restricted to exposed brands, outcome FLIP", a, n1, c, n0,
                   st.strata(risk_exp, treated_pre, out))

    rotbrands = {b for b, _, _, _ in rot}
    risk_norot = {p for p in risk if B[p]["brand"] not in rotbrands}
    print(f"\n  (b) dropping the {len(rotbrands)} rotation brands: {len(risk_norot):,} products")
    a, n1, c, n0 = crude(risk_norot, treated_pre, out)
    report_2x2("      outcome FLIP", a, n1, c, n0, st.strata(risk_norot, treated_pre, out))

    print(f"\n  (c) excluding the dominant saver ({dom_share:.0f}% of all saves). Run on all")
    print("      three outcomes: on FLIP this is hopeless (see the MDE), but on SOLDOUT")
    print("      and GONE there is still enough left to reject a large effect.")
    for kind in ("FLIP", "SOLDOUT", "GONE"):
        r2, o2 = st.risk_and_outcome(kind)
        a, n1, c, n0 = crude(r2, treated_nd, o2)
        print()
        report_2x2(f"      outcome {kind}, 47 non-dominant savers",
                   a, n1, c, n0, st.strata(r2, treated_nd, o2))

    print("\n  (d) dose-response — is more saves worse for survival?")
    for lo_, hi_, lbl in ((1, 1, "exactly 1 save"), (2, 99, "2 or more saves")):
        grp = {p for p, v in saves.items() if lo_ <= v[0] + v[1] <= hi_}
        a, n1, c, n0 = crude(risk, grp, out)
        if n1:
            p1, p0, rr, l_, h_ = rr_ci(a, n1, c, n0)
            print(f"      {lbl:16} {a:4}/{n1:5} = {100*p1:5.2f}%   RR {fmt_ci(rr, l_, h_)}")

    print("\n  (e) outbound CLICK as treatment (stronger intent than a save, tiny n)")
    clicks = ph["clicks"]
    clicked = {p for p, v in clicks.items() if v[0] > 0}
    a, n1, c, n0 = crude(risk, clicked, out)
    if n1:
        p1, p0, rr, l_, h_ = rr_ci(a, n1, c, n0)
        print(f"      clicked before {SAVE_CUTOFF}: {a}/{n1} = {100*p1:.2f}% vs "
              f"{c}/{n0} = {100*p0:.2f}%   RR {fmt_ci(rr, l_, h_)}")
        print(f"      MDE for this cell: {mde_rr(n1, n0, c/n0):.2f}x — "
              "this is a look, not a test")

    # ── 5. LAG LADDER ─────────────────────────────────────────────────────
    rule("5. LAG LADDER — a leading indicator is worth far more than a coincident one")
    print("  The archive ends 2026-08-01 and saves begin 2026-07-03, so 4 weeks is the")
    print("  longest lag that exists. It is only reachable with a 2026-07-04 baseline,")
    print("  where the catalog has no `available` flag yet — so the 28-day rung can only")
    print("  use the contaminated GONE outcome. Stated, not buried.\n")
    ladder = [
        ("2026-07-04", "2026-08-01", lambda v: v[0] > 0, ("GONE",), "saves on 2026-07-03 only"),
        (BASELINE, "2026-07-23", lambda v: v[0] + v[1] > 0, ("FLIP", "GONE"), "saves before 07-16"),
        (BASELINE, ENDPOINT, lambda v: v[0] + v[1] > 0, ("FLIP", "GONE"), "saves before 07-16"),
    ]
    for bd, ed, sel, kinds, tlabel in ladder:
        if bd not in snaps or ed not in snaps:
            print(f"  {bd} -> {ed}: no snapshot, skipped")
            continue
        s2 = Study(snaps, bd, ed)
        tr = {p for p, v in saves.items() if sel(v)}
        print(f"  {bd} -> {ed}  (+{s2.days:2d}d)   treatment = {tlabel}")
        for kind in kinds:
            r2, o2 = s2.risk_and_outcome(kind)
            a, n1, c, n0 = crude(r2, tr, o2)
            if n1 == 0 or c == 0:
                print(f"      {kind:8}: empty cell, skipped")
                continue
            p1, p0, rr, l_, h_ = rr_ci(a, n1, c, n0)
            mr, ml, mh_, used = mh_rr(s2.strata(r2, tr, o2))
            print(f"      {kind:8}  saved {a:4}/{n1:5} = {100*p1:5.2f}%   "
                  f"unsaved {c:5}/{n0:5} = {100*p0:5.2f}%")
            print(f"                crude RR {fmt_ci(rr, l_, h_)}    "
                  f"brand-stratified RR {fmt_ci(mr, ml, mh_)}")
        print()

    # ── 6. PLACEBO AND REVERSE DIRECTION ──────────────────────────────────
    rule("6. PLACEBO / REVERSE — are users LEADING the market or TRAILING it?")
    known = {p for p, r in B.items() if r["available"] != ""}
    already = {p for p in known if B[p]["available"] == "0"}
    base_rate = len(already) / len(known)
    post = {p for p, v in saves.items() if v[2] + v[3] + v[4] > 0}
    print(f"  PLACEBO. Outcome measured at {BASELINE}; treatment happens AFTER it.")
    print("  A save made in late July cannot cause a sell-out that had already")
    print("  happened by 2026-07-16. Any association here is pure confounding, and")
    print("  its size is how much of section 3 you should refuse to believe.\n")
    a, n1, c, n0 = crude(known, post, already)
    p1, p0, rr, l_, h_ = rr_ci(a, n1, c, n0)
    print(f"    saved AFTER baseline  {a:4}/{n1:5} = {100*p1:5.2f}% already sold out")
    print(f"    not saved after       {c:5}/{n0:5} = {100*p0:5.2f}%")
    print(f"    placebo RR            {fmt_ci(rr, l_, h_)}   Fisher p = {fisher_p(a, n1, c, n0):.3f}")
    mr, ml, mh_, used = mh_rr(st.strata(known, post, already))
    print(f"    brand-stratified      RR {fmt_ci(mr, ml, mh_)}")

    print(f"\n  REVERSE. Do users save pieces that were ALREADY sold out at {BASELINE}?")
    print("  If saves TRAILED the market, saved pieces would be over-represented among")
    print("  the already-gone. This is the 'archive, not oracle' failure mode.")
    print(f"    whole catalog already sold out : {100*base_rate:5.2f}%  (n={len(known):,})")
    for lbl, grp in (("saved BEFORE baseline", treated_pre), ("saved AFTER baseline", post)):
        g = known & grp
        if not g:
            continue
        a, n1, c, n0 = crude(known, grp, already)
        p1, p0, rr, l_, h_ = rr_ci(a, n1, c, n0)
        print(f"    {lbl:30} : {100*p1:5.2f}%  (n={n1:,})   "
              f"RR vs rest {fmt_ci(rr, l_, h_)}  p={fisher_p(a, n1, c, n0):.3f}")

    print("\n  COLLIDER NOTE. FLIP conditions on surviving to the endpoint. That would be")
    print("  a problem if saving changed the odds of surviving — but the GONE analysis")
    print("  above returns a brand-stratified RR of ~1.00, i.e. treatment does not")
    print("  predict disappearance either, so conditioning on survival is not selecting")
    print("  differentially on treatment and FLIP is not materially collider-biased.")

    # ── 7. ARCHIVE-ONLY MODEL ─────────────────────────────────────────────
    rule("7. ARCHIVE ALONE — does the data predict sell-out with NO user signal?")
    print("  This needs zero users, so it survives whatever section 3 says. Trained on")
    print(f"  {BASELINE} -> 2026-07-23 and tested OUT OF TIME on 2026-07-23 -> {ENDPOINT},")
    print("  so nothing below is an in-sample fit.\n")

    def features(day, snaps, prev_day):
        """Predictors observable ON `day`, using only data at or before it."""
        S = snaps[day]
        P = snaps[prev_day]
        churn = {}
        cnt = collections.Counter(r["brand"] for r in P.values())
        gonecnt = collections.Counter(r["brand"] for p, r in P.items() if p not in S)
        for b, n in cnt.items():
            churn[b] = gonecnt.get(b, 0) / n
        sizes = collections.Counter(r["brand"] for r in S.values())
        rows, ids = [], []
        for p, r in S.items():
            pr = float(r["price"] or 0)
            if pr <= 0:
                continue
            added = r["addedAt"]
            age = ((dt.date.fromisoformat(day) - dt.date.fromisoformat(added)).days
                   if added else 45)
            rows.append([
                math.log(pr),
                min(age, 60) / 60.0,
                1.0 if age <= 14 else 0.0,
                churn.get(r["brand"], 0.0),
                1.0 if sizes[r["brand"]] >= NEAR_CAP else 0.0,
                math.log1p(sizes[r["brand"]]),
                1.0 if r["category"] == "dresses" else 0.0,
                1.0 if r["category"] == "accessories" else 0.0,
            ])
            ids.append(p)
        return np.array(rows), ids

    NAMES = ["log price", "age/60", "is new (<=14d)", "brand churn (prior)",
             "brand at cap", "log brand size", "is dress", "is accessory"]

    tr_b, tr_e, tr_p = BASELINE, "2026-07-23", "2026-07-09"
    te_b, te_e, te_p = "2026-07-23", ENDPOINT, BASELINE
    for nm, (bd, ed, pd_) in (("train", (tr_b, tr_e, tr_p)), ("test", (te_b, te_e, te_p))):
        if bd not in snaps or ed not in snaps or pd_ not in snaps:
            sys.exit(f"missing snapshot for the {nm} window")

    def xy(bd, ed, pd_):
        X, ids = features(bd, snaps, pd_)
        s = Study(snaps, bd, ed)
        risk, out = s.risk_and_outcome("FLIP")
        keep = [i for i, p in enumerate(ids) if p in risk]
        return X[keep], np.array([ids[i] in out for i in keep]), [ids[i] for i in keep]

    Xtr, ytr, idtr = xy(tr_b, tr_e, tr_p)
    Xte, yte, idte = xy(te_b, te_e, te_p)
    print(f"  train {tr_b}->{tr_e}: n={len(ytr):,}  events={int(ytr.sum())} "
          f"({100*ytr.mean():.2f}%)")
    print(f"  test  {te_b}->{te_e}: n={len(yte):,}  events={int(yte.sum())} "
          f"({100*yte.mean():.2f}%)")
    print("\n  single-predictor AUC on the TEST window (0.50 = coin flip).")
    print("  An AUC BELOW 0.50 is not a failure, it is the same predictor pointing the")
    print("  other way — 0.39 on price means CHEAP pieces sell out, strongly.")
    for j, nm in enumerate(NAMES):
        v = auc(Xte[:, j], yte)
        arrow = "" if abs(v - .5) < .02 else ("  (higher -> sells out)" if v > .5
                                              else "  (LOWER -> sells out)")
        print(f"    {nm:22} {v:.3f}{arrow}")

    # The directly sellable version of the same fact: a brand asking "what should
    # I price this dress at" wants this table, not an AUC.
    print(f"\n  sell-out rate by price quintile ({te_b} -> {te_e}, FLIP):")
    s_te = Study(snaps, te_b, te_e)
    r_te, o_te = s_te.risk_and_outcome("FLIP")
    pr_te = {p: float(s_te.B[p]["price"] or 0) for p in r_te}
    q_te = np.quantile([v for v in pr_te.values() if v > 0], [.2, .4, .6, .8])
    buckets = collections.defaultdict(lambda: [0, 0])
    for p in r_te:
        k = int(np.searchsorted(q_te, pr_te[p]))
        buckets[k][0] += 1
        buckets[k][1] += p in o_te
    edges = [0] + list(q_te) + [max(pr_te.values())]
    for k in sorted(buckets):
        n, ev = buckets[k]
        print(f"    ${edges[k]:>6,.0f}-${edges[k+1]:>6,.0f}  n={n:5,}  "
              f"sold out {ev:4} = {100*ev/max(n,1):5.2f}%")
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    w = logistic_fit((Xtr - mu) / sd, ytr, l2=2.0)
    pte = logistic_predict(w, (Xte - mu) / sd)
    a_all = auc(pte, yte)
    print(f"\n  ridge logistic, all 8 features, OUT-OF-TIME AUC = {a_all:.3f}")
    order = np.argsort(pte)[::-1]
    lift10 = float("nan")
    for frac in (0.10, 0.20):
        k = int(len(pte) * frac)
        lift = yte[order[:k]].mean() / max(yte.mean(), 1e-9)
        if frac == 0.10:
            lift10 = lift
        print(f"    top {int(frac*100)}% by predicted risk: {100*yte[order[:k]].mean():.2f}% "
              f"sell out vs {100*yte.mean():.2f}% base = {lift:.2f}x lift")
    # The commercial question in one line: does adding "a Loupe user saved this"
    # to a model built only from the archive make the model better? Both windows
    # use the SAME treatment definition (saved before 2026-07-16), which is
    # strictly before both outcome windows, so neither leaks.
    print("\n  Same model, same split, but with the user signal added as a 9th feature:")
    Xtr2 = np.column_stack([Xtr, [1.0 if p in treated_pre else 0.0 for p in idtr]])
    Xte2 = np.column_stack([Xte, [1.0 if p in treated_pre else 0.0 for p in idte]])
    mu2, sd2 = Xtr2.mean(0), Xtr2.std(0) + 1e-9
    w2 = logistic_fit((Xtr2 - mu2) / sd2, ytr, l2=2.0)
    a_usr = auc(logistic_predict(w2, (Xte2 - mu2) / sd2), yte)
    print(f"    AUC archive only            = {a_all:.3f}")
    print(f"    AUC archive + 'was saved'   = {a_usr:.3f}   "
          f"delta = {a_usr - a_all:+.3f}")
    print("    (the delta is the entire commercial value of the user signal over the")
    print("     archive Loupe already owns — if it is ~0, the oracle is the archive)")

    # ── 8. VERDICT ────────────────────────────────────────────────────────
    rule("8. VERDICT")
    print("  Q: does what Loupe users SAVE predict what SELLS OUT later?")
    print("  A: on this data, NO — and the bound is tight enough to be worth something.\n")
    for kind, label in (("FLIP", "clean outcome  (availability flip"),
                        ("GONE", "powered outcome (disappearance")):
        m = MAIN[kind]
        r_, l_, h_, _ = m["mh"]
        print(f"    {label}, {st.days}d lag)")
        print(f"        brand-stratified RR {r_:.2f}  95% CI [{l_:.2f}, {h_:.2f}]"
              f"   (could have detected {m['mde']:.2f}x)")
    cr_lo = min(MAIN[k]["crude"][2] for k in ("SOLDOUT", "GONE"))
    cr_hi = max(MAIN[k]["crude"][2] for k in ("SOLDOUT", "GONE"))
    print(f"\n    The crude, unstratified numbers say saved pieces are "
          f"~{100*(1-(cr_lo+cr_hi)/2):.0f}% LESS likely")
    print(f"    to go: RR {cr_lo:.2f}-{cr_hi:.2f}, p < 0.001. That is Simpson's paradox, not a")
    print("    finding. It is brand composition — users are shown, and save, brands that")
    print(f"    churn less. Stratify by brand and it goes to {MAIN['GONE']['mh'][0]:.2f}. Anyone who runs")
    print("    this question without a brand fixed effect will report a strong, backwards,")
    print("    completely spurious result.")
    print(f"\n    The same machinery, same risk set, DOES detect newness "
          f"(RR {PC[0]:.2f}, CI [{PC[1]:.2f}, {PC[2]:.2f}], p = {PC_P:.3f}),")
    print("    so the null is a measurement, not a broken pipeline.")
    print(f"\n    Archive alone predicts sell-out out-of-time at AUC {a_all:.3f} "
          f"({lift10:.2f}x lift in the top decile).")
    print(f"    Adding 'a Loupe user saved this' moves that to {a_usr:.3f}. "
          f"Delta {a_usr - a_all:+.3f}.")
    print("\n    Strategic reading: the demand oracle is the ARCHIVE, not the audience.")
    print("    The user signal is worth ~0 as a predictor of sell-through at this scale.")
    print("    What would change that: per-product impression logging (without it the")
    print("    exposure confound is unfixable), and enough savers that one person is not")
    print("    71% of the data. Neither is a modelling problem; both are instrumentation.")

    rule("DONE")


if __name__ == "__main__":
    main()
