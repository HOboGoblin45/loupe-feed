#!/usr/bin/env python3
"""Loupe -- take the archive somewhere that is not this repository.

WHY THIS EXISTS

The price/availability archive exists in exactly one place: the commit history of
github.com/HOboGoblin45/loupe-feed. That is one account, one force-push, one
DMCA complaint, one "let's clean up the history to shrink the clone" away from
being gone. Nothing about it is secret -- the repo is public and the catalog is
CC-BY -- so this is not about confidentiality. It is about the fact that elapsed
time is the only input to this dataset that cannot be re-acquired: the storefronts
that 2026-06-17 recorded no longer look like that, and no amount of money buys a
second copy of a day.

WHAT IT WRITES  (default: <repo>/archive_out/, gitignored)

  loupe-feed-<date>.bundle
      `git bundle --all`: the ENTIRE repository -- every commit, every daily
      catalog blob, every branch -- in one file. Restore is one command:
          git clone loupe-feed-<date>.bundle loupe-feed
      This is the real backup. It is opaque without git, which is why it is not
      the only thing here.

  loupe-price-archive-<date>.jsonl.gz
      The distilled series: for every archived day, the price and the size list
      of every product on that day. ~1% of the size of the raw snapshots, plain
      gzipped JSONL, readable by anything, and a superset of what
      build_price_history.py needs to rebuild price_history.json from scratch.
      This is the copy that survives git itself going away, and the one an
      acquirer's analyst can open without cloning anything.

  ARCHIVE_MANIFEST.json
      sha256 and byte size of both files, the day list, the gaps, and the
      integrity headline. A backup nobody can verify is a rumour.

FORMAT (JSONL, one JSON object per line, gzipped)

  line 1   {"kind":"meta",  ...days, missingDays, integrity, schema...}
  line 2   {"kind":"index", ids[], names[], brands[], brandOf[], categories[]}
  line 3+  {"kind":"day", "day":"YYYY-MM-DD", "products":N,
            "i":[...index into ids...], "p":[...USD price...], "s":["24,26,28",...]}

  Product k on that day is ids[i[k]] at p[k] with sizes s[k].split(","). A
  product ABSENT from a day's arrays was not on sale that day -- presence is the
  availability signal, which is why the day list and the gap list are part of the
  meta line: an analyst has to be able to tell "sold out" from "we weren't
  looking".

SHALLOW CLONES

Refused by default, via the same history_is_truncated() guard as
build_price_history.py and archive_integrity.py. A backup that silently holds 28
of 42 days is worse than no backup, because it will be trusted and it will be the
thing that gets restored.

  CI: actions/checkout@v4 with fetch-depth: 0. This one needs the BLOBS as well
  as the commits, so it cannot use a treeless/partial clone.

USAGE
    python archive_backup.py                    # bundle + distilled + manifest
    python archive_backup.py --out DIR
    python archive_backup.py --skip-bundle      # distilled only (fast)
    python archive_backup.py --verify FILE.gz   # read a distilled archive back
"""

import argparse
import datetime as dt
import gzip
import hashlib
import json
import pathlib
import subprocess
import sys

