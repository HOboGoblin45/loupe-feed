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

# Leave the process env clean for anything running after us in the same shell.
for key in ("SOVRN_API_KEY", "SOVRN_CUID", "BRAND_AFFILIATE_TEMPLATES"):
    os.environ.pop(key, None)

print("test_affiliate_wrappers: 7 fixture groups passed")
