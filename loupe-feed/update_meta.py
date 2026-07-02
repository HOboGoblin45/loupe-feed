#!/usr/bin/env python3
"""Write loupe-feed/catalog.meta.json — a ~200-byte version stamp for the app.

The app probes THIS file on every open (cheap) and only downloads the multi-MB
catalog.json / embeddings.json when the sha actually changed. Run after either
artifact is (re)written: build_catalog.py calls write_meta() itself, and the
embed workflow runs this script directly after embed_catalog.py.
"""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
META_FILE = HERE / "catalog.meta.json"


def write_meta() -> None:
    meta = {}
    for name in ("catalog.json", "embeddings.json"):
        p = HERE / name
        if p.exists():
            b = p.read_bytes()
            meta[name] = {"sha1": hashlib.sha1(b).hexdigest(), "bytes": len(b)}
    META_FILE.write_text(json.dumps(meta, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"meta: {META_FILE.name} <- {', '.join(meta) or 'nothing'}")


if __name__ == "__main__":
    write_meta()
