#!/usr/bin/env python3
"""Loupe -- establish the TRUE presentment currency of every storefront we walk.

WHY THIS EXISTS

Shopify's /products.json quotes variant prices in the shop's own presentment
currency and attaches NO currency field. brands.json carries a hand-maintained
per-brand `currency` annotation, and build_catalog.py multiplies by the FX rate
for that annotation. So the annotation IS the price: get it wrong and every
piece that brand ever published is wrong by the FX factor, in a file we hand to
the brand itself.

/cart.js is the shop telling us, live, what currency the current session is
priced in -- the same presentment /products.json?country=US answers in. It is
the only authority available to us, and it settles the three failure modes that
have all actually happened here:

  * a store tagged EUR that really publishes DKK   (Stine Goya, 7.4x HIGH)
  * a Shopify Markets store tagged GBP/EUR that serves USD under country=US, so
    the FX multiply DOUBLE-CONVERTS                (Martine Rose +27%)
  * an untagged store (defaults USD) that really publishes EUR  (SIEDRES, 8% low)

RATE LIMITING IS THE WHOLE DIFFICULTY

Shopify throttles per client, not per shop. A concurrent probe on 2026-08-06 got
HTTP 429 from 151 of 162 stores -- i.e. it looked like it ran, and produced
almost entirely garbage. Sequential requests spaced 2.5s apart got 157 of 162.
Shopify's agents.md asks for exactly one thing: back off on 429. So:

  * one request at a time, SPACING seconds apart, no concurrency ever;
  * on 429, honour Retry-After when present and otherwise back off
    exponentially, and count it -- a run that ate a lot of 429s is a run whose
    negative results mean nothing;
  * a domain that never answers is recorded as UNKNOWN and NEVER guessed at.
    Guessing is what created this bug.

USAGE
    python probe_currency.py                 # probe, write currency_probe.json
    python probe_currency.py --apply         # ...and correct brands.json
    python probe_currency.py --limit 10      # short run for testing
"""

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
BRANDS = HERE / "brands.json"
OUT = HERE / "currency_probe.json"

# Measured: 2.5s spacing answered 157/162; concurrent answered 11/162.
SPACING = 2.5
TIMEOUT = 20
MAX_429_BACKOFF = 120
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CUR_RE = re.compile(r"^[A-Z]{3}$")

_last = [0.0]


def _pace():
    wait = SPACING - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


def _get(url):
    """Return (json_or_None, http_status_or_None). Never raises."""
    _pace()
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), resp.status
    except urllib.error.HTTPError as e:
        retry_after = e.headers.get("Retry-After") if e.headers else None
        return {"_retry_after": retry_after}, e.code
    except Exception:  # noqa: BLE001 -- a probe must never be fatal
        return None, None


def probe_domain(domain, log=None):
    """The live presentment currency for a US shopper, or None.

    Tries the bare host then www. Backs off on 429 as Shopify asks. Returns
    (currency|None, detail) where detail records what actually happened, so a
    'no answer' can be told apart from 'answered, and it is USD'.
    """
    hosts = (domain,) if domain.startswith("www.") else (domain, "www." + domain)
    detail = {"attempts": 0, "throttled": 0, "lastStatus": None}
    backoff = 10
    for host in hosts:
        for attempt in range(3):
            detail["attempts"] += 1
            data, status = _get(f"https://{host}/cart.js?country=US")
            detail["lastStatus"] = status
            if status == 429:
                detail["throttled"] += 1
                ra = (data or {}).get("_retry_after")
                try:
                    wait = min(float(ra), MAX_429_BACKOFF) if ra else backoff
                except (TypeError, ValueError):
                    wait = backoff
                if log:
                    log(f"    429 from {host}; backing off {wait:.0f}s")
                time.sleep(wait)
                backoff = min(backoff * 2, MAX_429_BACKOFF)
                continue
            cur = (data or {}).get("currency")
            if isinstance(cur, str) and CUR_RE.match(cur.strip().upper()):
                detail["host"] = host
                return cur.strip().upper(), detail
            if status is not None and status != 429:
                break  # answered, but not usefully -- try www, do not hammer
    return None, detail


