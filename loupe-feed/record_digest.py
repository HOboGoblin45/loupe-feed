#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# Loupe — the WEEKLY "YOUR RECORD" push (one per user per week, or nothing)
#
# WHY THIS EXISTS
#
# Measured on live telemetry: 1,701 saves against 113 outbound clicks. She saves
# constantly and then does not come back. The daily digest already tells her when
# a piece she saved has fallen below the price SHE paid attention to — that is a
# comparison against one number from one day.
#
# This is the other half, and it is the half nobody else can send. Loupe has
# photographed every product in ~185 independent labels every day since
# 2026-06-17. build_price_history.py turns those commits into price_records.json,
# which knows things a shop's own site does not say out loud: this is the lowest
# this piece has ever been; it fell 22% on Aug 12; it normally offers seven sizes
# and two are left. Once a week we tell each user the single biggest genuine move
# among the pieces SHE saved, and nothing at all on a week with no such move.
#
# WHAT THIS FILE IS NOT ALLOWED TO DO, IN ORDER OF HOW BADLY IT WOULD GO
#
#   1. Invent a number. Every figure in every sentence here is read out of
#      price_records.json or the live catalog, cross-checked against the other,
#      and dropped when the two disagree. There is no rounding-up, no "about",
#      no estimate.
#   2. Report our own arithmetic as somebody's markdown. Two separate guards:
#      PRICE EPOCHS (the days our pricing METHODOLOGY changed — 2026-07-15's
#      country=US pin, 2026-09-05's FX-table refresh) and the per-brand FX
#      CORRECTIONS published in price_corrections.json. A move that touches
#      either is not a move, it is us.
#   3. Interrupt on a shrug. 5% / $1 floors, a 5% range floor, and a hard
#      requirement that the move happened THIS WEEK.
#   4. Interrupt twice. Never within 24h of any marketing push, never more than
#      one a week, never anything on a quiet week.
#
# HOW THE CAPS WORK WITHOUT A NEW COLUMN (read this before changing them)
#
# profiles carries exactly two marketing fields — last_marketing_push_at and
# last_marketing_push_sig (supabase/2026-07_marketing_push_cols.sql) — and both
# belong to the LIVE daily digest. This job reads `at` and writes `at`, and does
# not touch `sig` at all:
#
#   • Reading `at` gives both caps for free. It is stamped by whichever pipeline
#     last interrupted the user, so "no push in the last 24h" and "no push in the
#     last 7 days" are answered by one field. The weekly cap is therefore
#     slightly STRICTER than "one record digest a week" — a user who got a daily
#     sale digest on Tuesday is not eligible on Thursday. That is the safe
#     direction, it needs no migration, and it makes a manual re-dispatch of this
#     workflow a no-op instead of a second push.
#   • NOT writing `sig` is deliberate and load-bearing. price_drop_push.py skips
#     a user whose freshly-computed signature equals the stored one; a value this
#     job wrote could never equal that, so the daily digest's anti-repeat would be
#     defeated for a day and it would re-send a digest it had already sent. One
#     pipeline must not corrupt another's idempotency key to save itself a column.
#
# Reads:   price_records.json + catalog.json (published), price_corrections.json
#          (local, and a hard stop if missing), Supabase profiles + saved_items.
# Writes:  Supabase profiles.last_marketing_push_at, and the Expo push itself.
#
# Stdlib only. Everything that can be shared with the daily digest IS shared —
# the Supabase client, the pagination, the roster, the Expo sender, the FX
# step test — by importing price_drop_push rather than restating it. The record
# predicates are imported from build_price_history for the same reason: the
# sentence this push quotes must be the same sentence the app shows, and two
# copies of a threshold are two thresholds.
#
# Required env:
#   SUPABASE_URL, SUPABASE_SERVICE_KEY  (SUPABASE_SERVICE_ROLE_KEY also accepted)
# Optional env:
#   RECORDS_URL          the published price_records.json (default: jsDelivr)
#   CATALOG_URL          the published catalog (default: price_drop_push's)
#   EXPO_ACCESS_TOKEN    Expo push auth (recommended, not required)
#   RECORD_DRYRUN=1      compute + print what WOULD send; no Expo call, no write
#   RECORD_TODAY         'YYYY-MM-DD' override, for rehearsing a specific day
#   RECORD_SAVES_FILE    DRY-RUN ONLY — read the roster + saves from a JSON file
#                        instead of Supabase, so the send can be rehearsed
#                        end-to-end without the service key. Ignored when live.
# ─────────────────────────────────────────────────────────────────────────────

