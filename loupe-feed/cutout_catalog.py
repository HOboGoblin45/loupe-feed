#!/usr/bin/env python3
"""
Loupe — product image cutouts (powers the Look Builder's premium collage).

Runs rembg (U²-Net, open-source, CPU) over each product's hero image to produce an
alpha-matted transparent WebP, trims the transparent margin, and records the piece's
content aspect ratio. Output is hosted on the orphan `cutouts` branch (jsDelivr-served,
same free-CDN model as catalog.json / embeddings.json) so `main` stays lean; a small
manifest `cutouts.json` maps productId -> {aspect,status}. build_catalog.py fetches the
manifest and stamps every catalog product with a `cutoutUrl` when ready.

INCREMENTAL + CACHED: skips any product already in the manifest, caps `--limit` new
cutouts per run, so the ~8k-item backfill completes over several dispatched runs and
new products are cheap thereafter. Cutouts are DATA — re-run to improve, no app update.

Operates entirely inside a working dir (the checked-out `cutouts` branch):
    <dir>/cutouts.json        manifest {generatedAt,count,items:{id:{aspect,status}}}
    <dir>/img/<id>.webp       the alpha-matted cutouts

Usage: python cutout_catalog.py --dir cutrepo --limit 500

v2 (2026-09-23) — GARMENTS, NOT MODELS. u2net is a salient-object model, so on an
on-model photo it cut out the MODEL: in a hand-labelled sample 16 of 28 cutouts were a
whole person (a bag came out as a woman carrying it; a necklace as a clump of hair).
Cutting now goes through garment_cutout.cut_product(), which keeps u2net for product
shots, finds a clean same-colourway gallery shot when the hero is on-model, otherwise
lifts only the garment-class pixels off the model, and falls back to a framed tile
rather than ever shipping a person. On the same sample: 0 people, 0 clean cutouts
damaged, 25/28 usable.

A RECHECK pass works through cutouts made by v1 (manifest entries without "v": 2):
each is checked for a person ON DISK (no network); clean ones are just marked v2,
person-shaped ones are re-cut. --priority-ids lets a caller put the products people
actually have in their Dressers first, since those are the only ones the Look
Builder tray ever shows. A wall-clock --budget-min makes every run stop and COMMIT
before the job timeout, whatever the models cost on the runner.

Accessories get two extra rules: earrings and shoes photographed as a pair pass the
product-shot gate (two equal pieces is normal, not a failed cut), and backdrop that
u2net fills in INSIDE a necklace, bracelet, hoop or bag handle is cut back out - on
new cuts, and on person-free v1 cutouts that look filled in (re-cut from their clean
hero, or kept as they are if the hero is on-model).
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import garment_cutout as gc

PIPELINE_V = 2

# The catalog is the source of truth for which products exist. Read it fresh from
# raw GitHub (uncached) rather than the jsDelivr CDN, so a just-refreshed catalog's
# new products are cut out on the very next run.
CATALOG_URL = "https://raw.githubusercontent.com/HOboGoblin45/loupe-feed/main/loupe-feed/catalog.json"
UA = "Mozilla/5.0 (compatible; LoupeCutout/1.0)"
TIMEOUT = 20
MAX_W = 800          # single retina-adequate size; WebP w/ alpha ~20-40KB each


def log(*a):
    print(*a, flush=True)


def fetch_catalog():
    req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8")).get("products", [])


def encode(rgba):
    """Trimmed RGBA -> (webp_bytes, aspect) at the single retina-adequate width."""
    w, h = rgba.size
    if w < 8 or h < 8:
        return None, None
    aspect = round(w / h, 4)
    if w > MAX_W:
        rgba = rgba.resize((MAX_W, round(h * MAX_W / w)), Image.LANCZOS)
    buf = io.BytesIO()
    rgba.save(buf, format="WEBP", quality=85, method=6)
    return buf.getvalue(), aspect


def cut_v2(product, existing_rgba=None):
    """-> (webp_or_None, aspect_or_None, status, mode). webp is None for keep-existing."""
    rgba, rep = gc.cut_product(product, check_existing=existing_rgba is not None,
                               existing_rgba=existing_rgba)
    mode = rep.get("mode", "fallback")
    if rep.get("status") != "ready":
        return None, None, "fallback", mode
    if rgba is None:                     # keep-existing: the file on disk is already right
        return None, None, "ready", mode
    webp, aspect = encode(rgba)
    if webp is None:
        return None, None, "fallback", "too-small"
    return webp, aspect, "ready", mode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="working dir = checked-out cutouts branch")
    ap.add_argument("--limit", type=int, default=500, help="max NEW cutouts this run")
    ap.add_argument("--recheck-limit", type=int, default=300,
                    help="max v1 cutouts to re-examine this run (see RECHECK in the header)")
    ap.add_argument("--priority-ids", default="",
                    help="file of product ids (one per line) to recheck first, e.g. Dresser items")
    ap.add_argument("--budget-min", type=float, default=150,
                    help="stop and commit after this many minutes, whatever is left")
    ap.add_argument("--catalog", default="", help="local catalog.json instead of fetching it")
    args = ap.parse_args()

    root = Path(args.dir)
    img_dir = root / "img"
    img_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "cutouts.json"

    manifest = {"items": {}}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            manifest = {"items": {}}
    items = manifest.setdefault("items", {})

    products = (json.loads(Path(args.catalog).read_text(encoding="utf-8")).get("products", [])
                if args.catalog else fetch_catalog())
    log(f"catalog: {len(products)} products · manifest already has {len(items)}")

    changed = False

    # Prune manifest entries for products that left the catalog — but GUARD it:
    # a truncated/partial catalog fetch must never trigger a mass-deletion that the
    # workflow then commits (wiping most cutouts). Only prune when the fetch looks
    # complete and the removal is a small fraction of the manifest.
    live_ids = {p.get("id") for p in products if p.get("id")}
    gone_ids = [pid for pid in items if pid not in live_ids]
    if gone_ids and len(products) >= 1000 and len(gone_ids) <= max(50, int(len(items) * 0.3)):
        for gone in gone_ids:
            items.pop(gone, None)
            f = img_dir / f"{gone}.webp"
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
        changed = True
        log(f"pruned {len(gone_ids)} delisted from manifest")
    elif gone_ids:
        log(f"SKIP prune ({len(gone_ids)} would-remove, catalog={len(products)}) — looks partial, not committing deletions")

    todo = [p for p in products if p.get("id") and p.get("imageUrl") and p["id"] not in items]
    log(f"to cut this run: {min(len(todo), args.limit)} of {len(todo)} remaining")
    t0 = time.time()
    budget_s = args.budget_min * 60
    done = ok = fb = fail = attempts = 0
    # Bound total attempts so a run of dead image URLs can't walk the whole catalog
    # and blow the CI time budget (each fetch can hang to TIMEOUT). A run either
    # reaches `limit` successes or gives up after limit*2 attempts, then COMMITS.
    attempt_cap = args.limit * 2
    for p in todo:
        if done >= args.limit or attempts >= attempt_cap or time.time() - t0 > budget_s:
            break
        attempts += 1
        pid = p["id"]
        try:
            webp, aspect, status, mode = cut_v2(p)
        except Exception as e:
            log(f"  cut failed {pid}: {type(e).__name__}")
            fail += 1
            continue
        done += 1
        # FILENAME CONTRACT (do not change without changing build_catalog.py):
        # the cutout is stored under the product's RAW id — accents and all
        # (pärlemor-…, démodémodé-…) — and the manifest is keyed by that same raw
        # id. build_catalog.py percent-encodes the id when it builds cutoutUrl
        # (urllib.parse.quote(id, safe='')), which is the correct URL for exactly
        # this file. Encoding the name on DISK too would double-encode the URL and
        # 404 every accented cutout, so the raw form is deliberate.
        if status == "fallback" or webp is None:
            # Record the fallback so we don't retry it every run (keeps backfill
            # moving); the app renders a framed tile for status='fallback'.
            items[pid] = {"aspect": aspect or 0.8, "status": "fallback", "v": PIPELINE_V, "mode": mode}
            fb += 1
        else:
            (img_dir / f"{pid}.webp").write_bytes(webp)
            items[pid] = {"aspect": aspect, "status": status, "v": PIPELINE_V, "mode": mode}
            ok += 1
        changed = True
        if done % 50 == 0:
            log(f"  {done}/{min(len(todo), args.limit)} ready={ok} fallback={fb} fail={fail} attempts={attempts} {time.time()-t0:.0f}s")
    remaining = max(0, len(todo) - done)
    if not todo:
        log("no new products to cut.")

    # ── RECHECK: cutouts made before v2 may be a person rather than a garment ──────
    by_id = {p["id"]: p for p in products if p.get("id")}
    stale = [pid for pid, v in items.items() if v.get("v") != PIPELINE_V and pid in by_id]
    prio = []
    if args.priority_ids and Path(args.priority_ids).exists():
        wanted = [l.strip() for l in Path(args.priority_ids).read_text(encoding="utf-8").splitlines() if l.strip()]
        rank = {pid: i for i, pid in enumerate(wanted)}
        prio = sorted((pid for pid in stale if pid in rank), key=rank.get)
    order = prio + [pid for pid in stale if pid not in set(prio)]
    kept = recut = demoted = rescued = rfail = rdone = 0
    for pid in order:
        if rdone >= args.recheck_limit or time.time() - t0 > budget_s:
            break
        rdone += 1
        p, entry, f = by_id[pid], items[pid], img_dir / f"{pid}.webp"
        try:
            if entry.get("status") == "ready" and f.exists():
                existing = Image.open(f).convert("RGBA")
                webp, aspect, status, mode = cut_v2(p, existing_rgba=existing)
            else:                          # a v1 fallback: v2 may now find a usable image
                webp, aspect, status, mode = cut_v2(p)
        except Exception as e:
            log(f"  recheck failed {pid}: {type(e).__name__}")
            rfail += 1
            continue
        if mode == "keep-existing":
            entry.update({"v": PIPELINE_V, "mode": mode}); kept += 1
        elif status == "ready" and webp is not None:
            f.write_bytes(webp)
            if entry.get("status") == "ready": recut += 1
            else: rescued += 1
            items[pid] = {"aspect": aspect, "status": "ready", "v": PIPELINE_V, "mode": mode}
        else:
            # never keep a person: drop the file, the app renders a framed tile
            if f.exists():
                try: f.unlink()
                except OSError: pass
            if entry.get("status") == "ready": demoted += 1
            items[pid] = {"aspect": entry.get("aspect", 0.8), "status": "fallback", "v": PIPELINE_V, "mode": mode}
        changed = True
        if rdone % 50 == 0:
            log(f"  recheck {rdone}: kept={kept} recut={recut} demoted={demoted} rescued={rescued} fail={rfail} {time.time()-t0:.0f}s")
    recheck_remaining = max(0, len(order) - rdone)
    if order:
        log(f"recheck: {rdone} examined ({len(prio)} prioritised) · kept={kept} recut={recut} "
            f"demoted={demoted} rescued={rescued} fail={rfail} · {recheck_remaining} v1 cutouts left")

    # Only rewrite (and thus commit) when something actually changed, so a quiet
    # night doesn't churn a fresh timestamp commit forever.
    if changed:
        _write_manifest(manifest_path, items)
    log(f"manifest: {len(items)} total · new this run ready={ok} fallback={fb} fail={fail} "
        f"({done} processed, {remaining} still remaining) {time.time()-t0:.0f}s")
    # Signal to the workflow whether another pass is worthwhile.
    with Path(os.environ.get("GITHUB_OUTPUT", os.devnull)).open("a") as gh:
        gh.write(f"remaining={remaining}\nrecheck_remaining={recheck_remaining}\n")


def _write_manifest(path, items):
    path.write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(items),
        "ready": sum(1 for v in items.values() if v.get("status") == "ready"),
        "items": items,
    }, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
