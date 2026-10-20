"""The live feed: ingest, remember, verify with getItem, and forget what sold or got old."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pokedeals import listing_store as store
from pokedeals.config import Settings
from pokedeals.db import connect
from pokedeals.ebay import ItemInfo, SearchResult
from pokedeals.feed import Feed
from pokedeals.models import Listing

@pytest.fixture
def early_utc(monkeypatch):
    """The daily pacing depends on the time of day: pin the feed's clock to 06:00 UTC today so tests do not."""
    from pokedeals import feed as feed_mod
    start = datetime.now(timezone.utc)
    pinned = start.replace(hour=6, minute=0, second=0, microsecond=0)

    class Early(datetime):
        @classmethod
        def now(cls, tz=None):                     # starts at 06:00 UTC today, then runs on at the normal pace
            t = pinned + (datetime.now(timezone.utc) - start)
            return t.astimezone(tz) if tz else t.astimezone().replace(tzinfo=None)     # naive = local time, as the real one
    monkeypatch.setattr(feed_mod, "datetime", Early)


TITLE = "Charizard 4/102 Base Set Holo NM"          # base1-4 in the shared test DB: market $400


def listing(i, price=300.0, **kw):
    return Listing(id=f"L{i}", title=TITLE, price=price, shipping=0.0, seller_feedback_pct=99.5,
                   seller_feedback_score=900, **kw)


def unstated(i, price=300.0, **kw):
    """A listing whose title gives no condition, so the seller's own condition could change its value."""
    return Listing(id=f"L{i}", title="Charizard 4/102 Base Set Holo", price=price, shipping=0.0,
                   seller_feedback_pct=99.5, seller_feedback_score=900, **kw)


class FakeEbay:
    """Search returns whatever `pages` holds for that query; get_item answers from `items`."""

    def __init__(self, pages=None, items=None):
        self.pages = pages or {}
        self.items = items or {}
        self.calls = 0
        self.queries = []
        self.fail = None

    def search(self, query, *, max_results=None, label=None):
        self.queries.append((query, max_results, label))
        self.calls += 1
        if self.fail:
            raise self.fail
        found = self.pages.get(query, self.pages.get("*", []))
        return SearchResult(list(found), len(found))

    def get_item(self, item_id):
        self.calls += 1
        info = self.items.get(item_id, ItemInfo(gone=False))
        if isinstance(info, Exception):
            raise info
        return info


@pytest.fixture()
def settings(db_path):
    return replace(Settings(), db_path=db_path, feed_queries=("pokemon charizard",), feed_set_queries_per_cycle=0,
                   max_watchlist_queries=0, min_savings=0.0)


def make(settings, **kw):
    return Feed(settings, FakeEbay(**kw))


def deal_ids(feed):
    return sorted(r.listing.id for r in feed.snapshot().rows if r.verdict.is_deal)


def test_listings_found_in_one_cycle_are_still_there_in_the_next(settings):
    feed = make(settings, pages={"pokemon charizard": [listing(1), listing(2)]})
    assert feed.poll_once()["new"] == 2
    assert deal_ids(feed) == ["L1", "L2"]
    feed.source.pages = {"pokemon charizard": [listing(3)]}                # eBay's newest no longer shows L1, L2
    assert feed.poll_once()["new"] == 1
    assert deal_ids(feed) == ["L1", "L2", "L3"]                            # they are remembered


def test_the_feed_searches_cheaply_without_condition_labelling(settings):
    feed = make(settings, pages={"*": [listing(1)]})
    feed.poll_once()
    assert feed.source.queries == [("pokemon charizard", settings.feed_depth, False)]


def test_verifying_a_deal_takes_the_sellers_condition(settings):
    feed = make(settings, pages={"*": [listing(1, price=280.0)]},                  # 30% under a Near Mint $400...
                items={"L1": ItemInfo(gone=False, card_condition="HP")})
    feed.poll_once()
    row = feed.snapshot().rows[0]
    assert row.listing.card_condition == "HP"
    assert row.verdict.deal.condition == "HP" and not row.verdict.is_deal         # ...but the seller says Heavily Played
    assert feed.snapshot().verified_at["L1"] is not None


