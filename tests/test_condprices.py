"""Per-condition market prices from TCGplayer's sales data: parsing, caching, failures, and valuation."""
import json

import httpx
import pytest

from pokedeals.condprices import CondPrice, TcgplayerConditions, condition_value, parse_history
from pokedeals.config import Settings
from pokedeals.models import Listing
from pokedeals.pipeline import scan_listings

TITLE = "Charizard 4/102 Base Set Holo"            # base1-4: market $400 in the shared test DB


def sku(variant, condition, prices, sold, language="English"):
    """One SKU of TCGplayer's price-history response: 3-day buckets, newest first in some, any order in others."""
    return {"skuId": "1", "variant": variant, "language": language, "condition": condition,
            "totalQuantitySold": str(sold),
            "buckets": [{"marketPrice": str(p), "bucketStartDate": f"2026-10-{18 - 3 * i:02d}"}
                        for i, p in enumerate(prices)]}


# what sales say a copy is worth in each condition (a Base Set Charizard-like spread, newest bucket first)
HISTORY = {"result": [
    sku("Holofoil", "Near Mint", [380, 370], 9), sku("Holofoil", "Lightly Played", [210, 205], 25),
    sku("Holofoil", "Moderately Played", [150, 148], 40), sku("Holofoil", "Heavily Played", [100], 59),
    sku("Holofoil", "Damaged", [60], 1),
    sku("Reverse Holofoil", "Near Mint", [999], 5),
    sku("Holofoil", "Near Mint", [1], 3, language="Japanese"),                     # never mixed in
]}


