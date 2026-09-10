#!/usr/bin/env python3
"""Fixtures for the weekly content sheet — run by CI BEFORE anything is written.

Plain asserts, no pytest dep, same shape as the other gates in this directory.

WHAT THIS FILE IS FOR. build_content_sheet.py is the only code in this repo that
writes a sentence somebody will read out on camera to an audience that supplies
84% of our installs. A false push can be apologised for privately; a false post
is a screenshot. Every gate below exists because of something the live data
actually did:

  * the 2026-09-05 FX epoch left TWO comparable snapshots, so every brand in the
    published table read "0% ever discounted" — including labels we have watched
    mark down. (window claims)
  * every dated move in the archive landed on the last snapshot, so nothing had
    been seen twice. (confirmation)
  * catalog.json is the 60-per-brand DECK, so 14 of 807 pieces that earned "New
    this week" had been on their own shop's site for a fortnight or more, and
    Mondo Mondo's Star Studs — published 2025-09-12 — came within one run of
    going out as "put this up 5 days ago". (rotation)
  * "this ALOHAS shoes" is what CATEGORY_NOUN produces if nobody checks.

A guard nobody tests is a guard that gets deleted by whoever finds it
inconvenient, so section 12 disables two of them on purpose and proves the suite
goes red without them.
"""
import datetime as dt
import json
import pathlib
import re
import shutil
import tempfile
import urllib.parse

import build_content_sheet as bcs
import build_price_history as bph
import record_digest as rd

failures = []
checked = 0
TMPDIRS = []


def check(name, cond, detail=""):
    global checked
    checked += 1
    if not cond:
        failures.append(f"  {name}{(': ' + detail) if detail else ''}")


# ── Fixture world ────────────────────────────────────────────────────────────
# 40 consecutive snapshot days ending 2026-09-09, and "today" the morning after.
# Consecutive on purpose; the test that cares about holes punches its own.
DAYS = [(dt.date(2026, 8, 1) + dt.timedelta(days=i)).isoformat() for i in range(40)]
TODAY = "2026-09-10"
LAST = len(DAYS) - 1              # 39
CONFIRMED = LAST - 1              # 38 — a move here has been seen twice


class Epochs:
    """Pin build_price_history.PRICE_EPOCHS for the duration of a fixture.

    record_digest.price_epochs() deliberately unions the LIVE module list into
    every document it reads, so that an epoch registered this morning takes
    effect before the archive is rebuilt. That is right in production and would
    otherwise silently change what these fixtures mean the next time somebody
    registers one.
    """

    def __init__(self, days):
        self.days = list(days)

    def __enter__(self):
        self.saved = bph.PRICE_EPOCHS
        bph.PRICE_EPOCHS = list(self.days)
        return self

    def __exit__(self, *exc):
        bph.PRICE_EPOCHS = self.saved
        return False


def rec(f=0, n=40, lo=100.0, hi=100.0, p=100.0, q=0.0, c=-1, sn=0, sx=0):
    """[firstDayIdx, daysSeen, minPrice, maxPrice, lastPrice, prevPrice,
    lastChangeDayIdx, sizesNow, sizesMax] — the published row shape."""
    return [f, n, lo, hi, p, q, c, sn, sx]


def piece(pid, brand, **kw):
    """A catalog row. Defaults are a piece that is live, linkable and unremarkable."""
    row = {
        "id": pid,
        "brand": brand,
        "name": kw.get("name", f"{pid.replace('-', ' ').title()}"),
        "price": kw.get("price", 100),
        "currency": "USD",
        "category": kw.get("category", "tops"),
        "imageUrl": kw.get("imageUrl", f"https://cdn.example/{pid}.jpg"),
        "images": [],
        "sizes": kw.get("sizes", []),
        "available": kw.get("available", True),
        # Old enough that the rotation check never accidentally makes a piece
        # "new"; the tests that want a new arrival say so explicitly.
        "publishedAt": kw.get("publishedAt", "2026-01-01"),
        "affiliateUrl": f"https://shop.example/{pid}",
    }
    if kw.get("retailer"):
        row["retailer"] = kw["retailer"]
    if kw.get("stale"):
        row["stale"] = True
    return row


DEFAULT_CORRECTIONS = {
    "generatedAt": "2026-08-06",
    "appliesToRowsWithout": "currency",
    "fxEpochs": [],
    "corrections": [],
}

GEMINI_RETAILER = {
    "id": "gemini", "enabled": True, "name": "Gemini", "domain": "geminishop.com",
    "currency": "USD",
    "store": {"name": "Gemini", "city": "Chicago",
              "address": "1911 W. Division St., Chicago, IL 60622"},
}


