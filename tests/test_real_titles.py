"""Title patterns seen in REAL eBay listings (paraphrased, not copied) that the first, synthetic tests missed."""
import pytest

from pokedeals.db import connect
from pokedeals.matcher import Matcher, _name_hit, _stem
from pokedeals.parser import parse_title
from pokedeals.textnorm import local_key, name_key


# ---- parser: lots ----

@pytest.mark.parametrize("title", [
    "Regigigas LV.X 100/100 Stormfront Holo",                 # "X 100" is NOT "x100 copies" (skipped 31 real listings)
    "Pokemon Card Regigigas Level X 100/100 Diamond And Pearl Storm FR",
    "Umbreon VMAX 215/203 Evolving Skies Alt Art",
    "M Gengar EX 121/119 Phantom Forces Secret Rare",
    "1x Mega-Gengar-EX - 121/119 - Secret Rare Lightly Played",   # a quantity of one is a single card
    "Pokemon Celebrations Classic Collection Charizard 4/102",     # "Collection" is part of a set name
    "Pokémon Legendary Collection Charizard 3/110 Holo",
])
def test_single_cards_are_not_lots(title):
    assert "lot/multiple" not in parse_title(title).reasons


@pytest.mark.parametrize("title", [
    "Onix 92/130 & Steelix 38/130 LP Lot Diamond & Pearl",
    "2x Charizard 4/102 Base Set", "Charizard 4/102 x3", "10x Pikachu 58/102", "x10 bulk commons",
    "Pokemon card bundle", "Base Set binder", "playset of 4 Pikachu 58/102",
])
def test_real_lots_are_still_caught(title):
    assert "lot/multiple" in parse_title(title).reasons


# ---- parser: prints we cannot value ----

@pytest.mark.parametrize("title,phrase", [
    ("Deoxys ex - 2006 (Jason Klaczynski) 93/106 World Championship Decks Regular", "world championship decks"),
    ("2006 Pokémon TCG World Championships – Deoxys ex 93/106", "world championships"),
    ("Lightly Played DeoxysEX 93/106 Jason Klaczynski WCS 2006", "wcs"),
    ("Deoxys ex 93/106 World Champs deck", "world champs"),
    ("Jumpluff 14/214 Reverse Holo League Stamped Promo", "stamped"),
    ("Charizard 4/102 Staff Prerelease", "staff"),
    ("Pikachu 25/185 Pokemon Center exclusive", "pokémon center" if False else "pokemon center"),
    ("M Gengar EX 121/119 Secret Rare Miscut! MP", "miscut"),
    ("Charizard 4/102 Base Set Holo Black Dot Error", "error"),
    ("Pikachu 58/102 Cosmos Holo", "cosmos holo"),
])
def test_different_prints_are_skipped_with_the_reason(title, phrase):
    reasons = parse_title(title).reasons
    assert any(r.startswith("different print") and phrase in r for r in reasons), reasons


@pytest.mark.parametrize("title", [
    "Charizard 4/102 Base Set Holo Rare NM", "Umbreon VMAX 215/203 Alternate Art", "Pikachu 58/102 Red Cheeks",
    "Blastoise 2/102 Base Set Holo LP",
])
def test_ordinary_titles_are_not_flagged_as_other_prints(title):
    assert not any(r.startswith("different print") for r in parse_title(title).reasons)


# ---- Shadowless ----

@pytest.mark.parametrize("title,expected", [
    ("Charizard Shadowless 4/102 Base Set Holo Rare Stage 2 HP 120", True),
    ("1999 Pokemon Base Set Charizard Shadow-less Holo 4/102", True),
    ("Charizard SHADOW LESS 4/102", True),
    ("Charizard 4/102 Base Set Unlimited Holo", False),
    ("Charizard 4/102 Base Set Holo (not shadowless)", False),
    ("Charizard 4/102 Non-Shadowless Unlimited", False),
    ("Charizard 4/102 non shadowless", False),
])
def test_shadowless_detection(title, expected):
    assert parse_title(title).shadowless is expected


def test_choose_price_values_each_print_against_its_own_price():
    from pokedeals.matcher import choose_price
    prices = {"holofoil": 954.65, "shadowless-holofoil": 2257.87, "1st-edition-holofoil": 9000.0}
    assert choose_price(prices, False, None)[:2] == ("holofoil", 954.65)                    # plain: never rarer prints
    assert choose_price(prices, False, None, shadowless=True)[:2] == ("shadowless-holofoil", 2257.87)
    assert choose_price(prices, True, None)[:2] == ("1st-edition-holofoil", 9000.0)
    assert choose_price(prices, True, None, shadowless=True)[:2] == ("1st-edition-holofoil", 9000.0)