import collections
import datetime as dt
import json
import math
import os
import pathlib
import re
import sys
import urllib.error
from datetime import datetime, timezone

import build_price_history as bph
import price_drop_push as pdp

HERE = pathlib.Path(__file__).resolve().parent

RECORDS_URL = os.environ.get(
    "RECORDS_URL",
    "https://cdn.jsdelivr.net/gh/HOboGoblin45/loupe-feed@main/loupe-feed/price_records.json",
)
DRYRUN = os.environ.get("RECORD_DRYRUN", "").strip() in ("1", "true", "yes")
TOP_N = int(os.environ.get("RECORD_TOP_N", "10") or "10")
SAVES_FILE = os.environ.get("RECORD_SAVES_FILE", "").strip()

# Print the composed message bodies. OFF unless explicitly asked for, because
# loupe-feed is a PUBLIC repository and an Actions log is a public artifact. The
# bodies name the pieces individual users saved: not identities, but a list of
# somebody's taste, and there is no reason for it to be on the open internet. A
# dry run in CI reports counts and lead kinds; a human at a terminal can opt in.
LOG_BODIES = os.environ.get("RECORD_LOG_BODIES", "").strip() in ("1", "true", "yes")

# ── The interruption budget ──────────────────────────────────────────────────
# Both read profiles.last_marketing_push_at (see the header). They are stated as
# two rules rather than one because they answer two different questions, and the
# run log reports them separately so a change to either is visible in the skips.
PUSH_QUIET_HOURS = 24        # never within a day of ANY marketing push
PUSH_MIN_GAP_DAYS = 7        # …and never more than one of these a week

# ── What counts as a move worth a lock screen ────────────────────────────────
# A weekly digest that reports a standing FACT rather than a fresh MOVE would say
# the same thing every Thursday until the piece sold out. Every price claim here
# must therefore be dated to the last MOVE_RECENT_DAYS — which is also what makes
# "dropped this week" a true sentence rather than a figure of speech.
MOVE_RECENT_DAYS = 7

# …and a move must survive one more snapshot before we wake a phone with it.
#
# WHY, MEASURED. On 2026-09-09 every single dated move in the published archive
# resolved to that same morning — `lastChangeDayIdx` was -1 (2,030 rows) or the
# LAST day index (79 rows), and nothing else. That is not a coincidence: the
# 2026-09-05 FX epoch plus EPOCH_SETTLE_DAYS leaves exactly two comparable
# snapshots, so every "move" the file can currently describe is a one-day diff
# confirmed by a single scrape. 58 of the 65 drops were one brand.
#
# A single scrape is the weakest evidence this pipeline produces, and its failure
# mode is documented all over this directory: a store served a different currency
# for one morning, a Markets endpoint misfired, a probe came back wrong. Requiring
# the new price to appear in at least one LATER snapshot is the same argument as
# EPOCH_SETTLE_DAYS one level down — a change does not land in one clean day — and
# on a weekly job it costs a day of latency and nothing else.
#
# Expressed as an index comparison, not a date subtraction: a snapshot day that is
# not the last snapshot day is, by definition of `c` being the LAST move, a price
# we have now seen twice.
MOVE_MIN_CONFIRM_SNAPSHOTS = 1

# The floors, taken from the archive's own published thresholds (minDropPct 5,
# minRangePct 5) plus the catalog's resolution. Every price in the feed is a
# dollar-rounded integer, so a sub-dollar "move" cannot exist in the data; the $1
# floor is that fact, not a preference. Same pair lib/savedItemLine.ts passes.
MIN_DROP_ABS = 1.0

# How far the archive's lastPrice may sit from the live catalog price before we
# refuse to quote either. The archive's last snapshot is yesterday's; if the shop
# repriced overnight, the number in the push would be stale the moment it landed.
# Half a dollar each side of a dollar-rounded integer, plus a cent for float
# comparison — the same rounding argument as FX_STEP_ROUNDING_SLACK.
PRICE_MATCH_SLACK = 0.51

# The ladder, most actionable first. A drop on a piece she saved is the only line
# that is both news and an instruction; a new all-time low is news about a price
# she has already looked at; a size run collapsing is news about losing the
# chance; tenure is context.
MOVE_SCORE = {"drop": 400, "lowest": 300, "sizes": 200, "tenure": 100, "new": 50, "rebound": 0}

# Which of those may LEAD a push. Tenure is scored (it is a real, ranked record —
# see MOVE_SCORE) but never leads, and neither do the other two:
#   • "On the shelf 48 days" says the piece is NOT scarce. True, useful on a
#     product page where she is deciding, and not worth waking a phone for.
#   • "New this week" cannot be news about a piece already in her Dresser.
#   • "Back at full price" argues against the purchase. It earns the other lines
#     their credibility on the product page (see DETAIL_ONLY_LINES in
#     build_price_history.py) and is the last thing a notification should say.
LEAD_KINDS = ("drop", "lowest", "sizes")

