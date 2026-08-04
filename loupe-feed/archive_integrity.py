#!/usr/bin/env python3
"""Loupe -- the integrity record for the daily catalog archive.

WHY THIS EXISTS

Everything else in this repo can be rebuilt. The archive cannot. catalog.json is
re-scraped from the brands' live storefronts every morning and committed, so the
git history is a day-by-day observation of what 209 independent labels were
selling and what they were charging. That series is the one asset here that money
cannot buy: a competitor with unlimited budget still starts at day zero. Its
value is a function of its LENGTH, and a missing day is not a bug that can be
retried -- the storefronts have already changed.

THE INCIDENT THIS IS BUILT AROUND (2026-07-25 -> 2026-07-28)

On 2026-07-24, commit 0732108 added a $5 hard price floor to build_catalog.py's
is_junk() and did not update test_junk_filter.py. refresh-catalog.yml runs those
fixtures BEFORE the build, so from the next morning every scheduled run died at
the fixture step and published nothing. Commit 4a85662 fixed the fixture on
2026-07-29. Four days -- 2026-07-25, -26, -27, -28 -- are permanently absent.

Read the failure mode carefully, because it decides the design:

  * The job did NOT fail quietly. It failed hard, on a red step, and GitHub sent
    its "run failed" email each time. Nobody looked. Four consecutive daily
    failure emails were not enough signal.
  * Nothing anywhere reported the ABSENCE. There was no place to look that would
    have said "the archive is 3 days behind", so the only way to notice was to
    already suspect something.
  * The watchdog cannot live inside the thing it watches. A check bolted onto
    the end of refresh-catalog.yml would have been skipped by exactly the
    failure it exists to detect, on all four days.

So: this module MEASURES (gaps, runs, coverage, freshness), refresh-catalog.yml
publishes the measurement into the repo on every commit, and archive-watchdog.yml
re-measures on a schedule of its own and shouts through channels the build does
not own.

THE SHALLOW-CLONE TRAP

The input is `git log`, so the answer depends on how the repo was CLONED, and
nothing about running the script reveals that. On 2026-08-01 the working clone
was shallow: it could see 28 of 42 daily snapshots and said so with no warning.
build_price_history.py hit this first and grew history_is_truncated(); this
module imports that same guard rather than re-implementing it, and refuses to
report by default when it fires. An integrity record that under-counts the
archive is worse than none, because it will be believed.

  CI NOTE: actions/checkout defaults to fetch-depth: 1. Any workflow that runs
  this MUST set `fetch-depth: 0`. Measured 2026-08-01, the whole repo is ~95 MB
  of .git (10 MB packed) -- minified single-line JSON delta-compresses extremely
  well -- so full history is a cheap checkout, not an expensive one.

USAGE
    python archive_integrity.py                      # human report
    python archive_integrity.py --with-counts        # ...plus per-day product counts
    python archive_integrity.py --json               # machine-readable, stdout
    python archive_integrity.py --write              # ARCHIVE_STATUS.md + archive_status.json
    python archive_integrity.py --check-age 36       # exit 1 if newest snapshot > 36h old
    python archive_integrity.py --heartbeat          # exit 1 if this build produced nothing new
    python archive_integrity.py --allow-shallow      # report anyway, stamped partial
"""

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import time

# Import rather than re-implement. Two reasons, both load-bearing:
#   * history_is_truncated() is the guard build_price_history.py grew after the
#     2026-08-01 shallow-clone incident. One copy means one thing to keep right.
#   * daily_snapshots() defines what "a day in the archive" MEANS. If this file
#     counted days differently from the file that builds the price history, the
#     integrity record would be certifying a dataset nobody actually ships.
#     test_archive_integrity.py pins that the two agree.
from build_price_history import (
    CATALOG_REL,
    REPO,
    daily_snapshots,
    history_is_truncated,
)

HERE = pathlib.Path(__file__).resolve().parent
STATUS_MD = REPO / "ARCHIVE_STATUS.md"
STATUS_JSON = HERE / "archive_status.json"

