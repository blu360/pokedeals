"""Warning signs that a cheap listing is a fake, a different card, or a scam rather than a bargain.

Real mispriced cards exist, but a card at 90% off is far more often counterfeit than lucky, and TCGplayer's
price alone cannot tell the difference. So a cheap listing is only called a deal if it does not look like one
of these. Signals come in two strengths: one *strong* signal, or any two *weak* ones, makes a listing
"suspicious". A single weak signal is not enough (a new seller with a fair price is just a new seller).
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import Listing

_COUNTRIES = {"CN": "China", "HK": "Hong Kong"}


@dataclass(frozen=True)
class Signal:
    code: str
    strong: bool
    text: str                   # one plain-English sentence, shown to the user


def signals(listing: Listing, market: float, discount: float, settings: Settings,
            vs_ebay: float | None = None) -> list[Signal]:
    """Every warning sign present. `discount` is against the valued (condition-adjusted) market price and
    `vs_ebay` against what other eBay sellers ask for the same card; both are fractions (0.5 = 50% under)."""
    found: list[Signal] = []
    cheap = discount >= settings.min_discount          # most signs only matter when the price is tempting

    if discount > settings.max_plausible_discount:
        found.append(Signal(
            "far-below-market", True,
            f"Priced {discount:.0%} under the market price. Genuine cards almost never sell this cheap: it is "
            "usually a counterfeit, a different print, or a card in much worse shape than described."))
    elif discount > settings.suspicious_discount:
        found.append(Signal(
            "deep-discount", False,
            f"Priced {discount:.0%} under the market price, which is unusually deep for a real seller."))

    score = listing.seller_feedback_score
    if cheap and score is not None and score < settings.new_seller_feedback:
        found.append(Signal(
            "new-seller", False,
            f"The seller has only {score} feedback. Scam listings often come from new accounts."))

    country = (listing.item_country or "").upper()
    if cheap and country in settings.high_risk_countries:
        strong = market >= settings.fake_risk_min_market
        found.append(Signal(
            "ships-from-asia", strong,
            f"Ships from {_COUNTRIES.get(country, country)}, where most counterfeit cards come from"
            + (" (and this card is valuable enough to be worth faking)." if strong else ".")))

    if vs_ebay is not None and vs_ebay >= settings.suspicious_vs_ebay:
        found.append(Signal(
            "far-below-ebay", False,
            f"Priced {vs_ebay:.0%} under what other eBay sellers ask for the same card."))
    return found


def is_suspicious(found: list[Signal]) -> bool:
    return any(s.strong for s in found) or len(found) >= 2
