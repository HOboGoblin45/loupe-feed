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
from concurrent.futures import ThreadPoolExecutor
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
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?:s|es)?(?![a-z0-9])", hay) is not None


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
    # One-piece full-body garments map to 'dresses' (closest existing silhouette).
    ("dresses",     JUMPSUIT_KEYWORDS),
    ("dresses",     ["dress", "gown"]),
    ("bottoms",     ["skirt", "trouser", "pant", "short", "jean", "legging",
                     "culotte", "capri"]),
    ("outerwear",   ["coat", "jacket", "blazer", "cardigan", "trench", "parka",
                     "anorak", "overcoat", "puffer"]),
    ("shoes",       ["shoe", "boot", "sandal", "mule", "flat", "sneaker", "heel",
                     "loafer", "pump", "clog", "slipper"]),
    # Compound forms (handbag/hairband/headband/crossbody...) are listed explicitly
    # because word-boundary matching won't find 'bag'/'hair' inside them — and we
    # deliberately don't want the bare 'hair' substring (it would catch "mohair").
    ("accessories", ["bag", "handbag", "crossbody", "backpack", "tote", "clutch",
                      "pouch", "purse", "scarf", "necklace", "earring", "bracelet",
                      "ring", "pendant", "brooch", "anklet", "cufflink", "hat", "cap", "beret", "belt", "sunglass",
                      "jewel", "hair", "hairband", "headband", "hairclip", "barrette",
                      "scrunchie", "glove", "wallet"]),
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
# TODO: verify the exact redirect format against a Sovrn dashboard "Create Links"
# output before enabling the key (base host + param names). The base/params are
# isolated here so confirming it is a one-line change.
SOVRN_API_KEY = os.environ.get("SOVRN_API_KEY", "").strip()
SOVRN_REDIRECT_BASE = "https://redirect.viglink.com/"
SOVRN_CUID = os.environ.get("SOVRN_CUID", "loupeapp").strip()


def monetize(url):
    """Wrap a destination URL in a Sovrn affiliate redirect when a key is set.

    Idempotent: a URL that is ALREADY a Sovrn redirect is returned unchanged, so
    re-running the build (or re-wrapping curated links that were saved already
    wrapped) can never double-wrap into redirect.viglink.com/?...&u=redirect...
    """
    if not SOVRN_API_KEY:
        return url
    # Already wrapped (e.g. a curated link or a carried-forward product) → leave it.
    if isinstance(url, str) and url.startswith(SOVRN_REDIRECT_BASE):
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
    # Word-boundary match (reuses the junk filter's _word_in) so a keyword only
    # hits a whole word: 'ring' no longer matches inside "...ring"/"earring",
    # 'hair' no longer matches inside "mohair", 'top' not inside "stop". This,
    # plus the garment-before-accessories ordering above, is the P1-15 fix.
    # Examples now classified correctly:
    #   "Sleeper Linen Maxi Dress"  -> dresses   (was 'accessories' via 'ring')
    #   "Cashmere Blanket"          -> tops fallback, NOT 'tops' via a substring
    #   "Mohair Sweater"            -> tops       (was 'accessories' via 'hair')
    hay = f"{product_type} {title}".lower()
    for cat, kws in CATEGORY_RULES:
        if any(_word_in(k, hay) for k in kws):
            return cat
    return "tops"


def infer_colors(title, options, tags=None, product_type=""):
    """Infer up to 2 color tags for the colorway actually shown.

    Filter-accuracy fix: a Shopify product lists EVERY colorway it sells in its
    variant color option, but the catalog shows ONE image (one colorway). Reading
    all variant values tags a black-pictured top that also comes in pink as 'pink'
    so it wrongly surfaces under the Pink filter. So read the SHOWN colorway from
    the title/product_type FIRST, and only fall back to tags + variant color
    values when the title names no color at all."""
    def _from(hay):
        found = []
        for tag, kws in COLOR_RULES:
            if any(k in hay for k in kws):
                found.append(tag)
        if any(h in hay for h in MULTICOLOR_HINTS):
            found.append("multicolor")
        seen, out = set(), []
        for c in found:
            if c in VALID_COLORS and c not in seen:
                seen.add(c)
                out.append(c)
        return out[:2]
    # 1) The colorway the image shows is almost always named in the title.
    title_hay = (title or "").lower()
    if product_type:
        title_hay += " " + str(product_type).lower()
    shown = _from(title_hay)
    if shown:
        return shown
    # 2) Untitled colorway -> fall back to tags + the variant color values.
    fb = ""
    if tags:
        fb += " " + (tags.lower() if isinstance(tags, str)
                     else " ".join(str(t).lower() for t in tags))
    for opt in options or []:
        name = (opt.get("name") or "").lower()
        if "color" in name or "colour" in name:
            fb += " " + " ".join(str(v).lower() for v in opt.get("values", []))
    out = _from(fb)
    return out if out else ["neutral"]


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


