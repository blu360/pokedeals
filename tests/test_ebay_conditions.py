"""Labelling listings with the seller's Card Condition, using eBay's aspect filter (verified against live eBay)."""
from dataclasses import replace

import httpx
import pytest

from pokedeals.config import Settings
from pokedeals.ebay import EbaySource
from pokedeals.parser import parse_title
from pokedeals.scorer import evaluate

NM, LP, MP, HP = ("Near Mint or Better", "Lightly Played (Excellent)", "Moderately Played (Very Good)",
                  "Heavily Played (Poor)")


class FakeEbay:
    """Serves items and honours aspect_filter=...Card Condition:{value} like the real Browse API."""

    def __init__(self, conditions, *, fail_aspect=False, multi=None):
        self.conditions = conditions            # list of eBay condition strings or None, one per item
        self.fail_aspect = fail_aspect
        self.multi = multi or {}                # index -> extra condition value the item ALSO matches
        self.calls = []                         # (aspect value or None, offset, limit)
        self.filters = []                       # every aspect_filter string received

    def values(self, i):
        vals = [self.conditions[i]] if self.conditions[i] else []
        return vals + ([self.multi[i]] if i in self.multi else [])

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        p = request.url.params
        aspect = None
        if "aspect_filter" in p:
            raw = p["aspect_filter"]
            assert raw.startswith("categoryId:183454,"), raw
            self.filters.append(raw)
            if "Card Condition:{" in raw:
                aspect = raw[raw.index("Card Condition:{") + 16:].split("}")[0]
                if self.fail_aspect:
                    return httpx.Response(500, json={})
        offset, limit = int(p.get("offset", 0)), int(p["limit"])
        self.calls.append((aspect, offset, limit))
        idx = [i for i in range(len(self.conditions)) if aspect is None or aspect in self.values(i)]
        page = idx[offset:offset + limit]
        body = {"total": len(idx), "offset": offset, "limit": limit}
        if page:
            body["itemSummaries"] = [{"itemId": f"v1|{i}|0", "title": f"Charizard 4/102 #{i}",
                                      "price": {"value": "10.00", "currency": "USD"}} for i in page]
        return httpx.Response(200, json=body)


def source(fake, **settings):
    s = replace(Settings(), ebay_client_id="id", ebay_client_secret="secret", **settings)
    return EbaySource(s, client=httpx.Client(transport=httpx.MockTransport(fake.handler)))


def by_index(result):
    return {int(l.id.split("|")[1]): l.card_condition for l in result.listings}


def test_listings_get_the_sellers_condition():
    fake = FakeEbay([NM, LP, MP, HP, None, NM])
    got = by_index(source(fake).search("charizard 4/102"))
    assert got == {0: "NM", 1: "LP", 2: "MP", 3: "HP", 4: None, 5: "NM"}      # no label stays None, never guessed


def test_one_base_search_plus_one_per_condition():
    fake = FakeEbay([NM, LP, MP, HP, NM])
    source(fake).search("charizard 4/102")
    aspects = [a for a, _, _ in fake.calls]
    assert aspects == [None, NM, LP, MP, HP]                 # 5 calls, in the order of worst-last labelling


def test_an_item_under_two_conditions_keeps_the_worse():
    fake = FakeEbay([NM, LP], multi={0: HP})                 # item 0 is tagged both NM and HP
    assert by_index(source(fake).search("x"))[0] == "HP"


def test_labelling_can_be_turned_off_to_save_api_calls():
    fake = FakeEbay([NM, LP])
    r = source(fake, ebay_label_conditions=False).search("x")
    assert [a for a, _, _ in fake.calls] == [None] and by_index(r) == {0: None, 1: None}


def test_a_failing_label_search_does_not_lose_the_listings():
    fake = FakeEbay([NM, LP, MP], fail_aspect=True)
    r = source(fake).search("x")
    assert len(r.listings) == 3 and by_index(r) == {0: None, 1: None, 2: None}


