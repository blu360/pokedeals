"""Credentials, sandbox vs production, and eBay error handling. No network and no real keys."""
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from pokedeals import web
from pokedeals.config import Settings, detect_ebay_env, load_keyset
from pokedeals.ebay import EbaySource

SANDBOX_ID = "Someone-pkdeals-SBX-0123456789ab-cdef0123"
SANDBOX_SECRET = "SBX-0123456789ab-0123-4567-89ab-cdef"
PROD_ID = "Someone-pkdeals-PRD-0123456789ab-cdef0123"
PROD_SECRET = "PRD-0123456789ab-0123-4567-89ab-cdef"


# ---- keyset.txt ----

def test_load_keyset_in_the_format_ebays_portal_shows(tmp_path):
    f = tmp_path / "keyset.txt"
    f.write_text(f"APP ID\n{SANDBOX_ID}\n\nDEV ID\n04fd2375-dev\n\nCert ID\n{SANDBOX_SECRET}", encoding="utf-8")
    assert load_keyset(f) == {"client_id": SANDBOX_ID, "dev_id": "04fd2375-dev", "client_secret": SANDBOX_SECRET}


@pytest.mark.parametrize("text", [
    f"App ID: {PROD_ID}\nCert ID: {PROD_SECRET}\n",
    f"app id={PROD_ID}\r\ncert id={PROD_SECRET}\r\n",
    f"﻿APP ID\n\n{PROD_ID}\n\n\nCERT ID\n  {PROD_SECRET}  \n",         # BOM, blank lines, padding
])
def test_load_keyset_tolerates_formatting(tmp_path, text):
    f = tmp_path / "keyset.txt"
    f.write_text(text, encoding="utf-8")
    assert load_keyset(f) == {"client_id": PROD_ID, "client_secret": PROD_SECRET}


TWO_KEYSETS = (
    f"APP ID\n{SANDBOX_ID}\n\nDEV ID\ndev-uuid\n\nCert ID\n{SANDBOX_SECRET}\n\n\n"
    f"PRODUCTION\nAPP: {PROD_ID}\n\nCLIENT: dev-uuid\n\nCERT: {PROD_SECRET}\n"
)


def test_file_with_both_keysets_prefers_production(tmp_path):
    """The real file shape: a sandbox block, then a PRODUCTION block using APP:/CLIENT:/CERT: labels."""
    f = tmp_path / "keyset.txt"
    f.write_text(TWO_KEYSETS, encoding="utf-8")
    assert load_keyset(f) == {"client_id": PROD_ID, "client_secret": PROD_SECRET}    # bare CLIENT is ignored


def test_both_keysets_are_kept_apart(tmp_path):
    from pokedeals.config import parse_keysets
    both = parse_keysets(TWO_KEYSETS)
    assert both["sandbox"] == {"client_id": SANDBOX_ID, "dev_id": "dev-uuid", "client_secret": SANDBOX_SECRET}
    assert both["production"] == {"client_id": PROD_ID, "client_secret": PROD_SECRET}


def test_ebay_env_can_pick_the_sandbox_block(tmp_path, monkeypatch):
    f = tmp_path / "keyset.txt"
    f.write_text(TWO_KEYSETS, encoding="utf-8")
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    assert load_keyset(f)["client_id"] == SANDBOX_ID
    monkeypatch.setenv("POKEDEALS_KEYSET", str(f))
    s = Settings()
    assert (s.ebay_client_id, s.ebay_env) == (SANDBOX_ID, "sandbox")


def test_settings_use_production_when_the_file_has_both(tmp_path, monkeypatch):
    f = tmp_path / "keyset.txt"
    f.write_text(TWO_KEYSETS, encoding="utf-8")
    monkeypatch.setenv("POKEDEALS_KEYSET", str(f))
    s = Settings()
    assert (s.ebay_client_id, s.ebay_client_secret, s.ebay_env) == (PROD_ID, PROD_SECRET, "production")


def test_explicit_section_headers_work_in_either_order(tmp_path):
    f = tmp_path / "keyset.txt"
    f.write_text(f"production\nApp ID: {PROD_ID}\nCert ID: {PROD_SECRET}\nSANDBOX:\nApp ID: {SANDBOX_ID}\n"
                 f"Cert ID: {SANDBOX_SECRET}\n", encoding="utf-8")
    assert load_keyset(f)["client_id"] == PROD_ID


