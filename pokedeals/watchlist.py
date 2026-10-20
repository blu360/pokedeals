"""Card search over the local DB, and the user's watchlist.

A watched card is identified for matching purposes by a *signature* (name, number, set size) rather
than its card id: the same print can be listed under several set ids, and an ambiguous eBay title
("Charizard 4/102") may be attributed to a sibling set. The signature treats those as the same card.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .textnorm import local_key, name_key

Signature = tuple[str, str, int | None]  # (name_key, local_key, official_count)

_NUMBER = re.compile(r"(?<![\w/])([A-Za-z]{0,4}\d{1,3})\s*/\s*([A-Za-z]{0,4}\d{1,3})(?![\w/])")
_MAX_TOKENS = 6
MAX_PAGE = 50   # most cards returned per request; callers page with offset

# How a card's prices are presented (one place, so the list, the sort and the set value agree):
#   market_price  the average of its REGULAR prints (anything that is not a reverse holo, a 1st Edition or
#                 Shadowless). For the great majority of cards that is a single print: simply its price.
#   reverse_price the reverse holofoil price, if that print exists (else NULL -> shown as N/A)
#   stamped_price the 1st Edition (stamped) price, if that print exists
#   shadowless_price  Base Set's Shadowless print, if it exists (TCGplayer prices it far above the ordinary one)
#   sort_price    what price sorting uses: market_price, or whichever other price a card has if it has
#                 no regular print (e.g. a promo that only exists as a reverse holo)
# Averaging the special prints in with the regular one would blur them (Legendary Collection Slowpoke is
# $1.65 normal and $1,000 reverse holo; Base Set Charizard is $955 unlimited and $2,258 Shadowless),
# which is why they are separate columns.
REGULAR = ("p.variant != 'reverse-holofoil' AND p.variant NOT LIKE '1st-edition%' "
           "AND p.variant NOT LIKE 'shadowless%'")


def _price_subquery(agg: str, where: str) -> str:
    return f"(SELECT {agg} FROM card_prices p WHERE p.card_id = c.id AND p.market > 0 AND {where})"


MARKET_PRICE_SQL = _price_subquery("ROUND(AVG(p.market), 2)", REGULAR)
REVERSE_PRICE_SQL = _price_subquery("MIN(p.market)", "p.variant = 'reverse-holofoil'")
STAMPED_PRICE_SQL = _price_subquery("MIN(p.market)", "p.variant LIKE '1st-edition%'")
SHADOWLESS_PRICE_SQL = _price_subquery("MIN(p.market)", "p.variant LIKE 'shadowless%'")
SORT_PRICE_SQL = (f"COALESCE({MARKET_PRICE_SQL}, {REVERSE_PRICE_SQL}, {STAMPED_PRICE_SQL}, "
                  f"{SHADOWLESS_PRICE_SQL})")

_SELECT = f"""
SELECT c.id, c.name, c.local_id, c.rarity, c.image, s.name AS set_name, s.official_count,
       {MARKET_PRICE_SQL} AS market_price,
       {REVERSE_PRICE_SQL} AS reverse_price,
       {STAMPED_PRICE_SQL} AS stamped_price,
       {SHADOWLESS_PRICE_SQL} AS shadowless_price,
       {_price_subquery("COUNT(*)", REGULAR)} AS regular_count,
       {SORT_PRICE_SQL} AS sort_price,
       (SELECT min(market) FROM card_prices p WHERE p.card_id = c.id AND p.market > 0) AS market_low,
       (SELECT max(market) FROM card_prices p WHERE p.card_id = c.id AND p.market > 0) AS market_high,
       EXISTS (SELECT 1 FROM watchlist w WHERE w.card_id = c.id) AS watched
