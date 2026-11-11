"""A deal must be cheap against other eBay listings of the card, not only against TCGplayer.

Real case: eight Mega Gengar EX listings at $116-$250 against a TCGplayer market of $503. That is not eight
bargains: eBay just trades the card below TCGplayer.
"""
import pytest

from pokedeals.config import Settings
from pokedeals.models import Listing
from pokedeals.pipeline import scan_listings

TITLE = "Charizard 4/102 Base Set Holo NM"          # base1-4 in the shared test DB: TCGplayer market $400


def listings(prices, title=TITLE, prefix="c"):
    return [Listing(id=f"{prefix}{i}", title=title, price=p, shipping=0.0, seller_feedback_pct=99.5,
                    seller_feedback_score=900, card_condition="NM") for i, p in enumerate(prices)]


def scan(matcher, prices, **settings):
    from dataclasses import replace
    s = replace(Settings(), **settings)
    return scan_listings(listings(prices), matcher, s)


def deals(rows):
    return sorted(r.listing.price for r in rows if r.verdict.is_deal)


def test_when_every_listing_is_far_below_tcgplayer_only_the_standouts_are_deals(matcher):
    # all ~50% under TCGplayer's $400, but that is just eBay's price; only listings 10%+ under the $220 median
    # are deals: $150 and $190, not the $200-$260 crowd
    rows = scan(matcher, [190, 200, 210, 220, 230, 240, 250, 260, 150])
    assert deals(rows) == [150.0, 190.0]
    rejected = [r for r in rows if r.listing.price == 250][0].verdict
    assert rejected.deal is not None and not rejected.is_deal
    assert rejected.why.startswith("not cheap for eBay: the typical eBay listing of this card costs $220.00")
    assert "(9 compared)" in rejected.why


def test_every_cohort_member_carries_the_ebay_comparison(matcher):
    rows = scan(matcher, [190, 200, 210, 220, 230, 240, 250, 260, 150])
    d = next(r.verdict.deal for r in rows if r.listing.price == 150)
    assert d.ebay_typical == 220.0 and d.cohort_size == 9
    assert d.vs_ebay == pytest.approx((220 - 150) / 220, abs=0.001)           # 32% under eBay...
    assert d.discount == pytest.approx((400 - 150) / 400, abs=0.001)          # ...but 62% under TCGplayer's number
    assert d.conservative_discount == d.vs_ebay


def test_too_few_comparable_listings_means_no_judgement(matcher):
    rows = scan(matcher, [200, 210, 220, 230, 240])                           # 5 < min_cohort of 6
    assert len(deals(rows)) == 5
    d = rows[0].verdict.deal
    assert d.ebay_typical is None and d.vs_ebay is None and d.cohort_size == 0
    assert d.conservative_discount == d.discount


def test_a_genuine_bargain_among_normally_priced_listings_survives(matcher):
    """Listings clustered near TCGplayer's price, one well under: that one is a real deal on both measures."""
    rows = scan(matcher, [380, 390, 395, 400, 405, 410, 420, 250])
    assert deals(rows) == [250.0]
    d = next(r.verdict.deal for r in rows if r.listing.price == 250)
    assert d.vs_ebay > 0.35 and d.discount > 0.35


def test_fakes_and_wrong_prints_do_not_drag_the_median_down(matcher):
    """Suspiciously cheap listings (>65% under) are left out of the comparison so they cannot make real
    listings look expensive."""
    rows = scan(matcher, [230, 240, 250, 260, 270, 280, 40, 45, 50, 55])      # four ~$50 "too good to be true"
    assert all(r.verdict.deal.cohort_size == 6 for r in rows if r.verdict.deal.discount <= 0.65)
    assert next(r.verdict.deal for r in rows if r.listing.price == 250).ebay_typical == 255.0


def test_different_cards_are_never_compared_with_each_other(matcher):
    other = listings([20, 21, 22, 23, 24, 25], title="Furret 136/189 Darkness Ablaze Reverse Holo NM", prefix="f")
    rows = scan_listings(listings([190, 200, 210, 220, 230, 240]) + other, matcher, Settings())
    furret = [r for r in rows if r.listing.id.startswith("f")]
    assert all(r.verdict.deal.cohort_size == 6 for r in furret)
    assert all(r.verdict.deal.ebay_typical == pytest.approx(22.5) for r in furret)   # not dragged up by Charizards


def test_condition_is_put_on_a_common_footing_before_comparing(matcher):
    """An HP copy at $120 and an NM copy at $240 are the same price once condition is accounted for (HP = 50%)."""
    mixed = []
    for i, (price, cond) in enumerate([(240, "NM"), (238, "NM"), (242, "NM"), (239, "NM"), (241, "NM"), (120, "HP")]):
        mixed.append(Listing(id=f"m{i}", title=TITLE, price=price, shipping=0.0, seller_feedback_pct=99.5,
                             seller_feedback_score=900, card_condition=cond))
    rows = scan_listings(mixed, matcher, Settings())
    hp = next(r.verdict.deal for r in rows if r.listing.id == "m5")
    assert hp.condition == "HP" and hp.ebay_typical == pytest.approx(120.0, abs=1.0)     # typical *HP* price
    assert abs(hp.vs_ebay) < 0.02                                                        # so it is not a bargain


def test_the_check_can_be_loosened(matcher):
    rows = scan(matcher, [190, 200, 210, 220, 230, 240, 250, 260, 150], max_cost_vs_ebay_median=1.2)
    assert len(deals(rows)) == 9

