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
import random
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
BRANDS_FILE = HERE / "brands.json"
OUT_FILE = HERE / "catalog.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ── Category inference ────────────────────────────────────────────────────────
# Checked in priority order; first hit wins. Falls back to 'tops'.
CATEGORY_RULES = [
    ("accessories", ["bag", "tote", "clutch", "pouch", "purse", "scarf", "necklace",
                      "earring", "bracelet", "ring", "pendant", "hat", "cap", "beret",
                      "belt", "sunglass", "jewel", "hair", "glove", "wallet"]),
    ("shoes",       ["shoe", "boot", "sandal", "mule", "flat", "sneaker", "heel",
                     "loafer", "pump", "clog", "slipper", "ballet"]),
    ("outerwear",   ["coat", "jacket", "blazer", "cardigan", "trench", "parka",
                     "anorak", "overcoat", "puffer"]),
    ("bottoms",     ["skirt", "trouser", "pant", "short", "jean", "legging",
                     "culotte", "capri"]),
    ("dresses",     ["dress", "gown"]),
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


def infer_colors(title, options):
    hay = title.lower()
    # pull color option values too
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
    # de-dup, keep order, cap at 2
    seen, out = set(), []
    for c in found:
        if c in VALID_COLORS and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:2] if out else ["neutral"]


def slugify(brand):
    return "".join(c if c.isalnum() else "-" for c in brand.lower()).strip("-")


def first_image(product):
    imgs = product.get("images") or []
    for im in imgs:
        src = im.get("src")
        if src:
            return src
    return None


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
    category = infer_category(product.get("product_type", ""), title)
    colors = infer_colors(title, product.get("options"))
    return {
        "id": f"{slugify(brand)}-{handle}",
        "brand": brand,
        "name": title,
        "price": price,
        "category": category,
        "colorTags": colors,
        "imageUrl": img,
        "affiliateUrl": f"https://{domain}/products/{handle}",
    }


def main():
    cfg = json.loads(BRANDS_FILE.read_text(encoding="utf-8"))
    fx_table = cfg["fx_to_usd"]
    per_brand = int(cfg.get("perBrand", 10))
    products, seen_ids = [], set()
    by_brand = {}
    summary = []

    for entry in cfg["brands"]:
        brand, domain = entry["brand"], entry["domain"]
        fx = fx_table.get(entry.get("currency", "USD"), 1.0)
        got = 0
        bucket = []
        try:
            # pull a generous page, then take the first `per_brand` valid items
            data = fetch_json(f"https://{domain}/products.json?limit={max(per_brand * 3, 30)}")
            for product in data.get("products", []):
                if got >= per_brand:
                    break
                norm = normalize(product, brand, domain, fx)
                if not norm or norm["id"] in seen_ids:
                    continue
                seen_ids.add(norm["id"])
                bucket.append(norm)
                got += 1
            if bucket:
                by_brand[brand] = bucket
            summary.append(f"  {brand:<22} {got:>3} items")
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
            summary.append(f"  {brand:<22}  SKIP ({type(e).__name__})")
        time.sleep(0.5)  # be polite

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

    catalog = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(products),
        "products": products,
    }
    OUT_FILE.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Loupe catalog build")
    print("\n".join(summary))
    print(f"\nTotal: {len(products)} products -> {OUT_FILE.name}")
    # Fail the CI run only if we got essentially nothing (keeps a bad scrape from
    # overwriting a good catalog with an empty one).
    if len(products) < 20:
        print("ERROR: too few products scraped \u2014 not enough to publish.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
