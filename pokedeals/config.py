from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import credentials


ROOT = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or default)


_KEYSET_LABELS = {
    "app id": "client_id", "app": "client_id", "client id": "client_id",
    "cert id": "client_secret", "cert": "client_secret", "client secret": "client_secret",
    "dev id": "dev_id", "dev": "dev_id",
    "zip": "buyer_zip", "zip code": "buyer_zip", "postal code": "buyer_zip",
    # a bare "client" is ambiguous (eBay calls the App ID the "client id"), so it is deliberately not mapped
}
_KEYSET_SECTION = re.compile(r"^(sandbox|production)\s*:?$", re.I)


def _looks_like_sandbox(client_id: str, client_secret: str) -> bool:
    return "-SBX-" in client_id.upper() or client_secret.upper().startswith("SBX-")


def parse_keysets(text: str) -> dict[str, dict[str, str]]:
    """Parse a keyset file into {"sandbox": {...}, "production": {...}}.

    A file may hold one keyset (labels, each followed by its value on the next line, or `Label: value`)
    or both, with a `SANDBOX` / `PRODUCTION` header line before each block. A block with no header is
    filed by the key itself (-SBX- is sandbox). Dev ID is the same in both environments and unused here.
    """
    buckets: dict[str, dict[str, str]] = {"sandbox": {}, "production": {}, "_": {}}
    section, pending = "_", None
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line:
            continue
        if pending:                                  # the value line after a bare label
            buckets[section][pending] = line
            pending = None
            continue
        if m := _KEYSET_SECTION.match(line):
            section = m.group(1).lower()
            continue
        label, sep, value = line.partition(":") if ":" in line else line.partition("=")
        key = _KEYSET_LABELS.get(label.strip().lower())
        if key and sep and value.strip():
            buckets[section][key] = value.strip()
        elif key and not sep:
            pending = key
    loose = buckets.pop("_")
    if loose:                                        # an un-headed block: let the key say which it is
        env = "sandbox" if _looks_like_sandbox(loose.get("client_id", ""), loose.get("client_secret", "")) else "production"
        buckets[env] = {**loose, **buckets[env]}
    return buckets


def load_keyset(path: Path | None = None) -> dict[str, str]:
    """eBay credentials from keyset.txt: {"client_id", "client_secret", "dev_id"} for whichever exist.

    With both keysets in the file, production wins unless EBAY_ENV=sandbox asks for the other one.
    Never logs or prints values.
    """
    path = path or _env_path("POKEDEALS_KEYSET", ROOT / "keyset.txt")
    try:
        buckets = parse_keysets(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}
    forced = os.environ.get("EBAY_ENV", "").strip().lower()
    chosen = buckets[forced] if forced in buckets else (buckets["production"] or buckets["sandbox"])
    if "buyer_zip" not in chosen:                    # a ZIP is about you, not about an environment
        zips = [b["buyer_zip"] for b in buckets.values() if "buyer_zip" in b]
        if zips:
            chosen = {**chosen, "buyer_zip": zips[0]}
    return chosen


def _credential(env_name: str, keyset_key: str) -> str:
    """Environment variable (or ..._FILE), then keys saved by the first-run prompt, then keyset.txt."""
    return (credentials.env_value(env_name) or credentials.stored().get(keyset_key, "")
            or load_keyset().get(keyset_key, ""))


def detect_ebay_env(client_id: str, client_secret: str) -> str:
    """'sandbox' or 'production', from the key itself (sandbox keys contain -SBX- / start with SBX-,
    production keys contain -PRD- / start with PRD-). EBAY_ENV overrides. Defaults to production."""
    forced = os.environ.get("EBAY_ENV", "").strip().lower()
    if forced in ("sandbox", "production"):
        return forced
    return "sandbox" if _looks_like_sandbox(client_id, client_secret) else "production"


