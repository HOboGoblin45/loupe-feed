import re
#!/usr/bin/env python3
"""Square Online source fixtures -- run by CI before every catalog build.

Plain asserts (no pytest dep), same as the other fixture files. The fixture is
a trimmed copy of a REAL storefront response (rbk-lab.square.site, 2026-11-09),
so these checks describe what the API actually returns, not what we hoped.
"""
import copy
import json
from pathlib import Path

import build_catalog
import square_source

FIX = json.loads((Path(__file__).parent / "fixtures" / "square_products_rbk_lab.json")
                 .read_text(encoding="utf-8"))
ENTRY = {"brand": "RBK Lab", "domain": "rbk-lab.square.site", "currency": "JPY",
         "currencyVerified": True, "platform": "square",
         "squareUserId": "156408825", "squareSiteId": "299912790691262459",
         "inStockOnly": True}
FX = {"USD": 1.0, "JPY": 0.006369}
checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def by_id(sid):
    return copy.deepcopy(next(p for p in FIX["data"] if p["id"] == sid))


SKIRT, JACKET, KIMONO = "J6HA6ACNSWJLTN6MRGCYXCK7", "LGWGZUDFWLZ6BJTPIET3F2KC", "OY73CI7VJ752EQEC6OKTYAEA"

# ── the entry is recognised, and only when it opts in ────────────────────────
ok(square_source.is_square(ENTRY), "platform: square not recognised")
ok(not square_source.is_square({"brand": "X", "domain": "x.com"}), "a Shopify entry read as Square")
ok(not square_source.is_square(None), "None entry read as Square")

# ── URL building: ids come from a hand-edited file and go into a URL ──────────
url = square_source.products_url("156408825", "299912790691262459", 2)
ok(url.startswith("https://cdn5.editmysite.com/app/store/api/v28/editor/users/156408825/"
                  "sites/299912790691262459/products?page=2&per_page=60"), f"unexpected URL {url}")
for bad in ("", None, "12/../x", "a b", "1?x=1"):
    try:
        square_source.products_url(bad, "299912790691262459")
        ok(False, f"accepted a bad user id {bad!r}")
    except ValueError:
        ok(True, "")

# ── one real product through the adapter ─────────────────────────────────────
shaped = square_source.to_shopify_shape(by_id(SKIRT), ENTRY["domain"], ENTRY["brand"])
ok(shaped is not None, "real product rejected")
ok(shaped["title"] == "One-of-a-Kind Reconstructed Denim Skirt | Japanese Wearable Art",
   f"brand suffix not stripped: {shaped['title']!r}")
ok(shaped["handle"] == "one-of-a-kind-reconstructed-denim-skirt-japanese-wearable-art-rbk-lab", "wrong handle")
ok(shaped["_url"] == "https://rbk-lab.square.site/product/"
   "one-of-a-kind-reconstructed-denim-skirt-japanese-wearable-art-rbk-lab/J6HA6ACNSWJLTN6MRGCYXCK7",
   "product URL is not the storefront's own")
ok(shaped["variants"] == [{"price": "42500", "available": True}], f"variant: {shaped['variants']}")
ok(len(shaped["images"]) == 3 and all(i["src"].endswith("?width=1280") for i in shaped["images"]),
   "images must use the 1280px rendition, not the 2560px original")
ok(shaped["published_at"].startswith("2026-10-21"), "created_date not carried as published_at")

# ── and on through the SAME normalize() every Shopify row uses ───────────────
row = build_catalog.normalize(shaped, ENTRY["brand"], ENTRY["domain"], FX["JPY"],
                              currency="JPY", verified=True)
ok(row is not None, "normalize() dropped a real Square product")
ok(row["id"] == "rbk-lab-one-of-a-kind-reconstructed-denim-skirt-japanese-wearable-art-rbk-lab", row["id"])
ok(row["brand"] == "RBK Lab", "brand")
ok(row["priceRaw"] == 42500 and row["currency"] == "JPY", "raw price / currency not preserved")
ok(row["price"] == round(42500 * 0.006369) == 271, f"USD price {row['price']}")
ok(row["category"] == "bottoms", f"a skirt categorised as {row['category']}")
ok(row["available"] is True and row["sizes"] == [], "one-of-a-kind piece: available, no size run")
ok(row["publishedAt"] == "2026-10-21", "publishedAt")
ok(row["affiliateUrl"].startswith("https://rbk-lab.square.site/product/") and
   "utm_source=loupe" in row["affiliateUrl"], f"link must go to their store, tagged: {row['affiliateUrl']}")
