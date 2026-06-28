#!/usr/bin/env python3
"""
Loupe — Aniqa-approval pipeline for user-submitted brand requests.

Brand requests are NEVER auto-added. Every valid candidate is emailed to Aniqa
with one-click Approve / Deny buttons; a brand is added to the catalog ONLY
after she approves. She is in sole control of what goes live.

Runs in CI (GitHub Actions) BEFORE build_catalog.py, in TWO stages each run:

  STAGE A "review":
    Pull every status='pending' row from Supabase `brand_requests`. Resolve each
    to a live Shopify store, run it through the safety gate.
      • VALID   → status='awaiting_review'; store resolved_brand / resolved_domain
                  / candidate_currency / sample (2-3 products) / review_emailed_at;
                  EMAIL ANIQA with Approve + Deny links (Resend REST API).
      • INVALID → status='rejected' + reject_reason (unresolvable, exact dupe,
                  mega-retailer denylist, unsupported currency, too small). No
                  email is sent for junk — Aniqa's inbox stays clean.

  STAGE B "add":
    Pull every status='approved' row (Aniqa approved via the Edge Function).
      • Append {brand, domain, currency} to brands.json (deduped by domain),
        set status='added' + added_at, and collect into new_brands.json so the
        existing notify step pings the requester + that brand's followers.

The Approve / Deny links point at the Supabase Edge Function (brand-approval),
which renders a confirmation page; ONLY a POST from that page flips the row to
'approved' / 'rejected' (a GET never mutates — defeats mail-scanner/prefetch
auto-approval). Each link carries an HMAC token bound to a per-request random
`approval_nonce` and an expiry (`exp`), so a leaked/forwarded/scanner-cached URL
can't be forged, used after expiry, or replayed once the decision rotates the
nonce.

Pure standard library — no pip install. Talks to Supabase, the resolver targets,
and the Resend API over urllib.

Env (GitHub Action secrets):
  SUPABASE_URL          e.g. https://abcd.supabase.co
  SUPABASE_SERVICE_KEY  service-role key (bypasses RLS)
  RESEND_API_KEY        Resend API key (email send). Missing → skip email, still
                        set awaiting_review.
  ANIQA_EMAIL           reviewer's email (the "to")
  EMAIL_FROM            verified sender, e.g. "Loupe <brands@loupe.app>"
  APPROVAL_BASE_URL     deployed Edge Function URL (the Approve/Deny target)
  APPROVAL_HMAC_SECRET  shared secret used to sign the Approve/Deny tokens

If SUPABASE_URL / SUPABASE_SERVICE_KEY are missing the script prints a notice and
exits 0 (no-op), so local runs and the daily build never fail. It always exits 0
unless something catastrophic happens.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
BRANDS_FILE = HERE / "brands.json"
NEW_BRANDS_FILE = HERE / "new_brands.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

# ── Email / approval env ──────────────────────────────────────────────────────
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
ANIQA_EMAIL = os.environ.get("ANIQA_EMAIL", "").strip()
EMAIL_FROM = os.environ.get("EMAIL_FROM", "").strip() or "Loupe <onboarding@resend.dev>"
APPROVAL_BASE_URL = os.environ.get("APPROVAL_BASE_URL", "").strip()
APPROVAL_HMAC_SECRET = os.environ.get("APPROVAL_HMAC_SECRET", "").strip()

RESEND_API_URL = "https://api.resend.com/emails"

# Approve/Deny links expire this many seconds after they're minted (14 days).
# Past expiry the Edge Function refuses the click — a forwarded / scanner-cached
# link can't be used forever.
APPROVAL_TTL_SECONDS = 14 * 24 * 60 * 60

# SSRF guard: hard cap on bytes we read from any resolver fetch. brand_text is
# attacker-controlled, so a resolved host could be a hostile server trying to
# stream gigabytes or smuggle a redirect to an internal address.
MAX_FETCH_BYTES = 2 * 1024 * 1024  # 2 MiB

# Brand palette (flat colors, NO gradients).
PINK = "#F3CBF0"
BLACK = "#1A1A1A"
WHITE = "#FFFFFF"
GREEN = "#1F8A4C"
RED = "#C0392B"

# Minimum number of valid, shoppable products for a store to count as "real".
MIN_PRODUCTS = 5

# ── Off-brand denylist ────────────────────────────────────────────────────────
# Mainstream / fast-fashion / marketplace stores. Loupe is a niche-brand
# discovery app — these dilute the catalog and (for marketplaces) aren't a
# single brand at all. Matched against the resolved domain's registrable part
# AND the normalized request text, so "Zara", "zara.com" and "shop ASOS" all
# get caught.
DENYLIST = {
    "amazon", "ebay", "etsy", "aliexpress", "alibaba", "temu", "shein",
    "zara", "hm", "handm", "asos", "boohoo", "prettylittlething", "plt",
    "walmart", "target", "macys", "nordstrom", "shopbop", "revolve",
    "urbanoutfitters", "forever21", "uniqlo", "gap", "oldnavy", "bananarepublic",
    "shopify", "wish", "etsystudio", "depop", "vinted", "poshmark", "mercari",
    "nastygal", "missguided", "fashionnova", "romwe", "cider", "zaful",
    "primark", "hollister", "abercrombie", "americaneagle", "victoriassecret",
    "anthropologie", "freepeople", "mango", "bershka", "pullandbear", "stradivarius",
    "topshop", "riverisland", "newlook", "matalan", "next",
    "farfetch", "ssense", "netaporter", "mytheresa", "lyst", "thereup", "therealreal",
    "tjmaxx", "marshalls", "kohls", "jcpenney", "dillards", "bloomingdales",
    "saksfifthavenue", "saks", "neimanmarcus", "costco", "wayfair", "overstock",
    "google", "facebook", "instagram", "tiktok", "pinterest",
}

# Common multi-word fast-fashion / marketplace names that don't collapse cleanly
# to a single denylist token; checked against the normalized request text.
DENYLIST_PHRASES = [
    "h and m", "h m", "old navy", "banana republic", "american eagle",
    "victoria secret", "victorias secret", "free people", "river island",
    "new look", "saks fifth avenue", "neiman marcus", "pretty little thing",
    "nasty gal", "fashion nova", "net a porter", "the real real", "real real",
    "urban outfitters", "pull and bear",
]


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────────────────

def fetch_json(url, timeout=20):
    """GET a TRUSTED URL (constant, not attacker-derived) and parse JSON.
    Used only for Supabase/Resend-style endpoints we control. For brand-resolver
    fetches built from user input use fetch_json_guarded()."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(MAX_FETCH_BYTES + 1)[:MAX_FETCH_BYTES].decode("utf-8"))


