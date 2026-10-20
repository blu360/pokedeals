"""What the Status page shows: eBay usage against the daily limit, data freshness, source health, backups, updates."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import backup
from .config import Settings
from .feed import Feed, calls_today


def _api_usage(conn, s: Settings) -> dict:
    by_task = calls_today(conn)
    used = sum(by_task.values())
    now = datetime.now(timezone.utc)
    resets = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    share = used / max(1, s.ebay_daily_limit)
    return {"used": used, "limit": s.ebay_daily_limit, "budget": s.feed_daily_call_budget, "by_task": by_task,
            "resets_at": resets.isoformat(), "level": "full" if share >= 1 else "warn" if share >= 0.8 else "ok"}


def build(s: Settings, feed: Feed | None, conditions, conn) -> dict:
    info = feed.info() if feed is not None else {}
    cond_state = conditions.state() if conditions is not None else None
    price_error = bool(info.get("error") and "price refresh failed" in str(info["error"]))
    sources = [
        {"name": "eBay listings", "ok": feed is not None and not (info.get("error") and not price_error),
         "detail": "No eBay keys are set up, so there are no live listings (see the README)." if feed is None
         else info.get("error") if not price_error else None},
        {"name": "TCGplayer daily price feed (tcgcsv.com)", "ok": not price_error,
         "detail": info.get("error") if price_error else None},
    ]
    if cond_state is not None:
        sources.append({"name": "TCGplayer prices by condition", "ok": cond_state["available"],
                        "detail": None if cond_state["available"] else
                        f"TCGplayer's site is not answering; paused for {cond_state['retry_in_seconds'] // 60 + 1} more "
                        "minutes. Worth uses the daily feed price meanwhile (marked as an estimate)."})
    return {
        "api": _api_usage(conn, s),
        "prices": {"as_of": conn.execute("SELECT max(prices_updated) FROM cards").fetchone()[0],
                   "checked_at": info.get("prices_checked_at")},
        "sources": sources,
        "feed": {k: info.get(k) for k in ("running", "cycling", "cycles", "last_cycle_at", "last_cycle_seconds",
                                          "tracked", "deals", "error", "budget_reached")} if feed is not None else None,
        "backups": backup.list_backups(s.db_path)[:5],
        "update": info.get("update"),
    }
