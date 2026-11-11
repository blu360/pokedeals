"""Tell people about new deals: email, Discord, Telegram and ntfy.sh (desktop notifications come from the page).

Choices live in the database; secrets (SMTP password, webhook address, bot token, ntfy topic) are kept with
the other secrets (see credentials.py) and are never sent to the page or put in an error message.
"""
from __future__ import annotations

import json
import smtplib
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from . import credentials
from .db import get_setting, set_setting

CHANNELS = ("email", "discord", "telegram", "ntfy")
SECRETS = {"email": "smtp_password", "discord": "discord_webhook", "telegram": "telegram_token", "ntfy": "ntfy_topic"}
DEFAULTS: dict[str, Any] = {
    "min": "steal",                      # "steal": only steals, "deal": steals and deals
    "include_uncertain": False,
    "email": {"enabled": False, "host": "", "port": 587, "security": "starttls", "username": "", "sender": "", "to": ""},
    "discord": {"enabled": False},
    "telegram": {"enabled": False, "chat_id": ""},
    "ntfy": {"enabled": False, "server": "https://ntfy.sh"},
}
WINDOW_MINUTES = 30                      # only listings first seen this recently count as new
MAX_LISTED = 5
KEY = "notify_config"


class NotifyError(Exception):
    """A delivery problem, worded so it can be shown to the user (never contains a secret)."""


# ---- configuration ----

def load_config(conn) -> dict:
    stored = {}
    try:
        stored = json.loads(get_setting(conn, KEY) or "{}")
    except ValueError:
        pass
    cfg = json.loads(json.dumps(DEFAULTS))
    for key, value in stored.items():
        if isinstance(cfg.get(key), dict) and isinstance(value, dict):
            cfg[key].update({k: v for k, v in value.items() if k in cfg[key]})
        elif key in cfg and not isinstance(cfg[key], dict):
            cfg[key] = value
    return cfg


def configured(channel: str, cfg: dict) -> bool:
    """Has everything the channel needs (secrets included)."""
    c = cfg[channel]
    has_secret = bool(credentials.secret(SECRETS[channel]))
    if channel == "email":
        return bool(c["host"] and c["sender"] and c["to"] and (has_secret or not c["username"]))
    if channel == "telegram":
        return bool(c["chat_id"]) and has_secret
    return has_secret


def active_channels(cfg: dict) -> list[str]:
    return [ch for ch in CHANNELS if cfg[ch]["enabled"] and configured(ch, cfg)]


def public_view(conn) -> dict:
    """The configuration for the page: choices only; secrets are reported as present or not."""
    cfg = load_config(conn)
    return {**cfg, "configured": {ch: configured(ch, cfg) for ch in CHANNELS},
            "has_secret": {ch: bool(credentials.secret(SECRETS[ch])) for ch in CHANNELS}}


def save_config(conn, patch: dict, secrets: dict[str, str] | None = None, clear: list[str] | None = None) -> dict:
    """Merge a partial configuration. Raises ValueError for a bad value. Blank secrets are ignored (keep what is there)."""
    cfg = load_config(conn)
    if "min" in patch:
        if patch["min"] not in ("steal", "deal"):
            raise ValueError("min must be 'steal' or 'deal'")
        cfg["min"] = patch["min"]
    if "include_uncertain" in patch:
        cfg["include_uncertain"] = bool(patch["include_uncertain"])
    for ch in CHANNELS:
        for k, v in (patch.get(ch) or {}).items():
            if k not in cfg[ch]:
                continue
            if k == "enabled":
                v = bool(v)
            elif k == "port":
                v = int(v)
                if not 1 <= v <= 65535:
                    raise ValueError("port must be between 1 and 65535")
            elif k == "security":
                if v not in ("starttls", "ssl", "none"):
                    raise ValueError("security must be starttls, ssl or none")
            elif k == "server":
                if urlparse(str(v)).scheme not in ("http", "https"):
                    raise ValueError("the ntfy server must start with http:// or https://")
                v = str(v).rstrip("/")
            else:
                v = str(v).strip()
            cfg[ch][k] = v
    for ch, value in (secrets or {}).items():
        if ch in SECRETS and str(value).strip():
            value = str(value).strip()
            if ch == "discord" and urlparse(value).hostname not in ("discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"):
                raise ValueError("that does not look like a Discord webhook address")
            credentials.save_secret(SECRETS[ch], value)
    for ch in clear or []:
        if ch in SECRETS:
            credentials.forget_secret(SECRETS[ch])
    set_setting(conn, KEY, json.dumps(cfg))
    return cfg


# ---- what gets said ----

def describe(row) -> str:
    v, d = row.verdict, row.verdict.deal
    card = d.match.card
    name = f"{card.name} · {card.set_name} #{card.local_id}" if card else row.listing.title[:70]
    kind = (v.category or "deal").capitalize()
    extra = " (price uncertain)" if v.category == "uncertain" else ""
    return f"{kind}: {name} {d.condition}: ${d.total_cost:.2f} vs worth ${d.fair_value:.2f} ({round(d.discount * 100)}% under){extra}"