# ──────────────────────────────────────────────────────────────────────────────
# SSRF-hardened resolver fetch
# ──────────────────────────────────────────────────────────────────────────────

class SSRFError(Exception):
    """A resolver fetch was refused because the target looked unsafe."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects on resolver fetches: a hostile store could 30x
    us to http://169.254.169.254/ (cloud metadata) or an internal host, slipping
    past the up-front host vetting. Any redirect becomes an SSRFError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SSRFError(f"redirect to {newurl!r} refused")


_RESOLVER_OPENER = urllib.request.build_opener(_NoRedirect)


def _host_is_public(host):
    """True only if EVERY address `host` resolves to is a public, routable IP.
    Rejects loopback / private / link-local / reserved / multicast ranges and
    bare IP-literal hosts (a niche Shopify brand is always a real DNS name)."""
    if not host:
        return False
    # Reject IP-literal hosts outright — brand requests are names, never raw IPs,
    # and an IP literal is the obvious SSRF vector (e.g. 127.0.0.1, 169.254.169.254).
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        # Reject anything that isn't a globally-routable public address.
        if (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified or
                not ip.is_global):
            return False
    return True


def fetch_json_guarded(url, timeout=20):
    """GET a resolver URL built from user-controlled brand_text and parse JSON,
    with SSRF protections:
      • the host must be a DNS name resolving ONLY to public IPs,
      • redirects are NOT followed (a 30x to an internal host raises),
      • the response body read is capped at MAX_FETCH_BYTES.
    Raises SSRFError on a refused target; other failures raise as before."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise SSRFError(f"non-https scheme {parsed.scheme!r}")
    host = parsed.hostname or ""
    if not _host_is_public(host):
        raise SSRFError(f"host {host!r} did not resolve to a public address")
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with _RESOLVER_OPENER.open(req, timeout=timeout) as resp:
        # Belt-and-suspenders: if a redirect somehow slipped through, the final
        # URL host must equal the one we vetted.
        final_host = urllib.parse.urlsplit(resp.geturl()).hostname or ""
        if final_host.lower() != host.lower():
            raise SSRFError(f"final host {final_host!r} != requested {host!r}")
        raw = resp.read(MAX_FETCH_BYTES + 1)
    if len(raw) > MAX_FETCH_BYTES:
        raise SSRFError("response body exceeded size cap")
    return json.loads(raw.decode("utf-8"))


def _sb_headers(extra=None):
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if extra:
        h.update(extra)
    return h


def sb_get(path):
    """GET {SUPABASE_URL}/rest/v1/<path>. Returns parsed JSON (list)."""
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    req = urllib.request.Request(url, headers=_sb_headers())
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else []


def sb_patch(path, payload):
    """PATCH {SUPABASE_URL}/rest/v1/<path> with a JSON body. return=minimal."""
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="PATCH",
        headers=_sb_headers(
            {"Content-Type": "application/json", "Prefer": "return=minimal"}
        ),
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Text / domain normalization
# ──────────────────────────────────────────────────────────────────────────────