# Sent so the app can float the exact pieces this push named to the top of the
# Alerts screen (src/lib/dresserAlerts.ts rankAlerts, `focus`). Capped: Expo's
# payload limit is ~4 KB and nobody scans more than a screenful.
MAX_FOCUS_IDS = 12

# The app routes this to the "Sale & restock alerts" screen
# (src/services/notifications.ts SALES_UPDATES_NOTIFICATION_TYPE).
NOTIFICATION_TYPE = "sales_updates"
NOTIFICATION_SOURCE = "record_digest"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Category → a noun we are willing to put in a sentence, used only when brand +
# the shop's own product name is too long for a lock screen. "bottoms",
# "outerwear" and "accessories" have no honest singular that is not a guess (a
# jacket is not a coat, a skirt is not a pair), so they become "piece".
CATEGORY_NOUN = {
    "dresses": "dress",
    "tops": "top",
    "shoes": "shoes",
    "bottoms": "piece",
    "outerwear": "piece",
    "accessories": "piece",
}
LABEL_BUDGET = 46

# Words this copy may not contain, at all, ever. Not a style preference: the
# whole value of the sentence is that it is a measurement somebody else made and
# we merely kept. "Recommended for you" and "our algorithm" both say the opposite.
BANNED_WORDS = re.compile(
    r"\b(algorithm\w*|recommend\w*|personali[sz]e[ds]?|personali[sz]ation)\b", re.I
)
# The TERM "AI", not the letters: 'available', 'detail' and 'again' all contain
# them, and a case-insensitive \bai\b would eventually eat a brand name.
BANNED_AI = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])")


def copy_is_clean(*parts) -> bool:
    """True when none of the given strings contains a forbidden word."""
    for text in parts:
        if not isinstance(text, str):
            return False
        if BANNED_WORDS.search(text) or BANNED_AI.search(text):
            return False
    return True


# ── Dates ────────────────────────────────────────────────────────────────────

def today_utc() -> str:
    return os.environ.get("RECORD_TODAY", "").strip() or dt.date.today().isoformat()


def _date(day):
    """'YYYY-MM-DD' → date, or None. Never raises on junk."""
    if not isinstance(day, str):
        return None
    try:
        return dt.date.fromisoformat(day.strip())
    except ValueError:
        return None


def days_between(a: str, b: str):
    """(b - a) in days, or None when either end will not parse."""
    da, db = _date(a), _date(b)
    if da is None or db is None:
        return None
    return (db - da).days


def fmt_day(day: str, today: str) -> str:
    """'2026-08-12' → 'Aug 12' ('Aug 12, 2025' across a year boundary).

    Mirrors formatDay() in src/utils/priceRecordCopy.ts so the push and the
    product page date the same event with the same words.
    """
    d, t = _date(day), _date(today)
    if d is None:
        return ""
    label = f"{MONTHS[d.month - 1]} {d.day}"
    return label if (t is not None and d.year == t.year) else f"{label}, {d.year}"


# ── The archive ──────────────────────────────────────────────────────────────

def load_records(url: str = RECORDS_URL) -> dict:
    doc = pdp._req(url)
    if not isinstance(doc, dict) or not isinstance(doc.get("records"), dict):
        sys.exit(f"REFUSING TO SEND: {url} is not a price_records.json document.")
    return doc


def price_epochs(doc: dict) -> list:
    """Every day on which a price comparison becomes void, from ALL registers.

    The published file lists the epochs known when it was BUILT; the repo's
    build_price_history.PRICE_EPOCHS lists the ones known now; price_corrections
    .json's fxEpochs lists the FX-table refreshes that create them. A day in any
    register voids the comparison, so the union is what we guard on — otherwise
    an epoch registered this morning would not take effect until the archive was
    rebuilt, which is precisely the window in which the mistake gets made.
    """
    days = set(bph.PRICE_EPOCHS)
    for d in doc.get("priceEpochs") or []:
        if _date(d):
            days.add(d)
    try:
        corr = json.loads(pdp.CORRECTIONS.read_text(encoding="utf-8"))
        for e in corr.get("fxEpochs") or []:
            d = (e or {}).get("day")
            if _date(d):
                days.add(d)
    except (OSError, ValueError):
        pass  # load_price_corrections() below is the hard stop; this is a bonus.
    return sorted(days)