def test_a_missing_rarer_price_falls_back_with_a_warning_not_silently():
    from pokedeals.matcher import choose_price
    only_regular = {"holofoil": 954.65}
    variant, market, flags = choose_price(only_regular, False, None, shadowless=True)
    assert (variant, market) == ("holofoil", 954.65) and "shadowless-price-missing" in flags
    assert "first-edition-price-missing" in choose_price(only_regular, True, None)[2]
    # 1st Edition missing but Shadowless known: 1st Editions are Shadowless, so that is the better fallback
    both = {"holofoil": 954.65, "shadowless-holofoil": 2257.87}
    assert choose_price(both, True, None, shadowless=True)[:2] == ("shadowless-holofoil", 2257.87)


def test_a_shadowless_title_is_valued_as_shadowless_through_the_matcher(tmp_path):
    conn = connect(tmp_path / "s.db")
    conn.execute("INSERT INTO sets (id,name,name_key,official_count) VALUES ('base1','Base Set','base set',102)")
    conn.execute("INSERT INTO cards (id,set_id,local_id,local_key,name,name_key) VALUES ('base1-4','base1','4','4','Charizard','charizard')")
    for variant, market in {"holofoil": 954.65, "shadowless-holofoil": 2257.87}.items():
        conn.execute("INSERT INTO card_prices (card_id,variant,market) VALUES ('base1-4',?,?)", (variant, market))
    conn.commit()
    m = Matcher(conn)
    plain = m.match(parse_title("Charizard 4/102 Base Set Holo Rare"))
    shadow = m.match(parse_title("Charizard Shadowless 4/102 Base Set Holo Rare"))
    assert (plain.variant, plain.market) == ("holofoil", 954.65)
    assert (shadow.variant, shadow.market) == ("shadowless-holofoil", 2257.87)       # was valued at $954
    assert shadow.confidence >= 0.85 and "shadowless-price-missing" not in shadow.flags


# ---- name matching ----

@pytest.mark.parametrize("card,title,expected", [
    ("m gengar ex", " lightly played mega gengar ex 121 119 secret rare ", "exact"),   # "Mega-Gengar-EX"
    ("m gengar ex", " the pokemon company m gengar ex 121 119 ", "exact"),
    ("deoxys ex", " lightly played deoxysex 93 106 ", "fuzzy"),                         # glued together
    ("charizard", " charizard 4 102 base set ", "exact"),
    ("abra", " cabrasomething 49 82 ", None),                  # 4 letters: never matched inside another word
    ("pikachu", " raichu 58 102 ", None),
])
def test_name_hit(card, title, expected):
    assert _name_hit(card, title) == expected


def test_stem_makes_plural_titles_match_singular_set_names():
    assert _stem("celebrations") == "celebration" and _stem("glass") == "glass" and _stem("ex") == "ex"


# ---- reprint collections (Classic Collection) ----

SETS = [  # id, name, official_count, release
    ("base1", "Base Set", 102, "1999-01-09"),
    ("cel25", "Celebrations", 25, "2021-10-08"),
    ("cel25cc", "Celebrations Classic Collection", 25, "2021-10-08"),
    ("30th", "30th Celebration", 128, "2026-09-16"),
    ("30th-c", "30th Classic Collection", 0, "2026-09-16"),
]
CARDS = [  # id, set, local_id, name, {variant: market}
    ("base1-4", "base1", "4", "Charizard", {"holofoil": 954.65}),
    ("base1-2", "base1", "2", "Blastoise", {"holofoil": 226.58}),
    ("cel25-5", "cel25", "5", "Pikachu", {"holofoil": 20.0}),                         # a card of the main set
    ("cel25cc-4", "cel25cc", "4", "Charizard", {"holofoil": 150.0}),                  # reprint, ORIGINAL number
    ("30th-c-1", "30th-c", "001", "Charizard", {"holofoil": 134.86}),
    ("30th-c-2", "30th-c", "002", "Pikachu & Zekrom GX", {"holofoil": 27.22}),
    ("30th-c-3", "30th-c", "003", "Pikachu", {"holofoil": 9.0}),
]


@pytest.fixture()
def matcher(tmp_path):
    conn = connect(tmp_path / "reprints.db")
    for sid, name, n, rel in SETS:
        conn.execute("INSERT INTO sets (id,name,name_key,official_count,release_date) VALUES (?,?,?,?,?)",
                     (sid, name, name_key(name), n, rel))
    for cid, sid, lid, name, prices in CARDS:
        conn.execute("INSERT INTO cards (id,set_id,local_id,local_key,name,name_key) VALUES (?,?,?,?,?,?)",
                     (cid, sid, lid, local_key(lid), name, name_key(name)))
        for variant, market in prices.items():
            conn.execute("INSERT INTO card_prices (card_id,variant,market) VALUES (?,?,?)", (cid, variant, market))
    conn.commit()
    return Matcher(conn)


def match(matcher, title):
    return matcher.match(parse_title(title))


def test_a_plain_base_set_title_is_still_base_set(matcher):
    m = match(matcher, "Charizard 4/102 Base Set Holo Rare NM")
    assert m.card.id == "base1-4" and m.market == 954.65 and "reprint-set" not in m.flags


