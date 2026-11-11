"""TCGplayer market prices from tcgcsv.com, applied over the TCGdex prices.

TCGdex's TCGplayer snapshot has gaps (e.g. every card in a brand-new set is null) and can lag. tcgcsv.com
republishes TCGplayer's own product and price feed as JSON once a day for every Pokémon set ("group").
This module:

  1. downloads the feed (about 440 small requests),
  2. works out which feed group matches each of our sets (by voting on matching cards),
  3. matches our cards to products inside that group,
  4. stores the feed's prices per variant, OVERRIDING TCGdex's where a card matched.

Unmatched cards keep TCGdex's price. The feed refreshes daily, so prices can be about a day old.
"""
from __future__ import annotations

import asyncio
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable

import httpx

from .textnorm import local_key, name_key

USER_AGENT = "pokedeals/0.1 (personal price lookup)"      # tcgcsv blocks requests that do not identify themselves
BASE_URL = "https://tcgcsv.com"
POKEMON_CATEGORY = 3
Progress = Callable[[str], None]

# Feed "subTypeName" -> the variant keys TCGdex uses, so the matcher/UI treat both sources alike.
SUBTYPE_VARIANT = {
    "Normal": "normal",
    "Holofoil": "holofoil",
    "Reverse Holofoil": "reverse-holofoil",
    "1st Edition": "1st-edition",
    "1st Edition Holofoil": "1st-edition-holofoil",
    "Unlimited": "unlimited",
    "Unlimited Holofoil": "unlimited-holofoil",
}


# ---- feed model ----

@dataclass(frozen=True)
class FeedPrice:
    variant: str
    market: float | None
    low: float | None
    mid: float | None
    high: float | None
    direct_low: float | None


@dataclass
class Product:
    product_id: int
    group_id: int
    name: str
    loose: str                  # name minus number suffix and "(...)" qualifiers, normalised
    qualified: bool             # the name carried a qualifier like "(Staff)" or "(Delta Species)"
    num_key: str                # printed card number normalised like local_key: "21", "TG1", "XY1"
    denom: int | None           # set size from "021/128"; None for promos ("XY01", "002")
    prices: list[FeedPrice] = field(default_factory=list)

    @property
    def best_market(self) -> float | None:
        marks = [p.market for p in self.prices if p.market]
        return max(marks) if marks else None


@dataclass
class Group:
    group_id: int
    name: str
    products: list[Product]


@dataclass
class Feed:
    groups: list[Group]
    updated: str                # when the feed says it was generated (ISO), or when we fetched it


_NUMBER = re.compile(r"^\s*([A-Za-z]*)\s*0*(\d+)([A-Za-z]?)\s*(?:/\s*([A-Za-z]*)\s*0*(\d+))?\s*$")
_NUM_SUFFIX = re.compile(r"\s+-\s+[A-Za-z]{0,6}\d+(?:\s*/\s*[A-Za-z]{0,6}\d+)?\s*$")
_QUALIFIER = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
_NOT_A_CARD = re.compile(r"code card|jumbo|oversize", re.I)


def parse_number(raw: str | None) -> tuple[str, int | None] | None:
    """'021/128' -> ('21', 128); 'TG01/TG30' -> ('TG1', 30); 'XY01' -> ('XY1', None);
    '43a/124' -> ('43A', 124) (alternate arts); junk -> None."""
    m = _NUMBER.match(raw or "")
    if not m:
        return None
    prefix, digits, suffix, _, denom = m.groups()
    key = f"{prefix.upper()}{int(digits)}{suffix.upper()}"
    return key, (int(denom) if denom else None)


def loose_name(name: str) -> tuple[str, bool]:
    """Comparable form of a card or product name, and whether it had a '(...)' qualifier.

    'Altaria ex (Delta Species)' and TCGdex's 'Altaria ex δ' both become 'altaria ex'.
    """
    unqualified = _QUALIFIER.sub("", name).strip()          # "Pikachu - 025/165 (Staff)" -> "Pikachu - 025/165"
    return name_key(_NUM_SUFFIX.sub("", unqualified)), unqualified != name.strip()