def build_ctx(doc: dict, today: str = None) -> dict:
    """Everything record_kinds() and the guards below need, in one dict."""
    today = today or today_utc()
    settle = doc.get("epochSettleDays")
    if not isinstance(settle, int) or settle < 0:
        settle = bph.EPOCH_SETTLE_DAYS
    thresholds = doc.get("thresholds") if isinstance(doc.get("thresholds"), dict) else {}
    return {
        "today": today,
        "windowStart": doc.get("windowStart") or "",
        "windowEnd": doc.get("windowEnd") or "",
        "dayDates": doc.get("dayDates") or [],
        "tenureTrustedFrom": doc.get("tenureTrustedFrom") or "",
        "priceEpochs": price_epochs(doc),
        "epochSettleDays": settle,
        "minDropPct": thresholds.get("minDropPct", bph.MIN_DROP_PCT),
        "minRangePct": thresholds.get("minRangePct", bph.MIN_RANGE_PCT),
        "maxStaleDays": thresholds.get("maxStaleDays", bph.MAX_STALE_DAYS),
    }


def ctx_is_broken(ctx: dict) -> bool:
    """True when the archive's header cannot support ANY claim.

    build_price_history.record_kinds() reads these four straight into
    date.fromisoformat() and len(), so a missing or malformed one raises there
    rather than returning silence. This is that check on our side: the file
    arrives over the wire and its header is not a promise.
    """
    if not isinstance(ctx.get("dayDates"), list):
        return True
    if _date(ctx.get("today")) is None or _date(ctx.get("windowEnd")) is None:
        return True
    return not isinstance(ctx.get("tenureTrustedFrom"), str)


def records_are_stale(ctx: dict) -> bool:
    """True when the archive has not been rebuilt recently enough to date a claim.

    Same rule record_kinds() applies to tenure, hoisted to the whole run: a
    weekly push whose "this week" is measured against a five-day-old file is
    making a dated claim it cannot support. A header we cannot read at all is
    the same answer, arrived at sooner.
    """
    if ctx_is_broken(ctx):
        return True
    age = days_between(ctx.get("windowEnd") or "", ctx["today"])
    return age is None or age > ctx.get("maxStaleDays", bph.MAX_STALE_DAYS) or age < 0


def day_at(ctx: dict, i):
    """The real DATE of a day index, or None. Never arithmetic — the snapshot
    list has holes (2026-07-25..28 are missing), so windowStart + i is four days
    early for everything after July."""
    return bph.day_at(ctx, i)


def in_settle_window(day: str, ctx: dict) -> bool:
    """True while a pricing-methodology change is still working through the feed."""
    if not day:
        return True
    for e in ctx["priceEpochs"]:
        end = _date(e)
        if end is None:
            continue
        if e <= day < (end + dt.timedelta(days=ctx["epochSettleDays"])).isoformat():
            return True
    return False


def move_is_void(prev_day, change_day, ctx: dict) -> bool:
    """True when a price move from `prev_day` to `change_day` may not be reported.

    Three ways a move is ours rather than the brand's:
      • it landed ON an epoch or inside its settle tail (in_settle_window);
      • it STRADDLES an epoch — the two prices being differenced were produced by
        different arithmetic;
      • we cannot bound it at all, because the change is the first day we ever
        saw the piece. An unbounded move is not a move we can vouch for.
    """
    if not change_day:
        return True
    if in_settle_window(change_day, ctx):
        return True
    if not prev_day:
        return True
    for e in ctx["priceEpochs"]:
        if prev_day < e <= change_day:
            return True
    return False


def fx_windows(corrections_doc: dict) -> dict:
    """{brand: (fromDay, toDay|None, factor)} — the published FX repair windows.

    Read, never re-listed. price_drop_push.py's header states why at length: a
    parallel list in this file would drift from the archive the first time anyone
    added a correction, and the two would then disagree about what a user is owed.
    """
    out = {}
    for c in corrections_doc.get("corrections", []) or []:
        brand = c.get("brand")
        try:
            factor = float(c.get("factor"))
        except (TypeError, ValueError):
            continue
        if brand and factor > 0:
            out[brand] = (c.get("fromDay") or "", c.get("toDay"), factor)
    return out


