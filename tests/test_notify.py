"""Email, Discord, Telegram and ntfy notifications about new deals."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from pokedeals import credentials, notify
from pokedeals.config import Settings
from pokedeals.db import connect
from pokedeals.models import Listing
from pokedeals.pipeline import scan_listings
from pokedeals.matcher import Matcher
from pokedeals.web import create_app


def deal_row(db_path, price=250.0, lid="L1"):
    listing = Listing(id=lid, title="Charizard 4/102 Base Set Holo NM", price=price, shipping=0.0,
                      seller_feedback_pct=99.5, seller_feedback_score=900, url="https://www.ebay.com/itm/1")
    conn = connect(db_path)
    return scan_listings([listing], Matcher(conn), Settings(db_path=db_path), fetch=False)[0]


def recording_client(status=200):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, json={})
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def configure(db_path, **channels):
    conn = connect(db_path)
    patch = {ch: {"enabled": True, **extra} for ch, extra in channels.items() if ch != "min"}
    secrets = {"discord": "https://discord.com/api/webhooks/1/abc", "telegram": "123:TOKEN", "ntfy": "my-secret-topic",
               "email": "pw"}
    notify.save_config(conn, {**patch, **({"min": channels["min"]} if "min" in channels else {})},
                       {ch: secrets[ch] for ch in patch})
    conn.close()


# ---- configuration -----------------------------------------------------------------------------------

def test_defaults_notify_about_nothing_until_a_channel_is_set_up(db_path):
    conn = connect(db_path)
    cfg = notify.load_config(conn)
    assert notify.active_channels(cfg) == [] and cfg["min"] == "steal"


def test_a_channel_needs_its_secret_before_it_is_active(db_path):
    conn = connect(db_path)
    notify.save_config(conn, {"discord": {"enabled": True}})
    assert notify.active_channels(notify.load_config(conn)) == []
    notify.save_config(conn, {}, {"discord": "https://discord.com/api/webhooks/1/abc"})
    assert notify.active_channels(notify.load_config(conn)) == ["discord"]


def test_secrets_never_come_back_out(db_path):
    conn = connect(db_path)
    notify.save_config(conn, {"telegram": {"enabled": True, "chat_id": "42"}}, {"telegram": "123:SUPERSECRET"})
    view = json.dumps(notify.public_view(conn))
    assert "SUPERSECRET" not in view and '"telegram": true' in view      # has_secret says so, without the value
    assert "SUPERSECRET" not in connect(db_path).execute("SELECT value FROM app_settings WHERE key='notify_config'").fetchone()[0]


def test_blank_secrets_keep_what_is_stored_and_clear_removes_it(db_path):
    conn = connect(db_path)
    notify.save_config(conn, {}, {"ntfy": "topic-one"})
    notify.save_config(conn, {}, {"ntfy": "   "})
    assert credentials.secret("ntfy_topic") == "topic-one"
    notify.save_config(conn, {}, clear=["ntfy"])
    assert credentials.secret("ntfy_topic") == ""


@pytest.mark.parametrize("patch", [{"min": "everything"}, {"email": {"port": 0}}, {"email": {"security": "plain"}},
                                   {"ntfy": {"server": "ftp://x"}}])
def test_bad_values_are_rejected(db_path, patch):
    with pytest.raises(ValueError):
        notify.save_config(connect(db_path), patch)


def test_only_discord_addresses_are_accepted_for_discord(db_path):
    with pytest.raises(ValueError):
        notify.save_config(connect(db_path), {}, {"discord": "https://evil.example/hook"})


def test_the_environment_can_supply_a_secret(db_path, monkeypatch):
    monkeypatch.setenv("POKEDEALS_NTFY_TOPIC", "from-env")
    assert credentials.secret("ntfy_topic") == "from-env"


# ---- delivery ----------------------------------------------------------------------------------------

def test_each_channel_sends_what_its_service_expects(db_path):
    configure(db_path, discord={}, telegram={"chat_id": "42"}, ntfy={"server": "https://ntfy.example"})
    cfg = notify.load_config(connect(db_path))
    client, seen = recording_client()
    result = notify.send(cfg, "Steal: Charizard", "body text", "https://www.ebay.com/itm/1", client=client)
    assert result == {"discord": None, "telegram": None, "ntfy": None}
    by_host = {r.url.host: r for r in seen}
    assert json.loads(by_host["discord.com"].content)["allowed_mentions"] == {"parse": []}    # a listing title cannot ping @everyone
    tg = by_host["api.telegram.org"]
    assert tg.url.path == "/bot123:TOKEN/sendMessage" and json.loads(tg.content)["chat_id"] == "42"
    ntfy = by_host["ntfy.example"]
    assert ntfy.url.path == "/my-secret-topic" and ntfy.headers["click"] == "https://www.ebay.com/itm/1"


def test_a_failure_names_the_service_but_not_the_secret(db_path):
    configure(db_path, discord={})
    cfg = notify.load_config(connect(db_path))
    client, _ = recording_client(status=404)
    err = notify.send(cfg, "t", "b", None, client=client)["discord"]
    assert err == "Discord answered 404" and "abc" not in err
    down = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x " + str(r.url)))))
    assert notify.send(cfg, "t", "b", None, client=down)["discord"] == "could not reach Discord"


def test_one_broken_channel_does_not_stop_the_others(db_path):
    configure(db_path, discord={}, ntfy={"server": "https://ntfy.example"})
    cfg = notify.load_config(connect(db_path))

    def handler(req):
        return httpx.Response(500 if req.url.host == "discord.com" else 200)
    result = notify.send(cfg, "t", "b", None, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert result["discord"] and result["ntfy"] is None


def test_email_goes_through_the_configured_server(db_path, monkeypatch):
    configure(db_path, email={"host": "mail.example", "port": 587, "username": "me", "sender": "a@x.io", "to": "b@y.io"})
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): sent.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent.append("starttls")
        def login(self, user, pw): sent.append(("login", user, pw))
        def send_message(self, msg): sent.append(("msg", msg["To"], msg["Subject"]))
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    cfg = notify.load_config(connect(db_path))
    assert notify.send(cfg, "Subject here", "body", None) == {"email": None}
    assert sent == [("connect", "mail.example", 587), "starttls", ("login", "me", "pw"), ("msg", "b@y.io", "Subject here")]


# ---- when a notification goes out ----------------------------------------------------------------------

def announce(db_path, row, first_seen=None, **kw):
    conn = connect(db_path)
    seen = {row.listing.id: first_seen or datetime.now(timezone.utc).isoformat()}
    return notify.announce(conn, [row], seen, background=False, **kw)


def test_a_new_steal_is_announced_once(db_path, monkeypatch):
    configure(db_path, discord={})
    client, seen = recording_client()
    monkeypatch.setattr(notify.httpx, "post", client.post)
    row = deal_row(db_path)
    assert row.verdict.category == "steal"
    assert announce(db_path, row) == 1 and len(seen) == 1
    assert announce(db_path, row) == 0 and len(seen) == 1                 # told once, never again (even after a restart)


def test_old_listings_and_unwanted_categories_stay_quiet(db_path, monkeypatch):
    configure(db_path, discord={})
    client, seen = recording_client()
    monkeypatch.setattr(notify.httpx, "post", client.post)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    assert announce(db_path, deal_row(db_path), first_seen=old) == 0      # found before enabling: no flood
    assert announce(db_path, deal_row(db_path, price=330.0, lid="L2")) == 0     # a Deal, but "steals only" is the default
    configure(db_path, min="deal", discord={})
    assert announce(db_path, deal_row(db_path, price=330.0, lid="L2")) == 1
    assert len(seen) == 1


def test_nothing_happens_when_no_channel_is_on(db_path):
    assert announce(db_path, deal_row(db_path)) == 0
    assert connect(db_path).execute("SELECT count(*) FROM notified").fetchone()[0] == 0     # and nothing is marked as told


def test_several_deals_become_one_message(db_path):
    rows = [deal_row(db_path, price=250.0 + i, lid=f"L{i}") for i in range(7)]
    title, body, link = notify.build_message(rows)
    assert title == "7 new deals on eBay" and body.count("\n") == 5 and "and 2 more" in body and link is None
    one = notify.build_message(rows[:1])
    assert one[0].startswith("Steal: Charizard") and one[2] == "https://www.ebay.com/itm/1"


# ---- the page's endpoints --------------------------------------------------------------------------------

def test_endpoints_save_show_and_test(db_path, monkeypatch):
    client = TestClient(create_app(Settings(db_path=db_path), conditions=None))
    assert client.get("/api/notifications").json()["configured"]["discord"] is False
    v = client.put("/api/notifications", json={"config": {"min": "deal", "discord": {"enabled": True}},
                                               "secrets": {"discord": "https://discord.com/api/webhooks/1/abc"}}).json()
    assert v["min"] == "deal" and v["configured"]["discord"] and "abc" not in json.dumps(v)
    assert client.put("/api/notifications", json={"config": {"min": "nope"}}).status_code == 422
    assert client.post("/api/notifications/test", json={"channel": "telegram"}).status_code == 409
    http, seen = recording_client()
    monkeypatch.setattr(notify.httpx, "post", http.post)
    assert client.post("/api/notifications/test", json={"channel": "discord"}).json() == {"ok": True, "error": None}
    assert len(seen) == 1
