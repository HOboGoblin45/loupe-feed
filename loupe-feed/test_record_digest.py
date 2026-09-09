#!/usr/bin/env python3
"""Fixtures for the weekly "your record" push — run by CI BEFORE anything sends.

Plain asserts, no pytest dep, same shape as the other gates in this directory.

WHAT THIS FILE IS FOR. record_digest.py is the only code in this repo that
composes a sentence and puts it on a stranger's lock screen. Every gate it has
exists because of something that has already gone wrong here — an FX correction
one afternoon from being pushed as a 73% sale, four days of archive lost to a red
fixture gate, a day index read as a date offset — and a guard nobody tests is a
guard that will be deleted by whoever finds it inconvenient.

So each block below states the claim it protects, and record-digest.yml refuses to
send if any of them fails. A missed digest is recoverable; a false one is not.
"""
import datetime as dt
import math

import record_digest as rd

failures = []
checked = 0


def check(name, cond, detail=""):
    global checked
    checked += 1
    if not cond:
        failures.append(f"  {name}{(': ' + detail) if detail else ''}")


# ── Fixture world ────────────────────────────────────────────────────────────
# 40 consecutive snapshot days ending on 2026-09-09. Consecutive ON PURPOSE:
# the real archive has holes, and the tests that care about that (below) punch
# their own. Everywhere else a dense list keeps the arithmetic readable.
DAYS = [(dt.date(2026, 8, 1) + dt.timedelta(days=i)).isoformat() for i in range(40)]
TODAY = DAYS[-1]                 # 2026-09-09, index 39
YESTERDAY_I = len(DAYS) - 2      # 38 — a move here is confirmed by one later snapshot


def ctx(**over):
    c = {
        "today": TODAY,
        "windowStart": DAYS[0],
        "windowEnd": DAYS[-1],
        "dayDates": list(DAYS),
        "tenureTrustedFrom": "2026-08-09",
        "priceEpochs": [],
        "epochSettleDays": 3,
        "minDropPct": 5,
        "minRangePct": 5,
        "maxStaleDays": 3,
    }
    c.update(over)
    return c


def rec(f=0, n=40, lo=100.0, hi=100.0, p=100.0, q=0.0, c=-1, sn=0, sx=0):
    """[firstDayIdx, daysSeen, minPrice, maxPrice, lastPrice, prevPrice,
    lastChangeDayIdx, sizesNow, sizesMax] — the published row, defaulting to a
    piece that has never moved and says nothing."""
    return [f, n, lo, hi, p, q, c, sn, sx]


def live(price=100.0, sizes=(), brand="Test Brand", name="Silk Thing",
         category="dresses", stale=False):
    return {"price": price, "sizes": list(sizes), "brand": brand, "name": name,
            "category": category, "stale": stale}


# A recent, confirmed 22% markdown that is ALSO the lowest we have seen.
DROP_AND_LOW = rec(lo=78.0, hi=100.0, p=78.0, q=100.0, c=YESTERDAY_I)
# The same markdown, but the piece has been cheaper before, so no "lowest".
DROP_ONLY = rec(lo=50.0, hi=100.0, p=78.0, q=100.0, c=YESTERDAY_I)
# Sitting on its floor after ticking up a dollar: lowest, but not a drop.
LOW_ONLY = rec(lo=70.0, hi=100.0, p=71.0, q=70.0, c=YESTERDAY_I)
# Normally seven sizes, two left.
SIZES = rec(sn=2, sx=7)
# On the shelf 31 days (firstDay 2026-08-09 == tenureTrustedFrom).
TENURE = rec(f=8)

FX = {"Sir the Label": ("2026-07-15", None, 0.66)}
NO_FX = {}


def cand(record, l=None, c=None, fx=None, pid="p1"):
    return rd.candidate(pid, {pid: record}, c or ctx(), {pid: l or live()},
                        NO_FX if fx is None else fx)


