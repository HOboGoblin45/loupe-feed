#!/usr/bin/env python3
"""Sampling fixtures — the measurement guarantees, not the plumbing.

This file exists because the flaw it pins was invisible for six weeks, produced
clean-looking numbers the whole time, and those numbers went into a public
market index and into outreach emails to the labels being measured.

WHAT HAPPENED

brands.json set perBrand = 60 and Shopify's /products.json returns published_at
DESCENDING, so what the daily scrape captured was never a brand's catalogue — it
was the 60 most recently published pieces. Measured live on 2026-08-06, the
median store on the roster publishes 225 eligible pieces (p75 358, p90 626, max
3,104); only 9% of stores fit inside 60. For the other 91% the tracked shelf
ROTATED, and a piece pushed off the front by a newer listing was indistinguishable
from a piece that left the market.

Cost: 981 of 2,041 disappearances between 2026-07-16 and 2026-08-01 — 48% — were
whole-brand rotation. Bec + Bridge "lost" 60 of 60 products and finished the
window holding 60. Nothing sold out.

THE THREE THINGS THAT MUST NOT COME BACK

1. ONE NUMBER DOING TWO JOBS. The walk depth (measurement) and the display cap
   (what the app downloads, and therefore what becomes a ~111 KB cutout blob and
   an embedding) have opposite requirements. Merging them again would either
   re-break the measurement or push catalog.json past jsDelivr's ~20 MB file
   ceiling and take the feed off the air.

2. A WALK THAT LOOKS LIKE IT RUNS AND DOESN'T. The brand walk used `since_id`,
   which assumes ASCENDING ids; products.json returns DESCENDING. At a 60 cap
   one page always sufficed so the bug could not show. Raising the cap without
   fixing it would have spent the requests, logged the pages, and still stopped
   at ~one page per brand — a fix that changed nothing while looking like it had.

3. AN ARRIVAL SPIKE ON THE DAY THE CAP MOVES. Thousands of pieces that were
   always for sale become visible at once. Under the old rule every one would be
   stamped addedAt = today, firing new-drop push on ~20,000 items and printing a
   several-hundred-percent "refresh rate" for brands that published nothing.
"""
import inspect
import json
import pathlib
import re

import build_catalog
from build_catalog import (
    DEFAULT_WALK_DEPTH,
    MAX_VARIANTS_PER_BASE,
    MIN_REQUEST_INTERVAL,
    REQUEST_BUDGET,
    budget_left,
    effective_cap,
    normalize,
)

HERE = pathlib.Path(__file__).resolve().parent
failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# ── 1. Two caps, two jobs ────────────────────────────────────────────────────
cfg = json.loads((HERE / "brands.json").read_text(encoding="utf-8"))
walk = int(cfg.get("perBrand", 0))
disp = int(cfg.get("perBrandDisplay", 0))

check("brands.json declares a walk depth", walk > 0)
check("brands.json declares a separate display cap", disp > 0)
check("the walk depth is deeper than the display cap", walk > disp)
# The whole point. 60 was the rotating window; anything at or below it is the bug.
check("the walk depth clears the old 60-item front", walk >= 250)
# Measured roster (157 of 161 stores walked to exhaustion, 2026-08-06): p90 of
# eligible catalogue size is 638 pieces, median 157, max 3,104, 45,718 total.
check("the walk depth clears the roster's 90th percentile (638)", walk >= 638)
# Shopify pages are 250, so a walk depth is really a page count. A depth that is
# not a whole number of pages buys nothing and costs a request.
check("the walk depth is a whole number of Shopify pages", walk % 250 == 0)
# catalog.json is 1.09 KB/product against jsDelivr's ~20 MB ceiling, and every
# product becomes a ~111 KB cutout. ~180 labels x display cap must stay sane.
check("the display cap keeps catalog.json inside its ceiling", disp <= 120)

check("mainstream houses are still display-capped below the tier",
      effective_cap("Khaite", disp) < disp)