# How old the newest snapshot may be before the archive is considered at risk.
# The build runs at 08:00 UTC daily, so a healthy archive is never more than ~24h
# behind; 36h is one missed run plus half a day of slack, which means the first
# missed morning is caught the same evening rather than four days later.
STALE_HOURS = 36

# Day-over-day product-count movement that is implausible enough to stop a
# publish. Both directions, because the two failure shapes are different:
#
#   DOWN -- build_catalog.py's own PUBLISH GUARD already refuses at -20% (and at
#     -10% distinct brand labels) before it writes anything, so this is a second
#     line at a different layer: the guard compares the new build against the
#     file on disk, this compares the working tree against what is COMMITTED, so
#     it also catches "the build never wrote at all".
#   UP -- nothing else checks this. A duplicate-emitting bug or a junk filter
#     that stopped firing inflates the catalog, and inflation looks like success.
#
# Calibration, measured over all 42 archived days: the catalog has been stable
# since it finished bootstrapping. From 2026-07-01 the largest single-day move in
# either direction is +5.6% (2026-07-21, a brand batch) and the largest fall is
# -1.2%. The pre-2026-06-27 bootstrap did move +47% in a day, which is why this
# is overridable -- but only from a human-triggered run, never from cron.
MAX_DAILY_DROP = 0.20
MAX_DAILY_RISE = 0.25

# generatedAt is a wall clock, so it changes on every run whether or not the
# scrape found anything. Comparing raw bytes would therefore NEVER report two
# identical catalogs. Strip it, and "identical" starts meaning what it says.
_GENERATED_AT_RE = re.compile(rb'"generatedAt":\s*"[^"]*"')

# Why each gap happened, keyed by the exact range so a cause can never end up
# attached to a hole it did not cause. Anything not in here renders as
# "cause not recorded", which is a prompt, not a decoration: a gap somebody can
# explain is a bad morning, and a gap nobody can explain is a diligence problem.
KNOWN_CAUSES = {
    ("2026-07-25", "2026-07-28"):
        "`2026-07-25` -> `2026-07-28`: commit `0732108` (2026-07-24) added a $5 hard "
        "price floor to `is_junk()` without updating `test_junk_filter.py`. The daily "
        "workflow runs the fixtures before the build, so every run failed at that step "
        "and published nothing until `4a85662` fixed the fixture on 2026-07-29. The "
        "workflow failed loudly all four days; nobody was looking. That is what this "
        "file and the watchdog exist to change.",
}


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def git_text(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


def git_bytes(*args: str) -> bytes:
    """Raw bytes, deliberately NOT decoded.

    Anything that gets hashed has to come through here. Decoding with
    errors="replace" (which git_text does, correctly, for log output) silently
    rewrites every byte it cannot map, so two different catalogs could digest
    the same. A comparison that can quietly return "equal" is the exact class of
    bug this file exists to catch.
    """
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True,
    ).stdout


def payload_digest(raw: bytes) -> str:
    """sha256 of a catalog snapshot with its generatedAt timestamp removed."""
    return hashlib.sha256(_GENERATED_AT_RE.sub(b"", raw)).hexdigest()


def head_stats(raw: bytes):
    """(products, brands) from the first 8 KB of a snapshot -- no full parse.

    catalog.json is ~9 MB. Both numbers live in the header (`count`, and
    `provenance.brands` since provenance was added), so reading 8 KB answers it.
    Snapshots older than provenance return brands=None rather than a guess.
    """
    head = raw[:8192].decode("utf-8", "replace")
    products = None
    m = re.search(r'"count":\s*(\d+)', head)
    if m:
        products = int(m.group(1))
    brands = None
    i = head.find('"provenance"')
    if i != -1:
        j = head.find("{", i)
        depth = 0
        for k in range(j, len(head)):
            if head[k] == "{":
                depth += 1
            elif head[k] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        prov = json.loads(head[j:k + 1])
                    except ValueError:
                        prov = {}
                    if isinstance(prov.get("brands"), int):
                        brands = prov["brands"]
                    break
    return products, brands