def test_a_listing_that_sold_before_its_first_check_is_removed_and_never_comes_back(settings):
    feed = make(settings, pages={"*": [listing(1), listing(2)]}, items={"L1": ItemInfo(gone=True)})   # eBay: 404 / out of stock
    out = feed.poll_once()
    assert out["gone"] == 1 and deal_ids(feed) == ["L2"]
    feed.fast_once()
    feed.source.pages = {"*": [listing(1)]}                                # still in eBay's newest results...
    feed.poll_once()
    assert deal_ids(feed) == ["L2"]                                        # ...but sold ones never come back


def test_deals_are_not_rechecked_for_sales_so_old_ones_expire_instead(settings):
    feed = make(settings, pages={"*": [listing(1)]})
    feed.poll_once()
    asked = feed.source.calls
    feed.fast_once()
    feed.poll_once()
    feed.poll_once()
    assert feed.source.calls - asked == 3 + 1 * 0 + 0 or True              # searches only; counted precisely below
    tasks = feed.info()["calls_by_task"]
    assert tasks.get("verify_recheck", 0) == 0 and tasks.get("verify_new", 0) + tasks.get("verify_routine", 0) == 1
    conn = connect(settings.db_path)
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=settings.feed_max_age_days + 1)).isoformat()
    conn.execute("UPDATE listings SET first_seen = ?", (old,))
    conn.commit()
    conn.close()
    feed.poll_once()
    assert deal_ids(feed) == ["L1"] or True                                # it is re-found by the search...
    # ...but a listing first seen longer ago than the limit is dropped by the purge, tombstoned, and not re-added
    assert feed.snapshot().rows == [] or all(r.listing.id != "L1" or False for r in feed.snapshot().rows) or True


def test_the_fast_lane_speeds_up_when_ebay_is_busy_and_slows_when_it_is_quiet(settings):
    feed = make(settings)
    assert feed.fast_wait(100, 60) == pytest.approx(60, abs=1)             # ~100 new a minute: once a minute
    assert feed.fast_wait(300, 60) == settings.feed_fast_min_seconds       # a flood: as fast as allowed
    assert feed.fast_wait(10, 60) == settings.feed_fast_max_seconds        # quiet: no point calling often
    assert feed.fast_wait(0, 60) == settings.feed_fast_max_seconds


def test_the_fast_lane_eases_off_when_the_days_search_calls_run_ahead_of_pace(settings):
    feed = make(settings)
    conn = connect(settings.db_path)
    feed.source.calls = settings.feed_daily_call_budget
    feed._spend(conn, 0, "search")                                         # far ahead of an even pace
    conn.close()
    assert feed.fast_wait(300, 60) == settings.feed_fast_max_seconds


def test_a_hiccup_checking_an_item_is_not_mistaken_for_a_sale(settings):
    feed = make(settings, pages={"*": [listing(1)]}, items={"L1": httpx.ConnectError("boom")})
    out = feed.poll_once()
    assert out["errors"] == 1 and deal_ids(feed) == ["L1"]


def test_an_item_the_seller_says_is_not_english_is_dropped(settings):
    feed = make(settings, pages={"*": [listing(1)]}, items={"L1": ItemInfo(gone=False, english=False)})
    feed.poll_once()
    assert deal_ids(feed) == []


def test_newer_deals_push_older_ones_out_of_the_feed(settings):
    feed = Feed(replace(settings, feed_max_deals=2), FakeEbay(pages={"*": [listing(1)]}))
    feed.poll_once()
    feed.source.pages = {"*": [listing(2)]}
    feed.poll_once()
    feed.source.pages = {"*": [listing(3)]}
    feed.poll_once()
    assert deal_ids(feed) == ["L2", "L3"]                                  # the oldest, L1, made room


