"""The listings the live feed has seen, kept in SQLite so deals build up over time.

A listing is in one of three states:
  active   believed to be for sale
  ended    sold, ended or removed: never shown again
  dropped  pushed out by newer deals, or too old: never shown again

Ended/dropped rows stay as tombstones for a while so an item still in eBay's "newest" results is not
re-added. Every function takes an open connection and commits itself.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import Listing


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Stored:
    listing: Listing
    first_seen: str
    verified_at: str | None


def upsert(conn: sqlite3.Connection, listings: list[Listing]) -> int:
    """Add new listings and refresh known ones. Returns how many were new.

    A refreshed listing keeps what a getItem check taught us (the seller's condition), since search results
    lack it. Ended and dropped rows are left alone.
    """
    stamp, new = now(), 0
    for l in listings:
        row = conn.execute("SELECT data, state FROM listings WHERE id = ?", (l.id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO listings (id, data, first_seen, last_seen) VALUES (?,?,?,?)",
                         (l.id, json.dumps(l.to_json()), stamp, stamp))
            new += 1
        elif row["state"] == "active":
            old = Listing.from_json(json.loads(row["data"]))
            keep = l if l.card_condition else Listing.from_json({**l.to_json(), "card_condition": old.card_condition})
            conn.execute("UPDATE listings SET data = ?, last_seen = ? WHERE id = ?",
                         (json.dumps(keep.to_json()), stamp, l.id))
        else:
            conn.execute("UPDATE listings SET last_seen = ? WHERE id = ?", (stamp, l.id))
    conn.commit()
    return new


def known_ids(conn: sqlite3.Connection) -> set[str]:
    """Every listing id we have ever stored and not yet deleted (active, ended or dropped)."""
    return {r[0] for r in conn.execute("SELECT id FROM listings")}


def load_active(conn: sqlite3.Connection) -> list[Stored]:
    rows = conn.execute(
        "SELECT data, first_seen, verified_at FROM listings WHERE state = 'active' ORDER BY first_seen DESC").fetchall()
    return [Stored(Listing.from_json(json.loads(r["data"])), r["first_seen"], r["verified_at"]) for r in rows]


def mark_verified(conn: sqlite3.Connection, listing_id: str, card_condition: str | None = None,
                  price: float | None = None) -> None:
    """Record a successful getItem check, folding in the condition (and any price change) it showed."""
    row = conn.execute("SELECT data FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        return
    data = json.loads(row["data"])
    if card_condition:
        data["card_condition"] = card_condition
    if price is not None:
        data["price"] = price
    conn.execute("UPDATE listings SET data = ?, verified_at = ? WHERE id = ?",
                 (json.dumps(data), now(), listing_id))
    conn.commit()


def set_state(conn: sqlite3.Connection, ids: list[str], state: str) -> None:
    conn.executemany("UPDATE listings SET state = ? WHERE id = ?", [(state, i) for i in ids])
    conn.commit()


def purge(conn: sqlite3.Connection, *, deal_ids: set[str], nondeal_hours: int, max_age_days: int,
          max_deals: int) -> dict[str, int]:
    """Forget what is no longer worth keeping. Returns counts.

    - an auction past its end time has ended
    - a non-deal is forgotten `nondeal_hours` after it was first seen
    - nothing is kept past `max_age_days`
    - beyond `max_deals` deals, the OLDEST are dropped
    - tombstones are deleted after a fortnight
    """
    stamp = datetime.now(timezone.utc)
    out = {"ended": 0, "dropped": 0, "deleted": 0}

    ended = [s.listing.id for s in load_active(conn)
             if s.listing.is_auction_only and s.listing.end_time and s.listing.end_time < stamp]
    set_state(conn, ended, "ended")
    out["ended"] = len(ended)

    cutoff_nondeal = (stamp - timedelta(hours=nondeal_hours)).isoformat()
    cutoff_age = (stamp - timedelta(days=max_age_days)).isoformat()
    drop = [r["id"] for r in conn.execute("SELECT id, first_seen FROM listings WHERE state = 'active'")
            if r["first_seen"] < cutoff_age or (r["id"] not in deal_ids and r["first_seen"] < cutoff_nondeal)]

    deals = [r["id"] for r in conn.execute(
        "SELECT id FROM listings WHERE state = 'active' ORDER BY first_seen DESC") if r["id"] in deal_ids
        and r["id"] not in drop]
    drop += deals[max_deals:]                       # newest first, so what is past the cap is the oldest
    set_state(conn, drop, "dropped")
    out["dropped"] = len(drop)

    out["deleted"] = conn.execute(
        "DELETE FROM listings WHERE state != 'active' AND last_seen < ?",
        ((stamp - timedelta(days=14)).isoformat(),)).rowcount
    conn.commit()
    return out


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {state: n for state, n in conn.execute("SELECT state, count(*) FROM listings GROUP BY state")}