def strip_accents(text):
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_name(text):
    """Lowercase, strip accents, collapse to [a-z0-9 ] with single spaces."""
    t = strip_accents(text or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def slugify(brand):
    """Matches build_catalog.py's slugify."""
    return "".join(c if c.isalnum() else "-" for c in (brand or "").lower()).strip("-")


def registrable_label(domain):
    """The brand-ish label of a domain: host minus www, minus TLD.
    e.g. 'www.shop-peche.com' -> 'shoppeche', 'becandbridge.com.au' -> 'becandbridge'."""
    host = domain.lower()
    host = re.sub(r"^www\.", "", host)
    parts = host.split(".")
    if not parts:
        return host
    # Drop the TLD (and a country second-level like .com.au / .co.uk).
    label = parts[0]
    return re.sub(r"[^a-z0-9]", "", label)


def extract_host(text):
    """If text contains a URL/domain, return its bare host (no scheme/www/path).
    Otherwise return None."""
    t = text.strip()
    # Has a scheme or an obvious domain token.
    m = re.search(r"https?://([^/\s]+)", t, re.I)
    if m:
        host = m.group(1)
    else:
        # bare domain like "shop-peche.com" or "becandbridge.com.au"
        m = re.search(r"\b([a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9\-]+)+)\b", t, re.I)
        if not m:
            return None
        host = m.group(1)
    host = host.split("/")[0].split("?")[0].split("#")[0]
    host = re.sub(r"^www\.", "", host.lower()).strip(".")
    # Must look like a real host (has a dot and a plausible TLD).
    if "." not in host:
        return None
    return host


def candidate_domains(name):
    """Ordered candidate Shopify domains derived from a brand name (no URL given).
    Small list (<=6). Accents stripped, punctuation removed."""
    norm = normalize_name(name)          # e.g. "realisation par"
    if not norm:
        return []
    words = norm.split()
    nospace = "".join(words)             # realisationpar
    hyphen = "-".join(words)             # realisation-par

    # ccTLD hints from the request text.
    low = (name or "").lower()
    hints_au = any(h in low for h in ("australia", ".au", "au)", " au"))
    hints_uk = any(h in low for h in ("uk", "london", "britain", ".co.uk"))

    cands = []

    def add(d):
        if d and d not in cands:
            cands.append(d)

    add(f"{nospace}.com")
    if hyphen != nospace:
        add(f"{hyphen}.com")
    add(f"the{nospace}.com")
    add(f"{nospace}.co")
    if hints_uk:
        add(f"{nospace}.co.uk")
    if hints_au:
        add(f"{nospace}.com.au")
    # studio/shop variants are common for niche labels.
    add(f"{nospace}studio.com")

    return cands[:6]


# ──────────────────────────────────────────────────────────────────────────────
# Store validation
# ──────────────────────────────────────────────────────────────────────────────

def store_products(domain):
    """Return the products list for a domain's Shopify feed, or None on any error.
    A valid store returns JSON with a non-empty 'products' array."""
    try:
        data = fetch_json_guarded(f"https://{domain}/products.json?limit=10")
    except (SSRFError, urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    products = data.get("products")
    if not isinstance(products, list) or not products:
        return None
    return products


def detect_currency(domain, fx_table):
    """Return (currency, supported_bool, detected_bool).
    Tries https://<domain>/meta.json -> {'currency': 'USD'}. Falls back to USD."""
    try:
        meta = fetch_json_guarded(f"https://{domain}/meta.json")
        cur = (meta.get("currency") or "").strip().upper() if isinstance(meta, dict) else ""
    except (SSRFError, urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError):
        cur = ""
    if not cur:
        return ("USD", True, False)          # undetectable -> default USD
    return (cur, cur in fx_table, True)


def resolve_domain(brand_text, fx_table):
    """Resolve a request to a working Shopify domain.
    Returns (domain, products) on success, or (None, None) if nothing validates."""
    host = extract_host(brand_text)
    if host:
        candidates = [host]
    else:
        candidates = candidate_domains(brand_text)

    for dom in candidates:
        products = store_products(dom)
        if products:
            return (dom, products)
        time.sleep(0.3)   # be polite between candidate probes
    return (None, None)


# ──────────────────────────────────────────────────────────────────────────────
# Safety gate
# ──────────────────────────────────────────────────────────────────────────────

def is_denylisted(brand_text, domain):
    """True if the request is a mainstream/fast-fashion/marketplace brand."""
    norm = normalize_name(brand_text)
    norm_nospace = norm.replace(" ", "")
    label = registrable_label(domain) if domain else ""

    if label and label in DENYLIST:
        return True
    if norm_nospace and norm_nospace in DENYLIST:
        return True
    # token-level (e.g. "shop asos" -> tokens include "asos")
    for tok in norm.split():
        if tok in DENYLIST:
            return True
    for phrase in DENYLIST_PHRASES:
        if phrase in norm:
            return True
    return False


def already_on_loupe(domain, brand_text, brands_cfg):
    """True if this domain or normalized brand name is already in brands.json."""
    norm_req = normalize_name(brand_text).replace(" ", "")
    dom_l = (domain or "").lower()
    dom_label = registrable_label(domain) if domain else ""
    for b in brands_cfg["brands"]:
        bdom = (b.get("domain") or "").lower()
        if dom_l and bdom == dom_l:
            return True
        if dom_label and registrable_label(bdom) == dom_label:
            return True
        if norm_req and normalize_name(b.get("brand", "")).replace(" ", "") == norm_req:
            return True
    return False


def display_name(brand_text, domain):
    """A clean display name. If the request was a URL, derive from the domain;
    otherwise title-case the request text (preserving its real words)."""
    host = extract_host(brand_text)
    cleaned = re.sub(r"https?://\S+", "", brand_text or "").strip()
    # If the text is essentially just a URL/domain, build a name from the label.
    if host and (not cleaned or normalize_name(cleaned) == "" or
                 normalize_name(cleaned) == registrable_label(domain)):
        label = registrable_label(domain)
        # split camelCase-ish runs is overkill; just title-case the label words
        words = re.findall(r"[a-z]+|[0-9]+", label)
        return " ".join(w.capitalize() for w in words) if words else label.capitalize()
    # Otherwise title-case the request words.
    name = cleaned or brand_text or ""
    return " ".join(w.capitalize() if not w.isupper() else w for w in name.split()).strip()


# ──────────────────────────────────────────────────────────────────────────────
# Sample products (for the review email)
# ──────────────────────────────────────────────────────────────────────────────

def sample_products(products, currency, n=3):
    """Up to `n` lightweight sample products for the review email.
    Each: {name, price, image}. price is a display string in the store currency."""
    out = []
    for p in products:
        if len(out) >= n:
            break
        name = (p.get("title") or "").strip()
        if not name:
            continue
        # First non-empty variant price.
        price_val = None
        for v in p.get("variants") or []:
            pv = v.get("price")
            if pv:
                try:
                    price_val = float(pv)
                    break
                except (TypeError, ValueError):
                    continue
        # First image.
        image = None
        for im in p.get("images") or []:
            if im.get("src"):
                image = im.get("src")
                break
        if not image:
            continue
        price_str = f"{currency} {price_val:.0f}" if price_val is not None else ""
        out.append({"name": name, "price": price_str, "image": image})
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Approval tokens + email
# ──────────────────────────────────────────────────────────────────────────────

def new_nonce():
    """A fresh per-request random nonce, set on the row at Stage A and woven into
    every Approve/Deny token. Cleared/rotated by the Edge Function on a successful
    decision so the link can't be replayed."""
    return secrets.token_hex(16)


def _exp_for_nonce():
    """Epoch-seconds expiry for a freshly minted approval nonce/token."""
    return int(time.time()) + APPROVAL_TTL_SECONDS


def approval_token(request_id, action, nonce, exp, secret=None):
    """HMAC-SHA256 hex of "{request_id}:{action}:{nonce}:{exp}" keyed by
    APPROVAL_HMAC_SECRET. The Edge Function loads the row's nonce, checks exp,
    recomputes this and constant-time compares on each click. Binding the nonce
    + exp means a leaked/forwarded link stops working once it expires or once the
    decision rotates the nonce."""
    key = (secret if secret is not None else APPROVAL_HMAC_SECRET).encode("utf-8")
    msg = f"{request_id}:{action}:{nonce}:{exp}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def approval_link(request_id, action, nonce, exp):
    """Approve/Deny URL into the Edge Function, carrying the nonce-bound HMAC token
    and its expiry (epoch seconds)."""
    token = approval_token(request_id, action, nonce, exp)
    qs = urllib.parse.urlencode(
        {"rid": request_id, "action": action, "exp": exp, "token": token}
    )
    return f"{APPROVAL_BASE_URL}?{qs}"


def _esc(s):
    """Minimal HTML escaping for interpolated text."""
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def review_email_html(brand, domain, samples, request_id, nonce, exp):
    """Branded, flat-color (no-gradient) HTML for the Approve/Deny review email."""
    approve_url = approval_link(request_id, "approve", nonce, exp)
    deny_url = approval_link(request_id, "deny", nonce, exp)

    sample_cells = ""
    for s in samples:
        sample_cells += f"""
            <td width="33%" valign="top" style="padding:6px;text-align:center;">
              <img src="{_esc(s['image'])}" width="150" alt="{_esc(s['name'])}"
                   style="width:150px;height:200px;object-fit:cover;border-radius:8px;border:1px solid #eee;display:block;margin:0 auto;" />
              <div style="font-size:13px;color:{BLACK};margin-top:8px;line-height:1.3;">{_esc(s['name'])}</div>
              <div style="font-size:13px;color:#666;margin-top:2px;">{_esc(s['price'])}</div>
            </td>"""
    if not sample_cells:
        sample_cells = (
            f'<td style="padding:6px;text-align:center;color:#666;font-size:13px;">'
            f"(no sample products available)</td>"
        )

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:{WHITE};font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:{WHITE};padding:24px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
        <tr><td style="background:{PINK};border-radius:14px 14px 0 0;padding:22px 28px;">
          <div style="font-size:22px;font-weight:800;color:{BLACK};letter-spacing:0.5px;">Loupe</div>
          <div style="font-size:14px;color:{BLACK};margin-top:2px;">New brand request — your call</div>
        </td></tr>
        <tr><td style="border:1px solid {PINK};border-top:none;border-radius:0 0 14px 14px;padding:28px;background:{WHITE};">
          <div style="font-size:20px;font-weight:700;color:{BLACK};">{_esc(brand)}</div>
          <div style="font-size:14px;color:#666;margin-top:4px;">
            Store: <a href="https://{_esc(domain)}" style="color:{BLACK};">{_esc(domain)}</a>
          </div>
          <div style="font-size:14px;color:{BLACK};margin-top:18px;">A few of their products:</div>
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;">
            <tr>{sample_cells}</tr>
          </table>

          <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:28px;">
            <tr>
              <td width="50%" style="padding-right:6px;">
                <a href="{approve_url}"
                   style="display:block;background:{GREEN};color:{WHITE};text-align:center;
                          text-decoration:none;font-size:16px;font-weight:700;
                          padding:16px 0;border-radius:10px;">&#10003; Approve</a>
              </td>
              <td width="50%" style="padding-left:6px;">
                <a href="{deny_url}"
                   style="display:block;background:{BLACK};color:{WHITE};text-align:center;
                          text-decoration:none;font-size:16px;font-weight:700;
                          padding:16px 0;border-radius:10px;">&#10005; Deny</a>
              </td>
            </tr>
          </table>

          <div style="font-size:12px;color:#999;margin-top:22px;line-height:1.5;">
            Approve adds {_esc(brand)} to Loupe at the next catalog refresh and notifies the
            people who asked for it / follow it. Deny discards it. Nothing is added unless you approve.
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_review_email(brand, domain, samples, request_id, nonce, exp):
    """Send the Approve/Deny email to Aniqa via Resend. Fail-soft → returns bool.
    If RESEND_API_KEY / ANIQA_EMAIL / APPROVAL_BASE_URL are absent, logs + skips."""
    missing = [n for n, v in (
        ("RESEND_API_KEY", RESEND_API_KEY),
        ("ANIQA_EMAIL", ANIQA_EMAIL),
        ("APPROVAL_BASE_URL", APPROVAL_BASE_URL),
        ("APPROVAL_HMAC_SECRET", APPROVAL_HMAC_SECRET),
    ) if not v]
    if missing:
        print(f"    note: review email NOT sent for {brand!r} "
              f"(missing: {', '.join(missing)}). Row still set to awaiting_review.")
        return False

    payload = {
        "from": EMAIL_FROM,
        "to": [ANIQA_EMAIL],
        "subject": f"Loupe — approve {brand}?",
        "html": review_email_html(brand, domain, samples, request_id, nonce, exp),
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        RESEND_API_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp.read()
        print(f"    emailed Aniqa for review: {brand} ({domain})")
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        detail = ""
        if isinstance(e, urllib.error.HTTPError):
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                detail = ""
        print(f"    WARNING: review email failed for {brand!r} "
              f"({type(e).__name__}) {detail}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_new_brands(new_brands):
    payload = {"generatedAt": iso_now(), "brands": new_brands}
    NEW_BRANDS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_cfg():
    return json.loads(BRANDS_FILE.read_text(encoding="utf-8"))


def save_cfg(cfg):
    """Write brands.json preserving _comment / fx_to_usd / perBrand / order."""
    BRANDS_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── STAGE A: review ───────────────────────────────────────────────────────────

def stage_review(cfg):
    """Resolve + gate pending requests. Valid → awaiting_review + email Aniqa.
    Junk → rejected (no email). Never appends to brands.json."""
    fx_table = cfg["fx_to_usd"]

    try:
        pending = sb_get(
            "brand_requests?status=eq.pending&select=id,brand_text,user_id"
        )
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"  STAGE A: WARNING — could not reach Supabase "
              f"({type(e).__name__}: {e}). Skipping review stage.")
        return

    if not pending:
        print("  STAGE A: no pending brand requests.")
        return

    print(f"  STAGE A: {len(pending)} pending request(s).")
    queued, rejected = 0, 0

    for row in pending:
        req_id = row.get("id")
        brand_text = (row.get("brand_text") or "").strip()

        if not brand_text:
            _reject(req_id, "empty request")
            rejected += 1
            continue

        # 1) Resolve to a live Shopify domain.
        domain, products = resolve_domain(brand_text, fx_table)
        if not domain:
            _reject(req_id, "could not find a Shopify store for this brand")
            print(f"    rejected: {brand_text!r} -> no Shopify store found")
            rejected += 1
            continue

        # 2) SAFETY GATE.
        if already_on_loupe(domain, brand_text, cfg):
            _reject(req_id, "already on Loupe")
            print(f"    rejected: {brand_text!r} ({domain}) -> already on Loupe")
            rejected += 1
            continue
        if is_denylisted(brand_text, domain):
            _reject(req_id, "not a niche brand")
            print(f"    rejected: {brand_text!r} ({domain}) -> not a niche brand")
            rejected += 1
            continue
        if len(products) < MIN_PRODUCTS:
            _reject(req_id, "store too small")
            print(f"    rejected: {brand_text!r} ({domain}) -> store too small "
                  f"({len(products)} products)")
            rejected += 1
            continue
        currency, supported, detected = detect_currency(domain, fx_table)
        if detected and not supported:
            _reject(req_id, f"unsupported currency {currency}")
            print(f"    rejected: {brand_text!r} ({domain}) -> unsupported "
                  f"currency {currency}")
            rejected += 1
            continue
        if not detected:
            print(f"    note: currency undetectable for {domain}, defaulting to USD")

        # 3) VALID → awaiting_review + email Aniqa. NOT added to brands.json.
        #    STATE IS THE SOURCE OF TRUTH: transition the row to awaiting_review
        #    (minting + storing the nonce/expiry) BEFORE sending the email. The
        #    email is a separate, idempotent step keyed off review_emailed_at: it
        #    is stamped only AFTER the send succeeds. So a PATCH failure leaves the
        #    row pending (retried next run, no email yet → no duplicate), and an
        #    email failure leaves review_emailed_at NULL so the sweep re-sends.
        disp = display_name(brand_text, domain)
        samples = sample_products(products, currency, n=3)
        nonce = new_nonce()
        exp = _exp_for_nonce()
        if not _set_awaiting_review(req_id, disp, domain, currency, samples, nonce, exp):
            # Could not persist state → do NOT email (state must lead). Leave the
            # row 'pending' so the next run retries cleanly without a stray email.
            print(f"    deferred: {brand_text!r} -> could not set awaiting_review; "
                  f"will retry next run (no email sent).")
            continue
        # State persisted. Now (and only now) send the email; stamp the marker on
        # success so it's sent exactly once.
        if send_review_email(disp, domain, samples, req_id, nonce, exp):
            _stamp_review_emailed(req_id)
        print(f"    awaiting review: {brand_text!r} -> {disp} ({domain}, {currency})")
        queued += 1

    # Re-send / re-mint sweep: catch awaiting_review rows whose email never sent
    # (review_emailed_at unset) or whose approval nonce has expired (a stale link
    # that can no longer be approved). Re-mints a fresh nonce/expiry and re-emails.
    resent = _resend_sweep(cfg)

    print(f"  STAGE A summary: {queued} emailed for review, {rejected} auto-rejected, "
          f"{resent} re-sent.")


# ── STAGE B: add ──────────────────────────────────────────────────────────────

def stage_add(cfg):
    """Append Aniqa-approved brands to brands.json, set status='added', and
    collect them into new_brands.json for the notify step. Returns the list of
    new brands (for new_brands.json).

    SELF-HEALING (P1-8c): also re-scans rows already marked 'added' (and any left
    'approved') and re-appends any whose brand is NOT in the current brands.json —
    so a lost commit (a failed git push after the row flipped to 'added') can't
    permanently strand an approved brand out of the catalog. Recovered 'added'
    rows are re-appended but NOT re-notified (the requester/followers were already
    pinged when the brand first went added). Idempotent: a brand already present
    in brands.json is never double-appended."""
    try:
        approved = sb_get(
            "brand_requests?status=eq.approved"
            "&select=id,brand_text,user_id,resolved_brand,resolved_domain,candidate_currency"
        )
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"  STAGE B: WARNING — could not reach Supabase "
              f"({type(e).__name__}: {e}). Skipping add stage.")
        return []

    # Self-heal: pull rows already marked 'added' so we can detect any whose brand
    # fell out of brands.json (e.g. a build whose commit/push was lost). A reach
    # failure here is non-fatal — we still process the 'approved' rows.
    try:
        added_rows = sb_get(
            "brand_requests?status=eq.added"
            "&select=id,brand_text,user_id,resolved_brand,resolved_domain,candidate_currency"
        )
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"  STAGE B: note — could not query 'added' rows for self-heal "
              f"({type(e).__name__}); proceeding with approved only.")
        added_rows = []

    if not approved and not added_rows:
        print("  STAGE B: no approved brands to add.")
        return []

    # Existing domains (lowercased) for dedupe — the single idempotency guard.
    existing = {(b.get("domain") or "").lower() for b in cfg["brands"]}
    new_brands = []
    added = 0      # newly-approved brands appended (these DO notify)
    healed = 0     # already-'added' brands re-appended after a lost commit

    print(f"  STAGE B: {len(approved)} approved brand(s) to add"
          + (f", {len(added_rows)} 'added' row(s) to verify." if added_rows else "."))

    # 1) Newly-approved rows → append + mark 'added' + queue a notification.
    for row in approved:
        req_id = row.get("id")
        user_id = row.get("user_id")
        brand = (row.get("resolved_brand") or "").strip()
        domain = (row.get("resolved_domain") or "").strip()
        currency = (row.get("candidate_currency") or "USD").strip() or "USD"

        if not domain or not brand:
            # Shouldn't happen (review stage set these), but be defensive.
            print(f"    skip: approved id {req_id} missing resolved_brand/domain.")
            _mark_added(req_id)   # close it out so we don't loop on it forever
            continue

        if domain.lower() in existing:
            # Already on Loupe (e.g. approved twice / added meanwhile). Close it.
            print(f"    skip: {brand} ({domain}) already in brands.json.")
            _mark_added(req_id)
            continue

        cfg["brands"].append({"brand": brand, "domain": domain, "currency": currency})
        existing.add(domain.lower())
        _mark_added(req_id)
        new_brands.append({
            "brand": brand,
            "domain": domain,
            "user_id": user_id,
            "request_id": req_id,
        })
        print(f"    added: {brand} ({domain}, {currency})")
        added += 1

    # 2) SELF-HEAL: 'added' rows whose brand is missing from brands.json get
    #    re-appended (a lost commit stranded them). They are NOT re-notified and
    #    their status is already 'added', so nothing else changes. The `existing`
    #    set (updated above) keeps this idempotent against the rows added in (1).
    for row in added_rows:
        req_id = row.get("id")
        brand = (row.get("resolved_brand") or "").strip()
        domain = (row.get("resolved_domain") or "").strip()
        currency = (row.get("candidate_currency") or "USD").strip() or "USD"
        if not domain or not brand:
            continue
        if domain.lower() in existing:
            continue  # present (the normal case) → nothing to heal
        # Missing despite being 'added' → re-append (recover the lost commit).
        cfg["brands"].append({"brand": brand, "domain": domain, "currency": currency})
        existing.add(domain.lower())
        print(f"    re-added (self-heal, lost commit): {brand} ({domain}, {currency})")
        healed += 1

    if added or healed:
        # Dedupe brands.json by domain (belt-and-suspenders) preserving order.
        seen, deduped = set(), []
        for b in cfg["brands"]:
            d = (b.get("domain") or "").lower()
            if d in seen:
                continue
            seen.add(d)
            deduped.append(b)
        cfg["brands"] = deduped
        save_cfg(cfg)

    print(f"  STAGE B summary: {added} brand(s) added"
          + (f", {healed} re-added (self-heal)." if healed else "."))
    return new_brands


def main():
    # Missing creds → no-op, exit 0.
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("process_requests: SUPABASE_URL / SUPABASE_SERVICE_KEY not set — "
              "skipping brand-request processing (no-op).")
        return 0

    cfg = load_cfg()

    print("process_requests: starting two-stage run (review → add).")
    # STAGE A: email Aniqa about new candidates (no brands.json change).
    stage_review(cfg)
    # STAGE B: add the ones she already approved.
    new_brands = stage_add(cfg)

    # Hand off to the notify step (run-local).
    write_new_brands(new_brands)
    print(f"\nWrote {len(new_brands)} newly-added brand(s) -> {NEW_BRANDS_FILE.name}")
    return 0


# ── Supabase status transitions ───────────────────────────────────────────────

def _set_awaiting_review(req_id, resolved_brand, resolved_domain, currency,
                         samples, nonce, exp):
    """Transition a row to awaiting_review and store the resolved fields + a fresh
    approval nonce/expiry. Deliberately does NOT set review_emailed_at — that is
    stamped separately, only after the email actually sends, so the email is sent
    exactly once. Returns True iff the PATCH succeeded (caller must not email on
    a False)."""
    # NOTE: `exp` is intentionally NOT persisted as its own column (no schema
    # change beyond the columns already in use). The link's expiry is derived from
    # review_emailed_at + APPROVAL_TTL_SECONDS in the re-send sweep, and the
    # authoritative exp travels inside the signed Approve/Deny URL the Edge
    # Function verifies. `exp` is accepted here only to keep the signature uniform.
    try:
        sb_patch(
            f"brand_requests?id=eq.{urllib.parse.quote(str(req_id))}",
            {
                "status": "awaiting_review",
                "resolved_brand": resolved_brand,
                "resolved_domain": resolved_domain,
                "candidate_currency": currency,
                "sample": samples,
                "approval_nonce": nonce,
                "processed_at": iso_now(),
            },
        )
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"    WARNING: could not PATCH awaiting_review for id {req_id} "
              f"({type(e).__name__})")
        return False


