#!/usr/bin/env python3
"""Accessory-subtype regression fixtures — run by CI before every catalog build.

Plain asserts (no pytest dep), matching the other gates in this repo.

WHY THIS EXISTS. `accessories` holds two different things: pieces someone opens
a fashion app to find (a bag, a hat, sunglasses) and trinkets that fill a deck
without being what anyone came for (stud earrings, hair claws, tights, a
bookmark). The subtype drives two live decisions — trinkets are REMOVED from a
partner shop's curated shelf and DEMOTED app-wide in Discover — so a
misclassification either hides real clothing or leaves the trinket wall in.

Every case below is a real title from the live catalog.
"""
from build_catalog import infer_accessory_subtype, TRINKET_SUBTYPES

# (category, title, brand) -> expected subtype
CASES = [
    # ── Garments carry no subtype at all ─────────────────────────────────────
    ("dresses", "Rafaela Railway Stripes Dress", "Thinking Mu", None),
    ("tops", "Striped Sailor Neck Blouse", "Tiny Big Sister", None),
    ("bottoms", "Gigi Baggy Wide Turn-Up Jeans", "Damson Madder", None),
    ("outerwear", "Belted Short Trench Coat", "Sentaler", None),

    # ── Shoes map straight through: a real fashion category, not a trinket ────
    ("shoes", "Pinky Loafer in Sky", "Intentionally Blank", "shoes"),
    ("shoes", "Sybil Ankle Boot in Rouge Noir Haircalf", "Khaite", "shoes"),

    # ── Jewellery, including the bare-noun titles a naive \bstud\b misses ─────
    ("accessories", "Burbuja Necklace in White", "Amt.", "jewellery"),
    ("accessories", "Croissant Studs", "Namaste Jewelry", "jewellery"),
    ("accessories", "Freshly Shucked Earrings", "Haricot Vert", "jewellery"),
    ("accessories", "Make it Dirty Hoops", "Peter and June", "jewellery"),
    ("accessories", "Gold Plated Bow Cuff", "Cities in Dust", "jewellery"),
    ("accessories", "Fruit Basket Choker", "Girls Crew", "jewellery"),
    # Bare 'ring' — the guarded case. These four were all live and unclassified.
    ("accessories", "Medium Sterling Silver Abalone Ring", "VESTIGE", "jewellery"),
    ("accessories", "Zap Ring in Sterling Silver - Blue Topaz", "Mondo Mondo", "jewellery"),
    ("accessories", "BAE Ring - FW26", "Faris", "jewellery"),
    ("accessories", "Large Bow Ring", "Cities in Dust", "jewellery"),
    # ...but ring-as-HARDWARE is not jewellery.
    ("accessories", "O-Ring Leather Belt", "C'est Nous", "belt"),
    # A keychain is a trinket, not a bag. 'charm' would have claimed it for
    # jewellery, which is the same OUTCOME (both are trinkets) but the wrong
    # label — homeware is matched first so the catalog reads honestly.
    ("accessories", "Key Ring Charm Holder", "Primecut", "homeware"),
    # Bare 'chain' is hardware, not jewellery — bag must still win.
    ("accessories", "Chain Strap Shoulder Bag", "Miaou", "bag"),
    ("accessories", "Delicate Box Chain Necklace in Gold", "Martha Calvo", "jewellery"),
    # Brand name as the fallback signal, when the title says nothing.
    ("accessories", "Maya", "MAIVE Jewelry", "jewellery"),

    # ── Hair. Bare claw/clip counts — it is the whole Chunks / etc. shelf ─────
    ("accessories", "Mini Sheer Rose Stuffed Scrunchie", "Room Shop", "hair"),
    ("accessories", "Suki Claw", "Chunks", "hair"),
    ("accessories", "Leaf Claw", "et cetera", "hair"),
    ("accessories", "Checker Clip in Blue/White", "Chunks", "hair"),
    ("accessories", "Ball Hair Pick", "etc.", "hair"),

    # ── Hosiery ──────────────────────────────────────────────────────────────
    ("accessories", "Opaque Zokki Colored Tights in Black", "Tabbisocks", "hosiery"),
    ("accessories", "Women's Sunday Sweatshirt Sock", "American Trench", "hosiery"),
    ("accessories", "Olivia Premium Tights in Black", "Swedish Stockings", "hosiery"),

    # ── Homeware: stationery and desk objects filed as accessories ────────────
    ("accessories", "The Crustacean Bookmark", "Alighieri", "homeware"),
    ("accessories", "The Lewis Journal in Black", "Maketh Thou", "homeware"),
    ("accessories", "Kat Laptop Case Leather Black", "Flattered", "homeware"),

    # ── Kept: these read as fashion and outperform the trinkets on saves ──────
    ("accessories", "Pia Rattan Shoulder Bag in Tan", "The Artisan and Company", "bag"),
    ("accessories", "ASA XL TOTE BAG - NATURAL", "Cult Gaia", "bag"),
    ("accessories", "Cardholder in Robin Leather", "Primecut", "bag"),
    ("accessories", "Colette Fedora Hat in Ivory", "Olive & Pique", "hat"),
    ("accessories", "Alpaca Pom Beanie", "Cableami", "hat"),
    ("accessories", "Noor Black Sunglasses", "DMY Studios", "eyewear"),
    ("accessories", "Bamba | Black Acetate | Black Lens", "Port Tanger", "eyewear"),
    ("accessories", "Eira Belt", "ALEXIS", "belt"),
    ("accessories", "Sheer Lace Gloves in Off White", "Esthe", "gloves"),
    ("accessories", "Split Scarf in Cream", "Echo", "scarf"),

    # ── THE GARMENT VETO ─────────────────────────────────────────────────────
    # Seven of these were live: real tops their store filed under accessories.
    # Demoting them would hide clothing to solve a trinket problem, so a garment
    # noun in the title beats any trinket reading and falls through to `other`.
    ("accessories", "Scarf Top Ivory", "Eshim", "other"),
    ("accessories", "SCARF TOP | black silk", "Phoebe Philo", "other"),
    ("accessories", "Livae Knitted Scarf Top", "Sir the Label", "other"),
    ("accessories", "Spiral Lace Scarf Top", "Christopher Esber", "other"),
    ("accessories", "The Hobby Horse Scarf Top", "Conner Ives", "other"),
    # ...and the veto must not eat a genuine scarf that merely mentions knitting.
    ("accessories", "Cable Knit Skinny Scarf", "Oddli", "scarf"),
    # 'top handle' is a bag, never a top — so the veto must not fire on it.
    ("accessories", "T-lock leather top handle bag", "Marge Sherwood", "bag"),

    # ── Unknown is the SAFE bucket: kept, never demoted ───────────────────────
    ("accessories", "1225 MADISON AVE", "Side Eye", "other"),
    ("accessories", "Silk tie", "Dries Van Noten", "other"),
]