# ── 1. The ladder: drop > lowest > sizes > tenure ────────────────────────────
# The order is a product claim, not a tuning knob: a drop on a piece she saved is
# the only line that is both news and an instruction.
check("drop outscores lowest", rd.MOVE_SCORE["drop"] > rd.MOVE_SCORE["lowest"])
check("lowest outscores sizes", rd.MOVE_SCORE["lowest"] > rd.MOVE_SCORE["sizes"])
check("sizes outscores tenure", rd.MOVE_SCORE["sizes"] > rd.MOVE_SCORE["tenure"])

c1 = cand(DROP_AND_LOW, live(price=78.0))
check("a row that earns both leads with the drop", c1 and c1["kind"] == "drop",
      f"got {c1 and c1['kind']}")
check("the drop percent comes off the record", c1 and c1["pct"] == 22,
      f"got {c1 and c1['pct']}")

c2 = cand(LOW_ONLY, live(price=71.0))
check("a floor-sitter that did not fall still leads with lowest",
      c2 and c2["kind"] == "lowest", f"got {c2 and c2['kind']}")

c3 = cand(SIZES, live(sizes=["XS", "S"]))
check("a collapsing size run qualifies", c3 and c3["kind"] == "sizes",
      f"got {c3 and c3['kind']}")

# Tenure is SCORED (it is a real, ranked record) and never leads a push.
check("tenure is on the ladder", "tenure" in rd.MOVE_SCORE)
check("tenure may not lead a push", "tenure" not in rd.LEAD_KINDS)
check("new may not lead a push", "new" not in rd.LEAD_KINDS)
check("rebound may not lead a push", "rebound" not in rd.LEAD_KINDS)
check("a piece whose only record is tenure gets nothing",
      cand(TENURE, live()) is None)

# Ordering across pieces, including the tiebreak. Two identical candidates must
# not swap between two runs over the same data.
ranked = sorted([
    {"kind": "sizes", "score": 200, "pid": "b", "pct": 0, "range_pct": 0, "sizes_left": 2},
    {"kind": "drop", "score": 400, "pid": "c", "pct": 9, "range_pct": 40, "sizes_left": 0},
    {"kind": "drop", "score": 400, "pid": "a", "pct": 40, "range_pct": 40, "sizes_left": 0},
    {"kind": "sizes", "score": 200, "pid": "d", "pct": 0, "range_pct": 0, "sizes_left": 1},
], key=rd.rank_key)
check("kind wins, then magnitude, then id",
      [r["pid"] for r in ranked] == ["a", "c", "d", "b"],
      str([r["pid"] for r in ranked]))
check("fewer sizes left is MORE urgent, not less",
      ranked[2]["sizes_left"] < ranked[3]["sizes_left"])


# ── 2. A move must be recent, and confirmed by a second snapshot ─────────────
# The weekly digest reports MOVES. A standing fact repeated every Thursday is a
# nag, and "dropped this week" has to be true.
old = rec(lo=78.0, hi=100.0, p=78.0, q=100.0, c=10)   # 2026-08-11, 29 days ago
check("a month-old markdown is not this week's news", cand(old, live(price=78.0)) is None)

# …and the other end. On 2026-09-09 EVERY dated move in the live archive resolved
# to that same morning (lastChangeDayIdx was -1 or the last index, nothing else),
# because the 2026-09-05 epoch plus its settle tail left two comparable snapshots.
# A one-scrape move is the weakest evidence this pipeline produces.
unconfirmed = rec(lo=78.0, hi=100.0, p=78.0, q=100.0, c=len(DAYS) - 1)
check("a move seen in only one snapshot is not pushed",
      cand(unconfirmed, live(price=78.0)) is None)
check("…and one more snapshot is enough", cand(DROP_AND_LOW, live(price=78.0)) is not None)
check("the confirmation requirement is a stated constant, not an accident",
      rd.MOVE_MIN_CONFIRM_SNAPSHOTS >= 1)

# A change we cannot DATE is a change we do not announce. dayDates is the only
# thing that turns an index into a date; without it, no arithmetic recovers one.
check("no dayDates -> no dated claim",
      cand(DROP_AND_LOW, live(price=78.0), ctx(dayDates=[])) is None)
check("an out-of-range day index -> no dated claim",
      cand(rec(lo=78.0, hi=100.0, p=78.0, q=100.0, c=999), live(price=78.0)) is None)

