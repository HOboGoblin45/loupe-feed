#!/usr/bin/env python3
"""first_deck.json fixtures. Plain asserts, no pytest."""
from datetime import date

from build_first_deck import build, ACCESSORY_SHARE, PER_BRAND

checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def prod(i, brand, cat="tops", imgs=2, available=True, added="2026-11-01"):
    return {"id": f"{brand}-{i}", "brand": brand, "category": cat, "available": available,
            "images": [f"https://x/{i}-{k}.jpg" for k in range(imgs)], "addedAt": added}


cat = {"generatedAt": "2026-11-18T00:00:00Z", "retailers": {"gemini": {}}, "ranker": {"w": 1},
       "featured": [{"brand": "Zed", "from": "2026-11-16", "to": "2026-11-22"}],
       "products": []}
for b in ["Ann", "Bea", "Cy", "Dee", "Eve", "Fay", "Zed"]:
    for i in range(10):
        cat["products"].append(prod(i, b, added=f"2026-11-{10 + i:02d}"))
for i in range(30):
    cat["products"].append(prod(100 + i, f"Acc{i}", cat="accessories"))
cat["products"].append(prod(900, "Solo", imgs=1))
cat["products"].append(prod(901, "Gone", available=False))

d = build(cat, date(2026, 11, 18), size=20)
ids = [p["id"] for p in d["products"]]
ok(d["firstDeck"] is True and d["count"] == len(ids) == 20, f"size and flag: {d['count']}")
ok(len(ids) == len(set(ids)), "no duplicates")
ok(ids[0].startswith("Zed-"), f"featured brand leads: {ids[:3]}")
ok(all(not i.startswith(("Solo", "Gone")) for i in ids), "one-photo and sold-out pieces excluded")
acc = sum(p["category"] == "accessories" for p in d["products"])
ok(acc <= int(20 * ACCESSORY_SHARE), f"accessories capped: {acc}")
per = {}
for p in d["products"]:
    if p["brand"] != "Zed":
        per[p["brand"]] = per.get(p["brand"], 0) + 1
ok(max(per.values()) <= PER_BRAND, f"per-brand cap: {per}")
ok(d["retailers"] == {"gemini": {}} and d["ranker"] == {"w": 1} and "featured" in d, "blocks carried")
ok(d["products"][0]["addedAt"] == "2026-11-19", "newest first within a brand")
ok(build(cat, date(2026, 11, 18), size=20) == d, "deterministic")

d2 = build(cat, date(2026, 12, 1), size=20)
ok(not d2["products"][0]["id"].startswith("Zed-") or d2["products"][0]["brand"] == "Ann", "expired window does not lead")


# A catalog too thin to fill the deck under the caps yields a shorter deck, never a
# padded one that breaks the caps.
thin = {"products": [prod(i, "Ann") for i in range(10)]}
ok(build(thin, date(2026, 11, 18), size=20)["count"] == PER_BRAND, "caps win over size")
print(f"first-deck fixtures: all OK ({checks} checks)")
