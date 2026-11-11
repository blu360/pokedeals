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
    # a bare "client" is ambiguous, so it is not mapped
}
_KEYSET_SECTION = re.compile(r"^(sandbox|production)\s*:?$", re.I)


def _looks_like_sandbox(client_id: str, client_secret: str) -> bool:
    return "-SBX-" in client_id.upper() or client_secret.upper().startswith("SBX-")


def parse_keysets(text: str) -> dict[str, dict[str, str]]:
    """Parse a keyset file into {"sandbox": {...}, "production": {...}}.

    The file may hold one keyset or both (with a SANDBOX / PRODUCTION header line before each).
    A block with no header is filed by the key itself (-SBX- means sandbox).
    """
    buckets: dict[str, dict[str, str]] = {"sandbox": {}, "production": {}, "_": {}}
    section, pending = "_", None
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line:
            continue
        if pending:                                  # the value after a bare label
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
    if loose:                                        # no header: the key says which one it is
        env = "sandbox" if _looks_like_sandbox(loose.get("client_id", ""), loose.get("client_secret", "")) else "production"
        buckets[env] = {**loose, **buckets[env]}
    return buckets


def load_keyset(path: Path | None = None) -> dict[str, str]:
    """eBay credentials from keyset.txt: {"client_id", "client_secret", "dev_id"}.

    If both keysets are present, production wins unless EBAY_ENV=sandbox. Never logs values.
    """
    path = path or _env_path("POKEDEALS_KEYSET", ROOT / "keyset.txt")
    try:
        buckets = parse_keysets(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}
    forced = os.environ.get("EBAY_ENV", "").strip().lower()
    chosen = buckets[forced] if forced in buckets else (buckets["production"] or buckets["sandbox"])
    if "buyer_zip" not in chosen:                    # a ZIP belongs to you, not to an environment
        zips = [b["buyer_zip"] for b in buckets.values() if "buyer_zip" in b]
        if zips:
            chosen = {**chosen, "buyer_zip": zips[0]}
    return chosen


def _credential(env_name: str, keyset_key: str) -> str:
    """Environment variable (or ..._FILE), then keys saved by the first-run prompt, then keyset.txt."""
    return (credentials.env_value(env_name) or credentials.stored().get(keyset_key, "")
            or load_keyset().get(keyset_key, ""))