# THE HOLE. The archive is missing 2026-07-25..28, so windowStart + index is four
# days early for everything after them. A consumer that did the arithmetic would
# date a 2026-09-08 markdown to 2026-09-04 — and could pass the recency gate on a
# move that is actually older. The lookup must be a lookup.
holed = list(DAYS)
holed[YESTERDAY_I] = "2026-07-20"     # same index, a real (much older) date
check("recency is measured on the LOOKED-UP date, not the index",
      cand(DROP_AND_LOW, live(price=78.0), ctx(dayDates=holed)) is None)

future = list(DAYS)
future[YESTERDAY_I] = "2027-01-01"
check("a change dated in the future is refused",
      cand(DROP_AND_LOW, live(price=78.0), ctx(dayDates=future)) is None)


# ── 3. Price epochs: our own arithmetic is never somebody's markdown ─────────
# 2026-07-15 pinned the scrape to country=US and flipped 49 brands into USD;
# 2026-09-05 re-fetched the whole FX table. Prices moved on both days for reasons
# that have nothing to do with a brand's decision.
on_epoch = ctx(priceEpochs=[DAYS[YESTERDAY_I]])
check("a move ON an epoch day is void",
      cand(DROP_AND_LOW, live(price=78.0), on_epoch) is None)

# …and for the tail, because a methodology change does not land in one clean day.
# Six brands showed 100% of their catalog "discounted" on the first run after the
# country=US pin; they were repricing into the new regime.
settling = ctx(priceEpochs=[DAYS[YESTERDAY_I - 2]], epochSettleDays=3)
check("a move inside the settle tail is void",
      cand(DROP_AND_LOW, live(price=78.0), settling) is None)
check("…and outside it, the same move stands",
      cand(DROP_AND_LOW, live(price=78.0),
           ctx(priceEpochs=[DAYS[YESTERDAY_I - 5]], epochSettleDays=3)) is not None)

# Straddling: the two prices being differenced were produced by different
# arithmetic even though neither endpoint is an epoch day itself.
straddle = ctx(priceEpochs=[DAYS[YESTERDAY_I]], epochSettleDays=0)
check("a move that STRADDLES an epoch is void",
      cand(DROP_AND_LOW, live(price=78.0), straddle) is None)

# An unbounded move — the change is the first day we ever saw the piece — cannot
# be tested against an epoch at all, so it is refused rather than assumed clean.
check("a move with no previous snapshot is refused",
      cand(rec(lo=78.0, hi=100.0, p=78.0, q=100.0, c=0), live(price=78.0)) is None)

# The union: an epoch registered in build_price_history but not yet baked into a
# published price_records.json must still bite. That gap is exactly the window in
# which the mistake gets made.
import build_price_history as bph  # noqa: E402
union = rd.price_epochs({"priceEpochs": ["2026-01-02"]})
check("the file's own epochs are honoured", "2026-01-02" in union)
for e in bph.PRICE_EPOCHS:
    check(f"the repo's epoch {e} is honoured even if the file predates it", e in union)
check("a junk epoch entry is dropped, not crashed on",
      "banana" not in rd.price_epochs({"priceEpochs": ["banana", None, 7]}))


# ── 4. FX corrections: read the published record, never re-derive it ─────────
# 28natelier x0.27229 and Sir the Label x0.66 made prices FALL. 115 saved pieces
# cleared the 10%/$3 alert threshold on the day the correction landed, and every
# holder would have been told "72% off" on a piece nobody marked down.
fx_step = rec(lo=66.0, hi=100.0, p=66.0, q=100.0, c=YESTERDAY_I)
check("a move that IS the published FX factor is not a sale",
      cand(fx_step, live(price=66.0, brand="Sir the Label"), fx=FX) is None)
check("…and the identical move on an uncorrected brand still sends",
      cand(fx_step, live(price=66.0, brand="Ganni"), fx=FX) is not None)

