"""Garment-aware cutouts for the Look Builder (v2).

WHY: the shipped pipeline runs rembg/u2net -- a salient-object model -- on the hero
image. On an on-model photo the most salient object is the MODEL, so roughly half
of all cutouts are a whole person (or, for jewellery, a clump of hair). u2net is
excellent on product shots, and a clothes parser is excellent at telling garment
from body. This uses each for what it is good at:

  1. KEEP  the existing cutout when it contains no person        (fast path, no work)
  2. CLEAN hero is a product shot -> u2net on the hero             (also fixes stale cutouts)
  3. SWAP  a gallery image is a product shot in the SAME colourway -> u2net on it
  4. EXTRACT on-model hero -> keep only the garment-class pixels, drop the body
  5. FALLBACK nothing trustworthy -> framed tile, never a person

For jewellery, bags and belts, u2net's habit of filling enclosed backdrop (the inside
of a necklace, a bag handle) is undone in step 2, and a person-free existing cutout
that looks filled in is re-cut from its clean hero - but kept if there is none.

The clothes parser is SegFormer-B2 fine-tuned on ATR (mattmdjaga/segformer_b2_clothes).
"""
import io, urllib.request
import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage
import torch
from transformers import SegformerImageProcessor, AutoModelForSemanticSegmentation

MODEL_ID = "mattmdjaga/segformer_b2_clothes"
BG, HAT, HAIR, SUNGL, UPPER, SKIRT, PANTS, DRESS, BELT, LSHOE, RSHOE, FACE, LLEG, RLEG, LARM, RARM, BAG, SCARF = range(18)
TARGETS = {
    "tops":      [{UPPER}],
    "dresses":   [{DRESS}, {DRESS, UPPER, SKIRT}],
    "bottoms":   [{PANTS, SKIRT}],
    "outerwear": [{UPPER}, {UPPER, DRESS}],
    "shoes":     [{LSHOE, RSHOE}],
    "swim":      [{UPPER, PANTS, SKIRT}],
}
ACC_SUBTYPE = {"bag": {BAG}, "hat": {HAT}, "eyewear": {SUNGL}, "scarf": {SCARF}, "belt": {BELT}}
UA = {"User-Agent": "Mozilla/5.0 (compatible; LoupeCutout/2.0)"}

_proc = _model = _u2 = None
def _seg_model():
    global _proc, _model
    if _model is None:
        _proc = SegformerImageProcessor.from_pretrained(MODEL_ID)
        _model = AutoModelForSemanticSegmentation.from_pretrained(MODEL_ID).eval()
    return _proc, _model

def _u2net():
    global _u2
    if _u2 is None:
        from rembg import new_session
        _u2 = new_session("u2net")
    return _u2

def fetch(url, max_side=768, timeout=25, mode="RGB"):
    req = urllib.request.Request(url, headers=UA)
    im = Image.open(io.BytesIO(urllib.request.urlopen(req, timeout=timeout).read())).convert(mode)
    im.thumbnail((max_side, max_side))
    return im

@torch.no_grad()
def parse(im):
    proc, model = _seg_model()
    logits = model(**proc(images=im, return_tensors="pt")).logits
    up = torch.nn.functional.interpolate(logits, size=im.size[::-1], mode="bilinear", align_corners=False)
    return up.argmax(1)[0].numpy()

def targets_for(category, subtype=None):
    if category == "accessories":
        s = ACC_SUBTYPE.get((subtype or "").lower())
        return [s] if s else []
    return TARGETS.get(category, [])

# --- person detection -------------------------------------------------------
# The clothes parser is NOT a person detector: clothed models with cropped faces
# read as "no skin", and ribbed metal reads as "hair". A COCO object detector
# catches the body; the parser's hair share catches a cutout that is just a clump
# of hair (jewellery worn on a model).
# Validated on 28 hand-labelled cutouts: 16/16 people caught, 0/12 false alarms
# (people scored >=0.99, clean items <=0.86), and 60/60 on a hand-checked audit
# sample. KNOWN GAP: a faceless crop of a neck or an ear (2 of ~200 jewellery
# cutouts examined). The parser's skin share would catch those, but it also reads
# polished GOLD as skin (gold hoops scored 0.94-0.97), and a re-cut driven by it
# swapped a gold earring for the silver variant further down the gallery - so it
# is deliberately not used.
DET_ID = "hustvl/yolos-small"
DET_MIN = 0.90
HAIR_SHARE_MIN = 0.35
_det_proc = _det = None
def _detector():
    global _det_proc, _det
    if _det is None:
        from transformers import AutoImageProcessor, AutoModelForObjectDetection
        _det_proc = AutoImageProcessor.from_pretrained(DET_ID)
        _det = AutoModelForObjectDetection.from_pretrained(DET_ID).eval()
    return _det_proc, _det

