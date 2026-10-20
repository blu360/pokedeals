"""Map a ParsedTitle onto a canonical card (and the right price variant) from the local DB."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .parser import ParsedTitle


@dataclass(frozen=True)
class CardRef:
    id: str
    name: str
    set_id: str
    set_name: str
    local_id: str
    official_count: int | None
    rarity: str | None
    image: str | None
    prices: dict[str, float | None]   # TCGplayer market price per variant
    product_id: int | None = None     # TCGplayer product page of the matched variant, when known


@dataclass(frozen=True)
class MatchResult:
    card: CardRef | None
    confidence: float = 0.0
    variant: str | None = None
    market: float | None = None       # TCGplayer market price (USD, Near Mint) for `variant`
    flags: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None         # why there is no match

    @property
    def ok(self) -> bool:
        return self.card is not None and self.market is not None


def choose_price(
    prices: dict[str, float | None], first_edition: bool, finish: str | None, shadowless: bool = False
) -> tuple[str | None, float | None, list[str]]:
    """Pick which TCGplayer variant a listing most likely is, conservatively (lowest if unsure).

    Variant keys: normal, holofoil, reverse-holofoil, unlimited(-holofoil), 1st-edition(-holofoil), and
    shadowless-holofoil / shadowless-normal (Base Set's early print, priced separately and far higher).
    A listing only gets a 1st Edition or Shadowless price if its title says so; otherwise those prints are
    left out, so a regular card is never valued at a rarer print's price (or the reverse).
    """
    pool = {k: v for k, v in prices.items() if v is not None and v > 0}
    flags: list[str] = []
    if not pool:
        return None, None, ["no-price"]

    def is_reverse(k: str) -> bool:
        return k == "reverse-holofoil"

    def is_holo(k: str) -> bool:
        return k.endswith("holofoil") and not is_reverse(k)

    # edition: WOTC-era cards have 1st-edition / shadowless / unlimited prints priced separately
    first = {k: v for k, v in pool.items() if k.startswith("1st-edition")}
    shadow = {k: v for k, v in pool.items() if k.startswith("shadowless")}
    rest = {k: v for k, v in pool.items() if k not in first and k not in shadow}   # the regular prints
    if first_edition:
        if first:
            pool = first
        else:
            flags.append("first-edition-price-missing")
            pool = shadow if (shadowless and shadow) else (rest or pool)
    elif shadowless:
        if shadow:
            pool = shadow
        else:
            flags.append("shadowless-price-missing")
            pool = rest or pool
    else:
        pool = rest or pool

    # finish
    wanted = {
        "reverse": is_reverse,
        "holo": is_holo,
        "non-holo": lambda k: not is_reverse(k) and not is_holo(k),
        None: lambda k: not is_reverse(k),
    }[finish]
    narrowed = {k: v for k, v in pool.items() if wanted(k)}
    if narrowed:
        pool = narrowed
    elif finish is not None:
        flags.append("finish-unavailable")
    # (finish None and only reverse exists -> keep pool as is)

    variant = min(pool, key=pool.__getitem__)
    if len(pool) > 1:
        flags.append("variant-assumed")
    return variant, pool[variant], flags


# What sellers (and TCGplayer, in "[30C - 4/102]") write instead of the set's name.
_REPRINT_ALIASES = {"30c": "30th", "ccc": "celebration", "25th": "celebration"}


def _name_hit(name_key: str, padded_title: str) -> str | None:
    """Does a card's name appear in the title? 'exact', 'fuzzy', or None.

    Real titles write 'Mega-Gengar-EX' for the card 'M Gengar EX' and glue 'DeoxysEX' together, so a card
    named 'M ...' also answers to 'Mega ...', and a second pass ignores spacing altogether (5+ letters, so
    short names like 'Abra' cannot match inside other words).
    """
    variants = {name_key}
    if name_key.startswith("m "):
        variants.add("mega " + name_key[2:])
    if any(f" {v} " in padded_title for v in variants):
        return "exact"
    compact_title = padded_title.replace(" ", "")
    if any(len(c) >= 5 and c in compact_title for c in (v.replace(" ", "") for v in variants)):
        return "fuzzy"
    return None


def _stem(token: str) -> str:
    """'celebrations' -> 'celebration' (titles say Celebrations, set names say Celebration)."""
    return token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") else token


def _tokens(normalised: str) -> set[str]:
    return {_stem(t) for t in normalised.split()}


class Matcher:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._reprints: list[dict] | None = None      # Classic Collection sets, loaded on first use

    def _candidates(self, number_key: str, denominator: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT c.id, c.name, c.name_key, c.set_id, c.local_id, c.rarity, c.image,
                      s.name AS set_name, s.name_key AS set_key, s.official_count
               FROM cards c JOIN sets s ON s.id = c.set_id
               WHERE c.local_key = ? AND s.official_count = ?""",
            (number_key, denominator),
        ).fetchall()

    def product_id(self, card_id: str, variant: str) -> int | None:
        """TCGplayer's product id behind a card's variant price (None for TCGdex-only prices)."""
        r = self.conn.execute("SELECT product_id FROM card_prices WHERE card_id = ? AND variant = ?",
                              (card_id, variant)).fetchone()
        return r["product_id"] if r and r["product_id"] else None

    def _prices(self, card_id: str) -> dict[str, float | None]:
        rows = self.conn.execute(
            "SELECT variant, market FROM card_prices WHERE card_id = ?", (card_id,)
        ).fetchall()
        return {r["variant"]: r["market"] for r in rows}

    # ---- reprint collections --------------------------------------------------------------------
    # "Classic Collection" sets (Celebrations, 30th Celebration) reprint old cards under their ORIGINAL
    # number, so a listing titled "Charizard 4/102 ... 30th Anniversary Celebrations" is not Base Set's
    # Charizard (~$950) but the reprint (~$135). Number and set size alone cannot tell them apart; the
    # words in the title can, and the reprint's own cards are found by name.

    def _reprint_sets(self) -> list[dict]:
        if self._reprints is None:
            self._reprints = []
            for r in self.conn.execute(
                    "SELECT id, name, name_key, release_date FROM sets WHERE name_key LIKE '%classic collection%'"):
                trigger = _tokens(r["name_key"]) - {"classic", "collection"}   # {"30th"}, {"celebration"}
                if trigger:
                    self._reprints.append({"id": r["id"], "name": r["name"], "trigger": trigger,
                                           "release": r["release_date"] or ""})
        return self._reprints

    def _cards_in_set(self, set_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT c.id, c.name, c.name_key, c.set_id, c.local_id, c.rarity, c.image,
                      s.name AS set_name, s.name_key AS set_key, s.official_count
               FROM cards c JOIN sets s ON s.id = c.set_id WHERE c.set_id = ?""", (set_id,)).fetchall()

    def _match_reprint(self, p: ParsedTitle, rows: list[sqlite3.Row]) -> MatchResult | None:
        """A MatchResult when the title names a Classic Collection reprint, else None (normal matching)."""
        sets = self._reprint_sets()
        toks = _tokens(p.norm) | {_REPRINT_ALIASES[t] for t in _tokens(p.norm) if t in _REPRINT_ALIASES}
        hits = [s for s in sets if s["trigger"] <= toks]
        explicit = bool(hits)                      # the title says WHICH reprint set (30th, Celebrations)
        if not hits and {"classic", "collection"} <= toks:
            hits = list(sets)                      # "Classic Collection" alone: any of them
        if not hits:
            return None

        # A main set of the same family that the printed number points at wins ("Pikachu 5/25 Celebrations"
        # is the Celebrations set itself, not a reprint).
        family = set().union(*(s["trigger"] for s in hits))
        reprint_ids = {s["id"] for s in sets}
        if any(r["set_id"] not in reprint_ids and _tokens(r["set_key"]) & family for r in rows):
            return None

        # 30th/25th are the strongest identifiers, then the more specific name, then the newest set
        hits.sort(key=lambda s: (any(ch.isdigit() for t in s["trigger"] for ch in t), len(s["trigger"]), s["release"]),
                  reverse=True)
        padded = f" {p.norm} "
        found: list[tuple[dict, list[sqlite3.Row]]] = []          # (reprint set, its cards named in the title)
        for s in hits:
            named = [c for c in self._cards_in_set(s["id"]) if _name_hit(c["name_key"], padded)]
            if named:
                longest = max(len(c["name_key"]) for c in named)  # "Pikachu & Zekrom GX" beats "Pikachu"
                found.append((s, [c for c in named if len(c["name_key"]) == longest]))
                if explicit:
                    break                                          # the title picked the set: first one that has it
        if not found:
            return MatchResult(None, reason="title names " + " / ".join(s["name"] for s in hits)
                                            + " but no card with that name is in it")
        best = found[0][1]
        flags = ["reprint-set"]
        confidence = 0.85
        if len(best) > 1:
            flags.append("ambiguous-card")
            confidence = 0.55
        if len(found) > 1:                                         # "Classic Collection" alone, card in several
            flags.append("ambiguous-set")
            confidence = 0.55
        return self._finalize(best[0], p, flags, confidence)

    def match(self, p: ParsedTitle) -> MatchResult:
        have_number = p.number_key is not None and p.denominator is not None
        rows = self._candidates(p.number_key, p.denominator) if have_number else []

        reprint = self._match_reprint(p, rows)
        if reprint is not None:
            return reprint

        if not have_number:
            return MatchResult(None, reason="no single card number (e.g. 136/189) in title")
        if not rows:
            return MatchResult(None, reason=f"no card #{p.number_key} in any set of {p.denominator}")

        padded = f" {p.norm} "
        hits = {r["id"]: _name_hit(r["name_key"], padded) for r in rows}
        named = [r for r in rows if hits[r["id"]]]
        pool = named or rows
        flags: list[str] = []
        if not named:
            confidence = 0.50
            flags.append("name-not-in-title")
        elif all(hits[r["id"]] == "fuzzy" for r in named):
            confidence = 0.85                  # found only once spacing is ignored ("DeoxysEX")
            flags.append("name-fuzzy")
        else:
            confidence = 0.90

        if len(pool) > 1:  # same number + set size in several sets: use set name as tie-break
            by_set = [r for r in pool if f" {r['set_key']} " in padded]
            if 0 < len(by_set) < len(pool):
                pool, confidence = by_set, min(confidence + 0.05, 1.0)
            else:
                flags.append("ambiguous-set")
                confidence = min(confidence, 0.55)
        elif f" {pool[0]['set_key']} " in padded:
            confidence = min(confidence + 0.05, 1.0)

        # deterministic + conservative: among remaining ties use the cheapest print
        picks = [(r, self._prices(r["id"])) for r in pool]
        best_row, best_prices = min(
            picks, key=lambda rp: min((v for v in rp[1].values() if v), default=float("inf"))
        )
        return self._finalize(best_row, p, flags, confidence, best_prices)

    def _finalize(self, row: sqlite3.Row, p: ParsedTitle, flags: list[str], confidence: float,
                  prices: dict[str, float | None] | None = None) -> MatchResult:
        """Turn a chosen card row into a priced MatchResult (variant from the title's hints)."""
        prices = self._prices(row["id"]) if prices is None else prices
        variant, market, price_flags = choose_price(prices, p.first_edition, p.finish, p.shadowless)
        flags = [*flags, *price_flags]
        if "variant-assumed" in price_flags:
            confidence -= 0.05
        if {"finish-unavailable", "first-edition-price-missing", "shadowless-price-missing"} & set(price_flags):
            confidence -= 0.10

        card = CardRef(
            id=row["id"], name=row["name"], set_id=row["set_id"], set_name=row["set_name"],
            local_id=row["local_id"], official_count=row["official_count"], rarity=row["rarity"],
            image=row["image"], prices=prices, product_id=self.product_id(row["id"], variant) if variant else None,
        )
        return MatchResult(
            card, round(confidence, 2), variant, market, tuple(flags),
            reason=None if market is not None else "card has no TCGplayer price",
        )
