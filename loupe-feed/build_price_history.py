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

Day values are integers, so the file stays small (~8,200 entries at ~40 bytes ≈
350 KB, vs 8.8 MB for one catalog snapshot).

A DAY VALUE IS AN INDEX, NOT A DATE OFFSET (2026-09-05)

This is the one thing in the format that reads like something it is not, and it
was documented wrongly here until 2026-09-05. Every day number is an index into
the LIST OF SNAPSHOT DAYS, and that list has holes: the archive is missing
2026-07-25 to 2026-07-28, the four days lost when the junk-filter fixture gate
went red every morning. Measured today: 76 snapshots across an 80-day span.

So `windowStart + dayIndex` is not the date. For anything after July it is FOUR
DAYS EARLY, which would have shipped "Down 22% since Aug 23" for a markdown that
happened on Aug 27, and "on the shelf 26 days" for a piece that had been there
22. `dayDates` is emitted for exactly this reason: index -> real date, one lookup,
no arithmetic. A consumer without it must make no dated claim at all.

price_history.json is GITIGNORED and stays that way: it is a pure function of the
commits, so committing it back would make the history self-referential and double
the repo's growth for nothing.

WHAT THE APP ACTUALLY DOWNLOADS (price_records.json, 2026-09-05)

The whole file above is the analyst's artefact. The phone needs one sentence per
card, so it gets a second, COMMITTED file — the app cannot read a gitignored one,
and jsDelivr can only serve what is in the tree:

  { "generatedAt", "windowStart", "windowEnd", "days",
    "priceEpochs", "samplingEpochs", "epochSettleDays", "arrivalBlackout",
    "dayDates": ["2026-06-17", …],           # index -> real date. NOT an offset.
    "tenureTrustedFrom",                     # first day firstDay means "arrived"
    "thresholds": { … },                     # the numbers the copy is gated on
    "schema": { "records": …, "brands": … },
    "records": { "<id>": [f, n, lo, hi, p, q, c, sn, sx] },
    "brands":  { "<brand>": [tracked, everDiscounted, medianHold] } }

    f  firstDayIdx   n  daysSeen    lo minPrice (this epoch)   hi maxPrice
    p  lastPrice     q  prevPrice — the price the last meaningful move came FROM
                        (0 = it never moved)
    c  lastChangeDayIdx — the day we first saw p (-1 = it never moved)
    sn sizesNow — in-stock size variants today
    sx sizesMax — the most we have ever seen in stock at once
                  (sizesNow/sizesMax 0 = the store lists no sizes for it)

Three deliberate reductions, in order of how much they save:

  • ONLY PRODUCTS WITH SOMETHING TO SAY. A row is emitted only if it earns at
    least one true line (see record_kinds() — the same predicates, thresholds and
    epoch guards as src/utils/priceRecordCopy.ts, which is the only other place
    they may live). ~8k products in, ~2k rows out.
  • ONLY PRODUCTS STILL ON THE SHELF. A row for a piece that left the catalog can
    never be rendered — the app looks records up by the id of a product it is
    already showing — so it is pure payload.
  • ONLY BRANDS WITH ENOUGH HISTORY. tracked >= BRAND_MIN_TRACKED, which is the
    same floor the brand sentence itself requires. A brand table nobody may quote
    is a brand table nobody should download.

Budget: RECORDS_BUDGET_KB. main() prints the size and the line-kind distribution
every run, and the fixture test refuses a schema whose row grew a field without
the header being updated.

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
import math
import pathlib
import statistics
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
CATALOG_REL = "loupe-feed/catalog.json"
OUT = HERE / "price_history.json"
CORRECTIONS = HERE / "price_corrections.json"