def world(records, products, brands_table=None, retailers=None, day_dates=None,
          epochs=None, tenure_from=None, corrections=None, window_end=None):
    """Write a fixture price_records / catalog / brands / corrections set to a
    fresh temp dir; return the three paths build_sheet() takes.

    price_corrections.json is pointed at from record_digest's module constant
    rather than passed, because that is how the real code finds it — a fixture
    that took a different path would not be exercising the production lookup.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="loupe-sheet-"))
    TMPDIRS.append(tmp)
    days = list(day_dates or DAYS)
    if brands_table is None:
        brands_table = {}
        for p in products:
            brands_table.setdefault(p["brand"], [40, 0, 35])
    doc = {
        "generatedAt": f"{days[-1]}T13:00:00Z",
        "windowStart": days[0],
        "windowEnd": window_end or days[-1],
        "days": len(days),
        "priceEpochs": list(epochs or []),
        "samplingEpochs": [],
        "epochSettleDays": bph.EPOCH_SETTLE_DAYS,
        "arrivalBlackout": [],
        "dayDates": days,
        "tenureTrustedFrom": tenure_from or days[0],
        "thresholds": {"minDropPct": bph.MIN_DROP_PCT,
                       "minRangePct": bph.MIN_RANGE_PCT,
                       "maxStaleDays": bph.MAX_STALE_DAYS},
        "schema": {"records": "…", "brands": "…"},
        "records": records,
        "brands": brands_table,
    }
    cat = {"generatedAt": f"{days[-1]}T13:00:00Z", "count": len(products),
           "products": products}
    brands_doc = {"perBrand": 750, "perBrandDisplay": 60,
                  "retailers": list(retailers or []),
                  "brands": [{"brand": b, "domain": "x.example", "currency": "USD"}
                             for b in sorted({p["brand"] for p in products})]}
    (tmp / "price_records.json").write_text(json.dumps(doc), encoding="utf-8")
    (tmp / "catalog.json").write_text(json.dumps(cat), encoding="utf-8")
    (tmp / "brands.json").write_text(json.dumps(brands_doc), encoding="utf-8")
    (tmp / "price_corrections.json").write_text(
        json.dumps(corrections or DEFAULT_CORRECTIONS), encoding="utf-8")
    rd.pdp.CORRECTIONS = tmp / "price_corrections.json"
    return tmp / "price_records.json", tmp / "catalog.json", tmp / "brands.json"


def build(records, products, today=TODAY, **kw):
    r, c, b = world(records, products, **kw)
    return bcs.build_sheet(r, c, b, today)


def ids(stories):
    return [s["id"] for s in stories]


def all_text(sheet):
    """Every line of copy the sheet publishes, stories and lists alike."""
    out = []
    for s in sheet["stories"]:
        out += [s["hook"], s["fact"], s["title"]]
    for lst in sheet["lists"]:
        out.append(lst["title"])
        out += [i["fact"] for i in lst["items"]]
    return out


# A broad, ordinary week: twelve labels, each with a piece that earns a line.
def broad_world():
    labels = ["Alpha Atelier", "Bravo Studio", "Charlie Wear", "Delta Label",
              "Echo Goods", "Foxtrot Ltd", "Golf Studio", "Hotel Made",
              "India Works", "Juliet Co", "Kilo House", "Lima Press"]
    records, products = {}, []
    for i, brand in enumerate(labels):
        slug = brand.lower().replace(" ", "-")
        # sizes: down to 1 of 6
        records[f"{slug}-sz"] = rec(f=1, n=30, lo=120.0, hi=120.0, p=120.0, sn=1, sx=6)
        products.append(piece(f"{slug}-sz", brand, price=120, sizes=["M"]))
        # new: the shop listed it three days ago
        records[f"{slug}-new"] = rec(f=LAST - 2, n=3, lo=80.0, hi=80.0, p=80.0)
        products.append(piece(f"{slug}-new", brand, price=80,
                              publishedAt=(dt.date.fromisoformat(TODAY)
                                           - dt.timedelta(days=3)).isoformat()))
        # tenure: seen since day 1
        records[f"{slug}-ten"] = rec(f=1, n=39, lo=200.0, hi=200.0, p=200.0, sn=3, sx=3)
        products.append(piece(f"{slug}-ten", brand, price=200,
                              category="dresses", sizes=["S", "M", "L"]))
    return records, products


# ── 1. The shape of a normal week ────────────────────────────────────────────
with Epochs([]):
    R, P = broad_world()
    SHEET = build(R, P)

check("a full week produces exactly ten stories",
      len(SHEET["stories"]) == bcs.STORY_COUNT, str(len(SHEET["stories"])))
check("…and three list posts", len(SHEET["lists"]) == 3, str(len(SHEET["lists"])))
check("…and a three-line header", len(SHEET["header"]) == 3, str(len(SHEET["header"])))
check("every story is a distinct piece", len(set(ids(SHEET["stories"]))) == 10)


# ── 2. The hook cap ──────────────────────────────────────────────────────────
long_hooks = [s["hook"] for s in SHEET["stories"] if len(s["hook"]) > bcs.HOOK_MAX]
check(f"no hook exceeds {bcs.HOOK_MAX} characters", not long_hooks, str(long_hooks))
check("no hook is empty", all(s["hook"].strip() for s in SHEET["stories"]))
check("fit_hook refuses an over-length line, it never truncates one",
      bcs.fit_hook(["x" * 200 + " 5 days."], "5") == "")


# ── 3. Banned words ──────────────────────────────────────────────────────────
# The push's list, imported not restated, plus this file's hype list and the
# no-exclamation rule.
check("'AI' is refused", not bcs.copy_is_clean("Picked by AI for you"))
check("'algorithm' is refused", not bcs.copy_is_clean("our algorithm found this"))
check("'recommended' is refused", not bcs.copy_is_clean("recommended for you"))
check("'personalised' is refused", not bcs.copy_is_clean("a personalised feed"))
check("hype is refused", not bcs.copy_is_clean("this stunning must-have dress"))
check("an exclamation mark is refused", not bcs.copy_is_clean("Down 30% today!"))
check("…but ordinary words containing those letters are not",
      bcs.copy_is_clean("available in detail again, plain and dry"))
check("…and a real hook passes", bcs.copy_is_clean("Down to 1 size from 6 on this top."))

dirty = [t for t in all_text(SHEET) if not bcs.copy_is_clean(t)]
check("nothing the sheet publishes contains a banned or hype word", not dirty,
      str(dirty[:2]))

# A shop may legitimately call a piece anything; the sheet still will not put the
# word in a caption, because a caption is Loupe speaking.
with Epochs([]):
    s2 = build({"aa-x": rec(f=1, n=30, lo=90.0, hi=90.0, p=90.0, sn=1, sx=6)},
               [piece("aa-x", "Alpha Atelier", name="The Algorithm Tee",
                      price=90, sizes=["M"])])
check("a product whose own name is a banned word yields no story",
      s2["stories"] == [], str(ids(s2["stories"])))


# ── 4. One label, one slot ───────────────────────────────────────────────────
with Epochs([]):
    hoard_recs, hoard_prods = {}, []
    for i in range(30):
        hoard_recs[f"hoard-{i}"] = rec(f=1, n=30, lo=120.0, hi=120.0, p=120.0, sn=1, sx=6)
        hoard_prods.append(piece(f"hoard-{i}", "Hoard House", price=120, sizes=["M"]))
    r2, p2 = broad_world()
    hoard = build({**hoard_recs, **r2}, hoard_prods + p2)
brand_counts = {}
for s in hoard["stories"]:
    brand_counts[s["brand"]] = brand_counts.get(s["brand"], 0) + 1
check("no brand appears twice in the ten",
      all(v == 1 for v in brand_counts.values()), str(brand_counts))
check("…even when one label could fill the whole sheet",
      brand_counts.get("Hoard House", 0) <= 1)

for lst in hoard["lists"]:
    per = {}
    for it in lst["items"]:
        per[it["brand"]] = per.get(it["brand"], 0) + 1
    check(f"list '{lst['title'][:34]}' takes at most "
          f"{bcs.LIST_MAX_PER_BRAND} pieces from one label",
          all(v <= bcs.LIST_MAX_PER_BRAND for v in per.values()), str(per))


# ── 5. Every link is a real product page for an available piece ──────────────
def link_check(sheet, catalog_by_id):
    bad = []
    linked = [(s["id"], s["url"]) for s in sheet["stories"]]
    linked += [(i["id"], i["url"]) for lst in sheet["lists"] for i in lst["items"]]
    for pid, url in linked:
        expected = f"{bcs.SITE}/product/{urllib.parse.quote(pid, safe='')}/"
        live = catalog_by_id.get(pid)
        if url != expected or not live or not live.get("available") or live.get("stale"):
            bad.append((pid, url))
    return bad


by_id = {p["id"]: p for p in P}
check("every link is /product/<id>/ on useloupe.shop for an AVAILABLE piece",
      not link_check(SHEET, by_id), str(link_check(SHEET, by_id)[:2]))

with Epochs([]):
    r3, p3 = broad_world()
    r3["gone-piece"] = rec(f=1, n=30, lo=99.0, hi=99.0, p=99.0, sn=1, sx=6)
    p3.append(piece("gone-piece", "Zulu Gone", price=99, sizes=["M"], available=False))
    r3["frozen-piece"] = rec(f=1, n=30, lo=98.0, hi=98.0, p=98.0, sn=1, sx=6)
    p3.append(piece("frozen-piece", "Yankee Frozen", price=98, sizes=["M"], stale=True))
    r3["nameless"] = rec(f=1, n=30, lo=97.0, hi=97.0, p=97.0, sn=1, sx=6)
    unlinked = build(r3, p3)
everything = ids(unlinked["stories"]) + [i["id"] for l in unlinked["lists"]
                                         for i in l["items"]]
check("a sold-out piece is never linked (the site deletes its page)",
      "gone-piece" not in everything)
check("a grace-carried (stale) row is never linked", "frozen-piece" not in everything)
check("a record with no catalog row at all is never linked", "nameless" not in everything)
check("has_live_page() rejects an id the site could not write a directory for",
      not bcs.has_live_page({"id": "nul", "available": True})
      and not bcs.has_live_page({"id": "a/b", "available": True})
      and bcs.has_live_page({"id": "ok-piece", "available": True}))
check("an accented id is percent-encoded exactly as the site links it",
      bcs.product_url("dôen-dress") == f"{bcs.SITE}/product/d%C3%B4en-dress/")


# ── 6. Every number in a hook is sourced by its fact line ────────────────────
unsourced = [(s["hook"], s["fact"]) for s in SHEET["stories"]
             if not (bcs.numbers_in(s["hook"]) <= bcs.numbers_in(s["fact"]))]
check("every number in every hook also appears in that story's fact line",
      not unsourced, str(unsourced[:1]))
check("a list post never prints the piece and its price twice",
      all(not s["detail"].startswith(s["piece"]) for s in SHEET["stories"]),
      str([s["detail"][:40] for s in SHEET["stories"]
           if s["detail"].startswith(s["piece"])][:1]))
check("…and `detail` is derived from the fact, so the two cannot disagree",
      all(s["fact"].endswith(s["detail"]) for s in SHEET["stories"]))
check("a hook with no number at all is refused",
      not bcs.hook_is_sound("This dress is on sale.", "This dress is on sale."))
check("a hook whose number is not in the fact line is refused",
      not bcs.hook_is_sound("Down 40% today.", "It fell 30% on Sep 2."))
check("thousands separators do not break the comparison",
      bcs.hook_is_sound("Listed 3 days ago. $1,650.",
                        "A piece at $1,650, listed 3 days ago."))


# ── 7. FX and epoch suppression ──────────────────────────────────────────────
def drop_world(change_idx, epochs=(), brand="Marfa Test", corrections=None,
               pieces=1, ratio=0.7):
    """A brand whose pieces all fell on one day, with everything else neutral."""
    records, products = {}, []
    for i in range(pieces):
        was, now = 200.0, round(200.0 * ratio)
        records[f"dw-{i}"] = rec(f=0, n=39, lo=now, hi=was, p=now, q=was,
                                 c=change_idx, sn=3, sx=3)
        products.append(piece(f"dw-{i}", brand, price=now, sizes=["S", "M", "L"]))
    return build(records, products, epochs=list(epochs), corrections=corrections)


with Epochs([]):
    ok = drop_world(CONFIRMED)
check("a confirmed, epoch-free markdown IS reported",
      any(s["kind"] == "drop" for s in ok["stories"]),
      str([(s["kind"], s["hook"]) for s in ok["stories"]]))

with Epochs([]):
    unconfirmed = drop_world(LAST)
check("a move seen in only the last snapshot is NOT reported",
      not any(s["kind"] == "drop" for s in unconfirmed["stories"]))
check("…and the sheet says so rather than going quiet",
      any("seen twice" in n for n in unconfirmed["notes"]), str(unconfirmed["notes"]))

with Epochs([]):
    on_epoch = drop_world(CONFIRMED, epochs=[DAYS[CONFIRMED]])
check("a move landing ON a price epoch is NOT reported",
      not any(s["kind"] == "drop" for s in on_epoch["stories"]))

with Epochs([]):
    in_tail = drop_world(CONFIRMED, epochs=[DAYS[CONFIRMED - 2]])
check("a move inside an epoch's settle tail is NOT reported",
      not any(s["kind"] == "drop" for s in in_tail["stories"]))

with Epochs([]):
    straddle = drop_world(CONFIRMED, epochs=[DAYS[CONFIRMED - 10]])
check("a move outside the tail but in the same epoch IS reported",
      any(s["kind"] == "drop" for s in straddle["stories"]))

OPEN_FX = {**DEFAULT_CORRECTIONS,
           "corrections": [{"brand": "Marfa Test", "fromDay": "2026-07-15",
                            "toDay": None, "factor": 1.09}]}
with Epochs([]):
    fxd = drop_world(CONFIRMED, corrections=OPEN_FX)
check("no price story is made about a brand with an OPEN FX correction",
      not any(s["kind"] in ("drop", "lowest", "hold") for s in fxd["stories"]))
check("…and the sheet names the brands it is staying quiet about",
      any("Marfa Test" in n for n in fxd["notes"]), str(fxd["notes"]))

# An FX epoch registered only in price_corrections.json still voids the move —
# record_digest.price_epochs() unions all three registers and this reads it.
FX_EPOCH = {**DEFAULT_CORRECTIONS,
            "fxEpochs": [{"day": DAYS[CONFIRMED], "kind": "fx-table-refresh"}]}
with Epochs([]):
    fxe = drop_world(CONFIRMED, corrections=FX_EPOCH)
check("an FX epoch registered only in price_corrections.json voids the move",
      not any(s["kind"] == "drop" for s in fxe["stories"]))

# The coordinated whole-brand step.
with Epochs([]):
    uniform = drop_world(CONFIRMED, pieces=8, ratio=0.90)
check("eight pieces stepping by ONE small ratio reads as arithmetic, not a sale",
      not any(s["kind"] == "drop" for s in uniform["stories"]))
with Epochs([]):
    big = drop_world(CONFIRMED, pieces=8, ratio=0.50)
check("…but a uniform 50% step is too big to be any currency table, so it stands",
      any(s["kind"] == "drop" for s in big["stories"]))


def tiered_world():
    records, products = {}, []
    for i, ratio in enumerate([0.5, 0.5, 0.7, 0.7, 0.7, 0.8, 0.8, 0.9]):
        was, now = 200.0, round(200.0 * ratio)
        records[f"tw-{i}"] = rec(f=0, n=39, lo=now, hi=was, p=now, q=was,
                                 c=CONFIRMED, sn=3, sx=3)
        products.append(piece(f"tw-{i}", "Tiered Label", price=now, sizes=["S", "M", "L"]))
    return build(records, products)


with Epochs([]):
    tiered = tiered_world()
check("a TIERED markdown (many ratios) is a real sale and IS reported",
      any(s["kind"] == "drop" for s in tiered["stories"]))
check("house_step_ratio() sees a uniform step",
      bcs.house_step_ratio({f"h{i}": rec(p=90.0, q=100.0, c=5) for i in range(8)},
                           {f"h{i}": {"brand": "B"} for i in range(8)}, "B", 5)
      is not None)
check("house_step_ratio() ignores a step too small to be a brand-wide event",
      bcs.house_step_ratio({f"h{i}": rec(p=90.0, q=100.0, c=5) for i in range(3)},
                           {f"h{i}": {"brand": "B"} for i in range(3)}, "B", 5)
      is None)


# ── 8. Window claims are scoped to the comparable window ─────────────────────
# The failure this whole gate exists for: three days after an epoch, "the lowest
# price we have ever seen" is a statement about two mornings.

# q = 0 / c = -1 so the piece earns "lowest" and NOT "drop": one piece yields one
# story (you do not post the same dress twice) and drop outranks lowest, so a row
# that earned both would test the wrong thing.
with Epochs([]):
    lowest_recs = {"lw-1": rec(f=0, n=39, lo=100.0, hi=200.0, p=100.0, q=0.0,
                               c=-1, sn=3, sx=3)}
    lowest_prods = [piece("lw-1", "Wide Window", price=100, sizes=["S", "M", "L"])]
    wide = build(lowest_recs, lowest_prods)
check("with a wide comparable window, a lowest-ever claim IS made",
      any(s["kind"] == "lowest" for s in wide["stories"]),
      str([s["kind"] for s in wide["stories"]]))
check("…and the window it quotes never exceeds the comparable window",
      all(s["measure"]["comparableDays"] <= wide["gates"]["comparableDays"]
          for s in wide["stories"] if s["kind"] == "lowest"))

with Epochs([]):
    narrow = build(lowest_recs, lowest_prods, epochs=[DAYS[LAST - 4]])
check("with a two-day comparable window, NO lowest-ever claim is made",
      not any(s["kind"] == "lowest" for s in narrow["stories"]),
      str([s["kind"] for s in narrow["stories"]]))
check("…and no brand-discipline percentage is quoted anywhere",
      not any(re.search(r"marked down \d+%", t) for t in all_text(narrow)))
check("…and the sheet says which claims it held and when they return",
      any("HELD this week" in n and narrow["gates"]["windowClaimsReturn"] in n
          for n in narrow["notes"]), str(narrow["notes"]))
check("comparable_days() drops the settle tail after an epoch",
      bcs.comparable_days({"dayDates": DAYS, "epochSettleDays": 3},
                          epochs=[DAYS[LAST - 4]]) == DAYS[LAST - 1:],
      str(bcs.comparable_days({"dayDates": DAYS, "epochSettleDays": 3},
                              epochs=[DAYS[LAST - 4]])))

HOLD = {"hd-1": rec(f=0, n=70, lo=300.0, hi=300.0, p=300.0, q=0.0, c=-1, sn=3, sx=3)}
HOLD_P = [piece("hd-1", "Steady Label", price=300, sizes=["S", "M", "L"])]
with Epochs([]):
    held = build(HOLD, HOLD_P, day_dates=[(dt.date(2026, 7, 2) + dt.timedelta(days=i)).isoformat()
                                          for i in range(70)],
                 brands_table={"Steady Label": [60, 1, 65]})
check("a 60+ day unmoved price at a label that rarely marks down IS a story",
      any(s["kind"] == "hold" for s in held["stories"]),
      str([s["kind"] for s in held["stories"]]))
with Epochs([]):
    not_held = build({"hd-1": rec(f=0, n=30, lo=300.0, hi=300.0, p=300.0, sn=3, sx=3)},
                     HOLD_P, brands_table={"Steady Label": [60, 1, 65]})
check("…but 30 days is not 60, so it is not that story",
      not any(s["kind"] == "hold" for s in not_held["stories"]))
with Epochs([]):
    discounter = build(HOLD, HOLD_P,
                       day_dates=[(dt.date(2026, 7, 2) + dt.timedelta(days=i)).isoformat()
                                  for i in range(70)],
                       brands_table={"Steady Label": [60, 30, 65]})
check("…and a label that marks down half its catalog never gets the line",
      not any(s["kind"] == "hold" for s in discounter["stories"]))


# ── 9. The rotation check ────────────────────────────────────────────────────
# catalog.json is the 60-per-brand DECK. A piece can enter it by rotation, and
# record_kinds' `new` cannot tell the difference. The shop's own listing date can.
NEW_R = {"nw-1": rec(f=LAST - 2, n=3, lo=90.0, hi=90.0, p=90.0)}


def rotation_sheet(published):
    with Epochs([]):
        return build(NEW_R, [piece("nw-1", "Rotate Label", price=90,
                                   publishedAt=published)])


fresh = (dt.date.fromisoformat(TODAY) - dt.timedelta(days=3)).isoformat()
check("a genuinely new listing IS a new-arrival story",
      any(s["kind"] == "new" for s in rotation_sheet(fresh)["stories"]))
check("a piece the shop listed a year ago is NOT 'new this week'",
      not any(s["kind"] == "new" for s in rotation_sheet("2025-09-12")["stories"]))
check("…nor one with no listing date at all",
      not any(s["kind"] == "new" for s in rotation_sheet("")["stories"]))
check("the new-arrival sentence is dated by the SHOP's date, not by our sighting",
      all(str(s["measure"]["publishedDaysAgo"]) in s["hook"]
          for s in rotation_sheet(fresh)["stories"] if s["kind"] == "new"))

with Epochs([]):
    impossible = build({"tn-1": rec(f=0, n=39, lo=100.0, hi=100.0, p=100.0, sn=2, sx=2)},
                       [piece("tn-1", "Future Label", price=100, sizes=["S", "M"],
                              publishedAt="2026-12-01")])
check("a shelf-life story is refused when the shop listed it AFTER we first saw it",
      not any(s["kind"] == "tenure" for s in impossible["stories"]))


# ── 10. Grammar, Chicago, and the quiet week ─────────────────────────────────
with Epochs([]):
    shoes = build({"sh-1": rec(f=1, n=30, lo=245.0, hi=245.0, p=245.0, sn=1, sx=8)},
                  [piece("sh-1", "ALOHAS", category="shoes", price=245, sizes=["35"])])
shoe_text = " ".join(all_text(shoes))
check("'shoes' takes 'these', never 'this'",
      "these ALOHAS shoes" in shoe_text and "this ALOHAS shoes" not in shoe_text,
      shoe_text[:120])
det, phrase, verb = bcs.subject({"brand": "ALOHAS", "category": "shoes"})
check("subject() agrees determiner, noun and verb",
      (det, phrase, verb) == ("these", "ALOHAS shoes", "have"), str((det, phrase, verb)))
check("…and a dress is singular even though it ends in s",
      bcs.subject({"brand": "X", "category": "dresses"}) == ("this", "X dress", "has"))

with Epochs([]):
    r4, p4 = broad_world()
    r4["gem-1"] = rec(f=1, n=30, lo=115.0, hi=115.0, p=115.0, sn=1, sx=4)
    p4.append(piece("gem-1", "Alpha Atelier", price=115, sizes=["6"], retailer="gemini"))
    chi = build(r4, p4, retailers=[GEMINI_RETAILER])
check("a Chicago story is present when the roster has a Chicago shop",
      any(s.get("chicago") for s in chi["stories"]),
      str([(s["brand"], s["retailer"]) for s in chi["stories"]]))
check("…even when that label already held a slot (the swap frees it)",
      "gem-1" in ids(chi["stories"]))
check("…and it still holds exactly ten stories",
      len(chi["stories"]) == bcs.STORY_COUNT)
check("…and it says the LABEL is not the Chicago party",
      any("Chicago DESIGNER cannot be identified" in n for n in chi["notes"]),
      str(chi["notes"]))
with Epochs([]):
    nochi = build(*broad_world(), retailers=[])
check("with no Chicago address anywhere, the sheet says so plainly",
      any("no city or location field" in n for n in nochi["notes"]),
      str(nochi["notes"]))

with Epochs([]):
    quiet = build({}, [])
check("an empty week produces no stories at all", quiet["stories"] == [])
check("…and says 'A quiet week' rather than nothing", "A quiet week" in quiet["markdown"])
check("…and invents no story blocks", "### 1." not in quiet["markdown"])
check("…and still writes the do-not-say section", "## Do not say" in quiet["markdown"])
check("…and its lists are empty rather than filled",
      all(not l["items"] for l in quiet["lists"]))
check("…and it says nothing was substituted",
      "Nothing has been substituted." in quiet["markdown"])

with Epochs([]):
    stale_sheet = build(*broad_world(), today="2026-10-30")
check("an archive older than maxStaleDays produces no dated claim at all",
      stale_sheet["stories"] == [] and stale_sheet["gates"]["archiveStale"],
      str(len(stale_sheet["stories"])))
check("…and names the reason", any("stale" in n for n in stale_sheet["notes"]))


# ── 11. Garbage in, silence out ──────────────────────────────────────────────
GARBAGE = {
    "g-short": [1, 2, 3],
    "g-long": [0] * 12,
    "g-bool": [True, True, True, True, True, True, True, True, True],
    "g-str": [0, 30, "100", 100.0, 100.0, 0.0, -1, 1, 6],
    "g-none": [0, 30, None, 100.0, 100.0, 0.0, -1, 1, 6],
    "g-nan": [0, 30, float("nan"), 100.0, 100.0, 0.0, -1, 1, 6],
    "g-inf": [0, 30, float("inf"), 100.0, 100.0, 0.0, -1, 1, 6],
    "g-neg": [0, 30, -5.0, -1.0, -5.0, 0.0, -1, 1, 6],
    "g-zero": [0, 0, 0.0, 0.0, 0.0, 0.0, -1, 0, 0],
    "g-dict": {"nope": 1},
    "g-null": None,
}
try:
    with Epochs([]):
        rg, pg = broad_world()
        junk = build({**GARBAGE, **rg},
                     pg + [piece(k, "Junk Label", price=100, sizes=["M"])
                           for k in GARBAGE])
    crashed = ""
except Exception as exc:                      # noqa: BLE001 — that IS the check
    junk, crashed = None, f"{type(exc).__name__}: {exc}"
check("malformed record rows never crash the build", not crashed, crashed)
if junk:
    check("…and never reach the sheet",
          not any(i.startswith("g-") for i in ids(junk["stories"])),
          str([i for i in ids(junk["stories"]) if i.startswith("g-")]))
    check("…and the sheet is still whole", len(junk["stories"]) == bcs.STORY_COUNT)

check("a day index outside dayDates resolves to nothing, never to a guessed date",
      bcs.rd.day_at({"dayDates": DAYS}, 999) is None
      and bcs.rd.day_at({"dayDates": DAYS}, -1) is None)


# ── 12. THE REVERT PROBES ────────────────────────────────────────────────────
# A guard nobody tests is a guard that gets deleted. Each probe disables one on
# purpose, proves the suite goes red, restores it, and proves it goes green.

def rotation_guard_holds():
    """True while a year-old listing cannot become 'new this week'."""
    return not any(s["kind"] == "new"
                   for s in rotation_sheet("2025-09-12")["stories"])


check("PROBE 1 baseline — the rotation guard holds", rotation_guard_holds())
_saved = bcs.NEW_PUBLISHED_MAX_DAYS
bcs.NEW_PUBLISHED_MAX_DAYS = 100_000          # disable it
red_1 = not rotation_guard_holds()
bcs.NEW_PUBLISHED_MAX_DAYS = _saved           # restore
green_1 = rotation_guard_holds()
check("PROBE 1 red — disabling NEW_PUBLISHED_MAX_DAYS publishes the false claim",
      red_1, "the guard is not load-bearing; this test proves nothing")
check("PROBE 1 green — restoring it suppresses the false claim again", green_1)


def confirm_guard_holds():
    """True while a move seen in only one snapshot cannot be reported."""
    with Epochs([]):
        return not any(s["kind"] == "drop" for s in drop_world(LAST)["stories"])


check("PROBE 2 baseline — the confirmation guard holds", confirm_guard_holds())
_saved2 = rd.MOVE_MIN_CONFIRM_SNAPSHOTS
rd.MOVE_MIN_CONFIRM_SNAPSHOTS = 0             # disable it
red_2 = not confirm_guard_holds()
rd.MOVE_MIN_CONFIRM_SNAPSHOTS = _saved2       # restore
green_2 = confirm_guard_holds()
check("PROBE 2 red — dropping MOVE_MIN_CONFIRM_SNAPSHOTS reports a one-day move",
      red_2, "the guard is not load-bearing; this test proves nothing")
check("PROBE 2 green — restoring it suppresses the one-day move again", green_2)


# ── 13. What lands on disk ───────────────────────────────────────────────────
_out = pathlib.Path(tempfile.mkdtemp(prefix="loupe-sheet-out-"))
TMPDIRS.append(_out)
paths = bcs.write_sheet(SHEET, _out)
week_md = _out / f"week-{SHEET['weekOf']}.md"
check("it writes week-<monday>.md, latest.md and latest.json",
      {p.name for p in paths} == {week_md.name, "latest.md", "latest.json"},
      str([p.name for p in paths]))
check("the week file is named for a MONDAY",
      dt.date.fromisoformat(SHEET["weekOf"]).weekday() == 0, SHEET["weekOf"])
raw_md = week_md.read_bytes()
check("no UTF-8 BOM", not raw_md.startswith(b"\xef\xbb\xbf"))
check("no CRLF — LF only, so a Linux CI run does not rewrite the file",
      b"\r\n" not in raw_md)
check("no mojibake in the markdown",
      "Ã©" not in raw_md.decode("utf-8") and "�" not in raw_md.decode("utf-8"))
check("latest.md is byte-identical to the week file",
      (_out / "latest.md").read_bytes() == raw_md)
machine = json.loads((_out / "latest.json").read_text(encoding="utf-8"))
check("latest.json parses and carries the same ten stories",
      ids(machine["stories"]) == ids(SHEET["stories"]))
check("latest.json carries the provenance of both inputs",
      machine["source"]["priceRecords"]["sha256"]
      and machine["source"]["catalog"]["sha256"])
check("latest.json carries no rendered markdown (that is what latest.md is for)",
      "markdown" not in machine)

# A re-dispatch on a day nothing moved must not manufacture a commit and a CDN
# purge out of a wall clock. Same shape as archive_integrity.py's heartbeat.
_before = {p.name: p.read_bytes() for p in _out.iterdir()}
_again = dict(SHEET)
_again["generatedAt"] = "2099-01-01T00:00:00Z"
bcs.write_sheet(_again, _out)
check("re-running with identical content rewrites the same bytes, not a new stamp",
      {p.name: p.read_bytes() for p in _out.iterdir()} == _before,
      str([n for n, b in _before.items()
           if (_out / n).read_bytes() != b]))
_changed = dict(SHEET)
_changed["notes"] = list(SHEET["notes"]) + ["something genuinely moved"]
_changed["generatedAt"] = "2099-01-01T00:00:00Z"
bcs.write_sheet(_changed, _out)
check("…but a real change does take the new stamp",
      json.loads((_out / "latest.json").read_text(encoding="utf-8"))["generatedAt"]
      == "2099-01-01T00:00:00Z")
bcs.write_sheet(SHEET, _out)                  # restore for the checks below

summary = bcs.summary_markdown(machine)
check("the job summary lists all ten hooks",
      all(s["hook"].replace("|", "\\|") in summary for s in SHEET["stories"]))
check("…and escapes a pipe in a hook so it cannot break the summary table",
      r"a \| b" in bcs.summary_markdown(
          {"weekOf": "2026-09-07", "header": [], "notes": [], "lists": [],
           "stories": [{"hook": "a | b", "kindLabel": "x", "brand": "Y"}]}))
check("…and a quiet week summarises as a quiet week",
      "A quiet week" in bcs.summary_markdown(
          {k: v for k, v in quiet.items() if k != "markdown"}))

SRC = (bcs.HERE / "build_content_sheet.py").read_text(encoding="utf-8")
# stock.json carries per-variant inventory and its own `use` block forbids "a
# named brand's stock or units, in any artefact". Every line of this sheet names
# a brand. Two checks: the builder never opens it, and it still builds when the
# file is nowhere on disk (no fixture directory contains one).
check("the builder never opens stock.json — its use policy forbids naming a brand",
      not re.search(r"""(open|read_text|read_bytes|load_json|Path)\s*\(?[^)\n]*["']"""
                    r"""[^"'\n]*stock\.json""", SRC),
      str(re.findall(r".*stock\.json.*", SRC)[:1]))
check("…and the sheet discloses that it does not use it",
      "stock.json" in SHEET["markdown"])
check("…and the build runs with no stock.json anywhere near it",
      not any((d / "stock.json").exists() for d in TMPDIRS)
      and len(SHEET["stories"]) == bcs.STORY_COUNT)
check("…and no secret, token or user table is referenced",
      not re.search(r"(SUPABASE|SERVICE_KEY|push_token|saved_items|profiles|"
                    r"EXPO_ACCESS|@gmail)", SRC))
check("the thresholds are imported from the archive, never restated",
      "bph.MIN_DAYS_FOR_PRICE_CLAIM" in SRC and "rd.MOVE_MIN_CONFIRM_SNAPSHOTS" in SRC)


def main():
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    if failures:
        print(f"CONTENT-SHEET REGRESSIONS ({len(failures)} of {checked}):")
        print("\n".join(failures))
        raise SystemExit(1)
    print(f"content-sheet fixtures: all OK ({checked} checks)")


if __name__ == "__main__":
    main()
