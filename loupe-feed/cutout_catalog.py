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
from rembg import remove, new_session

# The catalog is the source of truth for which products exist. Read it fresh from
# raw GitHub (uncached) rather than the jsDelivr CDN, so a just-refreshed catalog's
# new products are cut out on the very next run.
CATALOG_URL = "https://raw.githubusercontent.com/HOboGoblin45/loupe-feed/main/loupe-feed/catalog.json"
UA = "Mozilla/5.0 (compatible; LoupeCutout/1.0)"
TIMEOUT = 20
MAX_W = 800          # single retina-adequate size; WebP w/ alpha ~20-40KB each
MODEL = "u2net"      # general foreground model; good on flat-lay + on-model fashion


def log(*a):
    print(*a, flush=True)


def fetch_catalog():
    req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8")).get("products", [])


def fetch_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return Image.open(io.BytesIO(r.read())).convert("RGBA")
    except Exception:
        return None


def cut(img, session):
    """rembg -> trim transparent margin -> resize -> (webp_bytes, aspect, status)."""
    out = remove(img, session=session)  # RGBA with alpha matte
    if out.mode != "RGBA":
        out = out.convert("RGBA")
    alpha = out.split()[-1]
    bbox = alpha.getbbox()
    if not bbox:
        return None, None, "fallback"  # nothing survived — segmentation failed
    trimmed = out.crop(bbox)
    w, h = trimmed.size
    if w < 8 or h < 8:
        return None, None, "fallback"

    # Confidence proxy from the matte: a good cutout keeps a healthy but not-total
    # fraction of the frame. Near-empty (<3%) or near-full (>97%, i.e. nothing was
    # removed — likely a lifestyle/on-model shot the model couldn't isolate) is a
    # low-confidence fallback; the app renders those as a clean framed tile instead.
    import numpy as np
    a = np.asarray(alpha, dtype=np.float32) / 255.0
    coverage = float((a > 0.5).mean())
    aspect = round(w / h, 4)
    if not (0.03 <= coverage <= 0.97):
        # Unusable matte (near-empty, or nothing removed) — record the fallback so
        # we don't retry it every run, but store NO file. The app renders a framed
        # tile for fallbacks; an orphaned webp would just bloat the branch.
        return None, aspect, "fallback"
    if w > MAX_W:
        trimmed = trimmed.resize((MAX_W, round(h * MAX_W / w)), Image.LANCZOS)
    buf = io.BytesIO()
    trimmed.save(buf, format="WEBP", quality=85, method=6)
    return buf.getvalue(), aspect, "ready"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="working dir = checked-out cutouts branch")
    ap.add_argument("--limit", type=int, default=500, help="max NEW cutouts this run")
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

    products = fetch_catalog()
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
    if not todo:
        # Only rewrite (and thus commit) when something actually changed, so a
        # complete-backfill night doesn't churn a fresh timestamp commit forever.
        if changed:
            _write_manifest(manifest_path, items)
        log("nothing to cut — backfill complete.")
        Path(os.environ.get("GITHUB_OUTPUT", os.devnull)).open("a").write("remaining=0\n")
        return

    session = new_session(MODEL)
    t0 = time.time()
    done = ok = fb = fail = attempts = 0
    # Bound total attempts so a run of dead image URLs can't walk the whole catalog
    # and blow the CI time budget (each fetch can hang to TIMEOUT). A run either
    # reaches `limit` successes or gives up after limit*2 attempts, then COMMITS.
    attempt_cap = args.limit * 2
    for p in todo:
        if done >= args.limit or attempts >= attempt_cap:
            break
        attempts += 1
        pid = p["id"]
        img = fetch_image(p["imageUrl"])
        if img is None:
            fail += 1
            continue
        try:
            webp, aspect, status = cut(img, session)
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
        if webp is None:
            # Record the fallback so we don't retry it every run (keeps backfill
            # moving); the app renders a framed tile for status='fallback'.
            items[pid] = {"aspect": aspect or 0.8, "status": "fallback"}
            fb += 1
        else:
            (img_dir / f"{pid}.webp").write_bytes(webp)
            items[pid] = {"aspect": aspect, "status": status}
            ok += 1
        changed = True
        if done % 50 == 0:
            log(f"  {done}/{min(len(todo), args.limit)} ready={ok} fallback={fb} fail={fail} attempts={attempts} {time.time()-t0:.0f}s")

    if changed:
        _write_manifest(manifest_path, items)
    remaining = max(0, len(todo) - done)
    log(f"wrote manifest: {len(items)} total · this run ready={ok} fallback={fb} fail={fail} "
        f"({done} processed, {remaining} still remaining) {time.time()-t0:.0f}s")
    # Signal to the workflow whether another pass is worthwhile.
    Path(os.environ.get("GITHUB_OUTPUT", os.devnull)).open("a").write(f"remaining={remaining}\n")


def _write_manifest(path, items):
    path.write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(items),
        "ready": sum(1 for v in items.values() if v.get("status") == "ready"),
        "items": items,
    }, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