# Dates on or after which the pricing METHODOLOGY changed. A price move that
# straddles one of these is an artefact of our own pipeline, not the brand's
# decision, and must never be shown to a shopper as a discount.
PRICE_EPOCHS = [
    "2026-07-15",  # 10b4c79 — pinned scrape to country=US, 49 brands flipped to USD
    "2026-09-05",  # fx-refresh — fx_to_usd re-fetched (ECB 2026-09-04); every brand in a moved currency steps by one ratio on this day
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

# ── The shopper-facing thresholds ────────────────────────────────────────────
# Every number below gates a SENTENCE SHOWN TO A SHOPPER, so each one is a claim
# about what we are willing to vouch for, not a tuning knob. They are emitted
# into price_records.json under "thresholds" and mirrored exactly in
# src/utils/priceRecordCopy.ts; the two must never drift, which is why the app
# reads them from the file rather than hard-coding a second copy.
#
# A record is only worth a row if it earns a line, so these also decide the size
# of the published file — see record_kinds().

# "Lowest price we've seen" is a claim about a WINDOW. Two weeks is the shortest
# window in which "we've seen" is not an overstatement; below it we have watched
# a piece for a few days and know nothing about its pricing.
MIN_DAYS_FOR_PRICE_CLAIM = 14

# The size run is read off the store's own in-stock variants, which is a much
# noisier signal than price (a single unfulfilled order moves it), so it needs
# less history but a bigger gap before it says anything.
MIN_DAYS_FOR_SIZE_CLAIM = 7
SIZE_RUN_MIN = 4      # it must NORMALLY offer a real run, not two sizes total
SIZE_LEFT_MAX = 2     # …and only 1-2 may remain
SIZE_GAP_MIN = 2      # …and at least this many must have gone

# Tenure. "New this week" is a 7-day claim; below MIN_TENURE_DAYS "on the shelf
# N days" is not information, it is a number.
NEW_ARRIVAL_DAYS = 7
MIN_TENURE_DAYS = 21

# A price move smaller than this rounds to a shrug. 5% of $180 is $9.
MIN_DROP_PCT = 5

# How wide a piece's observed price range must be before "lowest price we've
# seen" is worth saying. NOT the same number as MIN_MEANINGFUL_MOVE, and the
# difference is the whole point: 2% is the floor for detecting that a price
# MOVED, but 49 brands here are converted from another currency every single day,
# so a 2.4% band is the exchange rate wobbling, not a brand marking down.
# Measured 2026-09-05, before this gate existed: Alighieri's Poet's Pencil
# Necklace (537/550/546) and three others earned "Lowest price we've seen" off
# pure FX jitter. Same principle as PRICE_EPOCHS, one level up — our own
# arithmetic must never be published as somebody's sale.
MIN_RANGE_PCT = 5

# The brand sentence. Under 20 tracked pieces a percentage is a coin flip
# wearing a decimal point, and it is the one line that names the brand.
BRAND_MIN_TRACKED = 20
BRAND_RARELY_PCT = 10   # <= this share ever discounted -> "rarely marks down"
BRAND_OFTEN_PCT = 30    # >= this share -> "discounts often"

# How stale the file may be before the DATE-derived lines stop being made. The
# refresh runs daily; three days means a broken pipeline silently stops the
# claims instead of quietly ageing them.
MAX_STALE_DAYS = 3

# The app payload's ceiling. Not a suggestion: it is downloaded on a phone, on
# cellular, after the 11 MB catalog. Blowing it is a build failure, not a warning.
RECORDS_BUDGET_KB = 500

RECORDS_OUT = HERE / "price_records.json"

# Which line the swipe card shows when a piece earns several. "Is this a fair
# price?" is the question 1,701 saves against 113 clicks says is unanswered, so
# the two price lines lead. "Back at full price" is last because it is the line
# that argues against the purchase — it belongs on the product page, where she
# went looking, not on a card she sees for two seconds. Mirrored by ORDER in
# src/utils/priceRecordCopy.ts; the two must not drift.
CARD_PRIORITY = ["lowest", "drop", "sizes", "new", "tenure", "rebound"]

# Lines the SWIPE CARD never shows, however true they are. "Back at full price"
# is the honest, unflattering half of the price record and it earns the other
# lines their credibility — but a card is two seconds of browsing with no context
# to hang "full price" on, and 420 of them would read as Loupe arguing with the
# feed. It belongs on the product page, which is where she went to decide.
DETAIL_ONLY_LINES = {"rebound"}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


# ── FX corrections ───────────────────────────────────────────────────────────
# Until 2026-08-06 every price here was converted using the per-brand `currency`
# ANNOTATION in brands.json, while a live probe that knew better ran alongside
# and only logged a warning. Eight stores were annotated wrong, so this file —
# the asset the whole business rests on — carried 422 wrong prices a day.
#
# The snapshots are the record and the record is not rewritten. The repair is
# published in price_corrections.json and applied on READ, here and in
# loupe-site/tools/build_loupe_index.py, which are the only two things that walk
# the archive. A missing table is a hard stop, not a silent zero: republishing
# uncorrected prices because a path was wrong is exactly the failure it exists
# to end.

def load_corrections():
    if not CORRECTIONS.exists():
        sys.exit(f"REFUSING TO BUILD: {CORRECTIONS.name} is missing.\n"
                 "  It records the FX errors this archive must correct before\n"
                 "  quoting a price. Without it price_history.json would restate\n"
                 "  8 brands' history wrong by up to 3.7x.")
    doc = json.loads(CORRECTIONS.read_text(encoding="utf-8"))
    return {c["brand"]: (c["fromDay"], c.get("toDay"), float(c["factor"]))
            for c in doc.get("corrections", [])}


def correct_price(price, brand, day, has_currency, corrections):
    """The archived price restated in USD.

    `has_currency` is the self-terminating guard: from 2026-08-06 every row
    carries its own observed `currency`, and such a row was priced from the
    observation — correcting it again would introduce the error a second time,
    in the opposite direction.
    """
    if price is None or has_currency:
        return price
    hit = corrections.get(brand)
    if not hit:
        return price
    lo, hi, factor = hit
    if day < lo or (hi and day > hi):
        return price
    # Rounded to the DOLLAR, like every other price in the archive. The stored
    # value is itself an integer, so the store's own number is only known to
    # within half a unit of its currency; measured across all 422 affected rows,
    # 89.8% of these corrections could be off by $1 and none by more. Emitting
    # 163.37 would claim a precision the archive cannot support.
    return round(price * factor)


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


def tenure_trusted_from(window_start: str, window_end: str) -> str:
    """The first day on which a product's firstDay means "it arrived".

    Before it, firstDay is a fact about US: either the day this archive began
    (a piece present in the first snapshot may have been on sale for a year), or
    the day the 60-item cap came off and thousands of long-standing pieces became
    visible at once. Neither is an arrival, and "New this week" printed over
    either one is a lie on the card.
    """
    cands = [_plus(e, EPOCH_SETTLE_DAYS) for e in SAMPLING_EPOCHS if e <= window_end]
    return max([window_start, *cands])


def _pct_drop(prev: float, now: float) -> int:
    """How far a price fell, in whole percent. 0 when it did not fall."""
    if prev <= 0 or now <= 0 or now >= prev:
        return 0
    return int(round((prev - now) / prev * 100))


def day_at(ctx, i):
    """The real DATE of a day index, or None.

    Day numbers index the snapshot list, which has holes (see the header). There
    is no arithmetic that recovers a date from one; there is only this lookup.
    None means "we cannot date this", and a claim we cannot date is a claim we
    do not make — never a claim we date approximately.
    """
    dates = ctx.get("dayDates")
    if not dates or not isinstance(i, int) or i < 0 or i >= len(dates):
        return None
    return dates[i]


def record_kinds(rec, ctx) -> list:
    """Which TRUE lines a compact record earns, in card-priority order.

    THE MIRROR. This is the same predicate set, in the same order, on the same
    thresholds as describeRecord() in src/utils/priceRecordCopy.ts. It exists
    here for two reasons that are really one: it decides which rows are worth
    publishing (a row that earns no line is bytes on a phone for nothing), and it
    lets the build PRINT what the app will say before anyone ships it.

    `rec` is the emitted row: [f, n, lo, hi, p, q, c, sn, sx].
    `ctx` carries windowStart, today and tenureTrustedFrom.
    """
    f, n, lo, hi, p, q, c, sn, sx = rec
    out = []
    # Garbage in, SILENCE out — never a default. A NaN that reaches a comparison
    # makes every one of them false, which happens to be safe here, but "happens
    # to be safe" is not a guarantee and this file's whole job is guarantees.
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) for v in rec):
        return out
    if p <= 0 or lo <= 0 or hi <= 0 or hi < lo or n <= 0:
        return out

    moved = hi > lo * (1 + MIN_MEANINGFUL_MOVE)
    # A range wide enough to be somebody's decision rather than the FX rate.
    real_range = _pct_drop(hi, lo) >= MIN_RANGE_PCT

    # "Lowest price we've seen" — it has genuinely been more expensive, and it is
    # sitting at the bottom of that range.
    if n >= MIN_DAYS_FOR_PRICE_CLAIM and real_range and p <= lo * (1 + MIN_MEANINGFUL_MOVE):
        out.append("lowest")

    # "Down 22% since Aug 12" — the last move, measured against the price it
    # moved FROM, dated by LOOKUP to the day we first saw the new price. Never
    # dated into the future, however wrong the clock is; never dated at all if
    # the index cannot be resolved.
    day_of_change = day_at(ctx, c)
    if (q > 0 and c >= 0 and day_of_change and day_of_change <= ctx["today"]
            and _pct_drop(q, p) >= MIN_DROP_PCT):
        out.append("drop")

    # "Back at full price" — it WAS cheaper, the last move was upward, and it is
    # back at the top of its range. Says the sale ended; does not imply one is coming.
    if (n >= MIN_DAYS_FOR_PRICE_CLAIM and moved and q > 0 and p > q
            and p >= hi * (1 - MIN_MEANINGFUL_MOVE)
            and _pct_drop(p, q) >= MIN_DROP_PCT):
        out.append("rebound")

    # "Only 2 sizes left" — off the store's own in-stock variants, and only for a
    # piece that normally offers a real run. sn == 0 is a one-size piece or a
    # sold-out one, and says nothing either way.
    if (n >= MIN_DAYS_FOR_SIZE_CLAIM and sx >= SIZE_RUN_MIN
            and 1 <= sn <= SIZE_LEFT_MAX and sx - sn >= SIZE_GAP_MIN):
        out.append("sizes")

    # Tenure. Both lines are void unless firstDay is an arrival (see
    # tenure_trusted_from) and unless the file is fresh enough for "today" to
    # mean anything against it.
    first_day = day_at(ctx, f)
    stale = (dt.date.fromisoformat(ctx["today"])
             - dt.date.fromisoformat(ctx["windowEnd"])).days > MAX_STALE_DAYS
    if (first_day and f > 0 and first_day >= ctx["tenureTrustedFrom"]
            and first_day <= ctx["today"] and not stale):
        age = (dt.date.fromisoformat(ctx["today"]) - dt.date.fromisoformat(first_day)).days
        if age <= NEW_ARRIVAL_DAYS:
            out.append("new")
        elif age >= MIN_TENURE_DAYS:
            out.append("tenure")
    # Sorted, not appended in evaluation order: out[0] is what the swipe CARD
    # shows (it has room for exactly one line), so this ordering is a product
    # decision and has to be stated as one rather than fall out of the order the
    # checks happen to be written in. Identical to ORDER in priceRecordCopy.ts.
    return sorted(out, key=CARD_PRIORITY.index)


