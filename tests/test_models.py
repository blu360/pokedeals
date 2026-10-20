"""Listing.from_browse_item against item shapes seen in real eBay production responses."""
from pokedeals.models import Listing


def item(**over):
    base = {
        "itemId": "v1|123|0", "title": "Charizard 4/102 Base Set Holo", "itemWebUrl": "https://www.ebay.com/itm/123",
        "price": {"value": "300.00", "currency": "USD"}, "condition": "Ungraded", "conditionId": "4000",
        "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
        "seller": {"username": "someone", "feedbackPercentage": "99.5", "feedbackScore": 1234},
        "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "4.95", "currency": "USD"}}],
    }
    return {**base, **over}


def test_a_normal_listing():
    l = Listing.from_browse_item(item())
    assert (l.price, l.shipping, l.graded, l.pickup_only) == (300.0, 4.95, False, False)
    assert l.seller_feedback_pct == 99.5 and l.seller_feedback_score == 1234 and l.end_time is None


def test_graded_is_read_from_the_condition_id_not_the_translated_name():
    """Real responses contain 'Valutata' and 'Gradée' for the same Graded condition."""
    for name in ("Graded", "Valutata", "Gradée"):
        assert Listing.from_browse_item(item(condition=name, conditionId="2750")).graded is True
    assert Listing.from_browse_item(item(condition="Graded", conditionId="4000")).graded is False


def test_calculated_shipping_without_a_cost_is_unknown_not_free():
    l = Listing.from_browse_item(item(shippingOptions=[{"shippingCostType": "CALCULATED"}]))
    assert l.shipping is None


def test_free_shipping_is_zero_not_unknown():
    l = Listing.from_browse_item(item(shippingOptions=[
        {"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}]))
    assert l.shipping == 0.0


def test_no_shipping_but_pickup_means_pickup_only():
    l = Listing.from_browse_item(item(shippingOptions=None, pickupOptions=[{"pickupLocationType": "STORE"}]))
    assert l.pickup_only is True
    assert Listing.from_browse_item(item(shippingOptions=None)).pickup_only is False      # unknown, not pickup


def test_brand_new_seller_values_are_kept_as_zero():
    l = Listing.from_browse_item(item(seller={"username": "new", "feedbackPercentage": "0.0", "feedbackScore": 0}))
    assert (l.seller_feedback_pct, l.seller_feedback_score) == (0.0, 0)


def test_a_buy_it_now_plus_auction_listing_uses_the_buy_it_now_price():
    l = Listing.from_browse_item(item(buyingOptions=["FIXED_PRICE", "AUCTION"], itemEndDate="2026-10-16T17:02:36.000Z",
                                      currentBidPrice={"value": "200.00", "currency": "USD"}))
    assert l.price == 300.0 and not l.is_auction_only and l.end_time is not None
