"""EbaySource must page through results and say when it stopped short. Uses a fake eBay (no network)."""
from dataclasses import replace

import httpx
import pytest

from pokedeals.config import Settings
from pokedeals.ebay import EbaySource


class FakeEbay:
    """Serves `total` synthetic items, honouring limit/offset like the Browse API."""

    def __init__(self, total, *, overlap=0):
        self.total = total
        self.overlap = overlap          # re-send this many items from the previous page (listings shift)
        self.calls = []                 # (offset, limit) per search request

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params["limit"])
        self.calls.append((offset, limit))
        start = max(0, offset - self.overlap) if offset else 0
        ids = range(start, min(offset + limit, self.total))
        body = {"total": self.total, "offset": offset, "limit": limit}
        if ids:
            body["itemSummaries"] = [
                {"itemId": f"v1|{i}|0", "title": f"Charizard 4/102 #{i}",
                 "price": {"value": "10.00", "currency": "USD"}} for i in ids]
        return httpx.Response(200, json=body)


def source(fake, **settings):
    settings.setdefault("ebay_label_conditions", False)      # these tests count base pages; labelling has its own
    s = replace(Settings(), ebay_client_id="id", ebay_client_secret="secret", **settings)
    return EbaySource(s, client=httpx.Client(transport=httpx.MockTransport(fake.handler)))


def test_pages_until_default_cap_and_reports_total():
    fake = FakeEbay(450)
    r = source(fake, ebay_max_listings_per_query=400).search("charizard")
    assert len(r.listings) == 400 and r.total == 450 and r.truncated
    assert fake.calls == [(0, 200), (200, 200)]


def test_stops_when_everything_fetched():
    fake = FakeEbay(120)
    r = source(fake).search("charizard")
    assert len(r.listings) == 120 and r.total == 120 and not r.truncated
    assert fake.calls == [(0, 200)]


def test_explicit_max_results_overrides_default_cap():
    fake = FakeEbay(450)
    r = source(fake, ebay_max_listings_per_query=200).search("charizard", max_results=450)
    assert len(r.listings) == 450 and not r.truncated
    assert fake.calls == [(0, 200), (200, 200), (400, 50)]   # last page asks only for what it needs


def test_no_results():
    fake = FakeEbay(0)
    r = source(fake).search("zzzz")
    assert r.listings == [] and r.total == 0 and not r.truncated


def test_duplicates_from_shifting_pages_are_dropped():
    fake = FakeEbay(300, overlap=5)
    r = source(fake, ebay_max_listings_per_query=400).search("charizard")
    ids = [l.id for l in r.listings]
    assert len(ids) == len(set(ids)) == 300


def test_http_errors_propagate():
    def boom(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        return httpx.Response(500, json={})
    s = replace(Settings(), ebay_client_id="id", ebay_client_secret="secret")
    src = EbaySource(s, client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(httpx.HTTPStatusError):
        src.search("charizard")
