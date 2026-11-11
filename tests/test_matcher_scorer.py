"""Matcher + scorer tests against a tiny hand-built DB (no network, no real sync needed)."""
from datetime import datetime, timedelta, timezone

import pytest

from pokedeals.config import Settings
from pokedeals.matcher import choose_price
from pokedeals.models import Listing
from pokedeals.parser import parse_title
from pokedeals.scorer import evaluate

# `matcher` fixture (tiny seeded DB) lives in conftest.py


def test_same_number_in_two_sets_uses_set_name(matcher):
    r = matcher.match(parse_title("Charizard 4/102 Base Set Holo"))
    assert r.card.id == "base1-4" and r.confidence >= 0.9 and "ambiguous-set" not in r.flags


def test_ambiguous_set_is_flagged_and_low_confidence(matcher):
    r = matcher.match(parse_title("Charizard 4/102 Holo"))
    assert "ambiguous-set" in r.flags and r.confidence <= 0.55
    assert r.card.id == "dup1-4"  # cheapest print: conservative


def test_wrong_name_gets_low_confidence(matcher):
    r = matcher.match(parse_title("Squirtle 58/102 Base Set"))
    assert "name-not-in-title" in r.flags and r.confidence < 0.75


def test_no_number_no_match(matcher):
    assert not matcher.match(parse_title("Charizard holo rare vintage")).ok


def test_edition_and_finish_selection(matcher):
    first = matcher.match(parse_title("Pikachu 58/102 Base Set 1st Edition"))
    plain = matcher.match(parse_title("Pikachu 58/102 Base Set"))
    assert (first.variant, first.market) == ("1st-edition", 60.0)
    assert (plain.variant, plain.market) == ("unlimited", 10.0)

    rev = matcher.match(parse_title("Furret 136/189 Reverse Holo"))
    norm = matcher.match(parse_title("Furret 136/189"))
    assert rev.variant == "reverse-holofoil"
    assert norm.variant == "normal" and "variant-assumed" not in norm.flags


def test_choose_price_conservative_when_unsure():
    v, m, flags = choose_price({"holofoil": 9.0, "reverse-holofoil": 3.0, "normal": 1.0}, False, None)
    assert (v, m) == ("normal", 1.0) and "variant-assumed" in flags  # never the reverse by default


# ---- scorer ----

def _eval(matcher, title, price, shipping=0.0, **kw):
    listing = Listing(id="t", title=title, price=price, shipping=shipping,
                      seller_feedback_pct=kw.pop("fb", 99.5), seller_feedback_score=kw.pop("fbn", 500), **kw)
    parsed = parse_title(title)
    return evaluate(listing, parsed, matcher.match(parsed), Settings())


def test_deal_found(matcher):
    v = _eval(matcher, "Charizard 4/102 Base Set Holo NM", 280.0, 5.0)
    assert v.is_deal and v.deal.discount == pytest.approx((400 - 285) / 400, abs=0.001)


def test_condition_lowers_fair_value(matcher):
    # same price: NM is a deal, damaged is not (fair value 400*0.35 = 140)
    assert _eval(matcher, "Charizard 4/102 Base Set Holo NM", 300.0).is_deal
    assert not _eval(matcher, "Charizard 4/102 Base Set Holo damaged", 300.0).is_deal


def test_shipping_counts(matcher):
    assert _eval(matcher, "Furret 136/189 NM", 0.20, 0.0).is_deal is False or True  # sanity: runs
    v = _eval(matcher, "Charizard 4/102 Base Set Holo NM", 300.0, 80.0)         # 5% under, not the 10% a Deal needs
    assert not v.is_deal and "discount" in v.why and v.category == "fair"


def test_rejections(matcher):
    assert "skipped" in _eval(matcher, "Charizard 4/102 Base Set PSA 9", 10).why
    assert "no match" in _eval(matcher, "vintage holo", 10).why
    assert "feedback" in _eval(matcher, "Charizard 4/102 Base Set Holo NM", 300, fb=90.0).why


