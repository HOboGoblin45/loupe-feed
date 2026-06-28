#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# Loupe — Server-side price-drop push notifications
#
# Runs daily (GitHub Action). For every signed-in user who has a saved Dresser
# item that is now cheaper than when they saved it, sends an Expo push — even
# when the app is closed. De-duplicated via saved_items.last_notified_price so a
# user is only pinged once per price level.
#
# Reads:   the published catalog (current prices) + Supabase (profiles, saved_items)
# Writes:  Supabase saved_items.last_notified_price (mark as notified)
#          Expo push service (the notification itself)
#
# Stdlib only — no pip install needed in CI.
#
# Required env:
#   SUPABASE_URL          e.g. https://aruguxhcexfvyyfboklt.supabase.co
#   SUPABASE_SERVICE_KEY  service-role key (bypasses RLS; keep secret).
#                         The legacy name SUPABASE_SERVICE_ROLE_KEY is also
#                         accepted as a fallback so old workflow secrets keep
#                         working.
# Optional env:
#   CATALOG_URL                 defaults to the jsDelivr-published catalog (the
#                               same generation the app reads)
# ─────────────────────────────────────────────────────────────────────────────

import json
import os
import sys
import urllib.error
import urllib.request

# Default to the SAME jsDelivr URL the app reads so price comparisons run against
# the exact catalog generation users see (raw.githubusercontent can serve a
# different, un-CDN'd generation).
CATALOG_URL = os.environ.get(
    "CATALOG_URL",
    "https://cdn.jsdelivr.net/gh/HOboGoblin45/loupe-feed@main/loupe-feed/catalog.json",
)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
# Standardize on SUPABASE_SERVICE_KEY (matches process_requests.py /
# notify_brand_requests.py); accept the old SUPABASE_SERVICE_ROLE_KEY as a fallback.
SERVICE_KEY = (
    os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
)
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

TIMEOUT = 30


def _req(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def sb(path, method="GET", params="", body=None, extra_headers=None):
    """Call the Supabase REST (PostgREST) API with the service-role key."""
    headers = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        url += f"?{params}"
    return _req(url, method=method, headers=headers, body=body)


def load_catalog_prices():
    """Return {product_id: price} from the published catalog."""
    data = _req(CATALOG_URL)
    products = data.get("products", data) if isinstance(data, dict) else data
    prices = {}
    for p in products:
        pid = str(p.get("id"))
        price = p.get("price")
        if pid and isinstance(price, (int, float)):
            prices[pid] = float(price)
    return prices


def load_push_tokens():
    """Return {user_id: expo_push_token} for users who opted in."""
    rows = sb("profiles", params="select=id,push_token&push_token=not.is.null") or []
    return {r["id"]: r["push_token"] for r in rows if r.get("push_token")}


def load_saved_items():
    """All saved Dresser items with the fields we need to detect drops."""
    return (
        sb(
            "saved_items",
            params=(
                "select=user_id,product_id,price_at_save,last_notified_price,product"
            ),
        )
        or []
    )


def send_expo_pushes(messages):
    """Send Expo push messages in batches of 100."""
    sent = 0
    for i in range(0, len(messages), 100):
        batch = messages[i : i + 100]
        try:
            _req(
                EXPO_PUSH_URL,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                body=batch,
            )
            sent += len(batch)
        except urllib.error.URLError as e:
            print(f"  ! Expo push batch failed: {e}", file=sys.stderr)
    return sent


def main():
    if not SUPABASE_URL or not SERVICE_KEY:
        # No-op (exit 0) when creds are absent, matching the sibling scripts
        # (process_requests.py / notify_brand_requests.py) — a missing secret
        # must not fail the workflow red.
        print(
            "price_drop_push: SUPABASE_URL / SUPABASE_SERVICE_KEY not set — "
            "skipping (no-op)."
        )
        return

    prices = load_catalog_prices()
    tokens = load_push_tokens()
    items = load_saved_items()
    print(f"Catalog prices: {len(prices)} | push users: {len(tokens)} | saved items: {len(items)}")

    messages = []
    marks = []  # (user_id, product_id, new_notified_price)

    for it in items:
        uid = it.get("user_id")
        pid = str(it.get("product_id"))
        token = tokens.get(uid)
        if not token:
            continue
        now = prices.get(pid)
        was = it.get("price_at_save")
        if not isinstance(now, (int, float)) or not isinstance(was, (int, float)):
            continue
        # Only a genuine drop below the saved price, and not already notified at
        # this exact level.
        if now < was and it.get("last_notified_price") != now:
            product = it.get("product") or {}
            name = product.get("name", "An item")
            brand = product.get("brand", "")
            pct = round((1 - now / was) * 100) if was else 0
            body = f"{name}{(' by ' + brand) if brand else ''} is now ${now:g}"
            if pct > 0:
                body += f" — {pct}% off"
            messages.append(
                {
                    "to": token,
                    "title": "Price drop in your Dresser",
                    "body": body + ".",
                    "sound": "default",
                    "data": {"productId": pid, "type": "price_drop"},
                }
            )
            marks.append((uid, pid, now))

    if not messages:
        print("No new price drops to notify.")
        return

    sent = send_expo_pushes(messages)
    print(f"Sent {sent} push notification(s).")

    # Mark as notified so we don't re-ping at the same price.
    for uid, pid, price in marks:
        try:
            sb(
                "saved_items",
                method="PATCH",
                params=f"user_id=eq.{uid}&product_id=eq.{pid}",
                body={"last_notified_price": price},
                extra_headers={"Prefer": "return=minimal"},
            )
        except urllib.error.URLError as e:
            print(f"  ! mark-notified failed for {uid}/{pid}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