@dataclass(frozen=True)
class Settings:
    # storage
    db_path: Path = field(default_factory=lambda: _env_path("POKEDEALS_DB", ROOT / "data" / "pokedeals.db"))

    # tcgdex
    tcgdex_base: str = "https://api.tcgdex.net/v2"
    language: str = "en"
    sync_concurrency: int = 20

    # deal thresholds
    # Price categories by how far under what the card is worth a listing is (positive = cheaper):
    #   Steal      steal_discount (25%) or more
    #   Deal       min_discount (10%) up to that
    #   Fair       from fair_floor (5% over) up to min_discount
    #   Overpriced more than 5% over
    # "Deal" and "Steal" are what the Deals tab shows by default and what counts as a deal everywhere.
    steal_discount: float = 0.25
    # A card whose condition has fewer than this many sales in the last 3 months has no established market price, so a
    # "Steal" measured against it is only a guess: such deals are called Uncertain and point to eBay's sold listings.
    thin_market_sales: int = 5
    fair_floor: float = -0.05
    min_discount: float = 0.10          # fraction below fair value
    min_savings: float = 3.00           # ...and at least this many dollars: a 25% saving on a $4 card is noise
    # A discount this deep is almost never a bargain from a real seller; it is the wrong print, a worse
    # condition than assumed, or a fake. Such listings are held back with a warning instead of being
    # called deals. (Real mispriced listings of about 60% off still get through.)
    max_plausible_discount: float = 0.65
    # Weaker warning signs. One alone is not enough to call a listing suspicious, two together are (see
    # suspicion.py): a deep-but-possible discount, a barely-established seller, a price far under what other
    # eBay sellers ask, or a seller shipping from a country most counterfeit cards come from.
    suspicious_discount: float = 0.45
    suspicious_vs_ebay: float = 0.60
    new_seller_feedback: int = 50       # 50+ feedback is established enough; fewer is a (weak) warning sign
    high_risk_countries: tuple[str, ...] = ("CN", "HK")
    fake_risk_min_market: float = 50.0  # counterfeiters only bother with cards worth about this much
    # A deal must also beat the other eBay listings of the same card (TCGplayer can price a card far above
    # what eBay trades it at). Costs are compared on a Near Mint footing against the median, and only
    # when at least `min_cohort` comparable listings were fetched.
    max_cost_vs_ebay_median: float = 0.90
    min_cohort: int = 6
    assumed_shipping: float = 4.50      # used, and flagged, when eBay gives no shipping cost for a listing
    min_confidence: float = 0.75        # matcher confidence
    auction_window_hours: float = 6.0   # only surface auctions ending within this window

    # live scans of the watchlist issue one eBay search per watched card; cap it to protect API quota
    max_watchlist_queries: int = 50

    # seller quality
    min_seller_feedback_pct: float = 98.0
    min_seller_feedback_score: int = 25

    # TCGplayer market price is for Near Mint; scale by stated condition.
    # Listings that state no condition get `default_condition`.
    condition_factor: dict[str, float] = field(default_factory=lambda: {
        "NM": 1.00, "LP": 0.85, "MP": 0.70, "HP": 0.50, "DMG": 0.35,
    })
    default_condition: str = "LP"       # conservative: unstated usually means "not perfect"

    # ebay: EBAY_CLIENT_ID / EBAY_CLIENT_SECRET in the environment, else keyset.txt in the project folder
    ebay_client_id: str = field(default_factory=lambda: _credential("EBAY_CLIENT_ID", "client_id"))
    ebay_client_secret: str = field(default_factory=lambda: _credential("EBAY_CLIENT_SECRET", "client_secret"))
    # "sandbox" (eBay's test environment: its own hosts, test data only) or "production" (real listings)
    ebay_env: str = field(default_factory=lambda: detect_ebay_env(
        _credential("EBAY_CLIENT_ID", "client_id"), _credential("EBAY_CLIENT_SECRET", "client_secret")))
    ebay_marketplace: str = "EBAY_US"
    # Raw cards only: eBay filters out graded slabs server-side (halves the results and the API calls).
    ebay_ungraded_only: bool = True
    # Ask eBay for each listing's seller-stated card condition (NM/LP/MP/HP). It is only available as a
    # filter, so this costs about 5 API calls per search instead of 1; without it condition comes from
    # the title alone, and an unstated one is assumed to be `default_condition`.
    ebay_label_conditions: bool = True
    # Your 5-digit US ZIP. eBay only prices "calculated" shipping when it knows where it is going; without
    # one those listings have no shipping cost (35% of listings). Env EBAY_BUYER_ZIP, or "ZIP: 12345" in keyset.txt.
    ebay_buyer_zip: str = field(default_factory=lambda: _credential("EBAY_BUYER_ZIP", "buyer_zip"))
    # Only fetch cards the seller lists in these languages (eBay's Language item specific). TCGplayer prices are
    # for English cards, so an Italian or French copy would be valued at the wrong price. Japanese is left out
    # until there is a price source for it; add "Japanese" here then. Empty = no language filter.
    ebay_languages: tuple[str, ...] = ("English",)
    ebay_category_ids: str = "183454"   # Toys & Hobbies > Collectible Card Games > CCG Individual Cards (verified)
    # Each search pages through eBay results (200 per call) up to this many listings. Every page
    # is one API call against your daily quota; results beyond the cap are reported, not hidden.
    ebay_max_listings_per_query: int = 200

    # ---- per-condition prices, read from TCGplayer's product listings (see condprices.py) -----------------
    use_tcgplayer_conditions: bool = field(default_factory=lambda: os.environ.get("POKEDEALS_TCG_CONDITIONS", "1") != "0")
    condition_price_ttl_hours: float = 12
    condition_lookup_min_market: float = 5.0         # cheaper cards are not worth the extra requests
    max_condition_lookups_per_scan: int = 250        # fresh TCGplayer lookups per pass; the rest use the cache

    # ---- the live feed: search eBay in the background, keep what it finds (see feed.py) ------------------
    feed_enabled: bool = field(default_factory=lambda: os.environ.get("POKEDEALS_FEED", "1") != "0")
    feed_interval_seconds: int = 3600                # pause between full cycles (about 15 eBay calls each)
    # The fast lane: every `feed_fast_interval_seconds` ask eBay for the newest listings and stop at the first one
    # already stored. About 37 new Pokemon listings appear per minute and a page holds 200, so one call a minute
    # misses nothing; it costs about 1,440 calls a day.
    feed_price_check_minutes: int = 60               # how often to ask the price feed whether it has a newer day
    feed_fast_interval_seconds: int = 60             # the starting pace; after that it follows how busy eBay is
    feed_fast_target: int = 100                      # aim for about this many new listings per call (a page holds 200)
    feed_fast_min_seconds: int = 30                  # fastest the lane will go when listings pour in
    feed_fast_max_seconds: int = 120                 # slowest, when it is quiet or the day's search budget is running ahead
    feed_fast_query: str = "pokemon"
    feed_fast_max_pages: int = 3
    feed_fast_verify: int = 15                       # eBay item checks allowed per fast tick
    feed_depth: int = 200                            # newest listings fetched per query per cycle (1 eBay call)
    # Measured against the fast lane, these found the most listings it had not already stored; the other generic
    # queries (vmax, vstar, gx, charizard, ...) found almost none, so their calls now go to rotating set searches.
    feed_queries: tuple[str, ...] = (
        "pokemon holo rare", "pokemon first edition", "pokemon ex", "pokemon full art", "pokemon secret rare",
        "pokemon base set",
    )
    feed_set_queries_per_cycle: int = 6              # also rotate through "pokemon <set name>", a few per cycle
    feed_search_share: float = 0.5                   # of that budget, searching may use this share; the rest is item checks
    feed_verify_urgent_savings: float = 15.0         # a new deal saving at least this much (and worth asking about) is checked at once
    feed_daily_call_budget: int = 4000               # stay under eBay's 5,000/day, leaving room for manual use
    feed_verify_per_cycle: int = 60                  # eBay getItem checks per cycle (condition, still for sale?)
    feed_max_deals: int = 300                        # past this many deals the oldest are dropped
    feed_nondeal_hours: int = 48                     # listings that are not deals are forgotten after this (only when feed_browse_hours is 0: otherwise they go when they leave the browse window)
    feed_browse_hours: int = 24                      # the feed shows only listings first seen this recently; searching reaches back to feed_max_age_days
    ebay_daily_limit: int = 5000                     # eBay's default daily allowance; the Status page shows usage against it
    backup_enabled: bool = True                      # a daily backup of your watchlist and settings (a few KB)
    backup_keep: int = 14
    update_check: bool = True                        # once a day, see whether a newer version exists (changes nothing)
    update_repo: str = "blu360/pokedeals"
    price_sources: tuple[str, ...] = ()              # optional extra price sources, none by default (see pricesources.py)
    feed_max_age_days: int = 3                       # nothing is kept longer than this: we do not re-check for sales, so old deals expire


SETTINGS = Settings()
