#!/usr/bin/env python3
"""Price-history fixtures — the honesty guarantees, not the plumbing.

This file exists because the output is shopper-facing and brand-facing. A wrong
"never discounted" badge is worse than no badge: it invites a brand to point at
their own sale and ask what else we got wrong.

The one real incident so far: on the first run, six brands showed 100% of their
catalog "discounted" — Rat and Boa, Attega, Susamusa, Martine Rose, Musier Paris
and C'est Nous. None of them had run a sale. On 2026-07-15 the scrape was pinned
to country=US (commit 10b4c79) and their prices stepped into the new regime over
the following days. Comparing across that boundary manufactured a discount.
"""
import inspect

import build_price_history
from build_price_history import (
    epoch_of,
    in_settle_window,
    history_is_truncated,
    sampling_epoch_of,
    in_sampling_settle_window,
    crosses_sampling_epoch,
    arrival_blackout,
    brand_line_kind,
    compact_records,
    record_kinds,
    tenure_trusted_from,
    PRICE_EPOCHS,
    SAMPLING_EPOCHS,
    EPOCH_SETTLE_DAYS,
    MIN_MEANINGFUL_MOVE,
    MIN_DAYS_FOR_PRICE_CLAIM,
    MIN_DAYS_FOR_SIZE_CLAIM,
    SIZE_RUN_MIN,
    SIZE_LEFT_MAX,
    MIN_DROP_PCT,
    MIN_RANGE_PCT,
    MIN_TENURE_DAYS,
    NEW_ARRIVAL_DAYS,
    BRAND_MIN_TRACKED,
    BRAND_RARELY_PCT,
    BRAND_OFTEN_PCT,
    MAX_STALE_DAYS,
    RECORDS_BUDGET_KB,
)

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# ── Epoch boundaries ─────────────────────────────────────────────────────────
check("a day before the pin is epoch 0", epoch_of("2026-07-14") == 0)
check("the pin day itself is epoch 1", epoch_of("2026-07-15") == 1)
check("a later day is epoch 1", epoch_of("2026-08-01") == 1)
check("epochs never go backwards",
      all(epoch_of("2026-07-14") <= epoch_of(d) for d in ("2026-07-15", "2026-09-01")))

# ── The settle window is what actually prevents the false discounts ──────────
check("the pin day is inside the settle window", in_settle_window("2026-07-15"))
check("day+1 is still settling", in_settle_window("2026-07-16"))
check("day+2 is still settling", in_settle_window("2026-07-17"))
check("day+3 is comparable again", not in_settle_window("2026-07-18"))
check("a day before the pin is not settling", not in_settle_window("2026-07-14"))
check("a much later day is not settling", not in_settle_window("2026-08-01"))
check("the settle window is exactly EPOCH_SETTLE_DAYS wide",
      sum(in_settle_window(d) for d in
          ("2026-07-15", "2026-07-16", "2026-07-17", "2026-07-18")) == EPOCH_SETTLE_DAYS)

# ── Contract checks that stop a well-meaning edit from re-breaking this ──────
check("there is at least one declared price epoch", len(PRICE_EPOCHS) >= 1)
check("the country=US pin is declared", "2026-07-15" in PRICE_EPOCHS)
check("epochs are sorted", PRICE_EPOCHS == sorted(PRICE_EPOCHS))
check("the settle window is non-trivial", EPOCH_SETTLE_DAYS >= 2)
# A 2% floor means FX rounding and cent-level drift never read as a sale.
check("the meaningful-move floor is a real floor", 0.005 <= MIN_MEANINGFUL_MOVE <= 0.10)

# ── The claim logic itself, on synthetic prices ──────────────────────────────
def claims(prices):
    """Mirror the emitter: (never_discounted, at_lowest) for one epoch's prices."""
    lo, hi, last = min(prices), max(prices), prices[-1]
    never = hi <= lo * (1 + MIN_MEANINGFUL_MOVE)
    lowest = last <= lo * (1 + MIN_MEANINGFUL_MOVE) and not never
    return never, lowest