# ── Image validation ─────────────────────────────────────────────────────────
# Some stores (especially multi-brand boutiques) carry products whose hero photo
# is a dead Shopify URL or is hotlinked from a designer CDN that blocks it. Such
# a product passes every text check but renders as a BLANK tile in the app. We
# verify each product's image actually loads (200 + image content-type) and keep
# only working images — repairing from the gallery when possible, dropping the
# product when nothing loads. A safety net (in main) ignores the whole pass if it
# would drop an implausible share of the catalog (i.e. a network problem, not
# genuinely dead images), so a transient blip can never gut the live feed.

def _image_ok(url, timeout=6):
    """True iff the URL returns 200 (or 206) with an image/* content-type."""
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return False
    # Try a cheap HEAD first; some CDNs reject HEAD, so fall back to a 1-byte GET.
    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        try:
            headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*"}
            headers.update(extra)
            req = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", resp.getcode())
                if status not in (200, 206):
                    return False
                ctype = (resp.headers.get("Content-Type") or "").lower()
                # P1-16: reject HEIC/HEIF — expo-image can't reliably decode them
                # on-device, so they render as blank tiles. Accept other image/*.
                if ctype.startswith("image/heic") or ctype.startswith("image/heif"):
                    return False
                return ctype.startswith("image/")
        except Exception:
            continue
    return False


def _repair_images(product):
    """Point the product at images that actually load (hero first), or return
    None when none of its images work so the caller can drop it."""
    candidates = []
    hero = product.get("imageUrl")
    if hero:
        candidates.append(hero)
    for u in product.get("images") or []:
        if u not in candidates:
            candidates.append(u)
    for u in candidates:
        if _image_ok(u):
            product["imageUrl"] = u
            gallery = [u] + [g for g in (product.get("images") or []) if g != u]
            product["images"] = gallery[:5]
            return product
    return None


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