# ISOLATING THE RATIO TEST. fx_suppressed has two arms and the window arm is the
# broader one, so with an OPEN window the assertion above passes even if the
# ratio test is deleted — which a probe proved. Closing the window leaves the
# ratio test as the only thing standing between a currency fix and a lock screen.
SPENT = {"Sir the Label": ("2026-07-15", "2026-07-20", 0.66)}
check("a x0.66 step is refused on the RATIO alone, window long closed",
      cand(fx_step, live(price=66.0, brand="Sir the Label"), fx=SPENT) is None)
check("…and the rounding bound is the published one, not a guess",
      rd.pdp.FX_STEP_ROUNDING_SLACK == 0.5)
# A move NEAR the factor but outside the double-rounding bound cannot be rounding,
# so it is a real move and the sale stands. Measured over all 400 corrected pieces
# the worst residual was $0.870 against a $1.258 bound.
near = rec(lo=62.0, hi=100.0, p=62.0, q=100.0, c=YESTERDAY_I)   # x0.62, not x0.66
check("a move outside the rounding bound is a real markdown",
      cand(near, live(price=62.0, brand="Sir the Label"), fx=SPENT) is not None)

# The blunt half, stated: every correction in the live file has toDay null, so a
# brand with an OPEN window makes no price claim at all. That costs real sales on
# those brands and it is the safe direction; closing the window in
# price_corrections.json is the fix, not an exception carved out here.
real_sale_on_corrected_brand = rec(lo=60.0, hi=100.0, p=60.0, q=100.0, c=YESTERDAY_I)
check("an open FX window suppresses price claims for that brand outright",
      cand(real_sale_on_corrected_brand, live(price=60.0, brand="Sir the Label"), fx=FX) is None)
closed = {"Sir the Label": ("2026-07-15", "2026-07-20", 0.66)}
check("a CLOSED FX window releases later moves",
      cand(real_sale_on_corrected_brand, live(price=60.0, brand="Sir the Label"),
           fx=closed) is not None)
check("the FX table is read from the published record",
      rd.fx_windows({"corrections": [
          {"brand": "X", "fromDay": "2026-07-15", "toDay": None, "factor": 0.5}]})
      == {"X": ("2026-07-15", None, 0.5)})
check("a correction with an unreadable factor is skipped, not crashed on",
      rd.fx_windows({"corrections": [{"brand": "X", "factor": "n/a"},
                                     {"brand": "Y", "factor": -1}]}) == {})

# An FX correction is about PRICE. It says nothing about how many sizes are left,
# so it must not silence a size claim on the same brand.
check("an FX window does not suppress a size claim",
      cand(SIZES, live(sizes=["XS", "S"], brand="Sir the Label"), fx=FX) is not None)


# ── 5. Thresholds ───────────────────────────────────────────────────────────
check("the drop floor is the archive's own 5%", bph.MIN_DROP_PCT == 5)
check("the range floor is the archive's own 5%", bph.MIN_RANGE_PCT == 5)

shrug = rec(lo=97.0, hi=100.0, p=97.0, q=100.0, c=YESTERDAY_I)   # 3% off
check("a 3% move is a shrug, not a push", cand(shrug, live(price=97.0)) is None)

# The dollar floor. Every price in the feed is a dollar-rounded integer, so a
# sub-dollar move cannot exist in the data — but a cheap piece can clear 5%
# without clearing $1. `lo` is kept well below `p` so the row earns ONLY "drop":
# a piece sitting on its floor legitimately earns "lowest" instead, and the row
# would then qualify for a reason that has nothing to do with the last step.
penny = rec(lo=5.0, hi=10.0, p=9.0, q=9.5, c=YESTERDAY_I)
check("a sub-dollar move is refused however good the percentage looks",
      cand(penny, live(price=9.0)) is None)
check("the dollar floor is stated", rd.MIN_DROP_ABS == 1.0)

# …but it must not reach "lowest", which is a claim about a RANGE. LOW_ONLY has
# q - p = -1: applying the drop floor there deleted the whole branch once.
check("the dollar floor does not silence a floor-sitter",
      cand(LOW_ONLY, live(price=71.0)) is not None)

