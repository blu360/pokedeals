"""The live feed: keep searching eBay in the background and remember what it finds.

Each cycle:
  1. search eBay (newest first) with a set of broad queries, your watchlist cards and a few set names,
     and store every listing in the database (`listing_store`);
  2. value everything stored, so deals build up across cycles instead of vanishing on the next refresh;
  3. check the listings that look like deals with eBay's getItem: it gives the seller's stated condition
     and tells us whether the listing is still for sale (sold ones are removed);
  4. forget what is no longer worth keeping: ended auctions, old non-deals, and the oldest deals once there
     are more than `feed_max_deals` (newer deals push older ones out).

The web UI reads the latest snapshot, which is instant, rather than searching eBay while you wait.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from . import listing_store as store
from . import watchlist as wl
from .condprices import TcgplayerConditions
from .config import Settings
from .db import connect, get_setting, set_setting
from .matcher import Matcher
from .pipeline import ScanRow, scan_listings
from .tcgcsv import BASE_URL, USER_AGENT, refresh_prices

log = logging.getLogger("pokedeals.feed")


def parse_stamp(text: str | None) -> datetime | None:
    """A price timestamp ('2026-10-18T20:05:40+0000' or '...45.902Z') as an aware datetime, or None if it is not one."""
    try:
        t = datetime.fromisoformat((text or "").strip())
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def latest_feed_stamp() -> datetime | None:
    """When the TCGplayer price feed (tcgcsv.com) last published, or None if it cannot be reached."""
    try:
        r = httpx.get(f"{BASE_URL}/last-updated.txt", timeout=20, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return parse_stamp(r.text)                            # a "blocked" message is a 200 too: not a date, not news
    except httpx.HTTPError:
        return None


def record_calls(conn, task: str, n: int) -> None:
    """Add `n` eBay requests to today's tally for `task` (fast_search, full_search, verify_new, popup, ...)."""
    if n <= 0:
        return
    key = f"calls:{datetime.now(timezone.utc):%Y-%m-%d}:{task}"
    set_setting(conn, key, str(int(get_setting(conn, key) or 0) + n))


def calls_today(conn) -> dict[str, int]:
    """Today's eBay requests by task (everything the app spent, feed and manual)."""
    prefix = f"calls:{datetime.now(timezone.utc):%Y-%m-%d}:"
    return {k[len(prefix):]: int(v) for k, v in conn.execute(
        "SELECT key, value FROM app_settings WHERE key LIKE ?", (prefix + "%",))}


def run_price_refresh(conn) -> None:
    asyncio.run(refresh_prices(conn, progress=lambda *_: None))


@dataclass
class Snapshot:
    rows: list[ScanRow] = field(default_factory=list)
    first_seen: dict[str, str] = field(default_factory=dict)
    verified_at: dict[str, str | None] = field(default_factory=dict)
    built_at: str | None = None