def test_auction_window(matcher):
    soon = datetime.now(timezone.utc) + timedelta(hours=2)
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    kw = dict(buying_options=("AUCTION",))
    assert _eval(matcher, "Charizard 4/102 Base Set Holo NM", 280, end_time=soon, **kw).is_deal
    v = _eval(matcher, "Charizard 4/102 Base Set Holo NM", 280, end_time=later, **kw)
    assert not v.is_deal and "auction" in v.why


# ---- gates added after the first look at real eBay data ----

def test_unknown_shipping_is_assumed_not_treated_as_free(matcher):
    """35% of real listings had no shipping cost; counting them as $0 flattered every one of them."""
    free = _eval(matcher, "Charizard 4/102 Base Set Holo NM", 280.0, 0.0)
    unknown = _eval(matcher, "Charizard 4/102 Base Set Holo NM", 280.0, None)
    assert unknown.deal.total_cost == pytest.approx(280.0 + Settings().assumed_shipping)
    assert "shipping-assumed" in unknown.deal.flags and "shipping-assumed" not in free.deal.flags
    assert unknown.deal.discount < free.deal.discount


def test_a_huge_discount_is_held_back_as_suspicious(matcher):
    """$100 for a $400 card (75% off) is a different print, a worse condition, or a fake, not a bargain."""
    v = _eval(matcher, "Charizard 4/102 Base Set Holo NM", 100.0)
    assert not v.is_deal and v.suspicious and v.label == "Suspicious" and v.status == "suspicious"
    assert v.deal.discount > 0.65                        # still valued, so the UI can show it
    assert v.reasons and "75% under" in v.reasons[0]
    assert _eval(matcher, "Charizard 4/102 Base Set Holo NM", 190.0).is_deal      # 52% off is allowed through


def test_tiny_dollar_savings_are_not_deals(matcher):
    # Furret: market $0.30, NM. $0.20 + $0.00 is 33% off but saves ten cents.
    v = _eval(matcher, "Furret 136/189 NM", 0.20, 0.0)
    assert not v.is_deal and "saves only" in v.why


def test_ebay_flagged_graded_slab_is_skipped_even_if_the_title_never_says_so(matcher):
    v = _eval(matcher, "Charizard 4/102 Base Set Holo NM", 280.0, graded=True)
    assert not v.is_deal and v.why == "skipped: graded (per eBay)"


def test_local_pickup_only_is_skipped(matcher):
    v = _eval(matcher, "Charizard 4/102 Base Set Holo NM", 280.0, pickup_only=True)
    assert not v.is_deal and "local pickup only" in v.why


# ---- price categories: Steal / Deal / Fair / Overpriced ----

@pytest.mark.parametrize("price,category,is_deal", [
    (200.0, "steal", True),          # 50% under the $400 market
    (300.0, "steal", True),          # exactly 25% under
    (301.0, "deal", True),           # just under 25%
    (360.0, "deal", True),           # exactly 10% under
    (361.0, "fair", False),
    (400.0, "fair", False),          # at market
    (420.0, "fair", False),          # 5% over: the edge of Fair
    (421.0, "overpriced", False),
    (600.0, "overpriced", False),
])
def test_price_categories(matcher, price, category, is_deal):
    v = _eval(matcher, "Charizard 4/102 Base Set Holo NM", price, 0.0)
    assert v.category == category and v.is_deal is is_deal and not v.blocked


def test_a_suspicious_listing_is_its_own_category_not_a_steal(matcher):
    v = _eval(matcher, "Charizard 4/102 Base Set Holo NM", 20.0, 0.0)
    assert v.category == "suspicious" and v.suspicious


def test_listings_that_could_not_be_valued_have_no_category(matcher):
    assert _eval(matcher, "Charizard 4/102 Base Set PSA 9", 10).category == ""


def test_a_cheap_listing_from_a_weak_seller_is_blocked_not_hidden_as_overpriced(matcher):
    v = _eval(matcher, "Charizard 4/102 Base Set Holo NM", 250.0, 0.0, fb=90.0)       # 37% under but 90% feedback
    assert v.category == "steal" and not v.is_deal and v.blocked and v.label == "Weak seller"