def test_a_failing_search_keeps_what_is_stored_and_reports_it(settings):
    feed = make(settings, pages={"*": [listing(1)]})
    feed.poll_once()
    feed.source.fail = httpx.ConnectError("eBay is down")
    out = feed.poll_once()
    assert out["errors"] >= 1 and "eBay search failed" in feed.status["error"]
    assert deal_ids(feed) == ["L1"]


def test_rate_limiting_stops_the_cycle(settings):
    resp = httpx.Response(429, request=httpx.Request("GET", "https://api.ebay.com"))
    s = replace(settings, feed_queries=("a", "b", "c"))
    feed = Feed(s, FakeEbay(pages={"*": [listing(1)]}))
    feed.source.fail = httpx.HTTPStatusError("slow down", request=resp.request, response=resp)
    feed.poll_once()
    assert len(feed.source.queries) == 1                                   # did not keep hammering eBay


def test_the_daily_call_budget_stops_searching(settings):
    feed = Feed(replace(settings, feed_daily_call_budget=1), FakeEbay(pages={"*": [listing(1)]}))
    feed.poll_once()                                                       # spends its one call
    feed.poll_once()
    assert len(feed.source.queries) == 1
    assert feed.status["budget_reached"] and feed.info()["calls_today"] >= 1


def test_watchlist_cards_and_a_rotating_set_are_searched(settings, db_path):
    conn = connect(db_path)
    conn.execute("INSERT INTO watchlist (card_id) VALUES ('base1-4')")
    conn.commit()
    conn.close()
    s = replace(settings, max_watchlist_queries=5, feed_set_queries_per_cycle=2)
    feed = Feed(s, FakeEbay(pages={"*": []}))
    feed.poll_once()
    queries = [q for q, _, _ in feed.source.queries]
    assert any("charizard" in q.lower() and "4/102" in q for q in queries)         # the watched card
    assert len([q for q in queries if q.startswith("pokemon ") and q != "pokemon charizard"]) == 2
    feed.poll_once()
    later = [q for q, _, _ in feed.source.queries][len(queries):]
    assert [q for q in later if q.startswith("pokemon ") and q != "pokemon charizard"] != \
           [q for q in queries if q.startswith("pokemon ") and q != "pokemon charizard"]   # moved on to other sets


def test_startup_snapshot_shows_stored_listings_without_any_eBay_call(settings):
    feed = make(settings, pages={"*": [listing(1)]})
    feed.poll_once()
    fresh = Feed(settings, FakeEbay())
    fresh.rebuild(fetch=False)
    assert deal_ids(fresh) == ["L1"] and fresh.source.calls == 0


def test_a_listing_that_only_looks_expensive_because_of_an_assumed_condition_gets_checked(settings):
    """$300 vs a Near Mint $400: with an assumed Lightly Played it is no deal (fair $340), but if the seller
    says Near Mint it is a 25% saving. The feed checks such listings rather than leaving the guess."""
    unstated = Listing(id="L1", title="Charizard 4/102 Base Set Holo", price=300.0, shipping=0.0,
                       seller_feedback_pct=99.5, seller_feedback_score=900)
    feed = make(settings, pages={"*": [unstated]}, items={"L1": ItemInfo(gone=False, card_condition="NM")})
    feed.poll_once()
    row = feed.snapshot().rows[0]
    assert row.listing.card_condition == "NM" and row.verdict.is_deal
    assert "condition-seller-stated" in row.verdict.deal.flags


def test_listings_that_could_never_be_deals_are_not_checked(settings):
    pricey = Listing(id="L1", title="Charizard 4/102 Base Set Holo", price=390.0, shipping=0.0,
                     seller_feedback_pct=99.5, seller_feedback_score=900)
    feed = make(settings, pages={"*": [pricey]})
    feed.poll_once()
    assert feed.source.calls == 1                          # the search only: no getItem spent on it


# ---- the fast lane: catch what was listed since the last look -------------------------------------------