# Size gates, straight off record_kinds: a real run must normally be on offer.
check("a piece that only ever had two sizes says nothing",
      cand(rec(sn=1, sx=2), live(sizes=["S"])) is None)
check("a one-size piece says nothing", cand(rec(sn=0, sx=0), live()) is None)
check("three left out of seven is not 'running low'",
      cand(rec(sn=3, sx=7), live(sizes=["XS", "S", "M"])) is None)
check("a piece watched for three days makes no size claim",
      cand(rec(n=3, sn=2, sx=7), live(sizes=["XS", "S"])) is None)


# ── 6. The archive must still agree with the shop ───────────────────────────
# Every number in the copy is quoted as current. The archive's last snapshot is
# from this morning; if the shop repriced since, the push would land stale.
check("a piece that has left the catalog says nothing",
      rd.candidate("p1", {"p1": DROP_AND_LOW}, ctx(), {}, NO_FX) is None)
check("a grace-carried (frozen) catalog row says nothing",
      cand(DROP_AND_LOW, live(price=78.0, stale=True)) is None)
check("a price that has moved since the snapshot says nothing",
      cand(DROP_AND_LOW, live(price=71.0)) is None)
check("…within the rounding bound it still sends",
      cand(DROP_AND_LOW, live(price=78.5)) is not None)
check("an unreadable live price says nothing",
      cand(DROP_AND_LOW, live(price="79ish")) is None)
check("a size run that has moved since the snapshot says nothing",
      cand(SIZES, live(sizes=["XS", "S", "M"])) is None)
check("size comparison ignores blanks and duplicates the way the feed does",
      cand(SIZES, live(sizes=["XS", " xs ", "S", ""])) is not None)

stale_ctx = ctx(windowEnd="2026-09-01")   # 8 days behind today
check("a stale archive makes no dated claim at all", rd.records_are_stale(stale_ctx))
check("a fresh archive does not trip the staleness guard", not rd.records_are_stale(ctx()))
check("an archive dated in the future is treated as broken",
      rd.records_are_stale(ctx(windowEnd="2027-01-01")))


# ── 7. The copy ─────────────────────────────────────────────────────────────
def body_of(lead, others=()):
    got = rd.compose(lead, list(others), TODAY)
    return (got["title"], got["body"]) if got else (None, None)


t, b = body_of(cand(DROP_ONLY, live(price=78.0)))
check("single drop title", t == "A piece you saved just dropped", repr(t))
check("single drop body",
      b == "Your Test Brand Silk Thing is down 22% since Sep 8.", repr(b))

two_drops = [cand(DROP_ONLY, live(price=78.0), pid="a"),
             cand(DROP_ONLY, live(price=78.0), pid="b")]
t, b = body_of(two_drops[0], two_drops[1:])
check("multi-drop title counts exactly", t == "2 pieces you saved dropped this week", repr(t))
check("multi-drop body names the biggest",
      b == "The biggest: your Test Brand Silk Thing, down 22% since Sep 8.", repr(b))

t, b = body_of(cand(LOW_ONLY, live(price=71.0)))
check("lowest title", t == "A new low on a piece you saved", repr(t))
check("lowest body quotes the record's own price",
      b == "Your Test Brand Silk Thing is at the lowest price we've seen — $71.", repr(b))

t, b = body_of(cand(SIZES, live(sizes=["XS", "S"])))
check("sizes title", t == "Running low on a piece you saved", repr(t))
check("sizes body, plural", b == "Only 2 sizes left in the Test Brand Silk Thing you saved.",
      repr(b))
t, b = body_of(cand(rec(sn=1, sx=7), live(sizes=["XS"])))
check("sizes body, singular", b == "Only 1 size left in the Test Brand Silk Thing you saved.",
      repr(b))

# The tail is a count of OTHER qualifying pieces, and it is exact.
t, b = body_of(cand(SIZES, live(sizes=["XS", "S"])),
               [cand(SIZES, live(sizes=["XS", "S"]), pid=f"x{i}") for i in range(3)])
check("the tail counts the others exactly",
      b.endswith(" +3 more of your saves moved this week."), repr(b))
check("no tail when there is nothing else",
      not body_of(cand(SIZES, live(sizes=["XS", "S"])))[1].endswith("moved this week."))