check("a flat price is 'never discounted'", claims([120, 120, 120]) == (True, False))
check("cent drift is still 'never discounted'", claims([120.0, 120.5, 120.0])[0])
check("a real sale is not 'never discounted'", claims([200, 200, 140])[0] is False)
check("a piece sitting at its low says so", claims([200, 200, 140])[1] is True)
check("a piece that rebounded does NOT say 'lowest'", claims([200, 140, 200])[1] is False)
check("a single observation makes no claim", claims([120]) == (True, False))

# ── The shallow-clone trap (2026-08-01) ──────────────────────────────────────
# A truncated clone produced a well-formed file covering 28 of 42 days and said
# nothing about it. These pin the guard so it cannot be quietly removed again.
check("truncation is detectable at all", isinstance(history_is_truncated(), bool))

_sig = inspect.signature(build_price_history.build).parameters
check("build() can be told to accept a truncated clone", "allow_shallow" in _sig)
check("build() refuses truncated clones by DEFAULT", _sig["allow_shallow"].default is False)

_src = inspect.getsource(build_price_history.build)
check("the guard runs before the git walk (nothing else can detect it later)",
      _src.index("history_is_truncated") < _src.index("daily_snapshots()"))
check("a refusal names the one-line fix", "--unshallow" in _src)
check("CI's fetch-depth is spelled out where someone will hit it",
      "fetch-depth: 0" in _src)
check("a permitted partial run is stamped, not silently emitted",
      "partialHistory" in _src)

# An absent flag must mean 'complete' — so the key may only ever appear as True.
check("partialHistory is never emitted as False (absent == complete)",
      "partialHistory\": False" not in _src and "'partialHistory': False" not in _src)

_rep = inspect.getsource(build_price_history.report)
check("the human summary surfaces a partial window", "partialHistory" in _rep)

# ── The sampling epoch (2026-08-06 cap raise) ────────────────────────────────
# The price epoch guards WHAT WE RECORD about a piece. This one guards WHICH
# PIECES WE LOOK AT, and it exists because until 2026-08-06 the scrape took the
# 60 most recently published items per store: 981 of 2,041 "disappearances" in
# the 2026-07-16 -> 2026-08-01 window were whole-brand rotation, 48%. Raising the
# cap makes thousands of always-for-sale pieces appear on one day, which must
# never read as newness, restock or a drop.
check("a sampling epoch is declared", len(SAMPLING_EPOCHS) >= 1)
check("the cap raise is the declared boundary", "2026-08-06" in SAMPLING_EPOCHS)
check("sampling epochs are sorted", SAMPLING_EPOCHS == sorted(SAMPLING_EPOCHS))
check("the day before the cap raise is sampling epoch 0",
      sampling_epoch_of("2026-08-05") == 0)
check("the cap-raise day is sampling epoch 1", sampling_epoch_of("2026-08-06") == 1)
check("a later day stays in epoch 1", sampling_epoch_of("2026-12-01") == 1)

check("the cap-raise day is settling", in_sampling_settle_window("2026-08-06"))
check("day+1 is still settling", in_sampling_settle_window("2026-08-07"))
check("day+2 is still settling", in_sampling_settle_window("2026-08-08"))
check("day+3 is comparable again", not in_sampling_settle_window("2026-08-09"))
check("a day before the raise is not settling", not in_sampling_settle_window("2026-08-05"))
check("the sampling settle window is exactly EPOCH_SETTLE_DAYS wide",
      sum(in_sampling_settle_window(d) for d in
          ("2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09")) == EPOCH_SETTLE_DAYS)

# The two epoch kinds are deliberately SEPARATE. Folding the cap raise into
# PRICE_EPOCHS would void every price comparison for 8,000 pieces whose prices
# did not move; leaving it out of SAMPLING_EPOCHS would leave arrivals exposed.
check("the cap raise is NOT a price epoch (no price moved)",
      "2026-08-06" not in PRICE_EPOCHS)
check("the country=US pin is NOT a sampling epoch (no population changed)",
      "2026-07-15" not in SAMPLING_EPOCHS)

# A window that spans the boundary cannot be compared at all.
check("a window spanning the cap raise is void",
      crosses_sampling_epoch("2026-08-01", "2026-08-10"))
check("a window entirely before it is fine",
      not crosses_sampling_epoch("2026-07-16", "2026-08-05"))
