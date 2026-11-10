#!/usr/bin/env python3
"""Square Online storefronts as a catalog source.

Every brand in brands.json was a Shopify store until 2026-11-09, because the
whole pipeline reads Shopify's public /products.json. A label that sells through
Square Online (`<name>.square.site`) publishes the same facts -- title, price,
photos, stock -- through the storefront's own public product API, the one the
shop's own pages call to draw their grid:

    https://cdn5.editmysite.com/app/store/api/v28/editor/users/<userId>/sites/<siteId>/products

This module turns one page of that API into the SHAPE of a Shopify product, so
normalize(), the junk filter, the category and colour rules, the shelf record
and every test that pins catalog.json's row shape apply to a Square brand
unchanged. It adds a source; it does not add a second pipeline.

A brands.json entry opts in with:

    {"brand": "RBK Lab", "domain": "rbk-lab.square.site", "currency": "JPY",
     "platform": "square", "squareUserId": "156408825",
     "squareSiteId": "299912790691262459", "inStockOnly": true}

`categoryByHandle` (optional) pins the category of a piece whose title misleads
the keyword rules. RBK Lab names pieces with a line of copy, and "Dress First,
Plan Later ... Tiered Skirt" reads as a dress to a rule that matches "dress".

`squareUserId` / `squareSiteId` are in the URL of the products request the
shop's own /s/shop page makes (browser dev tools, Network tab). Note that the
site id in that URL is the numeric one, not the UUID in the page's bootstrap
state: the UUID answers with an HTML page, not JSON.

What a Square storefront does NOT give us, stated so nobody assumes otherwise:

  * No /cart.js, so there is no live currency probe. The entry's `currency` is
    the authority and must be set by a human who looked at the storefront.
  * No per-size variants in the list response. `sizes` is therefore [] (the app
    reads that as a one-size piece), which is exactly right for one-of-a-kind
    work and wrong for a Square brand that sells a size run -- do not add one
    of those without extending this adapter to fetch variations.
"""
import html
import re

API_HOST = "cdn5.editmysite.com"
API_VERSION = "v28"
PER_PAGE = 60          # the storefront itself asks for 30; the API accepts 60
MAX_PAGES = 20         # 1,200 products: far past any independent label
IMAGE_WIDTH = "1280"   # originals are 2560px; the app never shows more than this

_TAG_RE = re.compile(r"<[^>]+>")
_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")


def is_square(entry):
    return str((entry or {}).get("platform") or "").strip().lower() == "square"


def products_url(user_id, site_id, page=1, per_page=PER_PAGE):
    """The storefront product-list URL. Ids are validated because they are
    interpolated into a URL from a hand-edited JSON file."""
    user_id, site_id = str(user_id or ""), str(site_id or "")
    if not _ID_RE.match(user_id) or not _ID_RE.match(site_id):
        raise ValueError("square source needs squareUserId and squareSiteId")
    return (f"https://{API_HOST}/app/store/api/{API_VERSION}/editor/users/{user_id}"
            f"/sites/{site_id}/products?page={int(page)}&per_page={int(per_page)}"
            f"&sort_by=created_date&sort_order=desc&include=images"
            f"&excluded_fulfillment=dine_in")


def _image_src(img):
    urls = img.get("absolute_urls") or {}
    return urls.get(IMAGE_WIDTH) or img.get("absolute_url") or None


def _strip_brand_suffix(title, brand):
    """'Denim Skirt | RBK Lab' -> 'Denim Skirt'. The store appends its own name
    for search engines; the app prints the brand on its own line."""
    if not brand:
        return title
    pat = re.compile(r"\s*[|–—-]\s*" + re.escape(brand) + r"\s*$", re.I)
    out = pat.sub("", title).strip()
    return out or title


_LEAD_SEP = re.compile(r"\s+[–—|]\s+")


def display_title(title, slug, entry):
    """The shorter title Loupe shows for a piece, or None to show the store's own.

    The store's titles are written for search engines ("Break the Pattern –
    Reconstructed Patchwork Denim Maxi Skirt, One of a Kind") and the app has
    room for about two short lines. The founder asked for shorter titles on
    Loupe WITHOUT changing the Square listings, so the short form lives here:

      titleByHandle   {slug: "Short Title"}, written by hand per piece. Wins.
      shortTitles     true = for a piece with no hand-written title (a new
                      listing), show the part before the first " – " / " | ",
                      which on this store is the piece's own name.

    Only the DISPLAY name changes. Category and colour inference still read the
    full store title, which carries the garment words the short one may drop."""
    entry = entry or {}
    pinned = (entry.get("titleByHandle") or {}).get(slug)
    if isinstance(pinned, str) and pinned.strip():
        return pinned.strip()
    if entry.get("shortTitles"):
        lead = _LEAD_SEP.split(title, 1)[0].strip()
        if len(lead) >= 3 and lead != title:
            return lead
    return None


