#!/usr/bin/env python3
"""Currency / FX regression fixtures — run by CI before every catalog build.

Plain asserts (no pytest dep), same shape as the other gates in this directory.
Every case here encodes a PAST INCIDENT:

  * 2026-07-15  Stine Goya tagged EUR, publishing DKK — 7.4x mispricing.
  * 2026-07-29  live_currency() written to catch exactly that, and wired in to
                PRINT. The FX multiply kept using the annotation, so the run log
                shouted "CURRENCY MISMATCH" every morning while the wrong price
                shipped anyway.
  * 2026-08-06  the bill for that: 8 stores annotated wrong, 422 rows a day out
                by the FX factor — 28natelier's 600 AED skirt published as $600
                instead of $163, Christopher Esber's $2,750 one-piece as $1,815.

Everything below fails LOUDLY and immediately: a wrong price in this feed is not
a rendering bug, it is a false statement about someone else's business, sent to
them.
"""
import json
import pathlib

from build_catalog import normalize, resolve_currency

HERE = pathlib.Path(__file__).resolve().parent
FX = {"USD": 1.0, "AUD": 0.66, "EUR": 1.08, "GBP": 1.27, "AED": 0.27229}

failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(f"  {name}{(': ' + detail) if detail else ''}")


def product(price, handle="thing", title="Silk Thing"):
    """The smallest /products.json shape normalize() will accept."""
    return {
        "title": title, "handle": handle, "product_type": "Dresses",
        "variants": [{"price": str(price), "available": True}],
        "images": [{"src": "https://cdn.example.com/a.jpg"}],
        "published_at": "2026-07-01T00:00:00Z",
    }


# ── 1. a live currency that CONTRADICTS the annotation wins ───────────────────
# The whole defect in one assertion. Sir the Label is annotated USD and serves
# AUD; before 2026-08-06 this printed a warning and priced at fx 1.0 anyway.
cur, fx, verified, notes = resolve_currency(
    {"brand": "Sir the Label", "domain": "sirthelabel.com", "currency": "USD"},
    FX, probe=lambda d: "AUD")
check("live currency overrides the annotation", cur == "AUD", f"got {cur}")
check("live currency selects the LIVE fx rate", fx == 0.66, f"got {fx}")
check("a contradicted annotation is still verified", verified is True)
check("a contradiction is shouted, not swallowed",
      any("CURRENCY CORRECTED" in n for n in notes), f"notes={notes}")

# ...and it reaches the arithmetic. This is the assertion that would have failed
# every day between 2026-07-29 and 2026-08-06.
row = normalize(product(240), "Sir the Label", "sirthelabel.com", fx,
                currency=cur, verified=verified)
check("the live rate is what the price is computed with",
      row["price"] == 158, f"got {row['price']} for 240 AUD, expected 158")

# The opposite direction: a Markets store tagged AUD that really serves USD.
# Getting this backwards is the double-conversion that shipped Christopher
# Esber's $2,750 one-piece as $1,815.
cur2, fx2, ver2, _ = resolve_currency(
    {"brand": "Christopher Esber", "domain": "x.com", "currency": "AUD"},
    FX, probe=lambda d: "USD")
row2 = normalize(product(2750), "Christopher Esber", "x.com", fx2,
                 currency=cur2, verified=ver2)
check("no double-conversion when the store really serves USD",
      row2["price"] == 2750, f"got {row2['price']}")

# An agreeing annotation must not be disturbed.
cur3, fx3, ver3, notes3 = resolve_currency(
    {"brand": "Deiji Studios", "domain": "d.com", "currency": "AUD"},
    FX, probe=lambda d: "AUD")
check("an agreeing annotation is left alone", (cur3, fx3) == ("AUD", 0.66))
check("agreement is silent", notes3 == [], f"notes={notes3}")


# ── 2. an unverified brand cannot reach a published price figure ──────────────
# The probe fails and no probe has EVER succeeded -> unverified.
cur4, fx4, ver4, _ = resolve_currency(
    {"brand": "Notte", "domain": "nottejewelry.com", "currency": "USD"},
    FX, probe=lambda d: None)
check("no live answer and no stamp => unverified", ver4 is False)
row4 = normalize(product(100), "Notte", "nottejewelry.com", fx4,
                 currency=cur4, verified=ver4)
