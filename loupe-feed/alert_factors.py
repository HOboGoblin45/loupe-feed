#!/usr/bin/env python3
"""Which price moves are OUR arithmetic and must never be shown as a sale.

One function, read by both things that can tell a user a piece is "on sale":

  * price_drop_push.py   -- the server's daily digest push
  * build_catalog.py     -- publishes the same map as catalog.json's
                            `priceCorrections` block, which the app reads for the
                            Dresser sale badge, the Sale & restock alerts screen,
                            the on-device digest and the "Share drop" image

Until 2026-11-09 only the first of those read price_corrections.json. The app has
parsed `priceCorrections` since August and the feed never published it, so every
in-app surface ran with no guard at all while the push had one.

Two sections of price_corrections.json feed the map:

  corrections       the eight brands whose base currency was mis-annotated. These
                    also re-derive archived prices (build_price_history.py).
  alertCorrections  brands whose stored prices stepped by one ratio for a reason
                    the archive CANNOT invert (see `_uncorrectable_comment`), so
                    they must not enter `corrections` -- but a user who saved a
                    piece on the wrong side of that step is still told, every
                    day, that it is "73% off". Alert paths only; nothing that
                    reads the archive looks at this section.

`corrections` wins where a brand appears in both.
"""


def alert_factors(doc):
    """{brand: factor} from a parsed price_corrections.json."""
    out = {}
    for section in ("alertCorrections", "corrections"):   # later section wins
        for c in (doc or {}).get(section) or []:
            if not isinstance(c, dict):
                continue
            brand = c.get("brand")
            try:
                factor = float(c.get("factor"))
            except (TypeError, ValueError):
                continue
            if isinstance(brand, str) and brand.strip() and factor > 0 and factor == factor \
                    and factor != float("inf"):
                out[brand] = factor
    return out
