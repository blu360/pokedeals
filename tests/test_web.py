import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from pokedeals import web
from pokedeals.config import Settings
from pokedeals.ebay import SearchResult
from pokedeals.models import Listing

LISTINGS = [
    {"id": "a", "title": "Charizard 4/102 Base Set Holo NM", "price": 280, "shipping": 5,
     "url": "https://www.ebay.com/itm/1", "seller_feedback_pct": 99.5, "seller_feedback_score": 900},
    {"id": "b", "title": "Charizard 4/102 Base Set Holo NM", "price": 395, "shipping": 5,
     "seller_feedback_pct": 99.5, "seller_feedback_score": 900},
    {"id": "c", "title": "Charizard 4/102 Base Set PSA 10", "price": 100, "shipping": 5},
    {"id": "d", "title": "<script>alert(1)</script> Furret 136/189 NM", "price": 0.20, "shipping": 0,
     "seller_feedback_pct": 99.5, "seller_feedback_score": 900},
]


@pytest.fixture()
def client(db_path, tmp_path):
    fixture = tmp_path / "listings.json"
    fixture.write_text(json.dumps(LISTINGS), encoding="utf-8")
    # min_savings=0: Furret is a 30-cent card, so the dollar floor would hide the only cheap-card case here
    app = web.create_app(replace(Settings(), db_path=db_path, min_savings=0.0), fixture=fixture)
    return TestClient(app)


def test_index_serves_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "pokedeals" in r.text


def test_status(client):
    s = client.get("/api/status").json()
    assert s["mode"] == "sample"
    assert s["db"]["cards"] == 4 and s["db"]["sets"] == 3
    assert s["settings"]["min_discount"] == 0.10


def test_scan_orders_deals_first_and_explains_rejections(client):
    data = client.get("/api/scan").json()
    assert data["count"] == 4 and data["deals"] == 2
    rows = data["rows"]
    assert [r["is_deal"] for r in rows] == [True, True, False, False]
    assert [r["id"] for r in rows[:2]] == ["d", "a"]             # biggest discount first
    by_id = {r["id"]: r for r in rows}
    assert by_id["a"]["card"]["name"] == "Charizard" and by_id["a"]["valuation"]["discount"] > 0.2
    assert by_id["b"]["why"].startswith("discount")
    assert "graded" in by_id["c"]["why"] and by_id["c"]["valuation"] is None