check("a window entirely after it is fine",
      not crosses_sampling_epoch("2026-08-09", "2026-09-01"))
check("direction does not matter", crosses_sampling_epoch("2026-08-10", "2026-08-01"))

# The blackout is what a consumer that never read the prose actually sees.
_days = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
         "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10"]
check("the blackout covers exactly the settling days",
      arrival_blackout(_days) == [[3, 5]])
check("an archive that never spans a sampling epoch has no blackout",
      arrival_blackout(["2026-07-16", "2026-07-17", "2026-07-18"]) == [])
check("blackout indices are into the day list, not dates",
      all(isinstance(i, int) for r in arrival_blackout(_days) for i in r))

_src = inspect.getsource(build_price_history.build)
check("the emitted file declares its sampling epochs", "samplingEpochs" in _src)
check("the emitted file carries the arrival blackout", "arrivalBlackout" in _src)
_rep = inspect.getsource(build_price_history.report)
check("the human summary shouts about a sampling boundary", "arrivalBlackout" in _rep)
check("the summary says the boundary is not an arrival", "NOT an arrival" in _rep)

# The docstring is load-bearing here: this file's whole method is that a reader
# is told what the number cannot mean before they are given the number.
_doc = build_price_history.__doc__ or ""
check("the rotation incident is written down, with its measurement",
      "981" in _doc and "48%" in _doc)
check("the docstring names what survives rotation", "available" in _doc)

# ── price_records.json — the file the PHONE reads (2026-09-05) ───────────────
# Everything above protects the archive. This section protects the SENTENCE, and
# the standard is higher: price_history.json is read by us, price_records.json is
# read out loud to a shopper on a product card. A wrong line here is not a bad
# number in a report, it is Loupe telling someone a lie about a $180 dress.
#
# record_kinds() is the mirror of describeRecord() in the app
# (src/utils/priceRecordCopy.ts). Both must gate on the same thresholds, in the
# same order, with the same epoch guards — so these cases are deliberately the
# same cases as __tests__/priceRecordCopy.test.ts.

import datetime as _dt

W_START, W_END, TODAY = "2026-06-17", "2026-09-04", "2026-09-05"

# The snapshot list, WITH THE REAL GAP IN IT. 2026-07-25..28 are missing from the
# live archive (the four days the junk-filter fixture gate cost in July 2026), so
# the fixture carries them missing too: 76 snapshots across an 80-day span. Any
# code that computes a date as windowStart + index instead of looking it up here
# is four days early for every day after July, and these fixtures are the only
# thing that can tell the difference.
_GAP = {"2026-07-25", "2026-07-26", "2026-07-27", "2026-07-28"}
DAY_DATES = []
_cur = _dt.date.fromisoformat(W_START)
while _cur <= _dt.date.fromisoformat(W_END):
    if _cur.isoformat() not in _GAP:
        DAY_DATES.append(_cur.isoformat())
    _cur += _dt.timedelta(days=1)

CTX = {
    "windowStart": W_START,
    "windowEnd": W_END,
    "today": TODAY,
    "dayDates": DAY_DATES,
    "tenureTrustedFrom": tenure_trusted_from(W_START, W_END),
}


def _idx(day):
    """The INDEX of a real date in the snapshot list — never an offset."""
    return DAY_DATES.index(day)


def row(f=0, n=30, lo=200.0, hi=200.0, p=200.0, q=0.0, c=-1, sn=0, sx=0):
    return [f, n, lo, hi, p, q, c, sn, sx]


def kinds(**kw):
    return record_kinds(row(**kw), CTX)


# The window this archive actually has, and the day tenure starts meaning
# something in it. 2026-08-06 raised the scrape cap; +3 settle days.
check("tenure is only trusted after the cap raise has settled",
      tenure_trusted_from(W_START, W_END) == "2026-08-09")
check("an archive that begins after the raise trusts its own start",
      tenure_trusted_from("2026-08-20", "2026-09-04") == "2026-08-20")
check("a sampling epoch in the FUTURE cannot move the boundary",
      tenure_trusted_from("2026-06-17", "2026-07-01") == "2026-06-17")

