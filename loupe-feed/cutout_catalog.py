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
    status = "ready" if 0.03 <= coverage <= 0.97 else "fallback"

    if w > MAX_W:
        trimmed = trimmed.resize((MAX_W, round(h * MAX_W / w)), Image.LANCZOS)
        w, h = trimmed.size
    buf = io.BytesIO()
    trimmed.save(buf, format="WEBP", quality=85, method=6)
    return buf.getvalue(), round(w / h, 4), status


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

    # Prune manifest entries for products that left the catalog (keeps it lean and
    # stops the app pointing cutoutUrls at delisted items).
    live_ids = {p.get("id") for p in products if p.get("id")}
    for gone in [pid for pid in items if pid not in live_ids]:
        items.pop(gone, None)
        f = img_dir / f"{gone}.webp"
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass

    todo = [p for p in products if p.get("id") and p.get("imageUrl") and p["id"] not in items]
    log(f"to cut this run: {min(len(todo), args.limit)} of {len(todo)} remaining")
    if not todo:
        # Still rewrite the manifest header so the version stamp/count refresh.
        _write_manifest(manifest_path, items)
        log("nothing to do — backfill complete.")
        return

    session = new_session(MODEL)
    t0 = time.time()
    done = ok = fb = fail = 0
    for p in todo:
        if done >= args.limit:
            break
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
        if webp is None:
            # Record the fallback so we don't retry it every run (keeps backfill moving);
            # the app renders a framed tile for status='fallback'.
            items[pid] = {"aspect": p.get("cutoutAspect") or 0.8, "status": "fallback"}
            fb += 1
        else:
            (img_dir / f"{pid}.webp").write_bytes(webp)
            items[pid] = {"aspect": aspect, "status": status}
            ok += 1 if status == "ready" else 0
            fb += 1 if status == "fallback" else 0
        if done % 50 == 0:
            log(f"  {done}/{min(len(todo), args.limit)} ready={ok} fallback={fb} fail={fail} {time.time()-t0:.0f}s")

    _write_manifest(manifest_path, items)
    remaining = len(todo) - done
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