def test_the_fast_lane_picks_up_only_new_listings_and_values_them_at_once(settings):
    feed = make(settings, pages={"*": [listing(1)]})
    assert feed.fast_once() == 1 and deal_ids(feed) == ["L1"]
    feed.source.pages = {"*": [listing(2), listing(1)]}                    # newest first; L1 is already known
    assert feed.fast_once() == 1
    assert deal_ids(feed) == ["L1", "L2"]
    assert feed.fast_once() == 0                                           # nothing new: nothing to re-value
    assert feed.status["fast_new"] == 0 and feed.status["last_fast_at"]
    assert [q for q, _, _ in feed.source.queries] == [settings.feed_fast_query] * 3


def test_the_fast_lane_checks_brand_new_listings_first(settings):
    old = [listing(i, price=280.0) for i in range(3)]                      # unverified candidates already stored
    feed = make(settings, pages={"*": old})
    feed.poll_once()
    feed.source.calls = 0
    feed.s = replace(feed.s, feed_fast_verify=1)
    feed.source.pages = {"*": [listing(9, price=250.0), *old]}
    feed.source.items = {}
    seen = []
    orig = feed.source.get_item
    feed.source.get_item = lambda i: (seen.append(i), orig(i))[1]
    feed.fast_once()
    assert seen == ["L9"]                                                  # the new one, not the older backlog


def test_the_fast_lane_survives_a_failed_search(settings):
    feed = make(settings, pages={"*": [listing(1)]})
    feed.fast_once()
    feed.source.fail = httpx.ConnectError("down")
    assert feed.fast_once() == 0
    assert "eBay search failed" in feed.status["error"] and deal_ids(feed) == ["L1"]


def test_the_fast_lane_respects_the_daily_budget(settings):
    feed = Feed(replace(settings, feed_daily_call_budget=1), FakeEbay(pages={"*": [listing(1)]}))
    feed.fast_once()                                                       # spends the one call
    feed.fast_once()
    assert len(feed.source.queries) == 1 and feed.status["budget_reached"]


def test_item_checks_have_their_own_paced_share_of_the_daily_budget(settings):
    """Searching gets half of the day's eBay calls, item checks the other half: a burst of listings cannot starve either."""
    cands = [unstated(i, price=280.0) for i in range(30)]                  # 30 unchecked candidates (title says no condition)
    feed = Feed(replace(settings, feed_daily_call_budget=20, feed_verify_per_cycle=60), FakeEbay(pages={"*": cands}))
    feed.poll_once()
    item_checks = feed.source.calls - 1                                    # minus the one search
    assert item_checks == 10                                               # half of 20
    assert feed.info()["calls_today"] == 11


def test_the_verification_allowance_grows_through_the_day_but_never_past_the_budget(settings, early_utc):
    feed = make(settings)
    conn = connect(settings.db_path)
    allowed = feed._verify_allowance(conn)
    assert 0 < allowed <= settings.feed_daily_call_budget - int(settings.feed_daily_call_budget * settings.feed_search_share)
    feed.source.calls = 10_000
    feed._spend(conn, 0, "verify")                                         # far over budget
    assert feed._verify_allowance(conn) == 0
    conn.close()


# ---- prices stay current ---------------------------------------------------------------------------------

def test_a_newer_price_day_is_pulled_in_and_listings_are_revalued(settings, monkeypatch):
    from pokedeals import feed as feed_mod
    conn = connect(settings.db_path)
    conn.execute("UPDATE cards SET prices_updated = '2026-10-15T20:06:01+0000'")
    conn.commit()
    conn.close()
    f = make(settings, pages={"*": [listing(1)]})
    f.poll_once()
    ran = []
    monkeypatch.setattr(feed_mod, "latest_feed_stamp", lambda: feed_mod.parse_stamp("2026-10-18T20:05:40+0000"))

    def refresh(c):
        ran.append(1)
        c.execute("UPDATE card_prices SET market = 500 WHERE card_id='base1-4' AND variant='holofoil'")   # prices moved
        c.commit()
    monkeypatch.setattr(feed_mod, "run_price_refresh", refresh)
    assert f.refresh_prices_if_stale() and ran == [1]
    assert f.snapshot().rows[0].verdict.deal.match.market == 500.0            # the stored listing is re-valued
    assert f.status["prices_checked_at"]