def fake_tcgplayer(history=HISTORY, calls=None, listings=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if "price/history" in request.url.path:
            if calls is not None:
                calls.append(request.url.path)
            return httpx.Response(200, json=history)
        body = json.loads(request.content)                 # live listings, for the dialog
        cond = body["filters"]["term"]["condition"][0]
        asks = (listings or {}).get(cond, [])
        return httpx.Response(200, json={"results": [{"totalResults": len(asks),
                                                      "results": [{"price": a} for a in asks[:1]]}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


def provider(db_path, **kw):
    return TcgplayerConditions(db_path, client=kw.pop("client", None) or fake_tcgplayer(), delay=0, **kw)


@pytest.fixture()
def priced(matcher):
    """The shared DB with a TCGplayer product id on Charizard's holofoil price."""
    matcher.conn.execute("UPDATE card_prices SET product_id = 42382 WHERE card_id='base1-4' AND variant='holofoil'")
    matcher.conn.commit()
    return matcher


# ---- parsing ---------------------------------------------------------------------------------------

def test_history_gives_the_newest_market_price_per_print_and_condition():
    rows = {(printing, cond): (price, sold, as_of) for printing, cond, price, sold, as_of in parse_history(HISTORY)}
    assert rows[("Holofoil", "LP")] == (210.0, 25, "2026-10-18")             # newest bucket, not the oldest
    assert rows[("Reverse Holofoil", "NM")][0] == 999.0
    assert len(rows) == 6 and all(p != "Japanese" for p, _ in rows)           # English only


def test_buckets_without_a_price_are_skipped_and_order_does_not_matter():
    entry = sku("Holofoil", "Near Mint", [0, 50, 60], 4)
    entry["buckets"] = [{"marketPrice": "0", "bucketStartDate": "2026-10-18"}, *reversed(entry["buckets"][1:])]
    assert parse_history({"result": [entry]})[0][2] == 50.0                  # newest bucket that has a price


def test_any_market_price_is_usable_even_with_one_sale_but_no_price_is_not():
    """A one-sale price is thin, but it is TCGplayer's own number: far better than a multiplier guess. How far to trust
    it is the 'uncertain' flag's job."""
    assert CondPrice("LP", 30.0, 10).usable and CondPrice("LP", 30.0, 1).usable and CondPrice("LP", 30.0, 0).usable
    assert not CondPrice("LP", None, 9).usable and not CondPrice("LP", 0.0, 9).usable
    assert condition_value(CondPrice("LP", 30.456, 10)) == 30.46
    assert condition_value(CondPrice("DMG", 60.0, 1)) == 60.0 and condition_value(None) is None


# ---- fetching and caching ----------------------------------------------------------------------------

def test_one_request_per_product_covers_every_print_and_condition(db_path, matcher):
    calls = []
    tc = provider(db_path, client=fake_tcgplayer(calls=calls))
    keys = [(42382, "holofoil", c) for c in ("NM", "LP", "MP", "HP")] + [(42382, "reverse-holofoil", "NM")]
    assert tc.prefetch(keys) == 1 and len(calls) == 1
    assert tc.get(matcher.conn, 42382, "holofoil", "LP").market == 210.0
    assert tc.get(matcher.conn, 42382, "reverse-holofoil", "NM").market == 999.0
    assert tc.prefetch(keys) == 0 and len(calls) == 1                         # cached


def test_a_product_with_no_sales_data_is_not_refetched_every_scan(db_path, matcher):
    calls = []
    tc = provider(db_path, client=fake_tcgplayer(history={"result": []}, calls=calls))
    tc.prefetch([(5, "holofoil", "NM")])
    tc.prefetch([(5, "holofoil", "NM")])
    assert len(calls) == 1 and tc.get(matcher.conn, 5, "holofoil", "NM") is None


def test_shadowless_tries_each_print_name(db_path, matcher):
    hist = {"result": [sku("Holofoil", "Lightly Played", [3000], 6)]}
    tc = provider(db_path, client=fake_tcgplayer(history=hist))
    tc.prefetch([(7, "shadowless-holofoil", "LP")])
    assert tc.get(matcher.conn, 7, "shadowless-holofoil", "LP").market == 3000.0


def test_a_failing_site_gives_no_prices_and_trips_the_breaker(db_path, matcher):
    tc = provider(db_path, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))), workers=1)
    assert tc.prefetch([(i, "holofoil", "NM") for i in range(10)]) == 0
    assert not tc.available                                   # stops hammering a site that refuses us
    assert tc.get(matcher.conn, 0, "holofoil", "NM") is None


def test_all_conditions_for_the_card_dialog_adds_the_live_cheapest_listing(db_path, matcher):
    tc = provider(db_path, client=fake_tcgplayer(listings={"Lightly Played": [215.0, 220.0], "Near Mint": [400.0]}))
    got = tc.all_conditions(matcher.conn, 42382, "holofoil")
    assert list(got) == ["NM", "LP", "MP", "HP", "DMG"]
    assert got["LP"].market == 210.0 and got["LP"].sold == 25 and got["LP"].lowest == 215.0 and got["LP"].listings == 2
    assert got["DMG"].sold == 1 and got["DMG"].market == 60.0 and got["DMG"].usable


# ---- valuation -----------------------------------------------------------------------------------

def scan(priced, conditions, price, condition="LP"):
    listing = Listing(id="x", title=f"{TITLE} {condition}", price=price, shipping=0.0, seller_feedback_pct=99.5,
                      seller_feedback_score=900)
    return scan_listings([listing], priced, Settings(), conditions=conditions)[0]


def test_lightly_played_is_valued_at_tcgplayers_market_price_for_that_condition(priced, db_path):
    row = scan(priced, provider(db_path), 250.0)         # old model: 400 x 0.85 = $340 -> a 26% "deal"
    d = row.verdict.deal
    assert d.fair_value == 210.0 and d.cond_sold == 25 and "condition-price-tcgplayer" in d.flags
    assert not row.verdict.is_deal                        # $250 is MORE than LPs really sell for
    assert d.factor == pytest.approx(210 / 400)


def test_a_real_discount_on_the_real_condition_price_is_still_a_deal(priced, db_path):
    row = scan(priced, provider(db_path), 150.0)         # 29% under the real $210 LP price
    assert row.verdict.is_deal and row.verdict.deal.fair_value == 210.0


def test_the_condition_price_may_exceed_the_blended_market_price(priced, db_path):
    hist = {"result": [sku("Holofoil", "Near Mint", [455], 12)]}       # sales-based NM above the $400 blend
    row = scan(priced, provider(db_path, client=fake_tcgplayer(history=hist)), 420.0, condition="NM")
    assert row.verdict.deal.fair_value == 455.0


def test_without_condition_prices_the_estimate_is_used(priced, db_path):
    row = scan(priced, None, 250.0)
    assert row.verdict.deal.fair_value == pytest.approx(340.0) and row.verdict.deal.cond_sold == 0


def test_a_condition_with_one_sale_uses_that_price_not_the_estimate(priced, db_path):
    row = scan(priced, provider(db_path), 100.0, condition="damaged")      # one Damaged sale, at $60
    d = row.verdict.deal
    assert d.fair_value == 60.0 and "condition-price-tcgplayer" in d.flags     # the multiplier would have said $140
    assert d.cond_sold == 1 and d.thin_reason.startswith("Only 1 sale")


def test_listings_far_above_market_cost_no_lookups(priced, db_path):
    calls = []
    scan(priced, provider(db_path, client=fake_tcgplayer(calls=calls)), 600.0)
    assert calls == []                                    # cannot be a deal: nothing to look up


def test_a_down_site_leaves_valuation_working(priced, db_path):
    def handler(request):
        raise httpx.ConnectError("offline")
    tc = provider(db_path, client=httpx.Client(transport=httpx.MockTransport(handler)))
    row = scan(priced, tc, 250.0)
    assert row.verdict.deal.fair_value == pytest.approx(340.0)


# ---- uncertain deals: too few sales for TCGplayer's price to be trusted -------------------------------------

def thin_history(sold):
    return {"result": [sku("Holofoil", "Lightly Played", [210], sold), sku("Holofoil", "Near Mint", [380], 20)]}


def test_a_big_saving_against_a_price_with_few_sales_is_uncertain_not_a_steal(priced, db_path):
    row = scan(priced, provider(db_path, client=fake_tcgplayer(history=thin_history(3))), 150.0)    # 29% under $210
    v = row.verdict
    assert v.is_deal and v.category == "uncertain" and v.would_be == "steal"
    assert v.deal.cond_sold == 3 and "Only 3 sales" in v.deal.thin_reason


def test_enough_sales_keeps_the_category(priced, db_path):
    v = scan(priced, provider(db_path, client=fake_tcgplayer(history=thin_history(5))), 150.0).verdict
    assert v.category == "steal" and v.deal.thin_reason is None             # exactly the threshold is established
    priced.conn.execute("DELETE FROM condition_market")                  # forget the cached history: read it again
    priced.conn.commit()
    assert scan(priced, provider(db_path, client=fake_tcgplayer(history=thin_history(4))), 150.0).verdict.category == "uncertain"


def test_a_price_with_one_sale_is_used_and_uncertain(priced, db_path):
    v = scan(priced, provider(db_path, client=fake_tcgplayer(history=thin_history(1))), 140.0).verdict
    assert v.deal.thin_reason.startswith("Only 1 sale ") and v.deal.cond_sold == 1 and v.deal.fair_value == 210.0
    assert v.category == "uncertain"


def test_the_deoxys_case_a_deal_against_a_guess_turns_out_overpriced_against_the_real_price(priced, db_path):
    """Heavily Played reverse holo: TCGplayer's own price is $49.99 (1 sale). The old 50%-of-market guess said $115, so an
    $80 listing looked 30% cheap. Against the real price it is 60% over."""
    hist = {"result": [sku("Holofoil", "Heavily Played", [49.99], 1)]}
    row = scan(priced, provider(db_path, client=fake_tcgplayer(history=hist)), 80.0, condition="heavily played")
    assert row.verdict.deal.fair_value == 49.99
    assert row.verdict.category == "overpriced" and not row.verdict.is_deal


def test_no_sales_for_the_print_or_condition_at_all_is_uncertain(priced, db_path):
    v = scan(priced, provider(db_path, client=fake_tcgplayer(history={"result": []})), 150.0).verdict
    assert v.deal.thin_reason == "TCGplayer has no price for this card in this condition (the worth is an estimate)"
    assert v.category == "uncertain"


def test_uncertainty_only_matters_for_deals(priced, db_path):
    v = scan(priced, provider(db_path, client=fake_tcgplayer(history=thin_history(2))), 400.0).verdict   # 90% over: no deal anyway
    assert v.category == "overpriced" and v.would_be == ""


def test_an_unreachable_site_is_not_mistaken_for_no_sales(priced, db_path):
    def handler(request):
        raise httpx.ConnectError("offline")
    tc = provider(db_path, client=httpx.Client(transport=httpx.MockTransport(handler)))
    v = scan(priced, tc, 150.0).verdict
    assert v.deal.thin_reason is None and v.category in ("steal", "deal")       # nothing was learned, so nothing is claimed