def parse_group(group: dict, products: list[dict], prices: list[dict]) -> Group:
    by_product: dict[int, list[FeedPrice]] = defaultdict(list)
    for p in prices:
        sub = p.get("subTypeName") or "Normal"
        by_product[p["productId"]].append(FeedPrice(
            SUBTYPE_VARIANT.get(sub, name_key(sub).replace(" ", "-")),
            p.get("marketPrice"), p.get("lowPrice"), p.get("midPrice"), p.get("highPrice"),
            p.get("directLowPrice")))
    out: list[Product] = []
    for pr in products:
        ext = {e.get("name"): e.get("value") for e in pr.get("extendedData") or []}
        number = parse_number(ext.get("Number"))
        if number is None or _NOT_A_CARD.search(pr["name"]):
            continue                      # sealed product or code card
        # Unpriced cards are kept (prices == []): they still claim their number in the group, so a lookalike
        # group (Base Set vs Base Set (Shadowless)) cannot fill the gap with a different print.
        loose, qualified = loose_name(pr["name"])
        out.append(Product(pr["productId"], group["groupId"], pr["name"], loose, qualified,
                           number[0], number[1], by_product.get(pr["productId"], [])))
    return Group(group["groupId"], group["name"], out)


# ---- download ----

async def fetch_feed(
    client: httpx.AsyncClient | None = None, *, concurrency: int = 6, progress: Progress | None = None
) -> Feed:
    """Download every Pokémon group. A group that keeps failing is skipped (and reported), not fatal."""
    client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=60, follow_redirects=True,
                                         headers={"User-Agent": USER_AGENT})
    sem = asyncio.Semaphore(concurrency)
    say = progress or (lambda m: None)

    async with client:
        async def get(path: str, attempts: int = 4):
            async with sem:
                for attempt in range(attempts):
                    try:
                        r = await client.get(path)
                        r.raise_for_status()
                        return r
                    except httpx.HTTPError:
                        if attempt == attempts - 1:
                            raise
                        await asyncio.sleep(0.5 * 2 ** attempt)

        base = f"/tcgplayer/{POKEMON_CATEGORY}"
        groups = (await get(f"{base}/groups")).json().get("results", [])

        try:
            stamp = (await get("/last-updated.txt")).text.strip()
            updated = stamp if stamp else datetime.now(timezone.utc).isoformat()
        except httpx.HTTPError:
            updated = datetime.now(timezone.utc).isoformat()

        failed: list[str] = []

        async def one(g: dict) -> Group | None:
            try:
                products = (await get(f"{base}/{g['groupId']}/products")).json().get("results", [])
                prices = (await get(f"{base}/{g['groupId']}/prices")).json().get("results", [])
            except httpx.HTTPError:
                failed.append(g["name"])
                return None
            return parse_group(g, products, prices)

        parsed = [g for g in await asyncio.gather(*(one(g) for g in groups)) if g is not None]
    if failed:
        say(f"warning: could not fetch {len(failed)} price groups: {', '.join(failed[:5])}")
    return Feed(parsed, updated)


# ---- matching ----

@dataclass(frozen=True)
class CardRow:
    id: str
    set_id: str
    name: str
    local_key: str
    official_count: int | None
    set_key: str = ""           # normalised set name, used to tell sibling half-decks apart


@dataclass(frozen=True)
class CardMatch:
    card_id: str
    product: Product
    group_id: int
    method: str                 # "number+name" | "number" | "name"
    # Base Set only: TCGplayer lists the 1st Editions (all Shadowless) and the Shadowless print in a separate
    # "Base Set (Shadowless)" group. Its rows become this card's 1st-edition-* and shadowless-* variants,
    # never mixed into the ordinary print.
    stamped: Product | None = None


