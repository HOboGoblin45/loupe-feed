#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# Loupe — the weekly CONTENT SHEET
#
# WHY THIS EXISTS
#
# 84% of Loupe's installs come from Aniqa's TikTok and Instagram. The bottleneck
# on that channel is not editing and it is not taste — it is Monday morning, when
# somebody has to decide what to point a camera at. Meanwhile this repository is
# quietly accumulating the one thing no other fashion account has: a photograph
# of the price and the in-stock size run of every product in ~185 independent
# labels, taken every single day since 2026-06-17.
#
# A price that fell 30% on a named day, a size run down to its last one from a
# run of seven, a piece that arrived four days ago — those are not opinions and
# they are not "content ideas". They are measurements, and a measurement is the
# only thing a small account can post that a big one cannot copy. This file turns
# the week's measurements into ten ready-to-shoot stories and three list posts,
# writes them to content/, and stops.
#
# WHAT IT IS NOT ALLOWED TO DO, IN ORDER OF HOW BADLY IT WOULD GO
#
#   1. Invent a story. Every figure in every hook comes out of price_records.json
#      or catalog.json, is cross-checked against the other, and is dropped when
#      the two disagree. On a week with nothing to say, the sheet says so. An
#      empty sheet costs a post; a false one costs the account.
#   2. Report our own arithmetic as somebody's sale. Same two guards as
#      record_digest.py — PRICE EPOCHS (the days our pricing METHODOLOGY changed)
#      and the per-brand FX corrections in price_corrections.json — plus a third
#      this file adds (see house_step_ratio) for a coordinated whole-brand step
#      that nobody has registered yet.
#   3. Quote a move we have seen once. MOVE_MIN_CONFIRM_SNAPSHOTS, imported from
#      record_digest, is the same rule that gates a push: a price must survive one
#      further snapshot before it is a fact. A post outlives a notification, so if
#      anything the bar here should be higher.
#   4. Say a word that makes a measurement sound like a guess. "AI", "algorithm",
#      "recommend", "personalised" are banned outright (the whole value of the
#      sentence is that somebody else made the number and we merely kept it), and
#      so is every hype word on the list — the copy is dry on purpose.
#
# THE ONE GUARD THAT IS NOT IN record_digest.py, AND WHY (2026-09-10)
#
# price_records.json's lo/hi/prevPrice/lastChangeDayIdx are all measured INSIDE
# the current pricing epoch, because comparing across one is void. What that
# means in practice is easy to miss and impossible to un-publish: three days
# after an epoch lands, "the lowest price we have ever seen" is a statement about
# two mornings.
#
# Measured on the live file the day this was written: the 2026-09-05 FX-table
# refresh plus EPOCH_SETTLE_DAYS left exactly TWO comparable snapshots
# (2026-09-08, 2026-09-09). Every brand in the published table therefore read
# "0% ever discounted" — including labels we have watched mark down — because
# `everDiscounted` counts pieces that moved inside that same two-day window.
# A sheet that printed "Bec + Bridge has never discounted in 81 days of watching"
# off that file would have been wrong, checkably, in public.
#
# So every WINDOW claim here (lowest-ever, brand price discipline, "this price has
# held") is gated on len(comparable_days) >= MIN_DAYS_FOR_PRICE_CLAIM — the
# archive's own published floor for when "we've seen" stops being an
# overstatement. STEP claims ("down 30% on Sep 9") are gated differently, on
# confirmation and epochs, because a step is a claim about two adjacent days
# rather than about a window. When a gate closes, the sheet prints what it could
# not say and the date the claim comes back. It never quietly makes it anyway.
#
# WHAT IT DELIBERATELY DOES NOT READ
#
# stock.json. It carries per-variant inventory quantities, and its own `use`
# block forbids "a named brand's stock or units, in any artefact" and permits
# only aggregates over at least five brands. Every line on this sheet names a
# brand, so the file is off-limits here by its own terms. The size numbers used
# below come from catalog.json's `sizes` — the in-stock size selector any shopper
# can see on the product page — and from price_records.json's sizesNow/sizesMax,
# which are counted from it.
#
# Reads:   price_records.json, catalog.json, brands.json, price_corrections.json
#          (all local, all already public; the last one is a hard stop if missing)
# Writes:  content/week-YYYY-MM-DD.md, content/latest.md, content/latest.json
#
# Stdlib only. Every predicate that also exists elsewhere is IMPORTED rather than
# restated: record_kinds and the thresholds from build_price_history, the epoch /
# FX / confirmation guards from record_digest. Two copies of a rule are two rules.
#
# USAGE
#     python build_content_sheet.py                    # write the sheet
#     python build_content_sheet.py --report           # print it, write nothing
#     python build_content_sheet.py --today 2026-09-14 # rehearse a given day
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import collections
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import urllib.parse

import build_price_history as bph
import record_digest as rd

HERE = pathlib.Path(__file__).resolve().parent

RECORDS = HERE / "price_records.json"
CATALOG = HERE / "catalog.json"
BRANDS = HERE / "brands.json"
OUT_DIR = HERE / "content"

# Every link on the sheet. loupe-site/tools/build_product_pages.py writes exactly
# one page per AVAILABLE catalog product at this path, so "the product is
# available" and "the link resolves" are the same statement — which is why
# availability is checked on every story rather than assumed.
SITE = "https://useloupe.shop"

STORY_COUNT = 10

# The hook is the first line of the caption and the first line on the screen. 90
# characters is roughly what fits above the fold on both platforms before the
# "more" truncation, and a hook that needs a tap is not a hook.
HOOK_MAX = 90

# "This price has held for N days" is only a story if N is long enough to be a
# decision rather than a number.
HOLD_MIN_DAYS = 60

# ── The rotation check ───────────────────────────────────────────────────────
# catalog.json is the perBrandDisplay = 60 DECK, not the full per-store walk
# (that is shelf.json, which the app never downloads and this sheet has no need
# of). So "first seen in our daily snapshot" is a fact about the DECK, and a
# piece can enter the deck because a rotation pushed it in rather than because a
# shop published it. record_kinds' `new` is computed off that first sighting.
#
# Measured on the live file the day this was written: of 807 available pieces
# earning "New this week", 14 had been on their own shop's site for longer than a
# fortnight and five for over a year. Mondo Mondo's Star Studs in 14k were
# published 2025-09-12 and would have gone out as "Mondo Mondo put this up 5 days
# ago" — a sentence the brand itself could have disproved in one click, on the
# channel that supplies 84% of our installs.
#
# So a new-arrival story needs the STORE's own published_at to agree, and it is
# dated by the store's date rather than by ours. The two are different facts and
# the fact line carries both.
NEW_PUBLISHED_MAX_DAYS = bph.NEW_ARRIVAL_DAYS

# Nouns that take "these" and "have". `shoes` is the only plural in CATEGORY_NOUN
# and "this ALOHAS shoes has been on the shelf" is the sentence produced by not
# checking. Membership, not an endswith("s") test — "dress" ends in s too.
PLURAL_NOUNS = {"shoes"}

# A list post of five pieces from one label is one post about one label. Applies
# to the list posts only; the ten stories enforce a stricter one-per-label rule.
LIST_MAX_PER_BRAND = 2

# ── The coordinated-step guard ───────────────────────────────────────────────
# PRICE_EPOCHS catch the methodology changes we KNOW about and price_corrections
# .json catches the per-brand FX errors we have DIAGNOSED. Neither catches the
# third shape: a whole label's catalogue stepping by one ratio on one morning
# because its store flipped presentment currency, or a Markets endpoint answered
# differently for a day. It looks exactly like a house-wide sale.
#
# The discriminator is the SHAPE of the step, and it is measurable. A real sale
# is tiered — measured on Marfa Istanbul, 2026-09-09: 58 pieces moved across 13
# distinct price ratios (0.50, 0.69, 0.70, 0.80…), i.e. 50% / 30% / 20% off
# different rails. Arithmetic is not tiered: an FX flip multiplies every piece by
# the SAME number.
#
# So a brand-wide move is suppressed only when all three hold:
#   • at least HOUSE_STEP_MIN_ROWS pieces moved on the same day,
#   • at least HOUSE_STEP_SHARE of them share ONE ratio, and
#   • that ratio is small enough to be plausibly a currency table — the largest
#     rate move in the registered fxEpochs to date is 13.8% (SEK, 2026-09-05), so
#     a uniform step beyond FX_LOOKALIKE_MAX_PCT is a markdown, not a rate.
# Below that bar a uniform 30%-off-everything sale still gets reported, which is
# the correct outcome: it IS the story.
HOUSE_STEP_MIN_ROWS = 5
HOUSE_STEP_SHARE = 0.80
FX_LOOKALIKE_MAX_PCT = 25
HOUSE_STEP_RATIO_TOLERANCE = 0.01

