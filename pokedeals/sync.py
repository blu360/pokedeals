"""Sync the local card DB (cards + TCGplayer prices) from the TCGdex REST API.

We call the REST API directly rather than `tcgdex-sdk`: the SDK's Card model has no `pricing` field, so it
would drop the TCGplayer prices we need.

A full cold sync is ~24k single-card requests (~8 min at 20 concurrent). Prices change daily; `--set`
limits it to specific sets.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from typing import Callable, Iterable

import httpx

from .config import SETTINGS, Settings
from .db import DIGITAL_SERIES, connect, purge_digital
from .tcgcsv import refresh_prices
from .textnorm import local_key, name_key

Progress = Callable[[str], None]

_RETRY_STATUS = {429, 500, 502, 503, 504}


async def _get_json(client: httpx.AsyncClient, path: str, attempts: int = 4) -> dict | list | None:
    for attempt in range(attempts):
        try:
            r = await client.get(path)
            if r.status_code == 404:
                return None
            if r.status_code in _RETRY_STATUS:
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(0.5 * 2 ** attempt)
    return None


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _price_rows(card_id: str, pricing: dict | None) -> list[tuple]:
    tcg = (pricing or {}).get("tcgplayer") or {}
    unit, updated = tcg.get("unit"), tcg.get("updated")
    rows = []
    for variant, p in tcg.items():
        if not isinstance(p, dict):  # skips "unit" / "updated"
            continue
        rows.append((
            card_id, variant,
            _num(p.get("marketPrice")), _num(p.get("lowPrice")), _num(p.get("midPrice")),
            _num(p.get("highPrice")), _num(p.get("directLowPrice")), unit, updated,
        ))
    return rows


def _store_set(conn: sqlite3.Connection, s: dict, cards: list[dict]) -> None:
    count = s.get("cardCount") or {}
    serie = s.get("serie") or {}
    conn.execute(
        """INSERT INTO sets (id, name, name_key, serie_id, serie_name, official_count, total_count, release_date)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, name_key=excluded.name_key,
             serie_id=excluded.serie_id, serie_name=excluded.serie_name,
             official_count=excluded.official_count, total_count=excluded.total_count,
             release_date=excluded.release_date""",
        (s["id"], s["name"], name_key(s["name"]), serie.get("id"), serie.get("name"),
         count.get("official"), count.get("total"), s.get("releaseDate")),
    )
    for c in cards:
        pricing = c.get("pricing")
        updated = ((pricing or {}).get("tcgplayer") or {}).get("updated")
        conn.execute(
            """INSERT INTO cards (id, set_id, local_id, local_key, name, name_key, category, rarity, image, prices_updated)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET set_id=excluded.set_id, local_id=excluded.local_id,
                 local_key=excluded.local_key, name=excluded.name, name_key=excluded.name_key,
                 category=excluded.category, rarity=excluded.rarity, image=excluded.image,
                 prices_updated=excluded.prices_updated""",
            (c["id"], s["id"], str(c["localId"]), local_key(str(c["localId"])), c["name"],
             name_key(c["name"]), c.get("category"), c.get("rarity"), c.get("image"), updated),
        )
        conn.execute("DELETE FROM card_prices WHERE card_id = ?", (c["id"],))
        conn.executemany(
            "INSERT INTO card_prices (card_id, variant, market, low, mid, high, direct_low, currency, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?)", _price_rows(c["id"], pricing)
        )
    conn.commit()


async def sync(
    settings: Settings = SETTINGS,
    set_ids: Iterable[str] | None = None,
    progress: Progress = print,
    client: httpx.AsyncClient | None = None,
    price_feed: bool = True,
    feed_client: httpx.AsyncClient | None = None,
) -> dict[str, int]:
    conn = connect(settings.db_path)
    wanted = set(set_ids) if set_ids else None
    sem = asyncio.Semaphore(settings.sync_concurrency)
    stats = {"sets": 0, "cards": 0, "priced": 0, "failed": 0, "skipped_digital": 0}
    started = time.time()

    purged = purge_digital(conn)      # clear out digital-only sets an older sync may have stored
    if purged["sets"]:
        progress(f"removed {purged['sets']} digital-only sets ({purged['cards']} cards) from the database")

    limits = httpx.Limits(max_connections=settings.sync_concurrency * 2)
    base = f"{settings.tcgdex_base}/{settings.language}"
    client = client or httpx.AsyncClient(base_url=base, timeout=30, limits=limits)
    async with client:
        listing = await _get_json(client, "/sets") or []
        todo = [s["id"] for s in listing if wanted is None or s["id"] in wanted]
        if wanted and (missing := wanted - set(todo)):
            progress(f"warning: unknown set ids: {', '.join(sorted(missing))}")

        async def fetch_card(card_id: str) -> dict | None:
            async with sem:
                try:
                    return await _get_json(client, f"/cards/{card_id}")
                except httpx.HTTPError:
                    stats["failed"] += 1
                    return None

        for n, set_id in enumerate(todo, 1):
            s = await _get_json(client, f"/sets/{set_id}")
            if not s:
                continue
            if (s.get("serie") or {}).get("id") in DIGITAL_SERIES:
                stats["skipped_digital"] += 1       # decided from the set record, before any card requests
                progress(f"[{n}/{len(todo)}] {set_id:<12} skipped (digital-only, not a physical card)")
                continue
            ids = [c["id"] for c in s.get("cards", [])]
            fetched = await asyncio.gather(*(fetch_card(i) for i in ids))
            cards = [c for c in fetched if c]
            _store_set(conn, s, cards)
            stats["sets"] += 1
            stats["cards"] += len(cards)
            stats["priced"] += sum(1 for c in cards if c.get("pricing"))
            progress(f"[{n}/{len(todo)}] {set_id:<12} {len(cards):>4} cards  ({time.time() - started:.0f}s)")

    # Re-storing a card replaces its prices with TCGdex's, so lay the TCGplayer feed back over them.
    if price_feed:
        try:
            report = await refresh_prices(conn, client=feed_client, progress=progress)
            stats.update(feed_matched=report.matched, feed_filled=report.filled, feed_changed=report.changed)
            progress(f"TCGplayer feed: matched {report.matched} cards, filled {report.filled} unpriced, "
                     f"corrected {report.changed}; {report.unpriced_after} still unpriced")
        except httpx.HTTPError as e:
            progress(f"warning: TCGplayer price feed unavailable ({e}); TCGdex prices kept")

    conn.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    ids = argv if argv is not None else sys.argv[1:]
    stats = asyncio.run(sync(set_ids=ids or None))
    print(stats)
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