def test_load_keyset_missing_or_junk_file(tmp_path):
    assert load_keyset(tmp_path / "nope.txt") == {}
    junk = tmp_path / "junk.txt"
    junk.write_text("hello\nworld\n", encoding="utf-8")
    assert load_keyset(junk) == {}


# ---- environment detection ----

@pytest.mark.parametrize("ident,secret,expected", [
    (SANDBOX_ID, SANDBOX_SECRET, "sandbox"),
    ("whatever", SANDBOX_SECRET, "sandbox"),
    (SANDBOX_ID, "", "sandbox"),
    (PROD_ID, PROD_SECRET, "production"),
    ("", "", "production"),
    ("plain-id", "plain-secret", "production"),
])
def test_detect_ebay_env(ident, secret, expected):
    assert detect_ebay_env(ident, secret) == expected


def test_ebay_env_override(monkeypatch):
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    assert detect_ebay_env(PROD_ID, PROD_SECRET) == "sandbox"
    monkeypatch.setenv("EBAY_ENV", "nonsense")                       # ignored
    assert detect_ebay_env(PROD_ID, PROD_SECRET) == "production"


def test_settings_read_keyset_file_and_detect_its_environment(monkeypatch, tmp_path):
    f = tmp_path / "keyset.txt"
    f.write_text(f"APP ID\n{SANDBOX_ID}\nCert ID\n{SANDBOX_SECRET}\n", encoding="utf-8")
    monkeypatch.setenv("POKEDEALS_KEYSET", str(f))
    s = Settings()
    assert (s.ebay_client_id, s.ebay_client_secret, s.ebay_env) == (SANDBOX_ID, SANDBOX_SECRET, "sandbox")


def test_environment_variables_beat_the_file(monkeypatch, tmp_path):
    f = tmp_path / "keyset.txt"
    f.write_text(f"APP ID\n{SANDBOX_ID}\nCert ID\n{SANDBOX_SECRET}\n", encoding="utf-8")
    monkeypatch.setenv("POKEDEALS_KEYSET", str(f))
    monkeypatch.setenv("EBAY_CLIENT_ID", PROD_ID)
    monkeypatch.setenv("EBAY_CLIENT_SECRET", PROD_SECRET)
    s = Settings()
    assert (s.ebay_client_id, s.ebay_env) == (PROD_ID, "production")


def test_no_credentials_anywhere(monkeypatch):
    s = Settings()                       # conftest points the keyset at a file that does not exist
    assert (s.ebay_client_id, s.ebay_client_secret) == ("", "")


# ---- EbaySource talks to the right host ----

