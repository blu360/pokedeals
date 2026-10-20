"""Glue shared by the CLI and the web UI: pick a listing source, run listings through parse → match → score."""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from .condprices import TcgplayerConditions, condition_value
from .config import ROOT, SETTINGS, Settings
from .db import connect, get_setting
from .ebay import EbaySource, FixtureSource, ListingSource
from .matcher import MatchResult, Matcher
from .models import Listing
from .parser import ParsedTitle, parse_title
from .scorer import Verdict, evaluate, with_category
from .suspicion import is_suspicious, signals

SAMPLE_FIXTURE = ROOT / "data" / "sample_listings.json"
ZIP_KEY = "buyer_zip"      # app_settings key for the ZIP entered in the UI


@dataclass(frozen=True)
class ScanRow:
    listing: Listing
    parsed: ParsedTitle
    match: MatchResult     # kept even when the verdict rejects the listing (e.g. graded) so callers can still attribute it
    verdict: Verdict


def has_ebay_keys(settings: Settings = SETTINGS) -> bool:
    return bool(settings.ebay_client_id and settings.ebay_client_secret)


def make_source(
    settings: Settings = SETTINGS, fixture: Path | None = None
) -> tuple[ListingSource, str]:
    """Return (source, mode) where mode is 'live' or 'sample'. A fixture path forces sample mode."""
    if fixture is None and has_ebay_keys(settings):
        source = EbaySource(settings)
        conn = connect(settings.db_path)
        try:
            source.buyer_zip = get_setting(conn, ZIP_KEY) or source.buyer_zip   # the UI's ZIP wins over keyset.txt
        finally:
            conn.close()
        return source, "live"
    return FixtureSource(fixture or SAMPLE_FIXTURE), "sample"


def evaluate_listing(listing: Listing, matcher: Matcher, settings: Settings = SETTINGS) -> ScanRow:
    parsed = parse_title(listing.title)
    match = matcher.match(parsed)
    return ScanRow(listing, parsed, match, evaluate(listing, parsed, match, settings))


def make_conditions(settings: Settings = SETTINGS) -> TcgplayerConditions | None:
    """The TCGplayer per-condition price reader, or None when switched off."""
    if not settings.use_tcgplayer_conditions:
        return None
    return TcgplayerConditions(settings.db_path, ttl_hours=settings.condition_price_ttl_hours)


def scan_listings(
    listings: Iterable[Listing], matcher: Matcher, settings: Settings = SETTINGS,
    conditions: TcgplayerConditions | None = None, fetch: bool = True,
) -> list[ScanRow]:
    """Evaluate each distinct listing (by id), keeping input order, re-value the promising ones with real
    per-condition prices from TCGplayer (`conditions`; `fetch=False` uses only what is already cached),
    then cross-check deals against the other eBay listings for the same card."""
    seen: set[str] = set()
    rows: list[ScanRow] = []
    for listing in listings:
        if listing.id in seen:
            continue
        seen.add(listing.id)
        rows.append(evaluate_listing(listing, matcher, settings))
    if conditions is not None:
        rows = _refine_with_conditions(rows, matcher, settings, conditions, fetch)
    return _apply_ebay_cohort_check(rows, settings)