def _stamp_review_emailed(req_id):
    """Mark that the reviewer email was successfully sent (idempotency marker).
    Kept separate from the state transition so a PATCH/email failure can never
    cause a duplicate reviewer email."""
    try:
        sb_patch(
            f"brand_requests?id=eq.{urllib.parse.quote(str(req_id))}",
            {"review_emailed_at": iso_now()},
        )
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"    WARNING: could not stamp review_emailed_at for id {req_id} "
              f"({type(e).__name__})")
        return False


def _parse_iso(ts):
    """Parse an ISO-8601 'YYYY-MM-DDTHH:MM:SSZ' (or +00:00) timestamp to epoch
    seconds, or None if it can't be parsed. Tolerant of a trailing 'Z'."""
    if not ts or not isinstance(ts, str):
        return None
    t = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _resend_sweep(cfg):
    """Re-mint + re-send the reviewer email for awaiting_review rows that need it:
      • review_emailed_at is NULL          → the email never went out (an earlier
        send failed, or RESEND was unconfigured when the row was queued), or
      • review_emailed_at + TTL is in the past → the Approve/Deny link can no
        longer be used, so a fresh nonce + a new email are needed.
    Expiry is derived from review_emailed_at (the email is sent within seconds of
    minting, so emailed_at + TTL == the link's real exp) — no extra column needed.
    Re-mints a fresh nonce first (state leads), then re-emails and stamps
    review_emailed_at on success. Fail-soft; returns the count actually re-sent."""
    try:
        rows = sb_get(
            "brand_requests?status=eq.awaiting_review"
            "&select=id,resolved_brand,resolved_domain,candidate_currency,sample,"
            "review_emailed_at"
        )
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"    note: re-send sweep skipped — could not query awaiting_review "
              f"({type(e).__name__}).")
        return 0

    if not rows:
        return 0

    now = int(time.time())
    resent = 0
    for row in rows:
        req_id = row.get("id")
        emailed_at = row.get("review_emailed_at")
        emailed_epoch = _parse_iso(emailed_at)
        expired = (emailed_epoch is not None and
                   emailed_epoch + APPROVAL_TTL_SECONDS <= now)
        needs = (not emailed_at) or expired
        if not needs:
            continue

        brand = (row.get("resolved_brand") or "").strip()
        domain = (row.get("resolved_domain") or "").strip()
        currency = (row.get("candidate_currency") or "USD").strip() or "USD"
        samples = row.get("sample") or []
        if not brand or not domain:
            # Nothing to email about (shouldn't happen for awaiting_review) — skip.
            continue

        # Re-mint a fresh nonce + expiry (invalidates any stale link) BEFORE email.
        nonce = new_nonce()
        exp = _exp_for_nonce()
        if not _remint_nonce(req_id, nonce):
            continue  # couldn't persist the new nonce → don't email with a stale one
        reason = "never emailed" if not emailed_at else "link expired"
        if send_review_email(brand, domain, samples, req_id, nonce, exp):
            _stamp_review_emailed(req_id)
            resent += 1
            print(f"    re-sent review email ({reason}): {brand} ({domain})")
        else:
            print(f"    note: re-send still failing ({reason}) for {brand} ({domain}); "
                  f"will retry next run.")

    return resent