# ── Copy rules ───────────────────────────────────────────────────────────────
# rd.BANNED_WORDS / rd.BANNED_AI are the same regexes the push copy is checked
# against; they are imported, never restated. This list is the second half —
# words that do not make a claim false, only unbelievable. The whole proposition
# is "we measured this", and nobody believes a measurement delivered in the voice
# of an ad.
HYPE = re.compile(
    r"\b(stunning|gorgeous|obsessed|iconic|amazing|incredible|insane|unreal|"
    r"must[\s-]?have|game[\s-]?chang\w*|viral|hurry|steal|slay|dreamy|swoon\w*|"
    r"literally dying|to die for|you need this|don'?t sleep|run don'?t walk|"
    r"trust me|the girlies|it girl|perfection|flawless|best ever|deal of)\b",
    re.I,
)

# An exclamation mark is a request to be excited rather than a reason to be. The
# sheet's whole tone rests on this and it is cheap to enforce.
NO_SHOUTING = re.compile(r"!")

# Numbers, for the rule that every figure in a hook must also appear in the fact
# line that sources it. Thousands separators are stripped so "$1,650" in the hook
# matches "$1,650" in the fact whichever way either was formatted.
NUMBER = re.compile(r"\d+(?:\.\d+)?")

# Category -> a noun we are willing to put in a sentence. Imported from
# record_digest so the sheet and the lock screen call a piece the same thing.
CATEGORY_NOUN = rd.CATEGORY_NOUN

# Which stories a shoot format suits. Apparel can be worn; a bag cannot.
WEARABLE = {"dresses", "tops", "bottoms", "outerwear", "shoes"}

# Priority, straight from the brief. `hold` and `tenure` are the two halves of
# the same editorial idea — "there is no rush" — and which one is available
# depends on how much comparable price history exists this week.
KIND_ORDER = ["drop", "lowest", "sizes", "new", "hold", "tenure"]

KIND_LABEL = {
    "drop": "confirmed markdown",
    "lowest": "lowest price on our record",
    "sizes": "about to sell out",
    "new": "new this week",
    "hold": "a price that will not move",
    "tenure": "shelf life",
}


# ── Small helpers ────────────────────────────────────────────────────────────

def load_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def monday_of(day: str) -> str:
    """The Monday of the week containing `day`. The sheet is named for it whether
    the job runs on Monday morning or is dispatched by hand on Thursday."""
    d = dt.date.fromisoformat(day)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def pretty_day(day: str) -> str:
    """'2026-09-07' -> 'Monday 7 September 2026'. Written out because the sheet is
    read by a person, not parsed."""
    d = dt.date.fromisoformat(day)
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return f"{days[d.weekday()]} {d.day} {months[d.month - 1]} {d.year}"


def money(x) -> str:
    """Dollar-rounded, thousands-separated. Every price in this feed is already a
    rounded integer (build_catalog.py), so a cent here would claim a precision
    the archive does not have."""
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "$?"


def numbers_in(text: str) -> set:
    return {m.group(0) for m in NUMBER.finditer((text or "").replace(",", ""))}


def copy_is_clean(*parts) -> bool:
    """True when every string is something we are willing to publish.

    Three separate rules, deliberately not merged: rd.copy_is_clean is the same
    check the push copy passes (and must stay identical to it), HYPE is this
    file's own, and the exclamation rule is about tone rather than truth.
    """
    if not rd.copy_is_clean(*parts):
        return False
    for text in parts:
        if not isinstance(text, str):
            return False
        if HYPE.search(text) or NO_SHOUTING.search(text):
            return False
    return True


def hook_is_sound(hook: str, fact: str) -> bool:
    """A hook may be published only if it is short, clean, specific and SOURCED.

    The last one is the rule that matters: every number in the hook must also
    appear in the fact line beneath it. That is what makes the sheet checkable by
    somebody who did not write it — Aniqa can read one line and know where the
    figure came from — and it is what stops a rounded, rephrased or remembered
    number reaching a caption.
    """
    if not hook or len(hook) > HOOK_MAX:
        return False
    if not copy_is_clean(hook, fact):
        return False
    nums = numbers_in(hook)
    if not nums:
        return False           # a hook with no number is not this sheet's job
    return nums <= numbers_in(fact)


# ── The site's URL contract ──────────────────────────────────────────────────
# Mirrors loupe-site/tools/build_product_pages.py (path_seg + usable_id). It
# cannot be imported — different repository — so it is restated here with the
# source named, and test_content_sheet.py pins the behaviour rather than the
# comment.

WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def usable_id(pid) -> bool:
    """Whether the site could have written a page for this id at all."""
    if not isinstance(pid, str) or not pid or len(pid) > 120:
        return False
    if any(c in pid for c in '\\/:*?"<>|') or any(ord(c) < 32 for c in pid):
        return False
    if pid != pid.strip() or pid.endswith("."):
        return False
    return pid.split(".")[0].lower() not in WINDOWS_RESERVED


def product_url(pid: str) -> str:
    return f"{SITE}/product/{urllib.parse.quote(pid, safe='')}/"


def has_live_page(live: dict) -> bool:
    """True when /product/<id>/ exists. build_product_pages.py writes a page for
    every available product and DELETES the page of one that is not, so this is
    the same condition, read off the same catalog."""
    return bool(live) and bool(live.get("available")) and not live.get("stale") \
        and usable_id(live.get("id"))


# ── The archive's own scope ──────────────────────────────────────────────────

def comparable_days(doc: dict, epochs=None) -> list:
    """The snapshot days on which a price may be compared with another.

    Exactly the filter build_price_history.build() applies before it computes
    minPrice / maxPrice / prevPrice / lastChangeDayIdx: the current pricing epoch
    only, minus each epoch's settle tail. Recomputed here because the length of
    this list is the scope of every window claim on the sheet, and the published
    file does not state it.
    """
    days = doc.get("dayDates") or []
    if not isinstance(days, list) or not days:
        return []
    days = [d for d in days if isinstance(d, str) and rd._date(d)]
    if not days:
        return []
    eps = [e for e in (epochs if epochs is not None else rd.price_epochs(doc)) if rd._date(e)]
    settle = doc.get("epochSettleDays")
    if not isinstance(settle, int) or settle < 0:
        settle = bph.EPOCH_SETTLE_DAYS

    def epoch_of(day):
        return sum(1 for e in eps if day >= e)

    def in_settle(day):
        for e in eps:
            end = (dt.date.fromisoformat(e) + dt.timedelta(days=settle)).isoformat()
            if e <= day < end:
                return True
        return False

    current = epoch_of(days[-1])
    return [d for d in days if epoch_of(d) == current and not in_settle(d)]


def window_claim_returns(doc: dict, comparable: list) -> str:
    """The date on which lowest-ever / brand-discipline claims become sayable
    again, or '' if they already are.

    Stated rather than implied. A gate that closes silently is a feature that
    quietly stopped working; a gate that prints the day it opens is a schedule.
    """
    need = bph.MIN_DAYS_FOR_PRICE_CLAIM - len(comparable)
    if need <= 0:
        return ""
    days = doc.get("dayDates") or []
    if not days or not rd._date(days[-1]):
        return ""
    # The archive gains one snapshot a day (refresh-catalog.yml, 08:07 UTC), so
    # the shortfall is a day count. A missed day pushes it back; this is the
    # earliest it can be, and it is labelled as such wherever it is printed.
    return (dt.date.fromisoformat(days[-1]) + dt.timedelta(days=need)).isoformat()


def brand_open_fx(fxw: dict) -> set:
    """Brands with an FX correction window that is still open (toDay null).

    price_corrections.json's own statement about which of its rows it does not
    vouch for. record_digest refuses to call those moves a markdown; this file
    refuses to make any price claim about them at all, because a sheet has no
    equivalent of the push's per-move check.
    """
    return {b for b, (_lo, hi, _f) in fxw.items() if not hi}


