import pytest

from pokedeals import watchlist as wl


def ids(rows):
    return [r["id"] for r in rows]


def test_search_by_name_ranks_by_value(matcher):
    # both Charizards, the pricier print first
    assert ids(wl.search_cards(matcher.conn, "charizard")) == ["base1-4", "dup1-4"]


def test_search_words_match_name_or_set(matcher):
    assert ids(wl.search_cards(matcher.conn, "char base")) == ["base1-4"]
    assert ids(wl.search_cards(matcher.conn, "darkness")) == ["swsh3-136"]


@pytest.mark.parametrize("text,expected", [
    ("4/102", ["base1-4", "dup1-4"]),
    ("58/102", ["base1-58"]),
    ("Furret 136 / 189", ["swsh3-136"]),
    ("pikachu 58", ["base1-58"]),          # bare number acts as a card number
    ("charizard 4/102 some other", ["dup1-4"]),
])
def test_search_numbers(matcher, text, expected):
    assert ids(wl.search_cards(matcher.conn, text)) == expected


@pytest.mark.parametrize("text", ["", "   ", "zzzzzz", "%", "_", "'; DROP TABLE cards;--", "100%"])
def test_search_junk_is_safe_and_empty(matcher, text):
    assert wl.search_cards(matcher.conn, text) == []
    assert matcher.conn.execute("SELECT count(*) FROM cards").fetchone()[0] == 4


def test_search_limit(matcher):
    assert len(wl.search_cards(matcher.conn, "charizard", limit=1)) == 1


def add_card_with_prices(conn, cid, name, prices):
    conn.execute("INSERT INTO cards (id,set_id,local_id,local_key,name,name_key) VALUES (?,?,?,?,?,?)",
                 (cid, "base1", cid.split("-")[-1], cid.split("-")[-1], name, name.lower()))
    for variant, market in prices.items():
        conn.execute("INSERT INTO card_prices (card_id,variant,market) VALUES (?,?,?)", (cid, variant, market))
    conn.commit()


def test_one_market_price_with_reverse_and_stamped_beside_it(matcher):
    c = matcher.conn
    charizard = wl.get_card(c, "base1-4")        # holofoil 400, 1st-edition-holofoil 5000
    assert charizard["market_price"] == 400.0    # the regular print, NOT pulled up by the 1st Edition
    assert charizard["stamped_price"] == 5000.0 and charizard["reverse_price"] is None
    assert charizard["regular_count"] == 1

    furret = wl.get_card(c, "swsh3-136")         # normal 0.30, reverse-holofoil 0.50
    assert (furret["market_price"], furret["reverse_price"], furret["stamped_price"]) == (0.30, 0.50, None)


def test_reverse_holo_is_never_averaged_into_the_market_price(matcher):
    """Real case: Legendary Collection Slowpoke is $1.65 normal and $1,000 reverse holo; a plain mean
    would show $500.82, a price no copy sells for."""
    add_card_with_prices(matcher.conn, "base1-93", "Slowpoke", {"normal": 1.65, "reverse-holofoil": 1000.0})
    card = wl.get_card(matcher.conn, "base1-93")
    assert card["market_price"] == 1.65 and card["reverse_price"] == 1000.0


def test_several_regular_prints_are_averaged(matcher):
    add_card_with_prices(matcher.conn, "base1-90", "Azumarill",
                         {"normal": 7.00, "holofoil": 55.00, "reverse-holofoil": 85.65})
    card = wl.get_card(matcher.conn, "base1-90")
    assert card["market_price"] == 31.00         # mean of normal and holofoil; reverse kept apart
    assert card["reverse_price"] == 85.65 and card["regular_count"] == 2


def test_shadowless_is_shown_apart_and_never_averaged_into_the_market_price(matcher):
    """Base Set Charizard: $955 ordinary vs $2,258 Shadowless. Averaging them would show a price for neither."""
    add_card_with_prices(matcher.conn, "base1-91", "Zard", {"holofoil": 954.65, "shadowless-holofoil": 2257.87,
                                                            "1st-edition-holofoil": 9000.0})
    card = wl.get_card(matcher.conn, "base1-91")
    assert card["market_price"] == 954.65 and card["regular_count"] == 1
    assert card["shadowless_price"] == 2257.87 and card["stamped_price"] == 9000.0 and card["reverse_price"] is None


def test_a_card_with_only_shadowless_prices_still_sorts(matcher):
    add_card_with_prices(matcher.conn, "base1-92", "Shadowy", {"shadowless-holofoil": 120.0})
    card = wl.get_card(matcher.conn, "base1-92")
    assert card["market_price"] is None and card["sort_price"] == 120.0


def test_card_with_only_a_reverse_print_has_no_market_price_but_still_sorts(matcher):
    c = matcher.conn
    add_card_with_prices(c, "base1-95", "Entei", {"reverse-holofoil": 28.64})
    card = wl.get_card(c, "base1-95")
    assert card["market_price"] is None and card["reverse_price"] == 28.64
    assert card["sort_price"] == 28.64
    ids = [x["id"] for x in wl.search_cards_page(c, "set", sort="price_asc", limit=50).cards]
    assert ids.index("base1-95") < ids.index("base1-4")       # $28.64 sorts below Charizard's $400


def test_search_pages_cover_every_match_exactly_once(matcher):
    c = matcher.conn
    first = wl.search_cards_page(c, "charizard", limit=1, offset=0)
    second = wl.search_cards_page(c, "charizard", limit=1, offset=1)
    beyond = wl.search_cards_page(c, "charizard", limit=1, offset=2)
    assert first.total == second.total == beyond.total == 2     # total is the full match count
    assert [x["id"] for x in first.cards + second.cards] == ["base1-4", "dup1-4"]
    assert beyond.cards == []


def test_search_page_total_ignores_page_size(matcher):
    assert wl.search_cards_page(matcher.conn, "charizard", limit=1).total == 2
    assert wl.search_cards_page(matcher.conn, "zzzz").total == 0
    assert wl.search_cards_page(matcher.conn, "").total == 0


def test_watchlist_add_remove_is_idempotent(matcher):
    c = matcher.conn
    assert wl.add_card(c, "nope-1") is False
    assert wl.add_card(c, "base1-4") and wl.add_card(c, "base1-4")
    assert ids(wl.list_watchlist(c)) == ["base1-4"]
    assert wl.search_cards(c, "charizard")[0]["watched"] is True
    assert wl.search_cards(c, "charizard")[1]["watched"] is False
    assert wl.remove_card(c, "base1-4") is True
    assert wl.remove_card(c, "base1-4") is False
    assert wl.list_watchlist(c) == []


def test_signatures_and_queries_dedupe_across_sets(matcher):
    c = matcher.conn
    for card_id in ("base1-4", "dup1-4", "swsh3-136"):
        wl.add_card(c, card_id)
    # the two Charizards are the same card as far as listings go
    assert len(wl.watch_signatures(c)) == 2
    assert sorted(wl.watch_queries(c, 10)) == ["Charizard 4/102", "Furret 136/189"]
    assert len(wl.watch_queries(c, 1)) == 1