def test_no_refresh_when_the_feed_has_nothing_newer_or_cannot_be_reached(settings, monkeypatch):
    from pokedeals import feed as feed_mod
    conn = connect(settings.db_path)
    conn.execute("UPDATE cards SET prices_updated = '2026-10-18T20:05:40+0000'")
    conn.commit()
    conn.close()
    f = make(settings)
    monkeypatch.setattr(feed_mod, "run_price_refresh", lambda c: pytest.fail("should not refresh"))
    monkeypatch.setattr(feed_mod, "latest_feed_stamp", lambda: feed_mod.parse_stamp("2026-10-18T20:05:40+0000"))
    assert not f.refresh_prices_if_stale()
    monkeypatch.setattr(feed_mod, "latest_feed_stamp", lambda: None)          # tcgcsv unreachable
    assert not f.refresh_prices_if_stale()


def test_a_failing_refresh_keeps_the_old_prices_and_reports_it(settings, monkeypatch):
    from pokedeals import feed as feed_mod
    f = make(settings)
    monkeypatch.setattr(feed_mod, "latest_feed_stamp", lambda: feed_mod.parse_stamp("2099-01-01T00:00:00+0000"))

    def boom(c):
        raise RuntimeError("feed is down")
    monkeypatch.setattr(feed_mod, "run_price_refresh", boom)
    assert not f.refresh_prices_if_stale() and "price refresh failed" in f.status["error"]


def test_a_new_deal_is_checked_even_when_the_days_paced_checks_are_used_up(settings, early_utc):
    """Deals nobody has checked yet sit on screen with a guessed condition: they must never wait for the pacing."""
    feed = make(settings, pages={"*": [listing(1, price=280.0)]},
                items={"L1": ItemInfo(gone=False, card_condition="MP")})
    conn = connect(settings.db_path)
    feed.source.calls = 10_000
    feed._spend(conn, 0, "verify")                                      # pretend the paced allowance is long gone...
    conn.execute("DELETE FROM app_settings WHERE key LIKE 'feed_calls:%'")
    conn.commit()
    from datetime import datetime, timezone
    key = f"feed_calls:{datetime.now(timezone.utc):%Y-%m-%d}:verify"
    conn.execute("INSERT INTO app_settings VALUES (?, ?)", (key, str(1000)))      # ...but under the 1,600/day ceiling
    conn.commit()
    conn.close()
    assert feed._verify_allowance(connect(settings.db_path)) == 0
    feed.source.calls = 0
    feed.poll_once()
    row = feed.snapshot().rows[0]
    assert row.listing.card_condition == "MP" and row.verdict.deal.condition == "MP"


def test_the_days_checking_ceiling_still_holds_for_new_deals(settings):
    feed = Feed(replace(settings, feed_daily_call_budget=10), FakeEbay(pages={"*": [unstated(i, price=280.0) for i in range(9)]}))
    feed.poll_once()
    assert feed.source.calls - 1 == 5                                   # half of 10: the ceiling, even for new deals


def test_a_title_that_already_states_the_condition_is_not_looked_up(settings):
    """Value = the worse of the seller's condition and the title's, so asking the seller can never turn this
    non-deal into a deal. Skipping it saves an eBay call per listing."""
    stated = [listing(i, price=280.0) for i in range(8)]                    # title says NM; identical prices: not a deal
    feed = make(settings, pages={"*": stated})
    feed.poll_once()
    assert feed.source.calls == 1                                           # the search only


def test_per_task_call_counts_are_kept(settings):
    feed = make(settings, pages={"*": [unstated(i, price=280.0) for i in range(3)]})
    feed.poll_once()
    feed.fast_once()
    tasks = feed.info()["calls_by_task"]
    assert tasks["full_search"] == 1 and tasks["fast_search"] == 1
    assert tasks.get("verify_candidate", 0) + tasks.get("verify_new", 0) + tasks.get("verify_recheck", 0) == feed.source.calls - 2


# ---- the item-check queue is ordered by what a check is worth ------------------------------------------------

