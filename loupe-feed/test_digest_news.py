#!/usr/bin/env python3
"""Daily digest: phantom sales and repeat pushes -- run by CI before every build.

Two faults, both reported on 2026-11-09 as "I get the same price drop alert for
Brooke Callahan every day":

  1. THE SALE WAS NOT A SALE. Pieces saved 2026-07-01..15 were stored at a foreign
     presentment price read as dollars (a $130 top saved at 485), so they looked
     73% off forever. The cases below are the real saves, at the real prices.
  2. THE PUSH REPEATED. It re-sent whenever anything about any saved piece changed,
     but only ever named the best sale, so one size selling out somewhere re-sent
     the identical sentence.
"""
import json
from pathlib import Path

import build_catalog
import price_drop_push as pdp
from alert_factors import alert_factors

HERE = Path(__file__).parent
DOC = json.loads((HERE / "price_corrections.json").read_text(encoding="utf-8"))
FACTORS = pdp.load_price_corrections()
checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def saved(pid, brand, name, price_at_save, sizes=None):
    return {"user_id": "u1", "product_id": pid, "price_at_save": price_at_save,
            "product": {"id": pid, "brand": brand, "name": name, "sizes": sizes or []}}


def live(pid, brand, name, price, sizes=None):
    return {"id": pid, "brand": brand, "name": name, "price": price, "sizes": sizes or []}


# ── 1. the real saves, at the real stored and live prices ────────────────────
REAL = [  # (product id, brand, stored price_at_save, live price on 2026-11-09)
    ("brooke-callahan-wool-skirt-black", "Brooke Callahan", 989, 265),
    ("brooke-callahan-halter-top-black", "Brooke Callahan", 485, 130),
    ("brooke-callahan-tassel-jacket-black", "Brooke Callahan", 634, 170),
    ("brooke-callahan-straight-skirt-black", "Brooke Callahan", 485, 130),
    ("brooke-callahan-bow-top-blush", "Brooke Callahan", 485, 130),
    ("brooke-callahan-halter-top-purple", "Brooke Callahan", 485, 130),
    ("marland-backus-cute-as-a-button-hairpin", "Marland Backus", 273, 73),
    ("marland-backus-bow-hairpins", "Marland Backus", 153, 41),
]
for pid, brand, then, now in REAL:
    ok(pdp.is_meaningful_price_drop(now, then), f"{pid}: fixture is not even a drop")
    items = [saved(pid, brand, "Piece", then)]
    cat = {pid: live(pid, brand, "Piece", now)}
    ok(pdp.compute_alerts(items, cat, {}) and pdp.compute_alerts(items, cat, {})[0]["sale"],
       f"{pid}: without the record this must alert, or the test proves nothing")
    ok(pdp.compute_alerts(items, cat, FACTORS) == [],
       f"{pid}: saved at {then}, now {now} -- still reported as a sale")

# A REAL markdown on the same brand still alerts: the guard matches a ratio, not a name.
items = [saved("bc-x", "Brooke Callahan", "Bow Top", 130)]
real = pdp.compute_alerts(items, {"bc-x": live("bc-x", "Brooke Callahan", "Bow Top", 91)}, FACTORS)
ok(real and real[0]["sale"] and real[0]["sale"]["pct"] == 30, "a real 30% markdown on Brooke Callahan was suppressed")
# ...and so does a correctly-priced save that happens to be on the corrected brand.
ok(pdp.compute_alerts([saved("bc-y", "Brooke Callahan", "Skirt", 265)],
                      {"bc-y": live("bc-y", "Brooke Callahan", "Skirt", 265)}, FACTORS) == [],
   "an unchanged price alerted")

# ── 2. the shared record ─────────────────────────────────────────────────────
ok({"Brooke Callahan", "Marland Backus", "MISBHV"} <= set(FACTORS), "alert-only brands missing from the digest's table")
ok(all(c["brand"] in FACTORS for c in DOC["corrections"]), "an archive correction is missing from the digest's table")
ok(alert_factors({"corrections": [{"brand": "A", "factor": 0.5}],
                  "alertCorrections": [{"brand": "A", "factor": 0.9}, {"brand": "B", "factor": 0.3}]})
   == {"A": 0.5, "B": 0.3}, "`corrections` must win over `alertCorrections` for the same brand")
ok(alert_factors({"alertCorrections": [{"brand": "", "factor": 1}, {"brand": "X", "factor": 0},
                                       {"brand": "Y", "factor": "nope"}, {"brand": "Z", "factor": -2},
                                       "junk", {"factor": 0.5}, {"brand": "N", "factor": float("nan")},
                                       {"brand": "I", "factor": float("inf")}]}) == {},
   "malformed alert corrections must be dropped")
ok(alert_factors(None) == {} and alert_factors({}) == {}, "an empty record must yield an empty table")
no_dupes = [c["brand"] for c in DOC.get("alertCorrections", [])]
ok(len(no_dupes) == len(set(no_dupes)), "a brand appears twice in alertCorrections")
# The app reads the same map from catalog.json. Nothing published it before today.
src = (HERE / "build_catalog.py").read_text(encoding="utf-8")
ok('catalog["priceCorrections"]' in src and "alert_factors(" in src,
   "build_catalog.py no longer publishes priceCorrections for the app")