def house_step_ratio(records: dict, catalog: dict, brand: str, change_idx: int):
    """(ratio, moved_rows, share) when this brand's pieces all stepped by ONE
    number on one day — the signature of arithmetic, not of a sale. None when the
    step is tiered, small, or absent. See the header for the measurement.
    """
    ratios = []
    for pid, rec in records.items():
        if (catalog.get(pid) or {}).get("brand") != brand:
            continue
        if not isinstance(rec, (list, tuple)) or len(rec) != 9:
            continue
        p, q, c = rec[4], rec[5], rec[6]
        if c != change_idx or not isinstance(q, (int, float)) or q <= 0 or p <= 0:
            continue
        ratios.append(round(float(p) / float(q) / HOUSE_STEP_RATIO_TOLERANCE)
                      * HOUSE_STEP_RATIO_TOLERANCE)
    if len(ratios) < HOUSE_STEP_MIN_ROWS:
        return None
    ratio, hits = collections.Counter(ratios).most_common(1)[0]
    share = hits / len(ratios)
    if share < HOUSE_STEP_SHARE:
        return None            # tiered — a real markdown structure
    if abs(1.0 - ratio) * 100 > FX_LOOKALIKE_MAX_PCT:
        return None            # too big to be any currency table we have seen
    return (ratio, len(ratios), share)


# ── Naming a piece ───────────────────────────────────────────────────────────

def piece_name(live: dict) -> str:
    """Brand + the shop's own product name, in full.

    The same two normalisations record_digest.piece_label() applies, and for the
    same reason — a name that already opens with the brand is not prefixed with
    it twice ("Oddli Oddli Baseball Tee"), and a pipe in a shop's own title reads
    as a broken template rather than as punctuation. What is NOT applied is that
    function's 46-character budget: a lock screen has to fit, a fact line does
    not, and the fact line is where the piece is identified.
    """
    brand = (live.get("brand") or "").strip()
    name = re.sub(r"\s+", " ", re.sub(r"\s*\|\s*", ", ", (live.get("name") or ""))).strip()
    if brand and name.lower().startswith(brand.lower()):
        return name
    return f"{brand} {name}".strip() or brand or "piece"


def noun_for(live: dict) -> str:
    return CATEGORY_NOUN.get((live.get("category") or "").strip().lower(), "piece")


def subject(live, with_brand=True):
    """(determiner, noun phrase, verb) that agree with each other.

    Returned as three pieces rather than one string because the hooks need them
    in different positions — "on these ALOHAS shoes" and "These ALOHAS shoes have
    been on the shelf 32 days" are the same phrase in two grammars.
    """
    noun = noun_for(live)
    brand = (live.get("brand") or "").strip()
    is_plural = noun in PLURAL_NOUNS
    core = f"{brand} {noun}".strip() if (with_brand and brand) else noun
    return ("these" if is_plural else "this"), core, ("have" if is_plural else "has")


def plural(n, word) -> str:
    return word if n == 1 else word + "s"


def published_date(live):
    """The shop's OWN listing date, or None. Never our first sighting — see
    NEW_PUBLISHED_MAX_DAYS for what that distinction cost."""
    return rd._date((live.get("publishedAt") or "")[:10])


def fit_hook(variants, fact) -> str:
    """The first hook that fits the cap and passes every rule, or ''.

    Variants are written longest-first and shed the brand name, never the number:
    a hook is a fact with a rhythm, and the fact is the part that cannot be cut.
    Nothing is ever truncated — a sentence cut at 90 characters is a different
    sentence, and this file has no way to know whether it is still true.
    """
    for h in variants:
        if hook_is_sound(h, fact):
            return h
    return ""


def shoot_format(kind: str, live: dict) -> str:
    cat = (live.get("category") or "").strip().lower()
    if kind == "drop":
        return "Two frames — the piece with the old price, then the new one. No voiceover needed."
    if kind == "lowest":
        return "Piece on screen, price as a text overlay held for the whole clip."
    if kind == "sizes":
        return "Piece on screen, the size count as a text overlay; cut on the number."
    if kind == "new":
        return ("Try-on" if cat in WEARABLE else "Piece on screen, slow pan") + \
               ", with the date it went up as a text overlay."
    if kind == "hold":
        return "Piece on screen + text overlay: the price, then the number of days it has held."
    return "Piece on screen + text overlay. The point is that there is no rush."


# ── Story construction ───────────────────────────────────────────────────────
# One function per kind. Each returns a story dict or None; None always means
# "the guard said no", never "something went wrong".

def _base(kind, pid, live, rec, extra=None):
    story = {
        "kind": kind,
        "kindLabel": KIND_LABEL[kind],
        "id": pid,
        "brand": (live.get("brand") or "").strip(),
        "piece": piece_name(live),
        "price": float(rec[4]),
        "category": (live.get("category") or "").strip(),
        "retailer": live.get("retailer") or "",
        "url": product_url(pid),
        "image": live.get("imageUrl") or (live.get("images") or [None])[0] or "",
        "format": shoot_format(kind, live),
    }
    story.update(extra or {})
    # Every fact line opens by naming and pricing the piece, which is right on a
    # story block and is said twice on a list post, where the entry is already
    # headed with the name and the price. `detail` is the same sentence without
    # its lead-in — derived from the fact rather than written separately, so the
    # two can never say different things.
    lead_a = f"{story['piece']}, {money(story['price'])}. "
    lead_b = f"{story['piece']}. "
    text = story.get("fact", "")
    story["detail"] = (text[len(lead_a):] if text.startswith(lead_a)
                       else text[len(lead_b):] if text.startswith(lead_b) else text)
    return story


def story_drop(pid, live, rec, ctx, doc, records, catalog, fxw, open_fx, reasons):
    """Story 1 — the biggest CONFIRMED markdown of the week."""
    f, n, lo, hi, p, q, c, sn, sx = rec
    brand = (live.get("brand") or "").strip()
    last_idx = len(ctx["dayDates"]) - 1
    change_day = rd.day_at(ctx, c) if isinstance(c, int) and c >= 0 else None
    prev_day = rd.day_at(ctx, c - 1) if isinstance(c, int) and c >= 1 else None

    if brand in open_fx:
        reasons["brand_has_an_open_fx_correction"] += 1
        return None
    try:
        live_price = float(live.get("price"))
    except (TypeError, ValueError):
        reasons["live_price_unreadable"] += 1
        return None
    if abs(live_price - float(p)) > rd.PRICE_MATCH_SLACK:
        reasons["price_moved_since_the_snapshot"] += 1
        return None
    age = rd.days_between(change_day, ctx["today"]) if change_day else None
    if age is None or not (0 <= age <= rd.MOVE_RECENT_DAYS):
        reasons["move_older_than_a_week"] += 1
        return None
    if c > last_idx - rd.MOVE_MIN_CONFIRM_SNAPSHOTS:
        reasons["move_seen_in_only_one_snapshot"] += 1
        return None
    if rd.move_is_void(prev_day, change_day, ctx):
        reasons["move_spans_a_price_epoch"] += 1
        return None
    if q > 0 and rd.fx_suppressed(brand, p, q, change_day, fxw):
        reasons["move_matches_a_published_fx_step"] += 1
        return None
    if float(q) - float(p) < rd.MIN_DROP_ABS:
        reasons["below_the_dollar_floor"] += 1
        return None
    house = house_step_ratio(records, catalog, brand, c)
    if house:
        reasons["whole_label_stepped_by_one_ratio"] += 1
        return None

    pct = bph._pct_drop(float(q), float(p))
    when = rd.fmt_day(change_day, ctx["today"])
    then = rd.fmt_day(prev_day, ctx["today"]) if prev_day else ""
    if not when or not then:
        reasons["move_could_not_be_dated"] += 1
        return None

    moved = sum(1 for pid2, r in records.items()
                if isinstance(r, (list, tuple)) and len(r) == 9 and r[6] == c
                and (catalog.get(pid2) or {}).get("brand") == brand)
    tracked = (doc.get("brands", {}).get(brand) or [0])[0]
    if moved > 1 and tracked:
        shape = (f"{moved} of the {tracked} pieces we track from this label moved the "
                 f"same day, so it is a house-wide markdown rather than one piece.")
    elif moved > 1:
        shape = f"{moved} pieces from this label moved the same day."
    else:
        shape = "No other piece from this label moved that day."

    fact = (f"{piece_name(live)}. {money(q)} on {then}, {money(p)} on {when} — a "
            f"{pct}% cut, still there in a later snapshot and still {money(p)} in "
            f"today's feed. {shape}")
    det, phrase, _v = subject(live, with_brand=False)
    hook = fit_hook([
        f"{brand} cut {det} {phrase} {pct}% on {when}.",
        f"{det.capitalize()} {phrase} fell {pct}% on {when}.",
        f"{pct}% off {det} {phrase} since {when}.",
    ], fact)
    if not hook:
        reasons["copy_refused"] += 1
        return None
    return _base("drop", pid, live, rec, {
        "title": f"{brand} cut {piece_name(live)} by {pct}%",
        "hook": hook, "fact": fact,
        "sort": (-pct, -float(q), pid),
        "measure": {"pct": pct, "from": float(q), "to": float(p),
                    "changedOn": change_day, "labelPiecesMoved": moved},
    })


