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
    ("Dress", 1.0, "", False),                       # cheap but no add-on word
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