@torch.no_grad()
def person_score(im):
    proc, det = _detector()
    pid = [k for k, v in det.config.id2label.items() if v == "person"][0]
    r = proc.post_process_object_detection(det(**proc(images=im, return_tensors="pt")),
                                           threshold=0.5, target_sizes=[im.size[::-1]])[0]
    area = im.size[0] * im.size[1]
    return max([float(s) for s, l, b in zip(r["scores"], r["labels"], r["boxes"])
                if int(l) == pid and float((b[2] - b[0]) * (b[3] - b[1])) / area >= 0.05] + [0.0])

def has_person(im, seg, fg=None):
    """Is there a person (or a clump of their hair) in frame? Cheap parser test first."""
    if fg is None: fg = seg != BG
    n = max(int(fg.sum()), 1)
    if float((seg[fg] == HAIR).sum()) / n >= HAIR_SHARE_MIN:
        return True
    return person_score(im) >= DET_MIN

def best_garment(seg, category, subtype):
    best = (0.0, None)
    for cls in targets_for(category, subtype):
        frac = float(np.isin(seg, list(cls)).mean())
        if frac > best[0]: best = (frac, cls)
    return best

def mean_lab(im, mask):
    """Mean CIELAB colour of the masked pixels (for colourway matching)."""
    lab = np.asarray(im.convert("LAB"), dtype=np.float32)
    m = mask.astype(bool)
    return lab[m].mean(0) if m.sum() > 200 else None

def delta_e(a, b):
    return float(np.linalg.norm(a - b)) if a is not None and b is not None else 999.0

def clean_components(alpha, keep_frac=0.12):
    """Drop floating fragments: keep connected parts >= keep_frac of the largest."""
    solid = alpha > 40
    lab, n = ndimage.label(solid)
    if n <= 1: return alpha, 1.0
    sizes = ndimage.sum(solid, lab, range(1, n + 1))
    keep = np.isin(lab, [i + 1 for i, s in enumerate(sizes) if s >= keep_frac * sizes.max()])
    coherence = float(sizes.max() / sizes.sum())
    return np.where(keep, alpha, 0).astype(np.uint8), coherence

def trim(rgba, pad=4):
    bbox = rgba.getchannel("A").point(lambda a: 255 if a > 12 else 0).getbbox()
    if not bbox: return None
    l, t, r, b = bbox
    return rgba.crop((max(0, l - pad), max(0, t - pad), min(rgba.width, r + pad), min(rgba.height, b + pad)))

GARMENT_CLASSES = {UPPER, SKIRT, PANTS, DRESS}
DOMINANCE_MAX = 3.0

def dominated(seg, classes):
    """Is some OTHER garment class far bigger than the product's own class?

    The photo is framed on the product, so its garment should be at least a main
    garment in its own image. When it isn't, the parser mislabelled it: a skirt shot
    cropped at the waist came back 27.8% 'upper-clothes' and 4.3% 'skirt' (only its
    white slip hem), and extracting 'skirt' shipped a thin white strip. Calibrated on
    23 real on-model garments, whose ratios ran 0.0-2.25; that skirt scored 6.49."""
    target = float(np.isin(seg, list(classes)).mean())
    others = max([float((seg == k).mean()) for k in GARMENT_CLASSES - set(classes)] + [0.0])
    return others > DOMINANCE_MAX * max(target, 1e-6)

def min_coherence(category):
    """How 'one-piece' an extraction must be. A garment lifted off a model is a single
    connected shape; two big blobs means the parser grabbed something else too - e.g. a
    bonnet miscategorised as a top came out as its ties plus the model's tank top
    (coherence 0.69). Pairs of shoes and bikini sets are legitimately two pieces."""
    if category in ("shoes", "swim"):
        return 0.40
    if category == "accessories":
        return 0.70
    return 0.85

def clean_min_coherence(category):
    """Coherence gate for u2net on a CLEAN product shot. Garments are one piece, so a
    split mask means u2net failed. Earrings, shoes and bikinis are routinely shot as a
    pair or a set: two equal pieces score 0.50, three 0.33. At a flat 0.55 gate, 6 of
    12 sampled earring pairs fell back to a framed tile although their photos were
    clean product shots."""
    return 0.25 if category in ("accessories", "shoes", "swim") else 0.55