def brand_line_kind(stat) -> str:
    """Which brand sentence a [tracked, everDiscounted, medianHold] row earns."""
    if not stat or stat[0] < BRAND_MIN_TRACKED:
        return ""
    pct = 100.0 * stat[1] / stat[0]
    if pct <= BRAND_RARELY_PCT:
        return "brand_rarely"
    if pct >= BRAND_OFTEN_PCT:
        return "brand_often"
    return ""


def compact_records(hist, detail, today=None):
    """The app payload: one row per product that has something true to say.

    Everything here is a subset of price_history.json — no new measurement, no
    new claim. What it adds is the two fields the sentence needs and the history
    file does not carry (the price a move came FROM, and the size run), and what
    it removes is every product and every brand we would refuse to say anything
    about anyway.
    """
    today = today or dt.date.today().isoformat()
    ctx = {
        "windowStart": hist["windowStart"],
        "windowEnd": hist["windowEnd"],
        "today": today,
        # The index -> date map. Without it nothing dated may be claimed, so it
        # is read from the archive rather than reconstructed from windowStart.
        "dayDates": hist.get("dayDates") or [],
        "tenureTrustedFrom": tenure_trusted_from(hist["windowStart"], hist["windowEnd"]),
    }

    records, kinds, card = {}, collections.Counter(), collections.Counter()
    live_brands = set()
    for pid, row in hist["products"].items():
        d = detail.get(pid)
        if not d or not d["onShelfNow"]:
            continue                      # cannot be rendered; pure payload
        live_brands.add(d["brand"])
        rec = [row[0], row[1], row[2], row[3], row[4],
               round(d["prevPrice"], 2), d["lastChange"], d["sizesNow"], d["sizesMax"]]
        ks = record_kinds(rec, ctx)
        if not ks:
            continue
        records[pid] = rec
        for k in ks:
            kinds[k] += 1
        on_card = [k for k in ks if k not in DETAIL_ONLY_LINES]
        if on_card:
            card[on_card[0]] += 1

    # The brand table covers every brand still on the shelf, NOT just the brands
    # with a product record: the brand sentence is shown on a product page whose
    # own row may say nothing at all. Brands that left the catalog are dropped —
    # nothing can render them, and a table naming labels we no longer carry is a
    # table that will one day be quoted.
    brands = {b: [s["tracked"], s["everDiscounted"], s["medianHold"]]
              for b, s in hist["brands"].items()
              if s["tracked"] >= BRAND_MIN_TRACKED and b in live_brands}

    out = {
        "generatedAt": hist["generatedAt"],
        "windowStart": hist["windowStart"],
        "windowEnd": hist["windowEnd"],
        "days": hist["days"],
        **({"partialHistory": True} if hist.get("partialHistory") else {}),
        "priceEpochs": PRICE_EPOCHS,
        "samplingEpochs": SAMPLING_EPOCHS,
        "epochSettleDays": EPOCH_SETTLE_DAYS,
        "arrivalBlackout": hist["arrivalBlackout"],
        # ~1 KB, and the only thing that turns a day number into a date. The
        # archive has holes (four days lost in July 2026), so windowStart + index
        # is off by four days for everything after them. Always shipped.
        "dayDates": ctx["dayDates"],
        "tenureTrustedFrom": ctx["tenureTrustedFrom"],
        # Shipped so the app gates on the SAME numbers this build selected on. A
        # threshold that lives in two places is a threshold that will disagree
        # with itself the first time one of them is tuned.
        "thresholds": {
            "minMeaningfulMove": MIN_MEANINGFUL_MOVE,
            "minDaysForPriceClaim": MIN_DAYS_FOR_PRICE_CLAIM,
            "minDaysForSizeClaim": MIN_DAYS_FOR_SIZE_CLAIM,
            "sizeRunMin": SIZE_RUN_MIN,
            "sizeLeftMax": SIZE_LEFT_MAX,
            "sizeGapMin": SIZE_GAP_MIN,
            "newArrivalDays": NEW_ARRIVAL_DAYS,
            "minTenureDays": MIN_TENURE_DAYS,
            "minDropPct": MIN_DROP_PCT,
            "minRangePct": MIN_RANGE_PCT,
            "brandMinTracked": BRAND_MIN_TRACKED,
            "brandRarelyPct": BRAND_RARELY_PCT,
            "brandOftenPct": BRAND_OFTEN_PCT,
            "maxStaleDays": MAX_STALE_DAYS,
        },
        "schema": {
            "records": "[firstDayIdx, daysSeen, minPrice, maxPrice, lastPrice, "
                       "prevPrice, lastChangeDayIdx, sizesNow, sizesMax]",
            "brands": "[tracked, everDiscounted, medianHold]",
            "zeros": "prevPrice 0 = never moved; lastChangeDayIdx -1 = never "
                     "moved; sizesNow/sizesMax 0 = the store lists no sizes",
        },
        "records": records,
        "brands": brands,
    }
    return out, {"earned": kinds, "card": card}


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

    corrections = load_corrections()
    n_corrected = 0

    # pid -> list of (dayIndex, price); brand/meta captured from the newest sighting
    seen = collections.defaultdict(list)
    brand_of = {}
    # The SIZE RUN, for "only 2 sizes left". catalog.json's `sizes` is the
    # IN-STOCK set (build_catalog.available_sizes), so its length is a count of
    # what she can actually buy — never of what the store ranges. sizes_max is
    # the most we have ever seen buyable, i.e. what "normally offers more" means
    # here, and it is measured rather than assumed. A one-size piece and a
    # sold-out one both read 0 and are excluded by SIZE_RUN_MIN.
    sizes_max = collections.defaultdict(int)
    sizes_now = {}
    last_seen_i = {}
    last_idx = len(days) - 1

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
            brand = p.get("brand") or "?"
            fixed = correct_price(float(price), brand, day, bool(p.get("currency")),
                                  corrections)
            if fixed != price:
                n_corrected += 1
            seen[pid].append((i, float(fixed)))
            brand_of[pid] = brand
            raw_sizes = p.get("sizes")
            n_sizes = len(raw_sizes) if isinstance(raw_sizes, list) else 0
            if n_sizes > sizes_max[pid]:
                sizes_max[pid] = n_sizes
            # Nothing is buyable on a sold-out piece, whatever the sizes array
            # still says — the app prints "Out of Stock" in that slot and a
            # "2 sizes left" line beside it would contradict it.
            sizes_now[pid] = 0 if p.get("available") is False else n_sizes
            last_seen_i[pid] = i
        if verbose:
            print(f"  {day}  {len(doc.get('products', [])):>5} products", file=sys.stderr)

    products = {}
    brand_stats = collections.defaultdict(lambda: {"tracked": 0, "everDiscounted": 0, "holds": []})
    # The two things the SENTENCE needs and the history file deliberately does
    # not carry: the price a move came FROM (so "down 22%" is measured against
    # the price it was, not against an unrelated high) and the size run. Kept out
    # of price_history.json to leave that file's published schema exactly as it
    # was, and handed to compact_records() instead.
    detail = {}

    for pid, points in seen.items():
        points.sort()
        first_i = points[0][0]
        last_i, last_price = points[-1]
        prices = [pr for _, pr in points]
        on_shelf_now = last_seen_i.get(pid) == last_idx

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
            # No comparable pair → no move, so prevPrice 0 / lastChange -1. The
            # sentinels are the point: `first_i` would read as "it changed on the
            # first day we saw it", which is the exact false claim this branch
            # exists to avoid.
            detail[pid] = {"prevPrice": 0.0, "lastChange": -1,
                           "sizesNow": sizes_now.get(pid, 0),
                           "sizesMax": sizes_max.get(pid, 0),
                           "onShelfNow": on_shelf_now,
                           "brand": brand_of[pid]}
            b0 = brand_of[pid]
            brand_stats[b0]["tracked"] += 1
            brand_stats[b0]["holds"].append(len(points))
            continue
        cmp_prices = [pr for _, pr in same_epoch]

        lo, hi = min(cmp_prices), max(cmp_prices)
        last_change = first_i
        change_i, prev_price = -1, 0.0
        for k in range(1, len(same_epoch)):
            if abs(same_epoch[k][1] - same_epoch[k - 1][1]) / max(same_epoch[k - 1][1], 1) > MIN_MEANINGFUL_MOVE:
                last_change = same_epoch[k][0]
                change_i, prev_price = same_epoch[k][0], same_epoch[k - 1][1]

        detail[pid] = {"prevPrice": prev_price, "lastChange": change_i,
                       "sizesNow": sizes_now.get(pid, 0),
                       "sizesMax": sizes_max.get(pid, 0),
                       "onShelfNow": on_shelf_now,
                       "brand": brand_of[pid]}

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

    if verbose:
        # Announced every run. A repair that happens silently is one nobody
        # notices has stopped happening.
        print(f"  FX: {n_corrected:,} archived prices re-derived from "
              f"{CORRECTIONS.name} ({len(corrections)} brands)", file=sys.stderr)

    hist = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "windowStart": start,
        "windowEnd": end,
        "days": len(days),
        # Which brands' archived prices were re-derived on read, and over what
        # window. Emitted rather than merely applied: a consumer must be able to
        # see that a number here is a correction and not the original reading.
        "fxCorrections": {b: {"from": lo, "to": hi, "factor": f}
                          for b, (lo, hi, f) in sorted(corrections.items())},
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
        # Every day index in this file points HERE. The snapshot list is not the
        # calendar — 2026-07-25..28 are missing — so windowStart + index is four
        # days early for everything after July and there is no arithmetic that
        # fixes it. Emitted since 2026-09-05, after the header spent two months
        # calling these "offsets from windowStart".
        "dayDates": days,
        "schema": "[firstDayIdx, daysSeen, minPrice, maxPrice, lastPrice, lastChangeDayIdx]",
        "products": products,
        "brands": brands,
    }
    # Two values, deliberately. `detail` is the per-product extra the SHOPPER-
    # facing file needs; returning it as a private key inside `hist` would put it
    # one forgotten line away from being published into price_history.json and
    # silently changing that file's schema.
    return hist, detail


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


