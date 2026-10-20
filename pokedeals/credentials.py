"""Where the eBay keys come from and where they are kept.

Looked up in this order, so the most deliberate choice wins:
  1. environment variables EBAY_CLIENT_ID / EBAY_CLIENT_SECRET (and EBAY_BUYER_ZIP), or a file named by
     EBAY_CLIENT_ID_FILE / EBAY_CLIENT_SECRET_FILE (Docker and Kubernetes secrets mount keys this way);
  2. the operating system's keyring (Windows Credential Manager, macOS Keychain, Secret Service on Linux),
     where the first-run prompt saves them;
  3. a private file in the data folder (owner-only permissions), used when there is no keyring;
  4. keyset.txt, the original format (see config.py).

Nothing here ever prints or logs a value.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

SERVICE = "pokedeals"
FIELDS = ("client_id", "client_secret")


def env_value(name: str) -> str:
    """$NAME, or the contents of the file named by $NAME_FILE; '' when neither is set."""
    if value := os.environ.get(name, "").strip():
        return value
    if path := os.environ.get(name + "_FILE", "").strip():
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def credentials_file() -> Path:
    if override := os.environ.get("POKEDEALS_CREDENTIALS"):
        return Path(override)
    root = Path(__file__).resolve().parent.parent
    db = Path(os.environ.get("POKEDEALS_DB") or root / "data" / "pokedeals.db")
    return db.parent / "credentials.json"


def _keyring():
    """The keyring module when this machine has a working backend, else None."""
    if os.environ.get("POKEDEALS_NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    return None if isinstance(keyring.get_keyring(), fail.Keyring) else keyring


@lru_cache(maxsize=1)
def stored() -> dict[str, str]:
    """Keys saved by the first-run prompt: {"client_id", "client_secret"} or {}."""
    kr = _keyring()
    if kr is not None:
        try:
            found = {f: kr.get_password(SERVICE, f) or "" for f in FIELDS}
            if all(found.values()):
                return found
        except Exception:                      # noqa: BLE001 - a locked or broken keyring must not stop startup
            pass
    try:
        data = json.loads(credentials_file().read_text(encoding="utf-8"))
        found = {f: str(data.get(f, "")).strip() for f in FIELDS}
        return found if all(found.values()) else {}
    except (OSError, ValueError):
        return {}


def save(client_id: str, client_secret: str) -> str:
    """Keep the keys for next time. Returns where: 'keyring' or the path of the private file."""
    stored.cache_clear()
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, "client_id", client_id)
            kr.set_password(SERVICE, "client_secret", client_secret)
            credentials_file().unlink(missing_ok=True)       # no second, weaker copy
            return "keyring"
        except Exception:                      # noqa: BLE001
            pass
    path = credentials_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)      # owner-only from the first byte
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"client_id": client_id, "client_secret": client_secret}, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)


def forget() -> None:
    """Remove saved keys (keyring and file)."""
    stored.cache_clear()
    kr = _keyring()
    if kr is not None:
        for f in FIELDS:
            try:
                kr.delete_password(SERVICE, f)
            except Exception:                  # noqa: BLE001
                pass
    credentials_file().unlink(missing_ok=True)


# ---- other secrets (SMTP password, webhook addresses, bot tokens, ntfy topic) --------------------------
# Same idea as the eBay keys: POKEDEALS_<NAME> in the environment (or ..._FILE), else the keyring, else a private file.

def secrets_file() -> Path:
    return credentials_file().with_name("secrets.json")


def _read_secrets_file() -> dict:
    try:
        return json.loads(secrets_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def secret(name: str) -> str:
    if value := env_value("POKEDEALS_" + name.upper()):
        return value
    kr = _keyring()
    if kr is not None:
        try:
            if value := kr.get_password(SERVICE, name):
                return value
        except Exception:                      # noqa: BLE001
            pass
    return str(_read_secrets_file().get(name, ""))


def save_secret(name: str, value: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, name, value)
            return
        except Exception:                      # noqa: BLE001
            pass
    data = _read_secrets_file()
    data[name] = value
    path = secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)


def forget_secret(name: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:                      # noqa: BLE001
            pass
    data = _read_secrets_file()
    if data.pop(name, None) is not None:
        secrets_file().write_text(json.dumps(data), encoding="utf-8")