def story_lowest(pid, live, rec, ctx, doc, records, catalog, open_fx, n_comp, reasons):
    """Story 2 — the lowest price on our record. A WINDOW claim, so it is only
    available when the comparable window is wide enough to be one."""
    f, n, lo, hi, p, q, c, sn, sx = rec
    brand = (live.get("brand") or "").strip()
    if brand in open_fx:
        reasons["brand_has_an_open_fx_correction"] += 1
        return None
    try:
        live_price = float(live.get("price"))
    except (TypeError, ValueError):
        reasons["live_price_unreadable"] += 1
        return None
    if abs(live_price - float(p)) > rd.PRICE_MATCH_SLACK:
        reasons["price_moved_since_the_snapshot"] += 1
        return None
    if isinstance(c, int) and c >= 0 and house_step_ratio(records, catalog, brand, c):
        reasons["whole_label_stepped_by_one_ratio"] += 1
        return None

    # The range was measured over the days this piece was seen AND comparable.
    # We do not have that intersection, so the claim is made over the smaller of
    # the two — an understatement is the only safe direction for a window.
    days = min(int(n), n_comp)
    spread = bph._pct_drop(float(hi), float(lo))
    fact = (f"{piece_name(live)}. Across the last {days} days we can compare "
            f"prices over, our record has it between {money(lo)} and {money(hi)}; "
            f"it is {money(p)} in today's feed, at the bottom of that range — a "
            f"{spread}% spread.")
    det, phrase, _v = subject(live)
    _d2, bare, _v2 = subject(live, with_brand=False)
    hook = fit_hook([
        f"{money(p)}, the lowest we have logged for {det} {phrase} in {days} days.",
        f"{money(p)} is the lowest we have logged for {det} {bare} in {days} days.",
        f"Lowest in {days} days on our record: {det} {bare}, {money(p)}.",
    ], fact)
    if not hook:
        reasons["copy_refused"] += 1
        return None
    return _base("lowest", pid, live, rec, {
        "title": f"{piece_name(live)} at the lowest price on our record",
        "hook": hook, "fact": fact,
        "sort": (-spread, -float(p), pid),
        "measure": {"low": float(lo), "high": float(hi), "now": float(p),
                    "spreadPct": spread, "comparableDays": days},
    })


def story_sizes(pid, live, rec, ctx, catalog_day, reasons):
    """Story 3 — about to sell out. The only story here that touches no price at
    all, which is why it survives an epoch that voids everything else."""
    f, n, lo, hi, p, q, c, sn, sx = rec
    live_sizes = rd.pdp.norm_sizes(live.get("sizes"))
    if len(live_sizes) != sn:
        # The record says one thing and the shop says another. Either could be
        # right; neither is publishable.
        reasons["size_run_moved_since_the_snapshot"] += 1
        return None
    brand = (live.get("brand") or "").strip()
    gone = int(sx) - int(sn)
    fact = (f"{piece_name(live)}, {money(p)}. We have seen this piece in {n} daily "
            f"snapshots and the widest in-stock size run we recorded was {sx}; "
            f"{sn} {plural(sn, 'size')} {'is' if sn == 1 else 'are'} left "
            f"({', '.join(live_sizes)}) as of {catalog_day}. {gone} have gone.")
    det, phrase, _v = subject(live)
    _d2, bare, _v2 = subject(live, with_brand=False)
    hook = fit_hook([
        f"Down to {sn} {plural(sn, 'size')} from {sx} on {det} {phrase}.",
        f"Down to {sn} {plural(sn, 'size')} from {sx} on {det} {bare}.",
        f"{sn} of {sx} sizes left on {det} {bare}.",
    ], fact)
    if not hook:
        reasons["copy_refused"] += 1
        return None
    return _base("sizes", pid, live, rec, {
        "title": f"{piece_name(live)} — down to {sn} of {sx} sizes",
        "hook": hook, "fact": fact,
        "sort": (int(sn), -int(sx), -gone, pid),
        "measure": {"sizesNow": int(sn), "sizesMax": int(sx), "gone": gone,
                    "sizesLeft": live_sizes},
    })


def story_new(pid, live, rec, ctx, doc, discipline, reasons):
    """Story 4 — new this week from a label with a strong record."""
    f, n, lo, hi, p, q, c, sn, sx = rec
    brand = (live.get("brand") or "").strip()
    stat = doc.get("brands", {}).get(brand)
    if not stat:
        # No brand row means fewer than BRAND_MIN_TRACKED pieces tracked, which is
        # exactly the case where "a label with a strong record" is unevidenced.
        reasons["label_has_too_little_record"] += 1
        return None
    tracked, ever, median_hold = stat[0], stat[1], stat[2]
    first_day = rd.day_at(ctx, f)
    age = rd.days_between(first_day, ctx["today"]) if first_day else None
    if age is None or age < 0:
        reasons["arrival_could_not_be_dated"] += 1
        return None
    when = rd.fmt_day(first_day, ctx["today"])

    # THE ROTATION CHECK. Our first sighting is a fact about the 60-per-brand
    # deck; the shop's published_at is a fact about the shop. Only the second one
    # supports "the brand put this up N days ago", so the second one is the gate
    # AND the number in the sentence. See NEW_PUBLISHED_MAX_DAYS.
    pub = published_date(live)
    if pub is None:
        reasons["store_publishes_no_listing_date"] += 1
        return None
    pub_age = (dt.date.fromisoformat(ctx["today"]) - pub).days
    if not 1 <= pub_age <= NEW_PUBLISHED_MAX_DAYS:
        # Either the shop says it is not new (a rotation into our deck), or it
        # went up today and there is no whole number of days to quote yet.
        reasons["store_listing_date_is_not_this_week"] += 1
        return None
    pub_when = rd.fmt_day(pub.isoformat(), ctx["today"])

    clause = discipline(brand)
    strength = clause or (
        f"We track {tracked} pieces from this label and the median one has been in "
        f"{median_hold} of our daily snapshots.")
    fact = (f"{piece_name(live)}, {money(p)}. The shop's own listing date is "
            f"{pub_when} — {pub_age} days ago; it first reached our daily snapshot "
            f"on {when}. {strength}")
    det, bare, _v = subject(live, with_brand=False)
    hook = fit_hook([
        f"{brand} put {det} {bare} up {pub_age} days ago. {money(p)}.",
        f"{brand} listed {det} {bare} {pub_age} days ago. {money(p)}.",
        f"Listed {pub_age} days ago at {brand}. {money(p)}.",
    ], fact)
    if not hook:
        reasons["copy_refused"] += 1
        return None
    return _base("new", pid, live, rec, {
        "title": f"New at {brand}: {piece_name(live)}",
        "hook": hook, "fact": fact,
        "sort": (-int(median_hold), -int(tracked), pid),
        "measure": {"firstSeen": first_day, "ageDays": age,
                    "publishedAt": pub.isoformat(), "publishedDaysAgo": pub_age,
                    "brandTracked": int(tracked), "brandMedianHold": int(median_hold),
                    # Only meaningful when the clause was sayable at all — see the
                    # header. The flag travels with the number so the list builder
                    # cannot filter on a percentage nobody was allowed to quote.
                    "brandDisciplineKnown": bool(clause),
                    "brandDiscountPct": int(round(100.0 * ever / tracked)) if tracked else None},
    })