RECORD_LABELS = {
    "lowest": "Lowest price we've seen",
    "drop": "Down N% since <date>",
    "rebound": "Back at full price",
    "sizes": "Only N sizes left",
    "new": "New this week",
    "tenure": "On the shelf N days",
}


def records_report(recs, kinds):
    """What the app will actually say, counted, before anyone ships it.

    The distribution is the honest measure of whether this feature exists. A
    build that emits 40 lines across 8,000 products has not shipped reassurance,
    it has shipped a rare easter egg, and that is visible here and nowhere else.
    """
    n = len(recs["records"])
    earned, card = kinds["earned"], kinds["card"]
    print("\n" + "=" * 74)
    print(f"PRICE RECORDS  (the app payload)  window {recs['windowStart']} -> "
          f"{recs['windowEnd']}, tenure trusted from {recs['tenureTrustedFrom']}")
    print(f"  products with a line : {n:,}")
    # EARNED is how many pieces the line is true of; ON THE CARD is how many
    # actually show it, since the swipe card has room for exactly one and takes
    # the highest-priority. The second column is the one that reaches a shopper.
    print(f"\n  {'line':28} {'earned':>8} {'on the card':>12} {'share':>8}")
    for kind, label in RECORD_LABELS.items():
        e = earned.get(kind, 0)
        c = "detail only" if kind in DETAIL_ONLY_LINES else f"{card.get(kind, 0):,}"
        share = ("" if kind in DETAIL_ONLY_LINES
                 else f"{100.0*card.get(kind, 0)/max(n, 1):>7.1f}%")
        print(f"  {label:28} {e:>8,} {c:>12} {share:>8}")
    print(f"  {'(total lines / products)':28} {sum(earned.values()):>8,} {n:>12,}")
    print(f"  {'(cards that show a line)':28} {'':>8} {sum(card.values()):>12,}")

    brands = recs["brands"]
    rarely = sum(1 for s in brands.values() if brand_line_kind(s) == "brand_rarely")
    often = sum(1 for s in brands.values() if brand_line_kind(s) == "brand_often")
    print(f"\n  brands in the table  : {len(brands):,} (>= {BRAND_MIN_TRACKED} tracked)")
    print(f"    'rarely marks down' : {rarely:,}")
    print(f"    'discounts often'   : {often:,}")
    print(f"    no brand line       : {len(brands) - rarely - often:,} "
          f"(between {BRAND_RARELY_PCT}% and {BRAND_OFTEN_PCT}% — we say nothing)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="print a summary, write nothing")
    ap.add_argument("--allow-shallow", action="store_true",
                    help="build from a truncated clone anyway (stamps partialHistory)")
    args = ap.parse_args()

    print("walking catalog history…", file=sys.stderr)
    hist, detail = build(allow_shallow=args.allow_shallow)
    report(hist)

    recs, kinds = compact_records(hist, detail)
    records_report(recs, kinds)

    if not args.report:
        OUT.write_text(json.dumps(hist, separators=(",", ":")), encoding="utf-8")
        kb = OUT.stat().st_size / 1024
        print(f"\nwrote {OUT.name}  ({kb:,.0f} KB, vs 8,800 KB for one catalog snapshot)")

        blob = json.dumps(recs, separators=(",", ":"))
        rec_kb = len(blob.encode("utf-8")) / 1024
        # Checked BEFORE the write. This file is downloaded on a phone, so an
        # over-budget payload must never reach the tree, be committed, and be
        # served for a day while somebody reads the log.
        if rec_kb > RECORDS_BUDGET_KB:
            sys.exit(f"REFUSING TO WRITE {RECORDS_OUT.name}: {rec_kb:,.0f} KB exceeds the "
                     f"{RECORDS_BUDGET_KB} KB app budget.\n"
                     "  Tighten record_kinds() (fewer rows earn a line) rather than\n"
                     "  raising the budget — the ceiling is the user's data plan.")
        RECORDS_OUT.write_text(blob, encoding="utf-8")
        print(f"wrote {RECORDS_OUT.name}  ({rec_kb:,.0f} KB of a "
              f"{RECORDS_BUDGET_KB} KB budget, {len(recs['records']):,} records, "
              f"{len(recs['brands']):,} brands)")


if __name__ == "__main__":
    main()