def test_evaluate_manual_listing(client):
    r = client.post("/api/evaluate", json={"title": "Charizard 4/102 Base Set Holo NM", "price": 250, "shipping": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["is_deal"] and body["card"]["set"] == "Base Set"
    assert body["parsed"]["number"] == "4" and body["parsed"]["condition"] == "NM"


@pytest.mark.parametrize("payload", [
    {"title": "", "price": 5},
    {"title": "x", "price": -1},
    {"title": "x" * 301, "price": 5},
    {"price": 5},
])
def test_evaluate_validates_input(client, payload):
    assert client.post("/api/evaluate", json=payload).status_code == 422


def watch(client, card_id):
    return client.post("/api/watchlist", json={"card_id": card_id})


def test_card_search_endpoint(client):
    cards = client.get("/api/cards", params={"q": "charizard"}).json()["cards"]
    assert [c["id"] for c in cards] == ["base1-4", "dup1-4"]
    assert cards[0]["market_high"] == 5000.0 and cards[0]["watched"] is False
    assert client.get("/api/cards", params={"q": ""}).json()["cards"] == []
    assert client.get("/api/cards", params={"q": "x", "limit": 0}).status_code == 422
    assert client.get("/api/cards", params={"q": "x", "limit": 51}).status_code == 422
    assert client.get("/api/cards", params={"q": "x", "offset": -1}).status_code == 422


def test_card_search_sort_param(client):
    asc = client.get("/api/cards", params={"q": "4/102", "sort": "price_asc"}).json()
    desc = client.get("/api/cards", params={"q": "4/102", "sort": "price_desc"}).json()
    assert [c["id"] for c in asc["cards"]] == ["dup1-4", "base1-4"] and asc["sort"] == "price_asc"
    assert [c["id"] for c in desc["cards"]] == ["base1-4", "dup1-4"]
    assert client.get("/api/cards", params={"q": "x", "sort": "cheapest"}).status_code == 422


def test_search_results_carry_one_price_plus_reverse_and_stamped(client):
    cards = {c["id"]: c for c in client.get("/api/cards", params={"q": "furret"}).json()["cards"]}
    furret = cards["swsh3-136"]
    assert furret["market_price"] == 0.30 and furret["reverse_price"] == 0.50 and furret["stamped_price"] is None
    pikachu = client.get("/api/cards", params={"q": "pikachu"}).json()["cards"][0]
    assert pikachu["market_price"] == 10.0 and pikachu["stamped_price"] == 60.0 and pikachu["reverse_price"] is None


def test_card_detail_has_large_image_and_variant_prices(client):
    r = client.get("/api/cards/base1-4").json()
    assert r["card"]["name"] == "Charizard" and r["query"] == "Charizard 4/102"
    assert [p["variant"] for p in r["prices"]] == ["holofoil", "1st-edition-holofoil"]
    assert client.get("/api/cards/nope-1").status_code == 404


def test_sets_endpoints(client):
    sets = client.get("/api/sets", params={"sort": "alpha"}).json()
    assert [s["id"] for s in sets["sets"]] == ["base1", "swsh3", "dup1"] and sets["total"] == 3
    cards = client.get("/api/sets/base1/cards", params={"sort": "price_desc"}).json()
    assert [c["id"] for c in cards["cards"]] == ["base1-4", "base1-58"] and cards["total"] == 2
    assert client.get("/api/sets/base1").json()["set"]["name"] == "Base Set"
    assert client.get("/api/sets/nope").status_code == 404
    assert client.get("/api/sets/nope/cards").status_code == 404
    assert client.get("/api/sets", params={"sort": "random"}).status_code == 422
    assert client.get("/api/sets/base1/cards", params={"sort": "random"}).status_code == 422


def test_profit_endpoint(client):
    r = client.get("/api/profit", params={"sale": 100, "cost": 40}).json()
    b = r["breakdown"]
    assert b["final_value_fee"] == 13.25 and b["order_fee"] == 0.4
    assert b["postage"] == 5.0 and b["postage_verified"] is False
    assert b["profit"] == 40.85 and b["break_even_price"] == 52.92
    assert [t["upto"] for t in r["rules"]["tiers"]] == [19.99, 250.0, 750.0, None]
    assert r["rules"]["fvf_rate"] == 0.1325

    env = client.get("/api/profit", params={"sale": 8, "cost": 3}).json()["breakdown"]
    assert env["postage"] == 0.84 and env["postage_verified"] is True and env["profit"] == 2.3

    over = client.get("/api/profit", params={"sale": 100, "cost": 40, "postage": 4, "packaging": 1,
                                             "ad_pct": 3, "shipping_charged": 5, "tax_pct": 8}).json()["breakdown"]
    assert over["postage_overridden"] and over["ad_fee"] == 3.15      # 3% of (100 + 5)


@pytest.mark.parametrize("params", [{"sale": -1}, {"sale": 5, "cost": -1}, {"sale": 5, "tax_pct": 99}, {}])
def test_profit_endpoint_validates(client, params):
    assert client.get("/api/profit", params=params).status_code == 422


def test_listing_rows_carry_a_resale_estimate(client):
    rows = {r["id"]: r for r in client.get("/api/scan").json()["rows"]}
    # bought for $285 delivered, fair value $400: FVF 53.00 + order fee 0.40, postage 5.00, packaging 0.50
    resale = rows["a"]["valuation"]["resale"]
    assert resale["sale_price"] == 400.0 and resale["profit"] == 56.10
    assert resale["postage_estimated"] is True
    assert rows["c"]["valuation"] is None                  # graded slab: nothing to estimate


def test_card_search_pages_through_every_match(client):
    p1 = client.get("/api/cards", params={"q": "charizard", "limit": 1, "offset": 0}).json()
    p2 = client.get("/api/cards", params={"q": "charizard", "limit": 1, "offset": 1}).json()
    p3 = client.get("/api/cards", params={"q": "charizard", "limit": 1, "offset": 2}).json()
    assert p1["total"] == p2["total"] == p3["total"] == 2
    assert [c["id"] for c in p1["cards"] + p2["cards"]] == ["base1-4", "dup1-4"]
    assert p3["cards"] == [] and p3["offset"] == 2


def test_watchlist_endpoints(client):
    assert watch(client, "nope-1").status_code == 404
    r = watch(client, "base1-4")
    assert r.status_code == 200 and r.json()["card"]["watched"] is True
    assert watch(client, "base1-4").status_code == 200            # idempotent
    assert [c["id"] for c in client.get("/api/watchlist").json()["cards"]] == ["base1-4"]
    assert client.get("/api/status").json()["db"]["watching"] == 1
    assert client.delete("/api/watchlist/base1-4").json() == {"removed": True}
    assert client.delete("/api/watchlist/base1-4").json() == {"removed": False}


def test_scan_scope_watchlist(client):
    empty = client.get("/api/scan", params={"scope": "watchlist"}).json()
    assert empty["empty_watchlist"] is True and empty["rows"] == []

    watch(client, "base1-4")
    data = client.get("/api/scan", params={"scope": "watchlist"}).json()
    ids = [r["id"] for r in data["rows"]]
    assert sorted(ids) == ["a", "b", "c"]                         # Furret excluded; graded slab still attributed
    assert all(r["watched"] for r in data["rows"])
    assert data["deals"] == 1 and data["empty_watchlist"] is False

    everything = client.get("/api/scan").json()                   # scope=all keeps everything, flags watched
    by_id = {r["id"]: r for r in everything["rows"]}
    assert len(by_id) == 4 and by_id["a"]["watched"] and not by_id["d"]["watched"]


def test_number_only_match_is_not_attributed_to_watched_card(db_path, tmp_path):
    fixture = tmp_path / "wrong_name.json"
    fixture.write_text(json.dumps([
        {"id": "x", "title": "Pikachu 4/102 Base Set Pokemon Card", "price": 3, "shipping": 1},
        {"id": "y", "title": "Charizard 4/102 Base Set Holo NM", "price": 300, "shipping": 5,
         "seller_feedback_pct": 99.5, "seller_feedback_score": 900},
    ]), encoding="utf-8")
    c = TestClient(web.create_app(replace(Settings(), db_path=db_path), fixture=fixture))
    watch(c, "base1-4")
    data = c.get("/api/scan", params={"scope": "watchlist"}).json()
    assert [r["id"] for r in data["rows"]] == ["y"]


def test_card_listings_compares_against_market(client):
    data = client.get("/api/cards/base1-4/listings").json()
    assert data["mode"] == "sample" and data["card"]["name"] == "Charizard"
    assert data["query"] == "Charizard 4/102"
    assert [r["id"] for r in data["rows"]] == ["a", "b", "c"]      # cheapest first, graded slab last
    assert data["rows"][-1]["valuation"] is None and "graded" in data["rows"][-1]["why"]
    s = data["stats"]
    assert s["listings"] == 2 and s["excluded"] == 1
    assert (s["cheapest"], s["median"]) == (285.0, 342.5)
    assert (s["reference_variant"], s["reference_market"]) == ("holofoil", 400.0)
    assert [p["variant"] for p in data["prices"]] == ["holofoil", "1st-edition-holofoil"]


def test_card_listings_only_that_card(client):
    rows = client.get("/api/cards/swsh3-136/listings").json()["rows"]
    assert [r["id"] for r in rows] == ["d"]


def test_card_listings_excludes_number_only_matches(db_path, tmp_path):
    fixture = tmp_path / "wrong_name.json"
    fixture.write_text(json.dumps([
        {"id": "x", "title": "Pikachu 4/102 Base Set Pokemon Card", "price": 3, "shipping": 1}]),
        encoding="utf-8")
    c = TestClient(web.create_app(replace(Settings(), db_path=db_path), fixture=fixture))
    data = c.get("/api/cards/base1-4/listings").json()
    assert data["rows"] == [] and data["stats"]["cheapest"] is None


def test_card_listings_unknown_card_404(client):
    assert client.get("/api/cards/nope-1/listings").status_code == 404


class RecordingSource:
    def __init__(self, listings=(), total=None):
        self.queries = []
        self.max_results = []
        self.listings = list(listings)
        self.total = total

    def search(self, query, *, max_results=None):
        self.queries.append(query)
        self.max_results.append(max_results)
        return SearchResult(self.listings, self.total if self.total is not None else len(self.listings))


def live_client(db_path, monkeypatch, source=None, **overrides):
    source = source or RecordingSource()
    monkeypatch.setattr(web, "make_source", lambda settings, fixture=None: (source, "live"))
    app = web.create_app(replace(Settings(), db_path=db_path, **overrides))
    return TestClient(app), source


def make_listings(n):
    return [Listing(id=f"L{i}", title=f"Charizard 4/102 Base Set Holo NM #{i}", price=300.0 + i,
                    shipping=0.0, seller_feedback_pct=99.5, seller_feedback_score=900)
            for i in range(n)]


def test_scan_reports_when_ebay_had_more_than_was_fetched(db_path, monkeypatch):
    src = RecordingSource(make_listings(3), total=1243)
    client, _ = live_client(db_path, monkeypatch, source=src)
    data = client.get("/api/scan", params={"q": "charizard"}).json()
    assert data["truncated"] == [{"query": "charizard", "fetched": 3, "total": 1243}]
    assert data["count"] == 3          # nothing silently dropped from what *was* fetched


def test_scan_not_truncated_when_everything_fetched(db_path, monkeypatch):
    client, _ = live_client(db_path, monkeypatch, source=RecordingSource(make_listings(3), total=3))
    assert client.get("/api/scan", params={"q": "charizard"}).json()["truncated"] == []


def test_card_listings_reports_fetch_coverage(db_path, monkeypatch):
    src = RecordingSource(make_listings(5), total=450)
    client, _ = live_client(db_path, monkeypatch, source=src, ebay_max_listings_per_query=400)
    data = client.get("/api/cards/base1-4/listings").json()
    assert data["fetch"] == {"fetched": 5, "available": 450, "truncated": True,
                             "requested": 400, "max": web.MAX_FETCH}
    assert data["stats"]["listings"] == 5
    assert src.max_results == [None]                      # default cap is the source's job

    data = client.get("/api/cards/base1-4/listings", params={"max_results": 1000}).json()
    assert data["fetch"]["requested"] == 1000 and src.max_results[-1] == 1000


def test_card_listings_rejects_oversized_fetch(db_path, monkeypatch):
    client, _ = live_client(db_path, monkeypatch)
    assert client.get("/api/cards/base1-4/listings", params={"max_results": web.MAX_FETCH + 1}).status_code == 422
    assert client.get("/api/cards/base1-4/listings", params={"max_results": 0}).status_code == 422


def test_live_watchlist_scan_issues_one_query_per_card(db_path, monkeypatch):
    client, source = live_client(db_path, monkeypatch)
    for card_id in ("base1-4", "dup1-4", "swsh3-136"):
        watch(client, card_id)
    assert client.get("/api/scan", params={"scope": "watchlist"}).status_code == 200
    assert sorted(source.queries) == ["Charizard 4/102", "Furret 136/189"]   # deduped across sets


def test_live_watchlist_scan_respects_query_cap(db_path, monkeypatch):
    client, source = live_client(db_path, monkeypatch, max_watchlist_queries=1)
    for card_id in ("base1-4", "swsh3-136"):
        watch(client, card_id)
    client.get("/api/scan", params={"scope": "watchlist"})
    assert len(source.queries) == 1


def test_live_card_listings_searches_that_card(db_path, monkeypatch):
    client, source = live_client(db_path, monkeypatch)
    assert client.get("/api/cards/swsh3-136/listings").status_code == 200
    assert source.queries == ["Furret 136/189"]


def test_live_scope_all_uses_search_text(db_path, monkeypatch):
    client, source = live_client(db_path, monkeypatch)
    client.get("/api/scan", params={"q": "pokemon umbreon"})
    client.get("/api/scan")
    assert source.queries == ["pokemon umbreon", "pokemon card"]


def test_live_source_failure_becomes_502(db_path, monkeypatch):
    class Boom:
        def search(self, query, *, max_results=None):
            raise httpx.ConnectError("no network")

    monkeypatch.setattr(web, "make_source", lambda settings, fixture=None: (Boom(), "live"))
    client = TestClient(web.create_app(replace(Settings(), db_path=db_path)))
    r = client.get("/api/scan", params={"q": "pokemon"})
    assert r.status_code == 502 and "eBay request failed" in r.json()["detail"]
    r = client.get("/api/cards/base1-4/listings")
    assert r.status_code == 502 and "eBay request failed" in r.json()["detail"]


# ---- buyer ZIP (entered in the UI) --------------------------------------------------------------

def test_zip_can_be_set_and_is_applied_to_the_source(db_path, monkeypatch):
    src = RecordingSource()
    src.buyer_zip = ""
    client, _ = live_client(db_path, monkeypatch, source=src)
    assert client.get("/api/status").json()["buyer_zip"] == ""
    r = client.put("/api/settings/zip", json={"zip": " 64101 "})
    assert r.status_code == 200 and r.json() == {"buyer_zip": "64101"}
    assert src.buyer_zip == "64101"                                   # the next eBay request uses it
    assert client.get("/api/status").json()["buyer_zip"] == "64101"


def test_zip_is_saved_and_reloaded_by_a_fresh_source(db_path):
    from pokedeals.db import connect, get_setting
    from pokedeals.ebay import EbaySource
    from pokedeals.pipeline import ZIP_KEY, make_source
    keys = replace(Settings(), db_path=db_path, ebay_client_id="PRD-app-PRD-x", ebay_client_secret="PRD-cert",
                   ebay_buyer_zip="10001")
    source, _ = make_source(keys)
    assert isinstance(source, EbaySource) and source.buyer_zip == "10001"          # keyset value by default
    conn = connect(db_path)
    from pokedeals.db import set_setting
    set_setting(conn, ZIP_KEY, "64101")
    assert get_setting(conn, ZIP_KEY) == "64101"
    conn.close()
    assert make_source(keys)[0].buyer_zip == "64101"                               # the UI's value wins
    conn = connect(db_path); set_setting(conn, ZIP_KEY, ""); conn.close()
    assert make_source(keys)[0].buyer_zip == "10001"                               # cleared: back to keyset


def test_zip_can_be_cleared(db_path, monkeypatch):
    src = RecordingSource(); src.buyer_zip = "64101"
    client, _ = live_client(db_path, monkeypatch, source=src)
    assert client.put("/api/settings/zip", json={"zip": ""}).json() == {"buyer_zip": ""}
    assert src.buyer_zip == ""


@pytest.mark.parametrize("bad", ["6410", "641011", "abcde", "64101-1234", "6 4101"])
def test_bad_zip_is_rejected_and_not_applied(db_path, monkeypatch, bad):
    src = RecordingSource(); src.buyer_zip = "64101"
    client, _ = live_client(db_path, monkeypatch, source=src)
    assert client.put("/api/settings/zip", json={"zip": bad}).status_code == 422
    assert src.buyer_zip == "64101"


def test_zip_is_not_offered_in_sample_mode(client):
    assert client.get("/api/status").json()["buyer_zip"] is None
    assert client.put("/api/settings/zip", json={"zip": "64101"}).status_code == 409


# ---- suspicious listings -------------------------------------------------------------------------

def test_scan_marks_fakes_suspicious_and_keeps_them_out_of_deals(db_path, monkeypatch):
    fake = Listing(id="fake", title="Charizard 4/102 Base Set Holo NM", price=5.0, shipping=0.0,
                   seller_feedback_pct=100.0, seller_feedback_score=3, item_country="HK")
    real = Listing(id="real", title="Charizard 4/102 Base Set Holo NM", price=300.0, shipping=0.0,
                   seller_feedback_pct=99.5, seller_feedback_score=900, item_country="US")
    client, _ = live_client(db_path, monkeypatch, source=RecordingSource([fake, real]))
    data = client.get("/api/scan", params={"q": "charizard"}).json()
    by_id = {r["id"]: r for r in data["rows"]}
    assert by_id["fake"]["status"] == "suspicious" and by_id["fake"]["label"] == "Suspicious"
    assert by_id["fake"]["reasons"] and not by_id["fake"]["is_deal"]
    assert by_id["real"]["status"] == "deal"
    assert data["deals"] == 1 and data["suspicious"] == 1
    assert data["rows"][0]["id"] == "real" and data["rows"][-1]["id"] == "fake"      # suspicious sink to the bottom


def test_card_listing_stats_leave_suspicious_out(db_path, monkeypatch):
    fake = Listing(id="fake", title="Charizard 4/102 Base Set Holo NM", price=5.0, shipping=0.0,
                   seller_feedback_pct=100.0, seller_feedback_score=3)
    real = Listing(id="real", title="Charizard 4/102 Base Set Holo NM", price=300.0, shipping=0.0,
                   seller_feedback_pct=99.5, seller_feedback_score=900)
    client, _ = live_client(db_path, monkeypatch, source=RecordingSource([fake, real]))
    data = client.get("/api/cards/base1-4/listings").json()
    assert data["stats"]["cheapest"] == 300.0 and data["stats"]["suspicious"] == 1
    assert data["stats"]["listings"] == 1 and data["rows"][-1]["id"] == "fake"


# ---- live feed endpoints, deals first ------------------------------------------------------------

def feed_client(db_path, monkeypatch, listings):
    from dataclasses import replace as rep
    src = RecordingSource(listings)
    monkeypatch.setattr(web, "make_source", lambda settings, fixture=None: (src, "live"))
    s = rep(Settings(), db_path=db_path, feed_enabled=True, feed_queries=("pokemon charizard",),
            feed_set_queries_per_cycle=0, max_watchlist_queries=0, min_savings=0.0)
    app = web.create_app(s, conditions=None)
    return TestClient(app), app.state.feed


def mk(i, price, **kw):
    return Listing(id=f"L{i}", title="Charizard 4/102 Base Set Holo NM", price=price, shipping=0.0,
                   seller_feedback_pct=99.5, seller_feedback_score=900, **kw)


def test_deals_endpoint_serves_the_feeds_stored_listings_best_first(db_path, monkeypatch):
    client, feed = feed_client(db_path, monkeypatch, [mk(1, 300.0), mk(2, 250.0), mk(3, 5.0, item_country="HK"), mk(4, 395.0)])
    feed.poll_once()
    data = client.get("/api/deals", params={"intl": "true", "cats": "steal,deal,suspicious"}).json()
    ids = [r["id"] for r in data["rows"]]
    assert data["deals"] == 2 and data["suspicious"] == 1 and data["feed"]["tracked"] == 4
    assert ids == ["L2", "L1", "L3"]                      # best saving first; the likely fake last
    assert data["rows"][0]["first_seen"] and "feed" in data
    newest = client.get("/api/deals", params={"sort": "newest"}).json()["rows"]
    assert {r["id"] for r in newest[:2]} == {"L1", "L2"}
    assert [r["id"] for r in client.get("/api/deals", params={"q": "nothing-like-this"}).json()["rows"]] == []


def test_categories_filter_what_is_returned_and_counts_cover_all_of_them(db_path, monkeypatch):
    # market $400: 250 = 37% off (steal), 330 = 17% (deal), 395 = fair, 450 = 12% over (overpriced)
    client, feed = feed_client(db_path, monkeypatch, [mk(1, 250.0), mk(2, 330.0), mk(3, 395.0), mk(4, 450.0)])
    feed.poll_once()

    def ids(**params):
        return [r["id"] for r in client.get("/api/deals", params=params).json()["rows"]]
    assert ids() == ["L1", "L2"]                                        # default: Steal and Deal only
    assert ids(cats="steal") == ["L1"] and ids(cats="deal") == ["L2"]
    assert ids(cats="fair") == ["L3"] and ids(cats="overpriced") == ["L4"]
    assert ids(cats="steal,deal,fair,overpriced") == ["L1", "L2", "L3", "L4"]      # best saving first
    assert ids(cats="") == []
    counts = client.get("/api/deals").json()["counts"]
    assert counts["steal"] == 1 and counts["deal"] == 1 and counts["fair"] == 1 and counts["overpriced"] == 1
    cats = {r["id"]: r["category"] for r in client.get("/api/deals", params={"cats": "steal,deal,fair,overpriced"}).json()["rows"]}
    assert cats == {"L1": "steal", "L2": "deal", "L3": "fair", "L4": "overpriced"}


def test_listings_held_back_for_non_price_reasons_are_blocked_until_asked_for(db_path, monkeypatch):
    weak = mk(1, 250.0)
    weak = Listing(**{**weak.__dict__, "seller_feedback_pct": 90.0})
    client, feed = feed_client(db_path, monkeypatch, [weak, mk(2, 250.0)])
    feed.poll_once()
    data = client.get("/api/deals").json()
    assert [r["id"] for r in data["rows"]] == ["L2"] and data["counts"]["blocked"] == 1
    shown = client.get("/api/deals", params={"blocked": "true"}).json()["rows"]
    assert {r["id"]: r["blocked"] for r in shown} == {"L1": True, "L2": False}


def test_international_sellers_are_left_out_unless_asked_for(db_path, monkeypatch):
    client, feed = feed_client(db_path, monkeypatch, [mk(1, 250.0, item_country="US"), mk(2, 250.0, item_country="GB"),
                                                      mk(3, 250.0)])                      # no country: treated as US
    feed.poll_once()
    assert sorted(r["id"] for r in client.get("/api/deals").json()["rows"]) == ["L1", "L3"]
    assert sorted(r["id"] for r in client.get("/api/deals", params={"intl": "true"}).json()["rows"]) == ["L1", "L2", "L3"]


def test_unvalued_listings_are_only_returned_on_request(db_path, monkeypatch):
    graded = Listing(id="G1", title="Charizard 4/102 Base Set PSA 9", price=500.0, shipping=0.0)
    client, feed = feed_client(db_path, monkeypatch, [mk(1, 250.0), graded])
    feed.poll_once()
    assert [r["id"] for r in client.get("/api/deals").json()["rows"]] == ["L1"]
    skipped = client.get("/api/deals", params={"cats": "skipped"}).json()
    assert [r["id"] for r in skipped["rows"]] == ["G1"] and skipped["counts"]["skipped"] == 1


def test_deals_endpoint_without_a_feed_falls_back_to_a_scan(client):
    data = client.get("/api/deals").json()
    assert data["mode"] == "sample" and data["rows"][0]["is_deal"]


def test_feed_status_and_poll_endpoints(db_path, monkeypatch):
    client, feed = feed_client(db_path, monkeypatch, [])
    assert client.get("/api/feed").json()["enabled"] is True
    assert client.post("/api/feed/poll").json() == {"queued": True}


def test_poll_endpoint_conflicts_when_there_is_no_feed(client):
    assert client.get("/api/feed").json() == {"enabled": False}
    assert client.post("/api/feed/poll").status_code == 409


def test_card_listings_put_deals_at_the_top(db_path, monkeypatch):
    mk = lambda i, p: Listing(id=f"L{i}", title="Charizard 4/102 Base Set Holo NM", price=p, shipping=0.0,
                              seller_feedback_pct=99.5, seller_feedback_score=900)
    # cheapest first would put $5 (a fake) and $390 (no saving) ahead of the real $250 deal
    client, _ = live_client(db_path, monkeypatch, source=RecordingSource([mk(1, 5.0), mk(2, 390.0), mk(3, 250.0)]))
    rows = client.get("/api/cards/base1-4/listings").json()["rows"]
    assert [r["id"] for r in rows] == ["L3", "L2", "L1"]
    assert rows[0]["is_deal"] and rows[-1]["status"] == "suspicious"


def test_card_conditions_endpoint(db_path, monkeypatch):
    import json as _json
    import httpx as _httpx
    from pokedeals.condprices import TcgplayerConditions

    history = {"result": [
        {"variant": "Holofoil", "language": "English", "condition": "Near Mint", "totalQuantitySold": "9",
         "buckets": [{"marketPrice": "390.50", "bucketStartDate": "2026-10-16"}]},
        {"variant": "Holofoil", "language": "English", "condition": "Lightly Played", "totalQuantitySold": "25",
         "buckets": [{"marketPrice": "205.00", "bucketStartDate": "2026-10-18"}]},
        {"variant": "Holofoil", "language": "English", "condition": "Damaged", "totalQuantitySold": "1",
         "buckets": [{"marketPrice": "60.00", "bucketStartDate": "2026-10-18"}]}]}

    def handler(request):
        if "price/history" in request.url.path:
            return _httpx.Response(200, json=history)
        cond = _json.loads(request.content)["filters"]["term"]["condition"][0]
        asks = {"Lightly Played": [215.0, 220.0]}.get(cond, [])
        return _httpx.Response(200, json={"results": [{"totalResults": len(asks),
                                                       "results": [{"price": a} for a in asks[:1]]}]})
    conn = web.connect(db_path)
    conn.execute("UPDATE card_prices SET product_id = 42382 WHERE card_id='base1-4' AND variant='holofoil'")
    conn.commit()
    conn.close()
    tc = TcgplayerConditions(db_path, client=_httpx.Client(transport=_httpx.MockTransport(handler)), delay=0)
    client = TestClient(web.create_app(replace(Settings(), db_path=db_path), conditions=tc))
    data = client.get("/api/cards/base1-4/conditions").json()
    assert data["available"] and len(data["variants"]) == 1
    conds = {c["code"]: c for c in data["variants"][0]["conditions"]}
    assert conds["LP"]["market"] == 205.0 and conds["LP"]["sold"] == 25 and conds["LP"]["usable"]
    assert conds["LP"]["lowest"] == 215.0 and conds["LP"]["listings"] == 2
    assert conds["NM"]["market"] == 390.5
    assert conds["DMG"]["usable"] is True and conds["DMG"]["sold"] == 1      # one sale: a real price, flagged as thin
    assert "MP" not in conds                                   # no data at all: not shown


def test_card_conditions_off(client):
    assert client.get("/api/cards/base1-4/conditions").json() == {"available": False, "variants": []}


# ---- listing popup details -------------------------------------------------------------------------

def test_listing_details_come_from_ebay(db_path, monkeypatch):
    src = RecordingSource()
    src.get_item_details = lambda item_id: (None if item_id == "gone" else
                                            {"images": ["a.jpg", "b.jpg"], "condition_notes": ["Moderate creasing"],
                                             "aspects": {"Card Number": "4/102"}, "seller": {}, "location": "US"})
    client, _ = live_client(db_path, monkeypatch, source=src)
    data = client.get("/api/listing", params={"id": "v1|1|0"}).json()
    assert data["images"] == ["a.jpg", "b.jpg"] and data["condition_notes"] == ["Moderate creasing"]
    assert client.get("/api/listing", params={"id": "gone"}).status_code == 404


def test_listing_details_need_a_live_connection(client):
    assert client.get("/api/listing", params={"id": "v1|1|0"}).status_code == 409


def test_rows_carry_the_listing_photo_and_seller(db_path, monkeypatch):
    l = Listing(id="L1", title="Charizard 4/102 Base Set Holo NM", price=300.0, shipping=0.0,
                image="https://i.ebayimg.com/images/g/x/s-l225.jpg", seller_name="bob",
                seller_feedback_pct=99.5, seller_feedback_score=900)
    client, _ = live_client(db_path, monkeypatch, source=RecordingSource([l]))
    row = client.get("/api/scan", params={"q": "x"}).json()["rows"][0]
    assert row["image"].endswith("s-l225.jpg") and row["seller_name"] == "bob"


def test_rows_link_to_the_tcgplayer_product_page(db_path, monkeypatch):
    conn = web.connect(db_path)
    conn.execute("UPDATE card_prices SET product_id = 42382 WHERE card_id='base1-4' AND variant='holofoil'")
    conn.commit()
    conn.close()
    l = Listing(id="L1", title="Charizard 4/102 Base Set Holo NM", price=300.0, shipping=0.0,
                seller_feedback_pct=99.5, seller_feedback_score=900)
    client, _ = live_client(db_path, monkeypatch, source=RecordingSource([l]))
    row = client.get("/api/scan", params={"q": "x"}).json()["rows"][0]
    assert row["card"]["tcgplayer_url"] == "https://www.tcgplayer.com/product/42382?Language=English"
    prices = client.get("/api/cards/base1-4").json()["prices"]
    assert {p["variant"]: p["product_id"] for p in prices}["holofoil"] == 42382


def test_uncertain_deals_have_their_own_filter_and_say_why(db_path, monkeypatch):
    import httpx as _httpx
    from dataclasses import replace as rep
    from pokedeals.condprices import TcgplayerConditions
    conn = web.connect(db_path)
    conn.execute("UPDATE card_prices SET product_id = 42382 WHERE card_id='base1-4' AND variant='holofoil'")
    conn.commit()
    conn.close()
    history = {"result": [{"variant": "Holofoil", "language": "English", "condition": "Near Mint", "totalQuantitySold": "3",
                           "buckets": [{"marketPrice": "400", "bucketStartDate": "2026-10-18"}]}]}
    tc = TcgplayerConditions(db_path, client=_httpx.Client(transport=_httpx.MockTransport(
        lambda r: _httpx.Response(200, json=history))), delay=0)
    src = RecordingSource([mk(1, 250.0)])
    monkeypatch.setattr(web, "make_source", lambda settings, fixture=None: (src, "live"))
    s = rep(Settings(), db_path=db_path, feed_enabled=True, feed_queries=("pokemon charizard",),
            feed_set_queries_per_cycle=0, max_watchlist_queries=0, min_savings=0.0)
    app = web.create_app(s, conditions=tc)
    client, feed = TestClient(app), app.state.feed
    feed.poll_once()
    assert client.get("/api/deals").json()["rows"] == []                             # not in Steal/Deal
    data = client.get("/api/deals", params={"cats": "steal,deal,uncertain"}).json()
    row = data["rows"][0]
    assert row["category"] == "uncertain" and row["would_be"] == "steal" and data["counts"]["uncertain"] == 1
    assert row["valuation"]["thin_reason"].startswith("Only 3 sales") and data["counts"]["steal"] == 0
