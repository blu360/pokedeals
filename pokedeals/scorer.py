"""Turn (listing, match) into a Deal, or explain why it isn't one."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .config import SETTINGS, Settings
from .matcher import MatchResult
from .models import Listing
from .parser import ParsedTitle
from .suspicion import is_suspicious, signals


@dataclass(frozen=True)
class Deal:
    listing: Listing
    match: MatchResult
    condition: str              # condition assumed for valuation
    fair_value: float           # market * condition factor
    total_cost: float           # price + shipping
    discount: float             # (fair_value - total_cost) / fair_value
    margin: float               # worth minus cost, in USD
    confidence: float
    flags: tuple[str, ...]
    # Filled in by the pipeline's eBay cross-check: the typical eBay cost in THIS listing's condition,
    # how far under it this listing is, and how many listings that rests on.
    ebay_typical: float | None = None
    vs_ebay: float | None = None
    cohort_size: int = 0
    # fair_value / market: how much of the market price a copy in THIS condition is worth. The settings'
    # multiplier (an estimate) or the real TCGplayer ratio (`cond_sold` = sales it rests on; 0 = estimate).
    factor: float = 1.0
    cond_sold: int = 0
    # Why the worth is not trustworthy (e.g. "Only 3 sales in the last 3 months"), or None.
    thin_reason: str | None = None

    @property
    def conservative_discount(self) -> float:
        """The smaller of the discount against TCGplayer and against eBay's own typical price."""
        return self.discount if self.vs_ebay is None else min(self.discount, self.vs_ebay)


@dataclass(frozen=True)
class Verdict:
    deal: Deal | None           # populated whenever we could value the listing
    is_deal: bool
    why: str                    # "deal", or the first reason it was rejected (a full sentence)
    label: str = ""             # the same reason in one or two words, for tables ("Graded", "Suspicious")
    suspicious: bool = False    # looks like a fake / scam: held back from deals and from price statistics
    reasons: tuple[str, ...] = ()   # every warning sign found, in plain English (also present on real deals)
    category: str = ""          # steal | deal | uncertain | fair | overpriced | suspicious ("" = could not be valued)
    would_be: str = ""          # for "uncertain": what the category would be if the price could be trusted

    @property
    def status(self) -> str:
        return "deal" if self.is_deal else "suspicious" if self.suspicious else "skip" if self.deal is None else "no_deal"

    @property
    def blocked(self) -> bool:
        """Valued, not suspicious, and not a deal for a reason other than price (weak seller, unsure match,
        auction far from ending). Hidden unless the user asks for such listings."""
        return (self.deal is not None and not self.is_deal and not self.suspicious
                and self.label not in ("Not cheap enough", "Not cheap on eBay"))


def categorize(discount: float, settings: Settings = SETTINGS) -> str:
    """Steal / Deal / Fair / Overpriced from how far under its worth a listing is (negative = over)."""
    if discount >= settings.steal_discount:
        return "steal"
    if discount >= settings.min_discount:
        return "deal"
    if discount >= settings.fair_floor:
        return "fair"
    return "overpriced"


def with_category(v: Verdict, settings: Settings = SETTINGS) -> Verdict:
    """The verdict with its category set, judged on the smaller of the saving against TCGplayer and
    against eBay asking prices, so a card eBay trades below TCGplayer is not called a Steal."""
    if v.deal is None:
        return v
    if v.suspicious:
        return replace(v, category="suspicious", would_be="")
    base = categorize(v.deal.conservative_discount, settings)
    if v.deal.thin_reason and base in ("steal", "deal"):         # looks like a deal against a price we cannot trust
        return replace(v, category="uncertain", would_be=base)
    return replace(v, category=base, would_be="")


# short table label for each way a listing can be skipped from parsing
_SKIP_LABELS = {"graded": "Graded", "junk/proxy": "Not a real card", "non-english": "Not English",
                "lot/multiple": "Lot / multiple"}


def _skip_label(reasons: tuple[str, ...]) -> str:
    first = reasons[0] if reasons else ""
    return "Other print" if first.startswith("different print") else _SKIP_LABELS.get(first, "Skipped")


_CONDITION_ORDER = ["NM", "LP", "MP", "HP", "DMG"]          # best to worst


def _worse(*conditions: str | None) -> str | None:
    """The worst of the known conditions: the seller's field and the title can disagree, so believe
    the pessimistic one."""
    known = [c for c in conditions if c in _CONDITION_ORDER]
    return max(known, key=_CONDITION_ORDER.index) if known else None