ok("/products/" not in row["affiliateUrl"], "Shopify path leaked into a Square link")
ok("currencyUnverified" not in row, "hand-verified currency flagged unverified")
ok(not any(k.startswith("_") for k in row), "private adapter key leaked into catalog.json")

jacket = build_catalog.normalize(
    square_source.to_shopify_shape(by_id(JACKET), ENTRY["domain"], ENTRY["brand"]),
    ENTRY["brand"], ENTRY["domain"], FX["JPY"], currency="JPY")
ok(jacket["category"] == "outerwear", f"a jacket categorised as {jacket['category']}")
ok(jacket["price"] == 188, f"jacket USD price {jacket['price']}")
# "Dress First, Plan Later ... Tiered Skirt": the keyword rules read "dress".
# That is a known limit of title inference, so the roster entry pins it.
unpinned = build_catalog.normalize(
    square_source.to_shopify_shape(by_id(KIMONO), ENTRY["domain"], ENTRY["brand"]),
    ENTRY["brand"], ENTRY["domain"], FX["JPY"], currency="JPY")
ok(unpinned["category"] == "dresses", "title inference changed: the pin may no longer be needed")
ROSTER = next(e for e in json.loads(build_catalog.BRANDS_FILE.read_text(encoding="utf-8"))["brands"]
              if e["brand"] == "RBK Lab")
kimono = build_catalog.normalize(
    square_source.to_shopify_shape(by_id(KIMONO), ENTRY["domain"], ENTRY["brand"],
                                   ROSTER.get("categoryByHandle")),
    ENTRY["brand"], ENTRY["domain"], FX["JPY"], currency="JPY")
ok(kimono["category"] == "bottoms",
   f"'Denim Kimono Tiered Skirt' is a skirt, categorised as {kimono['category']}")
bogus = square_source.to_shopify_shape(by_id(SKIRT), ENTRY["domain"], ENTRY["brand"],
                                       {by_id(SKIRT)["site_link"].split("/")[1]: "gradient(red)"})
ok(build_catalog.normalize(bogus, "RBK Lab", ENTRY["domain"], FX["JPY"])["category"] == "bottoms",
   "an invalid category pin must be ignored, not published")
for pin in ROSTER.get("categoryByHandle", {}).values():
    ok(pin in build_catalog.VALID_CATEGORIES, f"roster pins an unknown category {pin!r}")

# ── display titles: shorter on Loupe, the store's listing untouched ───────────
J_SLUG = by_id(JACKET)["site_link"].split("/")[1]
full_j = jacket["name"]
shaped_j = square_source.to_shopify_shape(by_id(JACKET), ENTRY["domain"], ENTRY["brand"],
                                          ROSTER.get("categoryByHandle"), ROSTER)
ok(shaped_j["title"] == full_j, "the full store title must still feed inference")
row_j = build_catalog.normalize(shaped_j, ENTRY["brand"], ENTRY["domain"], FX["JPY"], currency="JPY")
ok(row_j["name"] == "Be the Part They Remember Jacket", f"display title: {row_j['name']!r}")
ok(row_j["category"] == "outerwear" and row_j["id"] == jacket["id"] and row_j["price"] == jacket["price"],
   "a display title changed something other than the name")
kim = build_catalog.normalize(
    square_source.to_shopify_shape(by_id(KIMONO), ENTRY["domain"], ENTRY["brand"],
                                   ROSTER.get("categoryByHandle"), ROSTER),
    ENTRY["brand"], ENTRY["domain"], FX["JPY"], currency="JPY")
ok(kim["name"] == "Dress First, Plan Later Tiered Skirt" and kim["category"] == "bottoms",
   f"kimono skirt: {kim['name']!r} / {kim['category']}")
# A new listing with no hand-written title falls back to the piece's own name.
dt = square_source.display_title
ok(dt("Go Sideways – Reconstructed Denim Skirt, One of a Kind", "new", {"shortTitles": True}) == "Go Sideways",
   "lead fallback")
