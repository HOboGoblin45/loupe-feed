#!/usr/bin/env python3
"""Archive-integrity fixtures -- the continuity guarantees, not the plumbing.

WHAT WENT WRONG (2026-07-25 -> 2026-07-28)

On 2026-07-24, commit 0732108 added a $5 hard price floor to build_catalog.py's
is_junk() and left test_junk_filter.py expecting the old behaviour. The daily
workflow runs those fixtures BEFORE the build, so from the next morning every
scheduled run died at the fixture step and published nothing. Commit 4a85662
fixed the fixture on 2026-07-29. Four days -- 2026-07-25 through 2026-07-28 --
are permanently missing from a dataset whose only real property is that it is
unbroken and long. They cannot be re-scraped; those storefronts have moved on.

The failure was not silent. The workflow went red, and GitHub emailed the failure
four mornings in a row. What was missing was anything that reported the ABSENCE:
no status file, no independent check, nowhere to look that would have said "the
archive is three days behind". Noticing required already suspecting.

WHAT THESE FIXTURES PIN

  * the arithmetic, on synthetic day lists -- a gap detector that miscounts is
    worse than none, because the number it prints will be quoted at people;
  * the boundary honesty, which is the same trap build_price_history.py hit on
    2026-08-01: on a shallow clone every one of these numbers is a LOWER BOUND,
    and a truncated clone produces a beautifully formatted, entirely wrong
    integrity record. Refusal by default is a guarantee, not a preference;
  * the heartbeat's actual decision, run over synthetic catalogs, including the
    one case a naive implementation always gets wrong -- generatedAt is a wall
    clock, so two identical catalogs are never identical BYTES;
  * that the watchdog is still wired up and still independent. Every check above
    is worthless if the workflow that runs it has been quietly changed, and
    "quietly changed" is the entire genre of bug here.

A NOTE ON THIS FILE BEING A GATE

This runs in refresh-catalog.yml's pre-build fixture step -- the same step whose
failure cost four days. That is deliberate and it is the correct trade. A gate
that can block the build is exactly as dangerous as its blast radius, and the
blast radius is now bounded by archive-watchdog.yml, which is independent of this
workflow and shouts within ~15 hours. Gate plus watchdog. Never gate alone, and
never watchdog alone.
"""
import inspect
import re

import archive_integrity
from archive_integrity import (
    MAX_DAILY_DROP,
    MAX_DAILY_RISE,
    STALE_HOURS,
    analyze,
    headline,
    heartbeat,
    hours_since,
    missing_days,
    payload_digest,
    runs_of,
    status_markdown,
)
from build_price_history import REPO, daily_snapshots, history_is_truncated

failures = []
notes = []


def check(label, cond):
    if not cond:
        failures.append(label)


def wf(name):
    p = REPO / ".github" / "workflows" / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


# -- Runs and gaps, on synthetic day lists ------------------------------------
UNBROKEN = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
GAPPED = ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]

check("an unbroken week has no missing days", missing_days(UNBROKEN) == [])
check("a two-day hole is reported as both days",
      missing_days(GAPPED) == ["2026-01-03", "2026-01-04"])
check("nothing is 'missing' outside the observed window",
      all("2026-01-01" < d < "2026-01-07" for d in missing_days(GAPPED)))
check("a single day has no gaps", missing_days(["2026-01-01"]) == [])
check("an empty archive has no gaps", missing_days([]) == [])
check("a month boundary is not mistaken for a gap",
      missing_days(["2026-01-31", "2026-02-01"]) == [])
check("a leap day is a real day, not a gap",
      missing_days(["2028-02-28", "2028-02-29", "2028-03-01"]) == [])

check("an unbroken archive is one run", runs_of(UNBROKEN) == [("2026-01-01", "2026-01-04", 4)])
check("a hole splits the archive into two runs", len(runs_of(GAPPED)) == 2)
check("run lengths are inclusive of both ends",
      runs_of(GAPPED) == [("2026-01-01", "2026-01-02", 2), ("2026-01-05", "2026-01-07", 3)])

# -- analyze(): the numbers that end up in front of an acquirer ---------------
A = analyze(GAPPED, today="2026-01-07")
check("observedDays counts snapshots, not calendar days", A["observedDays"] == 5)
check("spanDays counts calendar days, not snapshots", A["spanDays"] == 7)
check("coverage is snapshots over span", A["coveragePct"] == round(100 * 5 / 7, 2))
check("coverage under 100% means there IS a hole", A["coveragePct"] < 100 and A["gapCount"] == 2)
check("the longest run is the longest one, not the newest",
      A["longestRun"]["days"] == 3 and A["longestRun"]["start"] == "2026-01-05")