def is_sold_out(sq):
    """True when the storefront itself says this piece cannot be bought."""
    badges = sq.get("badges") or {}
    inv = sq.get("inventory") or {}
    if badges.get("out_of_stock") or inv.get("all_variations_sold_out"):
        return True
    if inv.get("marked_sold_out_at_all_existing_locations"):
        return True
    # `enabled` is whether the merchant tracks counts. Only trust a zero when
    # they do; an untracked item reports 0 and is perfectly buyable.
    return bool(inv.get("enabled")) and (inv.get("total") or 0) <= 0


def to_shopify_shape(sq, domain, brand="", category_by_handle=None, entry=None):
    """One Square storefront product -> a dict normalize() can read, or None.

    None means "not a listable product" (hidden, no link, no price): the same
    contract normalize() has for a Shopify row it cannot use."""
    if (sq.get("visibility") or "visible") != "visible":
        return None
    link = str(sq.get("site_link") or "").strip("/")
    parts = link.split("/")
    # product/<slug>/<SQUARE_ID>
    if len(parts) != 3 or parts[0] != "product" or not parts[1] or not parts[2]:
        return None
    slug = parts[1]
    price = sq.get("price") or {}
    low = price.get("low")
    if not isinstance(low, (int, float)) or low <= 0:
        return None
    variant = {"price": str(low), "available": not is_sold_out(sq)}
    regular = price.get("regular_low")
    if sq.get("on_sale") and isinstance(regular, (int, float)) and regular > low:
        variant["compare_at_price"] = str(regular)
    images = []
    for im in ((sq.get("images") or {}).get("data") or []):
        src = _image_src(im)
        if src:
            images.append({"src": src})
    title = _strip_brand_suffix(html.unescape(str(sq.get("name") or "")).strip(), brand)
    return {
        "title": title,
        # What the app prints, when the roster asks for something shorter than
        # the store's title (None = print the title). See display_title().
        "_display_title": display_title(title, slug, entry),
        "handle": slug,
        "product_type": "",
        "tags": [],
        "vendor": brand,
        "options": [],
        "variants": [variant],
        "images": images,
        "published_at": sq.get("created_date") or "",
        # Square product pages do not live at /products/<handle>; normalize()
        # uses this instead of building the Shopify path.
        "_url": f"https://{domain}/{link}",
        # Optional per-piece category pin from the brand entry (None = infer).
        "_category": (category_by_handle or {}).get(slug),
    }


def description_text(sq):
    """Plain text of the store's own description. Not used by the catalog (which
    carries no descriptions); kept for tooling that needs measurements."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html.unescape(
        str(sq.get("short_description") or "")))).strip()


def pages(entry, fetch, budget_left=None, state=None):
    """Yield lists of Shopify-shaped products, one per API page.

    `fetch` is build_catalog.fetch_json (paced, counted against the run's
    request budget). Sold-out pieces are dropped here when the entry says
    `inStockOnly`, because for one-of-a-kind work a sold piece never restocks
    and an "Out of Stock" tile is a dead end.

    `state`, when given, gets state["exhausted"] = True only if the walk reached
    the END of the store. The caller cannot infer that from a page's length: the
    pages here are 60 long, not Shopify's 250, and the stock filter shortens
    them further, so "short page" says nothing. A walk stopped by the request
    budget or by MAX_PAGES leaves it False, which is the difference between
    "this store has nothing for sale" and "we did not finish looking"."""
    if state is not None:
        state["exhausted"] = False

    def done():
        if state is not None:
            state["exhausted"] = True
    domain = entry["domain"]
    brand = entry.get("brand") or ""
    in_stock_only = bool(entry.get("inStockOnly"))
    for n in range(1, MAX_PAGES + 1):
        if budget_left is not None and budget_left() <= 0:
            return
        data = fetch(products_url(entry.get("squareUserId"), entry.get("squareSiteId"), n))
        rows = (data or {}).get("data") or []
        if not rows:
            return done()
        out = []
        for sq in rows:
            if in_stock_only and is_sold_out(sq):
                continue
            shaped = to_shopify_shape(sq, domain, brand, entry.get("categoryByHandle"), entry)
            if shaped:
                out.append(shaped)
        yield out
        pg = ((data.get("meta") or {}).get("pagination") or {})
        total_pages = pg.get("total_pages")
        if isinstance(total_pages, int):
            if n >= total_pages:
                return done()
        elif len(rows) < PER_PAGE:
            return done()
