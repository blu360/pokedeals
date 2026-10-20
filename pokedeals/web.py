"""Local web UI: a JSON API plus one static page. Bind to localhost only -- there is no auth."""
from __future__ import annotations

import re
import statistics
import time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Iterator, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import catalog
from . import listing_store
from . import watchlist as wl
from .condprices import CONDITION_NAMES, CONDITION_ORDER, TcgplayerConditions
from .config import SETTINGS, Settings
from .db import connect, set_setting
from .matcher import Matcher, choose_price
from .models import Listing
from .feed import Feed, record_calls
from .pipeline import ZIP_KEY, ScanRow, evaluate_listing, make_conditions, make_source, scan_listings

STATIC = Path(__file__).parent / "static"
MAX_FETCH = 1000   # most eBay listings one card comparison may pull (5 API calls at 200 per page)


class EvaluateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    price: float = Field(ge=0, le=1_000_000)
    shipping: float | None = Field(default=0.0, ge=0, le=10_000)


class ZipRequest(BaseModel):
    zip: str = Field(default="", max_length=10)       # 5 digits, or empty to clear


class WatchRequest(BaseModel):
    card_id: str = Field(min_length=1, max_length=64)


def tcgplayer_url(product_id: int | None) -> str | None:
    return f"https://www.tcgplayer.com/product/{product_id}?Language=English" if product_id else None


def listing_order(r: ScanRow) -> tuple:
    """Best saving first. Listings we could not value (graded, lots, ...) and suspicious ones come last."""
    d = r.verdict.deal
    return (d is None, r.verdict.suspicious, -d.conservative_discount if d else 0.0)


def row_signature(row: ScanRow) -> wl.Signature | None:
    """Which card a listing is *about*, or None when we can't say.

    A number-only match (the card's name is absent from the title) is likely a different card:
    "Pikachu 4/102" must not count as "Charizard 4/102".
    """
    c = row.match.card
    if c is None or "name-not-in-title" in row.match.flags:
        return None
    return wl.signature(c.name, c.local_id, c.official_count)


def row_to_dict(row: ScanRow, watched: bool = False) -> dict:
    l, p, v = row.listing, row.parsed, row.verdict
    d = v.deal
    card = d.match.card if d else None
    return {
        "id": l.id,
        "title": l.title,
        "url": l.url,
        "image": l.image,
        "seller_name": l.seller_name,
        "price": l.price,
        "shipping": l.shipping,
        "buying_options": list(l.buying_options),
        "ends": l.end_time.isoformat() if l.end_time else None,
        "seller_feedback_pct": l.seller_feedback_pct,
        "card_condition": l.card_condition,          # seller-stated NM/LP/MP/HP when eBay gave one
        "seller_feedback_score": l.seller_feedback_score,
        "ships_from": l.item_country,
        "is_deal": v.is_deal,
        "category": v.category,                      # steal | deal | uncertain | fair | overpriced | suspicious | "" (not valued)
        "would_be": v.would_be,                      # for "uncertain": steal or deal, were the price trustworthy
        "blocked": v.blocked,                        # valued, but held back for a reason other than its price
        "status": v.status,                          # deal | suspicious | no_deal (valued) | skip (not valued)
        "label": v.label,                            # why not, in a word or two
        "reasons": list(v.reasons),                  # warning signs in plain English (also on real deals)
        "watched": watched,
        "why": v.why,
        "parsed": {
            "number": p.number_key,
            "denominator": p.denominator,
            "first_edition": p.first_edition,
            "finish": p.finish,
            "condition": p.condition,
            "skip": list(p.reasons),
        },
        "card": None if card is None else {
            "id": card.id, "name": card.name, "set": card.set_name, "number": card.local_id,
            "rarity": card.rarity, "image": card.image, "variant": d.match.variant,
            "tcgplayer_url": tcgplayer_url(card.product_id), "official_count": card.official_count,
        },
        "valuation": None if d is None else {
            "market": d.match.market, "fair_value": d.fair_value, "total_cost": d.total_cost,
            "discount": d.discount, "margin": d.margin, "confidence": d.confidence,
            "ebay_typical": d.ebay_typical, "vs_ebay": d.vs_ebay, "cohort_size": d.cohort_size,
            "factor": round(d.factor, 3), "cond_sold": d.cond_sold, "thin_reason": d.thin_reason,
            "condition": d.condition, "flags": list(d.flags),
        },
    }


_AUTO = object()


