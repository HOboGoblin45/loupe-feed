# loupe-feed

A hand-curated, daily-rebuilt index of **independent women's fashion brands** —
the tier that publishes no structured feed, sits in no marketplace, and is
therefore invisible to price comparison sites and AI shopping agents alike.

Currently **~8,200 products across ~180 brands**, rebuilt every day at 08:00 UTC
from each brand's own public storefront.

Licensed **CC BY 4.0**. See [TERMS.md](TERMS.md) — attribution is required, and
there are a couple of honest caveats there about what a licence over facts can
and cannot mean.

## Published files

Served free and globally cached by jsDelivr. Prefer these over cloning:

| File | Size | What it is |
| --- | --- | --- |
| [`catalog.json`](https://cdn.jsdelivr.net/gh/HOboGoblin45/loupe-feed@main/loupe-feed/catalog.json) | ~8.8 MB | The index. |
| [`catalog.meta.json`](https://cdn.jsdelivr.net/gh/HOboGoblin45/loupe-feed@main/loupe-feed/catalog.meta.json) | ~200 B | Version stamp: sha1 + byte size per file. |

**Check the stamp before you download the catalog.** It is rebuilt once a day; if
the sha in `catalog.meta.json` has not changed, neither have the 8.8 MB.

## Shape

```jsonc
{
  "generatedAt": "2026-08-01T00:57:40Z",
  "count": 8198,
  "provenance": { "source": "Loupe", "license": "CC-BY-4.0", "terms": "…", … },
  "products": [
    {
      "id":         "khaite-dylan-top-in-dark-navy",
      "brand":      "Khaite",
      "name":       "Dylan Top in Dark Navy",
      "price":      1380,              // always USD, converted at build time
      "category":   "tops",
      "colorTags":  ["blue"],
      "imageUrl":   "https://…",       // hosted by the brand, not by us
      "images":     ["https://…"],     // full gallery
      "sizes":      ["S", "M"],        // available sizes only; [] if unknown
      "available":  true,
      "affiliateUrl": "https://khaite.com/products/…?utm_source=loupe",
      "addedAt":    "2026-07-30T00:23:55Z",   // first time we ever saw it
      "lastSeenAt": "2026-08-01T00:57:40Z"    // last build that still found it
    }
  ],
  "retailers": { /* partner shops, keyed by a product's `retailer` field */ }
}
```

`addedAt` / `lastSeenAt` are the useful ones for anything time-aware: this tier
turns over **~34% per month**, so "when did this appear" and "is it still there"
carry real signal.

## What is not here

The catalog is the facts. The intelligence derived from it — visual embeddings,
price and availability history, ranking behaviour — is not published. See
[TERMS.md](TERMS.md).

## Branches

- **`main`** — the source, the build, and the published catalog. Small: its whole
  history is under 50 MB, and that history *is* the price record, so it is never
  rewritten.
- **`cutouts`** — alpha-matted product cutouts (`img/*.webp`) for the app's Look
  Builder. Derived, regenerable, and **republished as a single root commit each
  night**, so it stays bounded instead of growing ~285 MB/month forever. Do not
  expect its history to be stable; do not put anything irreplaceable on it.

## Brands

If you run one of these labels and would rather not be indexed, open an issue —
you will be dropped on the next daily build.