ok(dt("Unexpected Balance — Reconstructed Denim Patchwork Skirt", "new", {"shortTitles": True}) == "Unexpected Balance",
   "lead fallback must read an em dash too")
ok(dt("One-of-a-Kind Denim Skirt", "new", {"shortTitles": True}) is None,
   "a hyphenated word is not a separator")
ok(dt("Go Sideways – Denim Skirt", "new", {}) is None, "no opt-in, no shortening")
ok(dt("Go Sideways – Denim Skirt", "new", {"titleByHandle": {"new": "  "}, "shortTitles": False}) is None,
   "a blank pin must not blank the title")
ok(square_source.to_shopify_shape(by_id(JACKET), ENTRY["domain"], ENTRY["brand"])["_display_title"] is None,
   "without a roster entry the store title is shown")
# Every pinned title fits the card (about two short lines) and is shorter than the original.
for slug, short in ROSTER.get("titleByHandle", {}).items():
    ok(0 < len(short) <= 40, f"display title too long for the card: {short!r}")
    ok(re.fullmatch(r"[a-z0-9-]+", slug) is not None, f"bad handle {slug!r}")
ok(J_SLUG in ROSTER["titleByHandle"], "the jacket has no display title")

# ── a Shopify row is untouched by the _url hook ──────────────────────────────
shop = build_catalog.normalize(
    {"title": "Silk Wrap Dress", "handle": "silk-wrap-dress", "images": [{"src": "https://x/y.jpg"}],
     "variants": [{"price": "220.00", "available": True}]}, "Some Label", "somelabel.com", 1.0)
ok("https://somelabel.com/products/silk-wrap-dress" in shop["affiliateUrl"]
   or "somelabel.com%2Fproducts%2Fsilk-wrap-dress" in shop["affiliateUrl"],
   f"Shopify product URL changed: {shop['affiliateUrl']}")

# ── sold out: every way the storefront can say it ────────────────────────────
p = by_id(SKIRT)
ok(not square_source.is_sold_out(p), "an in-stock piece read as sold out")
p["badges"]["out_of_stock"] = True
ok(square_source.is_sold_out(p), "out_of_stock badge ignored")
p = by_id(SKIRT); p["inventory"]["all_variations_sold_out"] = True
ok(square_source.is_sold_out(p), "all_variations_sold_out ignored")
p = by_id(SKIRT); p["inventory"]["marked_sold_out_at_all_existing_locations"] = True
ok(square_source.is_sold_out(p), "marked sold out ignored")
p = by_id(SKIRT); p["inventory"].update(enabled=True, total=0)
ok(square_source.is_sold_out(p), "tracked inventory at zero not sold out")
p = by_id(SKIRT); p["inventory"].update(enabled=False, total=0)
ok(not square_source.is_sold_out(p), "UNTRACKED inventory at zero must stay buyable")

# ── rows the adapter must refuse ─────────────────────────────────────────────
p = by_id(SKIRT); p["visibility"] = "hidden"
ok(square_source.to_shopify_shape(p, ENTRY["domain"]) is None, "hidden product listed")
p = by_id(SKIRT); p["site_link"] = "product/only-a-slug"
ok(square_source.to_shopify_shape(p, ENTRY["domain"]) is None, "malformed link accepted")
p = by_id(SKIRT); p["price"]["low"] = 0
ok(square_source.to_shopify_shape(p, ENTRY["domain"]) is None, "zero price accepted")
p = by_id(SKIRT); p["images"] = {"data": []}
ok(build_catalog.normalize(square_source.to_shopify_shape(p, ENTRY["domain"]), "RBK Lab",
                           ENTRY["domain"], FX["JPY"]) is None, "imageless product listed")

# ── a markdown becomes a compare_at_price, in the store's own currency ───────
p = by_id(SKIRT); p["on_sale"] = True; p["price"]["low"] = 30000
ok(square_source.to_shopify_shape(p, ENTRY["domain"])["variants"][0].get("compare_at_price") == "42500",
   "sale price lost its 'was' price")

# ── the page walk: paging, the stock filter, and the request budget ──────────
calls = []