check("an unverified row is STAMPED, not silently published",
      row4.get("currencyUnverified") is True, f"row={row4.get('currencyUnverified')}")

# The probe fails but a real probe confirmed it before -> still verified. A
# transient network failure must not drop a brand out of every price figure.
_, _, ver5, _ = resolve_currency(
    {"brand": "Ganni", "domain": "ganni.com", "currency": "USD",
     "currencyVerified": True}, FX, probe=lambda d: None)
check("a stored verification survives one failed probe", ver5 is True)
row5 = normalize(product(100), "Ganni", "ganni.com", 1.0, currency="USD", verified=ver5)
check("a verified row carries no unverified stamp",
      "currencyUnverified" not in row5)

# A currency with NO FX rate cannot be converted, so it cannot be trusted. The
# old `fx_table.get(cur, 1.0)` treated this as par and would have published AED
# at 3.7x without a word.
cur6, fx6, ver6, notes6 = resolve_currency(
    {"brand": "Somewhere", "domain": "s.com", "currency": "USD"},
    {"USD": 1.0}, probe=lambda d: "AED")
check("an unknown currency is never silently treated as par",
      ver6 is False and fx6 == 1.0)
check("an unknown currency is shouted",
      any("NO FX RATE" in n for n in notes6), f"notes={notes6}")

# And the live roster must actually be verifiable: every brand contributing to a
# published figure has to be stamped one way or the other.
cfg = json.loads((HERE / "brands.json").read_text(encoding="utf-8"))
unstamped = [e["brand"] for e in cfg["brands"] if "currencyVerified" not in e]
check("every brand carries a currencyVerified stamp",
      not unstamped, f"{len(unstamped)} unstamped: {unstamped[:5]}")
# Every currency named in brands.json must have a rate, or its brands price at
# par by accident.
missing_fx = sorted({e.get("currency", "USD") for e in cfg["brands"]}
                    - set(cfg["fx_to_usd"]))
check("every configured currency has an FX rate", not missing_fx, str(missing_fx))


# ── 3. `price` is still a converted USD integer ───────────────────────────────
# The iOS app reads `price` as a plain USD number in ~20 render sites and in the
# budget filter, the price-affinity model and every look total. Adding fields
# beside it is free; changing what it means is a silent production break.
for raw, rate, want in [(240, 0.66, 158), (2750, 1.0, 2750), (600, 0.27229, 163),
                        (192, 1.27, 244), (500, 1.08, 540)]:
    r = normalize(product(raw), "B", "b.com", rate, currency="USD", verified=True)
    check(f"price is the converted integer ({raw}x{rate})",
          isinstance(r["price"], int) and not isinstance(r["price"], bool)
          and r["price"] == want, f"got {r['price']!r}, expected {want}")

r = normalize(product("149.95"), "B", "b.com", 1.0, currency="USD", verified=True)
check("a fractional store price still yields an integer USD price",
      isinstance(r["price"], int) and r["price"] == 150, f"got {r['price']!r}")


# ── 4. priceRaw / currency round-trip ─────────────────────────────────────────
# The two fields that make an FX mistake RECOVERABLE. Storing only the converted
# integer is what made the 2026-08-06 defect permanent: the multiplicand was
# gone and there was nothing left to re-derive from.
for raw, cur_code in [(240.0, "AUD"), (2750.0, "USD"), (600.0, "AED"),
                      (149.95, "EUR"), (192.5, "GBP")]:
    rate = FX[cur_code]
    r = normalize(product(raw), "B", "b.com", rate, currency=cur_code, verified=True)
    check(f"priceRaw is the store's own number ({raw} {cur_code})",
          r["priceRaw"] == round(raw, 2), f"got {r['priceRaw']!r}")
    check(f"currency is the observed code ({cur_code})", r["currency"] == cur_code)
    check(f"price == round(priceRaw * fx) ({raw} {cur_code})",
          r["price"] == round(r["priceRaw"] * FX[r["currency"]]),
          f"{r['price']} != round({r['priceRaw']} * {FX[r['currency']]})")

# The round trip is the point: from the two stored fields alone, a future
# correction is a re-derivation rather than a loss.
r = normalize(product(600), "B", "b.com", 1.0, currency="AED", verified=True)
check("a wrongly-converted row is fully recoverable from priceRaw+currency",
      round(r["priceRaw"] * FX["AED"]) == 163,
      f"got {round(r['priceRaw'] * FX['AED'])}")