def roster(cfg):
    """Every domain whose prices reach a published figure: brands + retailers."""
    rows = [{"kind": "brand", "label": e["brand"], "domain": e["domain"],
             "configured": e.get("currency") or "USD",
             "tagged": bool(e.get("currency"))} for e in cfg["brands"]]
    rows += [{"kind": "retailer", "label": r.get("name") or r.get("id"),
              "domain": r["domain"], "configured": r.get("currency") or "USD",
              "tagged": bool(r.get("currency"))}
             for r in cfg.get("retailers", []) if r.get("domain")]
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the corrected currencies back into brands.json")
    ap.add_argument("--limit", type=int, default=0, help="probe only the first N")
    args = ap.parse_args()

    cfg = json.loads(BRANDS.read_text(encoding="utf-8"))
    rows = roster(cfg)
    if args.limit:
        rows = rows[:args.limit]

    logf = HERE / "currency_probe.log"
    fh = logf.open("w", encoding="utf-8")

    def log(msg):
        print(msg, file=fh, flush=True)
        try:
            print(msg, file=sys.stderr, flush=True)
        except UnicodeEncodeError:
            print(msg.encode("ascii", "replace").decode(), file=sys.stderr, flush=True)

    started = time.time()
    log(f"probing {len(rows)} domains at {SPACING}s spacing "
        f"(~{len(rows) * SPACING / 60:.0f} min minimum)")

    results = {}
    for i, r in enumerate(rows, 1):
        cur, detail = probe_domain(r["domain"], log=log)
        agree = (cur == r["configured"]) if cur else None
        results[r["domain"]] = {
            "label": r["label"], "kind": r["kind"], "configured": r["configured"],
            "tagged": r["tagged"], "live": cur, "agrees": agree, **detail,
        }
        mark = "ok " if agree else ("MISMATCH" if cur else "no answer")
        log(f"[{i:>3}/{len(rows)}] {r['label'][:26]:<28} "
            f"cfg={r['configured']:<4} live={cur or '-':<4} {mark}")
        # Write incrementally: a run this long must survive being interrupted.
        OUT.write_text(json.dumps({
            "probedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "spacingSeconds": SPACING, "total": len(rows), "done": i,
            "results": results,
        }, indent=1, ensure_ascii=False), encoding="utf-8")

    answered = [v for v in results.values() if v["live"]]
    mism = [v for v in results.values() if v["agrees"] is False]
    thr = sum(v["throttled"] for v in results.values())
    log(f"\ndone in {time.time() - started:.0f}s: {len(answered)}/{len(rows)} answered, "
        f"{len(mism)} MISMATCH, {len(rows) - len(answered)} unknown, {thr} 429s")
    if thr > len(rows) * 0.25:
        log("!! more than a quarter of requests were throttled -- negative "
            "results from this run are NOT trustworthy. Re-run before applying.")

    if args.apply:
        apply_to_brands(cfg, results, log)
    fh.close()


def apply_to_brands(cfg, results, log):
    """Correct brands.json from the probe. Never guesses: a domain that did not
    answer keeps its existing annotation and is stamped currencyVerified=false,
    which is what keeps it out of every published price figure downstream."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    changed = 0
    unverified = []
    for entry in cfg["brands"] + [r for r in cfg.get("retailers", []) if r.get("domain")]:
        res = results.get(entry.get("domain"))
        if not res:
            continue
        if res["live"]:
            if res["live"] != (entry.get("currency") or "USD"):
                log(f"  CORRECTED {entry.get('brand') or entry.get('name')}: "
                    f"{entry.get('currency') or 'USD (untagged)'} -> {res['live']}")
                changed += 1
            entry["currency"] = res["live"]
            entry["currencyVerified"] = True
            entry["currencyCheckedAt"] = stamp
        else:
            entry["currencyVerified"] = False
            entry["currencyCheckedAt"] = stamp
            unverified.append(entry.get("brand") or entry.get("name"))
    BRANDS.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    log(f"\nbrands.json: {changed} annotations corrected, "
        f"{len(unverified)} left UNVERIFIED: {', '.join(map(str, unverified)) or '-'}")


if __name__ == "__main__":
    main()