class _GroupIndex:
    def __init__(self, group: Group):
        self.by_num_name: dict[tuple[str, str], list[Product]] = defaultdict(list)
        self.by_num: dict[str, list[Product]] = defaultdict(list)
        self.by_name: dict[str, list[Product]] = defaultdict(list)
        for p in group.products:
            self.by_num_name[(p.loose, p.num_key)].append(p)
            self.by_num[p.num_key].append(p)
            self.by_name[p.loose].append(p)


def _similar(a: str, b: str) -> bool:
    if a == b or (a and b and (a in b or b in a)):
        return True
    ta, tb = set(a.split()), set(b.split())
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.5


_QUALIFIER_TEXT = re.compile(r"[\(\[]([^)\]]*)[\)\]]")


def _qualifier_rank(name: str) -> tuple[bool, int]:
    """(is it a Staff stamp, how many '(...)'/'[...]' qualifiers). A Staff print is never the ordinary card;
    among the rest, fewer qualifiers is more likely the plain print:
    'Manectric - SM130 (Prerelease)' beats '... (Prerelease) [Staff]'."""
    quals = _QUALIFIER_TEXT.findall(name)
    return any("staff" in q.lower() for q in quals), len(quals)


def _names_our_set(product: Product, card: CardRow) -> bool:
    """A qualifier that matches our set's name picks the right deck: 'Potion (Latios)' belongs to the
    set 'EX trainer Kit (Latios)', not the sibling 'EX trainer Kit (Latias)'."""
    return any(q and f" {name_key(q)} " in f" {card.set_key} " for q in _QUALIFIER_TEXT.findall(product.name))


def _pick(cands: list[Product], card: CardRow) -> Product:
    """Several products share a number+name (stamped/promo variants, half-decks): prefer one whose qualifier
    names our set, then the matching set size, then the fewest qualifiers (never a Staff stamp over a plain
    one), then the shortest name, then the earliest."""
    return min(cands, key=lambda p: (not _names_our_set(p, card),
                                     p.denom is not None and p.denom != card.official_count,
                                     *_qualifier_rank(p.name), len(p.name), p.product_id))


def _name_score(group_name: str, set_key: str) -> int:
    """How closely a feed group's name matches our set's name (3 exact ... 0 unrelated), to prefer
    'Base Set' over 'Base Set (Shadowless)'."""
    g = name_key(group_name.split(":", 1)[-1])      # "SWSH01: Sword & Shield Base Set" -> the part after
    if not g or not set_key:
        return 0
    if g == set_key:
        return 3
    if g.startswith(set_key) or g.endswith(set_key) or set_key.startswith(g) or set_key.endswith(g):
        return 2
    gt, st = set(g.split()), set(set_key.split())
    return 1 if len(gt & st) / len(gt | st) >= 0.6 else 0


