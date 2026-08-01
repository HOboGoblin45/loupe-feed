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

# -- Junk / non-product filter -------------------------------------------------
# Real Shopify stores publish non-garment "products" via /products.json: gift
# cards, returns/refunds, shipping/insurance/route add-ons, deposits, fabric
# swatches, sample/sticker packs, gift wrap, warranties, and standalone
# SIZE-CHART / size-guide listings. They have images and prices, so they pass
# normalize()'s basic checks and surface as junk swipe cards. We reject a product
# when its TITLE or its PRODUCT_TYPE matches one of these phrases (word-aware).
# A softer rule drops anything cheap that ALSO reads like an add-on.
#
# We deliberately DO NOT look at Shopify tags: a live-feed audit found tags carry
# merch/policy words on ordinary garments (salereturnpolicy, womenssizechart,
# sample-sale, gift-under-100), so matching tags would drop hundreds of real
# items. Title + product_type only. (Images are never inspected here, so a brand
# that ships size-chart PHOTOS as product images is unaffected by this text filter.)
JUNK_TITLE_PHRASES = [
    # Gift cards / vouchers / store credit
    "gift card", "gift cards", "gift voucher", "gift certificate", "e-gift",
    "egift", "e gift card", "e-gift card", "digital gift", "store credit",
    # Returns / refunds / exchange-fee utility SKUs
    "return", "returns", "refund", "refunds", "restocking",
    # Shipping / insurance / protection add-ons (specific protection phrases only,
    # never bare "protection" -- that would nuke "UV/Sun Protection" swimwear)
    "shipping", "insurance", "package protection", "shipping protection",
    "order protection", "purchase protection", "route protection",
    # NOTE: bare "route" deliberately NOT here — it killed real titles like
    # "Route 66 Jacket". Cheap Route-insurance SKUs are still caught by the
    # price-gated JUNK_ADDON_WORDS list below (they are always a few dollars).
    "checkout+", "checkout plus",
    # Standalone size-chart / size-guide "products"
    "size chart", "size charts", "size guide", "sizing", "sizing guide",
    "fit guide", "measurement guide",
    # Swatches / fabric samples / stickers / deposits / donations / wrap / warranty
    # (never bare "sample" -- that would nuke "Sample Sale" garments)
    "sticker", "fabric sample", "sample pack", "free sample", "swatch",
    "deposit", "pre-order deposit", "donation", "gift wrap", "gift-wrap",
    "warranty", "add-on", "add on", "addon",
]
# Product_type values that are unambiguous utility SKUs (Shopify sets these
# literally: "Gift Cards", "return", "return,package_protection", "Shipping").
JUNK_PRODUCT_TYPE_PHRASES = [
    "gift card", "gift cards", "gift voucher", "voucher", "return", "returns",
    "refund", "package protection", "shipping", "insurance", "store credit",
    "donation", "warranty", "e-gift", "egift",
]
# Cheaper than this AND matching an add-on word -> almost certainly not a garment.
JUNK_PRICE_FLOOR = 15
# Below this (USD), a listing is a data artifact regardless of title — a placeholder,
# deposit, mispriced sample, or lone accessory-tag row. Curated fashion doesn't retail
# under $5, and a "$1" price on the brand directory reads as broken data to shoppers
# and investors. Dropped unconditionally. (Real cheap items observed bottom out ~$6.)
HARD_PRICE_FLOOR = 5
JUNK_ADDON_WORDS = [
    "shipping", "insurance", "route", "swatch", "sticker", "deposit",
    "donation", "gift", "warranty", "add-on", "add on", "addon", "wrap", "credit",
]

# ── Non-apparel GOODS a fashion label also happens to sell (2026-07-29) ───────
# Not utility SKUs — real, purchasable homeware/beauty/stationery. Loupe is a
# clothing discovery app, so a scented candle or a branded mug in the swipe deck
# reads as broken curation to a user and embarrassing to a demo. All of these
# were live and filed as `tops`:
#   "Pistachio Perfume" $225 · "Scout Candle" $68 · "Archipelago Scented Candle"
#   $60 · "Hand Over x Scalpers Mug" · "Terry Hand Towel" $34 · "Oddli Notebook"
#   $30 · "Oddli Keychain" $20 · "BRIDAL NOTECARD + ENVELOPE & SEAL" $6
#   · 4 Alighieri candlesticks ($306–$829)
#
# ⚠ GUARDED, not absolute: these words are also used as *names* for real clothes.
# Ghostboy sells an "INCENSE SKORT" and an "INCENSE TOP"; an absolute "incense"
# rule deletes both. So a phrase here only condemns a listing when the title
# carries NO garment noun at all (see is_junk). "Hand towel" — not bare "towel" —
# because beach/kitchen/terry towels and towelling shorts are real product here.
JUNK_NON_APPAREL_PHRASES = [
    "candle", "candlestick", "candle holder", "perfume", "fragrance", "incense",
    "notecard", "note card", "mug", "notebook", "keychain", "key chain",
    "hand towel",
]
# Promotional placeholder SKUs — a gift-with-purchase row, not a product you can
# buy ("Free Scrunchie With Every Swim Item Purchased", $8, Los Angeles Apparel).
# Unconditional: no real garment is titled as an offer.
_FREE_WITH_PURCHASE_RE = re.compile(
    r"(?<![a-z0-9])free\b.{0,40}\bwith\b.{0,40}\bpurchase[sd]?(?![a-z0-9])", re.I
)


