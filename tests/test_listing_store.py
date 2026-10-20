from datetime import datetime, timedelta, timezone

import pytest

from pokedeals import listing_store as store
from pokedeals.db import connect
from pokedeals.models import Listing


def L(i, price=100.0, **kw):
    return Listing(id=f"L{i}", title=f"Charizard 4/102 #{i}", price=price, shipping=1.0, **kw)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "s.db")
    yield c
    c.close()


def age(conn, listing_id, hours):
    t = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn.execute("UPDATE listings SET first_seen = ?, last_seen = ? WHERE id = ?", (t, t, listing_id))
    conn.commit()


def purge(conn, deals=(), **kw):
    args = dict(deal_ids=set(deals), nondeal_hours=48, max_age_days=30, max_deals=300)
    return store.purge(conn, **{**args, **kw})


def test_listings_round_trip_through_the_store(conn):
    l = L(1, buying_options=("AUCTION",), end_time=datetime(2030, 1, 1, tzinfo=timezone.utc),
          card_condition="LP", item_country="HK", seller_name="bob")
    assert store.upsert(conn, [l]) == 1
    assert store.load_active(conn)[0].listing == l


def test_upsert_counts_only_new_listings_and_keeps_the_seller_condition(conn):
    assert store.upsert(conn, [L(1), L(2)]) == 2
    store.mark_verified(conn, "L1", card_condition="MP", price=90.0)
    assert store.upsert(conn, [L(1, price=95.0), L(3)]) == 1               # L1 already known
    got = {s.listing.id: s.listing for s in store.load_active(conn)}
    assert got["L1"].card_condition == "MP" and got["L1"].price == 95.0    # a search result has no condition: kept


def test_ended_listings_never_come_back(conn):
    store.upsert(conn, [L(1)])
    store.set_state(conn, ["L1"], "ended")
    assert store.upsert(conn, [L(1)]) == 0                                 # still in the newest results: ignored
    assert store.load_active(conn) == []


def test_nondeals_are_forgotten_after_a_couple_of_days_but_deals_stay(conn):
    store.upsert(conn, [L(1), L(2), L(3)])
    age(conn, "L1", 60)
    age(conn, "L2", 60)
    out = purge(conn, deals={"L2"})
    assert out["dropped"] == 1
    assert {s.listing.id for s in store.load_active(conn)} == {"L2", "L3"}


def test_nothing_survives_the_max_age(conn):
    store.upsert(conn, [L(1)])
    age(conn, "L1", 24 * 31)
    purge(conn, deals={"L1"})
    assert store.load_active(conn) == []


def test_newer_deals_push_the_oldest_out(conn):
    store.upsert(conn, [L(i) for i in range(5)])
    for i in range(5):
        age(conn, f"L{i}", 10 - i)                                          # L0 oldest ... L4 newest
    purge(conn, deals={f"L{i}" for i in range(5)}, max_deals=3)
    assert {s.listing.id for s in store.load_active(conn)} == {"L2", "L3", "L4"}


def test_an_auction_past_its_end_time_has_ended(conn):
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    store.upsert(conn, [L(1, buying_options=("AUCTION",), end_time=past), L(2)])
    assert purge(conn, deals={"L1", "L2"})["ended"] == 1
    assert [s.listing.id for s in store.load_active(conn)] == ["L2"]


def test_old_tombstones_are_deleted(conn):
    store.upsert(conn, [L(1)])
    store.set_state(conn, ["L1"], "ended")
    age(conn, "L1", 24 * 15)
    assert purge(conn)["deleted"] == 1
    assert store.counts(conn) == {}