check("the current streak is the run ending at the newest day", A["currentStreak"] == 3)
check("gaps are collapsed into ranges", A["gapRanges"] == [("2026-01-03", "2026-01-04")])
check("a gapped archive is not clean", A["clean"] is False)

B = analyze(UNBROKEN, today="2026-01-04")
check("an unbroken archive is clean", B["clean"] is True)
check("an unbroken archive is 100% covered", B["coveragePct"] == 100.0)
check("an unbroken archive is one long run", B["longestRun"]["days"] == 4)
check("daysSinceLastSnapshot is 0 when today is covered", B["daysSinceLastSnapshot"] == 0)
check("daysSinceLastSnapshot counts forward from the last day",
      analyze(UNBROKEN, today="2026-01-09")["daysSinceLastSnapshot"] == 5)
check("an empty archive reports nothing rather than crashing",
      analyze([])["observedDays"] == 0 and analyze([])["clean"] is False)

# -- The headline is the artifact people will actually read -------------------
H = headline({**B, "brands": 209, "products": 8197, "generatedAt": ""})
check("the headline says how many days", "4 days" in H)
check("the headline says how many brands", "209 brands" in H)
check("the headline names the longest run", "longest run 4" in H)
check("a clean archive says 'gaps: none' in as many words", "gaps: none" in H)
G = headline({**A, "brands": 209, "products": 8197, "generatedAt": ""})
check("a gapped archive states the gap in the headline",
      "gaps: 2" in G and "2026-01-03..2026-01-04" in G)
check("the headline is one line", "\n" not in H and "\n" not in G)
# Windows consoles are cp1252. build_price_history.py already prints '->' rather
# than an arrow for this reason; a UnicodeEncodeError in the status generator
# would take out the catalog commit it is amended into.
check("the headline is pure ASCII (it gets printed on cp1252 consoles)",
      H.isascii() and G.isascii())
_MD_GAPPED = status_markdown({**A, "brands": 209, "products": 8197,
                              "generatedAt": "2026-01-07T00:00:00Z"})
check("the rendered status page is pure ASCII", _MD_GAPPED.isascii())
# A cause pinned to the wrong gap is a lie with a citation on it, and this page
# exists precisely so somebody can check rather than take a founder's word.
check("a gap with no recorded cause is NOT blamed on the July incident",
      "0732108" not in _MD_GAPPED)
check("...it says the cause is unrecorded instead",
      "Cause not recorded" in _MD_GAPPED)
_MD_JULY = status_markdown(
    {**analyze(["2026-07-24", "2026-07-29"], today="2026-07-29"),
     "brands": 209, "products": 8197, "generatedAt": "2026-07-29T00:00:00Z"})
check("the July gap IS explained, in the file, next to the gap",
      "0732108" in _MD_JULY and "test_junk_filter.py" in _MD_JULY)
check("the explained gap is not also reported as unexplained",
      "Cause not recorded" not in _MD_JULY)
check("a clean archive renders no cause section at all",
      "Cause not recorded" not in status_markdown(
          {**B, "brands": 209, "products": 8197, "generatedAt": ""}))

# -- Boundary honesty: the 2026-08-01 shallow-clone trap, again ---------------
# A shallow clone yields a SHORTER archive and a first-day that describes the
# clone. build_price_history.py refuses by default; so must this, because an
# integrity record that under-reports is the one error that gets believed.
check("truncation is detectable at all", isinstance(history_is_truncated(), bool))

_sig = inspect.signature(archive_integrity.refuse_if_truncated).parameters
check("the refusal can be waived explicitly", "allow_shallow" in _sig)
_csig = inspect.signature(archive_integrity.collect).parameters
check("collect() refuses truncated clones by DEFAULT", _csig["allow_shallow"].default is False)
_bsig = inspect.signature(archive_integrity.collect).parameters
check("per-day counts are opt-in (they read every blob)", _bsig["with_counts"].default is False)

_src = inspect.getsource(archive_integrity.refuse_if_truncated)
check("the refusal names the one-line fix", "--unshallow" in _src)
check("the refusal spells out CI's fetch-depth", "fetch-depth: 0" in _src)
_csrc = inspect.getsource(archive_integrity.collect)
check("the guard runs before anything is measured",
      _csrc.index("refuse_if_truncated") < _csrc.index("daily_snapshots()"))

_asrc = inspect.getsource(archive_integrity.analyze)
check("a permitted partial run is stamped, not silently emitted", "partialHistory" in _asrc)
check("partialHistory is never emitted as False (absent == complete)",
      '"partialHistory": False' not in _asrc and "'partialHistory': False" not in _asrc)

