"""The Status page's data, backups of your watchlist/settings, and the update check."""
import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pokedeals import backup, health, updates
from pokedeals.config import Settings
from pokedeals.db import connect, get_setting, set_setting
from pokedeals.feed import Feed, record_calls
from pokedeals.web import create_app


# ---- backups ----------------------------------------------------------------------------------------

def test_a_backup_holds_choices_but_not_tallies_or_caches(db_path):
    conn = connect(db_path)
    conn.execute("INSERT INTO watchlist (card_id) VALUES ('base1-4')")
    set_setting(conn, "buyer_zip", "90210")
    set_setting(conn, "calls:2026-10-20:fast_search", "812")
    set_setting(conn, "update_check", "{}")
    conn.commit()
    conn.close()
    path = backup.make_backup(db_path)
    data = json.loads(gzip.open(path, "rt", encoding="utf-8").read())
    assert data["settings"] == {"buyer_zip": "90210"}
    assert [w["card_id"] for w in data["watchlist"]] == ["base1-4"]
    assert path.stat().st_size < 2000                                   # kilobytes, not a copy of the database


def test_only_the_newest_backups_are_kept(db_path):
    folder = backup.backup_dir(db_path)
    folder.mkdir()
    for day in range(1, 6):
        (folder / f"userdata-2026010{day}-000000.json.gz").write_bytes(b"x")
    backup.make_backup(db_path, keep=3)
    names = [b["name"] for b in backup.list_backups(db_path)]
    assert len(names) == 3 and names[0] > "userdata-2026" + "0105"      # today's, then the two newest old ones


def test_a_backup_is_due_only_once_a_day(db_path):
    assert backup.due(db_path)
    backup.make_backup(db_path)
    assert not backup.due(db_path)
    assert backup.due(db_path, every_hours=0)


def test_restoring_puts_the_watchlist_and_settings_back_but_skips_unknown_cards(db_path, tmp_path):
    path = tmp_path / "userdata-20260101-000000.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"settings": {"buyer_zip": "10001"},
                   "watchlist": [{"card_id": "base1-4", "added_at": "2026-01-01"},
                                 {"card_id": "gone-1", "added_at": "2026-01-01"}]}, f)
    assert backup.restore_backup(db_path, path) == {"watchlist": 1, "settings": 1}
    conn = connect(db_path)
    assert [r[0] for r in conn.execute("SELECT card_id FROM watchlist")] == ["base1-4"]
    assert get_setting(conn, "buyer_zip") == "10001"


# ---- the status report ------------------------------------------------------------------------------

def test_usage_is_summed_against_the_limit_and_warns_near_it(db_path):
    s = Settings(db_path=db_path, ebay_daily_limit=1000)
    conn = connect(db_path)
    record_calls(conn, "fast_search", 500)
    record_calls(conn, "popup", 20)
    api = health.build(s, None, None, conn)["api"]
    assert api["used"] == 520 and api["limit"] == 1000 and api["level"] == "ok"
    assert api["by_task"] == {"fast_search": 500, "popup": 20}
    record_calls(conn, "verify_new", 300)
    assert health.build(s, None, None, conn)["api"]["level"] == "warn"           # 82%
    record_calls(conn, "verify_new", 200)
    assert health.build(s, None, None, conn)["api"]["level"] == "full"
    assert health.build(s, None, None, conn)["api"]["resets_at"].endswith("00:00:00+00:00")


def test_a_paused_tcgplayer_shows_up_as_a_source_problem(db_path):
    class Paused:
        def state(self):
            return {"available": False, "retry_in_seconds": 300}
    report = health.build(Settings(db_path=db_path), None, Paused(), connect(db_path))
    bad = [x for x in report["sources"] if not x["ok"] and x["name"] == "TCGplayer prices by condition"]
    assert len(bad) == 1 and "daily feed price" in bad[0]["detail"]
    assert "No eBay keys" in report["sources"][0]["detail"]               # no feed: say why there are no listings


def test_the_health_endpoint_and_backup_button(db_path):
    client = TestClient(create_app(Settings(db_path=db_path), conditions=None))
    d = client.get("/api/health").json()
    assert d["api"]["used"] == 0 and d["backups"] == [] and d["feed"] is None
    made = client.post("/api/backup").json()
    assert made["made"].startswith("userdata-") and len(made["backups"]) == 1


# ---- chores: the daily backup and update check --------------------------------------------------------

def make_feed(db_path, monkeypatch, result=None):
    s = Settings(db_path=db_path)
    monkeypatch.setattr("pokedeals.feed.updates.check", lambda repo: result or {"behind": 3, "error": None, "repo": repo})
    return Feed(s, source=None), s


def test_chores_back_up_and_look_for_updates_once_a_day(db_path, monkeypatch):
    calls = []
    feed, s = make_feed(db_path, monkeypatch)
    monkeypatch.setattr("pokedeals.feed.updates.check", lambda repo: calls.append(repo) or {"behind": 3, "error": None})
    feed.run_chores()
    feed.run_chores()
    assert len(backup.list_backups(db_path)) == 1 and calls == ["blu360/pokedeals"]
    assert feed.status["update"]["behind"] == 3 and feed.status["last_backup"].startswith("userdata-")


def test_chores_can_be_switched_off(db_path, monkeypatch):
    from dataclasses import replace
    feed, s = make_feed(db_path, monkeypatch)
    feed.s = replace(s, backup_enabled=False, update_check=False)
    feed.run_chores()
    assert backup.list_backups(db_path) == [] and feed.status.get("update") is None


# ---- updates -----------------------------------------------------------------------------------------

def fake_github(status, body=None):
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=body or {})))


def test_the_update_check_counts_commits_main_is_ahead_by():
    r = updates.check("me/repo", current="a" * 40, client=fake_github(200, {"ahead_by": 4, "status": "ahead"}))
    assert r["behind"] == 4 and r["error"] is None
    assert updates.check("me/repo", current="a" * 40, client=fake_github(200, {"ahead_by": 0}))["behind"] == 0


def test_the_update_check_never_raises():
    assert "not reachable" in updates.check("me/repo", current="a" * 40, client=fake_github(404))["error"]
    assert "500" in updates.check("me/repo", current="a" * 40, client=fake_github(500))["error"]
    boom = httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("down"))))
    assert updates.check("me/repo", current="a" * 40, client=boom)["error"].startswith("could not check")


def test_a_folder_that_is_not_a_git_checkout_cannot_update(tmp_path):
    assert updates.local_commit(tmp_path) is None
    said = []
    assert not updates.apply_update(tmp_path, say=said.append) and "not a git checkout" in said[0]
