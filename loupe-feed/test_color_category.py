#!/usr/bin/env python3
"""Colour + category regression fixtures — run by CI before every catalog build.

Each case encodes a REAL mis-tagging measured on the live catalog. Substring
matching (the pre-2026-07-29 behaviour) fired every colour keyword inside
unrelated words, and because only the first 2 tags survive, the phantom tag also
EVICTED the real colour — corrupting the Colour filter and the colour signal the
taste engine learns from.

Measured on the live catalog before the fix:
    'tan'  inside tank   -> 166/167 tanks tagged BROWN
    'oat'  inside coat   ->   67/69 coats tagged NEUTRAL
    'sand' inside sandal ->   23/23 sandals tagged NEUTRAL
    'red'  inside tiered / embroidered / tailored / gathered / flared / layered
                         ->  441 tagged RED, 302 with no real 'red' in the title
    'short' as sleeve length -> 11/12 short-sleeve items filed as BOTTOMS

Plain asserts, no pytest dependency (matches test_junk_filter.py).
"""
from build_catalog import infer_colors, infer_category

# (title, must_not_contain, must_contain_or_None)
COLOR_CASES = [
    # The exact collisions above — the phantom tag must be gone, the real colour kept.
    ("V Neck Tank - White",                    "brown",   "white"),
    ("Mehr Mesh Tank",                         "brown",   None),
    ("Simona Black Wool Coat",                 "neutral", "black"),
    ("Double Breasted Tailored Coat in Black", "red",     "black"),
    ("Noria Tiered Gown",                      "red",     None),
    ("Sunlit Cream Embroidered Dress",         "red",     "neutral"),
    ("Embroidered Headscarf In Khaki",         "red",     "green"),
    ("Mataro Black Sandal",                    "neutral", "black"),
    ("Aurelia Sculpted Spiral Column Skirt",   "red",     None),
    ("Gathered Poplin Blouse",                 "red",     None),
    ("Flared Denim Trouser",                   "red",     None),
    ("Layered Tulle Skirt",                    "red",     None),
]

# Real colours must STILL match — the fix must not over-correct.
COLOR_KEEPS = [
    ("Cherry Red Silk Dress",        "red"),
    ("Off-White Cotton Shirt",       "white"),   # hyphen is a word boundary
    ("Tan Leather Belt",             "brown"),   # standalone 'tan' still works
    ("Sand Linen Trouser",           "neutral"), # standalone 'sand' still works
    ("Oat Cashmere Sweater",         "neutral"), # standalone 'oat' still works
    ("Black Wool Coat",              "black"),
    # Multicolour INFLECTIONS: word-boundary matching only tolerates +s/+es, so
    # the -ed forms are listed explicitly. Without them these silently lost their
    # multicolour tag when the substring bug was fixed.
    ("Long printed skirt",           "multicolor"),
    ("Striped Cotton Tee",           "multicolor"),
    ("Checked Wool Scarf",           "multicolor"),
    ("Floral Midi Dress",            "multicolor"),
    ("Polka Dot Blouse",             "multicolor"),
]

# (title, product_type, expected category)
CATEGORY_CASES = [
    # Sleeve length is not a garment type.
    ("Posie Short Sleeve Top - Burg",      "", "tops"),
    ("KEY SHORT SLEEVE IN BLACK",          "", "tops"),
    ("Louis Polo Short Sleeve Light Pink", "", "tops"),
    ("Stripe Hemp Short Sleeve Hoodie",    "", "tops"),
    ("Long Sleeve Ribbed Tee",             "", "tops"),
    ("Flat Knit Sweater",                  "", "tops"),   # not shoes via 'flat'
    ("Belted Short Trench Coat",           "", "outerwear"),
    # ...but real shorts are still bottoms.
    ("Denim Shorts",                       "", "bottoms"),
    ("Pleated Short",                      "", "bottoms"),
    # A GARMENT IS A DRESS FIRST, A SLEEVE LENGTH SECOND. The sleeve override was
    # briefly placed with the dress-adjective rules, which run before the dress
    # check — that would have flipped 14 live dresses/gowns to `tops`.
    ("Lucia Keyhole Long Sleeve Mini Dress", "", "dresses"),
    ("Naru Long Sleeve Dress Black",         "", "dresses"),
    ("Long Sleeve Midi Dress",               "", "dresses"),
    ("Octavia Long Sleeve Gown - Leopard",   "", "dresses"),
    ("Cap Sleeve Gown",                      "", "dresses"),
    ("The Short Sleeve Sequin Jersey Polo Maxi Dress", "", "dresses"),
    ("Short Sleeve Sundress",                "", "dresses"),
    # Existing contracts must not regress.
    ("Sleeper Linen Maxi Dress",           "", "dresses"),
    ("Sundress",                           "", "dresses"),
    ("Dress Pants",                        "", "bottoms"),
    ("Dress Shirt",                        "", "tops"),
    ("Mohair Sweater",                     "", "tops"),
]


def main() -> None:
    failures = []

    for title, forbidden, required in COLOR_CASES:
        got = infer_colors(title, None, tags=None, product_type="")
        if forbidden in got:
            failures.append(f"  infer_colors({title!r}) = {got} — must NOT contain {forbidden!r}")
        if required is not None and required not in got:
            failures.append(f"  infer_colors({title!r}) = {got} — expected {required!r}")

    for title, required in COLOR_KEEPS:
        got = infer_colors(title, None, tags=None, product_type="")
        if required not in got:
            failures.append(f"  infer_colors({title!r}) = {got} — real colour {required!r} LOST")

    for title, ptype, expected in CATEGORY_CASES:
        got = infer_category(ptype, title, None)
        if got != expected:
            failures.append(f"  infer_category({title!r}) = {got!r}, expected {expected!r}")

    if failures:
        print("COLOUR/CATEGORY REGRESSIONS:")
        print("\n".join(failures))
        raise SystemExit(1)

    total = len(COLOR_CASES) + len(COLOR_KEEPS) + len(CATEGORY_CASES)
    print(f"colour/category fixtures: {total}/{total} OK")


if __name__ == "__main__":
    main()