def normalize(product, brand, domain, fx, multi_brand=False):
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
    # Multi-brand boutiques (e.g. Arete Studios) resell many designers under one
    # storefront. Label each item with its REAL vendor when present, falling back
    # to the store name — so the app shows the designer, not the shop, as the brand.
    display_brand = brand
    if multi_brand:
        vendor = (product.get("vendor") or "").strip()
        if vendor and vendor.lower() not in ("", "frontpage"):
            display_brand = vendor
    product_type = product.get("product_type", "")
    category = infer_category(product_type, title)
    colors = infer_colors(title, product.get("options"),
                          tags=product.get("tags"), product_type=product_type)
    return {
        "id": f"{slugify(display_brand)}-{handle}",
        "brand": display_brand,
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

    def scrape_page(domain, limit, since_id=None):
        """Fetch ONE products.json page with a few retries — most scrape
        'failures' are momentary timeouts / rate-limits, not a dead store.
        Shopify caps ?limit at 250; we never ask for more. `since_id` walks to
        the next page (Shopify returns products with id > since_id)."""
        # Shopify hard-caps page size at 250; asking for more is silently clamped.
        limit = min(max(int(limit), 1), 250)
        qs = f"limit={limit}"
        if since_id is not None:
            qs += f"&since_id={since_id}"
        last = None
        for attempt in range(3):
            try:
                return fetch_json(f"https://{domain}/products.json?{qs}")
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise last

    # Per-brand page size: enough headroom over `cap` to absorb junk/variant
    # filtering, but never above Shopify's 250 max.
    PAGE_LIMIT = min(max(per_brand * 3, 30), 250)
    # Safety bound so a huge store can't loop forever; cap*4 valid-item headroom
    # at PAGE_LIMIT per page is plenty to collect `cap` survivors.
    MAX_PAGES = 20

    def scrape_brand(domain, cap):
        """Yield successive products.json pages for a brand, walking `since_id`
        until a short/empty page (store exhausted) or MAX_PAGES. The caller stops
        early once it has `cap` post-filter items, so for most brands this fetches
        exactly one page."""
        since_id = None
        for _ in range(MAX_PAGES):
            data = scrape_page(domain, PAGE_LIMIT, since_id)
            page = (data or {}).get("products", []) or []
            if not page:
                return
            yield page
            # A page shorter than the requested limit means the store is exhausted.
            if len(page) < PAGE_LIMIT:
                return
            # Advance: next page is products with id greater than the last seen.
            last_id = None
            for p in page:
                pid = p.get("id")
                if isinstance(pid, int):
                    last_id = pid
            if last_id is None:
                return  # no numeric ids to paginate on — stop rather than loop
            since_id = last_id

    for entry in cfg["brands"]:
        brand, domain = entry["brand"], entry["domain"]
        fx = fx_table.get(entry.get("currency", "USD"), 1.0)
        multi_brand = bool(entry.get("multiBrand"))
        # Mainstream houses get a lower cap than indie brands (discovery-first).
        cap = effective_cap(brand, per_brand)
        got = 0
        bucket = []
        base_counts = {}  # base product name -> # color variants already kept
        pages_seen = 0
        try:
            # Walk products.json pages (since_id) until we have `cap` valid items
            # or the store is exhausted. Most brands satisfy `cap` on page 1.
            for page in scrape_brand(domain, cap):
                pages_seen += 1
                for product in page:
                    if got >= cap:
                        break
                    norm = normalize(product, brand, domain, fx, multi_brand=multi_brand)
                    if not norm or norm["id"] in seen_ids:
                        continue
                    # Cap near-identical color variants of the same base product so
                    # the deck stays visually varied (a 5-colorway loafer -> ~2 cards).
                    bkey = base_name(norm["name"])
                    if base_counts.get(bkey, 0) >= MAX_VARIANTS_PER_BASE:
                        continue
                    base_counts[bkey] = base_counts.get(bkey, 0) + 1
                    seen_ids.add(norm["id"])
                    bucket.append(norm)
                    got += 1
                if got >= cap:
                    break  # enough — don't fetch further pages
            if bucket:
                by_brand[brand] = bucket
            # Flag brands that exhausted their store without filling `cap` — usually
            # a small catalog, heavy junk/variant filtering, or a too-low page walk.
            short = " (under cap — store exhausted)" if got < cap else ""
            summary.append(f"  {brand:<22} {got:>3} items{short}")
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

    # ── Drop products whose image won't actually render (no blank tiles) ───────
    # Validate concurrently (I/O-bound). SAFETY is evaluated PER BRAND: if a
    # single brand loses most of its images — e.g. its CDN blocks the CI runner's
    # IP — we keep that brand's items UNVALIDATED (un-repaired, original images)
    # instead of letting them silently vanish, since "scrape succeeded" means
    # carry-forward won't rescue them. Other brands still get genuinely-dead
    # images repaired/dropped. A global guard remains as a secondary net for a
    # broad network blip that hits everything at once.
    PER_BRAND_KEEP_RATIO = 0.40   # a brand dropping >40% of its images is suspect
    GLOBAL_KEEP_RATIO = 0.40      # the original whole-catalog guard, kept as backup
    if products and os.environ.get("VALIDATE_IMAGES", "1") != "0":
        total_before = len(products)
        # Validate every product once (returns the repaired product or None).
        # Pair each ORIGINAL product with its result so we can choose, per brand,
        # whether to keep the validated survivors or fall back to the originals.
        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(_repair_images, list(products)))
        global_kept = sum(1 for r in results if r is not None)
        global_drop_ratio = (total_before - global_kept) / max(total_before, 1)

        if global_drop_ratio > GLOBAL_KEEP_RATIO:
            # Catalog-wide collapse → almost certainly a network problem, not dead
            # images. Keep EVERYTHING unvalidated rather than gut the live feed.
            summary.append(
                f"  -> image-check would drop {total_before - global_kept}/{total_before} "
                f"(>40% catalog-wide); assuming a network issue — keeping all, unvalidated"
            )
        else:
            # Decide per brand. Group (original, result) pairs by brand in order.
            per_brand_idx = {}
            for orig, res in zip(products, results):
                per_brand_idx.setdefault(orig.get("brand"), []).append((orig, res))

            kept = []
            total_dropped = 0
            for bname, pairs in per_brand_idx.items():
                b_total = len(pairs)
                b_dropped = sum(1 for _orig, res in pairs if res is None)
                b_ratio = b_dropped / max(b_total, 1)
                if b_dropped and b_ratio > PER_BRAND_KEEP_RATIO:
                    # This brand lost most of its images — treat as a CDN/IP block,
                    # NOT genuinely dead images. Keep all its items unvalidated
                    # (original images intact) so the brand never silently vanishes.
                    for orig, _res in pairs:
                        kept.append(orig)
                    summary.append(
                        f"  -> {bname}: image-check would drop {b_dropped}/{b_total} "
                        f"(>40%); keeping this brand unvalidated (likely CDN/IP block)"
                    )
                else:
                    # Normal case: keep validated/repaired survivors, drop the dead.
                    for _orig, res in pairs:
                        if res is not None:
                            kept.append(res)
                    total_dropped += b_dropped
            # Preserve the original feed order (per-brand grouping reshuffled it).
            kept_by_id = {p.get("id"): p for p in kept}
            products = [kept_by_id[p["id"]] for p in products if p.get("id") in kept_by_id]
            if total_dropped:
                summary.append(f"  -> dropped {total_dropped} items whose image did not load")

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