# ── A day number is an INDEX, not a date offset ──────────────────────────────
# The archive is a list of snapshots and the list has holes. Until 2026-09-05 the
# header called these values "offsets from windowStart", and computing a date
# that way is four days early for everything after July: it would have shipped
# "Down 22% since Aug 23" for a markdown made on Aug 27, and told a shopper a
# piece had been on the shelf 26 days when it had been there 22.
check("the fixture has the archive's real gap in it", len(DAY_DATES) == 76)
check("the calendar span is longer than the snapshot list",
      (_dt.date.fromisoformat(W_END) - _dt.date.fromisoformat(W_START)).days + 1 == 80)
check("a day index resolves by LOOKUP to its real date",
      build_price_history.day_at(CTX, _idx("2026-08-12")) == "2026-08-12")
check("...which is NOT what windowStart + index gives after the gap",
      build_price_history._plus(W_START, _idx("2026-08-12")) == "2026-08-08")
check("an index past the end of the archive resolves to nothing",
      build_price_history.day_at(CTX, len(DAY_DATES)) is None)
check("a negative index resolves to nothing", build_price_history.day_at(CTX, -1) is None)
check("without the map, nothing can be dated",
      build_price_history.day_at({"dayDates": []}, 3) is None)
_no_map = dict(CTX, dayDates=[])
check("without the map, no dated line is produced at all",
      record_kinds(row(f=_idx("2026-08-10"), n=26, lo=156, hi=200, p=156,
                       q=200, c=_idx("2026-08-12")), _no_map) == ["lowest"])

# ── Silence is the default ───────────────────────────────────────────────────
check("a flat, long-watched piece says NOTHING", kinds() == [])
check("a one-day-old piece with no price history says nothing", kinds(n=1) == [])

# ── "Lowest price we've seen" ────────────────────────────────────────────────
check("a piece sitting at its low, watched long enough, says so",
      "lowest" in kinds(n=30, lo=140, hi=200, p=140))
check("the same piece watched for 13 days says nothing",
      kinds(n=MIN_DAYS_FOR_PRICE_CLAIM - 1, lo=140, hi=200, p=140) == [])
check("a piece that never moved is not 'at its lowest'",
      "lowest" not in kinds(n=30, lo=200, hi=200, p=200))
check("cent-level drift is not a discount",
      kinds(n=30, lo=200, hi=201, p=200) == [])

# FX JITTER IS NOT A SALE. 49 brands here are converted from another currency
# every day, so a 2-3% band is the exchange rate moving, not a markdown.
# Measured 2026-09-05 before MIN_RANGE_PCT existed: Alighieri's Poet's Pencil
# Necklace (537 / 550 / now 546, watched 67 days) said "Lowest price we've seen",
# and so did three others. Same principle as PRICE_EPOCHS, one level up.
check("a 2.4% FX band never earns 'lowest price we've seen'",
      kinds(n=67, lo=537, hi=550, p=546) == [])
check("a 2.5% band on a cheap piece does not either",
      kinds(n=62, lo=79, hi=81, p=80) == [])
check("a real 20% markdown still does",
      "lowest" in kinds(n=30, lo=250, hi=300, p=250))
check("the range gate is stricter than the move floor",
      MIN_RANGE_PCT / 100.0 > MIN_MEANINGFUL_MOVE)

# ── "Down 22% since Aug 12" ──────────────────────────────────────────────────
_drop = row(n=30, lo=156, hi=200, p=156, q=200, c=_idx("2026-08-12"))
check("a real markdown is reported", "drop" in record_kinds(_drop, CTX))
check("a markdown at its low also reads as the low",
      "lowest" in record_kinds(_drop, CTX))
check("a 3% move is noise, not a markdown",
      "drop" not in kinds(n=30, lo=194, hi=200, p=194, q=200, c=_idx("2026-08-12")))
check("a move whose day index is past the end of the archive is never reported",
      "drop" not in kinds(n=30, lo=156, hi=200, p=156, q=200, c=len(DAY_DATES) + 5))
check("a move dated in the FUTURE is never reported",
      "drop" not in record_kinds(
          row(n=30, lo=156, hi=200, p=156, q=200, c=len(DAY_DATES)),
          dict(CTX, dayDates=[*DAY_DATES, "2026-09-30"])))