# An undatable drop prints nothing rather than an undated sentence.
undatable = dict(cand(DROP_ONLY, live(price=78.0)))
undatable["change_day"] = None
check("an undated drop composes nothing", rd.compose(undatable, [], TODAY) is None)
check("no lead composes nothing", rd.compose(None, [], TODAY) is None)

# Labels. Both halves are the shop's own words; the two normalisations do not
# change what the piece is called.
check("brand + name when it fits",
      rd.piece_label(live(brand="Ganni", name="Silk Midi Dress")) == "Ganni Silk Midi Dress")
check("a name that already opens with the brand is not doubled",
      rd.piece_label(live(brand="Oddli", name="Oddli Baseball Tee")) == "Oddli Baseball Tee")
check("a pipe in the shop's own title becomes punctuation",
      rd.piece_label(live(brand="Flore Flore", name="BIBI DRESS LW | Black"))
      == "Flore Flore BIBI DRESS LW, Black")
check("a name too long for a lock screen falls back to a category noun",
      rd.piece_label(live(brand="Christopher Esber",
                          name="Recycled Rib Wrapped Bodice Gown In Deep Pacific",
                          category="dresses")) == "Christopher Esber dress")
check("a category with no honest singular becomes 'piece'",
      rd.piece_label(live(brand="Christopher Esber", name="A" * 60, category="outerwear"))
      == "Christopher Esber piece")
check("an unknown category becomes 'piece'",
      rd.piece_label(live(brand="X", name="A" * 60, category="socks")) == "X piece")
check("a nameless piece still gets a label",
      rd.piece_label(live(brand="Ganni", name="", category="tops")) == "Ganni")

# Banned words. The whole value of these sentences is that they are measurements
# somebody else made and we merely kept; "recommended for you" says the opposite.
check("'algorithm' is refused", not rd.copy_is_clean("our algorithm found this"))
check("'recommend' is refused", not rd.copy_is_clean("Recommended for you"))
check("'personalised' is refused", not rd.copy_is_clean("A personalised pick"))
check("'personalized' is refused", not rd.copy_is_clean("A personalized pick"))
check("the TERM 'AI' is refused", not rd.copy_is_clean("Our AI picked this"))
check("…but the letters inside a word are fine",
      rd.copy_is_clean("Available again in your size, in detail"))
check("non-strings are not 'clean'", not rd.copy_is_clean(None))

# And the real thing: nothing composed from any branch may trip it.
for lead in (cand(DROP_ONLY, live(price=78.0)), cand(LOW_ONLY, live(price=71.0)),
             cand(SIZES, live(sizes=["XS", "S"]))):
    got = rd.compose(lead, [], TODAY)
    check(f"composed {lead['kind']} copy is clean", rd.copy_is_clean(got["title"], got["body"]))
    check(f"composed {lead['kind']} copy names the piece", "Test Brand" in got["body"])

# A brand whose NAME trips the rule must lose the push, not the rule.
dirty = rd.compose(cand(SIZES, live(sizes=["XS", "S"], brand="AI Studio")), [], TODAY)
check("a piece we cannot describe cleanly is dropped", dirty is None)

check("Sep 8 formats as the app formats it", rd.fmt_day("2026-09-08", TODAY) == "Sep 8")
check("a different year carries the year", rd.fmt_day("2025-09-08", TODAY) == "Sep 8, 2025")
check("an unparseable day formats to nothing", rd.fmt_day("not-a-day", TODAY) == "")


# ── 8. The caps ─────────────────────────────────────────────────────────────
NOW = dt.datetime(2026, 9, 9, 16, 23, tzinfo=dt.timezone.utc)


def hours_ago(h):
    return (NOW - dt.timedelta(hours=h)).isoformat()


def run(users, items, records=None, c=None, catalog=None, fx=None):
    records = records if records is not None else {"p1": SIZES}
    catalog = catalog if catalog is not None else {"p1": live(sizes=["XS", "S"])}
    return rd.build_record_digests(users, items, records, c or ctx(), catalog,
                                   NO_FX if fx is None else fx, now=NOW)


