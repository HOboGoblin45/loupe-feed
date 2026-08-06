#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# Loupe — Server-side DAILY SALE + RESTOCK DIGEST push (one per user per day)
#
# Runs daily (GitHub Action). For each signed-in user, it builds ONE digest of
# the sale + size-restock updates across the pieces in their Dresser/Likes and
# sends a single Expo push — even when the app is closed. It NEVER sends more than
# one push per user per day, and sends nothing on a day with no updates.
#
# This REPLACES the old per-item price-drop sender (which could fire many pushes a
# day). The rules here MUST match the app's src/lib/dresserAlerts.ts so the push,
# the in-app "Sale & restock alerts" screen and the Dresser badge always agree.
#
# A price move caused by OUR OWN pipeline is never reported as a sale. The brand
# factors published in price_corrections.json are read on every run and a drop that
# matches one is suppressed — see the FX block below for the derivation. The same
# rule lives in dresserAlerts.ts (isFxCorrectionStep), fed by the catalog's
# `priceCorrections` block, so the push and the in-app screen suppress identically.
#
# Reads:   the published catalog (current price + sizes) + Supabase
#          (profiles.push_token / last_marketing_push_*, saved_items.product/...)
#          + price_corrections.json (the shared FX record; missing = refuse to send).
# Writes:  Supabase profiles.last_marketing_push_at + last_marketing_push_sig
#          (the per-user daily cap + anti-repeat signature); the Expo push itself.
#
# Stdlib only — no pip install needed in CI.
#
# Required env:
#   SUPABASE_URL          e.g. https://aruguxhcexfvyyfboklt.supabase.co
#   SUPABASE_SERVICE_KEY  service-role key (bypasses RLS; keep secret).
#                         SUPABASE_SERVICE_ROLE_KEY accepted as a fallback.
# Optional env:
#   CATALOG_URL           defaults to the jsDelivr-published catalog the app reads
#   EXPO_ACCESS_TOKEN     Expo push security token (recommended, not required)
#   DIGEST_DRYRUN=1       compute + print what WOULD send; never calls Expo/writes
# ─────────────────────────────────────────────────────────────────────────────

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
CORRECTIONS = HERE / "price_corrections.json"

CATALOG_URL = os.environ.get(
    "CATALOG_URL",
    "https://cdn.jsdelivr.net/gh/HOboGoblin45/loupe-feed@main/loupe-feed/catalog.json",
)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = (
    os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
)
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_ACCESS_TOKEN = os.environ.get("EXPO_ACCESS_TOKEN", "").strip()
DRYRUN = os.environ.get("DIGEST_DRYRUN", "").strip() in ("1", "true", "yes")
# Skip users active in the app within this many days — they already receive the
# on-device 11am digest, so the server push would double them up. 0 disables.
ACTIVE_SKIP_DAYS = int(os.environ.get("DIGEST_ACTIVE_SKIP_DAYS", "2") or "0")
TIMEOUT = 30

# ── Alert rules — MUST mirror src/lib/dresserAlerts.ts ───────────────────────────
PRICE_DROP_MIN_PCT = 10  # ≥ 10% off price-at-save
PRICE_DROP_MIN_ABS = 3   # AND ≥ $3 cheaper


