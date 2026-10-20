"""Browse sets and the cards inside them."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .textnorm import local_key, name_key
from .watchlist import REGULAR, CardPage, _SELECT, card_to_dict

SET_SORTS = ("newest", "oldest", "alpha", "size", "value")
CARD_SORTS = ("number", "price_desc", "price_asc", "alpha")

_SETS_SQL = f"""
WITH card_price AS (          -- each card's displayed price (same rule as watchlist.SORT_PRICE_SQL)
    SELECT c.set_id, p.card_id,
           COALESCE(AVG(CASE WHEN {REGULAR} THEN p.market END),
                    MIN(CASE WHEN p.variant = 'reverse-holofoil' THEN p.market END),
                    MIN(CASE WHEN p.variant LIKE '1st-edition%' THEN p.market END),
                    MIN(CASE WHEN p.variant LIKE 'shadowless%' THEN p.market END)) AS price
    FROM card_prices p JOIN cards c ON c.id = p.card_id
    WHERE p.market > 0 GROUP BY p.card_id),
set_value AS (SELECT set_id, sum(price) AS value, count(*) AS priced FROM card_price GROUP BY set_id),
set_cards AS (SELECT set_id, count(*) AS n FROM cards GROUP BY set_id)
SELECT s.id, s.name, s.serie_id, s.serie_name, s.release_date, s.official_count, s.total_count,
       COALESCE(sc.n, 0) AS cards_in_db, COALESCE(sv.priced, 0) AS priced_cards,
       ROUND(COALESCE(sv.value, 0), 2) AS set_value
FROM sets s
LEFT JOIN set_cards sc ON sc.set_id = s.id
LEFT JOIN set_value sv ON sv.set_id = s.id
"""

_SET_ORDER = {
    "newest": "(s.release_date IS NULL), s.release_date DESC, s.name COLLATE NOCASE",
    "oldest": "(s.release_date IS NULL), s.release_date ASC, s.name COLLATE NOCASE",
    "alpha": "s.name COLLATE NOCASE, s.id",
    "size": "COALESCE(s.total_count, 0) DESC, s.name COLLATE NOCASE",
    "value": "set_value DESC, s.name COLLATE NOCASE",
}


def with_logo(d: dict) -> dict:
    """Add the set's logo URL (TCGdex hosts one for most sets; the page falls back to a badge when it 404s)."""
    d["logo"] = f"https://assets.tcgdex.net/en/{d['serie_id']}/{d['id']}/logo.webp" if d.get("serie_id") and d.get("id") else None
    return d


@dataclass(frozen=True)
class SetPage:
    sets: list[dict]
    total: int


def list_sets(
    conn: sqlite3.Connection, q: str = "", sort: str = "newest", limit: int = 50, offset: int = 0
) -> SetPage:
    """Sets, optionally filtered by words in the set or series name. `value` is the sum of each card's
    displayed market price, i.e. what one copy of every card in the set costs."""
    if sort not in SET_SORTS:
        raise ValueError(f"unknown sort {sort!r}; expected one of {SET_SORTS}")
    where, params = [], []
    for tok in name_key(q).split()[:6]:
        where.append("(s.name_key LIKE ? OR lower(COALESCE(s.serie_name, '')) LIKE ? OR lower(s.id) = ?)")
        params += [f"%{tok}%", f"%{tok}%", tok]
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT count(*) FROM sets s{clause}", params).fetchone()[0]
    rows = conn.execute(
        f"{_SETS_SQL}{clause} ORDER BY {_SET_ORDER[sort]} LIMIT ? OFFSET ?",
        params + [max(1, min(limit, 100)), max(0, offset)],
    ).fetchall()
    return SetPage([with_logo(dict(r)) for r in rows], total)


def get_set(conn: sqlite3.Connection, set_id: str) -> dict | None:
    row = conn.execute(f"{_SETS_SQL} WHERE s.id = ?", (set_id,)).fetchone()
    return with_logo(dict(row)) if row else None


_NUMBER = re.compile(r"^([A-Z]*)(\d+)$")


def _number_key(local_id: str) -> tuple[str, int, str]:
    """Natural card-number order: main set first (1, 2, ... 10), then lettered galleries (TG1, TG2...)."""
    key = local_key(local_id)
    m = _NUMBER.match(key)
    return (m.group(1), int(m.group(2)), key) if m else (key, 0, key)


def set_cards(
    conn: sqlite3.Connection, set_id: str, sort: str = "number", limit: int = 50, offset: int = 0
) -> CardPage:
    """One page of a set's cards. Sets hold at most a few hundred cards, so they are sorted in Python
    (natural card-number order is awkward in SQL) and then sliced."""
    if sort not in CARD_SORTS:
        raise ValueError(f"unknown sort {sort!r}; expected one of {CARD_SORTS}")
    cards = [card_to_dict(r) for r in conn.execute(
        f"{_SELECT} FROM cards c JOIN sets s ON s.id = c.set_id WHERE c.set_id = ?", (set_id,))]

    by_number = lambda c: _number_key(c["local_id"])
    if sort == "number":
        cards.sort(key=by_number)
    elif sort == "alpha":
        cards.sort(key=lambda c: (c["name"].casefold(), by_number(c)))
    else:
        sign = -1 if sort == "price_desc" else 1       # unpriced cards go last in both directions
        cards.sort(key=lambda c: (c["sort_price"] is None,
                                  sign * (c["sort_price"] or 0.0), by_number(c)))
    start = max(0, offset)
    return CardPage(cards[start:start + max(1, min(limit, 100))], len(cards))
