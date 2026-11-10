#!/usr/bin/env python3
"""Supabase request headers -- run by CI before every catalog build.

A new-style Supabase secret key is refused (HTTP 401) when the request carries
a browser User-Agent. process_requests.py sent the scraper's Chrome string to
Supabase, so the brand-request pipeline silently processed nothing. These
checks keep a browser string off every database call."""
import re

import notify_brand_requests
import price_drop_push
import process_requests

BROWSERISH = re.compile(r"mozilla|chrome|safari|applewebkit|gecko|firefox|edg/|opera", re.I)
checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    assert cond, msg


for mod in (process_requests, notify_brand_requests):
    h = mod._sb_headers()
    ua = h.get("User-Agent", "")
    ok(ua and not BROWSERISH.search(ua), f"{mod.__name__}: Supabase User-Agent looks like a browser: {ua!r}")
    ok(h.get("apikey") == h.get("Authorization", "")[len("Bearer "):],
       f"{mod.__name__}: apikey and bearer token must be the same value")
    # extra headers are merged in without bringing the browser string back
    h2 = mod._sb_headers({"Prefer": "return=minimal"})
    ok(h2.get("Prefer") == "return=minimal" and not BROWSERISH.search(h2.get("User-Agent", "")),
       f"{mod.__name__}: extra headers changed the User-Agent")
    # the storefront fetches still need the browser string
    ok(BROWSERISH.search(mod.USER_AGENT), f"{mod.__name__}: storefront User-Agent lost its browser string")

# price_drop_push.py builds its headers inline; it must not add a browser UA either.
src = open(price_drop_push.__file__, encoding="utf-8").read()
block = src[src.index("def sb("):src.index("def sb_all(")]
ok(not BROWSERISH.search(block), "price_drop_push.sb(): a browser User-Agent reached the Supabase call")

print(f"supabase-header fixtures: all OK ({checks} checks)")