def story_hold(pid, live, rec, ctx, doc, open_fx, discipline, reasons):
    """Story 5 — a price that has held, at a label that does not mark down.

    Both halves are WINDOW claims (the price has not moved over the window; the
    label discounts N% of the pieces we track over the window), so this whole
    kind is unavailable until the comparable window is long enough — see the
    header. `discipline` returns '' when it is not, and that is the gate.
    """
    f, n, lo, hi, p, q, c, sn, sx = rec
    brand = (live.get("brand") or "").strip()
    if brand in open_fx:
        reasons["brand_has_an_open_fx_correction"] += 1
        return None
    if n < HOLD_MIN_DAYS:
        reasons["not_held_long_enough"] += 1
        return None
    if not (q == 0 and c == -1 and float(hi) <= float(lo) * (1 + bph.MIN_MEANINGFUL_MOVE)):
        reasons["price_moved_in_the_window"] += 1
        return None
    stat = doc.get("brands", {}).get(brand)
    if not stat or bph.brand_line_kind(stat) != "brand_rarely":
        reasons["label_is_not_a_full_price_label"] += 1
        return None
    try:
        if abs(float(live.get("price")) - float(p)) > rd.PRICE_MATCH_SLACK:
            reasons["price_moved_since_the_snapshot"] += 1
            return None
    except (TypeError, ValueError):
        reasons["live_price_unreadable"] += 1
        return None

    clause = discipline(brand)
    if not clause:
        reasons["brand_discipline_not_measurable"] += 1
        return None
    pct = int(round(100.0 * stat[1] / stat[0]))
    fact = (f"{piece_name(live)}. {money(p)} in every one of the {n} daily snapshots "
            f"we have seen it in, with no move inside the window we can compare "
            f"prices over. {clause}")
    det, bare, _v = subject(live, with_brand=False)
    hook = fit_hook([
        f"{money(p)} for {n} days. This label marks down {pct}% of what we track.",
        f"{money(p)}, unmoved for {n} days at a label that marks down {pct}%.",
        f"Same {money(p)} for {n} days on {det} {bare}.",
    ], fact)
    if not hook:
        reasons["copy_refused"] += 1
        return None
    return _base("hold", pid, live, rec, {
        "title": f"{brand} has not moved this price in {n} days",
        "hook": hook, "fact": fact,
        "sort": (-int(n), pid),
        "measure": {"daysSeen": int(n), "brandDiscountPct": pct,
                    "brandTracked": int(stat[0])},
    })


def story_tenure(pid, live, rec, ctx, reasons):
    """The honest stand-in for story 5 while the price window is short.

    Same editorial point — you are not in a race — from a claim that survives an
    epoch: how long the piece has been on the shelf, counted from the day it
    first appeared, and only for pieces whose first appearance is a real arrival
    (record_kinds enforces tenureTrustedFrom).
    """
    f, n, lo, hi, p, q, c, sn, sx = rec
    brand = (live.get("brand") or "").strip()
    first_day = rd.day_at(ctx, f)
    age = rd.days_between(first_day, ctx["today"]) if first_day else None
    if age is None or age < bph.MIN_TENURE_DAYS:
        reasons["not_on_the_shelf_long_enough"] += 1
        return None
    when = rd.fmt_day(first_day, ctx["today"])

    # The same rotation check as story_new, pointing the other way. Our count is
    # only a FLOOR on how long the piece has been for sale if the shop listed it
    # no later than we first saw it — which is also what makes the understatement
    # safe. A piece the shop published after our first sighting is a contradiction
    # we do not publish either half of.
    pub = published_date(live)
    if pub is None or pub.isoformat() > first_day:
        reasons["listing_date_contradicts_our_first_sighting"] += 1
        return None

    sizes_clause = (f"{sn} {plural(sn, 'size')} in stock." if sn else
                    "The shop lists no sizes for it.")
    fact = (f"{piece_name(live)}, {money(p)}. The shop listed it on "
            f"{rd.fmt_day(pub.isoformat(), ctx['today'])}; it has been in our daily "
            f"snapshot since {when}, {age} days ago, and it is still there. "
            f"{sizes_clause}")
    det, phrase, verb = subject(live)
    _d, bare, _v = subject(live, with_brand=False)
    hook = fit_hook([
        f"{det.capitalize()} {phrase} {verb} been on the shelf {age} days.",
        f"{det.capitalize()} {bare} {verb} been sitting there {age} days.",
        f"On the shelf {age} days and counting.",
    ], fact)
    if not hook:
        reasons["copy_refused"] += 1
        return None
    return _base("tenure", pid, live, rec, {
        "title": f"{piece_name(live)} has been on the shelf {age} days",
        "hook": hook, "fact": fact,
        "sort": (-age, pid),
        "measure": {"firstSeen": first_day, "ageDays": age, "sizesNow": int(sn),
                    "publishedAt": pub.isoformat()},
    })


# ── Assembly ─────────────────────────────────────────────────────────────────

def chicago_retailers(brands_doc: dict) -> dict:
    """{retailerId: 'Gemini, 1911 W. Division St., Chicago'} for every enabled
    partner shop with a Chicago address.

    Read off brands.json's `retailers` block, which is the only place in the
    roster that carries a city at all: the 185 LABEL entries have brand, domain
    and currency and no location field, so a Chicago-based *label* cannot be
    identified from this data and the sheet does not pretend otherwise. What it
    can say truthfully is that a piece is stocked by a Chicago shop.
    """
    out = {}
    for r in brands_doc.get("retailers") or []:
        if not isinstance(r, dict) or not r.get("enabled"):
            continue
        store = r.get("store") or {}
        if (store.get("city") or "").strip().lower() != "chicago":
            continue
        name = store.get("name") or r.get("name") or r.get("id")
        where = store.get("address") or "Chicago"
        out[r.get("id")] = f"{name}, {where}"
    return out


def gather(doc, catalog, ctx, fxw, catalog_day):
    """Every publishable story, grouped by kind, best first."""
    records = doc.get("records") or {}
    comparable = comparable_days(doc)
    n_comp = len(comparable)
    window_ok = n_comp >= bph.MIN_DAYS_FOR_PRICE_CLAIM
    open_fx = brand_open_fx(fxw)
    reasons = collections.Counter()

    def discipline(brand):
        """The brand's price discipline as one clause, or '' when we cannot
        measure it. See the header: everDiscounted is counted inside the
        comparable window, so on a short window it is not a fact about the label."""
        if not window_ok:
            return ""
        stat = doc.get("brands", {}).get(brand)
        if not stat or stat[0] < bph.BRAND_MIN_TRACKED:
            return ""
        pct = int(round(100.0 * stat[1] / stat[0]))
        return (f"This label has marked down {pct}% of the {stat[0]} pieces we "
                f"track from it.")

    out = {k: [] for k in KIND_ORDER}
    for pid, rec in records.items():
        live = catalog.get(pid)
        if not has_live_page(live):
            reasons["no_live_product_page"] += 1
            continue
        # Shape FIRST, and imported rather than restated: several of the story
        # functions below unpack the row into nine names before they validate
        # anything, exactly as build_price_history.record_kinds() does, so a row
        # of the wrong length raises there instead of returning silence. The
        # published file is data, not a promise — and a fixture proved it: a
        # three-element row took the whole build down with a ValueError.
        if not rd.well_formed_row(rec):
            reasons["malformed_record_row"] += 1
            continue
        kinds = bph.record_kinds(rec, ctx)
        # `hold` is checked OUTSIDE this gate on purpose. Every other story is a
        # line record_kinds() already recognises; a price that has never moved
        # earns no line at all — that is what makes it the story — so gating it
        # on `kinds` made the whole kind unreachable.
        if window_ok:
            s = story_hold(pid, live, rec, ctx, doc, open_fx, discipline, reasons)
            if s:
                out["hold"].append(s)
        if not kinds:
            continue
        if "drop" in kinds:
            s = story_drop(pid, live, rec, ctx, doc, records, catalog, fxw, open_fx, reasons)
            if s:
                out["drop"].append(s)
        if "lowest" in kinds and window_ok:
            s = story_lowest(pid, live, rec, ctx, doc, records, catalog, open_fx,
                             n_comp, reasons)
            if s:
                out["lowest"].append(s)
        elif "lowest" in kinds:
            reasons["price_window_too_short_for_a_lowest_claim"] += 1
        if "sizes" in kinds:
            s = story_sizes(pid, live, rec, ctx, catalog_day, reasons)
            if s:
                out["sizes"].append(s)
        if "new" in kinds:
            s = story_new(pid, live, rec, ctx, doc, discipline, reasons)
            if s:
                out["new"].append(s)
        if "tenure" in kinds:
            s = story_tenure(pid, live, rec, ctx, reasons)
            if s:
                out["tenure"].append(s)

    for k in out:
        out[k].sort(key=lambda s: s["sort"])
    return out, reasons, {
        "comparableDays": n_comp,
        "comparableFrom": comparable[0] if comparable else "",
        "comparableTo": comparable[-1] if comparable else "",
        "windowClaimsAllowed": window_ok,
        "windowClaimsReturn": window_claim_returns(doc, comparable),
        "openFxBrands": sorted(open_fx),
    }


