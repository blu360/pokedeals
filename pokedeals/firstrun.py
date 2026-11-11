"""First-run setup: download the card catalogue and prices on the first start, in the background, so the
page can show progress. When it finishes, the live feed starts."""
from __future__ import annotations

import asyncio
import re
import threading
from typing import Callable

from .config import Settings
from .db import connect
from .sync import sync

_STEP = re.compile(r"\[(\d+)/(\d+)\]")
CARDS_SHARE = 90            # the set-by-set download is most of the wait; the price feed is the rest


def catalogue_empty(settings: Settings) -> bool:
    conn = connect(settings.db_path)
    try:
        return conn.execute("SELECT count(*) FROM cards").fetchone()[0] == 0
    finally:
        conn.close()


class FirstRun:
    """Runs `sync` once in a thread when the card catalogue is empty and reports how far it got."""

    def __init__(self, settings: Settings, run_sync: Callable[..., object] = sync):
        self.s = settings
        self._sync = run_sync
        self._thread: threading.Thread | None = None
        self.state = "ready"                       # ready | running | error
        self.percent = 100
        self.message = ""
        self.error: str | None = None

    def needed(self) -> bool:
        return catalogue_empty(self.s)

    def start(self, on_ready: Callable[[], None]) -> None:
        self.state, self.percent, self.error = "running", 0, None
        self.message = "Starting…"
        self._thread = threading.Thread(target=self._run, args=(on_ready,), name="pokedeals-firstrun", daemon=True)
        self._thread.start()

    def _run(self, on_ready: Callable[[], None]) -> None:
        try:
            stats = asyncio.run(self._sync(self.s, progress=self._progress))
            if stats.get("cards", 0) == 0:
                raise RuntimeError("no cards were downloaded; check the internet connection and start again")
        except Exception as e:                     # noqa: BLE001 - shown in the page
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            return
        self.state, self.percent, self.message = "ready", 100, "Done"
        on_ready()

    def _progress(self, msg: str) -> None:
        if m := _STEP.search(msg):
            done, total = int(m.group(1)), max(1, int(m.group(2)))
            self.percent = min(CARDS_SHARE, round(done / total * CARDS_SHARE))
            self.message = (f"Downloading cards: set {done} of {total}" if done < total
                            else "Downloading TCGplayer prices…")
        elif "feed" in msg.lower() or "price" in msg.lower():
            self.percent = max(self.percent, CARDS_SHARE)
            self.message = "Downloading TCGplayer prices…"

    def info(self) -> dict:
        return {"state": self.state, "percent": self.percent, "message": self.message, "error": self.error}
