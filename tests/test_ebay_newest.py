"""search_newest: catch everything listed since the last look with as few calls as possible."""
from dataclasses import replace

import httpx

from pokedeals.config import Settings
from pokedeals.ebay import EbaySource


def source(total_items, calls):
    """A fake eBay holding `total_items` listings, newest first: ids N-1 ... 0."""
    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        p = request.url.params
        offset, limit = int(p["offset"]), int(p["limit"])
        calls.append(offset)
        ids = list(range(total_items - 1 - offset, max(total_items - 1 - offset - limit, -1), -1))
        return httpx.Response(200, json={"total": total_items, "itemSummaries": [
            {"itemId": f"v1|{i}|0", "title": f"Charizard 4/102 #{i}", "price": {"value": "10", "currency": "USD"}}
            for i in ids]})
    s = replace(Settings(), ebay_client_id="id", ebay_client_secret="secret")
    return EbaySource(s, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_one_call_when_a_known_listing_is_on_the_first_page():
    calls = []
    known = {f"v1|{i}|0" for i in range(0, 900)}                 # we have seen everything older than #900
    result = source(1000, calls).search_newest("pokemon", known.__contains__)
    assert calls == [0] and len(result.listings) == 200


def test_pages_deeper_only_while_whole_pages_are_new():
    calls = []
    known = {f"v1|{i}|0" for i in range(0, 450)}                 # 550 listings are new
    result = source(1000, calls).search_newest("pokemon", known.__contains__)
    assert calls == [0, 200, 400] and len(result.listings) == 600


def test_stops_at_max_pages_even_if_everything_is_new():
    calls = []
    source(5000, calls).search_newest("pokemon", lambda _id: False, max_pages=2)
    assert calls == [0, 200]


def test_a_short_page_means_there_is_nothing_older():
    calls = []
    result = source(50, calls).search_newest("pokemon", lambda _id: False)
    assert calls == [0] and len(result.listings) == 50