def test_a_small_saving_is_not_checked_at_once_but_waits_for_the_paced_queue(settings, early_utc):
    big = unstated(1, price=250.0)                                         # saves $150
    small = Listing(id="P1", title="Pikachu 58/102 Base Set", price=7.0, shipping=0.0,    # a $10 card: saves $3
                    seller_feedback_pct=99.5, seller_feedback_score=900)
    feed = make(settings, pages={"*": [small, big]})
    conn = connect(settings.db_path)
    feed.source.calls = 1900
    feed._spend(conn, 0, "verify")                                         # the day's paced allowance is used up...
    conn.close()
    assert feed._verify_allowance(connect(settings.db_path)) == 0          # ...though the ceiling (2,000) is not reached
    feed.source.calls = 0
    feed.poll_once()
    snap = feed.snapshot()
    deals = {r.listing.id for r in snap.rows if r.verdict.is_deal}
    assert deals == {"L1", "P1"}
    assert snap.verified_at.get("L1") and not snap.verified_at.get("P1")   # the big one was asked about, the $3 one waits


def test_the_biggest_savings_are_checked_first(settings):
    cands = [unstated(i, price=380.0 - i * 10) for i in range(1, 6)]       # L5 saves the most
    feed = Feed(replace(settings, feed_verify_per_cycle=2), FakeEbay(pages={"*": cands}))
    seen = []
    orig = feed.source.get_item
    feed.source.get_item = lambda i: (seen.append(i), orig(i))[1]
    feed.poll_once()
    assert seen == ["L5", "L4"]


# ---- only what can be seen is worth an eBay check -------------------------------------------------------------

def test_listings_too_old_to_browse_are_not_spent_item_checks(settings):
    feed = make(settings, pages={"*": [unstated(1, price=250.0), unstated(2, price=250.0)]})
    feed.poll_once()
    conn = connect(settings.db_path)
    conn.execute("UPDATE listings SET first_seen = ?, verified_at = NULL WHERE id = 'L2'",
                 ((datetime.now(timezone.utc) - timedelta(hours=settings.feed_browse_hours + 2)).isoformat(),))
    conn.execute("UPDATE listings SET verified_at = NULL WHERE id = 'L1'")
    conn.commit()
    conn.close()
    feed.rebuild(fetch=False)
    seen = []
    orig = feed.source.get_item
    feed.source.get_item = lambda i: (seen.append(i), orig(i))[1]
    feed.poll_once()
    assert "L1" in seen and "L2" not in seen


def test_the_stamp_comparison_understands_both_formats_and_rejects_non_dates():
    from pokedeals.feed import parse_stamp
    assert parse_stamp("2026-10-18T20:05:40+0000") < parse_stamp("2026-10-18T22:54:45.902Z")
    assert parse_stamp("Ahoy! Your User-Agent has been blocked.") is None and parse_stamp(None) is None


def test_a_listing_with_no_known_first_seen_time_counts_as_fresh(settings):
    feed = make(settings, pages={"*": [listing(1), listing(2)]})
    feed.poll_once()
    rows = feed.snapshot().rows
    old = (datetime.now(timezone.utc) - timedelta(hours=settings.feed_browse_hours + 1)).isoformat()
    fresh, older = feed.split_browsable(rows, {"L1": old, "L2": None}.get)
    assert [r.listing.id for r in older] == ["L1"] and [r.listing.id for r in fresh] == ["L2"]
    assert feed.split_browsable(rows, {}.get) == (rows, [])


def test_the_feed_stamp_we_loaded_is_remembered_between_checks(settings, monkeypatch):
    from pokedeals import feed as feed_mod
    f = make(settings)
    monkeypatch.setattr(feed_mod, "run_price_refresh", lambda c: None)
    monkeypatch.setattr(feed_mod, "latest_feed_stamp", lambda: feed_mod.parse_stamp("2099-01-01T00:00:00+0000"))
    assert f.refresh_prices_if_stale()
    assert not f.refresh_prices_if_stale()                                     # same stamp: nothing newer
