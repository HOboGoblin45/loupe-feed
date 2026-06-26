#!/usr/bin/env python3
"""
Loupe — live catalog builder.

Pulls each curated brand's public Shopify product feed (https://<domain>/products.json),
normalizes every item into the app's Product shape, converts prices to USD, infers
category + color tags, and writes catalog.json.

Runs in CI (GitHub Actions) on a daily schedule. Pure standard library — no pip install.

Output (catalog.json):
  {
    "generatedAt": "2026-06-16T18:00:00Z",
    "count": 217,
    "products": [
      { "id", "brand", "name", "price", "category", "colorTags", "imageUrl", "affiliateUrl" },
      ...
    ]
  }
"""

import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
BRANDS_FILE = HERE / "brands.json"
OUT_FILE = HERE / "catalog.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ── Junk / non-product filter ─────────────────────────────────────────────────
# Real Shopify stores publish a lot of non-garment "products" through
# /products.json — gift cards, shipping/insurance/route add-ons, deposits,
# fabric swatches, sticker/sample packs, gift wrap, warranties. They have
# images and prices, so they pass normalize()'s basic checks and surface as
# junk swipe cards. We reject any product whose title contains one of these
# phrases (case-insensitive, word-aware so "shipping" matches but "ship" inside
# "relationship" does not). A second, softer rule drops anything cheap that ALSO
# reads like an add-on — this catches "Route Package Protection $0.98" style
# items without nuking genuinely cheap real products (a $12 hair clip stays).
JUNK_TITLE_PHRASES = [
    "gift card", "e-gift", "egift", "e gift card", "shipping", "insurance",
    "protection", "route", "checkout+", "checkout plus", "sticker", "sample",
    "deposit", "swatch", "donation", "gift wrap", "gift-wrap", "warranty",
    "add-on", "add on", "addon", "pre-order deposit", "store credit",
]
# Cheaper than this AND matching an add-on word → almost certainly not a garment.
JUNK_PRICE_FLOOR = 15
JUNK_ADDON_WORDS = [
    "shipping", "insurance", "protection", "route", "swatch", "sample",
    "sticker", "deposit", "donation", "gift", "warranty", "add-on", "add on",
    "addon", "wrap", "credit",
]


