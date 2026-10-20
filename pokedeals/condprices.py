"""What a card is worth *in each condition*, from TCGplayer's own sales data.

The price feed (tcgcsv) has one market price per print and nothing per condition, so Lightly Played used to be
guessed as 85% of market. Real cards are not that tidy: Leafeon (Call of Legends) is $51.66 Near Mint but
$30.81 Lightly Played, and a Base Set Charizard is $972 NM, $529 LP, $247 HP.

tcgplayer.com's product page shows exactly this: for each condition, a **Market Price** built from recent sales,
plus the number sold in the last three months. This module reads the same data (one request per product returns
every print and condition) and takes the newest market price for the condition we need. The number of sales is
kept as a confidence measure: a condition with no sales has no market price and falls back to the estimate.

For the card dialog it also reads the live listings (cheapest ask and how many are for sale) for context.

These are the endpoints behind tcgplayer.com's own pages, not a documented public API. They can change or start
refusing requests; every failure here degrades to the multiplier estimate, never to an error.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .db import connect

HISTORY_URL = "https://infinite-api.tcgplayer.com/price/history/{product_id}/detailed?range=quarter"
LISTINGS_URL = "https://mp-search-api.tcgplayer.com/v1/product/{product_id}/listings"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Origin": "https://www.tcgplayer.com", "Referer": "https://www.tcgplayer.com/",
    "Content-Type": "application/json",
}

CONDITION_NAMES = {"NM": "Near Mint", "LP": "Lightly Played", "MP": "Moderately Played",
                   "HP": "Heavily Played", "DMG": "Damaged"}
CONDITION_CODES = {v: k for k, v in CONDITION_NAMES.items()}
CONDITION_ORDER = ["NM", "LP", "MP", "HP", "DMG"]


# our variant keys -> the print names TCGplayer uses (the inverse of tcgcsv.SUBTYPE_VARIANT)
_PRINTINGS = {
    "normal": ["Normal"], "holofoil": ["Holofoil"], "reverse-holofoil": ["Reverse Holofoil"],
    "1st-edition": ["1st Edition"], "1st-edition-holofoil": ["1st Edition Holofoil"],
    "unlimited": ["Unlimited"], "unlimited-holofoil": ["Unlimited Holofoil"],
    # Base Set Shadowless is filed under the Shadowless product's "Unlimited" print
    "shadowless-holofoil": ["Unlimited Holofoil", "Holofoil"], "shadowless-normal": ["Unlimited", "Normal"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS condition_market (
    product_id INTEGER NOT NULL,
    printing   TEXT NOT NULL,            -- TCGplayer's print name; '' marks "fetched, nothing there"
    condition  TEXT NOT NULL,            -- NM / LP / MP / HP / DMG
    market     REAL,                     -- newest sales-based market price
    sold       INTEGER NOT NULL,         -- sales in the last 3 months
    as_of      TEXT,                     -- date of that price
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (product_id, printing, condition)
);
CREATE TABLE IF NOT EXISTS condition_asks (
    product_id INTEGER NOT NULL,
    printing   TEXT NOT NULL,
    condition  TEXT NOT NULL,
    lowest     REAL,
    listings   INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (product_id, printing, condition)
);
"""


@dataclass(frozen=True)
class CondPrice:
    condition: str
    market: float | None        # TCGplayer's market price for this condition (from recent sales)
    sold: int                   # sales in the last 3 months behind it
    as_of: str | None = None
    lowest: float | None = None     # cheapest live listing (dialog only)
    listings: int = 0               # live listings in this condition (dialog only)

    @property
    def usable(self) -> bool:
        """TCGplayer has a market price for this condition. Even one sale is a real number, far better than a guess
        (a Heavily Played Deoxys reverse holo is $49.99 on its last sale; the 50%-of-Near-Mint rule said $115). How
        far to trust it is a separate question, answered by `sold` and settings.thin_market_sales."""
        return self.market is not None and self.market > 0


Key = tuple[int, str, str]      # (product id, our variant key, condition code)


def condition_value(cp: CondPrice | None) -> float | None:
    """What a copy in this condition is worth: TCGplayer's market price for it, or None when it has none."""
    return round(cp.market, 2) if cp is not None and cp.usable else None


def parse_history(payload: dict) -> list[tuple[str, str, float | None, int, str | None]]:
    """(printing, condition code, newest market price, sales in the period, price date) per English SKU."""
    out = []
    for e in payload.get("result") or []:
        code = CONDITION_CODES.get(e.get("condition"))
        if code is None or e.get("language") != "English":
            continue
        buckets = [b for b in e.get("buckets") or [] if b.get("marketPrice") not in (None, "", "0")]
        newest = max(buckets, key=lambda b: b.get("bucketStartDate", ""), default=None)
        out.append((e.get("variant") or "", code,
                    float(newest["marketPrice"]) if newest else None,
                    int(float(e.get("totalQuantitySold") or 0)),
                    newest.get("bucketStartDate") if newest else None))
    return out


