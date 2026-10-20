from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import httpx

from .config import SETTINGS
from .db import connect
from .ebay import EbaySource
from .matcher import Matcher
from .models import Listing
from .parser import parse_title
from .pipeline import SAMPLE_FIXTURE, has_ebay_keys, make_source, scan_listings
from .scorer import Verdict, evaluate
from .sync import sync
from .tcgcsv import refresh_prices


def _print_verdict(listing: Listing, v: Verdict, *, verbose: bool) -> None:
    tag = "DEAL " if v.is_deal else "  -  "
    print(f"{tag} ${listing.price:>8.2f}  {listing.title[:78]}")
    d = v.deal
    if d:
        c = d.match.card
        print(f"        {c.name} · {c.set_name} #{c.local_id}  [{d.match.variant}] "
              f"market ${d.match.market:.2f} -> fair ${d.fair_value:.2f} ({d.condition}) "
              f"· cost ${d.total_cost:.2f} · {d.discount:+.0%} · conf {d.confidence:.2f}")
        if d.flags and (v.is_deal or verbose):
            print(f"        flags: {', '.join(d.flags)}")
    if verbose or v.is_deal:
        print(f"        -> {v.why}")


def cmd_sync(args) -> int:
    stats = asyncio.run(sync(set_ids=args.set or None, price_feed=not args.no_feed))
    print(stats)
    return 1 if stats["failed"] else 0


def cmd_prices(_args) -> int:
    """Refresh market prices from the TCGplayer feed only (fast; no TCGdex re-sync needed)."""
    conn = connect(SETTINGS.db_path)
    r = asyncio.run(refresh_prices(conn))
    print(f"feed updated:   {r.updated}\n"
          f"sets mapped:    {r.sets_mapped} of {r.sets_total}  (feed groups: {r.groups})\n"
          f"cards matched:  {r.matched}  {dict(r.by_method)}\n"
          f"  newly priced: {r.filled}\n"
          f"  corrected:    {r.changed}  (differed from TCGdex by more than 3%)\n"
          f"  unchanged:    {r.same}\n"
          f"  no market:    {r.no_market}\n"
          f"  1st Edition from the Shadowless group: {r.stamped_added}\n"
          f"still unpriced: {r.unpriced_after}")
    return 0


def cmd_stats(_args) -> int:
    conn = connect(SETTINGS.db_path)
    n_sets, n_cards = (conn.execute(q).fetchone()[0] for q in
                       ("SELECT count(*) FROM sets", "SELECT count(*) FROM cards"))
    n_priced = conn.execute("SELECT count(DISTINCT card_id) FROM card_prices WHERE market > 0").fetchone()[0]
    newest = conn.execute("SELECT max(prices_updated) FROM cards").fetchone()[0]
    by_source = dict(conn.execute(
        "SELECT source, count(DISTINCT card_id) FROM card_prices WHERE market > 0 GROUP BY source").fetchall())
    print(f"db: {SETTINGS.db_path}\nsets: {n_sets}  cards: {n_cards}  with a market price: {n_priced}  "
          f"(from {by_source})\nprices last updated: {newest}")
    return 0


def cmd_match(args) -> int:
    conn = connect(SETTINGS.db_path)
    listing = Listing(id="cli", title=args.title, price=args.price, shipping=args.shipping)
    parsed = parse_title(args.title)
    result = Matcher(conn).match(parsed)
    print(f"parsed: number={parsed.number_key} /{parsed.denominator} first_ed={parsed.first_edition} "
          f"finish={parsed.finish} condition={parsed.condition} skip={list(parsed.reasons)}")
    if not result.ok:
        print(f"match:  none ({result.reason})")
        return 0
    _print_verdict(listing, evaluate(listing, parsed, result), verbose=True)
    return 0