def fx_suppressed(brand: str, now_price, then_price, change_day, windows: dict) -> bool:
    """True when a price move on this brand may not be called a markdown.

    Two tests, and the second is the blunt one on purpose:
      1. The move IS the published correction, to within the rounding bound
         derived in price_drop_push.py. Imported, not re-derived.
      2. The change day falls inside the correction's own published window. Every
         correction in the file today has toDay: null — "still in force" — so
         this suppresses price claims for those brands outright. That is the
         file's own statement about which rows it does not vouch for, and this is
         not the place to second-guess it. main() prints what it costs, so
         closing a window in price_corrections.json is a visible, one-line fix
         rather than a silent code change here.
    """
    hit = windows.get((brand or "").strip())
    if not hit:
        return False
    lo, hi, factor = hit
    if pdp.is_fx_correction_step(now_price, then_price, factor):
        return True
    if change_day and lo and change_day >= lo and (not hi or change_day <= hi):
        return True
    return False


# ── One saved piece → the best true thing we can say about it ────────────────

def well_formed_row(rec) -> bool:
    """Exactly nine finite, non-boolean numbers — the published row's shape.

    `True` is an int in Python and would sail through every comparison below it
    as 1; build_price_history.record_kinds() excludes bools for the same reason.
    """
    if not isinstance(rec, (list, tuple)) or len(rec) != 9:
        return False
    return all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) for v in rec)


def piece_label(live: dict) -> str:
    """Brand + the shop's own product name; brand + a category noun when that is
    too long for a lock screen. Both halves come from somebody else's data — we
    never name a piece anything the shop does not call it.

    Two normalisations, neither of which changes what the piece is called:
      • a name that already opens with the brand is not prefixed with it again.
        Measured on the live catalog: "Oddli Oddli Baseball Tee".
      • a pipe in the shop's own title becomes a comma. "Flore Flore BIBI DRESS
        LW | Black" is a real product name and a pipe on a lock screen reads as a
        broken template rather than as punctuation.
    """
    brand = (live.get("brand") or "").strip()
    name = re.sub(r"\s+", " ", re.sub(r"\s*\|\s*", ", ", (live.get("name") or ""))).strip()
    if brand and name.lower().startswith(brand.lower()):
        full = name
    else:
        full = f"{brand} {name}".strip()
    if full and len(full) <= LABEL_BUDGET:
        return full
    noun = CATEGORY_NOUN.get((live.get("category") or "").strip().lower(), "piece")
    short = f"{brand} {noun}".strip()
    return short or full or "piece"


def candidate(pid: str, records: dict, ctx: dict, catalog: dict,
              fxw: dict, reasons=None) -> dict:
    """The strongest push-worthy record for one saved product, or None.

    Every gate that rejects records its reason into `reasons`, because "almost
    nobody qualified" is only useful when it says WHY.
    """
    def bump(reason):
        if reasons is not None:
            reasons[reason] += 1

    if ctx_is_broken(ctx):
        bump("archive_header_unreadable")
        return None
    rec = records.get(pid)
    live = catalog.get(pid)
    if rec is None:
        # Split, because the two halves mean opposite things and the ratio is the
        # single most useful number in this log: "she saved things that are gone"
        # is a churn fact about the independent tier, "the archive has nothing to
        # say about it" is a fact about how selective price_records.json is.
        bump("no_record_and_off_the_shelf" if live is None else "no_record_but_on_shelf")
        return None
    if not well_formed_row(rec):
        # record_kinds() unpacks its argument into nine names BEFORE it validates
        # anything, so a row of the wrong shape raises there rather than
        # returning silence. src/utils/priceRecordCopy.ts checks `length < 9`
        # first and this is that check, on this side of the network boundary —
        # the published file is fetched over the wire and its shape is not a
        # promise. Silence, never a default.
        bump("malformed_record_row")
        return None
    if live is None:
        bump("not_in_live_catalog")
        return None
    if live.get("stale"):
        # build_catalog.py's grace-carry: the piece is missing from the current
        # scrape and its price/sizes are FROZEN at last-good. A frozen number can
        # manufacture a move that nobody made.
        bump("catalog_row_is_stale")
        return None

    kinds = bph.record_kinds(rec, ctx)
    if not kinds:
        bump("record_says_nothing")
        return None

    f, n, lo, hi, p, q, c, sn, sx = rec
    live_price = live.get("price")
    try:
        live_price = float(live_price)
    except (TypeError, ValueError):
        bump("live_price_unreadable")
        return None

    last_day_idx = len(ctx["dayDates"]) - 1
    change_day = day_at(ctx, c) if isinstance(c, int) and c >= 0 else None
    prev_day = day_at(ctx, c - 1) if isinstance(c, int) and c >= 1 else None
    age = days_between(change_day, ctx["today"]) if change_day else None
    recent = age is not None and 0 <= age <= MOVE_RECENT_DAYS

    eligible = []
    for kind in kinds:
        if kind not in LEAD_KINDS:
            bump("kind_never_leads_a_push")
            continue

        if kind in ("drop", "lowest"):
            # The archive's lastPrice must still be the shop's price. If the shop
            # repriced overnight, the number would be stale on arrival — and we
            # would be quoting it as a fact.
            if abs(live_price - float(p)) > PRICE_MATCH_SLACK:
                bump("price_moved_since_the_snapshot")
                continue
            if not recent:
                bump("move_older_than_a_week")
                continue
            if c > last_day_idx - MOVE_MIN_CONFIRM_SNAPSHOTS:
                bump("move_seen_in_only_one_snapshot")
                continue
            if move_is_void(prev_day, change_day, ctx):
                bump("move_spans_a_price_epoch")
                continue
            if fx_suppressed(live.get("brand"), p, q, change_day, fxw):
                bump("brand_has_an_open_fx_correction")
                continue
            # The dollar floor belongs to "drop" ALONE. "Lowest price we've seen"
            # is a claim about the width of a RANGE (minRangePct, enforced in
            # record_kinds), not about the size of the last step: a piece that
            # ticked up a dollar and is still sitting on its floor is at its
            # lowest price, and q - p is negative there. Applying the floor to
            # both silently deleted that whole branch.
            if kind == "drop" and float(q) - float(p) < MIN_DROP_ABS:
                bump("below_the_dollar_floor")
                continue
            eligible.append(kind)

        elif kind == "sizes":
            # A size claim is about the store's own in-stock variant count, not a
            # price, so neither the epoch nor the FX guard applies to it. What
            # does apply is that the count must still be true right now.
            if len(pdp.norm_sizes(live.get("sizes"))) != sn:
                bump("size_run_moved_since_the_snapshot")
                continue
            eligible.append(kind)

    if not eligible:
        return None

    kind = max(eligible, key=lambda k: MOVE_SCORE.get(k, 0))
    return {
        "pid": pid,
        "kind": kind,
        "score": MOVE_SCORE.get(kind, 0),
        "brand": (live.get("brand") or "").strip(),
        "label": piece_label(live),
        "price": float(p),
        "prev": float(q),
        "pct": bph._pct_drop(float(q), float(p)),
        "range_pct": bph._pct_drop(float(hi), float(lo)),
        "sizes_left": int(sn),
        "sizes_max": int(sx),
        "change_day": change_day,
    }


