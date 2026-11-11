"""Turn a messy eBay title into structured fields. Pure functions: no DB, no network.

The printed card number ("136/189") is the strongest signal: with the set size it pins down a card
far better than the name. The rest are filters (graded, lot, proxy, non-English) or price modifiers
(finish, condition).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textnorm import local_key, name_key

# "4/102", "004/102", "TG01/TG30", "SV001/SV122", "201/189"
_NUMBER = re.compile(
    r"(?<![\w/])([A-Za-z]{0,4})0*(\d{1,3})\s*/\s*([A-Za-z]{0,4})0*(\d{1,3})(?![\w/])"
)

_GRADED = re.compile(r"\b(psa|bgs|cgc|sgc|beckett|graded|slab(?:bed)?)\b", re.I)

# NOTE: "collection" is left out on purpose: it is part of real set names ("Classic Collection").
# Quantity markers ("2x", "x10") must be attached, or "Regigigas LV.X 100/100" reads as "x100".
_LOT = re.compile(
    r"\b(lot|lots|bundle|bulk|playset|binder|complete set|master set|set of|"
    r"mystery|repack|[2-9]x|\d{2,}x|x[2-9]|x\d{2,}|cards? lot)\b", re.I
)

# Prints that share a regular card's name and number but are priced differently (stamped promos,
# misprints). We have no price for them, so they are skipped.
_OTHER_PRINT = re.compile(
    r"\b(world\s+champ(?:ionships?|s)?(?:\s+decks?)?|wcd|wcs|staff|pre-?release|stamped|pok[eé]mon cent(?:er|re)|"
    r"build\s*(?:&|and)\s*battle|cosmos(?:\s+holo)?|cracked\s+ice|mis-?print|miscut|error)\b", re.I
)

_JUNK = re.compile(
    r"\b(proxy|proxies|custom|reprint|replica|fan ?art|orica|digital|code card|online code|"
    r"ptcgo|ptcgl|metal|gold card|fake|unofficial|counterfeit|novelty|sticker|inspired|tribute|"
    r"unlicensed|handmade|art card|artcard|fantasy|acrylic|magnet|lenticular|"
    r"oversized|jumbo|pin|poster|sleeve|sleeves|empty|box only|case only|"
    r"ultra premium collection|not (?:an? )?(?:official|authentic|genuine))\b", re.I
)

_NON_ENGLISH = re.compile(
    r"\b(japanese|japan|jpn|jp|korean|kor|chinese|german|french|italian|spanish|portuguese|"
    r"dutch|thai|indonesian|italiano|ita|francais|français|deutsch|espanol|español|portugues|polish|polski|"
    r"russian|turkish|taiwan|taiwanese|brazilian|latin)\b", re.I
)

_FIRST_ED = re.compile(r"\b(1st|first)\s*(edition|ed)\b|\b1st\b", re.I)
# Base Set's early print (no drop shadow); TCGplayer prices it separately and far higher
_SHADOWLESS = re.compile(r"(?<!non-)(?<!non )(?<!not )(?<!non)\bshadow[\s-]*less\b", re.I)
_REVERSE = re.compile(r"\b(reverse|rev)\s*(holo(foil)?)?\b", re.I)
_NON_HOLO = re.compile(r"\bnon[\s-]?holo(foil)?\b", re.I)
_HOLO = re.compile(r"\bholo(foil)?\b", re.I)

_BARE_HP = re.compile(r"\bhp\b", re.I)


def _has_hp_condition(title: str) -> bool:
    """'HP' means heavily played, unless it is the hit-point stat ('120 HP' / 'HP 120').

    A number right before it is a stat, except a card number like '136/189 HP'.
    """
    for m in _BARE_HP.finditer(title):
        before, after = title[:m.start()], title[m.end():]
        if re.match(r"\s*\d", after):
            continue                                  # "HP 120"
        if re.search(r"\d\s*$", before) and not re.search(r"/\s*[A-Za-z]{0,4}\d+\s*$", before):
            continue                                  # "120 HP"
        return True
    return False


# first match wins, worst conditions first so hedges like "NM-LP" read pessimistically
_CONDITIONS: list[tuple[str, re.Pattern]] = [
    ("DMG", re.compile(r"\b(damaged|dmg|poor)\b", re.I)),
    ("HP", re.compile(r"\bheav(?:y|ily)[\s-]*play(?:ed)?\b", re.I)),
    ("MP", re.compile(r"\b(moderate(?:ly)?|mod)[\s-]*play(?:ed)?\b|\bmp\b", re.I)),
    ("LP", re.compile(r"\blight(?:ly)?[\s-]*play(?:ed)?\b|\blp\b|\bexcellent\b|\bplayed\b", re.I)),
    ("NM", re.compile(r"\bnear[\s-]*mint\b|\bnm\b|\bnm\s*[-/]\s*m\b|\bmint\b", re.I)),
]


@dataclass(frozen=True)
class ParsedTitle:
    raw: str
    norm: str                       # name_key(raw): what the matcher searches in
    number_key: str | None = None   # normalised card number, e.g. "136" / "TG1"
    denominator: int | None = None  # set size printed on card, e.g. 189
    n_numbers: int = 0              # distinct number tokens (>1 usually means a lot)
    first_edition: bool = False
    shadowless: bool = False        # "Shadowless" (Base Set's early print): a separate, pricier print
    finish: str | None = None       # "reverse" | "holo" | "non-holo" | None
    condition: str | None = None    # "NM" | "LP" | "MP" | "HP" | "DMG" | None
    reasons: tuple[str, ...] = field(default_factory=tuple)  # why this listing should be skipped

    @property
    def skip(self) -> bool:
        return bool(self.reasons)


def parse_title(title: str) -> ParsedTitle:
    reasons: list[str] = []
    if _GRADED.search(title):
        reasons.append("graded")
    if _JUNK.search(title):
        reasons.append("junk/proxy")
    if _NON_ENGLISH.search(title):
        reasons.append("non-english")
    if m := _OTHER_PRINT.search(title):
        reasons.append(f"different print ({m.group(1).lower()})")

    numbers = {
        (p1.upper() + str(int(d1)), int(d2))
        for p1, d1, _p2, d2 in _NUMBER.findall(title)
        if int(d1) > 0 and int(d2) > 0
    }
    if _LOT.search(title) or len(numbers) > 1:
        reasons.append("lot/multiple")

    number_key = denominator = None
    if len(numbers) == 1:
        number_key, denominator = next(iter(numbers))
        number_key = local_key(number_key)

    first_ed = bool(_FIRST_ED.search(title))
    shadowless = bool(_SHADOWLESS.search(title))
    finish = None
    if _NON_HOLO.search(title):
        finish = "non-holo"
    elif _REVERSE.search(title):
        finish = "reverse"
    elif _HOLO.search(title):
        finish = "holo"

    condition = next(
        (code for code, rx in _CONDITIONS
         if rx.search(title) or (code == "HP" and _has_hp_condition(title))),
        None,
    )

    return ParsedTitle(
        raw=title,
        norm=name_key(title),
        number_key=number_key,
        denominator=denominator,
        n_numbers=len(numbers),
        first_edition=first_ed,
        shadowless=shadowless,
        finish=finish,
        condition=condition,
        reasons=tuple(reasons),
    )