# On a truncated clone the oldest run was cut off by git-fetch, not measured, so
# it must not be reported as a plain "longest unbroken run".
T = analyze(UNBROKEN, truncated=True, today="2026-01-04")
check("a truncated clone stamps the record partial", T.get("partialHistory") is True)
check("a truncated clone is never 'clean'", T["clean"] is False)
check("a run starting at the clone boundary is flagged as open",
      T["longestRun"].get("boundaryOpen") is True)
check("the open-boundary run reads as a lower bound in the headline",
      "longest run 4+" in headline({**T, "brands": 1, "products": 1, "generatedAt": ""}))
check("a run NOT touching the boundary is not flagged",
      analyze(GAPPED, truncated=True, today="2026-01-07")["longestRun"].get("boundaryOpen")
      is None)
check("a complete clone never flags an open boundary",
      B["longestRun"].get("boundaryOpen") is None)

# -- One definition of "a day" ------------------------------------------------
# If this file counted days differently from the file that builds the price
# history, the integrity record would be certifying a dataset nobody ships.
check("the integrity record reads the SAME day source as the price history",
      archive_integrity.daily_snapshots is daily_snapshots)
check("the shallow guard is the same function, not a second copy",
      archive_integrity.history_is_truncated is history_is_truncated)

# -- payload_digest: 'identical' has to mean identical ------------------------
# generatedAt moves on every single run whether or not the scrape found anything,
# so a raw byte comparison would NEVER fire. This is the whole check.
_A = b'{"generatedAt":"2026-08-01T06:30:50Z","count":2,"products":[{"id":"a","price":10}]}'
_B_SAME = b'{"generatedAt":"2026-08-02T06:31:12Z","count":2,"products":[{"id":"a","price":10}]}'
_C_DIFF = b'{"generatedAt":"2026-08-01T06:30:50Z","count":2,"products":[{"id":"a","price":11}]}'
check("a new timestamp over identical data digests the same",
      payload_digest(_A) == payload_digest(_B_SAME))
check("raw bytes would have missed it (which is why we strip the clock)",
      _A != _B_SAME)
check("a one-cent price move digests differently",
      payload_digest(_A) != payload_digest(_C_DIFF))
check("digests are stable across calls", payload_digest(_A) == payload_digest(_A))
# Snapshots must reach the hash as raw bytes. git_text() decodes with
# errors="replace", which is right for log output and catastrophic here: it
# rewrites every byte it cannot map, so two different catalogs could digest the
# same and the comparison would quietly answer "equal".
check("hashed content is read as BYTES, not replacement-decoded text",
      isinstance(archive_integrity.git_bytes("rev-parse", "HEAD"), bytes))
check("log reading and blob reading are different functions",
      archive_integrity.git_bytes is not archive_integrity.git_text)


# -- The heartbeat's actual decision, on synthetic catalogs -------------------
def cat(n, price=100, stamp="2026-08-01T06:30:50Z"):
    prods = ",".join('{"id":"p%d","price":%s}' % (i, price) for i in range(n))
    return ('{"generatedAt":"%s","count":%d,"products":[%s]}' % (stamp, n, prods)).encode()


ok, _ = heartbeat(cur=cat(1000, 101, "2026-08-02T06:30:50Z"), prev=cat(1000, 100))
check("a normal rebuild passes", ok is True)

ok, why = heartbeat(cur=cat(1000), prev=cat(1000))
check("a byte-identical catalog fails", ok is False)
check("...and says the build never wrote", any("BYTE-IDENTICAL" in w for w in why))

ok, why = heartbeat(cur=cat(1000, 100, "2026-08-02T09:00:00Z"), prev=cat(1000, 100))
check("a catalog whose ONLY change is the clock fails", ok is False)
check("...and names the timestamp as the only difference",
      any("generatedAt" in w for w in why))

ok, _ = heartbeat(cur=cat(900, 101, "2026-08-02T06:30:50Z"), prev=cat(1000, 100))
check("a 10% fall is normal enough to publish", ok is True)
ok, why = heartbeat(cur=cat(700, 101, "2026-08-02T06:30:50Z"), prev=cat(1000, 100))
check("a 30% collapse fails", ok is False)
check("...and says which way it moved", any("collapsed" in w for w in why))
ok, _ = heartbeat(cur=cat(1200, 101, "2026-08-02T06:30:50Z"), prev=cat(1000, 100))
check("a 20% rise is still plausible (a brand batch lands)", ok is True)
ok, why = heartbeat(cur=cat(2000, 101, "2026-08-02T06:30:50Z"), prev=cat(1000, 100))
check("a 100% inflation fails -- nothing else checks this direction", ok is False)
check("...and says inflation, not collapse", any("inflated" in w for w in why))