check("a move with no recorded previous price is never reported",
      "drop" not in kinds(n=30, lo=156, hi=200, p=156, q=0, c=_idx("2026-08-12")))
check("a move with no recorded date is never reported",
      "drop" not in kinds(n=30, lo=156, hi=200, p=156, q=200, c=-1))

# ── "Back at full price" ─────────────────────────────────────────────────────
check("a piece whose sale ended says so",
      "rebound" in kinds(n=30, lo=156, hi=200, p=200, q=156, c=_idx("2026-08-20")))
check("'back at full price' and 'lowest' can never both be true",
      not ({"lowest", "rebound"} <= set(kinds(n=30, lo=156, hi=200, p=200,
                                              q=156, c=_idx("2026-08-20")))))
check("a piece that never went on sale is not 'back at full price'",
      "rebound" not in kinds(n=30, lo=200, hi=200, p=200, q=0))

# ── "Only 2 sizes left" ──────────────────────────────────────────────────────
check("a shrinking size run is reported", "sizes" in kinds(n=30, sn=2, sx=6))
check("a one-size piece says nothing about sizes", "sizes" not in kinds(n=30, sn=0, sx=0))
check("a sold-out piece says nothing about sizes (sizesNow 0)",
      "sizes" not in kinds(n=30, sn=0, sx=6))
check("a piece that only ever offered 3 sizes says nothing",
      "sizes" not in kinds(n=30, sn=1, sx=SIZE_RUN_MIN - 1))
check("3 of 6 left is not 'only'",
      "sizes" not in kinds(n=30, sn=SIZE_LEFT_MAX + 1, sx=6))
check("5 of 6 left is not a shrinking run",
      "sizes" not in kinds(n=30, sn=5, sx=6))
check("six days of size history is not enough",
      "sizes" not in kinds(n=MIN_DAYS_FOR_SIZE_CLAIM - 1, sn=2, sx=6))

# ── Tenure, and the sampling epoch that voids it ─────────────────────────────
check("a piece first seen after the cap raise settled reports its tenure",
      "tenure" in kinds(f=_idx("2026-08-10"), n=26))
check("a piece first seen four days ago is new",
      "new" in kinds(f=_idx("2026-09-01"), n=4))
check("'new' and 'tenure' are never both true",
      len({"new", "tenure"} & set(kinds(f=_idx("2026-09-01"), n=4))) == 1)
check("a piece present on the archive's FIRST day makes no tenure claim",
      kinds(f=0, n=80) == [])
check("a piece first seen BEFORE the cap raise makes no tenure claim",
      kinds(f=_idx("2026-07-20"), n=45) == [])
check("a piece first seen INSIDE the settle window makes no tenure claim",
      kinds(f=_idx("2026-08-07"), n=29) == [])
check("a 10-day-old piece is neither new nor a tenure claim",
      kinds(f=_idx("2026-08-26"), n=10) == [])
check("a first-seen index past the end of the archive is never a tenure claim",
      kinds(f=len(DAY_DATES) + 5, n=5) == [])
check("a first-seen date in the future is never a tenure claim",
      record_kinds(row(f=len(DAY_DATES), n=5),
                   dict(CTX, dayDates=[*DAY_DATES, "2026-09-30"])) == [])

# A payload that stopped updating must stop making claims that are measured
# against TODAY. Price claims are measured against the piece's own history and
# survive; anything dated does not.
_stale = dict(CTX, today="2026-09-20")
check("a stale payload makes no tenure claim",
      record_kinds(row(f=_idx("2026-08-10"), n=26), _stale) == [])
check("a stale payload still reports a price it measured",
      "lowest" in record_kinds(row(n=30, lo=140, hi=200, p=140), _stale))
check("MAX_STALE_DAYS is a real freshness bound", 1 <= MAX_STALE_DAYS <= 7)

# ── Garbage in, silence out ──────────────────────────────────────────────────
check("a NaN price says nothing",
      record_kinds(row(n=30, lo=140, hi=200, p=float("nan")), CTX) == [])
check("an infinite price says nothing",
      record_kinds(row(n=30, lo=140, hi=200, p=float("inf")), CTX) == [])