def evaluate(
    listing: Listing, parsed: ParsedTitle, match: MatchResult, settings: Settings = SETTINGS,
    cond_value: float | None = None, cond_sold: int = 0, thin_reason: str | None = None,
) -> Verdict:
    """`cond_value`: what TCGplayer's listings say a copy in this condition is worth (see condprices.py);
    without it, the market price times the settings' condition multiplier."""
    return with_category(_evaluate(listing, parsed, match, settings, cond_value, cond_sold, thin_reason), settings)


def _evaluate(
    listing: Listing, parsed: ParsedTitle, match: MatchResult, settings: Settings,
    cond_value: float | None, cond_sold: int, thin_reason: str | None,
) -> Verdict:
    if parsed.skip:
        return Verdict(None, False, "skipped: " + ", ".join(parsed.reasons), _skip_label(parsed.reasons))
    if listing.graded:                  # eBay's own flag; catches slabs whose title never says PSA/BGS
        return Verdict(None, False, "skipped: graded (per eBay)", "Graded")
    if listing.pickup_only:
        return Verdict(None, False, "skipped: local pickup only", "Pickup only")
    if not match.ok:
        return Verdict(None, False, f"no match: {match.reason}", "No match")
    if listing.currency != "USD":
        return Verdict(None, False, f"non-USD listing ({listing.currency})", "Not USD")

    flags = list(match.flags)
    condition = _worse(listing.card_condition, parsed.condition)
    if condition is None:
        condition = settings.default_condition
        flags.append(f"condition-assumed-{condition}")
    elif condition == listing.card_condition:
        flags.append("condition-seller-stated")
    if cond_value is not None:
        fair = cond_value
        flags.append("condition-price-tcgplayer")
    else:
        fair = match.market * settings.condition_factor[condition]

    shipping = listing.shipping
    if shipping is None:                # eBay gave no cost (calculated shipping, no ZIP): assume, never use $0
        shipping = settings.assumed_shipping
        flags.append("shipping-assumed")
    total_cost = listing.price + shipping
    discount = (fair - total_cost) / fair
    margin = fair - total_cost

    deal = Deal(listing, match, condition, round(fair, 2), round(total_cost, 2),
                round(discount, 3), round(margin, 2), match.confidence, tuple(flags),
                factor=fair / match.market, cond_sold=cond_sold if cond_value is not None else 0,
                thin_reason=thin_reason)

    # ---- gates: each returns the valued deal so callers can still show near-misses ----
    found = signals(listing, match.market, discount, settings)
    reasons = tuple(f.text for f in found)

    def reject(why: str, label: str) -> Verdict:
        return Verdict(deal, False, why, label, reasons=reasons)

    if match.confidence < settings.min_confidence:
        return reject(f"low match confidence ({match.confidence:.2f})", "Unsure match")

    if listing.is_auction_only:
        if listing.end_time is None:
            return reject("auction with unknown end time", "Auction")
        hours_left = (listing.end_time - datetime.now(timezone.utc)).total_seconds() / 3600
        if hours_left > settings.auction_window_hours:
            return reject(f"auction ends in {hours_left:.1f}h (current bid not meaningful yet)", "Auction")
        deal = replace(deal, flags=deal.flags + ("auction-current-bid",))

    # Checked first: a 99%-off listing from a brand-new seller is a scam to warn about, not just
    # "a seller with too little feedback".
    if is_suspicious(found):
        return Verdict(deal, False, "Looks like a fake or a scam: " + " ".join(reasons), "Suspicious",
                       suspicious=True, reasons=reasons)

    if (pct := listing.seller_feedback_pct) is not None and pct < settings.min_seller_feedback_pct:
        return reject(f"seller feedback {pct:.1f}% below {settings.min_seller_feedback_pct:.0f}%", "Weak seller")
    if (score := listing.seller_feedback_score) is not None and score < settings.min_seller_feedback_score:
        return reject(f"seller has only {score} feedback", "New seller")

    if discount < settings.min_discount:
        return reject(f"discount {discount:.0%} below {settings.min_discount:.0%}", "Not cheap enough")
    if fair - total_cost < settings.min_savings:
        return reject(f"saves only ${fair - total_cost:.2f} (minimum ${settings.min_savings:.2f})", "Saves too little")

    return Verdict(deal, True, "deal", "Deal", reasons=reasons)