# ── 3. a push is sent only when it has something NEW to say ──────────────────
USERS = {"u1": {"token": "ExponentPushToken[x]", "sig": None, "at": None, "active_at": None}}


def run(items, cat, prev=None):
    users = {"u1": dict(USERS["u1"], sig=prev)}
    msgs, stamps = pdp.build_digests(users, items, cat, FACTORS)
    return (msgs[0]["body"] if msgs else None), (stamps[0][1] if stamps else prev)


items = [saved("dress", "Deiji Studios", "Slip Dress", 200, ["S", "M"]),
         saved("top", "Baserange", "Tee", 80, ["S", "M", "L"]),
         saved("coat", "Toteme", "Coat", 900, ["S"])]
day1 = {"dress": live("dress", "Deiji Studios", "Slip Dress", 140, ["S", "M"]),
        "top": live("top", "Baserange", "Tee", 80, ["S", "M", "L"]),
        "coat": live("coat", "Toteme", "Coat", 900, ["S"])}
body, stamp = run(items, day1)
ok(body and "Deiji Studios Slip Dress is 30% off" in body, f"first push wrong: {body}")
ok(stamp.startswith("v2:"), "stamp is not the v2 form")

# Day 2: nothing changed at all.
ok(run(items, day1, stamp)[0] is None, "an unchanged day re-sent")

# Day 3: a size sold out on an UNRELATED piece. This is the reported repeat.
day3 = dict(day1, top=live("top", "Baserange", "Tee", 80, ["S", "L"]))
ok(pdp.signature(pdp.compute_alerts(items, day3, FACTORS)) != pdp.read_stamp(stamp)[0],
   "fixture must change the signature, or it does not reproduce the bug")
ok(run(items, day3, stamp)[0] is None, "a size selling out elsewhere re-sent the same sale")

# Day 4: the same sale, one dollar cheaper (an FX refresh). Still the same sale.
day4 = dict(day3, dress=live("dress", "Deiji Studios", "Slip Dress", 139, ["S", "M"]))
ok(run(items, day4, stamp)[0] is None, "a $1 drift re-sent the same sale")
day4u = dict(day3, dress=live("dress", "Deiji Studios", "Slip Dress", 141, ["S", "M"]))
ok(run(items, day4u, stamp)[0] is None, "a $1 drift upward re-sent the same sale")

# Day 5: it is marked down FURTHER. That is news.
day5 = dict(day3, dress=live("dress", "Deiji Studios", "Slip Dress", 100, ["S", "M"]))
body5, stamp5 = run(items, day5, stamp)
ok(body5 and "is 50% off" in body5, f"a further markdown was not sent: {body5}")

# Day 6: a DIFFERENT piece goes on sale. The push leads with the new one, and does
# not count the sale the user already knows about.
day6 = dict(day5, coat=live("coat", "Toteme", "Coat", 720, ["S"]))
body6, stamp6 = run(items, day6, stamp5)
ok(body6 and body6.startswith("Toteme Coat is 20% off"), f"did not lead with the new sale: {body6}")
ok("more on sale" not in body6, f"counted an already-announced sale as news: {body6}")

# Day 7: a size comes back on the tee. News, even though both sales persist.
day7 = dict(day6, top=live("top", "Baserange", "Tee", 80, ["S", "L", "XL"]))
body7, stamp7 = run(items, day7, stamp6)
ok(body7 and "Baserange Tee is back in XL" in body7, f"a restock was not sent: {body7}")
ok(run(items, day7, stamp7)[0] is None, "the restock repeated the next day")

# Sizes that contain the signature's own separators must not look new every day.
odd = [saved("jean", "EB Denim", "Jean", 250, ["24"])]
cat1 = {"jean": live("jean", "EB Denim", "Jean", 250, ["24", "6-8", "S/M", "1+"])}
b1, s1 = run(odd, cat1)
ok(b1 and "back in" in b1, f"odd sizes: first restock not sent: {b1}")
cat2 = {"jean": live("jean", "EB Denim", "Jean", 250, ["6-8", "S/M", "1+"])}   # 24 sold out
ok(run(odd, cat2, s1)[0] is None, "sizes '6-8', 'S/M', '1+' were re-announced after an unrelated change")

# A stamp written BEFORE this change (the bare signature) still suppresses the repeat.
legacy = pdp.signature(pdp.compute_alerts(items, day1, FACTORS))
ok(not legacy.startswith("v2:"), "legacy fixture is not legacy")
ok(run(items, day1, legacy)[0] is None, "legacy stamp: an unchanged day re-sent")
ok(run(items, day3, legacy)[0] is None, "legacy stamp: the reported repeat still happens")
body_l, stamp_l = run(items, day5, legacy)
ok(body_l and "50% off" in body_l and stamp_l.startswith("v2:"), "legacy stamp: a further markdown must send and upgrade the stamp")

