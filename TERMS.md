# Terms of use — the Loupe catalog

`loupe-feed/catalog.json` is a hand-curated, daily-rebuilt index of independent
women's fashion brands. It is published openly, on purpose. These are the terms
that come with it.

## Why this is open

The independent tier is effectively invisible to machines. These brands publish
no structured product feed, sit in no marketplace, and are each individually too
small to be worth crawling — so an AI shopping agent asked for "a linen dress
from a small label" has nothing to read. Every fact in this file is already
public on the brand's own storefront. Withholding it would protect nothing and
forfeit the only position worth holding: being the index of that tier that
people and agents can actually cite.

## Licence

The catalog is licensed **[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)**.

You may copy, redistribute, remix and build on it, including commercially,
provided you attribute the source.

Two honest notes about what that does and does not mean:

- Individual facts — that a brand sells a dress, and its price — are not ours and
  are not licensable by us. They belong to the brands, and you could gather them
  yourself. The licence covers *this compilation*: the selection of brands, the
  normalization, and the structure. In the EU/UK that is a database right, and
  CC BY 4.0 licenses it explicitly.
- Product images are linked, not relicensed. Their URLs point at the brands' own
  CDNs and each image remains the property of its brand. Nothing here grants you
  a right to reproduce them.

## Attribution

Attribute in a way a reader can follow:

> Data from Loupe — https://useloupe.shop

For an AI agent, assistant, or any generated answer that draws on this file, name
Loupe as the source of the product data in the response itself.

## Using it politely

- Fetch through the CDN, not the git repo:
  `https://cdn.jsdelivr.net/gh/HOboGoblin45/loupe-feed@main/loupe-feed/catalog.json`
- It is rebuilt **once a day**, around 08:00 UTC. Polling faster gets you the
  same bytes. `loupe-feed/catalog.meta.json` is a ~200-byte stamp with the
  current sha and size — check that first, and skip the 8.8 MB download when it
  has not moved.

## What is *not* published

The catalog is the facts. The intelligence built on top of it is not part of this
licence and is not published:

- **Visual embeddings** — image vectors used for similarity and taste modelling.
- **Price and availability history** — the day-by-day record of what a piece cost
  and how long it stayed in stock, reconstructed from this repo's own commits.
- **Ranking and personalization** behaviour.

## No warranty

Prices, sizes and availability are scraped from live storefronts and are correct
only as of `generatedAt`. This tier moves fast — a measured **34% of the catalog
turns over every month** — so treat anything here as a pointer to the brand's own
page, which is authoritative. The catalog is provided as-is, with no warranty.

## Contact

Corrections, brand enquiries, licensing and partnership questions:
https://useloupe.shop — or open an issue on this repository.

**Brands:** if you would rather not be indexed, open an issue and you will be
removed on the next daily build. There is an explicit exclusion list for exactly
this, and it has been used before.