def _refine_with_conditions(rows: list[ScanRow], matcher: Matcher, settings: Settings,
                            conditions: TcgplayerConditions, fetch: bool) -> list[ScanRow]:
    """Replace the guessed condition multiplier with what TCGplayer's listings say a copy in that condition
    costs. Only listings that could possibly be deals (priced under the market price) are looked up, since
    each lookup is a web request; a condition TCGplayer has too few listings for keeps the estimate."""
    plan: list[tuple[int, int, str, str]] = []
    for i, row in enumerate(rows):
        d, card = row.verdict.deal, row.match.card
        if d is None or card is None or d.confidence < settings.min_confidence:
            continue
        # a condition's sales-based price can sit above the blended market price (Near Mint often does), so
        # give listings a little headroom above it before ruling them out
        if d.match.market < settings.condition_lookup_min_market or d.total_cost >= d.match.market * 1.3:
            continue
        pid = matcher.product_id(card.id, d.match.variant)
        if pid is not None:
            plan.append((i, pid, d.match.variant, d.condition))
    if not plan:
        return rows
    # Most promising first: when there are more lookups than the per-scan limit, the listings closest to being
    # deals are the ones that must have real condition prices.
    plan.sort(key=lambda p: rows[p[0]].verdict.deal.discount, reverse=True)
    if fetch:
        conditions.prefetch([(pid, variant, cond) for _, pid, variant, cond in plan],
                            limit=settings.max_condition_lookups_per_scan)
    out = list(rows)
    for i, pid, variant, cond in plan:
        row = out[i]
        cp = conditions.get(matcher.conn, pid, variant, cond)
        value = condition_value(cp)
        thin = None
        if value is not None and cp.sold < settings.thin_market_sales:
            # TCGplayer has a price, but little behind it (with no sales in 3 months it is the last price it recorded)
            thin = (f"Only {cp.sold} {'sale' if cp.sold == 1 else 'sales'} of this card in this condition in the last 3 months"
                    if cp.sold else "No sales of this card in this condition in the last 3 months (this is the last price TCGplayer recorded)")
        elif value is None and conditions.has_history(matcher.conn, pid):
            # TCGplayer was asked and has no price at all for this print and condition: the worth is an estimate
            thin = "TCGplayer has no price for this card in this condition (the worth is an estimate)"
        if value is None and thin is None:
            continue                                   # not looked up (limit reached, site unreachable): nothing to say
        verdict = evaluate(row.listing, row.parsed, row.match, settings, cond_value=value,
                           cond_sold=cp.sold if cp else 0, thin_reason=thin)
        out[i] = replace(row, verdict=verdict)
    return out


def _apply_ebay_cohort_check(rows: list[ScanRow], settings: Settings) -> list[ScanRow]:
    """A deal must be cheap against what eBay sellers are asking, not just against TCGplayer.

    TCGplayer's price can sit far above eBay's for a card (thin market, different buyers). When every eBay
    listing of a card is "60% under market", that is not eight bargains: eBay just trades that card lower,
    and the TCGplayer reference is wrong for buying or reselling on eBay. So within each card (and print),
    costs are put on a Near Mint footing and compared with the median; a listing only stays a deal if it
    beats the median by `max_cost_vs_ebay_median`. Needs `min_cohort` comparable listings to judge.
    """
    cohorts: dict[tuple, list[tuple[int, float]]] = defaultdict(list)
    for i, row in enumerate(rows):
        d, card = row.verdict.deal, row.match.card
        if d is None or card is None or d.confidence < settings.min_confidence:
            continue
        if row.verdict.suspicious or d.discount > settings.max_plausible_discount:
            continue                                          # likely fakes/other prints: do not let them set the norm
        cohorts[(card.id, row.match.variant)].append((i, d.total_cost / d.factor))

    out = list(rows)
    for members in cohorts.values():
        if len(members) < settings.min_cohort:
            continue
        median = statistics.median(nm_cost for _, nm_cost in members)
        for i, nm_cost in members:
            row = out[i]
            deal = row.verdict.deal
            typical = median * deal.factor                                     # back into this listing's condition
            deal = replace(deal, ebay_typical=round(typical, 2), cohort_size=len(members),
                           vs_ebay=round((typical - deal.total_cost) / typical, 3))
            verdict = replace(row.verdict, deal=deal)
            if verdict.is_deal:
                found = signals(row.listing, deal.match.market, deal.discount, settings, deal.vs_ebay)
                reasons = tuple(f.text for f in found)
                if is_suspicious(found):                      # e.g. new seller AND far under every other listing
                    why = "Looks like a fake or a scam: " + " ".join(reasons)
                    verdict = Verdict(deal, False, why, "Suspicious", suspicious=True, reasons=reasons)
                elif nm_cost > settings.max_cost_vs_ebay_median * median:
                    why = (f"not cheap for eBay: the typical eBay listing of this card costs ${typical:.2f} "
                           f"in this condition ({len(members)} compared)")
                    verdict = Verdict(deal, False, why, "Not cheap on eBay", reasons=reasons)
                else:
                    verdict = replace(verdict, reasons=reasons)
            out[i] = replace(row, verdict=verdict)
    # the category is judged on the smaller of the saving against TCGplayer and against eBay's own prices
    return [replace(r, verdict=with_category(r.verdict, settings)) for r in out]