"""


def card_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["watched"] = bool(d["watched"])
    d["thumb"] = f"{d['image']}/low.webp" if d.get("image") else None
    d["image_large"] = f"{d['image']}/high.webp" if d.get("image") else None
    return d


@dataclass(frozen=True)
class CardPage:
    cards: list[dict]
    total: int          # every card matching the query, regardless of paging


def search_cards(conn: sqlite3.Connection, text: str, limit: int = 25) -> list[dict]:
    """First page of `search_cards_page`, cards only."""
    return search_cards_page(conn, text, limit).cards


SEARCH_SORTS = ("relevance", "price_desc", "price_asc")

def search_cards_page(
    conn: sqlite3.Connection, text: str, limit: int = 25, offset: int = 0, sort: str = "relevance"
) -> CardPage:
    """Find cards by free text: name and/or set words, plus an optional number like '4/102' or '58'.

    Every word must match the card name or the set name (substring), so "char base" finds Base Set
    Charizard. `total` is the full match count so callers can page through all of it.

    sort: "relevance" = exact-name matches first, then highest price; "price_desc" / "price_asc" order
    by each card's displayed market price (see the notes above `_SELECT`). Unpriced cards come last
    either way, and ordering is applied in SQL so paging stays consistent (sorting a single page
    client-side would be wrong).
    """
    if sort not in SEARCH_SORTS:
        raise ValueError(f"unknown sort {sort!r}; expected one of {SEARCH_SORTS}")
    where: list[str] = []
    params: list = []

    m = _NUMBER.search(text)
    if m:
        numerator, denominator = m.groups()
        where.append("c.local_key = ? AND s.official_count = ?")
        params += [local_key(numerator), int(re.sub(r"\D", "", denominator))]
        text = f"{text[:m.start()]} {text[m.end():]}"

    tokens = name_key(text).split()[:_MAX_TOKENS]
    if not where and not tokens:
        return CardPage([], 0)
    for tok in tokens:
        if tok.isdigit() and len(tok) <= 3:   # "pikachu 58": bare number is a card number or part of a name
            where.append("(c.local_key = ? OR c.name_key LIKE ? OR s.name_key LIKE ?)")
            params += [local_key(tok), f"%{tok}%", f"%{tok}%"]
        else:
            where.append("(c.name_key LIKE ? OR s.name_key LIKE ?)")
            params += [f"%{tok}%", f"%{tok}%"]

    condition = f"FROM cards c JOIN sets s ON s.id = c.set_id WHERE {' AND '.join(where)}"
    total = conn.execute(f"SELECT count(*) {condition}", params).fetchone()[0]

    order_params: list = []
    if sort == "price_desc":
        order = "(sort_price IS NULL), sort_price DESC, c.name, c.id"
    elif sort == "price_asc":
        order = "(sort_price IS NULL), sort_price ASC, c.name, c.id"
    else:
        order = "(c.name_key = ?) DESC, COALESCE(sort_price, -1) DESC, c.name, c.id"
        order_params = [" ".join(tokens)]
    sql = f"{_SELECT} {condition} ORDER BY {order} LIMIT ? OFFSET ?"
    page_params = params + order_params + [max(1, min(limit, MAX_PAGE)), max(0, offset)]
    return CardPage([card_to_dict(r) for r in conn.execute(sql, page_params)], total)


def get_card(conn: sqlite3.Connection, card_id: str) -> dict | None:
    row = conn.execute(
        f"{_SELECT} FROM cards c JOIN sets s ON s.id = c.set_id WHERE c.id = ?", (card_id,)
    ).fetchone()
    return card_to_dict(row) if row else None


def list_watchlist(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        f"{_SELECT}, w.added_at FROM watchlist w JOIN cards c ON c.id = w.card_id "
        "JOIN sets s ON s.id = c.set_id ORDER BY w.added_at DESC, c.name, c.id"
    ).fetchall()
    return [card_to_dict(r) for r in rows]


def add_card(conn: sqlite3.Connection, card_id: str) -> bool:
    """Idempotent. Returns False if the card id does not exist."""
    if conn.execute("SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone() is None:
        return False
    conn.execute("INSERT OR IGNORE INTO watchlist (card_id) VALUES (?)", (card_id,))
    conn.commit()
    return True


def remove_card(conn: sqlite3.Connection, card_id: str) -> bool:
    cur = conn.execute("DELETE FROM watchlist WHERE card_id = ?", (card_id,))
    conn.commit()
    return cur.rowcount > 0


def signature(name: str, local_id: str, official_count: int | None) -> Signature:
    return (name_key(name), local_key(local_id), official_count)


def watch_signatures(conn: sqlite3.Connection) -> set[Signature]:
    return {signature(c["name"], c["local_id"], c["official_count"]) for c in list_watchlist(conn)}


def card_query(card: dict) -> str:
    """The eBay search string for one card, e.g. 'Charizard 4/102' (or name + set when no set size)."""
    if card["official_count"]:
        return f"{card['name']} {card['local_id']}/{card['official_count']}"
    return f"{card['name']} {card['set_name']}"


def watch_queries(conn: sqlite3.Connection, limit: int) -> list[str]:
    """One eBay search string per watched card (de-duplicated, capped)."""
    seen: set[str] = set()
    queries: list[str] = []
    for c in list_watchlist(conn):
        q = card_query(c)
        if q not in seen:
            seen.add(q)
            queries.append(q)
    return queries[:limit]


def card_prices(conn: sqlite3.Connection, card_id: str) -> list[dict]:
    """TCGplayer prices per printing variant for one card, cheapest market first."""
    rows = conn.execute(
        "SELECT variant, market, low, mid, high, source, updated, product_id FROM card_prices WHERE card_id = ? "
        "ORDER BY COALESCE(market, 1e18), variant", (card_id,)
    ).fetchall()
    return [dict(r) for r in rows]