# ── FX / config corrections are NOT sales ────────────────────────────────────────
# On 2026-08-06 (eb5a0b3) eight brands' base currency was corrected: build_catalog.py
# had been converting each store's raw price at the FX rate for the per-brand
# `currency` ANNOTATION in brands.json, while a live probe that knew the store's real
# presentment currency ran alongside and only logged a warning.
#
# Two of those corrections make prices FALL. Measured on the real snapshots either
# side of the rebuild (bd7582e -> 3d8b7ca): 28natelier moved 60 of 60 pieces at a
# median x0.2721 and Sir the Label 57 of 57 at x0.6590. Against price-at-save that is
# a 73% and a 34% "discount" on 117 pieces, and the digest would have led with the
# largest one it could find:
#
#     "Price drop in your Dresser — 28natelier TEDDY DRESS is 72% off (now $151)"
#
# Nothing was marked down. We fixed our own arithmetic, and telling a user that a
# $555 dress is 72% off when it was never $555 is the worst thing this pipeline can
# say. The analysis path has known about this whole class of event for a while —
# build_price_history.PRICE_EPOCHS voids comparisons across a methodology change, and
# build_loupe_index.detect_uniform_steps() voids a brand-day where a whole line moves
# by one identical ratio (it caught Stine Goya's x0.134 krone step). The alert path
# knew none of it. This is that guard, on this side of the split.
#
# THE TELL, exactly as the index states it: not the SIZE of the move but its
# ratio. A real sale marks different pieces down by different amounts; an FX
# correction multiplies every piece by the same number — the one published in
# price_corrections.json. So the record is read, never re-listed here: a parallel
# list of brands in this file would drift from the archive on the first correction
# anyone added, and the two would then disagree about what a user is owed.
#
# WHY THE TOLERANCE IS DERIVED RATHER THAN TUNED. Every archived price is a
# dollar-rounded integer, so an FX-corrected price is round(raw * rightFx) for a raw
# only known to within half a unit of its own currency. That double rounding bounds
# the residual exactly:
#
#     then = round(raw * wrongFx)             =>  raw*wrongFx ∈ [then-0.5, then+0.5)
#     now  = round(raw * rightFx)             =>  |now - then*factor| ≤ 0.5*factor + 0.5
#
# Measured over all 400 corrected pieces present in both snapshots, the worst
# residual is $0.870 against a bound of $1.258 — 0.80 of it, with headroom and no
# fitting. A residual ABOVE the bound cannot be rounding, so it is a real move and
# the alert stands.
#
# THE AMBIGUITY, STATED RATHER THAN HIDDEN — the same one detect_uniform_steps()
# declares. "Everything x0.66 overnight" is a currency correction and it is also a
# sitewide 34%-off sale, and no amount of staring at two prices separates them. Both
# are suppressed. That biases this digest toward saying LESS than the truth, never
# more, which is the only safe direction for a push notification.
FX_STEP_ROUNDING_SLACK = 0.5  # the half-dollar each of the two roundings can cost


def load_price_corrections(path=CORRECTIONS):
    """{brand: factor} from the shared archive record.

    A MISSING TABLE IS A HARD STOP, not an empty dict. build_price_history.py refuses
    to build without this file for the same reason: quoting a price we cannot vouch
    for because a path was wrong is exactly the failure the file exists to end. Here
    the stakes are higher than a rebuilt artefact — a silent {} would send the 117
    false "on sale" pushes this guard was written to stop, to real phones, once, with
    no way to recall them. A missed digest is recoverable; a false one is not.
    """
    if not path.exists():
        sys.exit(
            f"REFUSING TO SEND: {path.name} is missing.\n"
            "  It records the FX corrections that must NOT be reported as sales.\n"
            "  Without it this digest would push a currency fix to users as a discount."
        )
    doc = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for c in doc.get("corrections", []):
        brand, factor = c.get("brand"), c.get("factor")
        try:
            factor = float(factor)
        except (TypeError, ValueError):
            continue
        if brand and factor > 0:
            out[brand] = factor
    return out


def is_fx_correction_step(now, then, factor):
    """True when `then` -> `now` is exactly the published FX correction for the brand.

    `factor` is the multiplier price_corrections.json says was applied to that brand's
    stored price. When the observed move lands within the rounding bound derived
    above, the move IS the correction and no part of it is a markdown.
    """
    if not factor or factor <= 0:
        return False
    try:
        now = float(now)
        then = float(then)
    except (TypeError, ValueError):
        return False
    expected = then * factor
    return abs(now - expected) <= FX_STEP_ROUNDING_SLACK * factor + FX_STEP_ROUNDING_SLACK


def fmt_price(x):
    """75.0 -> '75', 79.5 -> '79.5' (matches the app's $-display)."""
    try:
        return f"{float(x):g}"
    except (TypeError, ValueError):
        return str(x)


def is_meaningful_price_drop(now, then):
    try:
        now = float(now)
        then = float(then)
    except (TypeError, ValueError):
        return False
    if not (now < then):
        return False
    off = then - now
    return (off / then) * 100 >= PRICE_DROP_MIN_PCT and off >= PRICE_DROP_MIN_ABS


def sale_percent(now, then):
    try:
        now = float(now)
        then = float(then)
    except (TypeError, ValueError):
        return 0
    if not (now < then) or then <= 0:
        return 0
    return int(((then - now) / then) * 100)


def canon_size(s):
    """M == Medium, X-Large == XL, etc. (mirror src/lib/dresserAlerts.ts) so a
    catalog relabel isn't a false restock/sold-out."""
    import re as _re
    t = _re.sub(r"[\s._-]", "", s.strip().lower())
    m = {"extrasmall":"xs","xsmall":"xs","xs":"xs","small":"s","s":"s",
         "medium":"m","med":"m","m":"m","large":"l","l":"l",
         "extralarge":"xl","xlarge":"xl","xl":"xl","xxlarge":"xxl","xxl":"xxl","2xl":"xxl",
         "xxxlarge":"xxxl","xxxl":"xxxl","3xl":"xxxl","onesize":"os","os":"os"}
    return m.get(t, t)