ITEMS = [{"user_id": "u1", "product_id": "p1"}]


def one(at):
    return {"u1": {"token": "ExponentPushToken[t]", "at": at}}


msgs, _, skips, _ = run(one(None), ITEMS)
check("a user never pushed is eligible", len(msgs) == 1, str(skips))

msgs, _, skips, _ = run(one(hours_ago(3)), ITEMS)
check("pushed 3h ago -> skipped", len(msgs) == 0 and skips["pushed_within_24h"] == 1, str(skips))

msgs, _, skips, _ = run(one(hours_ago(23.9)), ITEMS)
check("pushed 23.9h ago -> still inside the 24h rule", skips["pushed_within_24h"] == 1, str(skips))

msgs, _, skips, _ = run(one(hours_ago(48)), ITEMS)
check("pushed 2 days ago -> caught by the WEEKLY cap, not the daily one",
      len(msgs) == 0 and skips["pushed_within_7d"] == 1, str(skips))

msgs, _, skips, _ = run(one(hours_ago(24 * 7 - 1)), ITEMS)
check("pushed 6d23h ago -> still inside the week", skips["pushed_within_7d"] == 1, str(skips))

msgs, _, skips, _ = run(one(hours_ago(24 * 7 + 1)), ITEMS)
check("pushed 7d1h ago -> eligible again", len(msgs) == 1, str(skips))

msgs, _, skips, _ = run(one((NOW + dt.timedelta(days=1)).isoformat()), ITEMS)
check("a stamp in the future is not permission to interrupt", len(msgs) == 0, str(skips))

msgs, _, skips, _ = run(one("garbage"), ITEMS)
check("an unreadable stamp is not permission to interrupt", len(msgs) == 0, str(skips))

check("the 24h rule is a stated constant", rd.PUSH_QUIET_HOURS == 24)
check("the weekly cap is a stated constant", rd.PUSH_MIN_GAP_DAYS == 7)

# A run must never produce two messages for one user, however many of her saves
# moved. One piece, one count, one push.
many = [{"user_id": "u1", "product_id": f"p{i}"} for i in range(1, 6)]
msgs, stamps, _, _ = run(one(None), many,
                         records={f"p{i}": SIZES for i in range(1, 6)},
                         catalog={f"p{i}": live(sizes=["XS", "S"]) for i in range(1, 6)})
check("five moves make ONE push", len(msgs) == 1 and len(stamps) == 1, str(len(msgs)))
check("…that counts the other four", "+4 more of your saves moved this week." in msgs[0]["body"],
      msgs[0]["body"])
check("…and carries them all as focus ids", len(msgs[0]["data"]["focus"]) == 5)
check("focus is capped", rd.MAX_FOCUS_IDS <= 12)

# A duplicate save (the same piece in both Dresser and Likes) is one piece.
dupes = [{"user_id": "u1", "product_id": "p1"}, {"user_id": "u1", "product_id": "p1"}]
msgs, _, _, _ = run(one(None), dupes)
check("a piece saved twice is counted once",
      msgs and not msgs[0]["body"].endswith("moved this week."), msgs[0]["body"] if msgs else "")


# ── 9. A quiet week sends nothing ───────────────────────────────────────────
msgs, stamps, skips, _ = run(one(None), ITEMS, records={"p1": rec()})
check("nothing to say -> no push", len(msgs) == 0 and len(stamps) == 0)
check("…and the log says why", skips["no_qualifying_move"] == 1, str(skips))

msgs, _, skips, _ = run(one(None), [])
check("a user with no saves is skipped, and counted", skips["no_saved_items"] == 1, str(skips))

msgs, _, skips, _ = run({}, ITEMS)
check("no users -> no push, no crash", len(msgs) == 0)

msgs, _, skips, _ = run(one(None), ITEMS, c=stale_ctx)
check("a stale archive sends NOTHING, to anyone",
      len(msgs) == 0 and skips["archive_too_stale_to_quote"] == 1, str(skips))

# Every rejection is counted. "Almost nobody qualified" is only useful with a why.
_, _, _, reasons = run(one(None), ITEMS, records={"p1": TENURE},
                       catalog={"p1": live()})