# --- backdrop seen THROUGH an accessory -------------------------------------------
# u2net fills enclosed background: the inside of a necklace laid in a circle, of a
# bracelet, of a bag handle, of a hoop. On the Look canvas that shows as a white
# disc. A real hole is the photo's own studio backdrop - the same colour as the image
# border and dead flat - bounded by the object. Metal highlights are near-white too,
# but they are shaded and fade into the metal, so they fail the flatness/edge tests.
# Checked on 30 flagged + 30 random catalog accessories: every region removed was a
# real hole (necklace and bracelet interiors, bag handles, hoops, ring openings).
HOLE_SUBTYPES = {"jewellery", "bag", "belt"}

def hole_kind(category, subtype):
    return category == "accessories" and (subtype or "").lower() in HOLE_SUBTYPES

def backdrop(im, border=6):
    """(median border colour, 90th-pct deviation) - a studio backdrop is uniform."""
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    b = np.concatenate([a[:border].reshape(-1, 3), a[-border:].reshape(-1, 3),
                        a[:, :border].reshape(-1, 3), a[:, -border:].reshape(-1, 3)])
    med = np.median(b, 0)
    return med, float(np.percentile(np.abs(b - med).max(1), 90))

def background_holes(im, alpha, relaxed=False):
    """Mask of backdrop pixels that u2net kept INSIDE the object, or None.

    strict  (all hole kinds): bounded by clearly different colour (edge >= 25) and flat.
    relaxed (jewellery only): a large (>= 20% of the piece), very flat region may have a
    low-contrast edge - silver chains and pastel beads on a white backdrop."""
    med, spread = backdrop(im)
    if spread > 6:                         # not a uniform backdrop: do not guess
        return None
    D = np.abs(np.asarray(im.convert("RGB")).astype(np.int16) - med).max(2)
    opaque = alpha > 128
    cand = opaque & (D <= max(4.0, spread + 2))
    lab, n = ndimage.label(cand)
    if n == 0:
        return None
    exterior = ndimage.binary_dilation(alpha <= 40, iterations=1)
    touching = set(np.unique(lab[exterior & cand]).tolist())
    total = max(int(opaque.sum()), 1)
    sizes = ndimage.sum(cand, lab, range(1, n + 1))
    keep = []
    for i, s in enumerate(sizes, 1):
        if i in touching or s < max(150, 0.005 * total):
            continue
        region = lab == i
        rim = ndimage.binary_dilation(region, iterations=3) & ~region & opaque
        edge = float(D[rim].mean()) if rim.any() else 0.0
        flat = float(D[region].std())
        if (edge >= 25 and flat <= 2.5) or (relaxed and s / total >= 0.20 and flat <= 0.9 and edge >= 5):
            keep.append(i)
    return np.isin(lab, keep) if keep else None

def punch(rgba, mask):
    """Make `mask` (plus its 1-px anti-aliased rim) transparent, softening the new edge."""
    grow = ndimage.binary_dilation(mask, iterations=1)
    a = np.asarray(rgba.getchannel("A")).copy()
    a[grow] = 0
    a = np.asarray(Image.fromarray(a, "L").filter(ImageFilter.GaussianBlur(0.6)))
    rgba = rgba.copy()
    rgba.putalpha(Image.fromarray(np.where(grow, 0, a).astype(np.uint8), "L"))
    return rgba

def suspect_filled_hole(rgba, min_frac=0.03):
    """Cheap test on an EXISTING cutout (no source photo needed): is there an enclosed,
    bright, neutral region worth re-cutting for? Deliberately over-sensitive - the
    re-cut applies the exact test, and never trades a cutout down to a fallback."""
    rgba = rgba.convert("RGBA")
    a = np.asarray(rgba.getchannel("A"))
    rgb = np.asarray(rgba.convert("RGB")).astype(np.int16)
    opaque = a > 200
    bright = opaque & (rgb.min(2) >= 225) & ((rgb.max(2) - rgb.min(2)) <= 12)
    lab, n = ndimage.label(bright)
    if n == 0:
        return False
    touching = set(np.unique(lab[ndimage.binary_dilation(a <= 40, iterations=2) & bright]).tolist())
    sizes = ndimage.sum(bright, lab, range(1, n + 1))
    total = max(int(opaque.sum()), 1)
    return any(s / total >= min_frac for i, s in enumerate(sizes, 1) if i not in touching)

def u2net_cut(im, category=None, subtype=None):
    from rembg import remove
    rgba = remove(im, session=_u2net())
    if hole_kind(category, subtype):
        holes = background_holes(im, np.asarray(rgba.getchannel("A")),
                                 relaxed=(subtype or "").lower() == "jewellery")
        if holes is not None:
            rgba = punch(rgba, holes)
    a, coh = clean_components(np.asarray(rgba.getchannel("A")))
    rgba.putalpha(Image.fromarray(a, "L"))
    return trim(rgba), coh