def fake_fetch(url):
    calls.append(url)
    page = int(url.split("page=")[1].split("&")[0])
    if page == 1:
        d = [by_id(SKIRT), by_id(JACKET)]
        d[1]["badges"]["out_of_stock"] = True      # the jacket sold
        return {"data": d, "meta": {"pagination": {"total_pages": 2}}}
    if page == 2:
        return {"data": [by_id(KIMONO)], "meta": {"pagination": {"total_pages": 2}}}
    raise AssertionError("walked past the last page")


got = list(square_source.pages(ENTRY, fake_fetch))
ok(len(calls) == 2, f"expected 2 requests, made {len(calls)}")
ok([len(pg) for pg in got] == [1, 1], f"sold piece not dropped: {[len(pg) for pg in got]}")
ok(got[0][0]["handle"].startswith("one-of-a-kind"), "wrong survivor on page 1")

calls.clear()
keep_all = list(square_source.pages({**ENTRY, "inStockOnly": False}, fake_fetch))
ok([len(pg) for pg in keep_all] == [2, 1], "inStockOnly: false must keep sold pieces")
ok(keep_all[0][1]["variants"][0]["available"] is False, "sold piece must carry available: false")

calls.clear()
ok(list(square_source.pages(ENTRY, fake_fetch, budget_left=lambda: 0)) == [] and not calls,
   "an exhausted request budget must stop the walk before any request")

ok(list(square_source.pages(ENTRY, lambda u: {"data": []})) == [], "empty store must yield nothing")

# ── did the walk reach the END of the store? Only the adapter knows ───────────
st = {}
list(square_source.pages(ENTRY, fake_fetch, state=st))
ok(st == {"exhausted": True}, f"a completed walk must report exhausted: {st}")
st = {}
list(square_source.pages(ENTRY, lambda u: {"data": []}, state=st))
ok(st == {"exhausted": True}, "an empty store IS a completed walk")
st = {}
list(square_source.pages(ENTRY, fake_fetch, budget_left=lambda: 0, state=st))
ok(st == {"exhausted": False}, "a budget-stopped walk must NOT report exhausted")
left = [1]


def one_request_budget():
    left[0] -= 1
    return left[0] + 1


st = {}
part = list(square_source.pages(ENTRY, fake_fetch, budget_left=one_request_budget, state=st))
ok(len(part) == 1 and st == {"exhausted": False},
   f"stopped after page 1 of 2: one page, not exhausted; got {len(part)} {st}")
st = {}
sold = list(square_source.pages(
    ENTRY, lambda u: {"data": [dict(by_id(SKIRT), badges={"out_of_stock": True})],
                      "meta": {"pagination": {"total_pages": 1}}}, state=st))
ok(sold == [[]] and st == {"exhausted": True},
   "every piece sold: an empty page from a COMPLETED walk (the caller must not grace-carry it)")

# ── and the builder acts on it: no grace carry for a confirmed sell-out ───────
src = (build_catalog.HERE / "build_catalog.py").read_text(encoding="utf-8")
ok("label in confirmed_empty" in src and "confirmed_empty.add(brand)" in src,
   "build_catalog no longer excludes a confirmed-empty Square brand from grace carry-forward")
ok("not is_sq and len(page) < PAGE_LIMIT" in src,
   "build_catalog infers Square exhaustion from Shopify's page size again")

# ── the real roster entry is wired the way this file assumes ─────────────────
cfg = json.loads(build_catalog.BRANDS_FILE.read_text(encoding="utf-8"))
sq = [e for e in cfg["brands"] if square_source.is_square(e)]
ok(len(sq) >= 1, "no Square brand in brands.json")
for e in sq:
    ok(e.get("squareUserId") and e.get("squareSiteId"), f"{e['brand']}: missing Square ids")
    ok(e.get("currency") in cfg["fx_to_usd"], f"{e['brand']}: currency has no FX rate")
    ok(e.get("currencyVerified") is True and e.get("currencyCheckedAt"),
       f"{e['brand']}: a Square brand cannot be probed, so its currency must be hand-verified and dated")
    square_source.products_url(e["squareUserId"], e["squareSiteId"])   # raises if malformed

# ── no /cart.js probe is ever sent to a Square storefront ────────────────────
probed = []
cur, fx, verified, notes = build_catalog.resolve_currency(
    ENTRY, FX, probe=lambda d: None)
ok((cur, verified, notes) == ("JPY", True, []), f"currency resolution: {(cur, verified, notes)}")

print(f"square-source fixtures: all OK ({checks} checks)")
