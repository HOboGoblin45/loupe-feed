#!/usr/bin/env python3
"""Market-signal fixtures — the two things we started recording on 2026-08-07,
and the one thing we promised never to publish.

Same shape as the other gates here: plain asserts, no pytest dep, and every case
encodes a specific incident or a specific promise rather than a specific line of
code. Run before pushing anything this file covers — on 2026-07-24 a change to
build_catalog.py landed without its fixture, the pre-build gate went red every
morning, and FOUR DAYS of an irreplaceable archive were lost before anyone
noticed.

WHAT THIS FILE IS ABOUT

1. THE OFFERED SIZE RUN. available_sizes() has always read product["options"],
   which carries the complete size run INCLUDING sold-out values, and then thrown
   the sold-out ones away at `if not v.get("available"): continue`. So the record
   said which sizes were IN STOCK and knew nothing about which were OFFERED.
   That ambiguity invalidated a published finding: "XL stocked on 45.8% of
   pieces" was read as an offer rate when it is an in-stock rate. Measured live
   on 2026-08-07 across 1,475 sized products at 7 stores, 23.2% of all offered
   size slots were sold out — so the two numbers are not close, and the wrong one
   says "they don't make an XL" about brands that do.

2. TRUE PER-VARIANT INVENTORY. 33 of 162 stores expose a real
   `inventory_quantity` on /products/<handle>.js (pre-2018 Shopify shops,
   grandfathered past the 2017-12-05 changelog). Re-derived live 2026-08-07:
   162/162 domains answered, 0 errors, exactly 33 expose it. It is a genuine
   ledger, not a placeholder — across 2,502 observed variants, `available` equals
   `qty > 0` for 1,831 of 1,831 shopify-managed deny-policy variants, and the
   126 negative values all sit on inventory_policy=continue (oversell/preorder).

3. THE PROMISE. These merchants did not intend to publish stock levels, and this
   repo is PUBLIC. A committed stock.json that named brands would BE the
   disclosure we said we would never make, to everyone, permanently — no policy
   document can undo a committed file. The pseudonymisation and the aggregate
   floor are therefore load-bearing, and are pinned here rather than trusted.

4. THE APP PAYLOAD MUST NOT MOVE. catalog.json is 1.09 KB/product against
   jsDelivr's ~20 MB ceiling, every row becomes a ~111 KB cutout and an
   embedding, and iOS reads `price` and `sizes` in ~20 render sites. None of this
   work is allowed to add a byte to it. That is checked, not asserted.
"""
import inspect
import json
import pathlib

import build_catalog
from build_catalog import (
    INVENTORY_BUDGET,
    INVENTORY_CANARIES,
    INVENTORY_PER_STORE,
    INVENTORY_STORES,
    MIN_BRANDS_PER_AGGREGATE,
    available_sizes,
    collect_stock,
    compare_at_signal,
    normalize,
    price_band,
    size_runs,
    stock_aggregate_ok,
    stock_cohort,
    stock_record,
    variant_stock,
)

HERE = pathlib.Path(__file__).resolve().parent
failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(f"  {name}{(': ' + detail) if detail else ''}")


def product(variants, options=None, handle="thing", title="Silk Thing"):
    """The smallest /products.json shape normalize() will accept."""
    return {
        "title": title, "handle": handle, "product_type": "Dresses",
        "variants": variants, "options": options or [],
        "images": [{"src": "https://cdn.example.com/a.jpg"}],
        "published_at": "2026-07-01T00:00:00Z",
    }