# ── 5. the historical correction table is self-terminating and consistent ─────
corr = json.loads((HERE / "price_corrections.json").read_text(encoding="utf-8"))
check("corrections apply only to rows lacking a currency",
      corr.get("appliesToRowsWithout") == "currency")
by_brand = {c["brand"]: c for c in corr["corrections"]}
cfg_by_brand = {e["brand"]: e for e in cfg["brands"]}
for b, c in by_brand.items():
    e = cfg_by_brand.get(b)
    check(f"corrected brand {b} is still in the roster", e is not None)
    if not e:
        continue
    # The table's "right" currency must be what brands.json now says, or the
    # archive would be corrected toward a value the live feed disagrees with.
    check(f"{b}: correction agrees with the corrected annotation",
          e.get("currency") == c["rightCurrency"],
          f"brands.json={e.get('currency')} corrections={c['rightCurrency']}")
    check(f"{b}: factor == rightFx / wrongFx",
          abs(c["factor"] - c["rightFx"] / c["wrongFx"]) < 1e-6,
          f"factor={c['factor']}")
    check(f"{b}: correction window starts on or after the country=US pin",
          c["fromDay"] >= "2026-07-15", f"fromDay={c['fromDay']}")

# Applying a correction to a row that already carries a currency would introduce
# the error a second time, in the opposite direction. Pin the guard itself.
import build_price_history as bph  # noqa: E402

table = bph.load_corrections()
check("the feed's reader loads every correction", len(table) == len(by_brand))
check("a row WITHOUT a currency is corrected",
      bph.correct_price(600, "28natelier", "2026-08-01", False, table) == 163,
      str(bph.correct_price(600, "28natelier", "2026-08-01", False, table)))
check("a row WITH a currency is never corrected twice",
      bph.correct_price(163, "28natelier", "2026-08-01", True, table) == 163)
check("a day before the window is left alone",
      bph.correct_price(600, "28natelier", "2026-07-01", False, table) == 600)
check("an uncorrected brand is left alone",
      bph.correct_price(600, "Deiji Studios", "2026-08-01", False, table) == 600)


# ── 4. an FX correction must never reach a user as a SALE ────────────────────
# 2026-08-06, the second half of the same incident and the more expensive one.
#
# The corrections above fixed the archive. Nothing fixed the ALERT path. eb5a0b3
# landed at 02:54 and the catalog rebuilt at 03:06; two of the eight corrections
# make prices FALL — 28natelier x0.27229 and Sir the Label x0.66 — and
# price_drop_push.py compares the live price against each user's price-at-save
# with a 10% / $3 threshold. Measured on the real snapshots either side of the
# rebuild (bd7582e -> 3d8b7ca): 117 pieces, 60 of 60 for 28natelier and 57 of 57
# for Sir the Label, cleared that threshold. The 17:00 digest would have told
# every holder of one:
#
#     "Price drop in your Dresser — 28natelier The Kufiya Hair Scarf is 73% off"
#
# Nothing was marked down. The analysis path had known about this class of event
# for weeks — PRICE_EPOCHS voids comparisons across a methodology change and
# build_loupe_index.detect_uniform_steps() voids a brand-day whose whole line
# moves by one ratio — and the alert path could not see any of it. These cases
# are that split brain nailed shut, on the side where it reaches a phone.
import io
import tokenize

import price_drop_push as pdp

pdp_table = pdp.load_price_corrections()

# The tell is the ratio, and the record is the source of it. A brand-wide step
# that lands exactly on the published factor is our own arithmetic, not a sale.
check("the digest reads the SHARED record, not its own brand list",
      pdp_table == {c["brand"]: float(c["factor"]) for c in corr["corrections"]},
      f"got {pdp_table}")
# The record must be READ, never restated. A brand named in the digest's
# executable code is a second list that drifts from the archive the first time
# anyone corrects a ninth brand — and the two would then disagree about what a
# user is owed. Comments and docstrings are prose, not a table, so the source is
# stripped of them before the check rather than pattern-matched raw.
_src = pathlib.Path(HERE / "price_drop_push.py").read_text(encoding="utf-8")
_code = "".join(
    tok.string if tok.type not in (tokenize.COMMENT, tokenize.STRING) else " "
    for tok in tokenize.generate_tokens(io.StringIO(_src).readline))