check("an indie label gets the full display budget",
      effective_cap("Fait Par Foutch", disp) == disp)
check("the display cap governs the deck, not the walk",
      effective_cap("Khaite", walk) != walk)

_main = inspect.getsource(build_catalog.main)
check("the walk depth is read from perBrand", 'cfg.get("perBrand"' in _main)
check("the display cap is read from its own key", 'cfg.get("perBrandDisplay"' in _main)
check("an old brands.json cannot silently ship a 750-per-brand catalog",
      'cfg.get("perBrandDisplay", 60)' in _main)
check("the display cap can never exceed the walk depth",
      "per_brand_display > walk_depth" in _main)


# ── 2. The walk must actually walk ───────────────────────────────────────────
_scrape = None
for name, obj in [(n, o) for n, o in [("main", build_catalog.main)]]:
    _scrape = inspect.getsource(obj)
check("the brand walk paginates by page number, not since_id",
      "scrape_page(domain, PAGE_LIMIT, page=n)" in _main)
check("since_id is no longer used to advance the brand walk",
      "since_id = last_id" not in _main)
check("the reason since_id collapses is written down where it was fixed",
      "DESCENDING" in _main and "collapses" in _main)
# Not an argument, a measurement: on carmensays.com a since_id walk returned
# page 1 again (250 of 250 overlapping) while page=2 returned 145 fresh items.
check("the collapse is recorded as a measurement, not a belief",
      "carmensays.com" in _main and "0 overlap with page 1" in _main)
check("pages are requested at Shopify's maximum", "PAGE_LIMIT = 250" in _main)
# Stopping at the display cap is what MADE the rotating window. If the walk ever
# breaks on the display cap again, the shelf is a front again.
check("the walk does not stop at the display cap",
      "if got >= walk_cap" in _main and "if got >= display_cap" not in _main)


# ── 3. Politeness — the budget is global, because the throttle is ────────────
# A sizing probe on 2026-08-06 ran six domains concurrently and got HTTP 429
# from 151 of 162 stores, robots.txt included. Shopify throttles per CLIENT.
check("there is a minimum interval between storefront requests",
      MIN_REQUEST_INTERVAL >= 0.25)
check("there is a hard ceiling on requests per run", 0 < REQUEST_BUDGET <= 5000)
check("the budget is big enough for the roster at this walk depth",
      REQUEST_BUDGET >= 161 * (walk / 250 + 1))
check("budget_left counts down from the budget", budget_left() <= REQUEST_BUDGET)

_fetch = inspect.getsource(build_catalog.fetch_json)
check("every storefront fetch is paced in ONE place", "_pace()" in _fetch)
_pace_body = "\n".join(
    ln for ln in inspect.getsource(build_catalog._pace).splitlines()
    if not ln.strip().startswith(("#", '"', "'")))
check("pacing never raises (a budget overrun must not cost a brand its items)",
      not re.search(r"^\s*raise\b", _pace_body, re.M))
check("the walk stops when the budget is spent", "budget_left() <= 0" in _main)
# The pre-existing per-brand and per-page sleeps are a second net. Removing them
# in favour of the global pacer would be a net loss of politeness.
check("the per-brand sleep is still there", "time.sleep(0.5)" in _main)
check("the retailer page delay is still there", "time.sleep(delay)" in _main)
check("retry backoff is still there", "if is_429 else 1.5) * (attempt + 1)" in _main)
# Every Shopify store's /agents.md states one obligation for read-only access:
# "Respect rate limits ... Back off on 429 responses." A 429 retried at 1.5s is
# not a back-off, and that is what produced the 151-of-162 wipeout.
check("a 429 backs off far harder than an ordinary flake",
      "e.code == 429" in _main and "30.0 if is_429" in _main)
check("the published agent contract is cited where it is honoured",
      "agents.md" in inspect.getsource(build_catalog))


