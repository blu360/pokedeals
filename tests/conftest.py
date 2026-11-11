"""Shared fixtures: a tiny hand-built card DB so tests never need the network or a real sync."""
import pytest

from pokedeals.db import connect
from pokedeals.matcher import Matcher
from pokedeals.textnorm import local_key, name_key

SETS = [  # id, name, official_count
    ("base1", "Base Set", 102),
    ("swsh3", "Darkness Ablaze", 189),
    ("dup1", "Some Other Set", 102),   # also 102 cards, to exercise the tie-break
]
CARDS = [  # id, set_id, local_id, name
    ("base1-4", "base1", "4", "Charizard"),
    ("base1-58", "base1", "58", "Pikachu"),
    ("dup1-4", "dup1", "4", "Charizard"),
    ("swsh3-136", "swsh3", "136", "Furret"),
]
PRICES = [  # card_id, variant, market
    ("base1-4", "holofoil", 400.0), ("base1-4", "1st-edition-holofoil", 5000.0),
    ("dup1-4", "holofoil", 30.0),
    ("base1-58", "unlimited", 10.0), ("base1-58", "1st-edition", 60.0),
    ("swsh3-136", "normal", 0.30), ("swsh3-136", "reverse-holofoil", 0.50),
]


@pytest.fixture(autouse=True)
def no_ebay_credentials(monkeypatch, tmp_path):
    """Tests must never pick up the developer's real keyset.txt or EBAY_* variables (that would make real network calls)."""
    monkeypatch.setenv("POKEDEALS_KEYSET", str(tmp_path / "no-keyset.txt"))
    # no background feed thread and no real calls to tcgplayer.com from tests (they inject fakes when needed)
    monkeypatch.setenv("POKEDEALS_FEED", "0")
    monkeypatch.setenv("POKEDEALS_NO_KEYRING", "1")                          # never the developer's real saved keys
    monkeypatch.setenv("POKEDEALS_CREDENTIALS", str(tmp_path / "no-credentials.json"))
    from pokedeals import credentials
    credentials.stored.cache_clear()
    monkeypatch.setenv("POKEDEALS_TCG_CONDITIONS", "0")
    for var in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_ENV"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "t.db"
    conn = connect(path)
    for sid, name, n in SETS:
        conn.execute("INSERT INTO sets (id,name,name_key,official_count) VALUES (?,?,?,?)",
                     (sid, name, name_key(name), n))
    for cid, sid, lid, name in CARDS:
        conn.execute("INSERT INTO cards (id,set_id,local_id,local_key,name,name_key) VALUES (?,?,?,?,?,?)",
                     (cid, sid, lid, local_key(lid), name, name_key(name)))
    for cid, variant, market in PRICES:
        conn.execute("INSERT INTO card_prices (card_id,variant,market) VALUES (?,?,?)", (cid, variant, market))
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def matcher(db_path):
    conn = connect(db_path)
    yield Matcher(conn)
    conn.close()