def norm_sizes(sizes):
    out, seen = [], set()
    if not isinstance(sizes, list):
        return out
    for s in sizes:
        if not isinstance(s, str):
            continue
        t = s.strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def compute_alerts(items, live_by_id, corrections=None):
    """Sale + size alerts for ONE user's saved items vs the live catalog.

    `corrections` is {brand: factor} from load_price_corrections(). A move that IS
    one of those factors is our own arithmetic being fixed, never a markdown.
    """
    corrections = corrections or {}
    out, seen = [], set()
    for it in items:
        product = it.get("product") or {}
        pid = str(it.get("product_id") or product.get("id") or "")
        if not pid or pid in seen:
            continue
        live = live_by_id.get(pid)
        if not live:
            continue
        seen.add(pid)
        # NOTE (stale / grace-carried items): live.get("stale") is True when the item
        # is missing from the current scrape and its price/sizes are FROZEN (see
        # build_catalog.py grace-window). A frozen price can manufacture a phantom
        # "sale" vs price_at_save. We deliberately do NOT drop stale items here yet:
        # this digest MUST mirror the app's src/lib/dresserAlerts.ts (see file header),
        # else the push and the in-app "Sale & restock alerts" screen would disagree.
        # TODO: add the SAME stale-skip to dresserAlerts.ts, then enable it here:
        #   if live.get("stale"): continue

        sale = None
        if is_meaningful_price_drop(live.get("price"), it.get("price_at_save")):
            # An FX/config correction is not a discount. The brand's published factor
            # is consulted BEFORE the drop is allowed to become a sale, so a currency
            # fix can never reach a user's lock screen as "72% off". See the block at
            # the top of this file for the derivation and the deliberate ambiguity.
            factor = corrections.get((live.get("brand") or "").strip())
            if not is_fx_correction_step(live.get("price"), it.get("price_at_save"), factor):
                sale = {
                    "was": float(it["price_at_save"]),
                    "now": float(live["price"]),
                    "pct": sale_percent(live["price"], it["price_at_save"]),
                }

        new_sizes, gone_sizes = [], []
        snap = norm_sizes(product.get("sizes"))
        livesz = norm_sizes(live.get("sizes"))
        if snap and livesz:
            sset = {canon_size(s) for s in snap}
            lset = {canon_size(s) for s in livesz}
            new_sizes = [s for s in livesz if canon_size(s) not in sset]
            gone_sizes = [s for s in snap if canon_size(s) not in lset]

        if sale or new_sizes or gone_sizes:
            out.append(
                {
                    "pid": pid,
                    "brand": (live.get("brand") or "").strip(),
                    "name": (live.get("name") or "").strip(),
                    "sale": sale,
                    "new_sizes": new_sizes,
                    "gone_sizes": gone_sizes,
                }
            )

    out.sort(key=lambda a: (-(a["sale"]["pct"] if a["sale"] else -1), -len(a["new_sizes"])))
    return out


def signature(alerts):
    parts = []
    for a in alerts:
        s = f"s{fmt_price(a['sale']['now'])}" if a["sale"] else ""
        ns = "+" + "|".join(sorted(a["new_sizes"])) if a["new_sizes"] else ""
        gs = "-" + "|".join(sorted(a["gone_sizes"])) if a["gone_sizes"] else ""
        parts.append(f"{a['pid']}:{s}{ns}{gs}")
    return ";".join(sorted(parts))


def _label(a):
    return ((a["brand"] + " ") if a["brand"] else "") + (a["name"] or "")


def summarize(alerts):
    """Digest title/body, or None when there's nothing worth a push."""
    if not alerts:
        return None
    sales = [a for a in alerts if a["sale"]]
    restocks = [a for a in alerts if not a["sale"] and a["new_sizes"]]

    if sales:
        lead = sales[0]
        more = len(sales) - 1
        if more > 0:
            tail = f" +{more} more on sale"
        elif restocks:
            tail = f" +{len(restocks)} back in your size"
        else:
            tail = ""
        piece = _label(lead).strip() or "A saved piece"
        return {
            "title": "Price drop in your Dresser ✦",
            "body": f"{piece} is {lead['sale']['pct']}% off (now ${fmt_price(lead['sale']['now'])}).{tail}",
        }

    if restocks:
        r = restocks[0]
        more = len(restocks) - 1
        tail = f" +{more} more restocked" if more > 0 else ""
        piece = _label(r).strip() or "A saved piece"
        size = r["new_sizes"][0]
        return {"title": "Back in your size ✦", "body": f"{piece} is back in {size}.{tail}"}

    return None  # sold-out-only changes don't warrant a push


