#!/usr/bin/env python3
"""
Loupe - catalog image embeddings (powers visual "more like this").

Embeds each product's hero image with Marqo-FashionSigLIP (open-source, fashion-
tuned, loaded via open_clip), L2-normalizes, int8-quantizes, writes embeddings.json.
The app fetches it once (jsDelivr, cached) and does on-device cosine nearest-neighbor
for: "More like this" on a product, a "Similar to your saves" feed, and a visual
boost in the Discover deck. Cross-category by design. Embeddings are DATA -> re-run
+ improve with no app update. Runs as its OWN CI workflow (heavy torch deps).
"""
import base64, io, json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
import open_clip
from PIL import Image

HERE = Path(__file__).parent
CATALOG = HERE / "catalog.json"
OUT = HERE / "embeddings.json"
MODEL_REF = "hf-hub:Marqo/marqo-fashionSigLIP"
BATCH = 32
TIMEOUT = 15
UA = "Mozilla/5.0 (compatible; LoupeEmbedder/1.0)"


def log(*a):
    print(*a, flush=True)


def fetch_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read()
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None


def main():
    cat = json.loads(CATALOG.read_text(encoding="utf-8"))
    products = cat.get("products", [])
    log(f"catalog: {len(products)} products")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"loading {MODEL_REF} on {device} ...")
    model, _, preprocess = open_clip.create_model_and_transforms(MODEL_REF)
    model = model.to(device).eval()

    ids, vecs, bimg, bid = [], [], [], []

    def flush():
        if not bimg:
            return
        t = torch.stack([preprocess(im) for im in bimg]).to(device)
        with torch.no_grad():
            f = model.encode_image(t)
            f = f / f.norm(dim=-1, keepdim=True)
        f = f.detach().cpu().numpy().astype(np.float32)
        for i, pid in enumerate(bid):
            ids.append(pid)
            vecs.append(f[i])
        bimg.clear()
        bid.clear()

    t0 = time.time()
    ok = fail = 0
    for n, p in enumerate(products):
        pid = p.get("id")
        url = p.get("imageUrl")
        if not pid or not url:
            fail += 1
            continue
        img = fetch_image(url)
        if img is None:
            fail += 1
            continue
        bimg.append(img)
        bid.append(pid)
        ok += 1
        if len(bimg) >= BATCH:
            flush()
        if n % 200 == 0:
            log(f"  {n}/{len(products)} ok={ok} fail={fail} {time.time()-t0:.0f}s")
    flush()

    if not vecs:
        log("ERROR: no embeddings produced")
        sys.exit(1)

    mat = np.vstack(vecs).astype(np.float32)
    dim = int(mat.shape[1])
    q = np.clip(np.round(mat * 127.0), -127, 127).astype(np.int8)
    payload = {
        "model": MODEL_REF,
        "dim": dim,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(ids),
        "scale": 1.0 / 127.0,
        "ids": ids,
        "vectors": base64.b64encode(q.tobytes()).decode("ascii"),
    }
    OUT.write_text(json.dumps(payload), encoding="utf-8")
    log(f"wrote {OUT.name}: {len(ids)}x{dim} {OUT.stat().st_size/1e6:.1f}MB ok={ok} fail={fail} {time.time()-t0:.0f}s")

    try:
        nm = {p["id"]: f'{p.get("brand","")}/{p.get("name","")[:22]}' for p in products if p.get("id")}
        fm = mat[:300]
        sims = fm @ fm.T
        np.fill_diagonal(sims, -1.0)
        log("sample visual neighbors:")
        for i in range(0, 300, 60):
            j = int(sims[i].argmax())
            log(f"  {nm.get(ids[i], '?')} ~ {nm.get(ids[j], '?')} (cos={sims[i][j]:.2f})")
    except Exception:
        pass


if __name__ == "__main__":
    main()