def _word_in(needle, hay):
    """True if `needle` appears in `hay` on word boundaries (handles multi-word
    phrases). Avoids matching e.g. 'ship' inside 'relationship'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", hay) is not None


def is_junk(title, price):
    """True if a product looks like a non-garment add-on / utility SKU."""
    hay = (title or "").lower()
    if not hay:
        return True
    for phrase in JUNK_TITLE_PHRASES:
        if _word_in(phrase, hay):
            return True
    # Cheap + add-on-flavored title → drop (keeps real low-priced products).
    if price is not None and price < JUNK_PRICE_FLOOR:
        if any(_word_in(w, hay) for w in JUNK_ADDON_WORDS):
            return True
    return False


# ── Category inference ────────────────────────────────────────────────────────
# Checked in priority order; first hit wins. Falls back to 'tops'.
#
# Category mapping decision (the app only supports 6 categories): the app's
# Category type — src/data/seedProducts.ts — and the catalog validator —
# src/services/catalog.ts (VALID_CATEGORIES) — accept ONLY:
#   tops · bottoms · dresses · outerwear · shoes · accessories
# Any other category string is *silently dropped* by the app's toProduct()
# validator. So we do NOT invent "swim"/"intimates"/"jumpsuits" categories
# (they'd vanish from the feed). Instead we route the mis-filed groups to the
# best-fitting existing category, with intent rather than accidental fall-through:
#   • jumpsuit / romper / playsuit / overall / boilersuit / unitard  → dresses
#       (closest one-piece, full-body silhouette; renders well in the deck)
#   • swim (bikini / swimsuit / one-piece / trunks)                   → tops
#   • intimates (bra / bralette / brief / lingerie / thong / corset)  → tops
# Swim + intimates land in "tops" because that's the closest existing bucket and
# ProductCard treats tops as a "cover" frame. They share the SWIM_INTIMATES
# keyword set below so a future real "swim"/"intimates" category is a one-liner.
SWIM_INTIMATES_KEYWORDS = [
    "swim", "bikini", "swimsuit", "swimwear", "one-piece", "one piece", "trunks",
    "bra", "bralette", "brief", "briefs", "lingerie", "thong", "knicker",
    "underwear", "intimate", "boyshort", "tankini", "rashguard", "rash guard",
]
JUMPSUIT_KEYWORDS = ["jumpsuit", "romper", "playsuit", "overall", "boilersuit",
                     "unitard", "catsuit"]
CATEGORY_RULES = [
    ("accessories", ["bag", "tote", "clutch", "pouch", "purse", "scarf", "necklace",
                      "earring", "bracelet", "ring", "pendant", "hat", "cap", "beret",
                      "belt", "sunglass", "jewel", "hair", "glove", "wallet"]),
    ("shoes",       ["shoe", "boot", "sandal", "mule", "flat", "sneaker", "heel",
                     "loafer", "pump", "clog", "slipper", "ballet"]),
    ("outerwear",   ["coat", "jacket", "blazer", "cardigan", "trench", "parka",
                     "anorak", "overcoat", "puffer"]),
    # One-piece full-body garments map to 'dresses' (closest existing silhouette).
    ("dresses",     JUMPSUIT_KEYWORDS),
    ("bottoms",     ["skirt", "trouser", "pant", "short", "jean", "legging",
                     "culotte", "capri"]),
    ("dresses",     ["dress", "gown"]),
    # Swim + intimates → 'tops' (best existing bucket; flag for a future real
    # 'swim'/'intimates' category). Checked before the generic tops keywords so a
    # bikini/bralette is categorized intentionally, not via a stray "set"/"tube".
    ("tops",        SWIM_INTIMATES_KEYWORDS),
    ("tops",        ["top", "shirt", "tee", "t-shirt", "blouse", "cami", "tank",
                     "sweater", "knit", "vest", "bodysuit", "corset", "bralette",
                     "halter", "tube", "set", "jumper", "polo", "turtleneck"]),
]

# ── Color inference ───────────────────────────────────────────────────────────
COLOR_RULES = [
    ("black",  ["black", "noir", "onyx", "jet"]),
    ("white",  ["white", "ivory", "blanc"]),
    ("pink",   ["pink", "rose", "blush", "fuchsia", "magenta", "vichy"]),
    ("blue",   ["blue", "navy", "teal", "cobalt", "denim", "azure", "sky", "indigo", "azul"]),
    ("green",  ["green", "sage", "olive", "celery", "khaki", "emerald", "mint", "verde", "forest"]),
    ("brown",  ["brown", "tan", "camel", "chocolate", "espresso", "caramel", "mocha", "taupe", "cognac", "marron"]),
    ("red",    ["red", "crimson", "cherry", "scarlet", "burgundy", "wine"]),
    ("neutral", ["cream", "ecru", "beige", "natural", "sand", "stone", "oat", "wheat",
                 "bone", "nude", "off-white", "butter", "vanilla", "grey", "gray", "charcoal", "silver"]),
]
MULTICOLOR_HINTS = ["print", "floral", "stripe", "check", "gingham", "multi", "foulard",
                    "patchwork", "rainbow", "tie-dye", "leopard", "animal", "paisley", "ditsy"]

VALID_COLORS = {"black", "white", "pink", "blue", "green", "brown", "red", "neutral", "multicolor"}

# ── Mainstream-house cap ──────────────────────────────────────────────────────
# Loupe's whole pitch is genuine *discovery* — niche, indie, micro-influencer
# labels. A handful of established designer houses are in the catalog for breadth
# and aspiration, but they aren't "discoveries", and at perBrand=60 each they'd
# crowd the deck and dilute the indie-forward feel. So we cap THESE brands at a
# much lower per-brand count (MAINSTREAM_CAP) while every indie brand keeps the
# full perBrand budget. Matching is case-insensitive and tolerant of accent
# variants (e.g. Toteme / Totême) by comparing on a normalized form of the
# brand name; add a normalized name here to cap a new mainstream house.
MAINSTREAM_CAP = 15


def _norm_brand(name):
    """Lowercased, accent/punctuation-folded brand key for mainstream matching.
    Folds Totême->toteme, 'LA Apparel'->'laapparel' so spelling/accent variants
    all collapse to one comparable token."""
    s = (name or "").lower()
    # Strip common accents we actually see (ê/é -> e) without pulling in a dep.
    for a, b in (("ê", "e"), ("é", "e"), ("è", "e"), ("ñ", "n"), ("í", "i"), ("á", "a"), ("ó", "o")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]", "", s)


# Established houses (not "discoveries"). Stored as normalized keys so accent /
# spacing / spelling variants match. Includes the brand names actually present
# in brands.json today plus the obvious aliases the audit called out.
MAINSTREAM_BRANDS = {
    _norm_brand(b) for b in [
        "The Row", "Khaite", "Toteme", "Totême", "Ganni", "Dries Van Noten",
        "Proenza Schouler", "Coperni", "Staud", "Cult Gaia", "Phoebe Philo",
        "Frankies Bikinis", "LA Apparel", "Los Angeles Apparel",
    ]
}


def effective_cap(brand, per_brand):
    """Per-brand product cap: mainstream houses are capped at MAINSTREAM_CAP so
    the feed stays indie-forward; everyone else keeps the full perBrand budget."""
    return MAINSTREAM_CAP if _norm_brand(brand) in MAINSTREAM_BRANDS else per_brand

# ── Sovrn Commerce affiliate wrapping ─────────────────────────────────────────
# When SOVRN_API_KEY is set (a GitHub Actions secret, injected as an env var),
# every product's affiliateUrl is wrapped in a Sovrn "Redirect API" link so the
# click is attributed to Loupe and earns commission. When the key is absent
# (e.g. local runs, or before the Sovrn account is approved), links pass through
# unchanged — the app still sends users straight to the brand's product page, so
# nothing breaks. This is a server-side switch: add the secret, the next catalog
# build monetizes all brands at once with no app update.
#
# Format (Sovrn Redirect API): https://redirect.viglink.com/?key=<KEY>&u=<dest>&cuid=<id>
#   key  = your Commerce API key (Platform → Commerce → Settings → "Key" icon)
#   u    = the destination URL, percent-encoded
#   cuid = optional Custom Tracking ID (<=32 alphanumeric chars) for reporting
# Confirm this exact format against one link from the dashboard's "Create Links"
# tool before going wide; the base/params are isolated here so it's a one-line tweak.
SOVRN_API_KEY = os.environ.get("SOVRN_API_KEY", "").strip()
SOVRN_REDIRECT_BASE = "https://redirect.viglink.com/"
SOVRN_CUID = os.environ.get("SOVRN_CUID", "loupeapp").strip()


def monetize(url):
    """Wrap a destination URL in a Sovrn affiliate redirect when a key is set."""
    if not SOVRN_API_KEY:
        return url
    params = {"key": SOVRN_API_KEY, "u": url}
    if SOVRN_CUID:
        params["cuid"] = SOVRN_CUID
    return SOVRN_REDIRECT_BASE + "?" + urllib.parse.urlencode(params)


def fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def infer_category(product_type, title):
    hay = f"{product_type} {title}".lower()
    for cat, kws in CATEGORY_RULES:
        if any(k in hay for k in kws):
            return cat
    return "tops"


def infer_colors(title, options, tags=None, product_type=""):
    """Infer up to 2 color tags. Color almost never lives in the title alone — it
    lives in the variant color option, the product tags, and sometimes the
    product_type. We read all of them so the catch-all 'neutral' fallback only
    fires when there's genuinely no color signal anywhere."""
    hay = (title or "").lower()
    if product_type:
        hay += " " + str(product_type).lower()
    # Shopify `tags` may be a comma string or a list — normalize either way.
    if tags:
        if isinstance(tags, str):
            hay += " " + tags.lower()
        else:
            hay += " " + " ".join(str(t).lower() for t in tags)
    # Pull values from EVERY option whose name looks like a color/colour option,
    # not just the first. Variant values are where the color usually is.
    for opt in options or []:
        name = (opt.get("name") or "").lower()
        if "color" in name or "colour" in name:
            hay += " " + " ".join(str(v).lower() for v in opt.get("values", []))
    found = []
    for tag, kws in COLOR_RULES:
        if any(k in hay for k in kws):
            found.append(tag)
    if any(h in hay for h in MULTICOLOR_HINTS):
        found.append("multicolor")
    # de-dup, keep order, cap at 2. VALID_COLORS guards against emitting any tag
    # the app can't filter (e.g. there is no 'gray' tag — grey maps to 'neutral').
    seen, out = set(), []
    for c in found:
        if c in VALID_COLORS and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:2] if out else ["neutral"]