def test_no_category_means_no_labelling():
    fake = FakeEbay([NM, LP])
    r = source(fake, ebay_category_ids="").search("x")
    assert [a for a, _, _ in fake.calls] == [None] and r.listings[0].card_condition is None


def test_no_results_means_no_label_calls():
    fake = FakeEbay([])
    source(fake).search("x")
    assert [a for a, _, _ in fake.calls] == [None]


def test_labels_cover_listings_beyond_the_first_page_of_each_condition():
    """450 items, cap 400: the NM subset alone spans two pages, and labelling must page through it."""
    conds = [NM if i % 2 == 0 else LP for i in range(450)]
    fake = FakeEbay(conds)
    r = source(fake, ebay_max_listings_per_query=400).search("x")
    assert len(r.listings) == 400
    got = by_index(r)
    assert all(got[i] == ("NM" if i % 2 == 0 else "LP") for i in range(400))      # none missed, none wrong
    nm_pages = [(o, n) for a, o, n in fake.calls if a == NM]
    assert nm_pages[0] == (0, 200) and len(nm_pages) >= 2


# ---- the scorer uses it ------------------------------------------------------------------------

def listing_with(card_condition=None, title="Charizard 4/102 Base Set Holo", price=300.0):
    from pokedeals.models import Listing
    return Listing(id="t", title=title, price=price, shipping=0.0, seller_feedback_pct=99.5,
                   seller_feedback_score=900, card_condition=card_condition)


@pytest.fixture()
def priced(matcher):
    return matcher


def verdict(matcher, **kw):
    listing = listing_with(**kw)
    parsed = parse_title(listing.title)
    return evaluate(listing, parsed, matcher.match(parsed), Settings())


def test_sellers_near_mint_means_full_market_value_not_the_lp_assumption(matcher):
    v = verdict(matcher, card_condition="NM")                       # market $400
    assert v.deal.condition == "NM" and v.deal.fair_value == 400.0
    assert "condition-seller-stated" in v.deal.flags and not any(f.startswith("condition-assumed") for f in v.deal.flags)


def test_sellers_heavily_played_lowers_the_fair_value(matcher):
    v = verdict(matcher, card_condition="HP")
    assert v.deal.condition == "HP" and v.deal.fair_value == 200.0


def test_unlabelled_listing_still_falls_back_to_the_assumption(matcher):
    v = verdict(matcher, card_condition=None)
    assert v.deal.condition == "LP" and "condition-assumed-LP" in v.deal.flags


@pytest.mark.parametrize("stated,titled,expected", [
    ("NM", "heavily played", "HP"),          # the title is more pessimistic: believe it
    ("HP", "near mint", "HP"),               # so is the seller's field
    ("LP", "damaged", "DMG"),                # the field has no DMG, the title can say it
    ("MP", "mp", "MP"),
])
def test_when_seller_field_and_title_disagree_the_worse_wins(matcher, stated, titled, expected):
    v = verdict(matcher, card_condition=stated, title=f"Charizard 4/102 Base Set Holo {titled}")
    assert v.deal.condition == expected


# ---- language: TCGplayer prices are for English cards ------------------------------------------------

def test_searches_are_limited_to_english_listings_by_default():
    fake = FakeEbay([None, None])
    source(fake, ebay_label_conditions=False).search("pokemon venusaur")
    assert fake.filters == ["categoryId:183454,Language:{English}"]


def test_the_language_filter_rides_along_with_the_condition_labelling():
    fake = FakeEbay(["Near Mint or Better", None])
    source(fake).search("pokemon venusaur")
    assert "categoryId:183454,Card Condition:{Near Mint or Better},Language:{English}" in fake.filters
    assert all("Language:{English}" in f for f in fake.filters)


def test_languages_are_configurable_and_can_be_switched_off():
    fake = FakeEbay([None])
    source(fake, ebay_label_conditions=False, ebay_languages=("English", "Japanese")).search("x")
    assert fake.filters == ["categoryId:183454,Language:{English|Japanese}"]
    fake = FakeEbay([None])
    source(fake, ebay_label_conditions=False, ebay_languages=()).search("x")
    assert fake.filters == []
