"""Optional extra price sources, for data the free sources do not have (graded cards first).

The core app needs no paid service: card prices come from TCGdex and the free tcgcsv.com feed, and per-condition
prices from TCGplayer's own site. A *graded price source* adds what a PSA/BGS/CGC slab is worth, from a service you
have your own key for (Pokémon Price Tracker, PriceCharting's API, ...). To add one:

1. write a class with `name`, `available()` and `graded_prices(card)` (see `GradedPriceSource`);
2. call `register("my-source", MySource)` where the module is imported;
3. list its name in `Settings.price_sources`, and put its key in the environment or keyring (credentials.secret).

A source that fails must raise `SourceError` or return nothing: the app then carries on without it and the Status
page shows it as having trouble.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


class SourceError(Exception):
    """The source could not answer (bad key, down, rate limited). Worded for the user; never contains a key."""


@dataclass(frozen=True)
class GradedPrice:
    grader: str              # "PSA", "BGS", "CGC", ...
    grade: str               # "10", "9.5", ...
    price: float             # USD
    sales: int | None = None            # how many sales it is based on, when the source says
    as_of: str | None = None            # ISO date of the newest sale or of the source's refresh


class GradedPriceSource(Protocol):
    name: str

    def available(self) -> bool:
        """True when the source is configured (has its key) and not backing off."""

    def graded_prices(self, card) -> list[GradedPrice]:
        """Prices for this card (a `catalog` card record: id, name, set, number...). [] when the source has none."""


_REGISTRY: dict[str, Callable[[], GradedPriceSource]] = {}


def register(name: str, factory: Callable[[], GradedPriceSource]) -> None:
    _REGISTRY[name] = factory


def registered() -> list[str]:
    return sorted(_REGISTRY)


def enabled_sources(names: tuple[str, ...]) -> list[GradedPriceSource]:
    """The configured sources that exist and are ready; unknown or unavailable names are skipped, never fatal."""
    out = []
    for n in names:
        factory = _REGISTRY.get(n)
        if factory is None:
            continue
        try:
            source = factory()
            if source.available():
                out.append(source)
        except Exception:                      # noqa: BLE001 - a broken plug-in must not stop the app
            continue
    return out


def graded_price(sources: list[GradedPriceSource], card, grader: str, grade: str) -> GradedPrice | None:
    """The first source that has a price for this grader and grade, or None."""
    for source in sources:
        try:
            for p in source.graded_prices(card):
                if p.grader.upper() == grader.upper() and p.grade == grade:
                    return p
        except SourceError:
            continue
    return None