check("a NaN anywhere in the row says nothing",
      record_kinds(row(n=30, lo=140, hi=200, p=140, sx=float("nan")), CTX) == [])
check("a zero price says nothing", record_kinds(row(lo=0, hi=0, p=0), CTX) == [])
check("a negative price says nothing", record_kinds(row(lo=-5, hi=-5, p=-5), CTX) == [])
check("a max below the min is corrupt and says nothing",
      record_kinds(row(n=30, lo=200, hi=140, p=140), CTX) == [])
check("a zero-day row says nothing", record_kinds(row(n=0, lo=140, hi=200, p=140), CTX) == [])

# ── Which line reaches the card ──────────────────────────────────────────────
# out[0] is what the swipe card shows, so the ORDER is a product decision, not
# an artefact of the order the checks are written in.
check("the price answer leads on the card",
      kinds(n=30, lo=140, hi=200, p=140, sn=2, sx=6)[0] == "lowest")
check("'back at full price' never takes the card from another true line",
      kinds(n=30, lo=156, hi=200, p=200, q=156, c=_idx("2026-08-20"),
            sn=2, sx=6)[0] == "sizes")
check("the card priority covers every line kind we emit",
      set(build_price_history.CARD_PRIORITY) ==
      {"lowest", "drop", "rebound", "sizes", "new", "tenure"})
check("the card priority is the same list the report labels",
      set(build_price_history.CARD_PRIORITY) == set(build_price_history.RECORD_LABELS))

# ── The brand sentence ───────────────────────────────────────────────────────
check("a disciplined brand earns the full-price line",
      brand_line_kind([40, 2, 30]) == "brand_rarely")
check("a promotional brand earns the discount line",
      brand_line_kind([40, 16, 30]) == "brand_often")
check("a brand in between earns nothing", brand_line_kind([40, 8, 30]) == "")
check("19 tracked pieces is not a percentage",
      brand_line_kind([BRAND_MIN_TRACKED - 1, 0, 30]) == "")
check("a missing brand row says nothing", brand_line_kind(None) == "")
check("the brand thresholds cannot overlap", BRAND_RARELY_PCT < BRAND_OFTEN_PCT)
check("the brand floor is a real sample", BRAND_MIN_TRACKED >= 20)

# ── compact_records(): what actually ships ───────────────────────────────────
_HIST = {
    "generatedAt": "2026-09-05T08:10:00Z",
    "windowStart": W_START,
    "windowEnd": W_END,
    "days": len(DAY_DATES),
    "dayDates": DAY_DATES,
    "arrivalBlackout": [[50, 52]],
    "products": {
        "says-something": [0, 30, 140.0, 200.0, 140.0, 40],
        "says-nothing": [0, 30, 200.0, 200.0, 200.0, 0],
        "gone-from-the-shelf": [0, 30, 140.0, 200.0, 140.0, 40],
    },
    "brands": {
        "Loud Brand": {"tracked": 40, "everDiscounted": 16, "medianHold": 30},
        "Quiet Brand": {"tracked": 40, "everDiscounted": 1, "medianHold": 30},
        "Too Small": {"tracked": 5, "everDiscounted": 0, "medianHold": 30},
        "Left The Catalog": {"tracked": 40, "everDiscounted": 1, "medianHold": 30},
    },
}
_DETAIL = {
    "says-something": {"prevPrice": 200.0, "lastChange": _idx("2026-08-12"),
                       "sizesNow": 2, "sizesMax": 6, "onShelfNow": True,
                       "brand": "Quiet Brand"},
    "says-nothing": {"prevPrice": 0.0, "lastChange": -1, "sizesNow": 4,
                     "sizesMax": 4, "onShelfNow": True, "brand": "Loud Brand"},
    "gone-from-the-shelf": {"prevPrice": 200.0, "lastChange": _idx("2026-08-12"),
                            "sizesNow": 2, "sizesMax": 6, "onShelfNow": False,
                            "brand": "Left The Catalog"},
}
_recs, _kinds = compact_records(_HIST, _DETAIL, today=TODAY)

