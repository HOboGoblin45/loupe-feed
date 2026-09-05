#!/usr/bin/env python3
"""Refresh brands.json's `fx_to_usd` from a live mid-market source — AS AN EPOCH.

WHY THIS EXISTS (2026-09-05)

`fx_to_usd` was a hand-typed table. Every rate in it except the pegs had drifted:
measured against ECB mid-market for 2026-09-04, SEK was 13.8% stale, AUD 9.2%,
EUR 7.6%, DKK 7.2%, GBP 6.5%. 17 of 185 brands price through that table, so a
stale rate is not a rounding error — it is a wrong price in the file we hand to
the brand it is about, exactly like a wrong `currency` annotation, only quieter
because nothing ever shouts about it.

THE THING THAT MAKES THIS DANGEROUS, AND THE WHOLE REASON FOR THIS SCRIPT

Changing a rate moves EVERY price of EVERY brand in that currency, on one day, by
one identical ratio. That is indistinguishable, in the archive, from the brand
running a sitewide sale — and it is the same event class as 2026-08-06, which got
one afternoon away from pushing "28natelier is 73% off" to real phones. So a rate
change is a PRICE EPOCH, and three separate readers have to see it as one:

  1. THE ARCHIVE   build_price_history.PRICE_EPOCHS — a comparison that straddles
                   an epoch is voided, so no card can claim a drop across it.
  2. THE INDEX     loupe-site/tools/build_loupe_index.py PRICE_EPOCHS (a verbatim
                   copy of the same list) plus detect_uniform_steps(), which
                   independently voids a brand-day whose whole line moves by one
                   ratio. The Index therefore self-protects for brands with >= 12
                   tracked pieces; the epoch is what protects the rest.
  3. THE DIGEST    price_drop_push.py, which suppresses a move that lands on the
                   factor published in price_corrections.json. It reads ONLY
                   {brand: factor} from `corrections`, ignores days entirely, and
                   fires only on price FALLS.

Both PRICE_EPOCHS lists are module constants in files this script does not own,
so the epoch is RECORDED here (price_corrections.json -> `fxEpochs`) and the exact
source line for each registry is PRINTED. `--write-epoch-registry` will also make
the one-line insert into build_price_history.py, verified by re-parsing the file.
The loupe-site copy lives in another repository and is always a manual paste.

WHAT IT WILL NOT DO

  * It will not move a PEGGED rate. AED has been 3.6725/USD since 1997; if the
    live quote departs from the peg by more than PEG_TOLERANCE_PCT the script
    keeps the pin and SHOUTS. A broken currency peg is a human's decision, not a
    cron job's.
  * It will not add a currency the table does not already carry. A rate nothing
    prices through is a number nobody checks.
  * It will not write a second `corrections` entry for a brand that already has
    one. That table is keyed by brand in every reader (load_corrections(),
    load_price_corrections(), load_fx_corrections()), so a duplicate does not
    add a rule — it REPLACES the archive repair for that brand, silently.
  * It will not commit a refresh nobody would notice: below REFRESH_PCT the table
    is left alone, so the archive does not collect epochs for 0.4% of nothing.

USAGE
    python refresh_fx.py                      # fetch, compare, print. Writes nothing.
    python refresh_fx.py --apply              # ...and write, if any rate moved >= 3%
    python refresh_fx.py --apply --force      # ...write regardless of the threshold
    python refresh_fx.py --apply --write-epoch-registry
                                              # ...and insert the day into
                                              #    build_price_history.PRICE_EPOCHS
    python refresh_fx.py --rates-file r.json  # offline: use a saved rate set
"""

import argparse
import ast
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
BRANDS = HERE / "brands.json"
CORRECTIONS = HERE / "price_corrections.json"
HISTORY = HERE / "build_price_history.py"

# ── sources ──────────────────────────────────────────────────────────────────
# Keyless, no account, no rate limit worth the name — both usable straight from a
# CI runner. Frankfurter is the ECB's own daily reference set, which is where the
# rates in this table were quoted from by hand in the first place (see brands.json
# `_fx_comment`: "ECB mid-market for 2026-08-05"), so it is the primary and the
# table keeps one provenance. It publishes ~30 majors and NOT the pegs we carry,
# so open.er-api.com fills the gaps (AED, EGP) and is the fallback if the ECB set
# is unreachable.
PRIMARY = "https://api.frankfurter.app/latest?from=USD"
FALLBACK = "https://open.er-api.com/v6/latest/USD"
TIMEOUT = 20
USER_AGENT = "LoupeFXRefresh/1.0 (+https://useloupe.shop)"