def select(by_kind, want=STORY_COUNT, chicago_ids=()):
    """Ten stories, at most one per label, priority-first with breadth.

    Round-robin down KIND_ORDER rather than draining it: taking the ten best by
    score would be ten pieces from whichever kind happened to be plentiful, and
    the sheet is a shooting schedule — ten variations of the same shot is not one.
    """
    cursors = {k: 0 for k in by_kind}
    used_brands, picked = set(), []
    while len(picked) < want:
        progressed = False
        for k in KIND_ORDER:
            if len(picked) >= want:
                break
            lst = by_kind.get(k) or []
            i = cursors[k]
            while i < len(lst) and lst[i]["brand"] in used_brands:
                i += 1
            cursors[k] = i + 1 if i < len(lst) else i
            if i < len(lst):
                picked.append(lst[i])
                used_brands.add(lst[i]["brand"])
                progressed = True
        if not progressed:
            break

    return ensure_chicago(picked, by_kind, chicago_ids, want)


def ensure_chicago(picked, by_kind, chicago_ids, want=STORY_COUNT):
    """Guarantee one Chicago story when the roster has a Chicago shop.

    It REPLACES rather than appends — ten is a promise, not a target — and it
    considers every slot, not just the last one. The first real sheet is why: the
    best Chicago piece was a Damson Madder cami stocked by Gemini, a Damson Madder
    top already held slot 9, and a swap that could only drop slot 10 quietly
    produced no Chicago story at all while reporting success.
    """
    if not chicago_ids or any(s["retailer"] in chicago_ids for s in picked):
        return picked
    chosen = {s["id"] for s in picked}
    for kind in KIND_ORDER:                      # best available, priority-first
        for cand in by_kind.get(kind) or []:
            if cand["retailer"] not in chicago_ids or cand["id"] in chosen:
                continue
            if len(picked) < want and cand["brand"] not in {s["brand"] for s in picked}:
                picked.append(cand)
                return picked
            # The LAST slot whose removal frees this label — i.e. the
            # lowest-priority story we can afford to give up.
            for i in range(len(picked) - 1, -1, -1):
                if cand["brand"] not in {s["brand"] for j, s in enumerate(picked) if j != i}:
                    picked[i] = cand
                    return picked
    return picked


# ── The three list posts ─────────────────────────────────────────────────────
# Each has a primary definition and a declared alternate. The alternate is not a
# fallback that fudges the primary — it is a DIFFERENT true list with its own
# title, used when the primary cannot be filled, and the sheet says which it is
# looking at. The one thing that never happens is the primary's title over the
# alternate's pieces.

def _under(stories, cap):
    return [s for s in stories if s["price"] < cap]


def _spread(stories, want, max_per_brand=LIST_MAX_PER_BRAND):
    """The first `want` stories, at most `max_per_brand` from any one label.

    Without it the first real sheet's under-$150 list was five Cou Cou Intimates
    pieces and its new-arrivals list was five Musier Paris pieces — five true
    entries that add up to one post about one brand.
    """
    out, seen = [], collections.Counter()
    for s in stories:
        if seen[s["brand"]] >= max_per_brand:
            continue
        out.append(s)
        seen[s["brand"]] += 1
        if len(out) >= want:
            break
    return out


def build_lists(by_kind, gates):
    def spec(title, pick, want, why):
        return {"title": title, "want": want, "items": _spread(pick(), want),
                "why": why}

    lists = []

    # 1 — the price list.
    primary = spec("5 pieces under $150 that dropped this week",
                   lambda: _under(by_kind.get("drop") or [], 150), 5,
                   "confirmed markdowns, priced under $150")
    if len(primary["items"]) < primary["want"]:
        alt = spec("5 pieces under $150 with one size left",
                   lambda: [s for s in _under(by_kind.get("sizes") or [], 150)
                            if s["measure"]["sizesNow"] == 1], 5,
                   "one in-stock size left, priced under $150")
        alt["swappedFrom"] = primary["title"]
        alt["swapReason"] = (
            "No confirmed markdown under $150 this week"
            + (f" — every dated move in the archive is still unconfirmed "
               f"({gates['comparableDays']} comparable snapshots)."
               if gates["comparableDays"] < 3 else "."))
        lists.append(alt if alt["items"] else primary)
    else:
        lists.append(primary)

    # 2 — the dresses list.
    lists.append(spec("The 4 dresses about to sell out",
                      lambda: [s for s in (by_kind.get("sizes") or [])
                               if s["category"] == "dresses"], 4,
                      "dresses down to one or two in-stock sizes from a run of four or more"))

    # 3 — the new-arrivals list.
    primary3 = spec("New this week from labels that never discount",
                    lambda: [s for s in (by_kind.get("new") or [])
                             if s["measure"].get("brandDisciplineKnown")
                             and (s["measure"].get("brandDiscountPct") or 0)
                             <= bph.BRAND_RARELY_PCT], 5,
                    f"arrived in the last seven days, from a label that has marked "
                    f"down {bph.BRAND_RARELY_PCT}% or less of what we track")
    if len(primary3["items"]) < 3:
        alt3 = spec("New this week from the labels whose pieces stay longest",
                    lambda: by_kind.get("new") or [], 5,
                    "arrived in the last seven days, ranked by how many of our daily "
                    "snapshots the label's median piece survives")
        alt3["swappedFrom"] = primary3["title"]
        alt3["swapReason"] = (
            "Brand price discipline is not measurable this week: prices can only be "
            f"compared over {gates['comparableDays']} snapshots, so \"never "
            "discounts\" would be a claim about "
            f"{gates['comparableDays']} days rather than about the label.")
        lists.append(alt3 if alt3["items"] else primary3)
    else:
        lists.append(primary3)

    return lists


# ── Rendering ────────────────────────────────────────────────────────────────

DO_NOT_SAY = [
    ("AI", "We do not have one and it is not what this is. The value is that a "
           "person kept a record."),
    ("algorithm", "Same reason. Nothing on this sheet was decided by one."),
    ("recommend / recommended for you", "We are reporting what a shop did, not "
                                        "telling anyone what to buy."),
    ("personalised / personalized", "None of these numbers are about the viewer."),
    ("stunning, must-have, obsessed, iconic, insane",
     "Hype is what everyone else has. A number is what we have."),
    ("!", "No exclamation marks. The number does the work."),
]


def header_lines(doc, gates, by_kind, counts, week, today):
    """Three lines. Every figure is measured, including the ones that are zero."""
    moved = counts["moved_rows"]
    confirmed = counts["confirmed_moves"]
    n_rec = len(doc.get("records") or {})
    n_brands = len(doc.get("brands") or {})

    l1 = (f"Week of {pretty_day(week)} — built {today} from {doc.get('days')} daily "
          f"snapshots ({doc.get('windowStart')} to {doc.get('windowEnd')}), "
          f"{n_rec:,} pieces with a price record across {n_brands} labels.")

    if moved == 0:
        l2 = ("No piece changed price inside the window we can compare prices over, "
              "so there is no markdown to report this week.")
    elif confirmed == 0:
        l2 = (f"{moved} pieces changed price in the comparable window and none of "
              f"those changes has yet been seen in a second snapshot, so none of "
              f"them is reportable.")
    else:
        best = (by_kind.get("drop") or [None])[0]
        l2 = (f"{moved} pieces changed price and {confirmed} of those moves are "
              f"confirmed by a later snapshot"
              + (f"; the biggest is {best['measure']['pct']}% at {best['brand']}."
                 if best else "."))

    n_sizes = len(by_kind.get("sizes") or [])
    n_new = len(by_kind.get("new") or [])
    if gates["windowClaimsAllowed"]:
        l3 = (f"{n_sizes} pieces are down to one or two in-stock sizes from a run of "
              f"four or more, and {n_new} were listed by their own shop in the last "
              f"{bph.NEW_ARRIVAL_DAYS} days.")
    else:
        l3 = (f"Prices can only be compared over {gates['comparableDays']} snapshots "
              f"this week, so every price story is held; {n_sizes} pieces down to one "
              f"or two sizes and {n_new} listed by their shop in the last "
              f"{bph.NEW_ARRIVAL_DAYS} days are what the record supports today.")
    return [l1, l2, l3]