from archive_integrity import (
    analyze,
    collect,
    git_bytes,
    headline,
    refuse_if_truncated,
)
from build_price_history import CATALOG_REL, REPO, daily_snapshots

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_OUT = REPO / "archive_out"
SOURCE_URL = "https://github.com/HOboGoblin45/loupe-feed"


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_distilled(out_path: pathlib.Path, verbose=True):
    """Walk every daily snapshot and write the compressed price/availability series."""
    snaps = daily_snapshots()
    if not snaps:
        sys.exit("No catalog snapshots in git history -- nothing to back up.")

    ids, id_index = [], {}
    names, brand_of, categories = [], [], []
    brands, brand_index = [], {}
    per_day = []

    for day, sha in snaps:
        raw = git_bytes("show", f"{sha}:{CATALOG_REL}")
        if not raw.strip():
            if verbose:
                print(f"  {day}: empty snapshot, skipped", file=sys.stderr)
            continue
        try:
            doc = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            if verbose:
                print(f"  {day}: unparseable snapshot, skipped", file=sys.stderr)
            continue

        idxs, prices, sizes = [], [], []
        for p in doc.get("products", []):
            pid = p.get("id")
            price = p.get("price")
            if not pid or not isinstance(price, (int, float)) or price <= 0:
                continue
            k = id_index.get(pid)
            if k is None:
                k = id_index[pid] = len(ids)
                ids.append(pid)
                names.append(p.get("name") or "")
                categories.append(p.get("category") or "")
                b = (p.get("brand") or "?").strip() or "?"
                bi = brand_index.get(b)
                if bi is None:
                    bi = brand_index[b] = len(brands)
                    brands.append(b)
                brand_of.append(bi)
            idxs.append(k)
            prices.append(round(float(price), 2))
            # Joined rather than nested: the same size string recurs on tens of
            # thousands of rows across 42 days, and gzip collapses repeated
            # literals far better than it collapses repeated small arrays.
            sizes.append(",".join(str(s) for s in (p.get("sizes") or [])))
        per_day.append({"day": day, "i": idxs, "p": prices, "s": sizes})
        if verbose:
            print(f"  {day}  {len(idxs):>5} products", file=sys.stderr)

    days = [d["day"] for d in per_day]
    st = analyze(days, truncated=False)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8", newline="\n") as fh:
        meta = {
            "kind": "meta",
            "format": "loupe-price-archive",
            "version": 1,
            "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": SOURCE_URL,
            "license": "CC-BY-4.0",
            "attribution": "Data from Loupe -- https://useloupe.shop",
            "days": days,
            "dayCount": len(days),
            "firstDay": st["first"],
            "lastDay": st["last"],
            "missingDays": st["missingDays"],
            "coveragePct": st["coveragePct"],
            "longestRunDays": (st["longestRun"] or {}).get("days"),
            "productsEverSeen": len(ids),
            "brandsEverSeen": len(brands),
            "schema": ("line 2 is the id index; every later line is one day, where "
                       "product k is ids[i[k]] at price p[k] with sizes s[k].split(','). "
                       "Absence from a day means it was not on sale that day -- check "
                       "missingDays before reading absence as a sell-out."),
        }
        fh.write(json.dumps(meta, separators=(",", ":"), ensure_ascii=False) + "\n")
        fh.write(json.dumps({
            "kind": "index",
            "ids": ids, "names": names, "categories": categories,
            "brands": brands, "brandOf": brand_of,
        }, separators=(",", ":"), ensure_ascii=False) + "\n")
        for row in per_day:
            row["kind"] = "day"
            row["products"] = len(row["i"])
            fh.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")

    return meta, out_path


