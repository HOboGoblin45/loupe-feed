"""Regression fixtures for the affiliate wrapper (monetize + per-brand templates).

Run directly:  python test_affiliate_wrappers.py   (exit 0 = pass)
Mirrors test_junk_filter.py: plain asserts, no test framework, gated in CI
before any catalog publish. The wrapper is pure env-driven config, so these
fixtures reload build_catalog under controlled environments.
"""
import importlib
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load(env):
    """(Re)import build_catalog with exactly `env` as the affiliate config."""
    for key in ("SOVRN_API_KEY", "SOVRN_CUID", "BRAND_AFFILIATE_TEMPLATES"):
        os.environ.pop(key, None)
    os.environ.update(env)
    import build_catalog
    return importlib.reload(build_catalog)


RAW = "https://peachyden.com/products/kylie-dress"
DM_RAW = "https://damsonmadder.com/products/frill-dress"
AWIN_TPL = {"Damson Madder": "https://www.awin1.com/cread.php?awinmid=114966&awinaffid=A1&ued={url}"}

# ── 1. No env at all → pass-through untouched (local runs / pre-approval) ─────
bc = load({})
assert bc.monetize(RAW, "Peachy Den") == RAW
assert bc.monetize(RAW) == RAW

# ── 2. Sovrn key only → viglink wrap with key + cuid; idempotent ──────────────
bc = load({"SOVRN_API_KEY": "k123", "SOVRN_CUID": "loupeapp"})
wrapped = bc.monetize(RAW, "Peachy Den")
assert wrapped.startswith("https://redirect.viglink.com/?")
assert "key=k123" in wrapped and "cuid=loupeapp" in wrapped
assert urllib.parse.quote(RAW, safe="") in wrapped
assert bc.monetize(wrapped, "Peachy Den") == wrapped  # never double-wrap

# ── 3. Brand template takes precedence; other brands keep the Sovrn catch-all ─
bc = load({"SOVRN_API_KEY": "k123", "BRAND_AFFILIATE_TEMPLATES": json.dumps(AWIN_TPL)})
dm = bc.monetize(DM_RAW, "Damson Madder")
assert dm.startswith("https://www.awin1.com/cread.php?awinmid=114966")
assert urllib.parse.quote(DM_RAW, safe="") in dm
assert bc.monetize(dm, "Damson Madder") == dm  # idempotent per-brand too
other = bc.monetize(RAW, "Peachy Den")
assert other.startswith("https://redirect.viglink.com/")  # catch-all intact

# ── 4. Brand matching is case / spacing / punctuation insensitive ─────────────
assert bc.monetize("https://damsonmadder.com/products/x", "  DAMSON  MADDER ").startswith(
    "https://www.awin1.com/"
)

# ── 5. Carried-forward Sovrn-wrapped URL is unwrapped, then template-wrapped ──
sovrn_wrapped = "https://redirect.viglink.com/?" + urllib.parse.urlencode(
    {"key": "k123", "u": DM_RAW, "cuid": "loupeapp"}
)
re_wrapped = bc.monetize(sovrn_wrapped, "Damson Madder")
assert re_wrapped.startswith("https://www.awin1.com/")
assert urllib.parse.quote(DM_RAW, safe="") in re_wrapped
# ...and the inner destination is the ORIGINAL product page, not the redirect.
assert "redirect.viglink.com" not in urllib.parse.unquote(
    re_wrapped.split("ued=", 1)[1]
).replace(sovrn_wrapped, "")

# ── 6. Malformed config fails SOFT: build must not crash, links pass through ──
bc = load({"BRAND_AFFILIATE_TEMPLATES": "not json"})
assert bc.monetize(RAW, "Peachy Den") == RAW
bc = load({"BRAND_AFFILIATE_TEMPLATES": json.dumps({"Ganni": "https://example.com/no-token"})})
assert bc.monetize("https://ganni.com/products/x", "Ganni") == "https://ganni.com/products/x"
bc = load({"BRAND_AFFILIATE_TEMPLATES": json.dumps(["not", "a", "dict"])})
assert bc.monetize(RAW, "Peachy Den") == RAW

# ── 7. Non-string / empty URLs are returned unchanged (curated-input safety) ──
bc = load({"SOVRN_API_KEY": "k123"})
assert bc.monetize(None, "Ganni") is None
assert bc.monetize("", "Ganni") == ""