# ── 4. No arrival spike on the boundary ──────────────────────────────────────
check("normalize carries the store's own publish date",
      '"publishedAt"' in inspect.getsource(normalize))
check("addedAt prefers the store's publish date for a newly-seen piece",
      'pub = product.get("publishedAt")' in _main)
check("a store date may only make a piece OLDER, never newer",
      "d < _today" in _main)
check("a malformed store date falls back rather than crashing the build",
      "except ValueError" in _main)

_fake = {
    "title": "Bias Slip Dress", "handle": "bias-slip-dress",
    "published_at": "2026-03-11T09:00:00-05:00",
    "images": [{"src": "https://cdn.shopify.com/x.jpg"}],
    "variants": [{"price": "180.00", "available": True, "title": "S"}],
}
_n = normalize(_fake, "Test Label", "test.com", 1.0)
check("normalize actually extracts the date", _n and _n.get("publishedAt") == "2026-03-11")
check("the extracted date is a date, not a timestamp",
      _n and re.fullmatch(r"\d{4}-\d{2}-\d{2}", _n["publishedAt"] or ""))
_no_date = dict(_fake)
_no_date.pop("published_at")
check("a store that publishes no date is not an error",
      (normalize(_no_date, "Test Label", "test.com", 1.0) or {}).get("publishedAt") is None)


# ── 5. shelf.json — the observation, and what it must never conflate ─────────
check("the full walk is written somewhere other than catalog.json",
      "SHELF_FILE.write_text" in _main)
check("shelf rows are arrays, not objects (60 bytes vs 1.09 KB)",
      '"schema": ["id", "brandIdx", "price", "available", "publishedAt", "category"]' in _main)
check("shelf.json declares the walk depth it was built at", '"walkDepth"' in _main)
check("shelf.json declares the sampling epoch", '"samplingEpochs"' in _main)
# The one that matters most: an unreachable store is not an empty store.
check("shelf.json records stores it could not reach", '"failed"' in _main)
check("a scrape failure is recorded, not just logged",
      "observed_failed[brand] = type(e).__name__" in _main)
check("shelf.json says how much of the request budget it used",
      '"requestsMade"' in _main and '"budgetExhausted"' in _main)
# "We saw the whole store" and "we ran out of budget" must never be the same
# flag — that is the shallow-clone mistake wearing different clothes.
check("'exhausted' means the STORE ran out, not that we did",
      '"exhausted": bool(exhausted)' in _main)
check("a budget-truncated walk says so per brand", '"budgetStopped"' in _main)
check("the observation is taken BEFORE display trimming",
      _main.index("observed[brand] = bucket") < _main.index("by_brand[brand] = bucket[:display_cap]"))

# The variant cap still applies to the walk: it is a data-quality rule (a
# 5-colourway loafer is one product), not a display rule.
check("the variant cap is still a real cap", 1 <= MAX_VARIANTS_PER_BASE <= 3)


# ── 6. The incident is written down where the next person will look ──────────
_doc = build_catalog.__doc__ or ""
_cap_block = _main[:0] + inspect.getsource(build_catalog)
check("the rotation measurement is recorded in the source",
      "981" in _cap_block and "48%" in _cap_block)
check("the reason the display cap cannot simply be raised is recorded",
      "jsDelivr" in _cap_block and "cutout" in _cap_block)
check("brands.json explains both numbers to whoever edits it next",
      "_cap_comment" in cfg and "rotating" in cfg["_cap_comment"])
check("brands.json records where the walk depth came from",
      "638" in cfg["_cap_comment"] and "157 eligible" in cfg["_cap_comment"])
check("DEFAULT_WALK_DEPTH is a real fallback, not the old front",
      DEFAULT_WALK_DEPTH >= 638)


if failures:
    print("FAIL — %d sampling guarantees broken:" % len(failures))
    for f in failures:
        print("   " + f)
    raise SystemExit(1)
print("OK — sampling: walk depth, politeness budget, arrival stamping and "
      "shelf.json guarantees all hold")
