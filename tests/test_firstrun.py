"""First start: credentials storage, the key prompt, the background catalogue download and the one-command launcher."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pokedeals import credentials, onboarding
from pokedeals.config import Settings
from pokedeals.firstrun import FirstRun, catalogue_empty
from pokedeals.web import create_app

ROOT = Path(__file__).resolve().parent.parent


# ---- where the keys come from -----------------------------------------------------------------------

def test_a_key_file_named_by_the_environment_is_read_like_docker_secrets(tmp_path, monkeypatch):
    f = tmp_path / "id.txt"
    f.write_text("abc-123\n", encoding="utf-8")
    monkeypatch.setenv("EBAY_CLIENT_ID_FILE", str(f))
    assert credentials.env_value("EBAY_CLIENT_ID") == "abc-123"
    monkeypatch.setenv("EBAY_CLIENT_ID", "direct")                       # the variable itself wins
    assert credentials.env_value("EBAY_CLIENT_ID") == "direct"
    monkeypatch.delenv("EBAY_CLIENT_ID")
    monkeypatch.setenv("EBAY_CLIENT_ID_FILE", str(tmp_path / "missing.txt"))
    assert credentials.env_value("EBAY_CLIENT_ID") == ""


def test_saved_keys_are_found_by_settings_and_can_be_forgotten():
    assert Settings().ebay_client_id == ""
    where = credentials.save("PRD-app", "PRD-cert")
    assert where.endswith("no-credentials.json")                         # the tests have no keyring: the private file
    s = Settings()
    assert (s.ebay_client_id, s.ebay_client_secret, s.ebay_env) == ("PRD-app", "PRD-cert", "production")
    credentials.forget()
    assert Settings().ebay_client_id == ""


def test_the_environment_beats_saved_keys(monkeypatch):
    credentials.save("saved-id", "saved-secret")
    monkeypatch.setenv("EBAY_CLIENT_ID", "env-id")
    assert Settings().ebay_client_id == "env-id" and Settings().ebay_client_secret == "saved-secret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_key_file_is_private_to_the_owner():
    credentials.save("a", "b")
    assert credentials.credentials_file().stat().st_mode & 0o077 == 0


# ---- the prompt ----------------------------------------------------------------------------------------

def scripted(answers):
    it = iter(answers)
    return lambda prompt: next(it)


def test_a_valid_key_pair_is_checked_then_kept(monkeypatch):
    checked = []
    monkeypatch.setattr(onboarding, "check_keys", lambda i, s: (checked.append((i, s)) or (True, "production")))
    out = []
    ok = onboarding.ensure_credentials(interactive=True, ask=scripted(["PRD-app"]), ask_secret=scripted(["PRD-cert"]),
                                       say=out.append)
    assert ok and checked == [("PRD-app", "PRD-cert")] and Settings().ebay_client_secret == "PRD-cert"
    assert not any("PRD-cert" in line for line in out)                   # the secret is never echoed


def test_a_wrong_key_is_not_saved_and_you_can_try_again(monkeypatch):
    results = iter([(False, "eBay returned 401"), (True, "production")])
    monkeypatch.setattr(onboarding, "check_keys", lambda i, s: next(results))
    ok = onboarding.ensure_credentials(interactive=True, ask=scripted(["bad", "good"]),
                                       ask_secret=scripted(["bad", "good"]), say=lambda m: None)
    assert ok and Settings().ebay_client_id == "good"


def test_pressing_enter_skips_and_saves_nothing(monkeypatch):
    monkeypatch.setattr(onboarding, "check_keys", lambda i, s: pytest.fail("nothing to check"))
    assert not onboarding.ensure_credentials(interactive=True, ask=scripted([""]), say=lambda m: None)
    assert Settings().ebay_client_id == ""


def test_no_terminal_means_no_prompt(monkeypatch):
    out = []
    assert not onboarding.ensure_credentials(interactive=False, say=out.append)
    assert "EBAY_CLIENT_ID" in out[0]


def test_nothing_is_asked_when_keys_already_exist(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "x")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "y")
    assert onboarding.ensure_credentials(interactive=True, ask=lambda p: pytest.fail("asked"))


def test_check_keys_never_returns_the_keys(monkeypatch):
    def refuse(self):
        raise httpx.HTTPStatusError("eBay returned 401: invalid_client", request=httpx.Request("POST", "https://x"),
                                    response=httpx.Response(401))
    monkeypatch.setattr(onboarding.EbaySource, "_auth", refuse)
    ok, detail = onboarding.check_keys("SECRET-ID", "SECRET-CERT")
    assert not ok and "SECRET" not in detail


# ---- the first download -------------------------------------------------------------------------------

def test_an_empty_catalogue_is_detected(db_path, tmp_path):
    assert not catalogue_empty(Settings(db_path=db_path))
    assert catalogue_empty(Settings(db_path=tmp_path / "empty.db"))


def test_progress_follows_the_download_then_the_feed_starts(tmp_path):
    seen = []

    async def fake_sync(settings, progress):
        progress("[1/4] base1   102 cards  (3s)")
        seen.append(run.info())
        progress("[4/4] swsh3   189 cards  (9s)")
        seen.append(run.info())
        return {"cards": 291}
    started = []
    run = FirstRun(Settings(db_path=tmp_path / "x.db"), run_sync=fake_sync)
    run.start(on_ready=lambda: started.append(1))
    run._thread.join(5)
    assert seen[0]["percent"] == 22 and "set 1 of 4" in seen[0]["message"]
    assert seen[1]["percent"] == 90 and "prices" in seen[1]["message"]
    assert run.info()["state"] == "ready" and run.percent == 100 and started == [1]


def test_a_failed_download_is_reported_and_the_feed_does_not_start(tmp_path):
    async def broken(settings, progress):
        return {"cards": 0}
    started = []
    run = FirstRun(Settings(db_path=tmp_path / "x.db"), run_sync=broken)
    run.start(on_ready=lambda: started.append(1))
    run._thread.join(5)
    assert run.state == "error" and "no cards" in run.error and started == []


def test_the_setup_endpoint_reports_ready_for_a_normal_start(db_path):
    client = TestClient(create_app(Settings(db_path=db_path), conditions=None))
    assert client.get("/api/setup").json() == {"state": "ready", "percent": 100, "message": "", "error": None}


# ---- the one-command launcher -----------------------------------------------------------------------

@pytest.fixture()
def launcher(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("pokedeals_start", ROOT / "start.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "VENV", tmp_path / ".venv")
    monkeypatch.setattr(mod, "REQUIREMENTS", tmp_path / "requirements.txt")
    return mod


def test_install_runs_once_and_again_only_when_requirements_change(launcher, tmp_path):
    (tmp_path / ".venv").mkdir()
    assert launcher.needs_install()
    launcher._stamp_file().write_text(launcher._requirements_hash(), encoding="utf-8")
    assert not launcher.needs_install()
    (tmp_path / "requirements.txt").write_text("httpx\nkeyring\n", encoding="utf-8")
    assert launcher.needs_install()


def test_the_launcher_refuses_an_old_python(launcher, monkeypatch, capsys):
    monkeypatch.setattr(launcher.sys, "version_info", (3, 9, 0))
    assert launcher.main([]) == 1 and "3.11" in capsys.readouterr().out


def test_the_launcher_passes_its_arguments_to_start(launcher, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "ensure_environment", lambda: None)
    monkeypatch.setattr(launcher.subprocess, "call", lambda cmd, cwd=None: calls.append(cmd) or 0)
    assert launcher.main(["--port", "9000"]) == 0
    assert calls[0][-4:] == ["pokedeals", "start", "--port", "9000"]