def _remint_nonce(req_id, nonce):
    """Store a freshly minted approval nonce on an awaiting_review row (used by the
    re-send sweep). The expiry travels inside the signed link, so only the nonce
    is persisted. Returns True on success."""
    try:
        sb_patch(
            f"brand_requests?id=eq.{urllib.parse.quote(str(req_id))}",
            {"approval_nonce": nonce},
        )
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"    WARNING: could not re-mint nonce for id {req_id} "
              f"({type(e).__name__})")
        return False


def _mark_added(req_id):
    try:
        sb_patch(
            f"brand_requests?id=eq.{urllib.parse.quote(str(req_id))}",
            {"status": "added", "added_at": iso_now()},
        )
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"    WARNING: could not PATCH added for id {req_id} "
              f"({type(e).__name__})")


def _reject(req_id, reason):
    try:
        sb_patch(
            f"brand_requests?id=eq.{urllib.parse.quote(str(req_id))}",
            {
                "status": "rejected",
                "reject_reason": reason,
                "processed_at": iso_now(),
            },
        )
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"    WARNING: could not PATCH reject for id {req_id} "
              f"({type(e).__name__})")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never let the daily build die here
        print(f"process_requests: unexpected error ({type(e).__name__}: {e}); "
              f"exiting 0 so the catalog still builds.", file=sys.stderr)
        try:
            write_new_brands([])
        except Exception:
            pass
        sys.exit(0)
