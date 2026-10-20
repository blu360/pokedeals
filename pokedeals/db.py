from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    name_key       TEXT NOT NULL,
    serie_id       TEXT,
    serie_name     TEXT,
    official_count INTEGER,          -- the "/189" printed on the card
    total_count    INTEGER,
    release_date   TEXT
);

CREATE TABLE IF NOT EXISTS cards (
    id             TEXT PRIMARY KEY, -- e.g. swsh3-136
    set_id         TEXT NOT NULL REFERENCES sets(id),
    local_id       TEXT NOT NULL,    -- as printed: "136", "TG01"
    local_key      TEXT NOT NULL,    -- normalised for matching
    name           TEXT NOT NULL,
    name_key       TEXT NOT NULL,
    category       TEXT,
    rarity         TEXT,
    image          TEXT,
    prices_updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_local ON cards(local_key);
CREATE INDEX IF NOT EXISTS idx_cards_set   ON cards(set_id);

-- TCGplayer prices, one row per printing variant (normal / holofoil / reverse-holofoil / ...).
-- `source` says where a row came from: 'tcgdex' (its TCGplayer snapshot) or 'tcgcsv' (a daily mirror of
-- TCGplayer's own price feed, which overrides tcgdex per variant where a card could be matched).
CREATE TABLE IF NOT EXISTS card_prices (
    card_id    TEXT NOT NULL REFERENCES cards(id),
    variant    TEXT NOT NULL,
    market     REAL,
    low        REAL,
    mid        REAL,
    high       REAL,
    direct_low REAL,
    currency   TEXT,
    updated    TEXT,
    source     TEXT NOT NULL DEFAULT 'tcgdex',
    product_id INTEGER,              -- TCGplayer product id, when known
    PRIMARY KEY (card_id, variant)
);

-- User data (not a cache): cards the user wants deals for. Sync never touches this table,
-- but deleting the DB file to "start fresh" would lose it.
CREATE TABLE IF NOT EXISTS watchlist (
    card_id  TEXT PRIMARY KEY REFERENCES cards(id),
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Live eBay listings the feed has seen (see listing_store.py). A cache, not user data: safe to delete.
CREATE TABLE IF NOT EXISTS listings (
    id          TEXT PRIMARY KEY,                  -- eBay item id
    data        TEXT NOT NULL,                     -- the Listing as JSON
    state       TEXT NOT NULL DEFAULT 'active',    -- active | ended (sold or gone) | dropped (pushed out)
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    verified_at TEXT                               -- last eBay getItem check (condition, still for sale?)
);
CREATE INDEX IF NOT EXISTS listings_state ON listings(state, first_seen);

-- Listings we already sent a notification about (so a restart never repeats one).
CREATE TABLE IF NOT EXISTS notified (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL
);

-- User preferences entered in the UI (e.g. the buyer ZIP). Also user data: sync never touches it.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    """Store a preference; None (or empty) removes it."""
    if value:
        conn.execute("INSERT INTO app_settings(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    else:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    conn.commit()


# Series that exist only in a video game ("Pokémon TCG Pocket"): no physical card, so nothing to sell on
# eBay and no market price. They are never synced, and purged if an older sync stored them.
DIGITAL_SERIES = ("tcgp",)


def purge_digital(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete every set/card/price (and watchlist entry) belonging to a digital-only series."""
    marks = ",".join("?" * len(DIGITAL_SERIES))
    set_ids = [r[0] for r in conn.execute(f"SELECT id FROM sets WHERE serie_id IN ({marks})", DIGITAL_SERIES)]
    if not set_ids:
        return {"sets": 0, "cards": 0, "watchlist": 0}
    q = ",".join("?" * len(set_ids))
    cards = f"SELECT id FROM cards WHERE set_id IN ({q})"
    watched = conn.execute(f"DELETE FROM watchlist WHERE card_id IN ({cards})", set_ids).rowcount
    conn.execute(f"DELETE FROM card_prices WHERE card_id IN ({cards})", set_ids)
    n_cards = conn.execute(f"DELETE FROM cards WHERE set_id IN ({q})", set_ids).rowcount
    n_sets = conn.execute(f"DELETE FROM sets WHERE id IN ({q})", set_ids).rowcount
    conn.commit()
    return {"sets": n_sets, "cards": n_cards, "watchlist": watched}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database created by an older version up to the current schema (additive only)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(card_prices)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE card_prices ADD COLUMN source TEXT NOT NULL DEFAULT 'tcgdex'")
    if "product_id" not in cols:
        conn.execute("ALTER TABLE card_prices ADD COLUMN product_id INTEGER")
    conn.commit()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