def slugify(brand):
    return "".join(c if c.isalnum() else "-" for c in brand.lower()).strip("-")


# Color words used to fold "Loafer in Black" / "Loafer - Tan" / "Loafer (Cream)"
# down to a shared base name so we can cap near-identical color variants. We use
# every color keyword the inference engine knows about, plus a few common variant
# qualifiers, so the same loafer in 5 colors collapses to one base.
_BASE_COLOR_WORDS = set()
for _tag, _kws in COLOR_RULES:
    _BASE_COLOR_WORDS.update(_kws)
_BASE_COLOR_WORDS.update(MULTICOLOR_HINTS)
_BASE_COLOR_WORDS.update([
    "colour", "color", "shade",
])
# Per base-name cap: at most this many color variants of one product per brand,
# so a 5-colorway loafer doesn't flood the deck with near-identical cards.
MAX_VARIANTS_PER_BASE = 2


def base_name(title):
    """Normalize a product title to a color-agnostic base name. Strips trailing
    color words and common separators so 'Romy Loafer - Black' and 'Romy Loafer
    in Cream' share a base. Used only to cap near-identical color variants."""
    t = (title or "").lower()
    # Split off anything after a separator commonly used to denote colorway.
    for sep in (" - ", " – ", " — ", " in ", " / ", " | "):
        idx = t.find(sep)
        if idx != -1:
            tail = t[idx + len(sep):]
            # Only treat the tail as a colorway if it's short and color-flavored.
            tail_words = re.findall(r"[a-z]+", tail)
            if tail_words and all(
                w in _BASE_COLOR_WORDS or len(tail_words) <= 2 for w in tail_words
            ):
                t = t[:idx]
                break
    # Drop a parenthetical color, e.g. "Loafer (Tan)".
    t = re.sub(r"\(([^)]*)\)", lambda m: "" if all(
        w in _BASE_COLOR_WORDS for w in re.findall(r"[a-z]+", m.group(1))
    ) else m.group(0), t)
    # Strip any trailing color tokens left dangling.
    words = re.findall(r"[a-z0-9']+", t)
    while words and words[-1] in _BASE_COLOR_WORDS:
        words.pop()
    return " ".join(words).strip() or (title or "").strip().lower()


