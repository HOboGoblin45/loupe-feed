#!/usr/bin/env python3
"""first_deck.json: a small slice of catalog.json for a phone's first open.

WHY THIS EXISTS
A first open has no cache, so the app downloads the whole catalog (about
11.7 MB) before the first card can show. PostHog `catalog_loaded.ms` over the
45 days to 2026-11-18: p50 2.2 s, p90 7.3 s, and 43% of people who load the
catalog never swipe once. This file is the same schema as catalog.json with a
few hundred pieces, so the app can show a real deck in well under a second and
swap in the full catalog when it lands.

WHAT IS IN IT (deterministic for a given catalog.json)
  - only available pieces with at least 2 photos (the first deck is the shop
    window; one-photo pieces are fine later, not first);
  - the featured brand's pieces first (up to FEATURED_MAX), so a paid or
    dated placement is honoured from the very first card;
  - then round-robin across brands, newest first within a brand, at most
    PER_BRAND each, so no label dominates the first impression;
  - accessories capped at ACCESSORY_SHARE of the deck (the catalog is 24%
    accessories; a first deck of rings and bags reads as a jewellery app);
  - the catalog's retailers / featured / priceCorrections / ranker blocks,
    copied as they are, because the app's parser reads them from any feed.

    python loupe-feed/build_first_deck.py [--catalog PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECK_SIZE = 300
PER_BRAND = 2
FEATURED_MAX = 6
ACCESSORY_SHARE = 0.20
CARRY_BLOCKS = ("retailers", "featured", "priceCorrections", "ranker")


def eligible(p: dict) -> bool:
    imgs = p.get("images") or []
    return p.get("available", True) is not False and isinstance(imgs, list) and len(imgs) >= 2


def featured_brands(catalog: dict, today: date) -> set:
    out = set()
    for w in catalog.get("featured") or []:
        try:
            if date.fromisoformat(w["from"]) <= today <= date.fromisoformat(w["to"]):
                out.add(w["brand"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def pick(catalog: dict, today: date, size: int = DECK_SIZE) -> list:
    pool = [p for p in catalog.get("products") or [] if eligible(p)]
    # newest first, id as a stable tie-break
    pool.sort(key=lambda p: (p.get("addedAt") or "", p.get("id") or ""), reverse=True)

    max_acc = int(size * ACCESSORY_SHARE)
    chosen, seen, acc = [], set(), 0

    def take(p) -> bool:
        nonlocal acc
        if p["id"] in seen or len(chosen) >= size:
            return False
        if p.get("category") == "accessories":
            if acc >= max_acc:
                return False
            acc += 1
        chosen.append(p)
        seen.add(p["id"])
        return True

    feat = featured_brands(catalog, today)
    n_feat = 0
    for p in pool:
        if n_feat >= FEATURED_MAX:
            break
        if p.get("brand") in feat and take(p):
            n_feat += 1

    by_brand: "OrderedDict[str, list]" = OrderedDict()
    for p in pool:
        by_brand.setdefault(p.get("brand") or "", []).append(p)
    # brands in a stable order: alphabetical, so the deck does not change just
    # because one brand's newest piece moved
    brands = sorted(by_brand)
    taken = {b: 0 for b in brands}
    for p in chosen:
        taken[p.get("brand") or ""] = taken.get(p.get("brand") or "", 0) + 1
    cursor = {b: 0 for b in brands}
    progressed = True
    while len(chosen) < size and progressed:
        progressed = False
        for b in brands:
            if len(chosen) >= size:
                break
            if taken[b] >= PER_BRAND:
                continue
            items = by_brand[b]
            while cursor[b] < len(items):
                p = items[cursor[b]]
                cursor[b] += 1
                if take(p):
                    taken[b] += 1
                    progressed = True
                    break
    return chosen


def build(catalog: dict, today: date, size: int = DECK_SIZE) -> dict:
    products = pick(catalog, today, size)
    out = {
        "generatedAt": catalog.get("generatedAt"),
        "count": len(products),
        "firstDeck": True,
        "products": products,
    }
    for k in CARRY_BLOCKS:
        if k in catalog:
            out[k] = catalog[k]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=str(HERE / "catalog.json"))
    ap.add_argument("--out", default=str(HERE / "first_deck.json"))
    args = ap.parse_args()
    with open(args.catalog, encoding="utf-8") as f:
        catalog = json.load(f)
    deck = build(catalog, date.today())
    body = json.dumps(deck, ensure_ascii=False, separators=(",", ":"))
    Path(args.out).write_text(body, encoding="utf-8")
    brands = len({p.get("brand") for p in deck["products"]})
    print(f"first_deck.json: {deck['count']} pieces, {brands} brands, {len(body.encode()) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