# A corrupt stamp can cost one extra push, never an exception.
ok(pdp.read_stamp("v2:{not json") == ("", {}, set()), "corrupt v2 stamp")
ok(pdp.read_stamp(None) == ("", {}, set()) and pdp.read_stamp("") == ("", {}, set()), "empty stamp")
ok(run(items, day1, "v2:{not json")[0] is not None, "a corrupt stamp must fail open (send), not crash or go silent")

# ── review findings: announce only the NEW part, and record only that ────────
# (a) A sale already announced gains a size: the push is about the RESTOCK.
back = dict(day1, dress=live("dress", "Deiji Studios", "Slip Dress", 140, ["S", "M", "L"]))
body_r, stamp_r = run(items, back, stamp)
ok(body_r and "Deiji Studios Slip Dress is back in L" in body_r and "% off" not in body_r,
   f"a known sale that gained a size re-announced the sale: {body_r}")
# (b) A second restock names only the size that is new.
tee1 = dict(day1, top=live("top", "Baserange", "Tee", 80, ["S", "M", "L", "XL"]))
_b, st1 = run(items, tee1, stamp)
tee2 = dict(day1, top=live("top", "Baserange", "Tee", 80, ["S", "M", "L", "XL", "XXL"]))
body_2, _s = run(items, tee2, st1)
ok(body_2 and "back in XXL" in body_2, f"second restock named an old size: {body_2}")
# (c) fresh_alerts returns copies; the caller's alerts are untouched.
al_in = pdp.compute_alerts(items, back, FACTORS)
snap_in = json.dumps(al_in, sort_keys=True)
pdp.fresh_alerts(al_in, stamp)
ok(json.dumps(al_in, sort_keys=True) == snap_in, "fresh_alerts mutated its input")
# (d) A sale this push did NOT announce keeps the price the user was last told.
#     Told: dress $140. The dress drifts to $135 while the coat's sale is announced.
drift = dict(day1, dress=live("dress", "Deiji Studios", "Slip Dress", 135, ["S", "M"]),
             coat=live("coat", "Toteme", "Coat", 720, ["S"]))
body_d, stamp_d = run(items, drift, stamp)
ok(body_d and body_d.startswith("Toteme Coat is 20% off"), f"drift case: wrong lead: {body_d}")
ok(pdp.read_stamp(stamp_d)[1] == {"dress": 140.0, "coat": 720.0},
   f"the unannounced $135 overwrote the announced $140: {pdp.read_stamp(stamp_d)[1]}")
#     $130 is 7% under the announced $140 (only 3.7% under $135): that is news.
drop = dict(drift, dress=live("dress", "Deiji Studios", "Slip Dress", 130, ["S", "M"]))
body_n, _s = run(items, drop, stamp_d)
ok(body_n and "Deiji Studios Slip Dress is 35% off" in body_n,
   f"a real further drop was hidden by an unannounced drift: {body_n}")
# (e) Ended sales and sizes gone again are pruned from the record.
xl = dict(day1, top=live("top", "Baserange", "Tee", 80, ["S", "M", "L", "XL"]))
_b, st_xl = run(items, xl, None)
ok(pdp.read_stamp(st_xl)[1:] == ({"dress": 140.0}, {"top:+xl"}), "fixture stamp")
coat_only = dict(day1, dress=live("dress", "Deiji Studios", "Slip Dress", 200, ["S", "M"]),
                 coat=live("coat", "Toteme", "Coat", 720, ["S"]))
_b, st_after = run(items, coat_only, st_xl)
ok(pdp.read_stamp(st_after)[1:] == ({"coat": 720.0}, set()),
   f"an ended sale or a gone size stayed in the record: {pdp.read_stamp(st_after)[1:]}")

# The stamp round-trips exactly.
al = pdp.compute_alerts(items, day7, FACTORS)
sig, sales, sizes = pdp.read_stamp(pdp.stamp_value(al))
ok(sig == pdp.signature(al) and sales == {"dress": 100.0, "coat": 720.0} and sizes == {"top:+xl"},
   f"stamp round-trip: {sales} {sizes}")

# The full reported case end to end: a phantom Brooke Callahan save plus daily size
# churn elsewhere produces NO push, on any day.
mine = [saved("brooke-callahan-bow-top-blush", "Brooke Callahan", "Bow Top Blush", 485, ["S", "M"]),
        saved("top", "Baserange", "Tee", 80, ["S", "M", "L"])]
for sizes_today in (["S", "M", "L"], ["S", "L"], ["S"], ["S", "M"]):
    cat = {"brooke-callahan-bow-top-blush": live("brooke-callahan-bow-top-blush", "Brooke Callahan", "Bow Top Blush", 130, ["S", "M"]),
           "top": live("top", "Baserange", "Tee", 80, sizes_today)}
    ok(run(mine, cat, None)[0] is None, f"phantom Brooke Callahan sale pushed (tee sizes {sizes_today})")

print(f"digest-news fixtures: all OK ({checks} checks)")