def first_image(product):
    imgs = product.get("images") or []
    for im in imgs:
        src = im.get("src")
        if src:
            return src
    return None


def gallery_images(product, n=5):
    """Up to `n` image src's for the product gallery (hero first, deduped)."""
    out = []
    seen = set()
    hero = first_image(product)
    if hero and hero not in seen:
        seen.add(hero)
        out.append(hero)
    for im in product.get("images") or []:
        if len(out) >= n:
            break
        src = im.get("src")
        if src and src not in seen:
            seen.add(src)
            out.append(src)
    return out[:n]


# Canonical letter-size ordering for sensible display.
_SIZE_ORDER = {
    "XXS": 0, "XS": 1, "S": 2, "M": 3, "L": 4,
    "XL": 5, "XXL": 6, "2XL": 6, "XXXL": 7, "3XL": 7,
}


def _norm_size(val):
    return str(val or "").strip().upper().replace(" ", "")


def available_sizes(product):
    """In-stock size values for a product, sensibly ordered, or [] if no size option."""
    options = product.get("options") or []
    size_opt = None
    for opt in options:
        name = (opt.get("name") or "").strip().lower()
        if name in ("size", "sizes"):
            size_opt = opt
            break
    if not size_opt:
        return []

    # position is 1-based → maps to option1/option2/option3 on each variant.
    pos = size_opt.get("position")
    try:
        pos = int(pos)
    except (TypeError, ValueError):
        pos = 1
    key = f"option{pos}"

    in_stock = []
    seen = set()
    for v in product.get("variants") or []:
        if not v.get("available"):
            continue
        raw = v.get(key)
        if raw is None:
            continue
        s = str(raw).strip()
        if s and s not in seen:
            seen.add(s)
            in_stock.append(s)

    if not in_stock:
        return []

    # If they look like standard letter sizes, sort by the canonical scale;
    # otherwise preserve the option's declared value order, filtered to in-stock.
    if all(_norm_size(s) in _SIZE_ORDER for s in in_stock):
        return sorted(in_stock, key=lambda s: _SIZE_ORDER[_norm_size(s)])

    order = [str(x).strip() for x in (size_opt.get("values") or [])]
    if order:
        in_stock_set = set(in_stock)
        ordered = [s for s in order if s in in_stock_set]
        # Append any in-stock values not present in the declared values list.
        for s in in_stock:
            if s not in order:
                ordered.append(s)
        if ordered:
            return ordered
    return in_stock


