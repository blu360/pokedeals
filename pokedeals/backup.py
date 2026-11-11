"""Backups of what cannot be downloaded again: your watchlist and settings.

Catalogue, prices and listings are re-created from the internet, so a backup is a few KB (one gzipped JSON
file per day). Secrets (eBay keys, SMTP password, tokens) are deliberately left out: see credentials.py.
"""
from __future__ import annotations

import gzip
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import connect

TRANSIENT = ("calls:", "feed_calls:", "cond_", "update_check", "backup_")      # tallies and caches, not choices
_NAME = re.compile(r"^userdata-(\d{8}-\d{6})\.json\.gz$")


def backup_dir(db_path: Path) -> Path:
    return db_path.parent / "backups"


def snapshot(conn: sqlite3.Connection) -> dict:
    settings = {k: v for k, v in conn.execute("SELECT key, value FROM app_settings")
                if not k.startswith(TRANSIENT)}
    watchlist = [{"card_id": r[0], "added_at": r[1]} for r in conn.execute("SELECT card_id, added_at FROM watchlist")]
    return {"version": 1, "made_at": datetime.now(timezone.utc).isoformat(), "settings": settings, "watchlist": watchlist}


def list_backups(db_path: Path) -> list[dict]:
    """Newest first: [{"name", "path", "bytes", "made_at"}]."""
    out = []
    for p in backup_dir(db_path).glob("userdata-*.json.gz"):
        if m := _NAME.match(p.name):
            made = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
            out.append({"name": p.name, "path": str(p), "bytes": p.stat().st_size, "made_at": made.isoformat()})
    return sorted(out, key=lambda b: b["name"], reverse=True)


def make_backup(db_path: Path, keep: int = 14) -> Path:
    """Write today's backup file and delete the oldest beyond `keep`."""
    conn = connect(db_path)
    try:
        data = snapshot(conn)
    finally:
        conn.close()
    folder = backup_dir(db_path)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"userdata-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f)
    for old in list_backups(db_path)[max(1, keep):]:
        Path(old["path"]).unlink(missing_ok=True)
    return path


def due(db_path: Path, every_hours: float = 24) -> bool:
    latest = list_backups(db_path)
    if not latest:
        return True
    made = datetime.fromisoformat(latest[0]["made_at"])
    return (datetime.now(timezone.utc) - made).total_seconds() >= every_hours * 3600


def restore_backup(db_path: Path, path: Path) -> dict[str, int]:
    """Put a backup's watchlist and settings back. Cards must already be synced (a watched card that no longer
    exists is skipped). Adds to what is there; it never deletes."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    conn = connect(db_path)
    try:
        known = {r[0] for r in conn.execute("SELECT id FROM cards")}
        watched = [w for w in data.get("watchlist", []) if w["card_id"] in known]
        conn.executemany("INSERT OR IGNORE INTO watchlist (card_id, added_at) VALUES (?, ?)",
                         [(w["card_id"], w["added_at"]) for w in watched])
        conn.executemany("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                         list(data.get("settings", {}).items()))
        conn.commit()
        return {"watchlist": len(watched), "settings": len(data.get("settings", {}))}
    finally:
        conn.close()