def render_md(sheet) -> str:
    g = sheet["gates"]
    out = []
    out.append(f"# Loupe content sheet — week of {pretty_day(sheet['weekOf'])}")
    out.append("")
    for line in sheet["header"]:
        out.append(line)
        out.append("")
    out.append("---")
    out.append("")

    if not sheet["stories"]:
        out.append("## A quiet week")
        out.append("")
        out.append("Nothing on the record earns a post this week. That is a real "
                   "outcome and not a broken build — the sheet would rather be "
                   "empty than invented.")
        out.append("")
        for note in sheet["notes"]:
            out.append(f"- {note}")
        out.append("")
    else:
        out.append(f"## {len(sheet['stories'])} stories")
        out.append("")
        for i, s in enumerate(sheet["stories"], 1):
            out.append(f"### {i}. {s['title']}")
            out.append("")
            out.append(f"- **Hook** ({len(s['hook'])} characters) — {s['hook']}")
            out.append(f"- **Fact** — {s['fact']}")
            out.append(f"- **Link** — {s['url']}")
            if s.get("brandRecord"):
                out.append(f"- **Label** — {s['brandRecord']}")
            if s.get("chicago"):
                out.append(f"- **Chicago** — {s['chicago']}")
            out.append(f"- **Format** — {s['format']}")
            out.append(f"- **Image** — {s['image']}")
            out.append(f"- **Type** — {s['kindLabel']}")
            out.append("")

    out.append("---")
    out.append("")
    out.append("## List posts")
    out.append("")
    for lst in sheet["lists"]:
        out.append(f"### {lst['title']}")
        out.append("")
        if lst.get("swappedFrom"):
            out.append(f"*Swapped in for \"{lst['swappedFrom']}\". "
                       f"{lst['swapReason']}*")
            out.append("")
        out.append(f"Definition: {lst['why']}.")
        out.append("")
        if not lst["items"]:
            out.append("Nothing qualified this week. Nothing has been substituted.")
            out.append("")
            continue
        for s in lst["items"]:
            out.append(f"- **{s['piece']}** — {money(s['price'])}. {s['detail']}")
            out.append(f"  - {s['url']}")
        out.append("")

    out.append("---")
    out.append("")
    out.append("## Do not say")
    out.append("")
    for word, why in DO_NOT_SAY:
        out.append(f"- **{word}** — {why}")
    out.append("")
    out.append("**And the rule underneath all of them: every number that appears in "
               "a caption must appear in that story's fact line.** The fact line is "
               "where it was measured. If a number is not there, it is not ours and "
               "it does not go on screen.")
    out.append("")
    out.append("---")
    out.append("")
    out.append("## What this sheet could not say this week")
    out.append("")
    for note in sheet["notes"]:
        out.append(f"- {note}")
    out.append("")
    out.append("<sub>Built by loupe-feed/build_content_sheet.py from "
               f"price_records.json ({sheet['source']['priceRecords']['generatedAt']}) "
               f"and catalog.json ({sheet['source']['catalog']['generatedAt']}). "
               f"Prices can be compared over {g['comparableDays']} snapshots "
               f"({g['comparableFrom']} to {g['comparableTo']}). No inventory data "
               "(stock.json) is used: naming a brand's stock is forbidden by that "
               "file's own use policy.</sub>")
    out.append("")
    return "\n".join(out)


def summary_markdown(machine: dict) -> str:
    """The run-page summary, from a written latest.json.

    Lives here rather than in the workflow because a shell heredoc nested inside
    a YAML block scalar is a syntax error waiting to happen, and because this is
    the one place a human looks to see what the week produced — which makes it
    worth testing.
    """
    out = [f"**Week of {machine.get('weekOf')}** — {len(machine.get('stories') or [])} "
           f"stories, {sum(len(l['items']) for l in machine.get('lists') or [])} "
           f"list pieces", ""]
    for line in machine.get("header") or []:
        out += [f"> {line}", ""]
    stories = machine.get("stories") or []
    if stories:
        out += ["| # | hook | chars | type | label |",
                "|--:|------|------:|------|-------|"]
        for i, s in enumerate(stories, 1):
            hook = s["hook"].replace("|", "\\|")
            out.append(f"| {i} | {hook} | {len(s['hook'])} | {s['kindLabel']} "
                       f"| {s['brand']} |")
    else:
        out.append("_A quiet week: nothing on the record earned a post._")
    out += ["", "### List posts", ""]
    for lst in machine.get("lists") or []:
        swapped = (f" _(swapped in for \"{lst['swappedFrom']}\")_"
                   if lst.get("swappedFrom") else "")
        out.append(f"- **{lst['title']}** — {len(lst['items'])} pieces{swapped}")
    out += ["", "### What it could not say", ""]
    for note in machine.get("notes") or []:
        out.append(f"- {note}")
    return "\n".join(out)