def first_price(product):
    for v in product.get("variants") or []:
        p = v.get("price")
        if p:
            try:
                return float(p)
            except (TypeError, ValueError):
                continue
    return None


def normalize(product, brand, domain, fx):
    title = (product.get("title") or "").strip()
    handle = product.get("handle")
    img = first_image(product)
    raw_price = first_price(product)
    if not title or not handle or not img or not raw_price:
        return None
    price = round(raw_price * fx)
    if price <= 0:
        return None
    # Drop gift cards, shipping/insurance/route add-ons, swatches, samples, etc.
    if is_junk(title, price):
        return None
    product_type = product.get("product_type", "")
    category = infer_category(product_type, title)
    colors = infer_colors(title, product.get("options"),
                          tags=product.get("tags"), product_type=product_type)
    return {
        "id": f"{slugify(brand)}-{handle}",
        "brand": brand,
        "name": title,
        "price": price,
        "category": category,
        "colorTags": colors,
        "imageUrl": img,
        "sizes": available_sizes(product),
        "images": gallery_images(product),
        "affiliateUrl": monetize(f"https://{domain}/products/{handle}"),
    }


def main():
    cfg = json.loads(BRANDS_FILE.read_text(encoding="utf-8"))
    fx_table = cfg["fx_to_usd"]
    per_brand = int(cfg.get("perBrand", 10))
    products, seen_ids = [], set()
    by_brand = {}
    summary = []

    # Load the previous good catalog UP FRONT. It does two jobs: (1) carries each
    # product's stable addedAt date (NEW-arrival flagging), and (2) lets us carry
    # FORWARD a brand's last-known products if its store fails to scrape THIS run —
    # so a transient outage or rate-limit can never silently drop a brand (and its
    # followers' feed) from the live catalog. A brand only truly leaves Loupe when
    # it's removed from brands.json.
    prev_ids = set()
    prev_added = {}
    prev_by_brand = {}
    if OUT_FILE.exists():
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            for p in prev.get("products", []):
                pid = p.get("id")
                if not pid:
                    continue
                prev_ids.add(pid)
                if p.get("addedAt"):
                    prev_added[pid] = p["addedAt"]
                prev_by_brand.setdefault(p.get("brand"), []).append(p)
        except (ValueError, OSError):
            pass

    def scrape_brand(domain):
        """Fetch a brand's products.json with a few retries — most scrape 'failures'
        are momentary timeouts / rate-limits, not a dead store."""
        last = None
        for attempt in range(3):
            try:
                return fetch_json(f"https://{domain}/products.json?limit={max(per_brand * 3, 30)}")
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise last

    for entry in cfg["brands"]:
        brand, domain = entry["brand"], entry["domain"]
        fx = fx_table.get(entry.get("currency", "USD"), 1.0)
        # Mainstream houses get a lower cap than indie brands (discovery-first).
        cap = effective_cap(brand, per_brand)
        got = 0
        bucket = []
        base_counts = {}  # base product name -> # color variants already kept
        try:
            # pull a generous page, then take the first `cap` valid items
            data = scrape_brand(domain)
            for product in data.get("products", []):
                if got >= cap:
                    break
                norm = normalize(product, brand, domain, fx)
                if not norm or norm["id"] in seen_ids:
                    continue
                # Cap near-identical color variants of the same base product so the
                # deck stays visually varied (a 5-colorway loafer -> ~2 cards).
                bkey = base_name(norm["name"])
                if base_counts.get(bkey, 0) >= MAX_VARIANTS_PER_BASE:
                    continue
                base_counts[bkey] = base_counts.get(bkey, 0) + 1
                seen_ids.add(norm["id"])
                bucket.append(norm)
                got += 1
            if bucket:
                by_brand[brand] = bucket
            summary.append(f"  {brand:<22} {got:>3} items")
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
            summary.append(f"  {brand:<22}  SKIP ({type(e).__name__})")
        time.sleep(0.5)  # be polite

    # Carry forward any brand that returned NOTHING this run but existed in the last
    # good catalog — reuse its previous products (already normalized + monetized) so
    # a momentary failure never drops the brand. seen_ids guards against duplicates.
    carried_total = 0
    for entry in cfg["brands"]:
        brand = entry["brand"]
        if brand in by_brand:
            continue
        carried = [p for p in prev_by_brand.get(brand, []) if p.get("id") and p["id"] not in seen_ids]
        if carried:
            for p in carried:
                seen_ids.add(p["id"])
            by_brand[brand] = carried
            carried_total += len(carried)
            summary.append(f"  {brand:<22} {len(carried):>3} items (carried)")
    if carried_total:
        summary.append(f"  -> carried forward {carried_total} items for brands that briefly failed")

    # Round-robin interleave across brands so the published feed is never grouped
    # brand-by-brand (the app shuffles too, but a mixed feed is the right default
    # for any consumer and for the very first cards a user sees).
    buckets = list(by_brand.values())
    random.shuffle(buckets)
    for b in buckets:
        random.shuffle(b)
    while any(buckets):
        random.shuffle(buckets)
        for b in buckets:
            if b:
                products.append(b.pop(0))

    # Merge in curated products — brands we can't auto-scrape (e.g. partners on
    # non-Shopify platforms, like Ganni on Salesforce Commerce Cloud). These are
    # hand-built to the exact catalog schema and appended; the app reshuffles the
    # feed so order here doesn't matter. They flow through the same addedAt
    # stamping and monetize() wrapping as scraped products.
    curated_file = HERE / "curated.json"
    if curated_file.exists():
        try:
            curated = json.loads(curated_file.read_text(encoding="utf-8"))
            added = 0
            for p in curated.get("products", []):
                pid = p.get("id")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                if p.get("affiliateUrl"):
                    p["affiliateUrl"] = monetize(p["affiliateUrl"])
                products.append(p)
                added += 1
            summary.append(f"  {'(curated)':<22} {added:>3} items")
        except (ValueError, OSError) as e:
            summary.append(f"  (curated)              SKIP ({type(e).__name__})")

    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Products that existed before we started stamping dates are backdated so the
    # FIRST run after this upgrade doesn't flag the entire catalog as "new".
    backdated_iso = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # (prev_ids / prev_added were loaded up front, together with the carry-forward
    # data, so a brand that briefly failed keeps its products AND their addedAt.)

    # Stamp each product's stable "first seen" date for NEW-arrival flagging:
    #   • seen before with a date  → carry it over
    #   • existed before this feature → backdate (not new)
    #   • genuinely new product     → now
    for product in products:
        pid = product["id"]
        if pid in prev_added:
            product["addedAt"] = prev_added[pid]
        elif pid in prev_ids:
            product["addedAt"] = backdated_iso
        else:
            product["addedAt"] = now_iso

    catalog = {
        "generatedAt": now_iso,
        "count": len(products),
        "products": products,
    }
    OUT_FILE.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Loupe catalog build")
    print("\n".join(summary))
    print(f"\nTotal: {len(products)} products -> {OUT_FILE.name}")

    # ── Content-quality stats ─────────────────────────────────────────────────
    # Surface the metrics the catalog audit cares about so each run is auditable:
    # how many products fell back to a bare 'neutral' color, and the category mix
    # (so swim/intimates/jumpsuit re-routing is visible).
    if products:
        neutral_only = sum(
            1 for p in products if p.get("colorTags") == ["neutral"]
        )
        cat_counts = {}
        for p in products:
            cat_counts[p["category"]] = cat_counts.get(p["category"], 0) + 1
        print("\nContent stats")
        print(f"  neutral-only color fallback: {neutral_only}/{len(products)} "
              f"({100 * neutral_only / len(products):.1f}%)")
        print("  category mix: " + ", ".join(
            f"{c}={n}" for c, n in sorted(cat_counts.items(), key=lambda kv: -kv[1])
        ))
    # Fail the CI run only if we got essentially nothing (keeps a bad scrape from
    # overwriting a good catalog with an empty one).
    if len(products) < 20:
        print("ERROR: too few products scraped \u2014 not enough to publish.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
