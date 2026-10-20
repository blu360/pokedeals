import pytest

from pokedeals.parser import parse_title
from pokedeals.textnorm import local_key, name_key


@pytest.mark.parametrize("title,key,denom", [
    ("Charizard 4/102 Base Set Holo", "4", 102),
    ("Charizard 004/102", "4", 102),
    ("Furret 136 / 189 Darkness Ablaze", "136", 189),
    ("Charizard VMAX TG03/TG30 Trainer Gallery", "TG3", 30),
    ("Pikachu SV001/SV122", "SV1", 122),
    ("Secret Rare Charizard 201/189", "201", 189),
])
def test_number_extraction(title, key, denom):
    p = parse_title(title)
    assert (p.number_key, p.denominator) == (key, denom)
    assert not p.skip


@pytest.mark.parametrize("title,reason", [
    ("Charizard 4/102 PSA 10 GEM MINT", "graded"),
    ("Charizard 4/102 BGS 9.5", "graded"),
    ("Charizard 4/102 CGC graded", "graded"),
    ("Charizard 4/102 Proxy custom card", "junk/proxy"),
    ("Charizard 4/102 Japanese", "non-english"),
    ("Pokemon lot of 50 cards 4/102", "lot/multiple"),
    ("Charizard 4/102 Pikachu 58/102", "lot/multiple"),
    ("4x Charizard 4/102", "lot/multiple"),
])
def test_skip_reasons(title, reason):
    assert reason in parse_title(title).reasons


def test_clean_listing_not_skipped():
    assert not parse_title("Charizard 4/102 Base Set Holo Near Mint").skip


def test_hp_stat_is_not_heavily_played():
    assert parse_title("Charizard V 19/189 120 HP Darkness Ablaze").condition is None
    assert parse_title("Charizard V 19/189 HP 220 Darkness Ablaze").condition is None
    assert parse_title("Charizard 4/102 HP condition").condition == "HP"
    assert parse_title("Charizard 4/102 Base Set heavily played").condition == "HP"


@pytest.mark.parametrize("title,cond", [
    ("Furret 136/189 NM", "NM"),
    ("Furret 136/189 Near Mint", "NM"),
    ("Furret 136/189 Lightly Played", "LP"),
    ("Furret 136/189 moderately played", "MP"),
    ("Furret 136/189 damaged", "DMG"),
    ("Furret 136/189", None),
])
def test_condition(title, cond):
    assert parse_title(title).condition == cond


@pytest.mark.parametrize("title,first,finish", [
    ("Charizard 4/102 1st Edition Holo", True, "holo"),
    ("Furret 136/189 Reverse Holo", False, "reverse"),
    ("Furret 136/189 non-holo", False, "non-holo"),
    ("Charizard 4/102 Holo Rare", False, "holo"),
    ("Furret 136/189", False, None),
])
def test_variant_hints(title, first, finish):
    p = parse_title(title)
    assert (p.first_edition, p.finish) == (first, finish)


def test_normalisers():
    assert name_key("Pokémon  Mr. Mime") == "pokemon mr mime"
    assert name_key("Farfetch’d") == "farfetch d"
    assert local_key("004") == "4"
    assert local_key("TG01") == "TG1"
    assert local_key("swsh001") == "SWSH1"


import pytest as _pytest


@_pytest.mark.parametrize("title", [
    "Venusaur 3/102 Reverse Holo Italiano", "Venusaur 3/102 Reverse Holo ITA", "Venusaur 3/102 Francais Reverse",
    "Venusaur 3/102 Deutsch Reverse Holo", "Venusaur 3/102 Espa\u00f1ol Holo", "Venusaur 3/102 Portugues Holo",
])
def test_titles_naming_another_language_are_skipped(title):
    assert "non-english" in parse_title(title).reasons


def test_english_titles_are_not_mistaken_for_another_language():
    assert "non-english" not in parse_title("Venusaur 3/102 Base Set Reverse Holo English NM").reasons