check("a product with something true to say ships", "says-something" in _recs["records"])
check("a product with nothing to say does NOT ship", "says-nothing" not in _recs["records"])
check("a product that left the shelf does NOT ship",
      "gone-from-the-shelf" not in _recs["records"])
check("every shipped row is exactly the 9 published fields",
      all(len(v) == 9 for v in _recs["records"].values()))
check("the row schema names exactly those 9 fields",
      _recs["schema"]["records"].count(",") == 8)
check("the brand table keeps brands with enough history",
      set(_recs["brands"]) == {"Loud Brand", "Quiet Brand"})
check("a brand below the floor is not shipped", "Too Small" not in _recs["brands"])
check("a brand that left the catalog is not shipped",
      "Left The Catalog" not in _recs["brands"])
check("the payload carries the window it is indexed against",
      _recs["windowStart"] == W_START and _recs["windowEnd"] == W_END)
check("the payload carries the day tenure starts being an arrival",
      _recs["tenureTrustedFrom"] == "2026-08-09")
check("the payload carries the arrival blackout the archive computed",
      _recs["arrivalBlackout"] == [[50, 52]])
check("the payload ALWAYS carries the index -> date map (no map, no dated claim)",
      _recs["dayDates"] == DAY_DATES)
check("the map is the same length as the day count it publishes",
      len(_recs["dayDates"]) == _recs["days"])
check("the payload ships the thresholds it selected on",
      _recs["thresholds"]["minDaysForPriceClaim"] == MIN_DAYS_FOR_PRICE_CLAIM
      and _recs["thresholds"]["minDropPct"] == MIN_DROP_PCT
      and _recs["thresholds"]["brandMinTracked"] == BRAND_MIN_TRACKED
      and _recs["thresholds"]["newArrivalDays"] == NEW_ARRIVAL_DAYS
      and _recs["thresholds"]["minTenureDays"] == MIN_TENURE_DAYS)
check("the payload never carries a price epoch it did not declare",
      _recs["priceEpochs"] == PRICE_EPOCHS and _recs["samplingEpochs"] == SAMPLING_EPOCHS)
check("the line distribution counts what earned a line",
      _kinds["earned"]["lowest"] == 1 and _kinds["card"]["lowest"] == 1)

# ── Contract checks that stop a well-meaning edit from re-breaking this ──────
_bsrc = inspect.getsource(build_price_history.build)
check("build() hands the shopper-facing detail back SEPARATELY, so it can never "
      "be published into price_history.json by accident",
      "return hist, detail" in _bsrc)
check("the size run is read from the catalog's in-stock sizes", "sizes_max" in _bsrc)
check("a sold-out piece records zero sizes now", 'p.get("available") is False' in _bsrc)

_msrc = inspect.getsource(build_price_history.main)
check("the app payload is written", "RECORDS_OUT.write_text" in _msrc)
check("the budget is checked BEFORE the write (an over-budget file must never "
      "reach the tree)",
      _msrc.index("RECORDS_BUDGET_KB") < _msrc.index("RECORDS_OUT.write_text"))
check("the budget is a phone-sized budget", 100 <= RECORDS_BUDGET_KB <= 500)
check("the distribution is printed every run", "records_report" in _msrc)

_doc = build_price_history.__doc__ or ""
check("the compact file's schema is documented in the header",
      "price_records.json" in _doc and "sizesMax" in _doc)
check("the header says a day value is an INDEX, not an offset",
      "dayDates" in _doc and "NOT A DATE OFFSET" in _doc)
check("the header names the days the archive is actually missing",
      "2026-07-25" in _doc)
check("the archive emits the map too, not only the app payload",
      '"dayDates": days' in inspect.getsource(build_price_history.build))
check("the header says why the app file is committed while the analyst file is not",
      "gitignored" in _doc.lower())

# The lines must never claim to be anything other than a measurement.
_all_src = inspect.getsource(build_price_history)
for _banned in ("recommend", "personalised", "personalized", "algorithm"):
    check(f"the record never calls itself '{_banned}'",
          _banned not in _all_src.lower())

if failures:
    print("FAIL — %d price-history guarantees broken:" % len(failures))
    for f in failures:
        print("   " + f)
    raise SystemExit(1)
print("OK — price history: all honesty guarantees hold")