check("a rejected piece records its reason", sum(reasons.values()) >= 1, str(reasons))

# The push points somewhere, and it points at the Alerts screen.
msgs, _, _, _ = run(one(None), ITEMS)
check("the push carries the routing type the app listens for",
      msgs[0]["data"]["type"] == "sales_updates", str(msgs[0]["data"]))
check("…and says which pipeline sent it", msgs[0]["data"]["source"] == "record_digest")
check("…and names the pieces it is about", msgs[0]["data"]["focus"] == ["p1"])


# ── 10. Garbage in, silence out — never a default ───────────────────────────
# A NaN that reaches a comparison makes every one of them false, which happens to
# be safe here. "Happens to be safe" is not a guarantee, and this file's whole job
# is guarantees.
for bad in ([], [1, 2, 3], "not a row", None, {}, [float("nan")] * 9,
            [math.inf] * 9, [True] * 9, [0] * 9, [-1] * 9,
            [0, 40, 100.0, 50.0, 100.0, 0.0, -1, 0, 0]):   # hi < lo
    check(f"garbage row {str(bad)[:28]!r} -> silence",
          rd.candidate("p1", {"p1": bad}, ctx(), {"p1": live()}, NO_FX) is None)

for bad_ctx in (ctx(today="nope"), ctx(dayDates=None), ctx(dayDates="xx"),
                ctx(windowEnd=""), ctx(tenureTrustedFrom=None)):
    try:
        rd.candidate("p1", {"p1": SIZES}, bad_ctx, {"p1": live(sizes=["XS", "S"])}, NO_FX)
        ok = True
    except Exception as e:                                   # noqa: BLE001
        ok = False
        detail = f"{type(e).__name__}: {e}"
    check("a broken header does not raise", ok, locals().get("detail", ""))

for bad_live in ({}, {"price": None}, {"price": 78.0, "sizes": None},
                 {"price": 78.0, "sizes": "XS"}, {"price": 78.0, "brand": None, "name": None}):
    try:
        rd.candidate("p1", {"p1": SIZES}, ctx(), {"p1": bad_live}, NO_FX)
        ok = True
    except Exception as e:                                   # noqa: BLE001
        ok = False
        detail2 = f"{type(e).__name__}: {e}"
    check("a broken catalog row does not raise", ok, locals().get("detail2", ""))

try:
    rd.build_record_digests({"u1": {"token": "t", "at": None}},
                            [{"user_id": None}, {}, {"product_id": ""}],
                            {"p1": SIZES}, ctx(), {"p1": live(sizes=["XS", "S"])}, NO_FX)
    ok = True
except Exception as e:                                       # noqa: BLE001
    ok, detail3 = False, f"{type(e).__name__}: {e}"
check("malformed saved-item rows do not raise", ok, locals().get("detail3", ""))

check("days_between refuses junk", rd.days_between("nope", TODAY) is None)
check("_pushed_within with no stamp is False", rd._pushed_within(None, 24, NOW) is False)


# ── 11. The one thing this job writes ───────────────────────────────────────
# profiles.last_marketing_push_sig belongs to the daily digest's anti-repeat. A
# value this job wrote could never equal the signature price_drop_push.py
# computes, so the daily digest would re-send a digest it had already sent.
src = (rd.HERE / "record_digest.py").read_text(encoding="utf-8")
# A dict KEY, which is the only shape a PostgREST write can take. Prose about the
# column is the point of the rule and must not trip it.
import re as _re  # noqa: E402
writes = _re.findall(r"""["']last_marketing_push_sig["']\s*:""", src)
check("this job never writes the daily digest's signature column",
      writes == [], str(writes))
check("…and it does stamp the timestamp it reads",
      "last_marketing_push_at" in src)


def main():
    if failures:
        print(f"RECORD-DIGEST REGRESSIONS ({len(failures)} of {checked}):")
        print("\n".join(failures))
        raise SystemExit(1)
    print(f"record-digest fixtures: all OK ({checked} checks)")


if __name__ == "__main__":
    main()