ok, why = heartbeat(cur=cat(1000), prev=cat(1000), override=True)
check("the override lets a manual run publish anyway", ok is True)
check("...but still prints what it overrode", any("OVERRIDE" in w for w in why))

ok, _ = heartbeat(cur=cat(10, 100, "2026-08-02T06:30:50Z"), prev=b"")
check("a first run with nothing to compare against is allowed", ok is True)

# Calibration, against the archive's own measured behaviour: since 2026-07-01 the
# largest single-day move in either direction is +5.6% / -1.2%. The bands must sit
# clear of that and must not be looser than build_catalog.py's own publish guard.
check("the drop limit matches build_catalog.py's publish guard", MAX_DAILY_DROP == 0.20)
check("the drop limit clears real daily drift", 0.10 <= MAX_DAILY_DROP <= 0.30)
check("the rise limit clears a real brand batch (+5.6% observed)", MAX_DAILY_RISE >= 0.10)
check("the rise limit is still a limit", MAX_DAILY_RISE <= 0.50)
check("staleness allows one missed run plus slack, not four",
      24 < STALE_HOURS <= 48)

# -- Freshness arithmetic -----------------------------------------------------
# Found by the end-to-end replay on 2026-08-01: the CLI compared the age that had
# been ROUNDED to one decimal for the status page. Correct at 36h, silently
# always-green under 0.05h -- which meant the watchdog's failure path could not be
# exercised at any threshold except its default. A guard you cannot test at will
# is a guard you will stop testing.
NOW = 1_785_600_000
check("one hour ago is one hour", hours_since(NOW - 3600, NOW) == 1.0)
check("36 hours ago is exactly at the limit", hours_since(NOW - 36 * 3600, NOW) == 36.0)
check("a sub-minute age is not rounded away to zero",
      0 < hours_since(NOW - 30, NOW) < 0.01)
check("...and it is still strictly greater than a tiny threshold",
      hours_since(NOW - 30, NOW) > 0.0001)
check("no commit means no age, not an age of zero", hours_since(None, NOW) is None)
_msrc = inspect.getsource(archive_integrity.main)
check("the age check compares hours_since(), not the rounded display value",
      "hours_since(" in _msrc)

# -- The wiring. Every check above is inert if nothing runs it ----------------
REFRESH = wf("refresh-catalog.yml")
WATCHDOG = wf("archive-watchdog.yml")
BACKUP = wf("archive-backup.yml")

check("the daily workflow still exists", bool(REFRESH))
check("the daily workflow checks out FULL history (shallow = a fake integrity record)",
      "fetch-depth: 0" in REFRESH)
check("the daily workflow runs the heartbeat", "--heartbeat" in REFRESH)
check("the heartbeat runs BEFORE the commit, not after",
      REFRESH.index("--heartbeat") < REFRESH.index("Commit if changed"))
check("the daily workflow regenerates the status record", "--write" in REFRESH)
check("the status record is committed, not just generated", "ARCHIVE_STATUS.md" in REFRESH)
# The record is written between `git commit` and `git push`, inside a `set -e`
# block. If it were allowed to fail hard it would abort the step with the day
# committed locally and never pushed -- destroying exactly the day it exists to
# account for. Bookkeeping must never be able to cost a snapshot.
check("generating the record cannot abort the push",
      "if python loupe-feed/archive_integrity.py --quiet --write; then" in REFRESH)
check("...and says so, so nobody 'tidies' the conditional away",
      "the snapshot matters more than the paperwork" in REFRESH)
check("the run-summary headline cannot abort the push either",
      "--headline || echo" in REFRESH)
check("these fixtures actually run in CI", "test_archive_integrity.py" in REFRESH)
check("the price-history fixtures run in CI too", "test_price_history.py" in REFRESH)
# The original 'no changes -> exit 0' path is the shape of a green run that
# published nothing. It must not come back.
check("a run that changes nothing no longer reports success",
      'echo "No catalog changes."' not in REFRESH)
check("the heartbeat override is reachable only from a manual run",
      "override_heartbeat" in REFRESH and "inputs.override_heartbeat" in REFRESH)
check("cron cannot set the override (it is a workflow_dispatch input)",
      REFRESH.index("workflow_dispatch") < REFRESH.index("override_heartbeat"))