def garment_cut(im, seg, classes):
    m = (np.isin(seg, list(classes)) * 255).astype(np.uint8)
    mk = Image.fromarray(m, "L").filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    a, coh = clean_components(np.asarray(mk.filter(ImageFilter.GaussianBlur(1.2))))
    rgba = im.convert("RGBA"); rgba.putalpha(Image.fromarray(a, "L"))
    return trim(rgba), coh

def rgba_has_person(rgba):
    """Does an existing transparent cutout contain a person (or a clump of hair)?"""
    rgba = rgba.convert("RGBA")
    rgba.thumbnail((768, 768))
    flat = Image.new("RGB", rgba.size, (255, 255, 255)); flat.paste(rgba, mask=rgba.getchannel("A"))
    return has_person(flat, parse(flat), fg=np.asarray(rgba.getchannel("A")) > 40)

def existing_is_clean(cutout_url):
    """Fast path over the network: does the CURRENT hosted cutout contain a person?"""
    return not rgba_has_person(fetch(cutout_url, mode="RGBA"))

def cut_product(p, max_images=4, check_existing=True, existing_rgba=None):
    """Return (rgba or None, report). report['status'] is 'ready' or 'fallback';
    report['mode'] says which path produced it. rgba is None for keep-existing
    (the caller already has that file) and for fallbacks (render a framed tile)."""
    cat, sub = p.get("category"), p.get("accessorySubtype")
    # A person-free existing cutout that looks filled in (backdrop inside a necklace or
    # a handle) is re-cut from its clean hero only - the same photo, holes punched - and
    # kept if the hero is on-model. It must not wander into the gallery: nothing there
    # guarantees the same colourway for jewellery, which the parser has no class for.
    # A cutout WITH a person is never kept.
    holes_only = False
    if check_existing and (existing_rgba is not None or p.get("cutoutUrl")):
        try:
            current = existing_rgba if existing_rgba is not None else fetch(p["cutoutUrl"], mode="RGBA")
            if not rgba_has_person(current):
                if not (hole_kind(cat, sub) and suspect_filled_hole(current)):
                    return None, {"status": "ready", "mode": "keep-existing"}
                holes_only = True
        except Exception:
            pass

    def give_up(reason):
        if holes_only:
            return None, {"status": "ready", "mode": "keep-existing", "note": "no clean hero to re-cut from"}
        return None, {"status": "fallback", "reason": reason}

    urls = [p["imageUrl"]] + [u for u in (p.get("images") or []) if u != p["imageUrl"]]
    hero = None; cands = []
    for i, u in enumerate(urls[:1 if holes_only else max_images]):
        try: im = fetch(u)
        except Exception: continue
        seg = parse(im)
        c = {"i": i, "url": u, "im": im, "seg": seg, "person_in": has_person(im, seg)}
        c["gfrac"], c["cls"] = best_garment(seg, cat, sub)
        if i == 0: hero = c
        cands.append(c)
    if not cands:
        return give_up("no image fetchable")
    # 2) hero is itself a clean product shot (catches brands that swapped their hero)
    if hero and not hero["person_in"]:
        out, coh = u2net_cut(hero["im"], cat, sub)
        if out is not None and coh >= clean_min_coherence(cat):
            return out, {"status": "ready", "mode": "clean-hero"}
    if holes_only:
        return give_up("hero is on-model")
    # colour reference: the garment as worn in the hero
    ref = None
    if hero and hero["cls"]:
        ref = mean_lab(hero["im"], np.isin(hero["seg"], list(hero["cls"])))
    # 3) a clean gallery shot in the SAME colourway
    for c in sorted([c for c in cands if c["i"] > 0 and not c["person_in"]], key=lambda c: -c["gfrac"]):
        fg = c["seg"] != BG
        if ref is not None and delta_e(ref, mean_lab(c["im"], fg)) > 22:
            continue                     # different colourway -> would show the wrong product
        out, coh = u2net_cut(c["im"], cat, sub)
        if out is not None and coh >= clean_min_coherence(cat):
            return out, {"status": "ready", "mode": "clean-gallery", "image_index": c["i"]}
    # 4) extract the garment from the on-model hero
    # small objects (bags, shoes, hats, eyewear) naturally fill less of the frame
    min_frac = 0.012 if cat in ("accessories", "shoes") else 0.04
    garment = cat in ("tops", "dresses", "bottoms", "outerwear")
    if hero and hero["cls"] and hero["gfrac"] >= min_frac \
            and not (garment and dominated(hero["seg"], hero["cls"])):
        out, coh = garment_cut(hero["im"], hero["seg"], hero["cls"])
        if out is not None and coh >= min_coherence(cat):
            return out, {"status": "ready", "mode": "garment-from-model", "coherence": round(coh, 2)}
    # 5) never ship a person or confetti
    return give_up("no clean product shot and garment not separable")