_baked = sorted(b for b in pdp_table if b in _code)
check("no brand from the record is baked into the digest's code",
      not _baked,
      f"{_baked} hardcoded in price_drop_push.py — it will drift from the record")

live = {"p": {"price": 151, "sizes": [], "brand": "28natelier", "name": "Teddy Dress"}}
saved = [{"product_id": "p", "price_at_save": 555, "product": {"id": "p", "sizes": []}}]
check("WITHOUT the record a currency fix is published as a 72% sale",
      pdp.compute_alerts(saved, live)[0]["sale"]["pct"] == 72)
check("WITH the record the same move is not a sale at all",
      pdp.compute_alerts(saved, live, pdp_table) == [],
      str(pdp.compute_alerts(saved, live, pdp_table)))

# ...and the same for the other falling brand, whose step is a plausible-looking
# 34% rather than an obviously-absurd 73%. Size matters less than uniformity.
live_sir = {"q": {"price": 79, "sizes": [], "brand": "Sir the Label", "name": "Calypso"}}
saved_sir = [{"product_id": "q", "price_at_save": 120, "product": {"id": "q", "sizes": []}}]
check("a merely plausible FX step is suppressed too",
      pdp.compute_alerts(saved_sir, live_sir, pdp_table) == [])

# THE CASE THAT MATTERS MOST. The guard must not mute the brand — only the step.
# A real markdown on a corrected brand, on the SAME day, still has to be told.
live_real = {"r": {"price": 90, "sizes": [], "brand": "Sir the Label", "name": "Olea"}}
saved_real = [{"product_id": "r", "price_at_save": 200, "product": {"id": "r", "sizes": []}}]
got_real = pdp.compute_alerts(saved_real, live_real, pdp_table)
check("a genuine markdown on a CORRECTED brand still alerts",
      len(got_real) == 1 and got_real[0]["sale"] is not None,
      f"got {got_real}")

# A single-item markdown on an uncorrected brand is untouched — this is the
# 7 real sales that survived alongside the 117 artefacts on 2026-08-06.
live_other = {"s": {"price": 110, "sizes": [], "brand": "Tyler McGillivary", "name": "Tank"}}
saved_other = [{"product_id": "s", "price_at_save": 275, "product": {"id": "s", "sizes": []}}]
check("an uncorrected brand's markdown is left alone",
      pdp.compute_alerts(saved_other, live_other, pdp_table)[0]["sale"]["pct"] == 60)

# The tolerance is DERIVED, not tuned: both prices are dollar-rounded, so a true
# FX step can miss its factor by at most 0.5*factor + 0.5. Inside that, suppress;
# outside it, the move is real and must be reported. Worst residual measured over
# all 400 corrected pieces was $0.870 against a $1.258 bound.
check("a step one cent inside the rounding bound is FX",
      pdp.is_fx_correction_step(100 * 0.66 + 0.82, 100, 0.66) is True)
check("a step outside the rounding bound is a real move",
      pdp.is_fx_correction_step(100 * 0.66 - 5, 100, 0.66) is False)
check("a brand with no correction is never suppressed",
      pdp.is_fx_correction_step(50, 100, None) is False)

# Restock/sold-out is a different axis and a PRICE guard must not touch it.
live_sz = {"t": {"price": 151, "sizes": ["S", "M"], "brand": "28natelier", "name": "Teddy"}}
saved_sz = [{"product_id": "t", "price_at_save": 555, "product": {"id": "t", "sizes": ["S"]}}]
got_sz = pdp.compute_alerts(saved_sz, live_sz, pdp_table)
check("the FX guard suppresses the phantom sale but keeps the real restock",
      len(got_sz) == 1 and got_sz[0]["sale"] is None and got_sz[0]["new_sizes"] == ["M"],
      f"got {got_sz}")

# A missing record is a REFUSAL to send, never an empty table. A silent {} here
# would send all 117 false pushes to real phones, once, with no way to recall
# them. build_price_history.py takes the same position for the same reason.
try:
    pdp.load_price_corrections(HERE / "price_corrections.__absent__.json")
    check("a missing corrections record stops the send", False, "it returned instead")
except SystemExit:
    check("a missing corrections record stops the send", True)


def main():
    if failures:
        print("CURRENCY/FX REGRESSIONS:")
        print("\n".join(failures))
        raise SystemExit(1)
    print("currency/FX fixtures: all OK")


if __name__ == "__main__":
    main()
