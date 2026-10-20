"""Digital-only series (Pokémon TCG Pocket) must never be stored: no physical card, nothing to sell."""
import asyncio
from dataclasses import replace

import httpx

from pokedeals import catalog
from pokedeals import watchlist as wl
from pokedeals.config import Settings
from pokedeals.db import connect, purge_digital
from pokedeals.sync import sync


def seed_pocket(conn):
    conn.execute("INSERT INTO sets (id,name,name_key,serie_id,official_count) "
                 "VALUES ('A1','Genetic Apex','genetic apex','tcgp',286)")
    conn.execute("INSERT INTO cards (id,set_id,local_id,local_key,name,name_key) "
                 "VALUES ('A1-1','A1','1','1','Charizard ex','charizard ex')")
    conn.execute("INSERT INTO card_prices (card_id,variant,market) VALUES ('A1-1','normal',1.0)")
    conn.execute("INSERT INTO watchlist (card_id) VALUES ('A1-1')")
    conn.commit()


def test_purge_removes_sets_cards_prices_and_watchlist_entries(matcher):
    c = matcher.conn
    seed_pocket(c)
    assert purge_digital(c) == {"sets": 1, "cards": 1, "watchlist": 1}
    assert c.execute("SELECT count(*) FROM sets WHERE id='A1'").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM cards WHERE id='A1-1'").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM card_prices WHERE card_id='A1-1'").fetchone()[0] == 0
    assert wl.list_watchlist(c) == []
    # nothing physical was touched
    assert c.execute("SELECT count(*) FROM sets").fetchone()[0] == 3
    assert c.execute("SELECT count(*) FROM cards").fetchone()[0] == 4
    assert [x["id"] for x in wl.search_cards(c, "charizard")] == ["base1-4", "dup1-4"]
    assert "A1" not in [s["id"] for s in catalog.list_sets(c).sets]


def test_purge_is_a_no_op_without_digital_sets(matcher):
    assert purge_digital(matcher.conn) == {"sets": 0, "cards": 0, "watchlist": 0}


def fake_tcgdex(requested):
    sets = {
        "A1": {"id": "A1", "name": "Genetic Apex", "serie": {"id": "tcgp", "name": "Pokémon TCG Pocket"},
               "cardCount": {"official": 286, "total": 286}, "cards": [{"id": "A1-001"}]},
        "base1": {"id": "base1", "name": "Base Set", "serie": {"id": "base", "name": "Base"},
                  "cardCount": {"official": 102, "total": 102}, "cards": [{"id": "base1-4"}]},
    }
    cards = {"base1-4": {"id": "base1-4", "localId": "4", "name": "Charizard", "category": "Pokemon",
                         "pricing": {"tcgplayer": {"unit": "USD", "updated": "x",
                                                   "holofoil": {"marketPrice": 400.0}}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v2/en")
        requested.append(path)
        if path == "/sets":
            return httpx.Response(200, json=[{"id": k} for k in sets])
        if path.startswith("/sets/"):
            return httpx.Response(200, json=sets[path.split("/")[-1]])
        if path.startswith("/cards/"):
            return httpx.Response(200, json=cards[path.split("/")[-1]])
        return httpx.Response(404)
    return handler


def make_client(requested):
    return httpx.AsyncClient(base_url="https://tcgdex.test/v2/en",
                             transport=httpx.MockTransport(fake_tcgdex(requested)))


def test_sync_never_fetches_or_stores_digital_sets(tmp_path):
    requested: list[str] = []
    settings = replace(Settings(), db_path=tmp_path / "sync.db")
    stats = asyncio.run(sync(settings, progress=lambda m: None, client=make_client(requested), price_feed=False))

    assert stats["skipped_digital"] == 1 and stats["sets"] == 1 and stats["cards"] == 1
    assert "/cards/A1-001" not in requested                  # not even requested
    conn = connect(settings.db_path)
    assert [r[0] for r in conn.execute("SELECT id FROM sets")] == ["base1"]
    assert [r[0] for r in conn.execute("SELECT id FROM cards")] == ["base1-4"]


def test_sync_purges_digital_sets_stored_by_an_older_sync(tmp_path):
    settings = replace(Settings(), db_path=tmp_path / "old.db")
    conn = connect(settings.db_path)
    seed_pocket(conn)
    conn.close()
    messages: list[str] = []
    asyncio.run(sync(settings, progress=messages.append, client=make_client([]), price_feed=False))
    conn = connect(settings.db_path)
    assert conn.execute("SELECT count(*) FROM sets WHERE serie_id='tcgp'").fetchone()[0] == 0
    assert any("removed 1 digital-only sets" in m for m in messages)