def main():
    failures = []
    for category, title, brand, expected in CASES:
        got = infer_accessory_subtype(category, title, "", brand)
        if got != expected:
            failures.append(f"  {category:12} {title[:44]:44} expected {expected}, got {got}")

    # Contract checks the cases above can't express on their own.
    assert TRINKET_SUBTYPES == {"jewellery", "hair", "hosiery", "scarf", "homeware"}, \
        "TRINKET_SUBTYPES changed — the app's demotion list and the partner-shelf " \
        "filter both key off this, so a change here needs a deliberate review."
    for keep in ("bag", "hat", "shoes", "eyewear", "belt", "gloves", "other"):
        assert keep not in TRINKET_SUBTYPES, f"{keep} must never be a trinket"
    # `other` is the fallback, so it must be safe: unknown accessories are kept.
    assert "other" not in TRINKET_SUBTYPES
    # A product_type must never be able to veto — that's the mis-filing we fix.
    assert infer_accessory_subtype("accessories", "Croissant Studs", "Dresses",
                                   "Namaste") == "jewellery"
    # Empty / missing inputs must not throw.
    assert infer_accessory_subtype("accessories", "", "", "") == "other"
    assert infer_accessory_subtype(None, None, None, None) is None

    if failures:
        print(f"FAIL — {len(failures)} of {len(CASES)} subtype cases:")
        print("\n".join(failures))
        raise SystemExit(1)
    print(f"OK — accessory subtype: {len(CASES)} cases pass")


if __name__ == "__main__":
    main()