# ── 8. NO key → legacy Sovrn-wrapped links are UNWRAPPED to direct URLs ───────
# (Sovrn account denied 2026-07-23: without this, carried-forward/curated
# products would keep dead viglink redirects for weeks after key removal.)
bc = load({})
legacy = "https://redirect.viglink.com/?" + urllib.parse.urlencode(
    {"key": "0fef505edead", "u": RAW, "cuid": "loupeapp"}
)
assert bc.monetize(legacy, "Peachy Den") == RAW
assert bc.monetize(legacy) == RAW
assert bc.monetize(RAW, "Peachy Den") == RAW  # raw stays raw, still

# ── 9. UTM attribution on EVERY outbound link (added 2026-07-29) ─────────────
# Before this, only the 400 Gemini partner links carried a UTM: 7,914 of 8,314
# clicks landed on a brand's store as plain "Direct" traffic, so a brand could
# not see Loupe in their own analytics — the entire partnership pitch.
bc = load({})
UTM = "utm_source=loupe&utm_medium=referral&utm_campaign=app"
assert bc.LOUPE_UTM == UTM

# 9a. A bare URL gains the UTM with '?'.
tagged = bc.add_utm(RAW)
assert tagged == RAW + "?" + UTM, tagged

# 9b. A URL that ALREADY has a query string gains it with '&' (and keeps its own).
withq = "https://peachyden.com/products/kylie-dress?variant=42&ref=ig"
t2 = bc.add_utm(withq)
assert t2 == withq + "&" + UTM, t2
assert "?variant=42&ref=ig&utm_source=loupe" in t2

# 9c. IDEMPOTENT — running twice never duplicates.
assert bc.add_utm(tagged) == tagged
assert bc.add_utm(bc.add_utm(bc.add_utm(RAW))) == tagged
assert t2.count("utm_source=") == 1 and bc.add_utm(t2).count("utm_source=") == 1

# 9d. A more specific tag already on the URL WINS (partner links keep their own
#     campaign) — we never overwrite an existing utm_* value.
gemini = "https://geminishop.com/products/x?utm_source=loupe&utm_medium=app&utm_campaign=gemini"
assert bc.add_utm(gemini) == gemini
# ...and building a partner link from scratch reproduces exactly that string.
assert bc.add_utm("https://geminishop.com/products/x",
                  "utm_source=loupe&utm_medium=app&utm_campaign=gemini") == gemini

# 9e. Composes with monetize(): no key -> clean direct link that KEEPS the UTM.
assert bc.monetize(bc.add_utm(RAW), "Peachy Den") == tagged
# ...with a brand template, the UTM survives inside the encoded destination.
bc = load({"BRAND_AFFILIATE_TEMPLATES": json.dumps(AWIN_TPL)})
dm = bc.monetize(bc.add_utm(DM_RAW), "Damson Madder")
assert dm.startswith("https://www.awin1.com/cread.php?awinmid=114966")
assert urllib.parse.quote(bc.add_utm(DM_RAW), safe="") in dm
assert "utm_source%3Dloupe" in dm
assert bc.monetize(dm, "Damson Madder") == dm          # still idempotent
# ...and with the Sovrn catch-all, the UTM rides inside u=.
bc = load({"SOVRN_API_KEY": "k123"})
sv = bc.monetize(bc.add_utm(RAW), "Peachy Den")
assert sv.startswith("https://redirect.viglink.com/?")
assert urllib.parse.unquote(sv.split("u=", 1)[1].split("&", 1)[0]) == tagged

# 9f. Fails soft on junk input (curated.json rows can carry anything).
assert bc.add_utm(None) is None
assert bc.add_utm("") == ""
assert bc.add_utm(RAW, "") == RAW
assert bc.add_utm(RAW, None) == RAW
# A fragment is preserved and the query goes BEFORE it.
frag = bc.add_utm("https://x.com/p/a#reviews")
assert frag == "https://x.com/p/a?" + UTM + "#reviews", frag

# Leave the process env clean for anything running after us in the same shell.
for key in ("SOVRN_API_KEY", "SOVRN_CUID", "BRAND_AFFILIATE_TEMPLATES"):
    os.environ.pop(key, None)

print("test_affiliate_wrappers: 9 fixture groups passed")