def _word_in(needle, hay):
    """True if `needle` appears in `hay` on word boundaries (handles multi-word
    phrases). Avoids matching e.g. 'ship' inside 'relationship'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?:s|es)?(?![a-z0-9])", hay) is not None


def _normalize_type(product_type):
    """Lowercase a product_type and fold separators (comma, underscore, slash,
    hyphen) to spaces so word matching reads 'return,package_protection' as the
    words 'return' and 'package protection'."""
    return re.sub(r"[^a-z0-9]+", " ", str(product_type or "").lower()).strip()


def is_junk(title, price, product_type=""):
    """True if a product looks like a non-garment add-on / utility SKU.

    Checks the TITLE and the PRODUCT_TYPE only -- never Shopify tags (tags carry
    policy/merch words that live on real garments).
    """
    hay = (title or "").lower()
    if not hay:
        return True
    # Absolute price floor: a sub-$5 listing is junk on its own (placeholder/deposit/
    # mispriced sample) — no add-on word required. Kills "$1" artifacts that otherwise
    # surface as "from $1" on the brand directory.
    if price is not None and price < HARD_PRICE_FLOOR:
        return True
    for phrase in JUNK_TITLE_PHRASES:
        if _word_in(phrase, hay):
            return True
    ptype = _normalize_type(product_type)
    if ptype:
        for phrase in JUNK_PRODUCT_TYPE_PHRASES:
            if _word_in(phrase, ptype):
                return True
    if price is not None and price < JUNK_PRICE_FLOOR:
        if any(_word_in(w, hay) for w in JUNK_ADDON_WORDS):
            return True
    # Promo placeholder ("Free X With Every Y Purchased") — never a real product.
    if _FREE_WITH_PURCHASE_RE.search(hay):
        return True
    # Non-apparel goods (candles, perfume, mugs, notebooks, keychains, hand
    # towels), but ONLY when the title names no garment. That guard is what keeps
    # Ghostboy's "INCENSE SKORT" / "INCENSE TOP" and any future "Candle Light Slip
    # Dress" alive while still dropping "Scout Candle" and "Oddli Notebook".
    if any(_word_in(p, hay) for p in JUNK_NON_APPAREL_PHRASES):
        if _category_from_keywords(hay) is None:
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
    # 'skort' is listed explicitly: it is a bottoms garment but shares no whole
    # word with 'skirt' or 'short', so word-boundary matching missed it and every
    # skort fell through to the `tops` fallback (3 live, Ghostboy).
    ("bottoms",     ["skirt", "skort", "trouser", "pant", "short", "jean", "legging",
                     "culotte", "capri"]),
    ("outerwear",   ["coat", "jacket", "blazer", "cardigan", "trench", "parka",
                     "anorak", "overcoat", "puffer"]),
    ("shoes",       ["shoe", "boot", "sandal", "mule", "flat", "sneaker", "heel",
                     "loafer", "pump", "clog", "slipper"]),
    # Compound forms (handbag/hairband/headband/crossbody...) are listed explicitly
    # because word-boundary matching won't find 'bag'/'hair' inside them — and we
    # deliberately don't want the bare 'hair' substring (it would catch "mohair").
    # 'scarves' is listed explicitly: _word_in's plural tolerance is +s/+es, so
    # the irregular f→ves plural never matches the 'scarf' keyword on its own.
    ("accessories", ["bag", "handbag", "crossbody", "backpack", "tote", "clutch",
                      "pouch", "purse", "scarf", "scarves", "necklace", "earring", "bracelet",
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
# Word-boundary matched (see infer_colors), with only +s/+es plural tolerance —
# so the -ed/-y INFLECTIONS have to be listed explicitly or real signal is lost
# ("Long printed skirt" and "Striped Tee" would stop reading as multicolour).
MULTICOLOR_HINTS = ["print", "printed", "floral", "stripe", "striped", "check", "checked",
                    "gingham", "multi", "multicolor", "multicolour", "foulard",
                    "patchwork", "rainbow", "tie-dye", "tie dye", "leopard", "animal",
                    "paisley", "ditsy", "polka dot", "polka", "plaid", "tartan"]

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


# Brands removed from the feed entirely by founder decision. Normalized keys, so
# accent/spacing variants match. A removed brand is dropped no matter how it
# enters — a direct storefront OR a multi-brand reseller's vendor field — and is
# also stripped from curated merges and grace-window carry-forward before write.
EXCLUDE_BRANDS = {
    _norm_brand(b) for b in [
        "Taottao",
        # Removed 2026-07-15 (founder decision). Deleting a brand from brands.json
        # alone lets its items ride the grace-window carry-forward for days — the
        # exclusion list is what makes removal IMMEDIATE and source-agnostic.
        "Rota",
        # Removed 2026-07-29 (founder decision).
        "Mackage",
        "Marfa Stance",
    ]
}


def effective_cap(brand, per_brand):
    """Per-brand product cap: mainstream houses are capped at MAINSTREAM_CAP so
    the feed stays indie-forward; everyone else keeps the full perBrand budget."""
    return MAINSTREAM_CAP if _norm_brand(brand) in MAINSTREAM_BRANDS else per_brand

# ── Affiliate wrapping: per-brand programs + Sovrn catch-all ──────────────────
# Two layers, both server-side switches (env vars via GitHub Actions secrets),
# so monetization changes never require an app update:
#
#   1. BRAND_AFFILIATE_TEMPLATES — JSON object mapping brand name → that brand's
#      own affiliate deep-link template (Rakuten / Awin / Impact / FlexOffers /
#      ShopMy…). The literal token {url} is replaced with the percent-encoded
#      destination. Brand keys match case/spacing-insensitively (_norm_brand).
#      Example value:
#        {"Damson Madder": "https://www.awin1.com/cread.php?awinmid=114966&awinaffid=YOURID&ued={url}",
#         "Ganni": "https://click.linksynergy.com/deeplink?id=YOURID&mid=XXXX&murl={url}"}
#      Direct programs pay 3–10x Sovrn's effective rate, so they take precedence.
#
#   2. SOVRN_API_KEY — the catch-all: any brand WITHOUT a template is wrapped in
#      a Sovrn Redirect API link (https://redirect.viglink.com/?key=<KEY>&u=<dest>
#      &cuid=<id>). When neither env var is set (local runs, pre-approval), links
#      pass through unchanged and the app still opens the brand's product page.
#
# Precedence: brand template > Sovrn > raw. Idempotent for both layers. A
# carried-forward or curated product that is still Sovrn-wrapped is UNWRAPPED
# and re-wrapped the first build after its brand's template lands.
SOVRN_API_KEY = os.environ.get("SOVRN_API_KEY", "").strip()
SOVRN_REDIRECT_BASE = "https://redirect.viglink.com/"
SOVRN_CUID = os.environ.get("SOVRN_CUID", "loupeapp").strip()


AFFILIATE_TEMPLATES_FILE = HERE / "affiliate_templates.json"


def _coerce_templates(data, source):
    """{brand: template} → {_norm_brand: template}, skipping anything unusable."""
    if not isinstance(data, dict):
        print(f"WARNING: affiliate templates from {source} must be a JSON object - ignoring.")
        return {}
    out = {}
    for brand_name, tpl in data.items():
        if brand_name.startswith("_"):
            continue  # "_comment" / "_howto" documentation keys
        if isinstance(tpl, str) and "{url}" in tpl:
            out[_norm_brand(brand_name)] = tpl
        elif isinstance(tpl, str) and tpl.strip():
            print(f"WARNING: affiliate template for {brand_name!r} ({source}) lacks a "
                  f"{{url}} token - skipped.")
    return out


def _load_brand_templates():
    """Per-brand affiliate deep-link templates → {_norm_brand: template}.

    TWO SOURCES, merged, env var wins:

      1. affiliate_templates.json (committed, in this repo)  ← the normal path
      2. BRAND_AFFILIATE_TEMPLATES (JSON env var / Actions secret) ← override

    WHY THE FILE EXISTS. Outreach to ~180 brands means links arrive one reply at
    a time, for months. When the ONLY way to add one was a GitHub Actions secret,
    every single "yes" was blocked on the one person who can edit repo secrets —
    so a brand could say yes on Monday and still not be earning by Friday.
    A committed file makes it a normal pull request.

    Is that safe in a public repo? Yes, and deliberately: an affiliate deep-link
    template is public BY CONSTRUCTION — it is embedded in every outbound URL the
    app produces, so anyone can read it off a single product tap. It is an
    identifier, not a credential. Nothing here can spend money or read an account.
    The env var stays supported and takes precedence, so if any future network
    ever issues a template containing something genuinely secret, put THAT one in
    the secret and leave the rest in the file.

    Fail-soft everywhere: a missing file, malformed JSON, or an entry without a
    {url} token is skipped with a warning. A bad template must never take the
    whole catalog build down.
    """
    templates = {}
    try:
        if AFFILIATE_TEMPLATES_FILE.exists():
            templates.update(_coerce_templates(
                json.loads(AFFILIATE_TEMPLATES_FILE.read_text(encoding="utf-8")),
                "affiliate_templates.json"))
    except ValueError:
        print("WARNING: affiliate_templates.json is not valid JSON - ignoring it.")
    except Exception as exc:  # unreadable file must not break the build
        print(f"WARNING: could not read affiliate_templates.json ({exc}) - ignoring it.")

    raw = os.environ.get("BRAND_AFFILIATE_TEMPLATES", "").strip()
    if raw:
        try:
            templates.update(_coerce_templates(json.loads(raw), "BRAND_AFFILIATE_TEMPLATES"))
        except ValueError:
            print("WARNING: BRAND_AFFILIATE_TEMPLATES is not valid JSON - ignoring it.")
    if templates:
        print(f"affiliate: {len(templates)} brand template(s) active")
    return templates


BRAND_AFFILIATE_TEMPLATES = _load_brand_templates()


def _sovrn_unwrap(url):
    """Return the original destination if url is a Sovrn redirect, else url unchanged."""
    if isinstance(url, str) and url.startswith(SOVRN_REDIRECT_BASE):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        dest = query.get("u", [""])[0]
        if dest:
            return dest
    return url


def monetize(url, brand=None):
    """Wrap a destination URL for affiliate attribution.

    Precedence: the brand's own program template (BRAND_AFFILIATE_TEMPLATES) →
    Sovrn catch-all (SOVRN_API_KEY) → unchanged. Idempotent: URLs already
    wrapped by the applicable layer are returned as-is, so re-running the build
    (or re-wrapping curated links saved wrapped) can never double-wrap.
    """
    if not isinstance(url, str) or not url:
        return url

    tpl = BRAND_AFFILIATE_TEMPLATES.get(_norm_brand(brand)) if brand else None
    if tpl:
        prefix = tpl.split("{url}", 1)[0]
        if prefix and url.startswith(prefix):
            return url  # already wrapped with this brand's own template
        dest = _sovrn_unwrap(url)  # direct program beats the Sovrn catch-all
        return tpl.replace("{url}", urllib.parse.quote(dest, safe=""))

    if not SOVRN_API_KEY:
        # No catch-all key = affiliate wrapping is OFF (Sovrn account denied,
        # 2026-07-23). UNWRAP any legacy Sovrn redirect instead of passing it
        # through: carried-forward (grace-window) and curated products keep the
        # affiliateUrl they were stored with, so without this the dead viglink
        # wrappers would linger in the catalog for weeks after the key removal.
        return _sovrn_unwrap(url)
    # Already wrapped (e.g. a curated link or a carried-forward product) → leave it.
    if url.startswith(SOVRN_REDIRECT_BASE):
        return url
    params = {"key": SOVRN_API_KEY, "u": url}
    if SOVRN_CUID:
        params["cuid"] = SOVRN_CUID
    return SOVRN_REDIRECT_BASE + "?" + urllib.parse.urlencode(params)


# ── Outbound attribution (UTM) ────────────────────────────────────────────────
# EVERY outbound product link carries a UTM. Before 2026-07-29 only the 400 Gemini
# partner links did, so 7,914 of 8,314 clicks arrived at a brand's store as plain
# "Direct" traffic: the brand could not see Loupe in their own analytics, which is
# the ENTIRE pitch we make to a brand ("we send you buyers, here's the proof").
# The tag is deliberately plain and honest — a referral tag, not a monetization
# wrapper — and it composes with the affiliate layer because it is applied to the
# DESTINATION url BEFORE monetize() wraps/encodes it.
LOUPE_UTM = "utm_source=loupe&utm_medium=referral&utm_campaign=app"


def add_utm(url, utm=LOUPE_UTM):
    """Append UTM params to a destination URL.

    • Uses '?' on a bare URL and '&' when a query string already exists.
    • IDEMPOTENT: a param already present (any value) is never appended again, so
      re-running the build, or re-processing a curated/carried-forward product
      that was stored already-tagged, can't produce ...&utm_source=loupe twice.
    • A more specific existing tag WINS — e.g. the Gemini partner links keep
      utm_campaign=gemini rather than being overwritten with the generic one.
    • Fails soft on non-string / empty input (curated rows can carry anything).
    """
    if not isinstance(url, str) or not url:
        return url
    utm = str(utm or "").lstrip("?&")
    if not utm:
        return url
    parts = urllib.parse.urlsplit(url)
    have = {k for k, _v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)}
    add = [(k, v) for k, v in urllib.parse.parse_qsl(utm, keep_blank_values=True)
           if k not in have]
    if not add:
        return url
    query = urllib.parse.urlencode(add)
    if parts.query:
        query = parts.query + "&" + query
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
    )


def fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def live_currency(domain, timeout=8):
    """The currency a store ACTUALLY publishes to a US shopper, or None.

    Every Shopify storefront exposes /cart.js, whose `currency` field is the
    presentment currency of the current session — the same presentment
    /products.json?country=US prices are quoted in. Comparing it against the
    per-brand `currency` in brands.json catches the one config mistake that
    mis-prices a brand's WHOLE catalog by the FX factor:

      • a store tagged EUR that really publishes DKK  (Stine Goya: 7.4x HIGH —
        a 499 DKK beanie shipped as $539 instead of $72)
      • a Shopify Markets store tagged GBP/EUR that serves USD under country=US,
        so the FX multiply DOUBLE-CONVERTS  (Martine Rose +27%, PDPAOLA +8%)
      • an UNTAGGED store (defaults USD) that really publishes EUR  (SIEDRÉS, 8% low)

    All four were live on 2026-07-29 and this one probe would have caught every
    one of them. Deliberately best-effort: short timeout, broad except, returns
    None on any failure — a currency probe must never cost us a brand's items.
    """
    try:
        data = fetch_json(f"https://{domain}/cart.js?country=US", timeout=timeout)
        cur = (data or {}).get("currency")
        if isinstance(cur, str) and re.fullmatch(r"[A-Z]{3}", cur.strip().upper()):
            return cur.strip().upper()
    except Exception:  # noqa: BLE001 — never fatal, see docstring
        pass
    return None


# "dress" used as an ADJECTIVE names a different garment — route it there BEFORE
# the generic dresses rule ("Dress Pants" are bottoms, not a dress). _word_in's
# plural tolerance covers "dress pants" / "dress shirts" / "dress shoes".
_DRESS_ADJECTIVE = [
    ("bottoms", ["dress pant", "dress trouser", "dress short"]),
    ("tops",    ["dress shirt", "dress blouse"]),
    ("shoes",   ["dress shoe", "dress boot", "dress sandal", "dress heel"]),
]

# SLEEVE LENGTH IS NOT A GARMENT TYPE (added 2026-07-29, reworked same day).
# "short" is a legitimate bottoms keyword but is ALSO a sleeve length, and bottoms
# is evaluated before tops/outerwear — so "Short Sleeve Top", "Louis Polo Short
# Sleeve" and "Short Sleeve Hoodie" were all filed as BOTTOMS (11 of 12 such items
# in the live catalog). Same class as "flat" in "Flat Knit Sweater" vs shoes.
#
# ⚠️ Implemented as a REDACTION, not as a rule that returns a category. The first
# attempt was a rule ("...sleeve" -> tops) and it hijacked every title containing a
# sleeve phrase: "Lucia Keyhole Long Sleeve Mini Dress" and "Octavia Long Sleeve
# Gown" would have flipped to `tops` on the next rebuild — the exact bug it was
# written to fix, inverted. Any short-circuit here has that failure mode, because
# the real garment noun can appear anywhere in the title. Deleting the phrase and
# letting the normal rules run cannot: what's left is the garment.
#   "Long Sleeve Mini Dress" -> "Mini Dress"  -> dresses
#   "Octavia Long Sleeve Gown" -> "Octavia Gown" -> dresses
#   "Short Sleeve Hoodie"    -> "Hoodie"      -> tops
#   "Belted Short Trench Coat" -> "Belted Trench Coat" -> outerwear
#   "Denim Shorts"           -> untouched     -> bottoms
_SLEEVE_NOISE_RE = re.compile(
    r"\b(?:short|long|cap|elbow|puff(?:ed)?|bell|balloon|bishop|dolman|raglan|"
    r"flutter|three[- ]quarter|3/4)[- ]sleeve[sd]?\b"
    r"|\bsleeveless\b"
    r"|\bflat[- ]knit\b"
    # "short" as a length modifier on an outer layer, not a pair of shorts.
    r"|\bshort(?=\s+(?:trench|coat|jacket|blazer|cardigan|parka)\b)",
    re.I,
)

# "…hair" as a MATERIAL, not a hair accessory. 'hair' has to stay a strong
# accessories keyword — Flattered files its hair clips under product_type "Shoes"
# and their titles say nothing but "Noomi Leather Hair" — but calf/pony/curly hair
# is shearling-type leather, and without this a "Curly Hair Zip Up River Vest"
# (VESTIGE, product_type "Jacket") lands in accessories. Redacted like sleeve
# length: delete the phrase and let the real garment noun decide.
_MATERIAL_NOISE_RE = re.compile(
    r"\b(?:calf|pony|curly|goat|horse|camel|shearling)[- ]hair\b|\bhair[- ]calf\b"
    # "<noun> detail(ed)" is TRIM, never the garment: "Belt detailed mini knit
    # shorts" is shorts, "bodysuit with belt detail" is a bodysuit. Without this,
    # 'belt' (accessories) outranks the garment noun and both land in accessories.
    r"|\b[a-z]+[- ](?:detail(?:ed|ing|s)?|trim(?:med|s)?|accent(?:ed|s)?)\b",
    re.I,
)

# Compound one-word dresses ("Sundress", "Maxidress", "Slipdress", "Shirtdress")
# end in -dress but never contain "dress" as a standalone word, so the keyword
# rules miss them (they fell to the tops fallback — Aniqa's wrong-filter report).
# Match any word ENDING in dress/dresses while excluding the English words that
# merely contain it (address / redress / headdress).
_DRESS_SUFFIX_RE = re.compile(
    r"(?<![a-z0-9])(?!address|redress|headdress)[a-z]*dress(?:es)?(?![a-z0-9])", re.I
)


# ── Which title words are strong enough to OUTRANK a store's product_type ─────
# (2026-07-29) A store's product_type is a merchandising bucket, not a garment
# name, and letting it win mis-files real product: "Runway • Trinity Skirt",
# "DUET SKIRT" and "NESTING SKIRT" all shipped as `dresses` because their stores
# file them under a Dresses/"Skirts & Dresses" product_type, and four Flattered
# hair clips shipped as `shoes` (product_type "Shoes").
#
# But "the title always wins" is WORSE, not better. Measured over 21,803 live
# products it flipped ~60 items correctly and ~60 items WRONGLY, because many
# category keywords name a DETAIL or a silhouette rather than the garment:
#     "Cami | Tort & Burnt Honey"        sunglasses -> tops   (via 'cami')
#     "Small + Mini Dahlia Hoop Set"     earrings   -> tops   (via 'set')
#     "T-lock leather top handle"        bag        -> tops   (via 'top')
#     "KNIT BUCKET"                      bag        -> tops   (via 'knit')
#     "The Cami Slip: White Pointelle"   dress      -> tops   (via 'cami')
#     "Scoop Tank Mini" / "Fine Halter Maxi"  dress -> tops   (tank / halter)
#     "Arcadia Zip High Top" / "Thong Wedge" shoe  -> tops   (top / thong)
#     "Cap Bloomers"                     shorts -> accessories (via 'cap')
# So only an UNAMBIGUOUS garment noun overrides the product_type. Everything in
# _WEAK_TITLE_KEYWORDS is still matched normally on the joined title+type string
# (nothing is lost); it just can't overrule the shop's own filing.
_WEAK_TITLE_KEYWORDS = set(SWIM_INTIMATES_KEYWORDS) | {
    # silhouette / cut / construction words, not garment names
    "top", "tube", "cami", "set", "knit", "vest", "tank", "halter",
    # length or fit modifiers ("Short Boot", "Flat Knit", "Cap Sleeve")
    "short", "flat", "heel", "cap", "overall",
    # material or trim details that ride on other garments ("Scarf slingbacks",
    # "Glove-Fit Flats", "leather bag charm"). Their compound forms — handbag,
    # crossbody, tote, clutch, hairclip, headband, scrunchie — stay STRONG.
    "scarf", "scarves", "bag", "glove",
}
# Keywords that exist ONLY in the strong set. "shorts" (plural) really is a pair
# of shorts — "Denim Shorts", "Bermuda Shorts", "Belt detailed mini knit shorts" —
# whereas the singular "Short" is nearly always a length modifier ("SHORT UZBEK
# CARDIGAN", "Short Hilda Earrings"). _word_in only tolerates a TRAILING +s/+es,
# so "shorts" cannot match a singular "short": the split is exact.
_STRONG_EXTRA_KEYWORDS = {"bottoms": ["shorts"]}
_STRONG_CATEGORY_RULES = []
for _cat, _kws in CATEGORY_RULES:
    _strong = [k for k in _kws if k not in _WEAK_TITLE_KEYWORDS]
    _strong += [k for k in _STRONG_EXTRA_KEYWORDS.get(_cat, []) if k not in _strong]
    if _strong:
        _STRONG_CATEGORY_RULES.append((_cat, _strong))


def _category_from_keywords(hay, strong_only=False):
    """Category from title/type keywords, or None — the CALLER owns the fallback.

    strong_only=True restricts matching to the unambiguous garment nouns in
    _STRONG_CATEGORY_RULES; used by infer_category to decide whether the TITLE is
    definite enough to overrule the store's product_type.
    Word-boundary match (the junk filter's _word_in) so a keyword only hits a
    whole word ('ring' never matches inside 'earring', 'hair' not in 'mohair'),
    with +s/+es plural tolerance; garment rules run before accessories (P1-15).
    Examples:
      "Sleeper Linen Maxi Dress"  -> dresses   (not 'accessories' via 'ring')
      "Mohair Sweater"            -> tops      (not 'accessories' via 'hair')
      "Sundress" / "Maxidress"    -> dresses   (compound suffix)
      "Dress Pants" / "Dress Shirt" -> bottoms / tops (adjective override)
      "Cashmere Blanket"          -> None      (caller decides the fallback)
    """
    # Delete sleeve length / knit construction FIRST so it can neither claim the
    # item ("Long Sleeve Gown" is a gown) nor be misread as another garment
    # ("Short Sleeve Top" is not a pair of shorts). See _SLEEVE_NOISE_RE.
    stripped = _MATERIAL_NOISE_RE.sub(" ", _SLEEVE_NOISE_RE.sub(" ", hay))
    for cat, phrases in _DRESS_ADJECTIVE:
        if any(_word_in(p, stripped) for p in phrases):
            return cat
    if _DRESS_SUFFIX_RE.search(stripped):
        return "dresses"
    for cat, kws in (_STRONG_CATEGORY_RULES if strong_only else CATEGORY_RULES):
        if any(_word_in(k, stripped) for k in kws):
            return cat
    if strong_only:
        # Deliberately NO sleeve fallback here: "Leda Long Sleeve" with a
        # product_type of "Dresses" is a dress, and letting sleeves-only override
        # the store would flip 10+ live dresses to `tops`.
        return None
    # Nothing but the sleeve phrase to go on ("KEY SHORT SLEEVE IN BLACK").
    # A garment described solely by its sleeves is a top.
    if stripped != hay and _SLEEVE_NOISE_RE.search(hay):
        return "tops"
    return None


# Shopify TAGS a store sets that map cleanly onto the app's 6 categories.
# Consulted ONLY when the title + product_type carry no category word (see
# infer_category), so a stray collection tag can never override a real garment
# title. This rescues stylistically-named accessories: Marge Sherwood's bags
# ("BRICK soup", "HEART CHARM", product_type "" or "ACC") are tagged BAG /
# SHOULDER / ACC — they belong in accessories, not the tops fallback. Junk /
# policy tags never match a category keyword, so they can't mis-classify.
_TAG_CATEGORY = {
    "accessory": "accessories", "accessories": "accessories", "acc": "accessories",
    "jewelry": "accessories", "jewellery": "accessories", "fine jewelry": "accessories",
    "bag": "accessories", "bags": "accessories", "handbag": "accessories",
    "handbags": "accessories", "bag acc": "accessories", "bag charm": "accessories",
    "belts": "accessories", "hats": "accessories", "scarves": "accessories",
    "sunglasses": "accessories",
    "dress": "dresses", "dresses": "dresses", "gown": "dresses", "gowns": "dresses",
    "bottom": "bottoms", "bottoms": "bottoms", "pants": "bottoms", "trousers": "bottoms",
    "skirts": "bottoms", "denim": "bottoms", "jeans": "bottoms", "shorts": "bottoms",
    "outerwear": "outerwear", "coats": "outerwear", "jackets": "outerwear",
    "outer": "outerwear", "coats & jackets": "outerwear", "coats and jackets": "outerwear",
    "shoes": "shoes", "footwear": "shoes", "boots": "shoes", "sandals": "shoes",
    "heels": "shoes", "sneakers": "shoes",
    "top": "tops", "tops": "tops", "knitwear": "tops", "knits": "tops",
    "sweaters": "tops", "sweaters and knitwear": "tops", "shirts": "tops",
    "tees": "tops", "t-shirts": "tops", "blouses": "tops", "swim": "tops",
    "swimwear": "tops", "intimates": "tops", "lingerie": "tops", "loungewear": "tops",
}


def category_from_tags(tags):
    """Infer a category from Shopify product TAGS — a signal stores DO set even
    when the title/type say nothing. Exact whole-tag hit first, then keyword
    matching on the joined tag text ('shoulder bags', 'bag charm'). Returns None
    when the tags say nothing about category."""
    if not tags:
        return None
    tag_list = tags if isinstance(tags, list) else str(tags).split(",")
    norm = [str(t).strip().lower() for t in tag_list if str(t).strip()]
    for t in norm:
        if t in _TAG_CATEGORY:
            return _TAG_CATEGORY[t]
    return _category_from_keywords(" ".join(norm))


def infer_category(product_type, title, tags=None):
    # 1. The TITLE alone — the single most trustworthy signal, and it OUTRANKS the
    #    store's product_type (fixed 2026-07-29).
    #
    #    The rules used to run on the joined f"{product_type} {title}", which let a
    #    store's collection label beat the actual garment noun: whichever category
    #    rule came first in CATEGORY_RULES won, and product_type is at the front of
    #    the string. Live examples, all shipping as `dresses` because the store
    #    files them under a "Dresses"/"Runway" product_type:
    #        "Runway • Trinity Skirt"  ·  "DUET SKIRT"  ·  "NESTING SKIRT"
    #    and three Flattered hair clips shipping as `shoes` (product_type "Shoes").
    #    A product_type is the shop's merchandising bucket; the title names the
    #    thing. When the title names a garment, believe the title.
    cat = _category_from_keywords((title or "").lower(), strong_only=True)
    if cat:
        return cat
    # 2. The title carries no definite garment noun ("Amelia", "Fine Halter Maxi",
    #    "BRICK soup") — NOW let product_type speak, on the joined string exactly
    #    as before, with the full keyword set.
    cat = _category_from_keywords(f"{product_type} {title}".lower())
    if cat:
        return cat
    # 3. No garment word anywhere in the title/type — lean on the store's OWN
    #    tags before falling back (fixes tag-labeled bags/jewelry landing in tops).
    cat = category_from_tags(tags)
    if cat:
        return cat
    # 4. Nothing anywhere → tops (the app's safest 'cover' bucket).
    return "tops"


# ── Accessory SUBTYPE ─────────────────────────────────────────────────────────
# `accessories` is one bucket holding two very different things: pieces someone
# opens Loupe to discover (a bag, a hat, sunglasses) and small trinkets that fill
# a feed without being what anyone came for (stud earrings, hair claws, tights).
# Measured on the live catalog, the split matters: accessory-only brands approve
# at 18.2% against 21.9% for everything else, and inside Gemini's own shelf bags
# and hats earn ~9.5 saves per 100 pieces against 8.6 for jewellery.
#
# So the subtype is stamped on every accessory and shoe, and the app decides what
# to do with it (currently: demote trinkets in Discover, never remove them). It's
# computed once here rather than by regex on-device, and the app degrades to
# "no opinion" on any product without it, so an old catalog stays safe.
#
# Ordered most-specific first — the first hit wins. `_word_in` gives word
# boundaries and tolerates a trailing s/es, which matters more here than
# anywhere: "Croissant Studs", "Freshly Shucked Earrings" and "Suki Claw" are
# exactly the titles a naive \bstud\b misses.
_ACCESSORY_SUBTYPES = [
    # Hair BEFORE jewellery: a "pearl hair pin" is a hair accessory, and 'pearl'
    # would otherwise claim it. Claw/clip/pin are only hair words when a hair
    # noun is present or the word is unambiguous on its own (scrunchie, barrette).
    ("hair", [
        "scrunchie", "barrette", "headband", "hair claw", "claw clip", "hair clip",
        "hair pin", "hairpin", "hair tie", "hair band", "hair comb", "hair pick",
        "hair slide", "bobby pin", "hair bow", "hair fork", "hair stick",
        # Bare "claw" and "clip": inside an ACCESSORIES product these are hair
        # pieces almost without exception ("Suki Claw", "Leaf Claw", "Checker
        # Clip", "Conch Clip" — the whole Chunks / etc. / Casa Clara shelf). The
        # residual risk is a clip-on EARRING landing here, which changes nothing
        # that matters: both hair and jewellery are trinkets, so the outcome is
        # identical either way.
        "claw", "clip",
    ]),
    ("hosiery", [
        "sock", "tight", "stocking", "hosiery", "knee high", "knee-high",
        "legwarmer", "leg warmer", "trouser sock", "crew sock", "ankle sock",
    ]),
    # Stationery, homewares and desk objects. Several fashion labels sell a
    # bookmark, a journal or a candle, and Shopify files them under the same
    # accessories bucket as a handbag — so they arrive in a fashion swipe deck
    # with no way for the app to tell them apart. This is the same complaint as
    # the trinket wall, one step further from clothes.
    #
    # Ahead of jewellery deliberately: "Key Ring Charm Holder" is a keychain, and
    # 'charm' / 'chain' would otherwise claim it for jewellery. Both are
    # trinkets, so the outcome is the same either way — this just labels it
    # honestly for anyone reading the catalog later.
    ("homeware", [
        "bookmark", "journal", "notebook", "notepad", "candle", "mug", "tumbler",
        "coaster", "sticker", "postcard", "greeting card", "puzzle", "poster",
        "keychain", "key chain", "keyring", "key ring", "magnet",
        "laptop case", "laptop sleeve", "phone case", "airpod", "tech accessory",
        "matchbox", "incense", "vase", "tray", "blanket", "towel", "pillow",
    ]),
    ("jewellery", [
        "earring", "necklace", "bracelet", "anklet", "pendant", "brooch",
        "stud", "hoop", "choker", "charm", "signet", "cuff", "locket",
        "bangle", "ear cuff", "body chain", "huggie", "jewel",
        # NOT bare 'chain' — "chain strap bag", "chain detail" and "chain trim"
        # are all hardware on something else, and bag is matched after this.
        "chain necklace", "chain bracelet", "curb chain", "box chain",
        "rope chain", "figaro", "herringbone chain",
    ]),
    ("scarf", ["scarf", "shawl", "bandana", "kerchief", "neckerchief", "pashmina"]),
    ("bag", [
        "bag", "tote", "purse", "clutch", "pouch", "backpack", "crossbody",
        "satchel", "hobo", "shopper", "wallet", "cardholder", "card holder",
        "card case", "coin purse", "duffle", "duffel",
    ]),
    ("hat", [
        "hat", "cap", "beanie", "beret", "bucket hat", "visor", "headscarf",
        "balaclava", "fascinator", "rancher",
    ]),
    # Eyewear brands title by MODEL NAME ("Nina | Night Shade", "Bunny",
    # "nolan ; mustard tortoiseshell"), so the only reliable words are the
    # materials. Eyewear is kept, not demoted — this just gets the label right.
    ("eyewear", ["sunglass", "sunnies", "eyewear", "shades", "spectacle",
                 "acetate", "tortoiseshell", "tortoise shell", "lens"]),
    ("belt", ["belt", "sash", "waistband", "suspender"]),
    ("gloves", ["glove", "mitten", "mitt"]),
]

# 'ring' is genuinely ambiguous — a ring is jewellery, but "ring detail",
# "O-ring strap", "ring handle bag" and "key ring" are hardware on something
# else. So it lives outside the keyword lists with its own guards: a standalone
# 'ring' that is not part of a compound and is not modifying a hardware noun.
_RING_RE = re.compile(r"(?<![a-z0-9-])rings?(?![a-z0-9])", re.I)
_RING_NOT_JEWELLERY_RE = re.compile(
    r"(?:key|o|d|split|tension|pull)[- ]rings?\b"
    r"|\brings?[- ](?:detail|handle|pull|trim|strap|closure|buckle|hardware|zip)",
    re.I,
)

# A brand whose NAME says jewellery is a stronger signal than any title. These
# labels sell nothing else, and their titles are frequently just a first name
# ("Maya Studs", "Zoey Earrings", "Iris Necklace") where the garment noun is the
# only word doing any work — and sometimes not even that ("Ramen Studs in Gold").
_JEWELLERY_BRAND_RE = re.compile(r"\b(jewel|jewellery|jewelry|joyas|bijoux)\b", re.I)

# A garment noun in the title VETOES a trinket classification. Some stores file
# real clothes under an accessories product_type, and the accessory word is then
# describing the STYLE of a garment rather than naming an object:
#     "Scarf Top" · "SCARF TOP | black silk" · "Livae Knitted Scarf Top"
# Seven of those were live, all genuine tops, and demoting them would have hidden
# real clothes to solve a trinket problem. When both readings are possible, the
# garment wins and the piece falls through to `other` — kept and never demoted.
# ("top handle" is excluded: that is a bag, not a top.)
_GARMENT_VETO_RE = re.compile(
    r"\b(?:dress|skirt|trouser|jean|coat|jacket|shirt|blouse|sweater|cardigan|"
    r"blazer|pant|jumpsuit|romper|bodysuit|gown|kaftan|caftan|robe|tee|"
    r"top(?![- ]handle))\w*\b",
    re.I,
)


def infer_accessory_subtype(category, title, product_type="", brand=""):
    """Sub-classify an accessory/shoe. Returns a subtype string, or None for
    anything that isn't an accessory (garments have no subtype).

    'shoes' is its own top-level category already, so it maps straight through
    rather than being re-derived — the app treats it as a real fashion category,
    not a trinket.
    """
    if category == "shoes":
        return "shoes"
    if category != "accessories":
        return None
    hay = f"{title or ''} {product_type or ''}".lower()
    # Only the TITLE can veto — a store's accessories product_type is exactly the
    # mis-filing we're correcting for, so it must not be able to veto itself.
    garment = _GARMENT_VETO_RE.search(str(title or ""))
    for subtype, words in _ACCESSORY_SUBTYPES:
        if any(_word_in(w, hay) for w in words):
            if garment and subtype in TRINKET_SUBTYPES:
                return "other"        # real clothing, mis-filed — keep it
            return subtype
    # Standalone 'ring' — checked after the lists so "ring handle bag" has
    # already been claimed by `bag`, and guarded so hardware never reads as
    # jewellery. Catches the bare-noun titles the lists miss: "BAE Ring",
    # "Zap Ring in Sterling Silver", "galán ring", "Large Bow Ring".
    if _RING_RE.search(hay) and not _RING_NOT_JEWELLERY_RE.search(hay):
        return "jewellery"
    if _JEWELLERY_BRAND_RE.search(str(brand or "")):
        return "jewellery"
    return "other"


# Trinkets: small, cheap, high-volume pieces that fill a feed without being what
# anyone opened the app for. NOT a quality judgement and NOT a removal from the
# app — the Discover ranker demotes them and the weight is remotely tunable. The
# one place they ARE removed is a partner shop's shelf (see normalize): when we
# curate 400 pieces to represent a boutique, we take their clothes, not their
# trinket wall.
TRINKET_SUBTYPES = {"jewellery", "hair", "hosiery", "scarf", "homeware"}


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
        # WORD-BOUNDARY matching (fixed 2026-07-29). Plain substring matching had
        # every colour keyword firing inside unrelated words, and because only the
        # first 2 tags survive, the phantom tag also EVICTED the real colour —
        # corrupting the Colour filter and the colour signal the taste engine
        # learns from. Measured on the live catalog before the fix:
        #   'tan'  ⊂ tank   -> 166/167 tanks tagged BROWN
        #   'oat'  ⊂ coat   ->   67/69 coats tagged NEUTRAL
        #   'sand' ⊂ sandal ->   23/23 sandals tagged NEUTRAL
        #   'red'  ⊂ tiered/embroidered/tailored/gathered/layered/flared…
        #                   ->  441 tagged RED, 302 of them with no real 'red'
        # _word_in() handles hyphens as boundaries, so 'off-white' and 'tie-dye'
        # still match, and it tolerates a trailing s/es.
        for tag, kws in COLOR_RULES:
            if any(_word_in(k, hay) for k in kws):
                found.append(tag)
        if any(_word_in(h, hay) for h in MULTICOLOR_HINTS):
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


# Size-chart graphics detected from their OWN filename / alt text — stores name
# them literally (size-chart.jpg, alt="Size Guide"). We never inspect pixels.
_SIZECHART_IMG_RE = re.compile(
    r"size[-_ ]?(?:chart|charts|guide|table|ref)|sizing|measurement|how[-_ ]?to[-_ ]?measure|fit[-_ ]?guide",
    re.I,
)


def _is_size_chart_img(im):
    """True when an image record's src filename or alt text says size chart."""
    hay = f"{im.get('src') or ''} {im.get('alt') or ''}"
    return _SIZECHART_IMG_RE.search(hay) is not None


def _product_image_srcs(product):
    """All image srcs with real product photos FIRST and size-chart graphics
    pushed to the END. Some stores list the chart as images[0], which used to
    become the tile hero (Aniqa's size-chart-photos report). A chart still
    survives as the last resort for a product whose ONLY image is a chart, so
    nothing renders blank."""
    real, charts = [], []
    for im in product.get("images") or []:
        src = im.get("src")
        if not src:
            continue
        (charts if _is_size_chart_img(im) else real).append(src)
    return real + charts


def first_image(product):
    srcs = _product_image_srcs(product)
    return srcs[0] if srcs else None


def gallery_images(product, n=5):
    """Up to `n` image src's for the product gallery (hero first, deduped,
    size-chart graphics only as a last resort)."""
    out = []
    seen = set()
    for src in _product_image_srcs(product):
        if len(out) >= n:
            break
        if src not in seen:
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


def normalize(product, brand, domain, fx, multi_brand=False, retailer=None):
    title = (product.get("title") or "").strip()
    handle = product.get("handle")
    img = first_image(product)
    raw_price = first_price(product)
    if not title or not handle or not img or not raw_price:
        return None
    price = round(raw_price * fx)
    if price <= 0:
        return None
    product_type = product.get("product_type", "")
    # Drop gift cards, returns/refunds, shipping/insurance add-ons, size charts,
    # swatches, samples, deposits, etc. Checks title AND product_type (never tags).
    if is_junk(title, price, product_type):
        return None
    # ── Partner RETAILER scoping ──────────────────────────────────────────────
    # A retailer source is a real shop (e.g. Gemini, Chicago) whose whole catalog
    # we do NOT want: Loupe is a women's app, and a shop also sells menswear,
    # candles, books and kitchenware. Keep only the product types the partnership
    # covers, and only what's actually purchasable — a partner tile that leads to
    # a sold-out page is worse than no tile.
    if retailer:
        wanted = [str(t).strip().lower() for t in (retailer.get("productTypes") or [])]
        if wanted and str(product_type or "").strip().lower() not in wanted:
            return None
        if retailer.get("inStockOnly", True):
            if not any(v.get("available") for v in (product.get("variants") or [])):
                return None
    # Multi-brand boutiques (e.g. Arete Studios) resell many designers under one
    # storefront. Label each item with its REAL vendor when present, falling back
    # to the store name — so the app shows the designer, not the shop, as the brand.
    display_brand = brand
    if multi_brand:
        vendor = (product.get("vendor") or "").strip()
        if vendor and vendor.lower() not in ("", "frontpage"):
            display_brand = vendor
    # Brand removed from the feed entirely (founder decision) — drop it regardless
    # of which storefront it entered through (direct or reseller vendor field).
    if _norm_brand(display_brand) in EXCLUDE_BRANDS:
        return None
    category = infer_category(product_type, title, product.get("tags"))
    subtype = infer_accessory_subtype(category, title, product_type, display_brand)
    # ── Partner shelves carry clothes, not the trinket wall ───────────────────
    # A partner's presence in Loupe is a CURATED shelf we assemble on their
    # behalf, not a mirror of their store. Gemini's 400 pieces were 42%
    # accessories, and ~125 of those were stud earrings, hair claws and tights:
    # volume that fills a swipe deck without being what anyone opened a fashion
    # app to find. Bags, hats and shoes stay — they read as fashion and, measured
    # on Gemini's own shelf, earn more saves per piece than the jewellery did.
    #
    # This is removal, which is deliberately reserved for partner sources. For
    # brand-direct products the same trinkets are KEPT and merely demoted by the
    # ranker, because dropping them would delete whole labels from the app and
    # from the public brand directory. See TRINKET_SUBTYPES.
    if retailer and subtype in TRINKET_SUBTYPES:
        return None
    colors = infer_colors(title, product.get("options"),
                          tags=product.get("tags"), product_type=product_type)
    # Product-level availability: True if ANY variant is in stock. Disambiguates
    # sizes == [] (a one-size item) from a genuine sell-out — the app renders
    # "Out of Stock" in the sizes slot only on an explicit false.
    is_available = any(v.get("available") for v in (product.get("variants") or []))
    product_url = f"https://{domain}/products/{handle}"
    if retailer:
        # Partner links stay CLEAN and go straight to the retailer's own product
        # page (never re-wrapped by an affiliate network), with a UTM so the shop
        # can see Loupe traffic and orders in their own analytics. That attribution
        # is the evidence the partnership renews on. Falls back to the generic
        # Loupe tag if a retailer entry forgets to set one.
        affiliate = add_utm(product_url, retailer.get("utm") or LOUPE_UTM)
    else:
        # Attribution FIRST, monetization second: the UTM belongs on the brand's own
        # product URL, so it survives inside whatever the affiliate layer wraps
        # around it (a per-brand deep-link template percent-encodes this whole URL
        # as its {url}, and the brand still sees utm_source=loupe on arrival).
        affiliate = monetize(add_utm(product_url), display_brand)
    out = {
        # Namespace retailer ids so the same designer stocked BOTH direct and at a
        # partner shop can't collide on id (different stores, different handles).
        "id": (f"{retailer['id']}-{slugify(display_brand)}-{handle}" if retailer
               else f"{slugify(display_brand)}-{handle}"),
        "brand": display_brand,
        "name": title,
        "price": price,
        "category": category,
        "colorTags": colors,
        "imageUrl": img,
        "sizes": available_sizes(product),
        "images": gallery_images(product),
        "available": is_available,
        "affiliateUrl": affiliate,
    }
    if subtype:
        # Accessories and shoes only — garments carry no subtype, so this adds
        # nothing to the ~72% of the catalog that is clothing. The app reads it
        # to decide how hard to demote trinkets in Discover, and treats a missing
        # value as "no opinion", so an older catalog behaves exactly as before.
        out["accessorySubtype"] = subtype
    if retailer:
        # Resolved by the app against catalog.retailers -> tile tint, badge, and
        # the store block (address / hours / map / call) on the product page.
        out["retailer"] = retailer["id"]
    return out


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
    # id -> {cutoutUrl, cutoutStatus, cutoutAspect} from the previous catalog. Reused
    # by the cutout merge below so a failed/implausible manifest fetch carries cutouts
    # FORWARD instead of shipping a catalog that strips every product's cutoutUrl.
    prev_cutouts = {}
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
                if p.get("cutoutUrl") or p.get("cutoutStatus"):
                    prev_cutouts[pid] = {
                        "cutoutUrl": p.get("cutoutUrl"),
                        "cutoutStatus": p.get("cutoutStatus"),
                        "cutoutAspect": p.get("cutoutAspect"),
                    }
        except (ValueError, OSError):
            pass

    def scrape_page(domain, limit, since_id=None):
        """Fetch ONE products.json page with a few retries — most scrape
        'failures' are momentary timeouts / rate-limits, not a dead store.
        Shopify caps ?limit at 250; we never ask for more. `since_id` walks to
        the next page (Shopify returns products with id > since_id)."""
        # Shopify hard-caps page size at 250; asking for more is silently clamped.
        limit = min(max(int(limit), 1), 250)
        # country=US pins Shopify Markets stores to their US-market presentment.
        # Without it, /products.json prices follow the REQUESTER's geo: from CI /
        # datacenter IPs, Brooke Callahan served 485 (AED presentment) for a top
        # whose US price is $130 — shipped to users as "$485". A 2026-07-15 audit
        # found 54/153 stores geo-priced this way; with country=US they return
        # the merchant's real US price in USD (those brands are tagged USD in
        # brands.json). Non-Markets stores ignore the param entirely and keep
        # publishing their base currency, which the per-brand `currency` + fx
        # table still converts as before.
        qs = f"limit={limit}&country=US"
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

    def scrape_retailer_pages(domain, page_limit, max_pages, delay):
        """Yield successive products.json pages for a PARTNER RETAILER using
        `page=N` pagination.

        WHY NOT since_id (what scrape_brand uses):
        `since_id` assumes products come back in ASCENDING id order — it takes the
        last id on a page and asks for products greater than it. Shopify's public
        products.json returns NEWEST FIRST, i.e. DESCENDING ids, for at least some
        stores (verified on geminishop.com: 8326636077122, 8326636044354, …). The
        last id on a page is then the SMALLEST, so the next request re-returns the
        same items and the walk collapses after one page.

        That's invisible for a brand — we only want ~60 items, which page one
        satisfies — but a retailer needs the WHOLE store walked (we filter ~75% of
        a general boutique away as menswear/goods/out-of-stock). Gemini yielded 89
        of ~400 expected items for exactly this reason.

        `page=N` is stable regardless of sort order. Pages are de-duplicated by
        product id by the caller (via seen_ids), so an overlap can't double-count.
        Yields None for a page that failed after retries, so the caller can SKIP it
        and keep walking instead of losing the rest of the store.
        """
        for n in range(1, max_pages + 1):
            try:
                # country=US is NOT optional: a Shopify Markets store serves the
                # REQUESTER's geo presentment without it, and the 2026-07-15 audit
                # found 54/153 stores doing exactly that (a $130 top shipped as
                # "$485"). Partner items are the ones where a wrong price damages a
                # real business relationship, so the retailer path needs the same
                # guard the brand path has.
                data = fetch_json(
                    f"https://{domain}/products.json?limit={page_limit}&page={n}&country=US"
                )
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
                # One bad page (usually a 429) must not cost us the whole store.
                yield None
                time.sleep(max(delay, 3.0))
                continue
            page = (data or {}).get("products", []) or []
            if not page:
                return
            yield page
            if len(page) < page_limit:
                return  # short page → store exhausted
            time.sleep(delay)

    def check_currency(label, domain, configured):
        """Probe the store's LIVE presentment currency once and shout if it
        disagrees with brands.json. Appends to `summary` so the mismatch lands in
        the run log the founder actually reads. Never raises, never blocks."""
        live = live_currency(domain)
        if live and live != (configured or "USD"):
            line = (f"  ⚠ CURRENCY MISMATCH (brand={label} "
                    f"config={configured or 'USD (untagged)'} live={live})")
            summary.append(line)
            print(line, file=sys.stderr)

    def scrape_brand(domain, cap, label=None, configured=None):
        """Yield successive products.json pages for a brand, walking `since_id`
        until a short/empty page (store exhausted) or MAX_PAGES. The caller stops
        early once it has `cap` post-filter items, so for most brands this fetches
        exactly one page.

        Also fires the one-shot currency probe (see live_currency): a wrong
        per-brand `currency` silently mis-prices that brand's ENTIRE catalog by
        the FX factor, and nothing else in the pipeline can detect it."""
        check_currency(label or domain, domain, configured)
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
            for page in scrape_brand(domain, cap, label=brand,
                                     configured=entry.get("currency")):
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
            # Currency sanity: price = raw × fx[brands.json currency], so a wrong
            # per-brand currency mis-prices the WHOLE brand by the FX factor. An
            # absurd median USD price is that mistake's unmistakable signature —
            # flag it in the run log so the bad brands.json line names itself.
            # (<$12 median: a weak-currency store mistagged strong, or vice versa;
            # >$2500 median is exempt for mainstream houses, which really do that.)
            flag = ""
            if bucket:
                _prices = sorted(p["price"] for p in bucket)
                _med = _prices[len(_prices) // 2]
                if _med < 12 or (_med > 2500 and _norm_brand(brand) not in MAINSTREAM_BRANDS):
                    flag = f"  ⚠ CHECK CURRENCY (median ${_med}, currency={entry.get('currency', 'USD')})"
            summary.append(f"  {brand:<22} {got:>3} items{short}{flag}")
        except Exception as e:  # noqa: BLE001 — see the retailer handler below
            # One flaky store must never abort the whole build. urllib's error
            # classes do NOT cover mid-read failures (ConnectionResetError,
            # RemoteDisconnected, IncompleteRead, ssl.SSLError), which is how a
            # single reset could take down a run that had already scraped ~150
            # brands. Log the skip and continue.
            summary.append(f"  {brand:<22}  SKIP ({type(e).__name__})")
        time.sleep(0.5)  # be polite

    # ── Partner RETAILER sources ──────────────────────────────────────────────
    # A retailer is a real shop we've partnered with (e.g. Gemini in Chicago) that
    # stocks many designers. Unlike a brand entry we pull a LOT from one domain, so
    # each label gets its own sub-cap — otherwise a single sock label (126 items)
    # would eat the whole allocation and the partner would read as one-note.
    # Items keep the DESIGNER as the brand (the taste engine learns designers, not
    # shops) and carry a `retailer` stamp the app uses for the tint/badge/store block.
    retailer_meta = {}
    retailer_counts = {}
    for r in cfg.get("retailers", []):
        rid, rdomain = r.get("id"), r.get("domain")
        if not rid or not rdomain or not r.get("enabled", True):
            continue
        per_vendor, rbase_counts = {}, {}
        rgot = 0
        try:
            # Parsed INSIDE the try: a typo'd "cap": "400 items" in brands.json is
            # a data error by a non-engineer, and it must cost this partner — not
            # the entire daily catalog build.
            rfx = fx_table.get(r.get("currency", "USD"), 1.0)
            rcap = int(r.get("cap", 400))
            vcap = int(r.get("perVendorCap", 24))
            # A partner's items are the ones where a wrong price damages a real
            # business relationship — probe their live currency too.
            check_currency(f"[retailer] {r.get('name', rid)}", rdomain, r.get("currency"))
            # Page-based walk (see scrape_retailer_pages): a retailer needs the
            # whole store paged through, and `since_id` silently collapses on a
            # newest-first store. 250 = Shopify's max page size.
            r_pages = 0
            r_failed_pages = 0
            for page in scrape_retailer_pages(rdomain, 250, 40,
                                              float(r.get("pageDelaySeconds", 1.5))):
                if page is None:
                    r_failed_pages += 1
                    continue  # skip the bad page, keep walking
                r_pages += 1
                for product in page:
                    if rgot >= rcap:
                        break
                    norm = normalize(product, r.get("name", rid), rdomain, rfx,
                                     multi_brand=True, retailer=r)
                    if not norm or norm["id"] in seen_ids:
                        continue
                    label = norm["brand"]
                    if per_vendor.get(label, 0) >= vcap:
                        continue
                    bkey = f"{label}|{base_name(norm['name'])}"
                    if rbase_counts.get(bkey, 0) >= MAX_VARIANTS_PER_BASE:
                        continue
                    rbase_counts[bkey] = rbase_counts.get(bkey, 0) + 1
                    per_vendor[label] = per_vendor.get(label, 0) + 1
                    seen_ids.add(norm["id"])
                    by_brand.setdefault(label, []).append(norm)
                    rgot += 1
                if rgot >= rcap:
                    break
                # NB: pacing lives INSIDE scrape_retailer_pages (between fetches),
                # so there is deliberately no sleep here.
            retailer_counts[rid] = rgot
            if rgot:
                retailer_meta[rid] = {k: v for k, v in r.items()
                                      if k in ("name", "tint", "tintBorder", "tintInk",
                                               "siteUrl", "store")}
            _fail = f", {r_failed_pages} page(s) failed" if r_failed_pages else ""
            summary.append(f"  {('[retailer] ' + str(r.get('name', rid))):<22} "
                           f"{rgot:>3} items across {len(per_vendor)} labels "
                           f"({r_pages} pages{_fail})")
        except Exception as e:  # noqa: BLE001
            # Broad ON PURPOSE. urllib only wraps OSError raised during request();
            # errors from getresponse()/read() (ConnectionResetError,
            # RemoteDisconnected, IncompleteRead, ssl.SSLError) are NOT URLError and
            # would otherwise escape main() and abort the whole daily refresh after
            # ~150 brands were already scraped. This handler only logs and moves on.
            summary.append(f"  {('[retailer] ' + str(r.get('name', rid))):<22}  SKIP ({type(e).__name__})")
        time.sleep(0.5)

    # Partner-wins de-duplication: when a partner shop stocks the SAME piece we also
    # pull from the designer's own store, keep the partner's copy and drop the direct
    # one — one tile per product, and the traffic goes to the partner (the agreed
    # perk). Matched on brand + color-agnostic base title, so it survives the
    # "- Black" / "in Cream" naming differences between two storefronts.
    if retailer_meta:
        partner_keys = set()
        for label, items in by_brand.items():
            for p in items:
                if p.get("retailer"):
                    partner_keys.add((_norm_brand(label), base_name(p["name"]).lower()))
        if partner_keys:
            dropped = 0
            for label, items in by_brand.items():
                kept = []
                for p in items:
                    if (not p.get("retailer")
                            and (_norm_brand(label), base_name(p["name"]).lower()) in partner_keys):
                        dropped += 1
                        continue
                    kept.append(p)
                by_brand[label] = kept
            if dropped:
                summary.append(f"  -> partner-wins dedupe: dropped {dropped} duplicate direct listings")

    # NOTE: carry-forward for brands that returned nothing this run now happens at
    # the LABEL level (Shopify vendor), with a grace window, AFTER the curated merge
    # below — see "Grace window" further down. Doing it per-label rather than per
    # configured store keeps niche designers sold through multi-brand boutiques from
    # flickering in and out of the catalog from one day to the next.

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
                    # Same attribution-then-monetization order as scraped products,
                    # so a curated brand shows up in its own analytics too. add_utm
                    # is idempotent, so a curated row stored already-tagged is a no-op.
                    p["affiliateUrl"] = monetize(add_utm(p["affiliateUrl"]), p.get("brand"))
                products.append(p)
                added += 1
            summary.append(f"  {'(curated)':<22} {added:>3} items")
        except (ValueError, OSError) as e:
            summary.append(f"  (curated)              SKIP ({type(e).__name__})")

    # ── Grace window: keep niche LABELS from flickering out ────────────────────
    # A product's "brand" is its Shopify vendor, so multi-brand boutiques (concept
    # stores) contribute many labels from a single configured domain. Because we pull
    # only a capped slice of each store's in-stock items, the exact set of long-tail
    # labels that surfaces shifts a little every run — so a label with one or two
    # pieces would otherwise blink in and out. We stamp every LIVE product with
    # `lastSeenAt`, then carry any label that was in the last good catalog but is
    # MISSING today forward — as long as it was live within GRACE_DAYS. Pieces gone
    # longer than that age out on their own, so a genuinely-dead label still leaves.
    GRACE_DAYS = 7
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Mark everything pulled live this run (scraped + curated) as seen right now.
    for _p in products:
        _p["lastSeenAt"] = now_iso

    def _seen_within(p, days):
        """True if p was last live within `days` (falls back to addedAt)."""
        stamp = p.get("lastSeenAt") or p.get("addedAt")
        if not stamp:
            return False
        try:
            dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return (now - dt) <= timedelta(days=days)

    present_labels = {(p.get("brand") or "").strip() for p in products}
    grace_labels = grace_items = 0
    for label, prev_items in prev_by_brand.items():
        if not label or label in present_labels:
            continue
        kept = []
        for p in prev_items:
            pid = p.get("id")
            if not pid or pid in seen_ids:
                continue
            # Re-validate against the CURRENT junk filter (title + price; carried
            # items no longer carry product_type). A tightening of is_junk() then
            # purges old junk within ONE cycle instead of lingering the whole grace
            # window — e.g. a size-chart / gift-card SKU that slipped through before.
            if is_junk(p.get("name"), p.get("price")):
                continue
            # Re-classify against the CURRENT subtype rules, for exactly the same
            # reason as the junk re-check above — and this one bites HARDER than
            # it looks. The grace window carries forward labels that are missing
            # from today's scrape, and a filter that removes every piece of a
            # label MAKES it missing. So when the partner-trinket rule first
            # shipped, the grace window immediately restored all 47 pieces it had
            # just removed (JESSA Jewelry, Namaste, Tai Jewelry…) and the change
            # looked like it had partly failed. A removal policy has to be
            # enforced on BOTH paths or it does nothing at all.
            #
            # Carried items no longer carry product_type, so classification runs
            # on title + brand alone; that's the same information the live path
            # relies on for these titles anyway.
            sub = infer_accessory_subtype(p.get("category"), p.get("name"), "", p.get("brand"))
            if p.get("retailer") and sub in TRINKET_SUBTYPES:
                continue
            if sub:
                p["accessorySubtype"] = sub
            else:
                p.pop("accessorySubtype", None)  # in case a category was corrected
            if _seen_within(p, GRACE_DAYS):
                seen_ids.add(pid)
                # Badge grace-carried items STALE: their price/sizes are frozen at
                # last-good-scrape, so the app can flag them and the digest can avoid
                # phantom "sale"/restock alerts off a frozen price (see
                # price_drop_push.load_catalog / compute_alerts).
                p["stale"] = True
                kept.append(p)  # keep its existing lastSeenAt — do NOT refresh it
        if kept:
            products.extend(kept)
            grace_labels += 1
            grace_items += len(kept)
    if grace_items:
        summary.append(
            f"  -> grace-carried {grace_items} items across {grace_labels} labels "
            f"(live within {GRACE_DAYS}d, missing today)"
        )

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

    # Final de-dup: collapse identical brand+name repeats. A piece is sometimes
    # listed once per colorway with the COLOR NOT in its title, so several entries
    # share the exact same brand + name (different handles -> ids). The id de-dup
    # can't catch these, and they read as the SAME card shown twice. Keep the FIRST
    # of each identical (brand, name); differently-named colorways stay, so variety
    # is preserved.
    _seen_bn = set()
    _deduped = []
    for prod in products:
        _bn = ((prod.get("brand") or "").strip().lower(), (prod.get("name") or "").strip().lower())
        if _bn in _seen_bn:
            continue
        _seen_bn.add(_bn)
        _deduped.append(prod)
    if len(_deduped) != len(products):
        summary.append(f"  -> de-duped {len(products) - len(_deduped)} same-name repeats")
    products = _deduped

    # Safety net: guarantee removed brands never ship, even if carried forward from
    # the previous catalog or merged in from curated.json (normalize() already drops
    # them from the live pull; this catches every other path in one place).
    if EXCLUDE_BRANDS:
        _before_excl = len(products)
        products = [p for p in products if _norm_brand(p.get("brand")) not in EXCLUDE_BRANDS]
        if len(products) != _before_excl:
            summary.append(f"  -> removed {_before_excl - len(products)} products from excluded brands")

    # `now` / `now_iso` were already computed in the grace-window step above.
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

    # ── Cutout merge (Look Builder) ────────────────────────────────────────────
    # cutout_catalog.py publishes alpha-matted WebP cutouts on the orphan `cutouts`
    # branch (jsDelivr-served) + a tiny id->{aspect,status} manifest. Stamp every
    # product that has a READY cutout with its transparent-image URL so the Look
    # Builder can float pieces on a colored artboard; anything missing/fallback
    # renders as a framed tile app-side. Fetched from RAW (uncached) so a fresh
    # cutout batch reflects on the next catalog build.
    #
    # FAIL-SAFE, NOT FAIL-WIPE: the build must NEVER ship a catalog that strips the
    # cutoutUrls it had yesterday. A raw.githubusercontent hiccup (or an implausibly
    # small manifest) used to be swallowed and the catalog committed with ZERO
    # cutoutUrls — every product lost its cutout for ≥24h. So on any fetch failure OR
    # when the freshly-merged "ready" count collapses to <50% of yesterday's, we
    # RE-STAMP each still-present product from prev_cutouts (harvested from the prior
    # catalog above) instead of shipping stripped. The build still succeeds either way.
    #
    # URL PINNING: jsDelivr caches the branch→commit mapping (and 404s) up to 12h, so
    # a webp pushed at 04:xx and stamped @cutouts in the 08:00 catalog can 404 against
    # yesterday's cached commit. When the manifest records the push commit `sha`
    # (cutout-catalog.yml writes it), pin URLs to the immutable @<sha>; otherwise fall
    # back to @cutouts (back-compat with manifests written before that change).
    _prev_ready = sum(1 for v in prev_cutouts.values() if v.get("cutoutUrl"))

    def _carry_prev_cutouts():
        """Re-stamp still-present products from the previous catalog's cutouts,
        never clobbering a fresh stamp already applied this run. Returns the number
        of cutoutUrls carried forward."""
        carried = 0
        for product in products:
            if product.get("cutoutUrl"):
                continue  # keep a fresh (or grace-carried) stamp
            info = prev_cutouts.get(product["id"])
            if not info:
                continue
            if info.get("cutoutStatus"):
                product["cutoutStatus"] = info["cutoutStatus"]
            if info.get("cutoutUrl"):
                product["cutoutUrl"] = info["cutoutUrl"]
                if info.get("cutoutAspect"):
                    product["cutoutAspect"] = info["cutoutAspect"]
                carried += 1
        return carried

    try:
        _cut_url = "https://raw.githubusercontent.com/HOboGoblin45/loupe-feed/cutouts/cutouts.json"
        _cut_req = urllib.request.Request(_cut_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(_cut_req, timeout=15) as _r:
            _cut_manifest = json.loads(_r.read().decode("utf-8"))
        _cut = _cut_manifest.get("items", {}) if isinstance(_cut_manifest, dict) else {}
        # Prefer the immutable push commit; fall back to the @cutouts branch ref.
        _cut_sha = (_cut_manifest.get("sha") or _cut_manifest.get("commit")) \
            if isinstance(_cut_manifest, dict) else None
        _cut_ref = _cut_sha if _cut_sha else "cutouts"
        _cut_base = f"https://cdn.jsdelivr.net/gh/HOboGoblin45/loupe-feed@{_cut_ref}/img/"
        _cut_n = 0
        for product in products:
            info = _cut.get(product["id"])
            if not info:
                continue
            product["cutoutStatus"] = info.get("status", "fallback")
            if info.get("status") == "ready":
                # PERCENT-ENCODE the id (2026-07-29). slugify() is Unicode-aware —
                # 'é'.isalnum() is True — so accented brands keep their accents in the
                # product id (pärlemor-…, démodémodé-…, siedrés-…, with-jéan-…). The
                # cutout file on the `cutouts` branch is named with that raw id, but a
                # raw non-ASCII byte in a URL is not a valid URL: jsDelivr 400'd all
                # ~178 of them, so five whole brands rendered as framed tiles instead
                # of cutouts in the Look Builder. quote() encodes exactly the UTF-8
                # bytes of the filename, which is the correct URL FOR THAT SAME FILE —
                # so cutout_catalog.py needs no change and the two stay in sync.
                # We deliberately do NOT fix this in slugify(): changing ids would
                # reset addedAt across those brands and orphan every saved item.
                product["cutoutUrl"] = (
                    f"{_cut_base}{urllib.parse.quote(product['id'], safe='')}.webp"
                )
                if info.get("aspect"):
                    product["cutoutAspect"] = info["aspect"]
                _cut_n += 1
        # Plausibility guard: a healthy manifest never loses half its ready cutouts
        # overnight (the backfill only grows). A collapse means a partial/empty
        # publish — carry yesterday's cutouts forward rather than stripping them.
        if _prev_ready and _cut_n < 0.5 * _prev_ready:
            _carried = _carry_prev_cutouts()
            print(
                f"WARNING: cutout manifest unavailable/implausible (merged {_cut_n} "
                f"ready vs {_prev_ready} previously) — carried {_carried} cutoutUrls "
                f"from previous catalog",
                file=sys.stderr,
            )
            summary.append(
                f"  -> merged {_cut_n} ready cutouts but that is <50% of the previous "
                f"{_prev_ready}; carried {_carried} forward (implausible manifest)"
            )
        else:
            summary.append(
                f"  -> merged {_cut_n} ready cutouts (of {len(_cut)} in manifest, "
                f"pinned @{_cut_sha[:7] if _cut_sha else 'cutouts'})"
            )
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError, OSError):
        _carried = _carry_prev_cutouts()
        print(
            f"WARNING: cutout manifest unavailable/implausible (fetch failed) — "
            f"carried {_carried} cutoutUrls from previous catalog",
            file=sys.stderr,
        )
        summary.append(
            f"  -> cutout manifest fetch failed; carried {_carried} cutoutUrls from "
            f"previous catalog (no fail-wipe)"
        )

    # ── PUBLISH GUARD: never overwrite a good catalog with a gutted one ────────
    # Runs BEFORE OUT_FILE.write_text, so a bad run leaves yesterday's catalog on
    # disk (and therefore live on jsDelivr) completely untouched.
    #
    # The only previous guard was `len(products) < 20`, and it fired AFTER the
    # write — so any run that returned, say, 3,000 of 8,314 products (a mass
    # bot-block, a Shopify-wide incident, a bad edit to brands.json, an over-eager
    # junk filter) published the gutted catalog and exited 0. Users would lose half
    # the feed with nothing in the run log saying so. Two ratios catch that:
    #   • product count down >20% vs the previous catalog
    #   • distinct brand labels down >10% vs the previous catalog
    # Both are far outside normal daily drift (a healthy day moves a few percent at
    # most) and both are skipped when there is no substantial previous catalog to
    # compare against, so a first/bootstrap run is never blocked.
    def _refuse(reason):
        print("Loupe catalog build (ABORTED — nothing written)")
        print("\n".join(summary))
        print(f"\nERROR: {reason}", file=sys.stderr)
        print("Refusing to publish. The previous catalog.json is left untouched "
              "and stays live; fix the cause and re-run.", file=sys.stderr)
        sys.exit(1)

    _prev_count = len(prev_ids)
    _prev_labels = len({b for b in prev_by_brand if b})
    _now_labels = len({(p.get("brand") or "").strip() for p in products
                       if (p.get("brand") or "").strip()})
    if len(products) < 20:
        _refuse(f"only {len(products)} products scraped — not enough to publish.")
    if _prev_count >= 100 and len(products) < _prev_count * 0.80:
        _refuse(f"product count collapsed: {len(products)} vs {_prev_count} yesterday "
                f"({100 * (1 - len(products) / _prev_count):.1f}% drop, limit 20%).")
    if _prev_labels >= 20 and _now_labels < _prev_labels * 0.90:
        _refuse(f"brand count collapsed: {_now_labels} labels vs {_prev_labels} yesterday "
                f"({100 * (1 - _now_labels / _prev_labels):.1f}% drop, limit 10%).")
    if _prev_count:
        summary.append(
            f"  -> publish guard OK: {len(products)} products / {_now_labels} labels "
            f"vs {_prev_count} / {_prev_labels} previously"
        )

    # ── Provenance ────────────────────────────────────────────────────────────
    # This file is deliberately public, and deliberately machine-readable. The
    # independent tier is invisible to AI shopping agents — these brands publish
    # no structured feed, sit in no marketplace, and are individually too small to
    # crawl. Being the trusted, citable index of that tier is a position nobody
    # occupies, and it is worth more than the file is worth withholding: every
    # product fact here is already public on the brand's own storefront, so
    # locking it down would forfeit the position while protecting nothing.
    #
    # What is NOT public is the intelligence derived from this: the visual
    # embeddings, the ranking behaviour, and the price/availability record
    # reconstructed from this repo's own history.
    #
    # Emitted BEFORE "products" so it is the first thing in the file — a crawler
    # or an agent that reads only the first kilobyte still gets the terms and the
    # attribution requirement. Unknown top-level keys are inert to the app, which
    # reads only .products / .retailers / .ranker.
    catalog = {
        "generatedAt": now_iso,
        "count": len(products),
        "provenance": {
            "source": "Loupe",
            "sourceUrl": "https://useloupe.shop",
            "description": (
                "A hand-curated index of independent women's fashion brands. Every "
                "product is scraped from the brand's own public storefront and "
                "normalized: prices converted to USD, categories and colours "
                "inferred, junk (gift cards, size charts, returns) removed."
            ),
            "brands": _now_labels,
            "products": len(products),
            "license": "CC-BY-4.0",
            "licenseUrl": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "Data from Loupe — https://useloupe.shop",
            "terms": "https://github.com/HOboGoblin45/loupe-feed/blob/main/TERMS.md",
            "updated": "daily",
        },
        "products": products,
    }
    # Partner retailers, keyed by the `retailer` stamp on a product. Stored ONCE
    # here rather than repeated on every item (564 copies of an address is ~140KB
    # of pure waste against jsDelivr's per-file ceiling). Only emitted for
    # retailers that actually contributed items this run, so a failed partner
    # scrape can never leave the app resolving a retailer with no products.
    if retailer_meta:
        live_rids = {p.get("retailer") for p in products if p.get("retailer")}
        emit = {k: v for k, v in retailer_meta.items() if k in live_rids}
        if emit:
            catalog["retailers"] = emit
            summary.append("  -> retailers: " + ", ".join(
                f"{k} ({sum(1 for p in products if p.get('retailer') == k)} items)" for k in emit))
    # Optional remote RANKER config: if loupe-feed/ranker_config.json exists, embed
    # it as catalog.ranker so the app can tune the Discover ranker weights (or KILL
    # the multi-signal boost via {"enabled": false}) with no OTA. Absent/malformed →
    # omit the block (the app falls back to its built-in defaults). Fail-open.
    try:
        _ranker_path = HERE / "ranker_config.json"
        if _ranker_path.exists():
            _ranker_cfg = json.loads(_ranker_path.read_text(encoding="utf-8"))
            if isinstance(_ranker_cfg, dict):
                _ranker_cfg.pop("_comment", None)
                catalog["ranker"] = _ranker_cfg
                summary.append(f"  -> embedded ranker config (version={_ranker_cfg.get('version')})")
    except (ValueError, OSError) as e:
        summary.append(f"  -> ranker_config.json present but unreadable ({type(e).__name__}); omitted")
    # Compact separators: pretty-printing made every daily commit a full-file diff
    # and inflated the payload ~30% against jsDelivr's ~20MB/file ceiling. The
    # app never reads this by eye; use scripts or jq locally.
    OUT_FILE.write_text(json.dumps(catalog, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    # Version stamp for the app-side download probe (see update_meta.py).
    from update_meta import write_meta
    write_meta()

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
    # NOTE: the "too few products" check used to live here \u2014 i.e. AFTER the catalog
    # had already been written to disk. It (and the new shrink ratios) now run in the
    # PUBLISH GUARD above, before OUT_FILE.write_text, so a bad run can't overwrite a
    # good catalog and only then complain.


if __name__ == "__main__":
    main()