def build_message(rows: list) -> tuple[str, str, str | None]:
    """(title, body, link) for one notification covering `rows` (best first)."""
    title = describe(rows[0]) if len(rows) == 1 else f"{len(rows)} new deals on eBay"
    lines = [describe(r) for r in rows[:MAX_LISTED]]
    if len(rows) > MAX_LISTED:
        lines.append(f"...and {len(rows) - MAX_LISTED} more")
    link = rows[0].listing.url if len(rows) == 1 else None
    return title, "\n".join(lines) if len(rows) > 1 else lines[0], link


def wanted_categories(cfg: dict) -> set[str]:
    cats = {"steal"} | ({"deal"} if cfg["min"] == "deal" else set())
    return cats | ({"uncertain"} if cfg["include_uncertain"] else set())


# ---- delivery ----

def _post(url: str, service: str, client: httpx.Client | None, **kw) -> None:
    try:
        r = (client or httpx).post(url, timeout=10, **kw)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise NotifyError(f"{service} answered {e.response.status_code}") from None
    except httpx.HTTPError:
        raise NotifyError(f"could not reach {service}") from None


def deliver(channel: str, cfg: dict, title: str, body: str, link: str | None, client: httpx.Client | None = None) -> None:
    secret = credentials.secret(SECRETS[channel])
    c = cfg[channel]
    text = body + (f"\n{link}" if link else "")
    if channel == "discord":
        _post(secret, "Discord", client, json={"content": f"**{title}**\n{text}"[:1900], "allowed_mentions": {"parse": []}})
    elif channel == "telegram":
        _post(f"https://api.telegram.org/bot{secret}/sendMessage", "Telegram", client,
              json={"chat_id": c["chat_id"], "text": f"{title}\n{text}"[:4000], "disable_web_page_preview": True})
    elif channel == "ntfy":
        headers = {"Title": title.encode("latin-1", "replace").decode("latin-1") or "pokedeals", "Tags": "moneybag"}   # headers are latin-1
        if link:
            headers["Click"] = link
        _post(f"{c['server']}/{secret}", "ntfy", client, content=text.encode("utf-8"), headers=headers)
    elif channel == "email":
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, c["sender"], c["to"]
        msg.set_content(text)
        try:
            smtp_cls = smtplib.SMTP_SSL if c["security"] == "ssl" else smtplib.SMTP
            with smtp_cls(c["host"], c["port"], timeout=15) as smtp:
                if c["security"] == "starttls":
                    smtp.starttls()
                if c["username"]:
                    smtp.login(c["username"], secret)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise NotifyError("the mail server refused the username or password") from None
        except (smtplib.SMTPException, OSError):
            raise NotifyError("could not send through the mail server; check host, port and security") from None


def send(cfg: dict, title: str, body: str, link: str | None, channels: list[str] | None = None,
         client: httpx.Client | None = None) -> dict[str, str | None]:
    """Deliver to each channel; {channel: None on success or the problem}. Never raises."""
    result: dict[str, str | None] = {}
    for ch in channels if channels is not None else active_channels(cfg):
        try:
            deliver(ch, cfg, title, body, link, client)
            result[ch] = None
        except NotifyError as e:
            result[ch] = str(e)
        except Exception as e:                      # noqa: BLE001 - one broken channel must not stop the others
            result[ch] = f"unexpected problem ({type(e).__name__})"
    return result


# ---- the feed's hook ----

def announce(conn, rows: list, first_seen: dict[str, str | None], *, background: bool = True,
             on_result: Callable[[dict], None] | None = None) -> int:
    """Notify about deals first seen in the last half hour that nobody was told about yet. Returns how many."""
    cfg = load_config(conn)
    channels = active_channels(cfg)
    if not channels:
        return 0
    cats = wanted_categories(cfg)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)).isoformat()
    told = {r[0] for r in conn.execute("SELECT id FROM notified")}
    fresh = [r for r in rows if r.verdict.category in cats and not r.verdict.blocked and r.verdict.deal is not None
             and r.listing.id not in told and (first_seen.get(r.listing.id) or "") >= cutoff]
    if not fresh:
        return 0
    fresh.sort(key=lambda r: -r.verdict.deal.discount)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany("INSERT OR IGNORE INTO notified (id, at) VALUES (?, ?)", [(r.listing.id, now) for r in fresh])
    conn.execute("DELETE FROM notified WHERE at < ?", ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),))
    conn.commit()
    title, body, link = build_message(fresh)
    job = lambda: (on_result or (lambda _r: None))(send(cfg, title, body, link, channels))      # noqa: E731
    if background:
        threading.Thread(target=job, name="pokedeals-notify", daemon=True).start()
    else:
        job()
    return len(fresh)