def sized(pairs, values=None, name="Size", position=1):
    """pairs: [(size, available)] -> a product with a Size option."""
    return product(
        [{"price": "180.00", "available": av, f"option{position}": s,
          "title": s} for s, av in pairs],
        options=[{"name": name, "position": position,
                  "values": list(values) if values is not None
                  else [s for s, _a in pairs]}],
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. THE OFFERED RUN — a superset BY CONSTRUCTION, not by convention
# ═════════════════════════════════════════════════════════════════════════════
p = sized([("XS", True), ("S", True), ("M", False), ("L", True), ("XL", False)])
offered, in_stock = size_runs(p)
check("the offered run keeps sold-out sizes",
      offered == ["XS", "S", "M", "L", "XL"], str(offered))
check("the in-stock run still drops them", in_stock == ["XS", "S", "L"], str(in_stock))
check("offered is a superset of in-stock", set(in_stock) <= set(offered))
# The whole point, in one assertion: these two are different numbers, and the
# in-stock one is the one that was being published as if it were the other.
check("the two runs actually differ when something is sold out",
      len(offered) > len(in_stock))

# A brand that offers XL but has sold out of it must not read the same as a brand
# that never made one. This is the sentence the change exists to make possible.
sold_out_xl = size_runs(sized([("S", True), ("M", True), ("XL", False)]))
never_made_xl = size_runs(sized([("S", True), ("M", True)]))
check("'sold out of XL' and 'never offered XL' are now distinguishable",
      ("XL" in sold_out_xl[0]) and ("XL" not in never_made_xl[0])
      and ("XL" not in sold_out_xl[1]) and ("XL" not in never_made_xl[1]))

# Everything sold out: the run is still known, the in-stock set is empty. Before
# today this product contributed NOTHING to the record at all.
all_gone = size_runs(sized([("S", False), ("M", False), ("L", False)]))
check("a fully sold-out piece still records what it offers",
      all_gone[0] == ["S", "M", "L"] and all_gone[1] == [], str(all_gone))

# The superset must survive a store whose options[].values is incomplete or
# empty — which is why `offered` is built from EVERY variant and not from the
# declared list alone. A convention would break here; a construction cannot.
partial = size_runs(sized([("S", True), ("M", False), ("L", True)], values=["S"]))
check("a variant value the store never declared is still 'offered'",
      set(partial[1]) <= set(partial[0]) and set(partial[0]) == {"S", "M", "L"},
      str(partial))
empty_values = size_runs(sized([("S", True), ("M", False)], values=[]))
check("an empty declared values list cannot break the superset",
      set(empty_values[1]) <= set(empty_values[0]), str(empty_values))

# No size option at all (a one-size bag, a candle, most jewellery) is not an
# error and never was.
check("no size option is ([], []), not a crash", size_runs(product([])) == ([], []))
check("a non-size option is ignored",
      size_runs(sized([("Red", True)], name="Color")) == ([], []))

# Ordering: canonical letter scale when they all look like letter sizes, the
# store's own declared order otherwise. Both runs must read the same way round,
# or an offer curve and an in-stock curve cannot be compared element-wise.
letters = size_runs(sized([("L", True), ("XS", False), ("M", True), ("XXL", False)]))
check("letter sizes are laid out on the canonical scale",
      letters[0] == ["XS", "M", "L", "XXL"], str(letters[0]))
nums = size_runs(sized([("10", True), ("2", False), ("6", True)],
                       values=["2", "6", "10"]))
check("numeric sizes keep the store's declared order",
      nums[0] == ["2", "6", "10"] and nums[1] == ["6", "10"], str(nums))

# option2 / option3 stores (Colour first, Size second) were always handled; pin
# it, because `offered` now also reads the variant rows and could regress here.
pos2 = {
    "title": "T", "handle": "t", "product_type": "Tops",
    "images": [{"src": "https://cdn.example.com/a.jpg"}],
    "options": [{"name": "Color", "position": 1, "values": ["Black"]},
                {"name": "Size", "position": 2, "values": ["S", "M", "L"]}],
    "variants": [{"price": "90", "available": True, "option1": "Black", "option2": "S"},
                 {"price": "90", "available": False, "option1": "Black", "option2": "M"},
                 {"price": "90", "available": True, "option1": "Black", "option2": "L"}],
}
check("the size option is read at its declared position",
      size_runs(pos2) == (["S", "M", "L"], ["S", "L"]), str(size_runs(pos2)))


# ═════════════════════════════════════════════════════════════════════════════
# 2. THE APP PAYLOAD DID NOT MOVE
# ═════════════════════════════════════════════════════════════════════════════
# available_sizes() is now a one-line wrapper, and that is deliberate: it makes
# "catalog.json's `sizes` field is unchanged" something a test can prove instead
# of something a reviewer has to believe.
for case in (p, all_gone and sized([("S", False)]), sized([("M", True)]),
             product([]), pos2):
    check("available_sizes is exactly the in-stock run",
          available_sizes(case) == size_runs(case)[1])

# The exact key set of a catalog row. A new key here is a new key on ~8,200 rows
# served to phones through jsDelivr and turned into cutouts and embeddings, so it
# is a product decision, not an implementation detail. If this fails, something
# leaked from the market record into the app payload.
_ALWAYS = {"id", "brand", "name", "price", "priceRaw", "currency", "category",
           "colorTags", "imageUrl", "sizes", "images", "available",
           "publishedAt", "affiliateUrl"}
_OPTIONAL = {"currencyUnverified", "accessorySubtype", "retailer"}
row = normalize(sized([("S", True), ("M", False)]), "B", "b.com", 1.0)
check("a catalog row carries exactly the known fields",
      set(row) == _ALWAYS, f"unexpected={sorted(set(row) - _ALWAYS)} "
                           f"missing={sorted(_ALWAYS - set(row))}")
check("no market-only field reached the catalog row",
      not (set(row) & {"sizesOffered", "offered", "compareAt", "compareAtRaw",
                       "inventory", "inventoryQuantity", "stock"}),
      str(sorted(set(row))))
check("`sizes` is the IN-STOCK run, not the offered one",
      row["sizes"] == ["S"], str(row["sizes"]))
row_acc = normalize(product([{"price": "40", "available": True}],
                            title="Gold Hoop Earrings"), "B", "b.com", 1.0)
check("an accessory row adds only its documented optional field",
      set(row_acc) <= _ALWAYS | _OPTIONAL,
      str(sorted(set(row_acc) - (_ALWAYS | _OPTIONAL))))

_main = inspect.getsource(build_catalog.main)
check("the market record is a SIDE MAP, not extra keys on the shipped row",
      'market[norm["id"]] = (*size_runs(product)' in _main)
check("the offered run is written to shelf.json, never to catalog.json",
      '"sizesOffered"' in _main
      and _main.index('"sizesOffered"') > _main.index("OUT_FILE.write_text"))
check("the shelf arrays are only written when they line up with the rows",
      "len(_offered) == len(_in_stock) == len(_compare_at) == len(_rows)" in _main)
check("a phase mismatch omits them loudly rather than shipping them skewed",
      "out of phase with the rows" in _main)


# ═════════════════════════════════════════════════════════════════════════════
# 3. compare_at_price — a SECONDARY signal, useless without its fill rate
# ═════════════════════════════════════════════════════════════════════════════
# Measured live 2026-08-07: baserange 57.0% fill, lisasaysgah 48.1%, miaou 11.5%,
# staud 0.1%, st-agni 0.0%, damson madder 0.0%, rat and boa 0.0%. A 0% store is
# NOT a store that never discounts; it is a store that never fills the field in.
# Read raw, this signal says "the top of the independent tier doesn't do sales" —
# a false statement about someone else's business, of the kind we put in writing.
none_set = compare_at_signal(product([{"price": "180.00", "available": True},
                                      {"price": "180.00", "available": True}]))
check("an unset compare_at is None, and the variants are still counted",
      none_set == (None, 2, 0), str(none_set))
some_set = compare_at_signal(product([
    {"price": "120.00", "compare_at_price": "240.00", "available": True},
    {"price": "120.00", "compare_at_price": None, "available": True},
    {"price": "120.00", "compare_at_price": "240.00", "available": False}]))
check("compare_at is the struck-through price of the first priced variant",
      some_set[0] == 240.0, str(some_set))
check("fill is counted over EVERY variant, not just the first",
      some_set[1:] == (3, 2), str(some_set))
check("a zero compare_at counts as unset, not as a free item",
      compare_at_signal(product([{"price": "90", "compare_at_price": "0.00",
                                  "available": True}])) == (None, 1, 0))
check("a junk compare_at cannot crash a build",
      compare_at_signal(product([{"price": "90", "compare_at_price": "n/a",
                                  "available": True}])) == (None, 1, 0))
check("a product with no variants is (None, 0, 0), which is not a 0% fill",
      compare_at_signal(product([])) == (None, 0, 0))
check("the per-store fill rate is RECORDED, not inferred later",
      '"capVariants": cap_variants' in _main and '"capFilled": cap_filled' in _main)
check("both numbers are stored, so 0/12 and 0/4000 stay different facts",
      "cap_variants += _nvar" in _main and "cap_filled += _nfill" in _main)
check("the fill-rate spread is written down where the field is read",
      "damsonmadder 0%" in inspect.getsource(build_catalog)
      or "damson" in inspect.getsource(build_catalog).lower())
check("observed price movement is still declared primary",
      "primary" in inspect.getsource(build_catalog.compare_at_signal)
      or "stays primary" in inspect.getsource(build_catalog))


# ═════════════════════════════════════════════════════════════════════════════
# 4. THE INVENTORY PASS — fail-soft is the entire contract
# ═════════════════════════════════════════════════════════════════════════════
def items(domain, n):
    return {"brand": domain.split(".")[0], "items": [
        (f"{domain}-p{i}", f"p{i}", "dresses", 200 + i) for i in range(n)]}


ALL = {"a.com": items("a.com", 20), "b.com": items("b.com", 20),
       "c.com": items("c.com", 20)}


def fetch_ok(domain, handle):
    return "ok", [(f"{domain}:{handle}:v1", 3, True, "deny", True),
                  (f"{domain}:{handle}:v2", 0, False, "deny", True)]


def fetch_mixed(domain, handle):
    if domain == "b.com":
        return "no-field", []          # the store stopped exposing it
    if domain == "c.com":
        return "err:HTTPError", []     # the store is broken today
    return fetch_ok(domain, handle)


got = collect_stock(ALL, fetch=fetch_ok, budget=200, per_store=5)
check("a healthy pass records every store", len(got["stores"]) == 3)
check("a healthy pass polls per_store products per store",
      got["requests"] == 15, str(got["requests"]))
check("every variant of every polled product lands in the panel",
      len(got["panel"]) == 30, str(len(got["panel"])))

mixed = collect_stock(ALL, fetch=fetch_mixed, budget=200, per_store=5)
# The one that matters most. The field's ABSENCE is a normal state — it is the
# default for every Shopify store created after 2017-12-05 — so it may not raise,
# may not be logged as an error, and may not cost the other stores anything.
check("a missing inventory_quantity is not an error",
      mixed["stores"]["b.com"]["errors"] == 0
      and mixed["stores"]["b.com"]["exposes"] is False)
check("a store that stops exposing the field is reported, not swallowed",
      [d["domain"] for d in mixed["dropped"]] == ["b.com"], str(mixed["dropped"]))
check("...and it costs ONE request, not fifteen a day forever",
      mixed["requests"] == 5 + 1 + 5, str(mixed["requests"]))
check("an ERRORING store is counted as an error, not as an empty store",
      mixed["stores"]["c.com"]["errors"] == 5
      and mixed["stores"]["c.com"]["exposes"] is False)
check("'we could not look' and 'there was nothing' are different rows",
      "c.com" not in [d["domain"] for d in mixed["dropped"]])
check("a dropped-out store costs the healthy ones nothing",
      len([r for r in mixed["panel"] if r[0] == "a.com"]) == 10)

# Nothing in this path may raise. The catalog is already on disk by the time it
# runs, but a traceback would still lose the panel AND the run's exit code.
def fetch_boom(domain, handle):
    raise RuntimeError("network on fire")


try:
    collect_stock(ALL, fetch=variant_stock, budget=0, per_store=5)
    check("a zero budget is a no-op, not an exception", True)
except Exception as e:  # noqa: BLE001
    check("a zero budget is a no-op, not an exception", False, repr(e))
_vs = inspect.getsource(variant_stock)
check("the one network call in the pass declares that it never raises",
      "NEVER raises" in _vs)
check("it reuses the ONE global pacer instead of its own",
      "fetch_json(" in _vs and "_pace" in _vs)
check("it backs off on 429 the way agents.md asks",
      "429" in _vs and "30.0" in _vs)
check("an absent field returns a status, not an exception",
      '"no-field"' in _vs)

# Budget. The daily job is ~540 requests / ~25 min; an unbounded extra GET per
# product on 33 stores would be ~10,000 requests against shops we do not pay,
# and REQUEST_BUDGET would then have truncated the WALK to pay for it.
capped = collect_stock(ALL, fetch=fetch_ok, budget=7, per_store=5)
check("the budget is a hard stop", capped["requests"] <= 7, str(capped["requests"]))
check("running out of budget is stated, not silent", capped["budgetExhausted"] is True)
# The bias that a store-at-a-time walk would bake in permanently: with a budget
# that runs out, the stores at the end of the sort would get nothing — not once,
# but every day, in the same order, invisibly, forever.
check("a budget cut-off is shared across stores, not paid by the last ones",
      all(capped["stores"][d]["variants"] > 0 for d in ("a.com", "b.com", "c.com")),
      str({d: m["variants"] for d, m in capped["stores"].items()}))
check("the fairness reason is written down where it would be undone",
      "FAIRNESS" in inspect.getsource(collect_stock)
      and "EVERY DAY" in inspect.getsource(collect_stock))
check("the inventory budget is separate from the walk's",
      INVENTORY_BUDGET != build_catalog.REQUEST_BUDGET)
check("the inventory budget is bounded by the roster it can reach",
      INVENTORY_BUDGET <= len(INVENTORY_STORES) * INVENTORY_PER_STORE
      + INVENTORY_CANARIES + 25)
check("the pass cannot outspend its own arithmetic",
      len(INVENTORY_STORES) * INVENTORY_PER_STORE + INVENTORY_CANARIES
      <= INVENTORY_BUDGET)

# The canary. A hardcoded list of 33 domains in a daily job is a list that is
# wrong later; this is what stops it going stale in the "someone new started
# exposing" direction, for 8 requests a day.
canary = collect_stock({}, canaries=[("new.com", "h")], fetch=fetch_ok, budget=9)
check("a canary that exposes the field is surfaced by name",
      canary["newExposers"] == ["new.com"], str(canary["newExposers"]))
check("a canary that does not is silent",
      collect_stock({}, canaries=[("x.com", "h")],
                    fetch=lambda d, h: ("no-field", []))["newExposers"] == [])
check("the roster of exposing stores is non-trivial", len(INVENTORY_STORES) >= 25)
check("it is stored as bare lowercase domains",
      all(d == d.lower() and "/" not in d for d in INVENTORY_STORES))
check("no duplicates in the roster", len(set(INVENTORY_STORES)) == len(INVENTORY_STORES))


# ── The cohort must be STABLE, or the panel never joins to itself ────────────
# This is the failure mode that looks exactly like success: a panel that runs,
# spends the requests, writes the file, and can never be differenced. Python's
# hash() of a str is salted PER PROCESS, so a cohort built on it would be
# different on every run and nothing would ever say so.
ids = [f"brand-piece-{i}" for i in range(40)]
check("the cohort is deterministic", stock_cohort(ids, 10) == stock_cohort(ids, 10))
check("the cohort is the right size", len(stock_cohort(ids, 10)) == 10)
grown = ids + [f"brand-newdrop-{i}" for i in range(40)]
kept = set(stock_cohort(ids, 10)) & set(stock_cohort(grown, 10))
check("most of the cohort survives the store publishing 40 new pieces",
      len(kept) >= 5, f"kept {len(kept)} of 10")
shuffled = list(reversed(ids))
check("cohort membership does not depend on walk order",
      set(stock_cohort(ids, 10)) == set(stock_cohort(shuffled, 10)))
check("an empty store is an empty cohort, not a crash", stock_cohort([], 10) == [])
check("a store smaller than the cohort is fine", len(stock_cohort(ids[:3], 10)) == 3)
_sc = inspect.getsource(stock_cohort)
check("the cohort hash is crc32, NOT Python's per-process-salted hash()",
      "zlib.crc32" in _sc)
check("the reason hash() cannot be used is written down where it would be used",
      "salted" in _sc and "per PROCESS" in _sc)


# ═════════════════════════════════════════════════════════════════════════════
# 5. THE PROMISE — what this record is allowed to be, encoded
# ═════════════════════════════════════════════════════════════════════════════
check("an aggregate below the floor is refused",
      stock_aggregate_ok(MIN_BRANDS_PER_AGGREGATE - 1) is False)
check("an aggregate at the floor is allowed",
      stock_aggregate_ok(MIN_BRANDS_PER_AGGREGATE) is True)
check("one brand is never an aggregate", stock_aggregate_ok(1) is False)
check("True is not a brand count", stock_aggregate_ok(True) is False)
check("a non-number is refused", stock_aggregate_ok("lots") is False)
check("the floor is a real floor", MIN_BRANDS_PER_AGGREGATE >= 3)

rec = stock_record(got, "salt-one", "env", "2026-08-07", "2026-08-07T08:00:00Z")
blob = json.dumps(rec)
check("no store domain survives into the published record",
      not any(d in blob for d in ("a.com", "b.com", "c.com")), blob[:200])
check("no product id or handle survives either",
      "a.com-p0" not in blob and '"p0"' not in blob)
check("the panel still carries the numbers", rec["count"] == 30)
check("the panel schema is declared",
      rec["schema"][:2] == ["storeKey", "variantKey"], str(rec["schema"]))
check("rows are arrays, like shelf.json's",
      all(isinstance(r, list) for r in rec["panel"]))
_ci = rec["schema"].index("category")
_bi = rec["schema"].index("priceBand")
check("a row carries a category and a price BAND, never an exact price",
      rec["panel"][0][_ci] == "dresses" and rec["panel"][0][_bi] in
      ("<100", "100-199", "200-349", "350-599", "600+"), str(rec["panel"][0]))
check("no row carries a raw price", not any(isinstance(c, float) for c in rec["panel"][0]))

# ── The 2.4% that would have poisoned every units figure ─────────────────────
# When a shop does not manage a variant's inventory through Shopify,
# `inventory_management` is null, `available` is true regardless of the number,
# and the number itself is a stale remnant. Measured on 2,502 live variants:
# 61 untracked, available on 61 of 61, quantities down to −208. Recorded and
# FLAGGED rather than dropped, because a store that starts tracking is a fact
# too — but a consumer that cannot tell them apart computes nonsense.
check("`tracked` is part of the schema", "tracked" in rec["schema"])
_ti = rec["schema"].index("tracked")


def fetch_untracked(domain, handle):
    return "ok", [(f"{domain}:{handle}:v1", 5, True, "deny", True),
                  (f"{domain}:{handle}:v2", -208, True, "deny", False)]


_mixmgmt = stock_record(
    collect_stock({"a.com": items("a.com", 1)}, fetch=fetch_untracked,
                  budget=9, per_store=1),
    "s", "env", "2026-08-07", "2026-08-07T08:00:00Z")
check("an untracked variant is kept, not silently dropped", _mixmgmt["count"] == 2)
check("...and is flagged so it can be excluded from a units figure",
      sorted(r[_ti] for r in _mixmgmt["panel"]) == [0, 1],
      str([r[_ti] for r in _mixmgmt["panel"]]))
check("a tracked, deny-policy row still obeys available == (qty > 0)",
      all((r[2] > 0) == bool(r[3]) for r in rec["panel"]
          if r[_ti] == 1 and r[4] == 1))
check("the three-way split is written down where the field is read",
      "untracked" in inspect.getsource(variant_stock)
      and "2,502" in inspect.getsource(variant_stock))

same = stock_record(got, "salt-one", "env", "2026-08-07", "2026-08-07T08:00:00Z")
other = stock_record(got, "salt-two", "env", "2026-08-07", "2026-08-07T08:00:00Z")
check("the same salt gives the same keys — this is what makes it a time series",
      [r[1] for r in same["panel"]] == [r[1] for r in rec["panel"]])
check("a different salt gives different keys",
      [r[1] for r in other["panel"]] != [r[1] for r in rec["panel"]])
check("the salt itself is never published",
      "salt-one" not in blob and "salt-two" not in json.dumps(other))
check("two days built on the same salt declare the same saltId",
      rec["saltId"] == same["saltId"] and len(rec["saltId"]) >= 8)
check("two days built on DIFFERENT salts say so, so nobody joins them by accident",
      other["saltId"] != rec["saltId"])
check("the saltId is a hash, not the salt", rec["saltId"] not in ("salt-one", "salt-two"))

# The degradation that must never be quiet: no secret -> random salt -> the rows
# cannot be differenced against any other day, which is the entire signal.
eph = stock_record(got, "whatever", "ephemeral", "2026-08-07", "2026-08-07T08:00:00Z")
check("a run with no configured salt marks itself UNJOINABLE",
      eph["joinable"] is False)
check("a configured salt is joinable", rec["joinable"] is True)
check("a borrowed salt is joinable",
      stock_record(got, "s", "derived", "d", "t")["joinable"] is True)
check("the unjoinable case is shouted at the operator too",
      "UNJOINABLE PANEL" in _main)
check("the fix is named in the shout", "LOUPE_STOCK_SALT" in _main)

use = rec.get("use") or {}
check("the record states what it may be used for", bool(use.get("permitted")))
check("...and what it may never be used for", len(use.get("forbidden") or []) >= 4)
check("the floor travels with the file",
      use.get("minBrandsPerAggregate") == MIN_BRANDS_PER_AGGREGATE)
_forbidden = " ".join(use.get("forbidden") or []).lower()
for phrase in ("named brand", "competitor", "outreach"):
    check(f"the forbidden list names '{phrase}'", phrase in _forbidden)
check("the record explains WHY, not just WHAT", len(use.get("why") or "") > 200)
check("the record admits what pseudonymisation does NOT buy",
      "cryptographic" in (use.get("pseudonymLimit") or "").lower())

_mod = inspect.getsource(build_catalog)
check("the boundary is a comment in the code, not only a field in the output",
      "did not intend to publish stock levels" in _mod)
check("the code records that no bypass or rate-limit evasion is involved",
      "unauthenticated" in _mod and "agents.md" in _mod)
check("the code says why telling a brand its own stock is pointless",
      "Shopify admin" in _mod)


# ═════════════════════════════════════════════════════════════════════════════
# 6. ORDERING — the pass CANNOT cost the catalog anything
# ═════════════════════════════════════════════════════════════════════════════
# The blast radius of a failing pre-build gate was four days of archive. The
# blast radius of this one is one day of one optional file, and that is a
# property of WHERE it runs, not of how carefully it is written.
check("the inventory pass runs after catalog.json is written",
      _main.index("STOCK_FILE.write_text") > _main.index("OUT_FILE.write_text"))
check("...and after shelf.json is written",
      _main.index("STOCK_FILE.write_text") > _main.index("SHELF_FILE.write_text"))
check("...and after the catalog version stamp",
      _main.index("STOCK_FILE.write_text") > _main.index("write_meta()"))
check("the whole pass is caught, so it cannot abort the run",
      "Inventory panel: SKIPPED" in _main)
check("the skip message says the catalog is unaffected",
      "are already written and are unaffected" in _main)
check("the pass can be switched off without a deploy", 'os.environ.get("STOCK_PANEL"' in _main)
check("the request cost is written down before anyone raises it",
      "~10,000 requests" in _mod and "REQUEST_BUDGET" in _mod)

check("price_band never crashes on junk", price_band(None) == "?" and price_band("x") == "?")
check("price_band is coarse, not a fingerprint",
      price_band(199) == "100-199" and price_band(200) == "200-349"
      and price_band(5000) == "600+")


if failures:
    print("MARKET-SIGNAL REGRESSIONS (%d):" % len(failures))
    print("\n".join(failures))
    raise SystemExit(1)
print("market signals: offered size run, compare_at calibration, inventory panel "
      "and its boundary — all OK")