# Hard pegs, in UNITS PER USD. Kept here rather than fetched because that is what
# a peg IS: a published policy, not a market observation.
PEGS = {"AED": 3.6725}          # UAE central bank, unchanged since 1997
PEG_TOLERANCE_PCT = 0.5

# Below this, a refresh is churn: it rewrites the table, adds an epoch and moves
# no price anyone can perceive.
REFRESH_PCT = 3.0

# Lifted from build_price_history.MIN_MEANINGFUL_MOVE. A brand whose prices move
# less than this has not, as far as anything downstream is concerned, moved — so
# no epoch is declared for it.
MIN_MEANINGFUL_MOVE_PCT = 2.0

# The digest only ever fires on a FALL of >= PRICE_DROP_MIN_PCT (10%). A currency
# that falls at least that far would push its whole shelf out as a sale, so THAT
# is the case that needs an entry in `corrections`, and only that case.
DIGEST_FALL_FACTOR = 0.90

ROUND_DP = 6


def _get(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_rates(codes, get=_get):
    """({code: usd_per_unit}, provenance) for the codes asked for.

    Providers quote UNITS PER USD; the table stores USD PER UNIT, which is the
    multiplicand build_catalog.py applies. The inversion happens exactly once,
    here, so no caller can get it backwards.
    """
    wanted = {c for c in codes if c != "USD"}
    out, src = {}, {}
    prov = {"asOf": None, "sources": {}, "errors": []}

    for name, url, key in (("frankfurter(ECB)", PRIMARY, "date"),
                           ("open.er-api.com", FALLBACK, "time_last_update_utc")):
        missing = wanted - set(out)
        if not missing:
            break
        try:
            doc = get(url)
        except Exception as e:                                   # noqa: BLE001
            prov["errors"].append(f"{name}: {e}")
            continue
        rates = (doc or {}).get("rates") or {}
        got = []
        for code in sorted(missing):
            v = rates.get(code)
            if isinstance(v, (int, float)) and v > 0:
                out[code] = 1.0 / float(v)
                src[code] = name
                got.append(code)
        if got:
            prov["sources"][name] = {"url": url, "asOf": (doc or {}).get(key),
                                     "codes": got}
            prov["asOf"] = prov["asOf"] or (doc or {}).get(key)

    prov["perCode"] = src
    still_missing = sorted(wanted - set(out))
    if still_missing:
        prov["errors"].append(f"no quote for {', '.join(still_missing)}")
    return out, prov


def apply_pegs(live, notes):
    """Pin the pegged currencies, and shout if the market disagrees with the peg."""
    for code, units in PEGS.items():
        pinned = 1.0 / units
        seen = live.get(code)
        if seen:
            drift = abs(seen - pinned) / pinned * 100
            if drift > PEG_TOLERANCE_PCT:
                notes.append(
                    f"  !! {code} QUOTED OFF ITS PEG by {drift:.2f}% "
                    f"({1/seen:.4f} vs {units} per USD). The pin is KEPT. A broken "
                    f"peg re-prices a whole shelf and is not a cron job's call — "
                    f"verify it, then edit PEGS in refresh_fx.py deliberately.")
        live[code] = pinned
    return live


def compare_table(old, live):
    """[{code, old, new, pct}] for every code ALREADY in the table, biggest first.

    Codes the providers could not answer for keep their existing value and are
    reported with new=None: a currency we cannot price today is not a currency
    worth 1.0, which is the mistake resolve_currency() exists to refuse.
    """
    rows = []
    for code, cur in old.items():
        if code == "USD":
            continue
        new = live.get(code)
        new = round(new, ROUND_DP) if new is not None else None
        pct = None if new is None or not cur else (new - cur) / cur * 100
        rows.append({"code": code, "old": cur, "new": new,
                     "pct": None if pct is None else round(pct, 2)})
    rows.sort(key=lambda r: (-abs(r["pct"]) if r["pct"] is not None else 1, r["code"]))
    return rows


def brand_impact(cfg, rows):
    """{brand: {currency, factor, pct}} for every brand a move actually re-prices."""
    moved = {r["code"]: r for r in rows
             if r["new"] is not None and r["pct"] not in (None, 0)}
    out = {}
    for e in cfg.get("brands", []):
        code = (e.get("currency") or "USD").upper()
        r = moved.get(code)
        if not r or not r["old"]:
            continue
        out[e["brand"]] = {"currency": code,
                           "factor": round(r["new"] / r["old"], 6),
                           "pct": r["pct"]}
    for r_ in cfg.get("retailers", []):
        code = (r_.get("currency") or "USD").upper()
        r = moved.get(code)
        if r and r["old"]:
            out[r_.get("name") or r_.get("id")] = {
                "currency": code, "factor": round(r["new"] / r["old"], 6),
                "pct": r["pct"]}
    return out


def source_urls(prov):
    """{name: url} however the provenance arrived — fetched live (nested dicts) or
    replayed from a --rates-file (already flat)."""
    return {k: (v.get("url") if isinstance(v, dict) else v)
            for k, v in (prov.get("sources") or {}).items()}


def epoch_record(day, rows, impact, prov):
    """The published statement of what WE changed — the shape the digest, the
    archive and the Index all reason about: a per-brand RATIO with a date."""
    changed = [r for r in rows if r["new"] is not None and r["old"] != r["new"]]
    return {
        "day": day,
        "kind": "fx-table-refresh",
        "asOf": prov.get("asOf"),
        "sources": source_urls(prov),
        "maxAbsPct": max((abs(r["pct"]) for r in changed if r["pct"] is not None),
                         default=0.0),
        "rates": {r["code"]: {"from": r["old"], "to": r["new"], "pct": r["pct"]}
                  for r in changed},
        "brands": {b: v["factor"] for b, v in sorted(impact.items())},
        "note": ("Scheduled FX refresh by refresh_fx.py. Every price in these "
                 "currencies steps by the brand ratio above on this day. It is "
                 "our own arithmetic, never a markdown: void price comparisons "
                 "across it (PRICE_EPOCHS) and never report it as a sale."),
    }


def digest_guard_entries(day, rows, impact, existing_brands):
    """`corrections` rows for brands whose prices FALL far enough to be pushed as
    a sale, plus the brands that need one and cannot safely be given one.

    A rise cannot become a false "price drop in your Dresser", so it needs no
    entry — and an entry it does not need is a permanent suppression of that
    brand's real markdowns, which costs a user the one true thing we may say.
    """
    by_code = {r["code"]: r for r in rows}
    entries, blocked = [], []
    for brand, v in sorted(impact.items()):
        if v["factor"] > DIGEST_FALL_FACTOR:
            continue
        r = by_code[v["currency"]]
        if brand in existing_brands:
            blocked.append(brand)
            continue
        entries.append({
            "brand": brand,
            "wrongCurrency": v["currency"], "wrongFx": r["old"],
            "rightCurrency": v["currency"], "rightFx": r["new"],
            "factor": v["factor"],
            "fromDay": day, "toDay": None,
            "note": (f"FX TABLE REFRESH {day}, not a markdown: {v['currency']} "
                     f"{r['old']} -> {r['new']} ({r['pct']:+.2f}%). Recorded so "
                     f"price_drop_push.py suppresses the step; the archive path "
                     f"ignores it, because every row since 2026-08-06 carries its "
                     f"own `currency` and is never corrected twice."),
        })
    return entries, blocked


# ── writing ──────────────────────────────────────────────────────────────────

def write_brands(cfg, table, prov, day, path=BRANDS):
    """brands.json with the new table and a machine-owned provenance stamp.

    Rebuilt through json rather than patched as text: verified byte-stable at
    indent=2 for this file, so the diff is exactly the values that changed.
    """
    out = {}
    for k, v in cfg.items():
        if k == "fx_to_usd":
            out["_fx_refreshed"] = {
                "tool": "refresh_fx.py", "day": day, "asOf": prov.get("asOf"),
                "sources": source_urls(prov),
                "pegged": sorted(PEGS),
                "note": ("Machine-written. Do not hand-edit a rate: a rate change "
                         "re-prices every brand in that currency on one day and has "
                         "to be published as an epoch (see price_corrections.json "
                         "`fxEpochs`). Run refresh_fx.py instead."),
            }
            out[k] = table
        elif k != "_fx_refreshed":
            out[k] = v
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def splice_into_array(raw, key, objects):
    """Insert `objects` at the end of the top-level JSON array `key`, leaving every
    existing byte of the file alone.

    price_corrections.json is a hand-formatted human document (grouped keys, long
    prose) that a json.dump round-trip would reflow from end to end. The array
    bounds are found with the real parser, never a regex, and the result is
    re-parsed before it is returned — a corrupted record here is a hard stop in
    both build_price_history.py and price_drop_push.py.
    """
    if not objects:
        return raw
    marker = f'"{key}":'
    i = raw.index(marker)
    j = raw.index("[", i + len(marker))
    arr, end = json.JSONDecoder().raw_decode(raw, j)
    if not isinstance(arr, list):
        raise ValueError(f"{key} is not an array")
    body = ",\n".join(
        "\n".join("    " + ln for ln in json.dumps(o, indent=2, ensure_ascii=False)
                  .splitlines())
        for o in objects)
    if arr:
        head = raw[:end - 1].rstrip()          # ...ends on the last '}' in the array
        out = head + ",\n" + body + "\n  " + raw[end - 1:]
    else:
        out = raw[:j] + "[\n" + body + "\n  " + raw[end - 1:]
    doc = json.loads(out)                      # refuses to hand back a broken record
    if len(doc[key]) != len(arr) + len(objects):
        raise ValueError(f"splice into {key} did not land")
    return out


def insert_price_epoch(src, day):
    """build_price_history.py source with `day` added to PRICE_EPOCHS.

    Text insert, then re-parsed with ast and compared against the intended list.
    This file gates the daily catalog build (test_price_history.py runs before
    the scrape), so a malformed edit here would cost days of the archive — the
    2026-07-25 failure mode exactly. Returns None if the file already has the day
    or if anything at all is not as expected.
    """
    tree = ast.parse(src)
    node = next((n for n in tree.body
                 if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == "PRICE_EPOCHS" for t in n.targets)),
                None)
    if node is None or not isinstance(node.value, ast.List):
        return None
    current = [el.value for el in node.value.elts
               if isinstance(el, ast.Constant) and isinstance(el.value, str)]
    if len(current) != len(node.value.elts) or day in current:
        return None
    last = node.value.elts[-1]
    line = src.splitlines(keepends=True)
    # Insert a new element on its own line, after the line the last element ends on.
    at = sum(len(x) for x in line[:last.end_lineno])
    indent = " " * (node.value.elts[0].col_offset)
    new = (f'{indent}"{day}",  # fx-refresh — fx_to_usd re-fetched; every brand in a '
           f'moved currency steps by one ratio on this day\n')
    out = src[:at] + new + src[at:]
    check = next((n for n in ast.parse(out).body
                  if isinstance(n, ast.Assign)
                  and any(getattr(t, "id", None) == "PRICE_EPOCHS" for t in n.targets)),
                 None)
    got = [el.value for el in check.value.elts] if check else None
    if got != sorted(current + [day]):
        return None
    return out