# ── Supabase + catalog + Expo I/O ────────────────────────────────────────────────

def _req(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def sb(path, method="GET", params="", body=None, extra_headers=None):
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


def sb_all(path, params, page=1000):
    """Paginate a PostgREST select until a short page — PostgREST caps a single
    response (commonly 1000 rows), which would silently drop users/items as we grow."""
    out, offset = [], 0
    while True:
        p = f"{params}&limit={page}&offset={offset}"
        rows = sb(path, params=p) or []
        out.extend(rows)
        if len(rows) < page:
            return out
        offset += page


def load_catalog():
    """{product_id: {price, sizes, brand, name, stale}} from the published catalog.

    `stale` mirrors build_catalog.py's grace-carry flag: the item is missing from the
    live scrape and its price/sizes are FROZEN at last-good-scrape. Comparing a frozen
    price to a user's price_at_save can manufacture a phantom "sale", so the flag is
    carried here for compute_alerts to consult (see the note there)."""
    data = _req(CATALOG_URL)
    products = data.get("products", data) if isinstance(data, dict) else data
    out = {}
    for p in products or []:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        out[pid] = {
            "price": p.get("price"),
            "sizes": p.get("sizes") or [],
            "brand": p.get("brand") or "",
            "name": p.get("name") or "",
            "stale": bool(p.get("stale")),
        }
    return out


def load_users():
    """{user_id: {token, sig, at}} for opted-in users."""
    try:
        rows = sb_all(
            "profiles",
            "select=id,push_token,last_marketing_push_sig,last_marketing_push_at,updated_at&push_token=not.is.null",
        )
    except urllib.error.HTTPError as e:
        # profiles.last_marketing_push_* not migrated yet → no-op SAFELY (never spam)
        # instead of failing the run. Apply supabase/2026-07_marketing_push_cols.sql.
        if e.code in (400, 404):
            print(
                "daily-digest: profiles.last_marketing_push_* columns missing — apply "
                "supabase/2026-07_marketing_push_cols.sql first. Skipping (no-op)."
            )
            return {}
        raise
    out = {}
    for r in rows:
        if r.get("push_token"):
            out[r["id"]] = {
                "token": r["push_token"],
                "sig": r.get("last_marketing_push_sig"),
                "at": r.get("last_marketing_push_at"),
                "active_at": r.get("updated_at"),
            }
    return out


def load_saved_items():
    return sb_all("saved_items", "select=user_id,product_id,price_at_save,product")


def pushed_today(at_iso):
    """True when last_marketing_push_at falls on today's UTC date (daily cap)."""
    if not at_iso:
        return False
    try:
        s = at_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date() == datetime.now(timezone.utc).date()
    except ValueError:
        return False


def recently_active(at_iso, days=ACTIVE_SKIP_DAYS):
    """True when the user opened the app within `days` (→ on-device digest covers them).

    NOTE (self-throttle risk): `at_iso` is profiles.updated_at (see load_users), used
    here as an activity proxy. If a Supabase touch-trigger bumps updated_at on ANY row
    write — including this script's own last_marketing_push_* stamp — then a user we
    push today looks "active" for the next `days` and gets skipped, throttling the
    digest with our OWN writes. Mitigations in place: the stamping PATCH writes ONLY
    the marketing columns, and pushed_today()+signature already gate re-sends.
    TODO: when profiles gains a dedicated activity column (e.g. last_active_at /
    last_seen_at that is NOT bumped by service-role marketing writes), switch
    load_users' `active_at` select to read that instead of updated_at.
    """
    if days <= 0 or not at_iso:
        return False
    try:
        s = at_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return age.total_seconds() < days * 86400
    except ValueError:
        return False


def send_expo_pushes(messages):
    """POST digests to Expo in ≤100-message batches.

    Returns (succeeded_idx, tickets):
      • succeeded_idx — the set of message indices whose batch POST was accepted by
        Expo. main() stamps the 1/day marketing cap ONLY for these users, so a user
        in a FAILED batch is retried next run instead of being marked "pushed" and
        skipped until their digest content changes (the old code stamped everyone).
      • tickets — per-message Expo response tickets, aligned to `messages` (None where
        the batch failed), so main() can null out DeviceNotRegistered push_tokens.
    """
    succeeded_idx = set()
    tickets = [None] * len(messages)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if EXPO_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {EXPO_ACCESS_TOKEN}"
    for i in range(0, len(messages), 100):
        batch = messages[i : i + 100]
        try:
            resp = _req(EXPO_PUSH_URL, method="POST", headers=headers, body=batch)
            succeeded_idx.update(range(i, i + len(batch)))
            # Expo replies {"data": [ {status, id?, message?, details?}, ... ]} in
            # batch order — keep each ticket so DeviceNotRegistered can be actioned.
            data = resp.get("data") if isinstance(resp, dict) else None
            if isinstance(data, list):
                for j, ticket in enumerate(data):
                    if i + j < len(tickets):
                        tickets[i + j] = ticket
        except urllib.error.URLError as e:
            print(f"  ! Expo push batch failed: {e}", file=sys.stderr)
    return succeeded_idx, tickets


def build_digests(users, items, catalog, corrections=None):
    """Return (messages, stamps) for users with a NEW digest, honoring the 1/day cap."""
    by_user = {}
    for it in items:
        by_user.setdefault(it.get("user_id"), []).append(it)

    messages, stamps = [], []  # stamps: (uid, sig)
    for uid, u in users.items():
        if pushed_today(u.get("at")):
            continue  # already pushed today — hard 1/day cap
        if recently_active(u.get("active_at")):
            continue  # on-device 11am digest already covers active users — no double
        alerts = compute_alerts(by_user.get(uid, []), catalog, corrections)
        summ = summarize(alerts)
        if not summ:
            continue
        sig = signature(alerts)
        if sig == u.get("sig"):
            continue  # unchanged since last push — don't re-nag
        messages.append(
            {
                "to": u["token"],
                "title": summ["title"],
                "body": summ["body"],
                "sound": "default",
                "data": {"type": "sales_updates"},
            }
        )
        stamps.append((uid, sig))
    return messages, stamps


def main():
    if not SUPABASE_URL or not SERVICE_KEY:
        print("daily-digest: SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping (no-op).")
        return

    # Loaded BEFORE any network call: a missing corrections table must stop the run
    # while it is still a no-op, not after the catalog and the roster are in hand.
    corrections = load_price_corrections()

    catalog = load_catalog()
    users = load_users()
    items = load_saved_items()
    print(
        f"Catalog: {len(catalog)} | push users: {len(users)} | saved items: {len(items)}"
        f" | FX corrections in force: {len(corrections)}"
    )

    messages, stamps = build_digests(users, items, catalog, corrections)
    if not messages:
        print("No new sale/restock digests to send today.")
        return

    if DRYRUN:
        print(f"[DRY RUN] would send {len(messages)} digest(s):")
        for m in messages[:20]:
            print(f"  → {m['title']} | {m['body']}")
        return

    succeeded_idx, tickets = send_expo_pushes(messages)
    print(f"Sent {len(succeeded_idx)} of {len(messages)} digest push(es).")

    # `messages`, `stamps` and `tickets` are index-aligned (build_digests appends to
    # messages/stamps in lockstep; send_expo_pushes returns tickets aligned to
    # messages). Two rules when recording the result:
    #   1. Only stamp the 1/day marketing cap for users whose batch actually sent —
    #      a failed batch is retried next run rather than silently marked "pushed".
    #   2. If Expo reports a token as DeviceNotRegistered, null out push_token so we
    #      stop pushing to a dead device (and do NOT stamp — nothing was delivered).
    now_iso = datetime.now(timezone.utc).isoformat()
    for i, (uid, sig) in enumerate(stamps):
        ticket = tickets[i] if i < len(tickets) else None
        if isinstance(ticket, dict) and ticket.get("status") == "error":
            details = ticket.get("details") or {}
            if details.get("error") == "DeviceNotRegistered":
                try:
                    sb(
                        "profiles",
                        method="PATCH",
                        params=f"id=eq.{uid}",
                        body={"push_token": None},
                        extra_headers={"Prefer": "return=minimal"},
                    )
                except urllib.error.URLError as e:
                    print(f"  ! token clear failed for {uid}: {e}", file=sys.stderr)
                continue  # dead token — delivered nothing, so don't stamp the cap
        if i not in succeeded_idx:
            continue  # batch POST failed — leave unstamped so it retries next run
        try:
            sb(
                "profiles",
                method="PATCH",
                params=f"id=eq.{uid}",
                # Marketing columns ONLY. If a DB touch-trigger bumps profiles.updated_at
                # on write, keeping this PATCH minimal limits the recently_active()
                # self-throttle flagged there (never PATCH unrelated profile fields).
                body={"last_marketing_push_at": now_iso, "last_marketing_push_sig": sig},
                extra_headers={"Prefer": "return=minimal"},
            )
        except urllib.error.URLError as e:
            print(f"  ! stamp failed for {uid}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
