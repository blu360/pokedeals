"""Fake / scam detection: strong signals alone, weak signals only in pairs."""
from dataclasses import replace

import pytest

from pokedeals.config import Settings
from pokedeals.models import Listing
from pokedeals.parser import parse_title
from pokedeals.pipeline import scan_listings
from pokedeals.scorer import evaluate

TITLE = "Charizard 4/102 Base Set Holo NM"          # seeded market: $400 (Near Mint)


def verdict(matcher, price, **kw):
    kw.setdefault("seller_feedback_pct", 99.5)
    kw.setdefault("seller_feedback_score", 500)
    listing = Listing(id="t", title=TITLE, price=price, shipping=0.0, **kw)
    parsed = parse_title(TITLE)
    return evaluate(listing, parsed, matcher.match(parsed), Settings())


def test_extreme_discount_is_suspicious_even_from_a_new_seller(matcher):
    v = verdict(matcher, 4.0, seller_feedback_score=3, seller_feedback_pct=100.0)
    assert v.suspicious and v.status == "suspicious" and not v.is_deal
    assert v.label == "Suspicious"
    assert any("under the market price" in r for r in v.reasons)


def test_two_weak_signs_together_are_suspicious(matcher):
    v = verdict(matcher, 200.0, seller_feedback_score=40)          # 50% off (deep) AND a young seller
    assert v.suspicious and len(v.reasons) == 2


def test_new_seller_alone_does_not_make_a_fair_price_suspicious(matcher):
    v = verdict(matcher, 300.0, seller_feedback_score=40)          # 25% off, young seller: one weak sign
    assert v.is_deal and not v.suspicious
    assert any("only 40 feedback" in r for r in v.reasons)         # still shown as a heads-up


def test_valuable_card_from_china_or_hong_kong_is_suspicious(matcher):
    v = verdict(matcher, 300.0, item_country="HK")
    assert v.suspicious and any("Hong Kong" in r for r in v.reasons)
    assert verdict(matcher, 300.0, item_country="US").is_deal


def test_cheap_card_from_asia_is_only_weak(matcher):
    s = replace(Settings(), fake_risk_min_market=1000.0)             # the card is below the "worth faking" line
    listing = Listing(id="t", title=TITLE, price=300.0, shipping=0.0, seller_feedback_pct=99.5,
                      seller_feedback_score=500, item_country="CN")
    parsed = parse_title(TITLE)
    v = evaluate(listing, parsed, matcher.match(parsed), s)
    assert v.is_deal and not v.suspicious


def test_a_fair_priced_listing_has_no_warnings_at_all(matcher):
    v = verdict(matcher, 390.0, item_country="CN", seller_feedback_score=3)      # not cheap: nothing to be fooled by
    assert not v.suspicious and v.reasons == ()


@pytest.mark.parametrize("title", [
    "Metal Charizard (4/102) Celebrations Ultra Premium Collection Gold",
    "Charizard 4/102 Pokemon card inspired art card",
    "Charizard 4/102 fantasy holo tribute card",
    "Charizard 4/102 not an official Pokemon card",
])
def test_fake_wording_in_the_title_is_skipped(title):
    assert "junk/proxy" in parse_title(title).reasons


def test_suspicious_listings_do_not_set_the_ebay_price_norm(matcher):
    """Eight fakes at $5 must not drag the 'typical eBay price' down and make a real $390 look expensive."""
    fakes = [Listing(id=f"f{i}", title=TITLE, price=5.0, shipping=0.0, seller_feedback_pct=100.0,
                     seller_feedback_score=2 + i) for i in range(8)]
    real = [Listing(id=f"r{i}", title=TITLE, price=300.0 + i, shipping=0.0, seller_feedback_pct=99.5,
                    seller_feedback_score=900) for i in range(6)]
    rows = scan_listings(fakes + real, matcher, Settings())
    assert all(r.verdict.suspicious for r in rows if r.listing.id.startswith("f"))
    typical = [r.verdict.deal.ebay_typical for r in rows if r.listing.id.startswith("r")]
    assert all(t is not None and 250 < t < 360 for t in typical)


def test_far_under_every_other_ebay_listing_plus_new_seller_is_suspicious(matcher):
    others = [Listing(id=f"o{i}", title=TITLE, price=330.0 + i, shipping=0.0, seller_feedback_pct=99.5,
                      seller_feedback_score=900) for i in range(7)]
    odd = Listing(id="odd", title=TITLE, price=150.0, shipping=0.0, seller_feedback_pct=100.0,
                  seller_feedback_score=40)                 # ~60% under the eBay norm, plus a young seller
    rows = {r.listing.id: r for r in scan_listings(others + [odd], matcher, Settings())}
    assert rows["odd"].verdict.suspicious


def test_fifty_feedback_is_enough_to_count_as_an_established_seller(matcher):
    v = verdict(matcher, 200.0, seller_feedback_score=50)          # 50% off with exactly 50 feedback: only the deep discount
    assert not v.suspicious and v.is_deal and len(v.reasons) == 1
    assert verdict(matcher, 200.0, seller_feedback_score=49).suspicious       # 49: deep discount AND a young seller