def rank_key(c: dict):
    """Most actionable first; deterministic all the way down.

    Within a kind, bigger is more actionable — except sizes, where FEWER left is
    more urgent, so it sorts ascending. The pid tiebreak is not cosmetic: without
    it, two equal candidates would order by dict iteration and the push would
    change between two runs over identical data.
    """
    if c["kind"] == "sizes":
        magnitude = -c["sizes_left"]
    elif c["kind"] == "lowest":
        magnitude = c["range_pct"]
    else:
        magnitude = c["pct"]
    return (-c["score"], -magnitude, c["pid"])


# ── Copy ─────────────────────────────────────────────────────────────────────

def _tail(count: int) -> str:
    if count <= 0:
        return ""
    return f" +{count} more of your saves moved this week."


def compose(lead: dict, others: list, today: str):
    """One push, or None when the copy would say something we cannot vouch for.

    Every number below is carried from `candidate`, which read it out of the
    archive and checked it against the live catalog. Nothing is computed here
    except counts of the candidates themselves.
    """
    if not lead:
        return None
    label = lead["label"]
    drops = [c for c in others if c["kind"] == "drop"]

    if lead["kind"] == "drop":
        when = fmt_day(lead["change_day"], today)
        if not when:
            return None  # an undatable drop is a drop we do not announce
        if drops:
            n = len(drops) + 1
            title = f"{n} pieces you saved dropped this week"
            body = (f"The biggest: your {label}, down {lead['pct']}% since {when}."
                    + _tail(len(others) - len(drops)))
        else:
            title = "A piece you saved just dropped"
            body = f"Your {label} is down {lead['pct']}% since {when}." + _tail(len(others))

    elif lead["kind"] == "lowest":
        title = "A new low on a piece you saved"
        body = (f"Your {label} is at the lowest price we've seen — "
                f"${pdp.fmt_price(lead['price'])}." + _tail(len(others)))

    elif lead["kind"] == "sizes":
        left = lead["sizes_left"]
        title = "Running low on a piece you saved"
        body = (f"Only {left} size{'' if left == 1 else 's'} left in the {label} "
                f"you saved." + _tail(len(others)))

    else:
        return None

    if not copy_is_clean(title, body):
        return None
    return {"title": title, "body": body}


# ── Per-user assembly ────────────────────────────────────────────────────────