def match_cards(cards: list[CardRow], feed: Feed) -> tuple[list[CardMatch], dict[str, list[int]]]:
    """Match our cards to feed products. Returns (matches, set_id -> the feed group ids used)."""
    index = {g.group_id: _GroupIndex(g) for g in feed.groups}
    group_names = {g.group_id: g.name for g in feed.groups}
    shadowless_of: dict[str, int] = {}       # normalised "Base Set" -> id of the "Base Set (Shadowless)" group
    for g in feed.groups:
        m = re.match(r"^(.*?)\s*\(shadowless\)\s*$", g.name, re.I)
        if m:
            shadowless_of[name_key(m.group(1))] = g.group_id

    def stamped_sibling(gid: int, loose_: str, card: CardRow) -> Product | None:
        sid = shadowless_of.get(name_key(group_names[gid]))
        if sid is None:
            return None
        cands = index[sid].by_num_name.get((loose_, card.local_key))
        return _pick(cands, card) if cands else None
    owners_num_name: dict[tuple[str, str], set[int]] = defaultdict(set)
    owners_name: dict[str, set[int]] = defaultdict(set)      # only names unique within their group
    for gid, gi in index.items():
        for key in gi.by_num_name:
            owners_num_name[key].add(gid)
        for name, prods in gi.by_name.items():
            if len(prods) == 1:
                owners_name[name].add(gid)

    cards_by_set: dict[str, list[CardRow]] = defaultdict(list)
    for c in cards:
        cards_by_set[c.set_id].append(c)

    matches: list[CardMatch] = []
    set_groups: dict[str, list[int]] = {}
    for set_id, set_cards in cards_by_set.items():
        n = len(set_cards)
        loose = {c.id: loose_name(c.name)[0] for c in set_cards}

        # Which feed group(s) is this set? Pick the group covering the most cards, then add groups only while
        # they cover cards the chosen ones do not (a Radiant Collection in a second group is picked up; one stray
        # reprint adds nothing and is ignored).
        set_key = set_cards[0].set_key

        def choose(cover: dict[int, set[str]], need_first: int, need_more: int) -> list[int]:
            chosen: list[int] = []
            covered: set[str] = set()
            while True:
                gains = [(g, cards_ - covered) for g, cards_ in cover.items() if g not in chosen]
                if not gains:
                    return chosen
                best = max(len(gained) for _, gained in gains)
                # Groups covering nearly as much are interchangeable by coverage: let the name decide.
                near = [t for t in gains if len(t[1]) >= 0.8 * best]
                gid, gained = max(near, key=lambda t: (_name_score(group_names[t[0]], set_key), len(t[1]), -t[0]))
                if len(gained) < (need_first if not chosen else need_more):
                    return chosen
                chosen.append(gid)
                covered |= gained

        by_number: dict[int, set[str]] = defaultdict(set)
        for c in set_cards:
            for gid in owners_num_name.get((loose[c.id], c.local_key), ()):
                by_number[gid].add(c.id)
        tenth = max(2, math.ceil(0.10 * n)) if n > 3 else 1
        groups = choose(by_number, tenth, tenth)
        if not groups:
            # No numbered evidence (Classic Collections reuse the original print's number): unique names.
            by_name: dict[int, set[str]] = defaultdict(set)
            for c in set_cards:
                for gid in owners_name.get(loose[c.id], ()):
                    by_name[gid].add(c.id)
            half = max(2, math.ceil(0.5 * n)) if n > 3 else 1
            groups = choose(by_name, half, half)[:2]
        if not groups:
            continue
        set_groups[set_id] = groups

        name_counts = Counter(loose.values())
        for c in set_cards:
            lk = loose[c.id]
            found: CardMatch | None = None
            for gid in groups:
                gi = index[gid]
                if cands := gi.by_num_name.get((lk, c.local_key)):
                    found = CardMatch(c.id, _pick(cands, c), gid, "number+name")
                elif (cands := gi.by_num.get(c.local_key)) and len(cands) == 1 and _similar(lk, cands[0].loose):
                    found = CardMatch(c.id, cands[0], gid, "number")
                elif (cands := gi.by_name.get(lk)) and len(cands) == 1 and name_counts[lk] == 1:
                    found = CardMatch(c.id, cands[0], gid, "name")
                if found:
                    sibling = stamped_sibling(gid, lk, c)
                    if sibling is not None:
                        found = CardMatch(found.card_id, found.product, found.group_id, found.method, sibling)
                    matches.append(found)
                    break
    return matches, set_groups


# ---- storing ----

@dataclass
class Report:
    groups: int = 0
    sets_mapped: int = 0
    sets_total: int = 0
    matched: int = 0
    by_method: Counter = field(default_factory=Counter)
    filled: int = 0             # had no market price before, has one now
    changed: int = 0            # had one that differed from the feed by more than 3%
    same: int = 0               # had one that agrees with the feed (within 3%)
    no_market: int = 0          # matched a product with no market price: left alone
    stamped_added: int = 0      # got a 1st Edition price from the Shadowless sibling group
    unpriced_after: int = 0
    updated: str = ""


