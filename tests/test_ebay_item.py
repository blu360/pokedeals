"""eBay getItem: the seller-stated card condition, language and availability."""
from dataclasses import replace

import httpx
import pytest

from pokedeals.config import Settings
from pokedeals.ebay import EbaySource, card_condition_code

ID = "Someone-pkdeals-PRD-0123456789ab-cdef0123"
SECRET = "PRD-0123456789ab-0123-4567-89ab-cdef"


def source(item_response):
    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        return item_response(request) if callable(item_response) else item_response
    s = replace(Settings(), ebay_client_id=ID, ebay_client_secret=SECRET, ebay_env="production")
    return EbaySource(s, client=httpx.Client(transport=httpx.MockTransport(handler)))


def item(**extra):
    return httpx.Response(200, json={
        "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK", "estimatedSoldQuantity": 2}],
        "price": {"value": "75.00", "currency": "USD"}, **extra})


@pytest.mark.parametrize("text,code", [
    ("Near mint or better", "NM"), ("Near Mint or Better", "NM"), ("Lightly played (Excellent)", "LP"),
    ("Moderately played (Very good)", "MP"), ("Heavily played (Poor)", "HP"), ("Damaged", "DMG"),
    ("Not specified", None), (None, None)])
def test_card_condition_words_map_to_codes(text, code):
    assert card_condition_code(text) == code


def test_condition_is_read_from_the_listing_pages_descriptor():
    """The page shows 'Moderately played (Very good)' as a condition descriptor, not an item specific."""
    info = source(item(conditionDescriptors=[{"name": "Card Condition", "values": [
        {"content": "Moderately played (Very good)", "additionalInfo": ["Moderate creasing"]}]}])).get_item("v1|1|0")
    assert info.card_condition == "MP" and not info.gone and info.sold == 2 and info.price == 75.0


def test_condition_falls_back_to_the_item_specific():
    info = source(item(localizedAspects=[{"name": "Card Condition", "value": "Near Mint or Better"},
                                         {"name": "Language", "value": "English"}])).get_item("v1|1|0")
    assert info.card_condition == "NM" and info.english is True


def test_a_non_english_language_is_reported():
    assert source(item(localizedAspects=[{"name": "Language", "value": "Japanese"}])).get_item("v1|1|0").english is False


def test_no_stated_condition_is_none_not_a_guess():
    assert source(item()).get_item("v1|1|0").card_condition is None


def test_a_404_means_the_listing_is_gone():
    assert source(httpx.Response(404, json={"errors": [{"message": "The specified item Id was not found."}]})
                  ).get_item("v1|1|0").gone


def test_out_of_stock_means_gone():
    r = httpx.Response(200, json={"estimatedAvailabilities": [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]})
    assert source(r).get_item("v1|1|0").gone


def test_other_errors_raise_instead_of_looking_like_a_sale():
    with pytest.raises(httpx.HTTPError):
        source(httpx.Response(500, json={})).get_item("v1|1|0")


def test_the_item_id_is_url_encoded_and_calls_are_counted():
    seen = []
    src = source(lambda r: (seen.append(r), item())[1])
    src.get_item("v1|123|0")
    assert "v1%7C123%7C0" in str(seen[0].url) and src.calls == 1


def test_item_details_for_the_popup():
    detail = source(httpx.Response(200, json={
        "image": {"imageUrl": "https://i.ebayimg.com/a.jpg"},
        "additionalImages": [{"imageUrl": "https://i.ebayimg.com/b.jpg"}, {"imageUrl": "https://i.ebayimg.com/a.jpg"}],
        "conditionDescriptors": [{"name": "Card Condition", "values": [
            {"content": "Lightly played (Excellent)", "additionalInfo": ["Light edge wear", "Minor scratch"]}]}],
        "localizedAspects": [{"name": "Card Number", "value": "4/102"}, {"name": "Set", "value": "Base Set"}],
        "seller": {"username": "bob", "feedbackPercentage": "99.5", "feedbackScore": 120},
        "itemLocation": {"country": "US"}, "returnTerms": {"returnsAccepted": True}})).get_item_details("v1|1|0")
    assert detail["images"] == ["https://i.ebayimg.com/a.jpg", "https://i.ebayimg.com/b.jpg"]    # no duplicates
    assert detail["condition_notes"] == ["Light edge wear", "Minor scratch"]
    assert detail["aspects"]["Card Number"] == "4/102" and detail["seller"]["name"] == "bob"
    assert detail["location"] == "US" and detail["returns"] is True


def test_item_details_for_a_listing_that_is_gone():
    assert source(httpx.Response(404, json={})).get_item_details("v1|1|0") is None