def verify_distilled(path: pathlib.Path) -> int:
    """Read a distilled archive back and prove it reconstructs. Exit code."""
    problems = []
    meta = index = None
    day_rows = 0
    seen_days = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            obj = json.loads(line)
            kind = obj.get("kind")
            if n == 1:
                meta = obj
                if kind != "meta":
                    problems.append("line 1 is not the meta record")
            elif n == 2:
                index = obj
                if kind != "index":
                    problems.append("line 2 is not the id index")
            else:
                day_rows += 1
                seen_days.append(obj["day"])
                if not (len(obj["i"]) == len(obj["p"]) == len(obj["s"])):
                    problems.append(f"{obj['day']}: i/p/s arrays are different lengths")
                if index and obj["i"] and max(obj["i"]) >= len(index["ids"]):
                    problems.append(f"{obj['day']}: product index out of range")
    if not meta or not index:
        problems.append("meta or index record missing")
    else:
        if day_rows != meta["dayCount"]:
            problems.append(f"meta says {meta['dayCount']} days, file holds {day_rows}")
        if seen_days != meta["days"]:
            problems.append("day rows do not match the meta day list")
        if seen_days != sorted(seen_days):
            problems.append("day rows are not in chronological order")
        if len(index["brandOf"]) != len(index["ids"]):
            problems.append("brandOf is not parallel to ids")

    print(f"VERIFY {path.name}")
    if meta:
        print(f"  {meta['dayCount']} days  {meta['firstDay']} -> {meta['lastDay']}  "
              f"{meta['productsEverSeen']:,} products  {meta['brandsEverSeen']} brands")
        if meta["missingDays"]:
            print(f"  gaps carried through faithfully: {len(meta['missingDays'])} day(s)")
    if problems:
        print("  FAILED:")
        for p in problems:
            print("    " + p)
        return 1
    print("  OK -- the archive reconstructs.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Write an off-repo copy of the Loupe price/availability archive.")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    ap.add_argument("--skip-bundle", action="store_true",
                    help="skip the full git bundle (distilled series only)")
    ap.add_argument("--verify", metavar="FILE",
                    help="verify an existing distilled archive and exit")
    ap.add_argument("--allow-shallow", action="store_true",
                    help="back up a truncated clone anyway (NOT a real backup)")
    args = ap.parse_args()

    if args.verify:
        raise SystemExit(verify_distilled(pathlib.Path(args.verify)))

    truncated = refuse_if_truncated(args.allow_shallow)
    if truncated:
        print("WARNING: backing up a SHALLOW clone. This is a fragment, not a backup.",
              file=sys.stderr)

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    print("distilling the archive from git...", file=sys.stderr)
    dist_path = out_dir / f"loupe-price-archive-{stamp}.jsonl.gz"
    meta, dist_path = build_distilled(dist_path)

    artifacts = [dist_path]
    if not args.skip_bundle:
        bundle = out_dir / f"loupe-feed-{stamp}.bundle"
        print("bundling the whole repository...", file=sys.stderr)
        r = subprocess.run(["git", "-C", str(REPO), "bundle", "create", str(bundle), "--all"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not bundle.exists():
            sys.exit(f"git bundle failed:\n{r.stderr}")
        # A bundle that does not verify is a file, not a backup.
        v = subprocess.run(["git", "bundle", "verify", str(bundle)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if v.returncode != 0:
            sys.exit(f"git bundle verify FAILED -- refusing to publish it:\n{v.stderr}")
        artifacts.append(bundle)

    st = collect(allow_shallow=args.allow_shallow)
    manifest = {
        "generatedAt": meta["generatedAt"],
        "source": SOURCE_URL,
        "headline": headline(st),
        "integrity": {k: st[k] for k in (
            "observedDays", "first", "last", "spanDays", "missingDays", "gapCount",
            "coveragePct", "longestRun", "currentStreak") if k in st},
        "partialHistory": bool(truncated),
        "restore": {
            "bundle": "git clone <bundle file> loupe-feed",
            "distilled": "gzip -dc <file>.jsonl.gz | head -1   # meta record",
        },
        "artifacts": [
            {"name": p.name, "bytes": p.stat().st_size, "sha256": sha256_of(p)}
            for p in artifacts
        ],
    }
    man_path = out_dir / "ARCHIVE_MANIFEST.json"
    man_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print()
    print("=" * 78)
    print("ARCHIVE BACKUP")
    print("=" * 78)
    print(manifest["headline"])
    print()
    for a in manifest["artifacts"]:
        print(f"  {a['name']:44} {a['bytes'] / 1048576:8.2f} MB  {a['sha256'][:16]}")
    print(f"  {man_path.name:44} {man_path.stat().st_size / 1024:8.2f} KB")
    print(f"\n  -> {out_dir}")
    if truncated:
        print("\n  !! PARTIAL -- taken from a shallow clone. Do not treat as the record.")


if __name__ == "__main__":
    main()