def newest_snapshot_epoch():
    """Commit time (unix, UTC) of the newest commit touching the catalog."""
    out = git_text("log", "-1", "--format=%ct", "--", CATALOG_REL).strip()
    return int(out) if out.isdigit() else None


def hours_since(epoch, now=None):
    """EXACT hours since a unix timestamp -- never the value rounded for display.

    The record rounds the age to one decimal so the status page reads nicely.
    Comparing that rounded number against a threshold is right at 36h and wrong
    at anything under 0.05h, and a watchdog that only works at its default
    setting cannot be tested at any other one -- which is how it stops being
    testable at all. Caught 2026-08-01 by the end-to-end replay.
    """
    if epoch is None:
        return None
    return ((time.time() if now is None else now) - epoch) / 3600.0


# ---------------------------------------------------------------------------
# analysis -- pure functions over a list of ISO day strings
# ---------------------------------------------------------------------------

def _plus(day: str, n: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()


def _span(a: str, b: str) -> int:
    """Inclusive day count from a to b."""
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days + 1


def runs_of(days):
    """Unbroken consecutive-day runs, as [(first, last, length), ...]."""
    if not days:
        return []
    out = []
    start = prev = days[0]
    for d in days[1:]:
        if d == _plus(prev, 1):
            prev = d
            continue
        out.append((start, prev, _span(start, prev)))
        start = prev = d
    out.append((start, prev, _span(start, prev)))
    return out


def missing_days(days):
    """Every calendar day between the first and last snapshot with no snapshot."""
    if len(days) < 2:
        return []
    have = set(days)
    cur, last, gone = days[0], days[-1], []
    while cur < last:
        cur = _plus(cur, 1)
        if cur not in have and cur != last:
            gone.append(cur)
    return gone


def analyze(days, truncated=False, today=None):
    """The whole integrity picture, from nothing but the day list."""
    today = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    if not days:
        return {
            "observedDays": 0, "first": None, "last": None, "spanDays": 0,
            "missingDays": [], "gapCount": 0, "coveragePct": 0.0,
            "longestRun": None, "currentStreak": 0, "daysSinceLastSnapshot": None,
            "partialHistory": bool(truncated), "clean": False,
        }

    runs = runs_of(days)
    gone = missing_days(days)
    span = _span(days[0], days[-1])
    longest = max(runs, key=lambda r: r[2])
    # A run that begins at the OLDEST day this clone can see is not a measured
    # run -- it is a run that was cut off by the clone. Reporting it as "the
    # longest unbroken run" would be reporting a fact about git-fetch.
    boundary_open = truncated and longest[0] == days[0]
    streak = runs[-1][2]

    return {
        "observedDays": len(days),
        "first": days[0],
        "last": days[-1],
        "spanDays": span,
        "missingDays": gone,
        "gapCount": len(gone),
        "gapRanges": [(a, b) for a, b, _ in runs_of(gone)],
        "coveragePct": round(100.0 * len(days) / span, 2),
        "longestRun": {
            "start": longest[0], "end": longest[1], "days": longest[2],
            # True == the run is a LOWER BOUND; it may have started earlier than
            # this clone can see. Absent/False == measured end to end.
            **({"boundaryOpen": True} if boundary_open else {}),
        },
        "runs": len(runs),
        "currentStreak": streak,
        "daysSinceLastSnapshot": (dt.date.fromisoformat(today)
                                  - dt.date.fromisoformat(days[-1])).days,
        # Never emitted as False: an absent flag has to mean "complete", or a
        # consumer that forgets to check the key gets the optimistic answer.
        **({"partialHistory": True} if truncated else {}),
        "clean": (not gone) and not truncated,
    }


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------

def refuse_if_truncated(allow_shallow: bool) -> bool:
    """Same contract as build_price_history.build(): hard stop unless asked."""
    truncated = history_is_truncated()
    if truncated and not allow_shallow:
        sys.exit(
            "REFUSING TO REPORT: this clone's history is truncated (shallow/grafted).\n"
            "\n"
            "  Every number below is derived from `git log`, so a shallow clone\n"
            "  reports FEWER days than the archive holds and invents a start date\n"
            "  that describes the clone. On 2026-08-01 that hid 14 of 42 days.\n"
            "  An integrity record that under-counts is worse than none: it will\n"
            "  be believed, and it makes the asset look smaller than it is.\n"
            "\n"
            "  Fix it:      git fetch --unshallow\n"
            "  In CI:       actions/checkout@v4  with:  fetch-depth: 0\n"
            "  Anyway:      python archive_integrity.py --allow-shallow\n"
            "               (output is stamped partialHistory: true)"
        )
    return truncated


def collect(allow_shallow=False, with_counts=False):
    truncated = refuse_if_truncated(allow_shallow)
    if truncated:
        print("WARNING: shallow/grafted clone -- this is a FRAGMENT, not the record.",
              file=sys.stderr)

    snaps = daily_snapshots()
    days = [d for d, _ in snaps]
    st = analyze(days, truncated=truncated)

    # Freshness. Distinct from gaps: a gap is a permanent historical fact, but
    # staleness is the live emergency, and it is the only one that is still
    # fixable at the moment it is detected.
    epoch = newest_snapshot_epoch()
    age_h = hours_since(epoch)
    st["newestSnapshotEpoch"] = epoch
    st["newestSnapshotAgeHours"] = None if age_h is None else round(age_h, 1)
    st["staleHours"] = STALE_HOURS
    st["stale"] = bool(age_h is not None and age_h > STALE_HOURS)

    # Scale of the newest snapshot -- the "M brands" half of the headline.
    st["products"] = st["brands"] = None
    if snaps:
        products, brands = head_stats(git_bytes("show", f"{snaps[-1][1]}:{CATALOG_REL}"))
        st["products"], st["brands"] = products, brands

    if with_counts:
        st["perDay"] = []
        for day, sha in snaps:
            products, brands = head_stats(git_bytes("show", f"{sha}:{CATALOG_REL}"))
            st["perDay"].append({"day": day, "products": products, "brands": brands})

    st["generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return st


# ---------------------------------------------------------------------------
# headline + report
# ---------------------------------------------------------------------------

def headline(st) -> str:
    """The one line. Deliberately fits in an email subject and a git commit."""
    lr = st.get("longestRun") or {}
    run = f"{lr.get('days', 0)}{'+' if lr.get('boundaryOpen') else ''}"
    if st["gapCount"] == 0:
        gaps = "none"
    else:
        gaps = "%d (%s)" % (st["gapCount"], ", ".join(
            a if a == b else f"{a}..{b}" for a, b in st.get("gapRanges", [])))
    bits = [
        f"{st['observedDays']} days",
        f"{st['brands']} brands" if st.get("brands") else None,
        f"{st['products']:,} products" if st.get("products") else None,
        f"longest run {run}",
        f"gaps: {gaps}",
    ]
    line = ", ".join(b for b in bits if b)
    if st.get("partialHistory"):
        line += "  [PARTIAL CLONE -- lower bound]"
    if st.get("stale"):
        line += "  [STALE: newest snapshot %.0fh old]" % st["newestSnapshotAgeHours"]
    return line


def report(st):
    print("=" * 78)
    print("LOUPE CATALOG ARCHIVE -- INTEGRITY RECORD")
    print("=" * 78)
    print(headline(st))
    print()
    if st.get("partialHistory"):
        print("  !! PARTIAL -- this clone is shallow. Every figure is a LOWER BOUND and")
        print("     'first' is where the CLONE begins, not where the data does.")
        print("     Run `git fetch --unshallow` and re-run before quoting any of it.")
        print()
    print(f"  window            : {st['first']} -> {st['last']}  ({st['spanDays']} calendar days)")
    print(f"  snapshots         : {st['observedDays']}")
    print(f"  coverage          : {st['coveragePct']}%")
    lr = st.get("longestRun") or {}
    print(f"  longest run       : {lr.get('days')} days  ({lr.get('start')} -> {lr.get('end')})"
          + ("   [open at the left boundary]" if lr.get("boundaryOpen") else ""))
    print(f"  current streak    : {st['currentStreak']} days")
    print(f"  days since last   : {st['daysSinceLastSnapshot']}")
    if st.get("newestSnapshotAgeHours") is not None:
        flag = "  <-- STALE" if st.get("stale") else ""
        print(f"  newest snapshot   : {st['newestSnapshotAgeHours']}h old "
              f"(threshold {STALE_HOURS}h){flag}")
    print()
    if st["gapCount"] == 0:
        print("  GAPS              : none. Every day between the first and last is present.")
    else:
        print(f"  GAPS              : {st['gapCount']} missing day(s) -- permanently unrecoverable")
        for a, b in st.get("gapRanges", []):
            n = _span(a, b)
            print(f"      {a} -> {b}   ({n} day{'s' if n != 1 else ''})")
    if st.get("perDay"):
        print("\n  PER-DAY")
        print(f"    {'day':12} {'products':>9} {'brands':>7}  {'delta':>8}")
        prev = None
        for row in st["perDay"]:
            n = row["products"]
            d = "" if (prev in (None, 0) or n is None) else f"{100.0 * (n - prev) / prev:+.1f}%"
            print(f"    {row['day']:12} {('?' if n is None else format(n, ',')):>9} "
                  f"{('?' if row['brands'] is None else row['brands']):>7}  {d:>8}")
            if n:
                prev = n
    print()


def status_markdown(st) -> str:
    lines = []
    lines.append("# Loupe catalog archive -- integrity record")
    lines.append("")
    lines.append(f"**{headline(st)}**")
    lines.append("")
    lines.append(
        "This file is generated by `loupe-feed/archive_integrity.py` on every catalog "
        "commit and re-checked twice a day by `.github/workflows/archive-watchdog.yml`. "
        "It is a machine-made continuity record for a dataset whose whole value is that "
        "it has no holes -- do not edit it by hand."
    )
    lines.append("")
    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| window | `{st['first']}` -> `{st['last']}` ({st['spanDays']} calendar days) |")
    lines.append(f"| daily snapshots | **{st['observedDays']}** |")
    lines.append(f"| coverage | **{st['coveragePct']}%** |")
    lr = st.get("longestRun") or {}
    open_note = " _(open at the clone boundary -- lower bound)_" if lr.get("boundaryOpen") else ""
    lines.append(f"| longest unbroken run | **{lr.get('days')} days** "
                 f"(`{lr.get('start')}` -> `{lr.get('end')}`){open_note} |")
    lines.append(f"| current streak | **{st['currentStreak']} days** |")
    if st.get("brands"):
        lines.append(f"| brands in newest snapshot | {st['brands']} |")
    if st.get("products"):
        lines.append(f"| products in newest snapshot | {st['products']:,} |")
    if st.get("newestSnapshotAgeHours") is not None:
        lines.append(f"| newest snapshot age | {st['newestSnapshotAgeHours']}h "
                     f"(stale above {STALE_HOURS}h) |")
    lines.append("")
    if st["gapCount"] == 0:
        lines.append("## Gaps")
        lines.append("")
        lines.append("**None.** Every calendar day between the first and last snapshot is present.")
    else:
        lines.append("## Gaps")
        lines.append("")
        lines.append(f"**{st['gapCount']} missing day(s).** A missing day cannot be backfilled: "
                     "the storefronts it would have recorded have already changed.")
        lines.append("")
        lines.append("| from | to | days |")
        lines.append("|---|---|---|")
        for a, b in st.get("gapRanges", []):
            lines.append(f"| `{a}` | `{b}` | {_span(a, b)} |")
        lines.append("")
        # Only ever attached to the gap it actually explains. A cause pinned to
        # the wrong gap is a lie with a citation on it, and this file is meant to
        # be the thing somebody checks instead of taking a founder's word.
        for a, b in KNOWN_CAUSES:
            if (a, b) in st.get("gapRanges", []):
                lines.append("> " + KNOWN_CAUSES[(a, b)])
                lines.append("")
        unexplained = [r for r in st.get("gapRanges", []) if tuple(r) not in KNOWN_CAUSES]
        if unexplained:
            lines.append("> Cause not recorded for: "
                         + ", ".join(f"`{a}` -> `{b}`" for a, b in unexplained)
                         + ". Find the failing run in Actions and write it down here "
                           "(`KNOWN_CAUSES` in `archive_integrity.py`) -- an unexplained "
                           "gap is the one an acquirer will ask about.")
    lines.append("")
    if st.get("partialHistory"):
        lines.append("> **PARTIAL** -- generated from a shallow clone, so every figure above is a "
                     "lower bound and `window` starts where the clone starts. Re-run after "
                     "`git fetch --unshallow`.")
        lines.append("")
    lines.append(f"_Generated {st.get('generatedAt')} by `archive_integrity.py`._")
    lines.append("")
    # Collapse runs of blank lines: the optional blocks above each pad themselves,
    # and this file is re-diffed every day -- stray whitespace churn makes the one
    # diff anyone should be reading harder to read.
    out = []
    for line in lines:
        if line == "" and out and out[-1] == "":
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# heartbeat -- run inside the daily build, before the commit
# ---------------------------------------------------------------------------

def heartbeat(cur=None, prev=None, max_drop=MAX_DAILY_DROP, max_rise=MAX_DAILY_RISE,
              override=False):
    """Did this build actually produce a new, plausible catalog? -> (ok, lines).

    The 2026-07-25 failure was loud. The failure this guards against is the
    quiet one on the other side of it: a run that goes green having published
    nothing new, which leaves no red mark anywhere and no gap in the day list
    either (the commit still happens, the content is yesterday's). Nothing in
    the pipeline noticed that shape before, because `generatedAt` moves on every
    run and therefore every rebuild LOOKS like a change.

    `cur` / `prev` are raw snapshot bytes; both default to the real ones (working
    tree vs HEAD). They are parameters so the fixtures can exercise the actual
    decision on synthetic catalogs instead of grepping the source and hoping.
    """
    lines = []
    if cur is None:
        cur_path = REPO / CATALOG_REL
        if not cur_path.exists():
            return False, [f"catalog is missing from the working tree ({CATALOG_REL})."]
        cur = cur_path.read_bytes()
    if prev is None:
        prev = git_bytes("show", f"HEAD:{CATALOG_REL}")
    if not prev:
        return True, ["no committed catalog to compare against (first run) -- allowed."]

    cur_n, _ = head_stats(cur)
    prev_n, _ = head_stats(prev)
    lines.append(f"committed {prev_n} products -> built {cur_n} products")

    problems = []
    if cur == prev:
        problems.append(
            "the built catalog is BYTE-IDENTICAL to the committed one. Even the "
            "generatedAt clock did not move, so build_catalog.py did not write.")
    elif payload_digest(cur) == payload_digest(prev):
        problems.append(
            "the built catalog is identical to the committed one except for its "
            "generatedAt timestamp -- the scrape produced nothing new. Across all "
            "42 archived days this has never once happened legitimately (FX alone "
            "moves prices daily), so treat it as a stale or cached scrape.")
    if cur_n and prev_n:
        move = (cur_n - prev_n) / prev_n
        lines.append(f"day-over-day move {move * 100:+.1f}% "
                     f"(limits -{max_drop * 100:.0f}% / +{max_rise * 100:.0f}%)")
        if move < -max_drop:
            problems.append(
                f"product count collapsed {abs(move) * 100:.1f}% "
                f"({prev_n} -> {cur_n}), past the {max_drop * 100:.0f}% floor.")
        if move > max_rise:
            problems.append(
                f"product count inflated {move * 100:.1f}% ({prev_n} -> {cur_n}), "
                f"past the {max_rise * 100:.0f}% ceiling. Inflation looks like "
                "success, so nothing else in the pipeline checks it: suspect "
                "duplicate emission or a junk filter that stopped firing.")
    elif cur_n is None:
        problems.append("could not read a product count out of the built catalog.")

    if problems and override:
        lines.append("OVERRIDE SET -- the following would have failed the run:")
        lines.extend("  " + p for p in problems)
        return True, lines
    return (not problems), lines + problems


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Gap detection, freshness and the published integrity record "
                    "for Loupe's daily catalog archive.")
    ap.add_argument("--json", action="store_true", help="print the record as JSON, write nothing")
    ap.add_argument("--headline", action="store_true",
                    help="print only the one-line summary (for run summaries, commit messages, emails)")
    ap.add_argument("--write", action="store_true",
                    help="write ARCHIVE_STATUS.md and loupe-feed/archive_status.json")
    ap.add_argument("--with-counts", action="store_true",
                    help="include per-day product counts (reads every snapshot)")
    ap.add_argument("--check-age", type=float, metavar="HOURS", nargs="?",
                    const=STALE_HOURS, default=None,
                    help=f"exit 1 if the newest snapshot is older than HOURS (default {STALE_HOURS})")
    ap.add_argument("--heartbeat", action="store_true",
                    help="exit 1 unless the working-tree catalog is new and plausible vs HEAD")
    ap.add_argument("--allow-shallow", action="store_true",
                    help="report from a truncated clone anyway (stamps partialHistory)")
    ap.add_argument("--quiet", action="store_true", help="suppress the human report")
    args = ap.parse_args()

    # --heartbeat compares the working tree against HEAD only. It deliberately
    # needs no history, so it stays valid on the shallow checkout the build uses
    # if fetch-depth is ever reverted -- a guard that is easy to keep alive.
    if args.heartbeat:
        import os
        override = bool(os.environ.get("ARCHIVE_HEARTBEAT_OVERRIDE", "").strip())
        ok, lines = heartbeat(override=override)
        print("HEARTBEAT: " + ("ok" if ok else "FAILED"))
        for line in lines:
            print("  " + line)
        if not ok:
            print("\nThe build ran but did not produce a usable new catalog, so committing\n"
                  "it would add a day to the archive that carries no new observation.\n"
                  "Failing the run instead. To publish anyway from a MANUAL run, set the\n"
                  "workflow's `override_heartbeat` input (cron can never set it).",
                  file=sys.stderr)
            raise SystemExit(1)
        return

    st = collect(allow_shallow=args.allow_shallow, with_counts=args.with_counts)

    if args.headline:
        print(headline(st))
    elif args.json:
        print(json.dumps(st, indent=2))
    elif not args.quiet:
        report(st)

    if args.write:
        # newline="\n" explicitly: these two files are regenerated and re-diffed
        # every single day, and write_text() on Windows would otherwise emit CRLF
        # while CI emits LF. .gitattributes normalizes it either way -- this just
        # means the file on a Windows disk is byte-identical to the CI one.
        STATUS_MD.write_text(status_markdown(st), encoding="utf-8", newline="\n")
        STATUS_JSON.write_text(json.dumps(st, separators=(",", ":")) + "\n",
                               encoding="utf-8", newline="\n")
        print(f"wrote {STATUS_MD.name} and {STATUS_JSON.name}")

    if args.check_age is not None:
        limit = args.check_age
        # hours_since(), not the rounded newestSnapshotAgeHours -- see its docstring.
        exact = hours_since(st.get("newestSnapshotEpoch"))
        age = st.get("newestSnapshotAgeHours")
        if exact is None:
            print("\nWATCHDOG FAILED: no catalog commit found at all.", file=sys.stderr)
            raise SystemExit(1)
        if exact > limit:
            print(f"\nWATCHDOG FAILED: newest catalog snapshot is {age:.1f}h old "
                  f"(limit {limit:.0f}h).\n"
                  f"  last snapshot : {st['last']}\n"
                  f"  archive now   : {headline(st)}\n"
                  "\n"
                  "  The daily refresh has stopped producing. Every hour this stays\n"
                  "  broken is an observation of 209 storefronts that cannot be taken\n"
                  "  again. Check the most recent run of 'Refresh Loupe catalog' --\n"
                  "  the last time this happened (2026-07-25) it was the fixture gate\n"
                  "  failing on an un-updated test_junk_filter.py, and it cost 4 days.",
                  file=sys.stderr)
            raise SystemExit(1)
        print(f"\nWATCHDOG OK: newest snapshot {age:.1f}h old (limit {limit:.0f}h).")


if __name__ == "__main__":
    main()
