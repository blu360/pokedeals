import pytest

from pokedeals import catalog
from pokedeals import watchlist as wl
from pokedeals.textnorm import local_key, name_key


@pytest.fixture()
def conn(matcher):
    """The shared seeded DB plus extra Base Set cards so ordering is observable."""
    c = matcher.conn
    extra = [  # id, local_id, name, holofoil market (None = no price)
        ("base1-1", "1", "Alakazam", None),
        ("base1-2", "2", "Blastoise", 226.0),
        ("base1-10", "10", "Mewtwo", 90.0),
        ("base1-TG01", "TG01", "Zeta", 5.0),
    ]
    for cid, lid, name, market in extra:
        c.execute("INSERT INTO cards (id,set_id,local_id,local_key,name,name_key) VALUES (?,?,?,?,?,?)",
                  (cid, "base1", lid, local_key(lid), name, name_key(name)))
        if market is not None:
            c.execute("INSERT INTO card_prices (card_id,variant,market) VALUES (?,?,?)", (cid, "holofoil", market))
    c.commit()
    return c


def names(page):
    return [c["name"] for c in page.cards]


# ---- cards inside a set ----

def test_set_cards_by_card_number_is_natural_order(conn):
    # 1, 2, 4, 10, 58 ... then the lettered gallery card; "10" must not sort before "2"
    assert names(catalog.set_cards(conn, "base1", "number")) == [
        "Alakazam", "Blastoise", "Charizard", "Mewtwo", "Pikachu", "Zeta"]


def test_set_cards_by_price_unpriced_last_both_ways(conn):
    assert names(catalog.set_cards(conn, "base1", "price_desc")) == [
        "Charizard", "Blastoise", "Mewtwo", "Pikachu", "Zeta", "Alakazam"]
    assert names(catalog.set_cards(conn, "base1", "price_asc")) == [
        "Zeta", "Pikachu", "Mewtwo", "Blastoise", "Charizard", "Alakazam"]


def test_set_cards_alphabetical(conn):
    assert names(catalog.set_cards(conn, "base1", "alpha")) == [
        "Alakazam", "Blastoise", "Charizard", "Mewtwo", "Pikachu", "Zeta"]


def test_set_cards_paging_and_total(conn):
    p1 = catalog.set_cards(conn, "base1", "number", limit=4, offset=0)
    p2 = catalog.set_cards(conn, "base1", "number", limit=4, offset=4)
    assert p1.total == p2.total == 6
    assert names(p1) + names(p2) == ["Alakazam", "Blastoise", "Charizard", "Mewtwo", "Pikachu", "Zeta"]


def test_set_cards_only_that_set(conn):
    assert names(catalog.set_cards(conn, "swsh3")) == ["Furret"]
    assert catalog.set_cards(conn, "nope").total == 0


def test_unknown_sort_rejected(conn):
    with pytest.raises(ValueError):
        catalog.set_cards(conn, "base1", "random")
    with pytest.raises(ValueError):
        catalog.list_sets(conn, sort="random")


# ---- the sets list ----

def test_sets_alpha_and_value(conn):
    alpha = catalog.list_sets(conn, sort="alpha")
    assert [s["name"] for s in alpha.sets] == ["Base Set", "Darkness Ablaze", "Some Other Set"]
    by_value = catalog.list_sets(conn, sort="value")
    # value = each card's cheapest priced variant, summed: Base Set 400+10+226+90+5 = 731
    assert [s["name"] for s in by_value.sets] == ["Base Set", "Some Other Set", "Darkness Ablaze"]
    base = by_value.sets[0]
    assert base["set_value"] == 731.0
    assert base["cards_in_db"] == 6 and base["priced_cards"] == 5     # Alakazam has no price


def test_sets_filter_and_total(conn):
    page = catalog.list_sets(conn, q="darkness")
    assert [s["id"] for s in page.sets] == ["swsh3"] and page.total == 1
    assert catalog.list_sets(conn, q="zzzz").total == 0
    assert catalog.list_sets(conn, q="").total == 3
    assert catalog.list_sets(conn, q="swsh3").total == 1          # set ids are searchable too


def test_set_lookup(conn):
    assert catalog.get_set(conn, "base1")["name"] == "Base Set"
    assert catalog.get_set(conn, "nope") is None


# ---- card search sort ----

def test_search_sort_by_price(conn):
    ids = lambda sort: [c["id"] for c in wl.search_cards_page(conn, "4/102", sort=sort).cards]
    assert ids("price_desc") == ["base1-4", "dup1-4"]       # 5000 then 30
    assert ids("price_asc") == ["dup1-4", "base1-4"]


def test_search_sort_puts_unpriced_last_in_both_directions(conn):
    for sort in ("price_desc", "price_asc"):
        got = wl.search_cards_page(conn, "set", sort=sort).cards      # every card in the *Set* named sets
        assert got[-1]["name"] == "Alakazam" and got[-1]["market_high"] is None


def test_search_sort_keeps_paging_consistent(conn):
    full = [c["id"] for c in wl.search_cards_page(conn, "set", limit=50, sort="price_asc").cards]
    paged = []
    for off in range(0, len(full), 2):
        paged += [c["id"] for c in wl.search_cards_page(conn, "set", limit=2, offset=off, sort="price_asc").cards]
    assert paged == full and len(set(full)) == len(full)


def test_search_unknown_sort_rejected(conn):
    with pytest.raises(ValueError):
        wl.search_cards_page(conn, "charizard", sort="cheapest")


def test_sets_carry_a_logo_url_built_from_series_and_set_ids(conn):
    conn.execute("UPDATE sets SET serie_id = 'base' WHERE id = 'base1'")
    conn.commit()
    page = catalog.list_sets(conn)
    base = next(s for s in page.sets if s["id"] == "base1")
    assert base["logo"] == "https://assets.tcgdex.net/en/base/base1/logo.webp"
    assert catalog.get_set(conn, "base1")["logo"] == base["logo"]
    other = next(s for s in page.sets if s["id"] == "dup1")                # no series recorded: no logo, no crash
    assert other["logo"] is None