class TcgplayerConditions:
    """Condition prices with a database cache, so a product is fetched about once per `ttl_hours`."""

    def __init__(self, db_path: Path, client: httpx.Client | None = None, ttl_hours: float = 12,
                 delay: float = 0.1, workers: int = 6):
        self.db_path = db_path
        self._client = client or httpx.Client(timeout=15, headers=HEADERS)
        self.ttl = timedelta(hours=ttl_hours)
        self.delay = delay
        self.workers = workers
        self._lock = threading.Lock()
        self._failures = 0                  # consecutive request failures
        self._paused_until = 0.0            # circuit breaker: stop hammering a site that refuses us
        conn = connect(db_path)
        conn.executescript(SCHEMA)
        conn.close()

    # ---- availability ---------------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return time.time() >= self._paused_until

    def _failed(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= 4:
                self._paused_until = time.time() + 600
                self._failures = 0

    def _ok(self) -> None:
        with self._lock:
            self._failures = 0

    def _fresh_after(self) -> str:
        return (datetime.now(timezone.utc) - self.ttl).isoformat()

    # ---- sales-based market prices --------------------------------------------------------------
    def _fetch_history(self, product_id: int) -> tuple[int, list | None]:
        time.sleep(self.delay)
        try:
            r = self._client.get(HISTORY_URL.format(product_id=product_id))
            r.raise_for_status()
            rows = parse_history(r.json())
            self._ok()
            return product_id, rows
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            self._failed()
            return product_id, None

    def _has_history(self, conn: sqlite3.Connection, product_id: int) -> bool:
        return conn.execute("SELECT 1 FROM condition_market WHERE product_id = ? AND fetched_at >= ? LIMIT 1",
                            (product_id, self._fresh_after())).fetchone() is not None

    def prefetch(self, keys: list[Key], limit: int | None = None) -> int:
        """Fetch (concurrently) the history of every product in `keys` that is not cached or is stale.
        One request covers all of a product's prints and conditions. Returns how many products were fetched."""
        if not self.available:
            return 0
        conn = connect(self.db_path)
        try:
            products = [p for p in dict.fromkeys(k[0] for k in keys) if not self._has_history(conn, p)]
            if limit is not None:
                products = products[:limit]
            if not products:
                return 0
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = list(pool.map(self._fetch_history, products))
            stamp, done = datetime.now(timezone.utc).isoformat(), 0
            for pid, rows in results:
                if rows is None:
                    continue
                conn.execute("DELETE FROM condition_market WHERE product_id = ?", (pid,))
                # an empty marker row, so a product with no sales data is not re-fetched on every scan
                conn.execute("INSERT OR REPLACE INTO condition_market VALUES (?,?,?,?,?,?,?)",
                             (pid, "", "", None, 0, None, stamp))
                conn.executemany("INSERT OR REPLACE INTO condition_market VALUES (?,?,?,?,?,?,?)",
                                 [(pid, printing, code, market, sold, as_of, stamp)
                                  for printing, code, market, sold, as_of in rows])
                done += 1
            conn.commit()
            return done
        finally:
            conn.close()

    def has_history(self, conn: sqlite3.Connection, product_id: int) -> bool:
        """True when we have (recently) read this product's sales history, even if it held nothing for our print."""
        return self._has_history(conn, product_id)

    def get(self, conn: sqlite3.Connection, product_id: int, variant: str, cond: str) -> CondPrice | None:
        """The cached market price for this print and condition (see `prefetch`); None when unknown."""
        for printing in _PRINTINGS.get(variant, []):
            r = conn.execute(
                "SELECT market, sold, as_of FROM condition_market WHERE product_id=? AND printing=? AND condition=? "
                "AND fetched_at >= ?", (product_id, printing, cond, self._fresh_after())).fetchone()
            if r is not None:
                return CondPrice(cond, r["market"], r["sold"], r["as_of"])
        return None

    # ---- live listings (for the card dialog) ----------------------------------------------------
    def _query_asks(self, product_id: int, cond: str, printing: str) -> tuple[int, float | None]:
        term: dict = {"sellerStatus": "Live", "channelId": 0, "language": ["English"],
                      "condition": [CONDITION_NAMES[cond]], "printing": [printing]}
        body = {"filters": {"term": term, "range": {"quantity": {"gte": 1}}, "exclude": {"channelExclusion": 0}},
                "from": 0, "size": 1, "sort": {"field": "price", "order": "asc"},
                "context": {"shippingCountry": "US", "cart": {}}, "aggregations": []}
        r = self._client.post(LISTINGS_URL.format(product_id=product_id), json=body)
        r.raise_for_status()
        res = r.json()["results"][0]
        first = (res.get("results") or [{}])[0].get("price")
        return int(res.get("totalResults", 0)), float(first) if first else None

    def _fetch_asks(self, product_id: int, variant: str, cond: str) -> tuple[str, int, float | None] | None:
        time.sleep(self.delay)
        try:
            for printing in _PRINTINGS.get(variant, []):
                total, lowest = self._query_asks(product_id, cond, printing)
                if total:
                    self._ok()
                    return printing, total, lowest
            self._ok()
            return (_PRINTINGS.get(variant) or [""])[0], 0, None
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            self._failed()
            return None

    def all_conditions(self, conn: sqlite3.Connection, product_id: int, variant: str) -> dict[str, CondPrice]:
        """Every condition of one print: market price and sales, plus the live cheapest listing (fetched now,
        a handful of requests). Used by the card dialog."""
        self.prefetch([(product_id, variant, c) for c in CONDITION_ORDER])
        asks: dict[str, tuple[int, float | None]] = {}
        if self.available:
            stamp = datetime.now(timezone.utc).isoformat()
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                got = list(pool.map(lambda c: (c, self._fetch_asks(product_id, variant, c)), CONDITION_ORDER))
            for c, res in got:
                if res is not None:
                    asks[c] = (res[1], res[2])
                    conn.execute("INSERT OR REPLACE INTO condition_asks VALUES (?,?,?,?,?,?)",
                                 (product_id, res[0], c, res[2], res[1], stamp))
            conn.commit()
        out = {}
        for c in CONDITION_ORDER:
            cp = self.get(conn, product_id, variant, c)
            n, lowest = asks.get(c, (0, None))
            if cp is None and not n:
                continue
            base = cp or CondPrice(c, None, 0)
            out[c] = CondPrice(c, base.market, base.sold, base.as_of, lowest, n)
        return out