def build_sheet(records_path=RECORDS, catalog_path=CATALOG, brands_path=BRANDS,
                today=None):
    today = today or dt.date.today().isoformat()
    week = monday_of(today)

    # Loaded FIRST and a hard stop when missing — the same order and the same
    # reason as record_digest.main(). A sheet built without the FX table would
    # publish a currency repair as somebody's sale.
    corrections_doc = json.loads(rd.pdp.CORRECTIONS.read_text(encoding="utf-8")) \
        if rd.pdp.CORRECTIONS.exists() else None
    if corrections_doc is None:
        rd.pdp.load_price_corrections()          # exits with the explanation
    fxw = rd.fx_windows(corrections_doc)

    doc = load_json(records_path)
    if not isinstance(doc.get("records"), dict):
        sys.exit(f"REFUSING TO BUILD: {records_path} is not a price_records document.")
    cat_doc = load_json(catalog_path)
    brands_doc = load_json(brands_path)

    catalog = {}
    for p in cat_doc.get("products") or []:
        pid = p.get("id")
        if isinstance(pid, str) and pid:
            catalog[pid] = p
    ctx = rd.build_ctx(doc, today)

    notes = []
    stale = rd.records_are_stale(ctx)
    if stale:
        notes.append(
            f"The archive's last snapshot is {doc.get('windowEnd')} and today is "
            f"{today}, more than {ctx['maxStaleDays']} days apart. Nothing dated can "
            "be claimed off a stale file, so no story was built. Check "
            "refresh-catalog.yml.")
        by_kind, reasons = {k: [] for k in KIND_ORDER}, collections.Counter()
        gates = {"comparableDays": 0, "comparableFrom": "", "comparableTo": "",
                 "windowClaimsAllowed": False, "windowClaimsReturn": "",
                 "openFxBrands": sorted(brand_open_fx(fxw)), "archiveStale": True}
    else:
        # The size line is cross-checked against the LIVE catalog, so it is dated
        # by the catalog's own stamp rather than by the archive's last snapshot —
        # they are normally the same run, and when they are not, the number came
        # from the newer of the two.
        catalog_day = str(cat_doc.get("generatedAt") or "")[:10] or doc.get("windowEnd") or today
        by_kind, reasons, gates = gather(doc, catalog, ctx, fxw, catalog_day)
        gates["archiveStale"] = False

    chicago = chicago_retailers(brands_doc)
    stories = select(by_kind, STORY_COUNT, set(chicago))

    # The clauses that depend on the whole run, attached once the ten are known.
    def discipline_clause(brand):
        if not gates["windowClaimsAllowed"]:
            return ""
        stat = doc.get("brands", {}).get(brand)
        if not stat or stat[0] < bph.BRAND_MIN_TRACKED:
            return ""
        pct = int(round(100.0 * stat[1] / stat[0]))
        return (f"This label has marked down {pct}% of the {stat[0]} pieces we track "
                f"from it.")

    for s in stories:
        s["brandRecord"] = discipline_clause(s["brand"])
        s["chicago"] = (f"In stock at {chicago[s['retailer']]}"
                        if s["retailer"] in chicago else "")

    lists = build_lists(by_kind, gates)

    moved_rows = sum(1 for r in doc["records"].values()
                     if isinstance(r, (list, tuple)) and len(r) == 9
                     and isinstance(r[6], int) and r[6] >= 0)
    last_idx = len(ctx["dayDates"]) - 1
    confirmed = sum(1 for r in doc["records"].values()
                    if isinstance(r, (list, tuple)) and len(r) == 9
                    and isinstance(r[6], int) and 0 <= r[6] <= last_idx - rd.MOVE_MIN_CONFIRM_SNAPSHOTS)
    counts = {"moved_rows": moved_rows, "confirmed_moves": confirmed}

    # ── The honest notes ────────────────────────────────────────────────────
    if not gates["archiveStale"]:
        if not gates["windowClaimsAllowed"]:
            notes.append(
                f"Lowest-ever prices, brand price discipline (\"this label discounts "
                f"N%\") and \"this price has held\" are all HELD this week. "
                f"price_records.json measures those inside the current pricing epoch, "
                f"and the {ctx['priceEpochs'][-1] if ctx['priceEpochs'] else '?'} "
                f"change plus its {ctx['epochSettleDays']}-day settle leaves only "
                f"{gates['comparableDays']} comparable snapshots "
                f"({gates['comparableFrom']} to {gates['comparableTo']}). Quoting "
                f"them would be a statement about {gates['comparableDays']} days "
                f"dressed up as one about "
                f"{doc.get('days')}. They come back on "
                f"{gates['windowClaimsReturn']} at the earliest.")
        if moved_rows and not confirmed:
            notes.append(
                f"All {moved_rows} dated price moves in the archive land on the last "
                f"snapshot, so none has been seen twice yet. A move becomes "
                f"reportable on the first later snapshot that still shows the new "
                f"price (MOVE_MIN_CONFIRM_SNAPSHOTS); a move that reverts overnight "
                f"never earns a post, which is the point of the rule.")
        if gates["openFxBrands"]:
            notes.append(
                "No price claim is made about "
                + ", ".join(gates["openFxBrands"]) +
                ": price_corrections.json still has an open FX repair window for "
                "each of them, which is that file's own statement that it does not "
                "vouch for those prices.")
        if not chicago:
            notes.append(
                "No Chicago story: brands.json carries no city or location field on "
                "any of its label entries, and no enabled partner retailer has a "
                "Chicago address.")
        elif not any(s.get("chicago") for s in stories):
            notes.append(
                "No Chicago story qualified this week. The roster's only Chicago "
                "address is the partner shop " + ", ".join(chicago.values()) +
                "; none of its pieces earned a line. (No LABEL in brands.json "
                "carries a city field, so a Chicago-based designer cannot be "
                "identified from this data at all.)")
        else:
            notes.append(
                "The Chicago story is a piece stocked by " +
                ", ".join(chicago.values()) + " — the label itself is not "
                "Chicago-based, and the sheet does not say it is. brands.json has no "
                "city field on any label entry, so a Chicago DESIGNER cannot be "
                "identified from this data.")
        thin = [k for k in KIND_ORDER if not by_kind.get(k)]
        if thin:
            notes.append("No story of these kinds was available: "
                         + ", ".join(f"{k} ({KIND_LABEL[k]})" for k in thin) + ".")

    sheet = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weekOf": week,
        "builtOn": today,
        "source": {
            "priceRecords": {
                "generatedAt": doc.get("generatedAt"),
                "windowStart": doc.get("windowStart"),
                "windowEnd": doc.get("windowEnd"),
                "days": doc.get("days"),
                "records": len(doc.get("records") or {}),
                "brands": len(doc.get("brands") or {}),
                "sha256": sha256_of(records_path),
            },
            "catalog": {
                "generatedAt": cat_doc.get("generatedAt"),
                "count": cat_doc.get("count") or len(catalog),
                "sha256": sha256_of(catalog_path),
            },
        },
        "gates": gates,
        "counts": {**counts, **{f"available_{k}": len(v) for k, v in by_kind.items()}},
        "header": header_lines(doc, gates, by_kind, counts, week, today),
        "stories": stories,
        "lists": [{k: v for k, v in lst.items() if k != "items"} |
                  {"items": [{"id": s["id"], "piece": s["piece"], "brand": s["brand"],
                              "price": s["price"], "url": s["url"], "image": s["image"],
                              "fact": s["fact"], "kind": s["kind"]}
                             for s in lst["items"]]}
                  for lst in lists],
        "doNotSay": [w for w, _ in DO_NOT_SAY],
        "notes": notes,
        "rejected": dict(reasons.most_common()),
    }
    sheet["markdown"] = render_md({**sheet, "lists": lists})
    return sheet


def write_sheet(sheet, out_dir=OUT_DIR):
    """Write the three files, and do not churn a commit when nothing moved.

    `generatedAt` is a wall clock: it moves on every run, so a re-dispatch on a
    day when the archive said exactly the same thing would produce a changed
    file, a normal-looking commit and a jsDelivr purge for nothing. That is the
    same shape as the stale-catalog "fake day" archive_integrity.py exists to
    catch, one size down. So a run whose output is identical apart from the
    stamp inherits the previous stamp and the files come out byte-identical.
    """
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = sheet["markdown"]
    machine = {k: v for k, v in sheet.items() if k != "markdown"}
    # `sort` is the internal ranking key (sign-flipped magnitudes), not content.
    # It is dropped rather than published: a consumer reading -48 out of a story
    # would be reading an implementation detail as a measurement.
    machine["stories"] = [{k: v for k, v in s.items() if k != "sort"}
                          for s in machine.get("stories") or []]

    def fingerprint(doc):
        # Through JSON, deliberately: the file on disk has been round-tripped
        # (tuples arrive back as lists), so anything comparing to it must be too.
        return json.dumps({**doc, "generatedAt": ""}, ensure_ascii=False,
                          sort_keys=True, default=list)

    try:
        old = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
        if fingerprint(old) == fingerprint(machine):
            machine["generatedAt"] = old["generatedAt"]
    except (OSError, ValueError, KeyError):
        pass
    paths = []
    for name, body in (
        (f"week-{sheet['weekOf']}.md", md),
        ("latest.md", md),
        ("latest.json", json.dumps(machine, ensure_ascii=False, indent=1) + "\n"),
    ):
        p = out_dir / name
        # newline="\n" and no BOM, explicitly. A UTF-8 BOM in a markdown file
        # renders as a stray character in the first heading, and CRLF would make
        # every Linux CI run rewrite a file nothing changed.
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        paths.append(p)
    return paths


def main():
    ap = argparse.ArgumentParser(description="Build Loupe's weekly content sheet.")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD, to rehearse a day")
    ap.add_argument("--records", default=str(RECORDS))
    ap.add_argument("--catalog", default=str(CATALOG))
    ap.add_argument("--brands", default=str(BRANDS))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--report", action="store_true", help="print it, write nothing")
    ap.add_argument("--hooks", action="store_true", help="print the hooks only")
    ap.add_argument("--summary", action="store_true",
                    help="render the job summary from the sheet already on disk")
    args = ap.parse_args()

    if args.summary:
        # Reads what was WRITTEN, not what could be rebuilt: the run page must
        # describe the sheet that landed in the tree.
        latest = pathlib.Path(args.out_dir) / "latest.json"
        try:
            print(summary_markdown(json.loads(latest.read_text(encoding="utf-8"))))
        except (OSError, ValueError) as exc:
            print(f"_No sheet on disk to summarise ({type(exc).__name__})._")
        return

    sheet = build_sheet(args.records, args.catalog, args.brands, args.today)

    def say(line):
        # Brand names here are accented (DemodeMODE, Siedres, SIEDRES) and a
        # cp1252 console raises on them. Degrade the line, never the run.
        try:
            print(line)
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or "ascii"
            print(line.encode(enc, "replace").decode(enc, "replace"))

    if args.hooks:
        for i, s in enumerate(sheet["stories"], 1):
            say(f"{i:2}. [{len(s['hook']):2}] {s['hook']}")
        return

    if args.report:
        say(sheet["markdown"])
        return

    paths = write_sheet(sheet, args.out_dir)
    say(f"content sheet: {len(sheet['stories'])} stories, "
        f"{sum(len(l['items']) for l in sheet['lists'])} list pieces, "
        f"{len(sheet['notes'])} notes")
    for line in sheet["header"]:
        say(f"  {line}")
    say("  hooks:")
    for i, s in enumerate(sheet["stories"], 1):
        say(f"   {i:2}. [{len(s['hook']):2}] {s['hook']}")
    for p in paths:
        say(f"  wrote {p.relative_to(pathlib.Path(args.out_dir).parent)} "
            f"({p.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