def _pushed_within(at_iso, hours: float, now: datetime) -> bool:
    """True when the last marketing push is younger than `hours`.

    A missing stamp is False — a user we have never pushed is eligible. An
    UNPARSEABLE stamp is True: a value we cannot read is not permission to
    interrupt somebody.
    """
    if not at_iso:
        return False
    try:
        d = datetime.fromisoformat(str(at_iso).replace("Z", "+00:00"))
    except ValueError:
        return True
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    delta = (now - d.astimezone(timezone.utc)).total_seconds()
    if delta < 0:
        return True  # stamped in the future — a broken clock is not a green light
    return delta < hours * 3600


def build_record_digests(users: dict, items: list, records: dict, ctx: dict,
                         catalog: dict, fxw: dict, now: datetime = None):
    """(messages, stamps, skips, reasons) for every user who has earned a push.

    Pure apart from `now`: no network, no Supabase, no clock of its own. That is
    what lets test_record_digest.py drive the whole decision from fixtures.
    """
    now = now or datetime.now(timezone.utc)
    skips = collections.Counter()
    reasons = collections.Counter()

    if records_are_stale(ctx):
        # A stale archive cannot date "this week", and every claim here is dated
        # or cross-checked against a price that is supposed to be current.
        skips["archive_too_stale_to_quote"] = len(users)
        return [], [], skips, reasons

    by_user = collections.defaultdict(list)
    for it in items:
        by_user[it.get("user_id")].append(it)

    messages, stamps = [], []
    for uid in sorted(users):
        u = users[uid]
        mine = by_user.get(uid) or []
        if not mine:
            skips["no_saved_items"] += 1
            continue
        if _pushed_within(u.get("at"), PUSH_QUIET_HOURS, now):
            skips["pushed_within_24h"] += 1
            continue
        if _pushed_within(u.get("at"), PUSH_MIN_GAP_DAYS * 24, now):
            skips["pushed_within_7d"] += 1
            continue

        cands, seen = [], set()
        for it in mine:
            pid = str(it.get("product_id") or (it.get("product") or {}).get("id") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            c = candidate(pid, records, ctx, catalog, fxw, reasons)
            if c:
                cands.append(c)
        if not cands:
            skips["no_qualifying_move"] += 1
            continue

        cands.sort(key=rank_key)
        summ = compose(cands[0], cands[1:], ctx["today"])
        if not summ:
            skips["copy_refused"] += 1
            continue

        messages.append({
            "to": u["token"],
            "title": summ["title"],
            "body": summ["body"],
            "sound": "default",
            "data": {
                "type": NOTIFICATION_TYPE,
                "source": NOTIFICATION_SOURCE,
                # The pieces this push is about, best first. The app floats them
                # to the top of the Alerts screen so the tap lands on the thing
                # the sentence named (src/lib/dresserAlerts.ts rankAlerts).
                "focus": [c["pid"] for c in cands[:MAX_FOCUS_IDS]],
            },
        })
        stamps.append((uid, cands[0]["kind"], len(cands) - 1))
    return messages, stamps, skips, reasons


# ── I/O ──────────────────────────────────────────────────────────────────────

def load_catalog(url: str = None) -> dict:
    """{id: {price, sizes, brand, name, category, stale}} from the published feed.

    price_drop_push.load_catalog() is the same walk minus `category`, which this
    file needs for the short label. Rather than fetch and parse an 11 MB document
    twice, the projection is done once here; every field means exactly what it
    means there, `stale` included (build_catalog.py's grace-carry flag).
    """
    data = pdp._req(url or pdp.CATALOG_URL)
    products = data.get("products", data) if isinstance(data, dict) else data
    out = {}
    for p in products or []:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        out[pid] = {
            "price": p.get("price"),
            "sizes": p.get("sizes") or [],
            "brand": p.get("brand") or "",
            "name": p.get("name") or "",
            "category": p.get("category") or "",
            "stale": bool(p.get("stale")),
        }
    return out


def load_roster():
    """(users, items) from Supabase — or from RECORD_SAVES_FILE in a dry run.

    The file path exists so this job can be rehearsed against a real roster
    without handing the service key to a laptop. It is refused outside a dry run:
    a fixture must never be able to decide who gets a real push.
    """
    if SAVES_FILE:
        if not DRYRUN:
            sys.exit("REFUSING TO SEND: RECORD_SAVES_FILE is dry-run only.")
        doc = json.loads(pathlib.Path(SAVES_FILE).read_text(encoding="utf-8"))
        return doc.get("users") or {}, doc.get("items") or []
    return pdp.load_users(), pdp.load_saved_items()


def stamp(uid: str, now_iso: str):
    """Record the interruption. ONLY last_marketing_push_at — see the header for
    why last_marketing_push_sig belongs to the daily digest and stays untouched."""
    pdp.sb("profiles", method="PATCH", params=f"id=eq.{uid}",
           body={"last_marketing_push_at": now_iso},
           extra_headers={"Prefer": "return=minimal"})


def say(line: str):
    """print() that cannot take the run down.

    Brand names in this feed carry accents (DémodéMODÉ, Siedrės, Pärlemor) and a
    console that is not UTF-8 raises UnicodeEncodeError on them — which would kill
    a job whose entire purpose is to send notifications, at the last step, after
    the work was done. The message is worth degrading; the run is not.
    """
    try:
        print(line)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(line.encode(enc, "replace").decode(enc, "replace"))


def summarize_run(messages, stamps, skips, reasons, users_n, verbose_bodies=False):
    """The run log. NO user id, no push token, and no message body unless this is
    a dry run — a scheduled run's log is a public artifact of a private list."""
    print(f"record-digest: {len(messages)} of {users_n} user(s) qualify.")
    for i, (_uid, kind, extra) in enumerate(stamps, 1):
        print(f"  #{i:03d}  lead={kind:<7} also_moved={extra}")
    if skips:
        print("  skipped:")
        for reason, n in skips.most_common():
            print(f"    {n:>5}  {reason}")
    if reasons:
        print("  pieces rejected (across all users, one line per gate):")
        for reason, n in reasons.most_common():
            print(f"    {n:>5}  {reason}")
    if verbose_bodies:
        print(f"  the first {min(TOP_N, len(messages))} message(s), verbatim:")
        for m in messages[:TOP_N]:
            # ASCII arrow, and say() for the body: a Windows console defaults to
            # cp1252 and half a dozen brands in this feed are accented.
            say(f"    -> {m['title']} | {m['body']}")


def main():
    if not SAVES_FILE and (not pdp.SUPABASE_URL or not pdp.SERVICE_KEY):
        print("record-digest: SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping (no-op).")
        return

    # Loaded FIRST: a missing corrections table is a hard stop while the run is
    # still a no-op, not after the catalog and the roster are in hand. Same
    # reasoning, same function, as the daily digest.
    corrections_doc = json.loads(pdp.CORRECTIONS.read_text(encoding="utf-8")) \
        if pdp.CORRECTIONS.exists() else None
    if corrections_doc is None:
        pdp.load_price_corrections()  # exits with the explanation
    fxw = fx_windows(corrections_doc)

    doc = load_records()
    ctx = build_ctx(doc)
    records = doc["records"]
    catalog = load_catalog()
    users, items = load_roster()

    print(f"Archive: {len(records)} records, window {ctx['windowStart']}..{ctx['windowEnd']}"
          f" (today {ctx['today']}) | catalog: {len(catalog)}"
          f" | push users: {len(users)} | saved items: {len(items)}"
          f" | price epochs: {len(ctx['priceEpochs'])} | FX windows: {len(fxw)}")
    if records_are_stale(ctx):
        print("  ! the archive is stale — nothing will be sent (see records_are_stale).")

    messages, stamps, skips, reasons = build_record_digests(
        users, items, records, ctx, catalog, fxw
    )
    summarize_run(messages, stamps, skips, reasons, len(users),
                  verbose_bodies=DRYRUN and LOG_BODIES)

    if not messages:
        print("Nothing to send this week. (A quiet week is a correct outcome.)")
        return
    if DRYRUN:
        print(f"[DRY RUN] would send {len(messages)} push(es); nothing was sent or written.")
        return

    succeeded_idx, tickets = pdp.send_expo_pushes(messages)
    print(f"Sent {len(succeeded_idx)} of {len(messages)} weekly record push(es).")

    now_iso = datetime.now(timezone.utc).isoformat()
    for i, (uid, _kind, _extra) in enumerate(stamps):
        ticket = tickets[i] if i < len(tickets) else None
        if isinstance(ticket, dict) and ticket.get("status") == "error":
            if (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
                try:
                    pdp.sb("profiles", method="PATCH", params=f"id=eq.{uid}",
                           body={"push_token": None},
                           extra_headers={"Prefer": "return=minimal"})
                except urllib.error.URLError as e:
                    print(f"  ! token clear failed: {e}", file=sys.stderr)
                continue  # dead device — delivered nothing, so do not stamp
        if i not in succeeded_idx:
            continue  # the batch POST failed — leave unstamped so it retries
        try:
            stamp(uid, now_iso)
        except urllib.error.URLError as e:
            print(f"  ! stamp failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