def apply_matches(conn: sqlite3.Connection, matches: list[CardMatch], updated: str) -> Report:
    """Replace a matched card's prices with the feed's. The feed is authoritative: both sources describe the
    same TCGplayer product, so a variant only TCGdex has is stale or misnamed, and keeping it would show two
    prices for one print. Unmatched cards, and ones whose product has no market price, are left as they were."""
    report = Report(matched=len(matches), updated=updated)
    old: dict[str, float] = {
        r[0]: r[1] for r in conn.execute(
            "SELECT card_id, max(market) FROM card_prices WHERE market > 0 GROUP BY card_id")}

    for m in matches:
        report.by_method[m.method] += 1
        rows = [(p, m.product.product_id) for p in m.product.prices if p.market and p.market > 0]
        if not rows:
            report.no_market += 1
            continue
        if m.stamped is not None:
            added = []
            for p in m.stamped.prices:
                if not (p.market and p.market > 0):
                    continue
                if p.variant.startswith("1st-edition"):
                    if not any(q.variant.startswith("1st-edition") for q, _ in rows):
                        added.append((p, m.stamped.product_id))               # 1st Edition: always a Shadowless print
                else:
                    # The Shadowless group's "Unlimited" rows are the Shadowless print itself (a much pricier card), so keep
                    # them apart under their own name.
                    finish = "holofoil" if p.variant.endswith("holofoil") else "normal"
                    added.append((replace(p, variant=f"shadowless-{finish}"), m.stamped.product_id))
            rows += added
            report.stamped_added += bool(added)
        conn.execute("DELETE FROM card_prices WHERE card_id = ?", (m.card_id,))   # also makes re-runs idempotent
        conn.executemany(
            "INSERT INTO card_prices (card_id, variant, market, low, mid, high, direct_low, currency, updated,"
            " source, product_id) VALUES (?,?,?,?,?,?,?,'USD',?,'tcgcsv',?)",
            [(m.card_id, p.variant, p.market, p.low, p.mid, p.high, p.direct_low, updated, pid)
             for p, pid in rows])
        conn.execute("UPDATE cards SET prices_updated = ? WHERE id = ?", (updated, m.card_id))

        before, after = old.get(m.card_id), max(p.market for p, _ in rows)
        if before is None:
            report.filled += 1
        elif abs(after - before) > 0.03 * before:
            report.changed += 1
        else:
            report.same += 1
    conn.commit()
    return report


async def refresh_prices(
    conn: sqlite3.Connection,
    *,
    client: httpx.AsyncClient | None = None,
    feed: Feed | None = None,
    progress: Progress = print,
) -> Report:
    """Fetch the feed, match it to our cards, and write its prices over TCGdex's."""
    feed = feed or await fetch_feed(client, progress=progress)
    cards = [CardRow(r["id"], r["set_id"], r["name"], r["local_key"], r["official_count"], r["set_key"])
             for r in conn.execute(
                 "SELECT c.id, c.set_id, c.name, c.local_key, s.official_count, s.name_key AS set_key "
                 "FROM cards c JOIN sets s ON s.id = c.set_id")]
    matches, set_groups = match_cards(cards, feed)
    report = apply_matches(conn, matches, feed.updated)
    report.groups = len(feed.groups)
    report.sets_total = len({c.set_id for c in cards})
    report.sets_mapped = len(set_groups)
    report.unpriced_after = conn.execute(
        "SELECT count(*) FROM cards c WHERE NOT EXISTS "
        "(SELECT 1 FROM card_prices p WHERE p.card_id = c.id AND p.market > 0)").fetchone()[0]
    return report