def registry_lines(day):
    """The two source edits this script cannot make for itself."""
    return [
        ("C:\\loupe-feed\\loupe-feed\\build_price_history.py  (PRICE_EPOCHS)",
         f'    "{day}",  # fx-refresh — fx_to_usd re-fetched; every brand in a '
         f'moved currency steps by one ratio on this day'),
        ("C:\\loupe-site\\tools\\build_loupe_index.py  (PRICE_EPOCHS — verbatim copy)",
         f'PRICE_EPOCHS = ["2026-07-15", "{day}"]'),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the files")
    ap.add_argument("--force", action="store_true",
                    help=f"apply even when nothing moved {REFRESH_PCT}%%")
    ap.add_argument("--write-epoch-registry", action="store_true",
                    help="also insert the day into build_price_history.PRICE_EPOCHS")
    ap.add_argument("--rates-file", help="offline rate set: {\"usdPerUnit\": {...}}")
    ap.add_argument("--day", help="override today's date (tests)")
    args = ap.parse_args()

    cfg = json.loads(BRANDS.read_text(encoding="utf-8"))
    old = cfg["fx_to_usd"]
    day = args.day or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    if args.rates_file:
        # utf-8-sig, not utf-8: a rate set saved from PowerShell carries a BOM,
        # and this path exists to be used by hand on exactly that machine.
        doc = json.loads(pathlib.Path(args.rates_file).read_text(encoding="utf-8-sig"))
        live = {k: float(v) for k, v in doc["usdPerUnit"].items()}
        prov = {"asOf": doc.get("asOf"), "sources": doc.get("sources", {}),
                "errors": [], "perCode": {}}
    else:
        live, prov = fetch_rates(old.keys())

    notes = list(prov.get("errors", []))
    live = apply_pegs(live, notes)
    rows = compare_table(old, live)
    impact = brand_impact(cfg, rows)
    biggest = max((abs(r["pct"]) for r in rows if r["pct"] is not None), default=0.0)
    due = biggest >= REFRESH_PCT

    print(f"FX table vs {prov.get('asOf') or 'live'} "
          f"({', '.join(prov.get('sources', {})) or args.rates_file}):")
    for r in rows:
        if r["new"] is None:
            print(f"  {r['code']}  {r['old']:<10} -> (no quote — left alone)")
        else:
            print(f"  {r['code']}  {r['old']:<10} -> {r['new']:<10} "
                  f"{r['pct']:+.2f}%{'  <-- stale' if abs(r['pct']) >= REFRESH_PCT else ''}")
    for n in notes:
        print(n)
    print(f"\n  biggest move {biggest:.2f}%  (threshold {REFRESH_PCT}%) -> "
          f"{'REFRESH DUE' if due else 'no refresh needed'}")
    print(f"  brands re-priced: {len(impact)}")
    for b, v in sorted(impact.items(), key=lambda x: -abs(x[1]['pct'])):
        print(f"    {b:<22} {v['currency']}  x{v['factor']:.6f}  {v['pct']:+.2f}%")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"due={'true' if due else 'false'}\n")
            fh.write(f"biggest={biggest:.2f}\n")
            fh.write(f"brands={len(impact)}\n")

    if not args.apply:
        print("\n(dry run — nothing written. Add --apply.)")
        return 0
    if not due and not args.force:
        print("\nNothing to do: no rate moved enough to be worth an epoch.")
        return 0
    if any(r["new"] is None for r in rows):
        print("\nNOTE: at least one currency had no quote and keeps its old rate.")

    # An epoch is only declared when a BRAND actually moves. A table refresh that
    # re-prices nobody is bookkeeping, and an epoch nothing can perceive would
    # void real price history for no reason.
    perceptible = {b: v for b, v in impact.items()
                   if abs(v["pct"]) >= MIN_MEANINGFUL_MOVE_PCT}
    table = dict(old)
    for r in rows:
        if r["new"] is not None:
            table[r["code"]] = r["new"]
    table["USD"] = 1.0

    write_brands(cfg, table, prov, day)
    print(f"\nwrote {BRANDS.name}: "
          f"{sum(1 for r in rows if r['new'] is not None and r['new'] != r['old'])} rates")

    if perceptible:
        raw = CORRECTIONS.read_text(encoding="utf-8")
        doc = json.loads(raw)
        existing = {c["brand"] for c in doc.get("corrections", [])}
        guards, blocked = digest_guard_entries(day, rows, perceptible, existing)
        raw = splice_into_array(raw, "fxEpochs",
                                [epoch_record(day, rows, perceptible, prov)])
        raw = splice_into_array(raw, "corrections", guards)
        CORRECTIONS.write_text(raw, encoding="utf-8")
        print(f"wrote {CORRECTIONS.name}: 1 fxEpoch, {len(guards)} digest guard(s)")
        for b in blocked:
            print(f"  !! {b} FALLS >= {round((1-DIGEST_FALL_FACTOR)*100)}% and ALREADY "
                  f"has a `corrections` entry. NOT written: every reader keys that "
                  f"table by brand, so a second row would replace the archive repair "
                  f"rather than add to it. Resolve by hand before the next digest.")
    else:
        print("no brand moved perceptibly — no epoch declared")

    if args.write_epoch_registry and perceptible:
        src = HISTORY.read_text(encoding="utf-8")
        out = insert_price_epoch(src, day)
        if out:
            HISTORY.write_text(out, encoding="utf-8")
            print(f"wrote {HISTORY.name}: PRICE_EPOCHS += {day}")
        else:
            print(f"!! could not insert {day} into {HISTORY.name} PRICE_EPOCHS "
                  f"(already present, or the file is not in the expected shape). "
                  f"Add it by hand — see below.")

    if perceptible:
        print("\nEPOCH REGISTRIES — add this day to BOTH lists or the step reads as a sale:")
        for where, line in registry_lines(day):
            print(f"\n  {where}\n    {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
