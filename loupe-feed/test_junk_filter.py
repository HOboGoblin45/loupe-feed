#!/usr/bin/env python3
"""Junk-filter regression fixtures — run by CI before every catalog build.

Plain asserts (no pytest dep). Each case encodes a PAST INCIDENT or a confirmed
contract; if a filter tweak flips one, the workflow fails BEFORE publishing a
broken feed instead of after users see it.
"""
from build_catalog import is_junk

# (title, price, product_type) -> expected is_junk
CASES = [
    # Real garments must survive — including past false-alarm vocabulary.
    ("Silk Wrap Dress", 220, "", False),
    ("Kylie Bubble Mini Dress, Graphite", 171, "", False),
    ("Sun Protection Hat", 45, "", False),          # 'protection' incident
    ("Sample Sale Tee", 30, "", False),             # 'sample' incident
    ("Route 66 Jacket", 180, "", False),            # bare-'route' over-drop (fixed 2026-07)
    # HARD_PRICE_FLOOR ($5), added 2026-07-29. A sub-$5 listing is a data artifact
    # (placeholder / deposit / mispriced sample) regardless of title — curated
    # fashion doesn't retail under $5, and these surfaced as "from $1" on the
    # public brand directory. This case previously expected False (the old policy:
    # a cheap price only counted as junk ALONGSIDE an add-on word); the policy
    # changed deliberately, so the fixture changed with it.
    ("Dress", 1.0, "", True),                       # $1 "dress" = artifact, drop
    ("Laces Oreo", 1, "", True),                    # real observed $1 row (Maguire)
    # ...but the floor must not eat genuinely cheap REAL product. Observed live
    # lows: Heaven Mayhem $6, Los Angeles Apparel $8, Anni Lu $10.
    ("Beaded Ring", 6, "", False),
    ("Cotton Socks", 8, "", False),
    ("Enamel Charm", 5, "", False),                 # exactly at the floor = keep
    # Confirmed junk must stay junk.
    ("Gift Card", None, "", True),
    ("E-Gift Voucher", 50, "", True),
    ("Returns and Exchanges", None, "", True),
    ("Size Chart", None, "", True),
    ("Shipping Protection", 2.5, "", True),
    ("Route Package Protection", 1.98, "", True),   # price-gated add-on word
    ("", None, "", True),                            # empty title
    # product_type path.
    ("Nice Thing", 30, "gift card", True),

    # ── Non-apparel GOODS (added 2026-07-29) ─────────────────────────────────
    # Real, purchasable homeware/beauty/stationery a fashion label also sells.
    # Every one of these was LIVE in the catalog, filed as `tops`.
    ("Pistachio Perfume", 225, "", True),
    ("Scout Candle", 68, "", True),
    ("Archipelago Scented Candle", 60, "", True),
    ("The Totemic Devotion Candlestick with Candles", 829, "", True),
    ("Hand Over x Scalpers Mug Red", 25, "", True),
    ("Terry Hand Towel", 34, "", True),
    ("Oddli Notebook", 30, "", True),
    ("Oddli Keychain", 20, "", True),
    ("BRIDAL NOTECARD + ENVELOPE & SEAL", 6, "", True),
    ("Sacred Ash Incense Cones", 62, "", True),
    ("Debaser Pocket Perfume Spray", 80, "", True),
    # Promo placeholder SKU, not a product (unconditional rule).
    ("Free Scrunchie With Every Swim Item Purchased", 8, "", True),

    # ...and the SURVIVORS. The non-apparel rule is GUARDED — it only fires when
    # the title carries no garment noun — precisely so a real garment merely
    # NAMED after one of those words lives. All of these are live product.
    ("INCENSE SKORT CLAY", 98, "", False),      # Ghostboy: a skort, not incense
    ("INCENSE TOP COAL", 126, "", False),
    ("Candy Terry Swim Shorts", 120, "", False),
    ("Candy Lover Bracelet", 60, "", False),
    ("CANDY SUSPENDER MINI", 150, "", False),
    ("HAND-EMBROIDERED PUZZLE COAT, SAND/RED", 900, "", False),
    ("Roller Coaster", 200, "", False),          # Bonnie Clyde sunglasses
    ("Linen Beach Towel", 60, "", False),        # only "hand towel" is junk
    ("Bikini Cotton Terry Towel - Beach Day", 68, "", False),
    ("Towel Upcycled Mini Shorts - 101", 120, "", False),
    ("Notation Silk Dress", 300, "", False),     # 'notecard' must not match here
]


def main() -> None:
    failures = []
    for title, price, ptype, expected in CASES:
        got = is_junk(title, price, ptype)
        if got != expected:
            failures.append(f"  is_junk({title!r}, {price!r}, {ptype!r}) = {got}, expected {expected}")
    if failures:
        print("JUNK-FILTER REGRESSIONS:")
        print("\n".join(failures))
        raise SystemExit(1)
    print(f"junk-filter fixtures: {len(CASES)}/{len(CASES)} OK")


if __name__ == "__main__":
    main()