def recording_source(env: str, handler=None):
    seen: list[httpx.Request] = []

    def default(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        return httpx.Response(200, json={"total": 0})

    def wrapped(request):
        seen.append(request)
        return (handler or default)(request)

    s = replace(Settings(), ebay_client_id=SANDBOX_ID if env == "sandbox" else PROD_ID,
                ebay_client_secret=SANDBOX_SECRET if env == "sandbox" else PROD_SECRET, ebay_env=env)
    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return EbaySource(s, client=client), seen


@pytest.mark.parametrize("env,host", [("sandbox", "api.sandbox.ebay.com"), ("production", "api.ebay.com")])
def test_source_uses_the_environments_host(env, host):
    src, seen = recording_source(env)
    src.search("pokemon charizard")
    assert {r.url.host for r in seen} == {host}
    assert src.is_sandbox == (env == "sandbox")


def test_search_request_shape():
    src, seen = recording_source("production")
    src.search("charizard 4/102")
    search = next(r for r in seen if r.url.path.endswith("/item_summary/search"))
    p = search.url.params
    assert p["q"] == "charizard 4/102" and p["category_ids"] == "183454"
    assert p["filter"] == "priceCurrency:USD,conditionIds:{4000}"       # raw cards only, filtered by eBay
    assert p["sort"] == "newlyListed" and p["limit"] == "200"
    assert search.headers["Authorization"] == "Bearer tok"
    assert search.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert "X-EBAY-C-ENDUSERCTX" not in search.headers                  # no ZIP configured


def search_request(**settings):
    s = replace(Settings(), ebay_client_id=PROD_ID, ebay_client_secret=PROD_SECRET, ebay_env="production", **settings)
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        return httpx.Response(200, json={"total": 0})

    EbaySource(s, client=httpx.Client(transport=httpx.MockTransport(handler))).search("x")
    return next(r for r in seen if "search" in r.url.path)


def test_graded_listings_can_be_included():
    assert search_request(ebay_ungraded_only=False).url.params["filter"] == "priceCurrency:USD"


def test_buyer_zip_is_sent_url_encoded_so_calculated_shipping_gets_a_price():
    """Verified against live eBay: the raw form is silently ignored, the URL-encoded form prices every listing."""
    r = search_request(ebay_buyer_zip="64101")
    assert r.headers["X-EBAY-C-ENDUSERCTX"] == "contextualLocation=country%3DUS%2Czip%3D64101"


@pytest.mark.parametrize("bad", ["", "6410", "641011", "abcde", "64101-1234", "  "])
def test_an_invalid_zip_is_ignored_rather_than_sent(bad):
    assert "X-EBAY-C-ENDUSERCTX" not in search_request(ebay_buyer_zip=bad).headers


def test_zip_comes_from_keyset_file(tmp_path, monkeypatch):
    f = tmp_path / "keyset.txt"
    f.write_text(f"PRODUCTION\nAPP: {PROD_ID}\nCERT: {PROD_SECRET}\nZIP: 64101\n", encoding="utf-8")
    monkeypatch.setenv("POKEDEALS_KEYSET", str(f))
    assert Settings().ebay_buyer_zip == "64101"
    monkeypatch.setenv("EBAY_BUYER_ZIP", "10001")                  # the environment wins
    assert Settings().ebay_buyer_zip == "10001"


def test_zip_is_found_even_when_it_sits_in_the_other_environments_block(tmp_path):
    f = tmp_path / "keyset.txt"
    f.write_text(f"SANDBOX\nAPP: {SANDBOX_ID}\nCERT: {SANDBOX_SECRET}\nZIP: 64101\n"
                 f"PRODUCTION\nAPP: {PROD_ID}\nCERT: {PROD_SECRET}\n", encoding="utf-8")
    assert load_keyset(f) == {"client_id": PROD_ID, "client_secret": PROD_SECRET, "buyer_zip": "64101"}


def test_empty_category_is_left_out_of_the_request():
    s = replace(Settings(), ebay_client_id=PROD_ID, ebay_client_secret=PROD_SECRET, ebay_env="production",
                ebay_category_ids="")
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        return httpx.Response(200, json={"total": 0})

    EbaySource(s, client=httpx.Client(transport=httpx.MockTransport(handler))).search("x")
    assert "category_ids" not in next(r for r in seen if "search" in r.url.path).url.params


# ---- errors: informative, and never leak credentials ----

def test_token_failure_explains_itself_without_leaking_the_secret():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_client", "error_description": "client authentication failed"})

    src, _ = recording_source("sandbox", handler)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        src.search("x")
    text = str(exc.value)
    assert "401" in text and "client authentication failed" in text
    assert SANDBOX_SECRET not in text and SANDBOX_ID not in text


def test_search_failure_reports_ebays_message():
    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        return httpx.Response(403, json={"errors": [{"errorId": 1100, "message": "Insufficient permissions"}]})

    src, _ = recording_source("production", handler)
    with pytest.raises(httpx.HTTPStatusError, match="403: Insufficient permissions"):
        src.search("x")


def test_error_body_that_is_not_json_still_raises_cleanly():
    src, _ = recording_source("production", lambda r: httpx.Response(502, text="<html>Bad gateway</html>"))
    with pytest.raises(httpx.HTTPStatusError, match="eBay returned 502"):
        src.search("x")


# ---- the UI is told which environment it is talking to ----

class FakeSource:
    def __init__(self, env):
        self.env = env

    def search(self, query, *, max_results=None):
        from pokedeals.ebay import SearchResult
        return SearchResult([], 0)


@pytest.mark.parametrize("env", ["sandbox", "production"])
def test_status_reports_the_ebay_environment(db_path, monkeypatch, env):
    monkeypatch.setattr(web, "make_source", lambda settings, fixture=None: (FakeSource(env), "live"))
    client = TestClient(web.create_app(replace(Settings(), db_path=db_path)))
    status = client.get("/api/status").json()
    assert status["mode"] == "live" and status["ebay_env"] == env


def test_status_has_no_environment_in_sample_mode(db_path, tmp_path):
    fixture = tmp_path / "l.json"
    fixture.write_text(json.dumps([]), encoding="utf-8")
    client = TestClient(web.create_app(replace(Settings(), db_path=db_path), fixture=fixture))
    status = client.get("/api/status").json()
    assert status["mode"] == "sample" and status["ebay_env"] is None