def detect_ebay_env(client_id: str, client_secret: str) -> str:
    """'sandbox' or 'production', judged from the key (EBAY_ENV overrides). Defaults to production."""
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
    # Categories by how far under its worth a listing is:
    #   Steal 25%+ · Deal 10-25% · Fair up to 5% over · Overpriced beyond that
    # Steals and Deals are what the Deals tab shows by default.
    steal_discount: float = 0.25
    # A condition with fewer sales than this (last 3 months) has no reliable price, so deals measured
    # against it are called Uncertain.
    thin_market_sales: int = 5
    fair_floor: float = -0.05
    min_discount: float = 0.10          # fraction below fair value
    min_savings: float = 3.00           # and at least this many dollars (25% off a $4 card is noise)
    # A discount this deep is almost never real (wrong print, worse condition or a fake), so such
    # listings are held back with a warning.
    max_plausible_discount: float = 0.65
    # Weaker warning signs: one alone is not enough, two together make a listing suspicious
    # (see suspicion.py).
    suspicious_discount: float = 0.45
    suspicious_vs_ebay: float = 0.60
    new_seller_feedback: int = 50       # 50+ feedback counts as established
    high_risk_countries: tuple[str, ...] = ("CN", "HK")
    fake_risk_min_market: float = 50.0  # counterfeiters only bother with cards worth about this much
    # A deal must also beat the median of other eBay listings of the same card (on a Near Mint
    # footing), once at least `min_cohort` comparable listings were seen.
    max_cost_vs_ebay_median: float = 0.90
    min_cohort: int = 6
    assumed_shipping: float = 4.50      # assumed (and flagged) when eBay gives no shipping cost
    min_confidence: float = 0.75        # matcher confidence
    auction_window_hours: float = 6.0   # only show auctions ending within this window

    # one eBay search per watched card; capped to protect the API quota
    max_watchlist_queries: int = 50

    # seller quality
    min_seller_feedback_pct: float = 98.0
    min_seller_feedback_score: int = 25

    # TCGplayer's market price is for Near Mint; scale it by the stated condition.
    condition_factor: dict[str, float] = field(default_factory=lambda: {
        "NM": 1.00, "LP": 0.85, "MP": 0.70, "HP": 0.50, "DMG": 0.35,
    })
    default_condition: str = "LP"       # unstated usually means "not perfect"

    # eBay keys: environment variables, else keyset.txt
    ebay_client_id: str = field(default_factory=lambda: _credential("EBAY_CLIENT_ID", "client_id"))
    ebay_client_secret: str = field(default_factory=lambda: _credential("EBAY_CLIENT_SECRET", "client_secret"))
    # "sandbox" (test data only) or "production" (real listings)
    ebay_env: str = field(default_factory=lambda: detect_ebay_env(
        _credential("EBAY_CLIENT_ID", "client_id"), _credential("EBAY_CLIENT_SECRET", "client_secret")))
    ebay_marketplace: str = "EBAY_US"
    # Raw cards only: eBay filters out graded slabs, which halves the results and API calls.
    ebay_ungraded_only: bool = True
    # Ask eBay for each seller-stated condition (NM/LP/MP/HP). Costs about 5 calls per search instead
    # of 1; without it the condition comes from the title alone.
    ebay_label_conditions: bool = True
    # Your 5-digit US ZIP, so eBay can price "calculated" shipping (about a third of listings).
    # Env EBAY_BUYER_ZIP, or "ZIP: 12345" in keyset.txt.
    ebay_buyer_zip: str = field(default_factory=lambda: _credential("EBAY_BUYER_ZIP", "buyer_zip"))
    # Only fetch cards listed in these languages: TCGplayer prices are for English cards. Japanese
    # can be added once there is a price source for it. Empty = no filter.
    ebay_languages: tuple[str, ...] = ("English",)
    ebay_category_ids: str = "183454"   # Collectible Card Games > CCG Individual Cards
    # Each search pages through eBay (200 per call) up to this many listings; each page is one API call.
    ebay_max_listings_per_query: int = 200

    # ---- per-condition prices from TCGplayer (see condprices.py) ----
    use_tcgplayer_conditions: bool = field(default_factory=lambda: os.environ.get("POKEDEALS_TCG_CONDITIONS", "1") != "0")
    condition_price_ttl_hours: float = 12
    condition_lookup_min_market: float = 5.0         # cheaper cards are not worth the extra requests
    max_condition_lookups_per_scan: int = 250        # fresh TCGplayer lookups per pass; the rest use the cache

    # ---- the live feed: search eBay in the background (see feed.py) ----
    feed_enabled: bool = field(default_factory=lambda: os.environ.get("POKEDEALS_FEED", "1") != "0")
    feed_interval_seconds: int = 3600                # pause between full cycles (about 15 eBay calls each)
    # The fast lane: ask eBay for the newest listings and stop at the first one already stored.
    # Usually one call, so it costs well under 1,500 calls a day.
    feed_price_check_minutes: int = 60               # how often to ask whether the price feed has a newer day
    feed_fast_interval_seconds: int = 60             # starting pace; it then follows how busy eBay is
    feed_fast_target: int = 100                      # aim for about this many new listings per call
    feed_fast_min_seconds: int = 30                  # fastest pace when listings pour in
    feed_fast_max_seconds: int = 120                 # slowest pace, when quiet or the day's search budget is running ahead
    feed_fast_query: str = "pokemon"
    feed_fast_max_pages: int = 3
    feed_fast_verify: int = 15                       # item checks allowed per fast tick
    feed_depth: int = 200                            # newest listings per query per cycle (1 call)
    # These generic queries found the most unseen listings; others found almost none, so their calls
    # go to rotating set searches.
    feed_queries: tuple[str, ...] = (
        "pokemon holo rare", "pokemon first edition", "pokemon ex", "pokemon full art", "pokemon secret rare",
        "pokemon base set",
    )
    feed_set_queries_per_cycle: int = 6              # also rotate through "pokemon <set name>", a few per cycle
    feed_search_share: float = 0.5                   # share of the budget for searching; the rest is item checks
    feed_verify_urgent_savings: float = 15.0         # a new deal saving at least this much is checked at once
    feed_daily_call_budget: int = 4000               # stay under eBay's 5,000/day, leaving room for manual use
    feed_verify_per_cycle: int = 60                  # item checks per cycle
    feed_max_deals: int = 300                        # past this many deals the oldest are dropped
    feed_nondeal_hours: int = 48                     # non-deals are forgotten after this (only when feed_browse_hours is 0)
    feed_browse_hours: int = 24                      # the feed shows only listings first seen this recently; search reaches back further
    ebay_daily_limit: int = 5000                     # eBay's default daily allowance, shown on the Status page
    backup_enabled: bool = True                      # daily backup of your watchlist and settings
    backup_keep: int = 14
    update_check: bool = True                        # check once a day for a newer version (changes nothing)
    update_repo: str = "blu360/pokedeals"
    price_sources: tuple[str, ...] = ()              # optional extra price sources (see pricesources.py)
    feed_max_age_days: int = 3                       # nothing is kept longer than this; old deals expire


SETTINGS = Settings()