def create_app(settings: Settings = SETTINGS, fixture: Path | None = None,
               conditions: TcgplayerConditions | None | object = _AUTO) -> FastAPI:
    """`conditions`: the TCGplayer per-condition price reader (default: built from settings; None = off)."""
    source, mode = make_source(settings, fixture)
    if conditions is _AUTO:
        conditions = make_conditions(settings)
    # the background feed only makes sense against real eBay; sample mode just scores the fixture on request
    feed = Feed(settings, source, conditions) if mode == "live" and settings.feed_enabled else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if feed is not None:
            feed.start()
        yield
        if feed is not None:
            feed.stop()

    app = FastAPI(title="pokedeals", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.feed = feed
    live_conditions = conditions if mode == "live" else None      # sample listings are not worth web requests

    def get_matcher() -> Iterator[Matcher]:
        # one connection per request: sqlite3 connections must not cross threads
        conn = connect(settings.db_path)
        try:
            yield Matcher(conn)
        finally:
            conn.close()

    def to_dict(row: ScanRow, watched: bool = False) -> dict:
        return row_to_dict(row, watched)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status(m: Matcher = Depends(get_matcher)) -> dict:
        q = m.conn.execute
        return {
            "mode": mode,
            # "sandbox" = eBay's test environment (test listings only); None in sample mode
            "ebay_env": getattr(source, "env", None),
            # the buyer ZIP lets eBay price "calculated" shipping; only live mode uses it
            "buyer_zip": getattr(source, "buyer_zip", "") if mode == "live" else None,
            "feed": feed.info() if feed is not None else None,
            "condition_prices": conditions is not None,
            "db": {
                "sets": q("SELECT count(*) FROM sets").fetchone()[0],
                "cards": q("SELECT count(*) FROM cards").fetchone()[0],
                "priced": q("SELECT count(DISTINCT card_id) FROM card_prices WHERE market > 0").fetchone()[0],
                "prices_updated": q("SELECT max(prices_updated) FROM cards").fetchone()[0],
                "watching": q("SELECT count(*) FROM watchlist").fetchone()[0],
            },
            "settings": {
                "min_discount": settings.min_discount,
                "min_confidence": settings.min_confidence,
                "default_condition": settings.default_condition,
                "min_seller_feedback_pct": settings.min_seller_feedback_pct,
                "auction_window_hours": settings.auction_window_hours,
            },
        }

    @app.put("/api/settings/zip")
    def set_zip(req: ZipRequest, m: Matcher = Depends(get_matcher)) -> dict:
        """Set (or clear) the buyer ZIP used to price eBay's calculated shipping. Saved in the local DB."""
        if mode != "live":
            raise HTTPException(409, "The ZIP only applies to live eBay listings (no eBay key is configured).")
        zip_code = req.zip.strip()
        if zip_code and not re.fullmatch(r"\d{5}", zip_code):
            raise HTTPException(422, "Enter a 5-digit US ZIP code, or leave it empty to clear it.")
        set_setting(m.conn, ZIP_KEY, zip_code)
        source.buyer_zip = zip_code
        return {"buyer_zip": zip_code}

    # ---- card search + watchlist -------------------------------------------------------------

    @app.get("/api/cards")
    def search_cards(
        q: str = Query(default="", max_length=100),
        limit: int = Query(default=25, ge=1, le=wl.MAX_PAGE),
        offset: int = Query(default=0, ge=0, le=100_000),
        sort: Literal["relevance", "price_desc", "price_asc"] = "relevance",
        m: Matcher = Depends(get_matcher),
    ) -> dict:
        """One page of matching cards plus `total`, so the client can page through every match."""
        page = wl.search_cards_page(m.conn, q, limit, offset, sort)
        return {"cards": page.cards, "total": page.total, "offset": offset, "limit": limit, "sort": sort}

    @app.get("/api/cards/{card_id}")
    def card_detail(card_id: str, m: Matcher = Depends(get_matcher)) -> dict:
        """One card with every variant's TCGplayer prices (for the large-image view)."""
        card = wl.get_card(m.conn, card_id)
        if card is None:
            raise HTTPException(404, f"unknown card id: {card_id}")
        return {"card": card, "prices": wl.card_prices(m.conn, card_id), "query": wl.card_query(card)}

    # ---- sets --------------------------------------------------------------------------------

    @app.get("/api/sets")
    def list_sets(
        q: str = Query(default="", max_length=100),
        sort: Literal["newest", "oldest", "alpha", "size", "value"] = "newest",
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10_000),
        m: Matcher = Depends(get_matcher),
    ) -> dict:
        page = catalog.list_sets(m.conn, q, sort, limit, offset)
        return {"sets": page.sets, "total": page.total, "offset": offset, "limit": limit, "sort": sort}

    @app.get("/api/sets/{set_id}")
    def get_set(set_id: str, m: Matcher = Depends(get_matcher)) -> dict:
        found = catalog.get_set(m.conn, set_id)
        if found is None:
            raise HTTPException(404, f"unknown set id: {set_id}")
        return {"set": found}

    @app.get("/api/sets/{set_id}/cards")
    def get_set_cards(
        set_id: str,
        sort: Literal["number", "price_desc", "price_asc", "alpha"] = "number",
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10_000),
        m: Matcher = Depends(get_matcher),
    ) -> dict:
        if catalog.get_set(m.conn, set_id) is None:
            raise HTTPException(404, f"unknown set id: {set_id}")
        page = catalog.set_cards(m.conn, set_id, sort, limit, offset)
        return {"cards": page.cards, "total": page.total, "offset": offset, "limit": limit, "sort": sort}

    @app.get("/api/cards/{card_id}/listings")
    def card_listings(
        card_id: str,
        max_results: int | None = Query(default=None, ge=1, le=MAX_FETCH),
        m: Matcher = Depends(get_matcher),
    ) -> dict:
        """Grab current listings for one card and set them against its market price.

        Fetches up to `max_results` eBay listings (default: settings.ebay_max_listings_per_query).
        `fetch` says how many eBay matched in total, so a short fetch is never mistaken for everything.
        """
        card = wl.get_card(m.conn, card_id)
        if card is None:
            raise HTTPException(404, f"unknown card id: {card_id}")

        query = wl.card_query(card)
        before = getattr(source, "calls", 0)
        try:
            result = source.search(query if mode == "live" else "", max_results=max_results)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"eBay request failed: {e}") from e
        finally:
            _record("card_listings", before)

        sig = wl.signature(card["name"], card["local_id"], card["official_count"])
        rows = [r for r in scan_listings(result.listings, m, settings, conditions=live_conditions)
                if row_signature(r) == sig]
        if feed is not None:                       # what the user looked up also feeds the Deals tab
            listing_store.upsert(m.conn, result.listings)
        rows.sort(key=listing_order)

        prices = wl.card_prices(m.conn, card_id)
        ref_variant, ref_market, _ = choose_price({p["variant"]: p["market"] for p in prices}, False, None)
        # suspicious listings (likely fakes at 90% off) must not become "the cheapest listing" or drag the median
        costs = [r.verdict.deal.total_cost for r in rows if r.verdict.deal and not r.verdict.suspicious]
        n_suspicious = sum(r.verdict.suspicious for r in rows)
        return {
            "mode": mode,
            "query": query,
            "card": card,
            "fetch": {
                "fetched": len(result.listings),    # listings pulled from eBay
                "available": result.total,          # listings eBay matched in total (None = unknown)
                "truncated": result.truncated,
                "requested": max_results or settings.ebay_max_listings_per_query,
                "max": MAX_FETCH,
            },
            "prices": prices,
            "stats": {
                "listings": len(costs),
                "suspicious": n_suspicious,
                "excluded": len(rows) - len(costs) - n_suspicious,
                "cheapest": min(costs) if costs else None,
                "median": statistics.median(costs) if costs else None,
                "reference_variant": ref_variant,
                "reference_market": ref_market,
            },
            "rows": [to_dict(r) for r in rows],
        }

    @app.get("/api/watchlist")
    def get_watchlist(m: Matcher = Depends(get_matcher)) -> dict:
        return {"cards": wl.list_watchlist(m.conn)}

    @app.post("/api/watchlist")
    def add_to_watchlist(body: WatchRequest, m: Matcher = Depends(get_matcher)) -> dict:
        if not wl.add_card(m.conn, body.card_id):
            raise HTTPException(404, f"unknown card id: {body.card_id}")
        return {"card": wl.get_card(m.conn, body.card_id)}

    @app.delete("/api/watchlist/{card_id}")
    def remove_from_watchlist(card_id: str, m: Matcher = Depends(get_matcher)) -> dict:
        return {"removed": wl.remove_card(m.conn, card_id)}

    # ---- scanning ----------------------------------------------------------------------------

    _details_cache: dict[str, tuple[float, dict]] = {}

    def _record(task: str, before: int) -> None:
        """Count eBay requests a person triggered by hand (they share the daily allowance with the feed)."""
        made = getattr(source, "calls", before) - before
        if made > 0:
            conn = connect(settings.db_path)
            try:
                record_calls(conn, task, made)
            finally:
                conn.close()

    def gather(q: list[str], scope: str, m: Matcher):
        """Search eBay (or read the sample file) and value what comes back. (rows, truncated, empty_watchlist)"""
        sigs = wl.watch_signatures(m.conn)
        if scope == "watchlist":
            if not sigs:
                return [], [], True
            # live: one targeted search per watched card; sample: the fixture ignores the query
            queries = wl.watch_queries(m.conn, settings.max_watchlist_queries) if mode == "live" else [""]
        else:
            queries = (q or ["pokemon card"]) if mode == "live" else [""]

        listings: list[Listing] = []
        truncated: list[dict] = []     # searches where eBay had more results than we fetched
        try:
            for query in queries:
                result = source.search(query)
                listings += result.listings
                if result.truncated:
                    truncated.append({"query": query, "fetched": len(result.listings), "total": result.total})
        except httpx.HTTPError as e:
            raise HTTPException(502, f"eBay request failed: {e}") from e
        return scan_listings(listings, m, settings, conditions=live_conditions), truncated, False

    @app.get("/api/scan")
    def scan(
        q: list[str] = Query(default=[]),
        scope: Literal["all", "watchlist"] = "all",
        m: Matcher = Depends(get_matcher),
    ) -> dict:
        rows, truncated, empty = gather(q, scope, m)
        if empty:
            return {"mode": mode, "scope": scope, "count": 0, "deals": 0, "empty_watchlist": True, "rows": []}
        sigs = wl.watch_signatures(m.conn)
        watched = {r.listing.id: row_signature(r) in sigs for r in rows}
        if scope == "watchlist":
            rows = [r for r in rows if watched[r.listing.id]]

        def sort_key(r: ScanRow) -> tuple:
            # deals first, then near misses by discount -- but a low-confidence match's discount is
            # likely fiction (wrong card), so it must not float to the top of the rejected list
            d = r.verdict.deal
            trusted = d is not None and d.confidence >= settings.min_confidence
            return (not r.verdict.is_deal, r.verdict.suspicious,
                    -d.conservative_discount if trusted else 0.0)

        rows.sort(key=sort_key)
        return {
            "mode": mode,
            "scope": scope,
            "count": len(rows),
            "deals": sum(r.verdict.is_deal for r in rows),
            "suspicious": sum(r.verdict.suspicious for r in rows),
            "empty_watchlist": False,
            "truncated": truncated,
            "rows": [to_dict(r, watched[r.listing.id]) for r in rows],
        }

    # ---- the live feed -----------------------------------------------------------------------

    @app.get("/api/deals")
    def deals(
        scope: Literal["all", "watchlist"] = "all",
        sort: Literal["savings", "newest"] = "savings",
        q: str = Query(default="", max_length=100),
        cats: str = Query(default="steal,deal", max_length=80),
        blocked: bool = False,
        intl: bool = False,
        m: Matcher = Depends(get_matcher),
    ) -> dict:
        """Listings from the live feed's stored set (instant), or a one-off scan when there is no feed.

        `cats` picks what to return: any of steal, deal, uncertain, fair, overpriced, suspicious, skipped (comma separated).
        `blocked` also returns valued listings held back for a reason other than price (a weak seller ...).
        `intl` includes sellers outside the US (default: US sellers only). `counts` always covers every category,
        so the page can show how many are behind each filter."""
        if feed is None:
            rows, _, empty = gather([], scope, m)
            first_seen, verified_at, feed_info = {}, {}, None
        else:
            snap = feed.snapshot()
            rows, first_seen, verified_at, feed_info, empty = snap.rows, snap.first_seen, snap.verified_at, feed.info(), False
        sigs = wl.watch_signatures(m.conn)
        if scope == "watchlist" and (empty or not sigs):
            return {"mode": mode, "scope": scope, "count": 0, "deals": 0, "suspicious": 0, "rows": [], "counts": {},
                    "empty_watchlist": True, "feed": feed_info}
        watched = {r.listing.id: row_signature(r) in sigs for r in rows}
        if scope == "watchlist":
            rows = [r for r in rows if watched[r.listing.id]]
        needle = q.strip().lower()
        if needle:                                 # a search reaches back through everything still stored
            rows = [r for r in rows if needle in r.listing.title.lower()
                    or (r.match.card is not None and needle in r.match.card.name.lower())]
        if not intl:                               # listings from abroad cost more to ship and are slower
            rows = [r for r in rows if (r.listing.item_country or "US").upper() == "US"]
        stale: list[ScanRow] = []
        if feed is not None and not needle and scope != "watchlist":
            # browsing shows only what is fresh: older listings have most likely sold, and stay findable by search
            rows, stale = feed.split_browsable(rows, first_seen.get)

        counts = {c: 0 for c in ("steal", "deal", "uncertain", "fair", "overpriced", "suspicious", "skipped", "blocked")}
        for r in rows:
            v = r.verdict
            if v.deal is None:
                counts["skipped"] += 1
            elif v.blocked:
                counts["blocked"] += 1
            else:
                counts[v.category] += 1

        wanted = {c for c in cats.split(",") if c in counts and c != "blocked"}

        def show(r: ScanRow) -> bool:
            v = r.verdict
            if v.deal is None:
                return "skipped" in wanted
            if v.blocked and not blocked:
                return False
            return v.category in wanted

        shown = [r for r in rows if show(r)]
        older = sum(show(r) for r in stale)        # only those a search would show under the chosen categories

        shown.sort(key=listing_order)
        if sort == "newest":
            shown.sort(key=lambda r: first_seen.get(r.listing.id, ""), reverse=True)
        total_shown = len(shown)
        shown = shown[:800]                        # plenty for a page; the counts say how many matched

        def out(r: ScanRow) -> dict:
            d = to_dict(r, watched[r.listing.id])
            d["first_seen"] = first_seen.get(r.listing.id)
            d["verified_at"] = verified_at.get(r.listing.id)
            return d
        return {"mode": mode, "scope": scope, "sort": sort, "count": len(rows) + len(stale), "older": older, "matching": total_shown,
                "deals": counts["steal"] + counts["deal"], "counts": counts, "suspicious": counts["suspicious"],
                "empty_watchlist": False, "feed": feed_info, "rows": [out(r) for r in shown]}

    @app.get("/api/listing")
    def listing_details(id: str = Query(min_length=1, max_length=200)) -> dict:
        """Extra detail for one eBay listing (all photos, seller's condition notes, item specifics)."""
        if mode != "live" or not hasattr(source, "get_item_details"):
            raise HTTPException(409, "Listing details come from eBay, which is not connected (sample mode).")
        hit = _details_cache.get(id)
        if hit and time.time() - hit[0] < 600:                 # reopening the same listing costs nothing
            return hit[1]
        before = getattr(source, "calls", 0)
        try:
            details = source.get_item_details(id)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"eBay request failed: {e}") from e
        finally:
            _record("popup", before)
        if feed is not None:                                   # what the popup learned is a free item check for the feed
            conn = connect(settings.db_path)
            try:
                if details is None:
                    listing_store.set_state(conn, [id], "ended")
                else:
                    listing_store.mark_verified(conn, id, details.get("card_condition"))
            finally:
                conn.close()
        if details is None:
            raise HTTPException(404, "eBay no longer has this listing: it has probably sold or ended.")
        _details_cache[id] = (time.time(), details)
        return details

    @app.get("/api/feed")
    def feed_status() -> dict:
        return {"enabled": feed is not None, **(feed.info() if feed is not None else {})}

    @app.post("/api/feed/poll")
    def feed_poll() -> dict:
        """Check eBay again now instead of waiting for the next cycle."""
        if feed is None:
            raise HTTPException(409, "The live feed is not running (sample mode, or switched off).")
        feed.poll_now()
        return {"queued": True}

    @app.get("/api/cards/{card_id}/conditions")
    def card_conditions(card_id: str, m: Matcher = Depends(get_matcher)) -> dict:
        """What this card is worth on TCGplayer in each condition: market price from recent sales, plus the
        cheapest live listing."""
        card = wl.get_card(m.conn, card_id)
        if card is None:
            raise HTTPException(404, f"unknown card id: {card_id}")
        if conditions is None:
            return {"available": False, "variants": []}
        prices = {p["variant"]: p for p in wl.card_prices(m.conn, card_id)}
        variants = []
        for variant, p in prices.items():
            pid = m.product_id(card_id, variant)
            if pid is None or not p["market"]:
                continue
            found = conditions.all_conditions(m.conn, pid, variant)
            rows = [{"code": c, "name": CONDITION_NAMES[c], "market": found[c].market, "sold": found[c].sold,
                     "as_of": found[c].as_of, "usable": found[c].usable,
                     "lowest": found[c].lowest, "listings": found[c].listings}
                    for c in CONDITION_ORDER if c in found]
            variants.append({"variant": variant, "market": p["market"], "product_id": pid, "conditions": rows})
        return {"available": True, "variants": variants}

    @app.post("/api/evaluate")
    def evaluate_one(body: EvaluateRequest, m: Matcher = Depends(get_matcher)) -> dict:
        listing = Listing(id="manual", title=body.title, price=body.price, shipping=body.shipping)
        return to_dict(evaluate_listing(listing, m, settings))

    return app