def cmd_scan(args) -> int:
    conn = connect(SETTINGS.db_path)
    fixture = Path(args.fixture) if args.fixture else (SAMPLE_FIXTURE if args.sample else None)
    source, mode = make_source(SETTINGS, fixture)
    queries = (args.query or ["pokemon card"]) if mode == "live" else [""]

    listings = []
    for q in queries:
        result = source.search(q)
        listings += result.listings
        if result.truncated:
            print(f"note: {q!r}: fetched {len(result.listings)} of {result.total} eBay results "
                  f"(raise ebay_max_listings_per_query in config.py to fetch more)")
    rows = scan_listings(listings, Matcher(conn), SETTINGS)
    for row in rows:
        if row.verdict.is_deal or args.verbose:
            _print_verdict(row.listing, row.verdict, verbose=args.verbose)
    print(f"\n{len(rows)} listings scanned ({mode}), {sum(r.verdict.is_deal for r in rows)} deals")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .web import create_app

    fixture = Path(args.fixture) if args.fixture else (SAMPLE_FIXTURE if args.sample else None)
    print(f"pokedeals UI: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(fixture=fixture), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_ebay_check(_args) -> int:
    """Check the eBay credentials end to end and show how real listings parse and match."""
    if not has_ebay_keys(SETTINGS):
        print("No eBay credentials. Put them in keyset.txt (APP ID / Cert ID) or set EBAY_CLIENT_ID and "
              "EBAY_CLIENT_SECRET.")
        return 1
    src, _ = make_source(SETTINGS)
    print(f"environment:  {src.env}  ({src._host})")
    zip_set = bool(re.fullmatch(r"\d{5}", src.buyer_zip))
    print("buyer ZIP:    " + ("set" if zip_set else
          f"NOT SET: listings with calculated shipping get an assumed ${SETTINGS.assumed_shipping:.2f}. "
          "Add 'ZIP: 12345' to keyset.txt for real shipping costs."))
    print(f"conditions:   {'labelled from the seller' if SETTINGS.ebay_label_conditions else 'from titles only'}"
          f" ({'about 5 API calls per search' if SETTINGS.ebay_label_conditions else '1 call per search'})")
    try:
        src._auth()
    except httpx.HTTPError as e:
        print(f"token:        FAILED ({e})")
        return 1
    print("token:        OK")

    query = "pokemon charizard 4/102"
    try:
        result = src.search(query, max_results=20)
    except httpx.HTTPError as e:
        print(f"search:       FAILED ({e})")
        return 1
    print(f"search:       {query!r} in category {SETTINGS.ebay_category_ids or '(any)'}: "
          f"eBay matched {result.total}, fetched {len(result.listings)}")
    rows = scan_listings(result.listings, Matcher(connect(SETTINGS.db_path)), SETTINGS)
    for row in rows[:8]:
        _print_verdict(row.listing, row.verdict, verbose=True)
    if src.is_sandbox:
        print("\nNOTE: these are SANDBOX keys. The sandbox holds eBay's test data, not real listings, so "
              "there are no real Pokémon cards to find.\nFor real deals, create a PRODUCTION keyset at "
              "developer.ebay.com, put its App ID / Cert ID in keyset.txt, and run this again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # card names contain accents; don't die on cp1252 consoles
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(prog="pokedeals", description="eBay Pokémon TCG deal finder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help="refresh cards + prices from TCGdex, then the TCGplayer price feed")
    p.add_argument("set", nargs="*", help="limit to these set ids (e.g. base1 swsh3)")
    p.add_argument("--no-feed", action="store_true", help="skip the TCGplayer price feed step")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("prices", help="refresh market prices from the TCGplayer feed only (fast, daily)")
    p.set_defaults(fn=cmd_prices)

    p = sub.add_parser("stats", help="show what is in the local DB")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("match", help="debug: parse + match + value a single title")
    p.add_argument("title")
    p.add_argument("--price", type=float, default=0.0)
    p.add_argument("--shipping", type=float, default=0.0)
    p.set_defaults(fn=cmd_match)

    p = sub.add_parser("scan", help="evaluate listings (live eBay, or --fixture file)")
    p.add_argument("--fixture", help="JSON file of sample listings instead of calling eBay")
    p.add_argument("--sample", action="store_true", help="use the bundled sample listings even if eBay keys exist")
    p.add_argument("-q", "--query", action="append", help="eBay search query (repeatable)")
    p.add_argument("-v", "--verbose", action="store_true", help="also show rejected listings and why")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("ebay-check", help="verify the eBay credentials and show how real listings parse")
    p.set_defaults(fn=cmd_ebay_check)

    p = sub.add_parser("serve", help="run the local web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--fixture", help="serve this JSON file of listings instead of calling eBay")
    p.add_argument("--sample", action="store_true", help="use the bundled sample listings even if eBay keys exist")
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    return args.fn(args)