check("a watchdog workflow exists", bool(WATCHDOG))
check("the watchdog is a SEPARATE file from the build it watches",
      WATCHDOG and WATCHDOG is not REFRESH and "archive-watchdog" not in REFRESH.split("name:")[0])
check("the watchdog runs on its own schedule", "schedule:" in WATCHDOG and "cron:" in WATCHDOG)
check("the watchdog runs more than once a day (so a miss is caught the same evening)",
      "11,23" in WATCHDOG or WATCHDOG.count("- cron:") >= 2)
check("the watchdog asserts freshness", "--check-age" in WATCHDOG)
check("the watchdog checks out full history", "fetch-depth: 0" in WATCHDOG)
check("the watchdog cannot write to the repo it guards", "contents: read" in WATCHDOG)
check("the watchdog can open an issue", "issues: write" in WATCHDOG)
check("the watchdog raises the alarm somewhere persistent", "gh issue" in WATCHDOG)
check("the watchdog does not share the build's concurrency queue",
      "loupe-main-writers" not in WATCHDOG)
# The July failure took out the build. Anything the watchdog EXECUTES that the
# build also executes is something the next incident can take out too -- so the
# watchdog is allowed to *mention* build_catalog.py and test_junk_filter.py (its
# alert body points at them, which is the useful part) but must never run them.
_wd_python = re.findall(r"^\s*python\s+(\S+)", WATCHDOG, flags=re.M)
check("the watchdog runs some python at all", bool(_wd_python))
check("the watchdog runs NOTHING but the integrity script -- no scraper, no "
      "fixture gate, nothing the build can break",
      all(p.endswith("archive_integrity.py") for p in _wd_python))
check("the watchdog needs no secret to do its job (secrets are alerting only)",
      "secrets.SUPABASE" not in WATCHDOG and "secrets.SOVRN" not in WATCHDOG)

check("a backup workflow exists", bool(BACKUP))
check("the backup checks out full history (with blobs)", "fetch-depth: 0" in BACKUP)
check("the backup is published outside the git object store", "gh release" in BACKUP)
check("the backup is ALSO kept as an Actions artifact (a second storage system)",
      "upload-artifact" in BACKUP)
check("the backup is verified before it is published", "--verify" in BACKUP)
check("pruning cannot delete the last backup", "REMAIN" in BACKUP and "KEEP" in BACKUP)

# The generated record has to be in the repo for it to be evidence of anything.
check("the status page is generated into the repo root",
      archive_integrity.STATUS_MD.name == "ARCHIVE_STATUS.md")
check("a machine-readable copy sits beside the feed",
      archive_integrity.STATUS_JSON.name == "archive_status.json")
# Derived, large, and the whole point of it is to live elsewhere.
_ignore = (REPO / ".gitignore").read_text(encoding="utf-8")
check("backup artifacts are not committed back into the repo", "archive_out/" in _ignore)


# -- The real archive ---------------------------------------------------------
# These read this clone's actual history. On a truncated clone they cannot be
# answered, so they are skipped LOUDLY rather than passed quietly -- a fixture
# that quietly passes when it could not run is how a guard dies.
if history_is_truncated():
    notes.append("SKIPPED the live-archive checks: this clone is shallow. "
                 "Run `git fetch --unshallow` and re-run.")
else:
    days = [d for d, _ in daily_snapshots()]
    check("the live archive has at least two snapshots to reason about", len(days) >= 2)
    live = analyze(days)
    check("the live archive starts on 2026-06-17 (the first catalog commit)",
          live["first"] == "2026-06-17")
    # The incident itself, pinned as a permanent fact of the dataset. This is a
    # regression test on the DETECTOR: if a future refactor stops seeing this
    # hole, it would also stop seeing the next one.
    for lost in ("2026-07-25", "2026-07-26", "2026-07-27", "2026-07-28"):
        check(f"the detector still finds the {lost} loss", lost in live["missingDays"])
    check("the July gap is reported as ONE range, not four alarms",
          ("2026-07-25", "2026-07-28") in live["gapRanges"])
    check("the pre-incident run is still the longest measured run",
          live["longestRun"]["days"] >= 38)
    check("the live archive is not falsely reported as clean", live["clean"] is False)
    check("coverage is computed against the calendar, not the snapshot count",
          live["coveragePct"] < 100.0)

if notes:
    print("\n".join("NOTE: " + n for n in notes))
if failures:
    print("FAIL -- %d archive-integrity guarantees broken:" % len(failures))
    for f in failures:
        print("   " + f)
    raise SystemExit(1)
print("OK -- archive integrity: gap detection, boundary honesty, heartbeat and "
      "watchdog wiring all hold")