@pytest.mark.parametrize("title", [
    "Pokémon TCG 30th Anniversary Celebrations Charizard EX Gold Holo 4/102 - MINT",
    "Pokemon TCG Charizard 4/102 Base Set Holo Rare - 30th Celebrations",     # names BOTH Base Set and the reprint
    "Charizard 4/102 30th Celebration",
])
def test_30th_celebration_reprints_are_not_valued_as_base_set(matcher, title):
    """Real case: these were valued at Base Set's $954 and showed up as +76% 'deals' at ~$195."""
    m = match(matcher, title)
    assert m.card.id == "30th-c-1" and m.market == 134.86 and "reprint-set" in m.flags


@pytest.mark.parametrize("title,expected", [
    ("Pokemon Charizard [30C - 4/102] Holofoil Classic Collection NM", "30th-c-1"),      # TCGplayer's own "30C"
    ("Charizard 4/102 30C Holo", "30th-c-1"),
    ("Charizard 4/102 CCC Classic Collection Holo", "cel25cc-4"),
    ("Charizard 4/102 25th Anniversary Holo", "cel25cc-4"),
])
def test_set_abbreviations_pick_the_reprint_set(matcher, title, expected):
    m = match(matcher, title)
    assert m.card.id == expected and "ambiguous-set" not in m.flags and m.confidence >= 0.85


def test_celebrations_25th_reprint_goes_to_that_classic_collection(matcher):
    m = match(matcher, "Pokémon TCG Charizard Holo Card 4/102 Celebrations 25th Anniversary Edition")
    assert m.card.id == "cel25cc-4" and m.market == 150.0


def test_classic_collection_in_the_title_is_enough(matcher):
    m = match(matcher, "Pokemon 30th Celebrations Classic Collection Charizard 4/102")
    assert m.card.id == "30th-c-1"


def test_the_celebrations_main_set_is_not_mistaken_for_a_reprint(matcher):
    """'Pikachu 5/25 Celebrations' is a card of the Celebrations set itself: the number points at it."""
    m = match(matcher, "Pikachu 5/25 Celebrations Holo")
    assert m.card.id == "cel25-5" and "reprint-set" not in m.flags


def test_the_most_specific_card_name_wins_inside_a_reprint_set(matcher):
    both = match(matcher, "30th Anniversary Celebrations Pikachu & Zekrom GX Classic Collection")
    assert both.card.id == "30th-c-2"                                   # not plain "Pikachu"
    plain = match(matcher, "30th Anniversary Celebrations Classic Collection Pikachu")
    assert plain.card.id == "30th-c-3"


def test_a_reprint_title_for_a_card_that_is_not_in_the_reprint_is_not_guessed(matcher):
    """Blastoise is not in either Classic Collection here: better no match than Base Set's price."""
    m = match(matcher, "Blastoise 2/102 Base Set Holo 30th Anniversary Celebrations")
    assert m.card is None and "30th Classic Collection" in m.reason


def test_a_bare_classic_collection_title_is_ambiguous_between_sets(matcher):
    m = match(matcher, "Pokemon Classic Collection Charizard")         # 30th-c AND cel25cc both have one
    assert m.card is not None and "ambiguous-set" in m.flags and m.confidence < 0.75


def test_name_variants_work_through_the_matcher(matcher, tmp_path):
    conn = matcher.conn
    conn.execute("INSERT INTO sets (id,name,name_key,official_count) VALUES ('pf','Phantom Forces','phantom forces',119)")
    conn.execute("INSERT INTO cards (id,set_id,local_id,local_key,name,name_key) VALUES "
                 "('pf-121','pf','121','121','M Gengar EX','m gengar ex')")
    conn.execute("INSERT INTO cards (id,set_id,local_id,local_key,name,name_key) VALUES "
                 "('pf-93','pf','93','93','Deoxys ex','deoxys ex')")
    conn.execute("INSERT INTO sets (id,name,name_key,official_count) VALUES ('em','Emerald','emerald',106)")
    conn.execute("UPDATE cards SET set_id='em' WHERE id='pf-93'")
    conn.execute("INSERT INTO card_prices (card_id,variant,market) VALUES ('pf-121','holofoil',500.0)")
    conn.execute("INSERT INTO card_prices (card_id,variant,market) VALUES ('pf-93','holofoil',265.0)")
    conn.commit()
    gengar = match(matcher, "Lightly Played Mega-Gengar-EX - 121/119 - Secret Rare Pokemon Phantom Forces")
    assert gengar.card.id == "pf-121" and gengar.confidence >= 0.9 and "name-not-in-title" not in gengar.flags
    deoxys = match(matcher, "Lightly Played DeoxysEX - 93/106 - Emerald Holo")
    assert deoxys.card.id == "pf-93" and "name-fuzzy" in deoxys.flags and deoxys.confidence >= 0.8
