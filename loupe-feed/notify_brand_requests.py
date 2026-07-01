#!/usr/bin/env python3
"""
Loupe — notify requesters & brand-followers when a requested brand goes live.

Runs in CI AFTER the catalog has been built and committed. Reads
new_brands.json (written by process_requests.py earlier in the same run). For
each newly-live brand it gathers recipients:

  • the requester (user_id from new_brands.json), and
  • everyone in `brand_follows` who follows that brand.

It looks up each recipient's Expo push token from `profiles`, sends an Expo
push (batched), and stamps the request's notified_at. Requester copy takes
priority if a user is both requester and follower.

Pure standard library. Talks to Supabase + Expo over urllib.

Env (GitHub Action secrets):
  SUPABASE_URL, SUPABASE_SERVICE_KEY  — missing -> exit 0.

NOTE: iOS push *delivery* also requires the Apple APNs key to be uploaded to
Expo (a known pending step). This code is complete; deliveries begin landing on
devices the moment that key is in place. Until then sends are accepted by Expo
but not delivered to iOS — harmless.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
NEW_BRANDS_FILE = HERE / "new_brands.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_TOKEN_PREFIXES = ("ExponentPushToken", "ExpoPushToken")
BATCH_SIZE = 100


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    req = urllib.request.Request(url, headers=_sb_headers())
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else []


def sb_patch(path, payload):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="PATCH",
        headers=_sb_headers({"Content-Type": "application/json",
                             "Prefer": "return=minimal"}),
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()
    return True


def followers_of(brand):
    """user_ids in brand_follows for an exact brand string."""
    try:
        rows = sb_get(f"brand_follows?brand=eq.{urllib.parse.quote(brand)}&select=user_id")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"    WARNING: could not fetch followers for {brand!r} ({type(e).__name__})")
        return []
    return [r.get("user_id") for r in rows if r.get("user_id")]


def push_tokens_for(user_ids):
    """Map user_id -> push_token for the given ids (valid Expo tokens only)."""
    ids = [u for u in dict.fromkeys(user_ids) if u]
    if not ids:
        return {}
    joined = ",".join(urllib.parse.quote(str(u)) for u in ids)
    try:
        rows = sb_get(f"profiles?id=in.({joined})&select=id,push_token")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"    WARNING: could not fetch profiles ({type(e).__name__})")
        return {}
    out = {}
    for r in rows:
        tok = (r.get("push_token") or "").strip()
        if tok.startswith(EXPO_TOKEN_PREFIXES):
            out[r.get("id")] = tok
    return out


def send_expo(messages):
    """POST a batch (<=100) of Expo push messages. Fail-soft; returns True/False."""
    if not messages:
        return True
    data = json.dumps(messages).encode("utf-8")
    req = urllib.request.Request(
        EXPO_PUSH_URL, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError) as e:
        print(f"    WARNING: Expo push batch failed ({type(e).__name__})")
        return False


def requester_copy(brand):
    return f"✦ {brand} is now on Loupe — the brand you asked for just landed."


def follower_copy(brand):
    return f"✦ New on Loupe: {brand} — a brand you follow just dropped."


def main():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("notify_brand_requests: SUPABASE creds not set — skipping (no-op).")
        return 0

    if not NEW_BRANDS_FILE.exists():
        print("notify_brand_requests: no new_brands.json — nothing to notify.")
        return 0

    try:
        payload = json.loads(NEW_BRANDS_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"notify_brand_requests: could not read new_brands.json ({type(e).__name__}).")
        return 0

    new_brands = payload.get("brands") or []
    if not new_brands:
        print("notify_brand_requests: no newly-live brands — nothing to notify.")
        return 0

    print(f"notify_brand_requests: {len(new_brands)} new brand(s) to announce.")

    messages = []
    request_ids = []

    for nb in new_brands:
        brand = nb.get("brand")
        requester_id = nb.get("user_id")
        req_id = nb.get("request_id")
        if not brand:
            continue
        if req_id is not None:
            request_ids.append(req_id)

        # Recipients = requester ∪ followers. Requester copy wins if both.
        follower_ids = followers_of(brand)
        recipients = list(dict.fromkeys(
            ([requester_id] if requester_id else []) + follower_ids
        ))

        tokens = push_tokens_for(recipients)
        if not tokens:
            print(f"  {brand}: 0 recipients with a valid push token.")
            continue

        sent = 0
        for uid, tok in tokens.items():
            is_requester = (uid == requester_id)
            body = requester_copy(brand) if is_requester else follower_copy(brand)
            messages.append({
                "to": tok,
                "title": "Loupe",
                "body": body,
                "sound": "default",
                "data": {"type": "brand_live", "brand": brand},
            })
            sent += 1
        print(f"  {brand}: {sent} push(es) queued "
              f"({'requester+' if requester_id in tokens else ''}"
              f"{len(follower_ids)} follower row(s)).")

    # Send in batches of <=100, fail-soft per batch.
    ok_batches = 0
    total_batches = 0
    for i in range(0, len(messages), BATCH_SIZE):
        total_batches += 1
        if send_expo(messages[i:i + BATCH_SIZE]):
            ok_batches += 1

    print(f"notify_brand_requests: sent {len(messages)} message(s) "
          f"in {ok_batches}/{total_batches} batch(es).")

    # Stamp notified_at on every processed request (regardless of token presence,
    # so we don't re-notify next run).
    stamp = iso_now()
    for req_id in dict.fromkeys(request_ids):
        try:
            sb_patch(
                f"brand_requests?id=eq.{urllib.parse.quote(str(req_id))}",
                {"notified_at": stamp},
            )
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError, OSError) as e:
            print(f"    WARNING: could not stamp notified_at for id {req_id} "
                  f"({type(e).__name__})")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"notify_brand_requests: unexpected error "
              f"({type(e).__name__}: {e}); exiting 0.", file=sys.stderr)
        sys.exit(0)