class Feed:
    def __init__(self, settings: Settings, source: Any, conditions: TcgplayerConditions | None = None):
        self.s = settings
        self.source = source
        self.conditions = conditions
        self._snap = Snapshot()
        self._pending: dict[str, tuple[str, str | None]] = {}     # id -> (first_seen, verified_at) of the last load
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status: dict[str, Any] = {
            "running": False, "cycling": False, "cycles": 0, "last_cycle_at": None, "last_cycle_seconds": None,
            "last_new": 0, "calls_today": 0, "budget": settings.feed_daily_call_budget,
            "budget_reached": False, "error": None, "next_cycle_at": None,
            "fast_interval": settings.feed_fast_interval_seconds, "last_fast_at": None, "fast_new": 0,
            "prices_checked_at": None,
        }

    # ---- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="pokedeals-feed", daemon=True)
        self.status["running"] = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.status["running"] = False

    def poll_now(self) -> None:
        """Ask for a cycle right away (the Refresh button)."""
        self._wake.set()

    def _loop(self) -> None:
        try:
            self.rebuild(fetch=False)               # show what is already stored before touching the network
        except Exception as e:                      # noqa: BLE001 - the loop must survive anything
            log.exception("initial snapshot failed")
            self.status["error"] = str(e)
        fast = self.s.feed_fast_interval_seconds
        next_full, next_fast = 0.0, float("inf") if fast <= 0 else 0.0
        last_tick = time.time()
        next_prices = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                if now >= next_prices:
                    next_prices = time.time() + self.s.feed_price_check_minutes * 60
                    self.refresh_prices_if_stale()
                if self._wake.is_set() or now >= next_full:
                    self._wake.clear()
                    self.poll_once()                # the full cycle: broad queries, watchlist, sets, pruning
                    next_full = time.time() + self.s.feed_interval_seconds
                    next_fast = time.time() + fast if fast > 0 else float("inf")
                    last_tick = time.time()
                    self.status["next_cycle_at"] = (datetime.now(timezone.utc) +
                                                    timedelta(seconds=self.s.feed_interval_seconds)).isoformat()
                elif now >= next_fast:
                    self.fast_once()                # the fast lane: just what was listed since last time
                    now2 = time.time()
                    wait = self.fast_wait(self.status["fast_new"], now2 - last_tick)
                    self.status["fast_interval"] = round(wait)
                    last_tick, next_fast = now2, now2 + wait
            except Exception as e:                  # noqa: BLE001
                log.exception("feed cycle failed")
                self.status["error"] = f"{type(e).__name__}: {e}"
                next_fast = time.time() + max(fast, 30)
            self._wake.wait(timeout=max(0.5, min(next_fast, next_full) - time.time()))

    def fast_wait(self, new_found: int, elapsed: float) -> float:
        """Seconds until the next fast-lane check.

        Aim for about `feed_fast_target` new listings per call, since one call reads up to 200: when eBay is busy
        (lots of new listings per minute) the lane speeds up, so a deal is seen sooner at no extra cost per listing;
        when it is quiet the lane slows down rather than spend calls on near-empty pages. If today's search calls are
        running ahead of an even pace, it slows to the maximum so the budget lasts the whole day."""
        s = self.s
        rate = new_found / max(elapsed / 60, 0.25)                 # new listings per minute, lately
        wait = s.feed_fast_target / max(rate, 1) * 60
        conn = connect(s.db_path)
        try:
            now = datetime.now(timezone.utc)
            share = (now.hour * 3600 + now.minute * 60 + now.second) / 86400
            if self._used(conn, "search") > self._search_budget() * share + 150:
                wait = s.feed_fast_max_seconds                     # ahead of pace: ease off
        finally:
            conn.close()
        return min(max(wait, s.feed_fast_min_seconds), s.feed_fast_max_seconds)

    def refresh_prices_if_stale(self) -> bool:
        """Pull a newer TCGplayer price day if the feed has published one since ours. True when prices changed.

        The feed (tcgcsv.com) publishes once a day, so this is one tiny request an hour and a few seconds of
        work on the day it has news. Without it the prices only moved when someone ran `sync` or `prices`."""
        conn = connect(self.s.db_path)
        try:
            self.status["prices_checked_at"] = datetime.now(timezone.utc).isoformat()
            # compare against the feed stamp we last loaded; the cards' own stamps come in several formats
            stored = parse_stamp(get_setting(conn, "prices_feed_stamp"))
            if stored is None:                      # first run: work it out from the cards once, then remember it
                stored = max(filter(None, (parse_stamp(r[0]) for r in conn.execute(
                    "SELECT DISTINCT prices_updated FROM cards"))), default=None)
                if stored is not None:
                    set_setting(conn, "prices_feed_stamp", stored.isoformat())
            latest = latest_feed_stamp()
            if latest is None or (stored is not None and latest <= stored):
                return False
            run_price_refresh(conn)
            set_setting(conn, "prices_feed_stamp", latest.isoformat())
            self._publish(conn, self._evaluate(conn, fetch=False))        # values change with the prices
            return True
        except Exception as e:                      # noqa: BLE001 - old prices beat a dead feed thread
            log.exception("price refresh failed")
            self.status["error"] = f"price refresh failed: {e}"
            return False
        finally:
            conn.close()

    # ---- reading --------------------------------------------------------------------------------
    def browse_cutoff(self) -> str:
        """Listings first seen before this (an ISO time; '' = no limit) are kept for searching but not browsed. They are
        most likely sold by now, so they are neither shown nor spent eBay checks on."""
        if self.s.feed_browse_hours <= 0:
            return ""
        return (datetime.now(timezone.utc) - timedelta(hours=self.s.feed_browse_hours)).isoformat()

    def split_browsable(self, rows: list[ScanRow], first_seen: Callable[[str], str | None]) -> tuple[list[ScanRow], list[ScanRow]]:
        """(fresh, older): older rows were first seen before the browse cutoff. A row with no known first-seen time
        counts as fresh. The one place that decides what can be seen, for the list and for the item checks alike."""
        cutoff = self.browse_cutoff()
        fresh, older = [], []
        for r in rows:
            t = first_seen(r.listing.id)
            (fresh if not cutoff or not t or t >= cutoff else older).append(r)
        return fresh, older

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snap

    def info(self) -> dict[str, Any]:
        snap = self.snapshot()
        conn = connect(self.s.db_path)
        try:
            by_task = calls_today(conn)
        finally:
            conn.close()
        return {**self.status, "calls_by_task": by_task,
                "tracked": len(snap.rows),
                "deals": sum(r.verdict.is_deal for r in snap.rows),
                "built_at": snap.built_at}

    # ---- one cycle ------------------------------------------------------------------------------
    def _queries(self, conn) -> list[str]:
        queries = list(wl.watch_queries(conn, self.s.max_watchlist_queries))
        queries += self.s.feed_queries
        n = self.s.feed_set_queries_per_cycle
        if n:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM sets ORDER BY release_date DESC, id")]
            if names:
                cursor = int(get_setting(conn, "feed_set_cursor") or 0) % len(names)
                for i in range(n):
                    queries.append("pokemon " + names[(cursor + i) % len(names)])
                set_setting(conn, "feed_set_cursor", str((cursor + n) % len(names)))
        return list(dict.fromkeys(q for q in queries if q))

    # Two daily tallies, because searching and checking compete for the same eBay allowance: searches may use
    # 60% of the budget (they run on a fixed schedule), the sold/condition checks the other 40%, and the checks
    # are paced across the day so a burst of new listings cannot use them all up in the first hour.
    def _key(self, kind: str) -> str:
        return f"feed_calls:{datetime.now(timezone.utc):%Y-%m-%d}:{kind}"

    def _used(self, conn, kind: str) -> int:
        return int(get_setting(conn, self._key(kind)) or 0)

    def _search_budget(self) -> int:
        return max(1, int(self.s.feed_daily_call_budget * self.s.feed_search_share))

    def _spend(self, conn, before: int, kind: str, task: str | None = None) -> None:
        """Add the eBay requests made since `before` to today's tally for `kind` ("search" or "verify")."""
        made = max(getattr(self.source, "calls", before) - before, 0)
        if task:
            record_calls(conn, task, made)
        set_setting(conn, self._key(kind), str(self._used(conn, kind) + made))
        self.status["calls_today"] = self._used(conn, "search") + self._used(conn, "verify")

    def _verify_allowance(self, conn) -> int:
        """How many item checks may be made right now (paced: an even share of the day's budget, plus a burst)."""
        budget = self.s.feed_daily_call_budget - self._search_budget()
        now = datetime.now(timezone.utc)
        share = (now.hour * 3600 + now.minute * 60 + now.second) / 86400
        return max(0, min(budget, int(budget * share) + 150) - self._used(conn, "verify"))

    def poll_once(self) -> dict[str, Any]:
        """Search, store, value, verify, prune. Safe to call from tests (no thread needed)."""
        started = time.time()
        self.status["cycling"] = True
        conn = connect(self.s.db_path)
        summary: dict[str, Any] = {"new": 0, "verified": 0, "gone": 0, "errors": 0}
        try:
            self.status["calls_today"] = self._used(conn, "search") + self._used(conn, "verify")
            self.status["budget_reached"] = self._used(conn, "search") >= self._search_budget()
            if not self.status["budget_reached"]:
                self._ingest(conn, summary)
            rows = self._evaluate(conn, fetch=True)

            if hasattr(self.source, "get_item"):
                if self._verify(conn, rows, summary, limit=self.s.feed_verify_per_cycle):
                    rows = self._evaluate(conn, fetch=False)

            deal_ids = {r.listing.id for r in rows if r.verdict.is_deal}
            pruned = store.purge(conn, deal_ids=deal_ids, nondeal_hours=self.s.feed_browse_hours or self.s.feed_nondeal_hours,
                                 max_age_days=self.s.feed_max_age_days, max_deals=self.s.feed_max_deals)
            if any(pruned[k] for k in ("ended", "dropped")):
                rows = self._evaluate(conn, fetch=False)
            summary["pruned"] = pruned
            self._publish(conn, rows)
            self.status.update(last_new=summary["new"], cycles=self.status["cycles"] + 1,
                               last_cycle_at=datetime.now(timezone.utc).isoformat(),
                               last_cycle_seconds=round(time.time() - started, 1))
            return summary
        finally:
            self.status["cycling"] = False
            conn.close()

    def fast_once(self) -> int:
        """Catch what was listed since the last look: newest-first, stopping at the first known listing.

        Usually one eBay call. New listings are valued and the likeliest deals checked straight away, so a
        fresh deal shows up within about a minute of being listed. Returns how many listings were new."""
        conn = connect(self.s.db_path)
        try:
            self.status["calls_today"] = self._used(conn, "search") + self._used(conn, "verify")
            if self._used(conn, "search") >= self._search_budget():
                self.status["budget_reached"] = True
                return 0
            before = getattr(self.source, "calls", 0)
            known = store.known_ids(conn)
            try:
                if hasattr(self.source, "search_newest"):
                    result = self.source.search_newest(self.s.feed_fast_query, known.__contains__,
                                                       self.s.feed_fast_max_pages)
                else:
                    result = self.source.search(self.s.feed_fast_query, max_results=self.s.feed_depth)
            except httpx.HTTPError as e:
                self.status["error"] = f"eBay search failed: {e}"
                self._spend(conn, before, "search", "fast_search")
                return 0
            fresh = {l.id for l in result.listings if l.id not in known}
            store.upsert(conn, result.listings)
            self._spend(conn, before, "search", "fast_search")
            self.status.update(last_fast_at=datetime.now(timezone.utc).isoformat(), fast_new=len(fresh), error=None)
            if fresh:
                rows = self._evaluate(conn, fetch=True)
                if hasattr(self.source, "get_item") and self._verify(
                        conn, rows, {"errors": 0, "verified": 0, "gone": 0},
                        limit=self.s.feed_fast_verify, first=fresh):
                    rows = self._evaluate(conn, fetch=False)
                self._publish(conn, rows)
            return len(fresh)
        finally:
            conn.close()

    def _ingest(self, conn, summary: dict) -> None:
        before = getattr(self.source, "calls", 0)
        kwargs = {"label": False} if hasattr(self.source, "get_item") else {}
        for query in self._queries(conn):
            spent = self._used(conn, "search") + max(getattr(self.source, "calls", 0) - before, 0)
            if spent >= self._search_budget():
                self.status["budget_reached"] = True
                break
            try:
                result = self.source.search(query, max_results=self.s.feed_depth, **kwargs)
            except httpx.HTTPError as e:
                summary["errors"] += 1
                self.status["error"] = f"eBay search failed: {e}"
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403, 429):
                    break                            # auth trouble or rate limit: stop spending calls this cycle
                continue
            summary["new"] += store.upsert(conn, result.listings)
        if not summary["errors"]:
            self.status["error"] = None
        self._spend(conn, before, "search", "full_search")

    def _evaluate(self, conn, *, fetch: bool) -> list[ScanRow]:
        stored = store.load_active(conn)
        self._pending = {s.listing.id: (s.first_seen, s.verified_at) for s in stored}
        return scan_listings([s.listing for s in stored], Matcher(conn), self.s,
                             conditions=self.conditions, fetch=fetch)

    def rebuild(self, *, fetch: bool = False) -> None:
        conn = connect(self.s.db_path)
        try:
            self._publish(conn, self._evaluate(conn, fetch=fetch))
        finally:
            conn.close()

    def _publish(self, conn, rows: list[ScanRow]) -> None:
        pending = self._pending
        snap = Snapshot(rows=rows,
                        first_seen={k: v[0] for k, v in pending.items()},
                        verified_at={k: v[1] for k, v in pending.items()},
                        built_at=datetime.now(timezone.utc).isoformat())
        with self._lock:
            self._snap = snap

    def _verify(self, conn, rows: list[ScanRow], summary: dict, limit: int | None = None,
                first: set[str] | None = None) -> bool:
        """getItem-check listings, most important first. True when anything changed.

        1. Deals nobody has checked yet. They are on screen with a guessed condition until we ask, so they are
           never held back by the daily pacing (only by the day's overall checking budget).
        2. Smaller new deals, then 3. listings that could become deals if the seller's condition is better than
           the one we assumed. These are paced through the day.

        Deals are not re-checked for "has it sold?": that was the biggest recurring cost for the least value, and a
        deal that has sold is only a click away from being found out (opening it checks live). Old deals expire
        instead (`feed_max_age_days`).
        """
        limit = self.s.feed_verify_per_cycle if limit is None else limit
        rows, _ = self.split_browsable(rows, lambda i: self._pending.get(i, (None, None))[0])      # only what can be seen
        def checked(r: ScanRow) -> str | None:
            return self._pending.get(r.listing.id, (None, None))[1]

        def saving(r: ScanRow) -> float:
            d = r.verdict.deal
            return d.fair_value - d.total_cost

        def worth_asking_now(r: ScanRow) -> bool:
            # Asking the seller can only matter when the title leaves the condition open, or when the deal is
            # big enough that a wrong guess is costly (a Steal). And a few dollars of saving is not worth a call.
            return (r.parsed.condition is None or r.verdict.category == "steal") and saving(r) >= self.s.feed_verify_urgent_savings

        unverified = [r for r in rows if r.verdict.is_deal and checked(r) is None]
        urgent = [r for r in unverified if worth_asking_now(r)]
        routine = [r for r in unverified if not worth_asking_now(r)]
        for group in (urgent, routine):
            group.sort(key=lambda r: ((r.listing.id not in first) if first else False, -saving(r)))   # fresh, then biggest
        # listings never checked that could be deals if the seller's condition is better than assumed:
        # the title gives no condition, and the card is at least min_discount under its Near Mint price.
        floor = 1 - self.s.min_discount
        candidates = [r for r in rows if checked(r) is None and not r.verdict.is_deal and not r.verdict.suspicious
                      and r.parsed.condition is None and r.listing.card_condition is None     # only a guess can improve
                      and (d := r.verdict.deal) is not None and d.confidence >= self.s.min_confidence
                      and d.total_cost <= d.match.market * floor]
        candidates.sort(key=lambda r: r.verdict.deal.match.market - r.verdict.deal.total_cost, reverse=True)

        hard_left = max(0, (self.s.feed_daily_call_budget - self._search_budget()) - self._used(conn, "verify"))
        tier: dict[str, str] = {}
        todo = urgent[:min(limit, hard_left)]                        # never held back by the day's pacing
        tier.update({r.listing.id: "verify_new" for r in todo})
        paced = min(self._verify_allowance(conn), limit - len(todo), hard_left - len(todo))
        if paced > 0:
            queue = [(r, "verify_new") for r in urgent[len(todo):]] + [(r, "verify_routine") for r in routine] \
                + [(r, "verify_candidate") for r in candidates]
            for r, kind in queue[:paced]:
                tier[r.listing.id] = kind
                todo.append(r)
        made: dict[str, int] = {}
        changed = False
        before = getattr(self.source, "calls", 0)
        for row in todo:
            made[tier[row.listing.id]] = made.get(tier[row.listing.id], 0) + 1
            try:
                info = self.source.get_item(row.listing.id)
            except httpx.HTTPError as e:
                summary["errors"] += 1
                self.status["error"] = f"eBay item check failed: {e}"
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403, 429):
                    break
                continue
            if info.gone:
                store.set_state(conn, [row.listing.id], "ended")
                summary["gone"] += 1
                changed = True
            elif info.english is False:
                store.set_state(conn, [row.listing.id], "dropped")      # the seller says it is not English
                changed = True
            else:
                if (info.card_condition and info.card_condition != row.listing.card_condition) or                         (info.price is not None and abs(info.price - row.listing.price) > 0.004):
                    changed = True
                store.mark_verified(conn, row.listing.id, info.card_condition, info.price)
                first_seen = self._pending.get(row.listing.id, (store.now(), None))[0]
                self._pending[row.listing.id] = (first_seen, store.now())      # so "still for sale?" updates at once
                summary["verified"] += 1
        self._spend(conn, before, "verify")
        for task, n in made.items():
            record_calls(conn, task, n)
        return changed
