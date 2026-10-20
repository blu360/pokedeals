"""The first-start questions: eBay keys (checked before they are kept) and, optionally, a ZIP code."""
from __future__ import annotations

import getpass
import re
from dataclasses import replace
from typing import Callable

import httpx

from . import credentials
from .config import Settings, detect_ebay_env
from .db import connect, set_setting
from .ebay import EbaySource
from .pipeline import ZIP_KEY, has_ebay_keys

KEYS_URL = "https://developer.ebay.com/my/keys"
GUIDE = f"""
pokedeals needs a free eBay developer account to look at listings (eBay allows 5,000 requests a day).

  1. Sign in at {KEYS_URL}  (create the account first if you do not have one).
  2. Under "Production", create a keyset. Copy two values:
       App ID   (also called Client ID)
       Cert ID  (also called Client Secret)
  3. Paste them below. They are checked with eBay, then kept in your operating system's keyring
     (or an owner-only file where there is none). They never leave this computer except to go to eBay.

Press Enter at the first question to skip this and add the keys later (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET).
"""


def check_keys(client_id: str, client_secret: str) -> tuple[bool, str]:
    """(ok, detail): try to get a token from eBay with these keys. Never includes the keys in the detail."""
    try:
        trial = replace(Settings(), ebay_client_id=client_id, ebay_client_secret=client_secret,
                        ebay_env=detect_ebay_env(client_id, client_secret))
        src = EbaySource(trial)
        src._auth()
    except httpx.HTTPStatusError as e:
        return False, str(e)
    except httpx.HTTPError:
        return False, "could not reach eBay; check the internet connection"
    return True, src.env


def ensure_credentials(
    *, interactive: bool, ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass, say: Callable[[str], None] = print,
    attempts: int = 3,
) -> bool:
    """True when eBay keys are available afterwards. Prompts only on a terminal, only when none are set."""
    if has_ebay_keys(Settings()):
        return True
    if not interactive:
        say("No eBay keys found. Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (or ..._FILE for Docker secrets) and "
            "start again. Without them the site runs without live listings.")
        return False
    say(GUIDE)
    for _ in range(attempts):
        try:
            client_id = ask("App ID (Client ID): ").strip()
            secret = ask_secret("Cert ID (Client Secret), hidden as you type: ").strip() if client_id else ""
        except (EOFError, KeyboardInterrupt):                # no one is typing (piped input, Ctrl+C)
            client_id = ""
        if not client_id:
            say("Skipped. Run again whenever you are ready; nothing was saved.")
            return False
        ok, detail = check_keys(client_id, secret)
        if ok:
            where = credentials.save(client_id, secret)
            say(f"Connected to eBay ({detail}). Keys saved in {'your keyring' if where == 'keyring' else where}.")
            if detail == "sandbox":
                say("Note: these are SANDBOX keys (eBay's test data, no real cards). Create a Production keyset for real deals.")
            return True
        say(f"eBay did not accept those keys: {detail}")
    say("Giving up for now. Nothing was saved.")
    return False


def ask_zip(settings: Settings, *, ask: Callable[[str], str] = input, say: Callable[[str], None] = print) -> None:
    """Optional: a US ZIP lets eBay price 'calculated' shipping (about a third of listings)."""
    conn = connect(settings.db_path)
    try:
        from .db import get_setting
        if get_setting(conn, ZIP_KEY) or settings.ebay_buyer_zip:
            return
        zip_code = ask("Your 5-digit US ZIP code, for real shipping costs (Enter to skip): ").strip()
        if re.fullmatch(r"\d{5}", zip_code):
            set_setting(conn, ZIP_KEY, zip_code)
            say("ZIP saved. You can change it any time under Tools.")
        elif zip_code:
            say("That is not a 5-digit ZIP; skipped. You can set it under Tools.")
    finally:
        conn.close()
