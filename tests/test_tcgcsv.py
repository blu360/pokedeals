"""TCGplayer price feed: parsing, set->group mapping, card matching, and overriding TCGdex prices."""
import asyncio
from dataclasses import replace

import httpx
import pytest

from pokedeals.config import Settings
from pokedeals.db import connect
from pokedeals.sync import sync
from pokedeals.tcgcsv import (
    CardRow, Feed, apply_matches, fetch_feed, loose_name, match_cards, parse_group, parse_number, refresh_prices,
)
from pokedeals.textnorm import local_key, name_key


# ---- a miniature feed shaped like the real one ----

def product(pid, name, number, rarity="Rare"):
    ext = [{"name": "Rarity", "value": rarity}]
    if number is not None:
        ext.append({"name": "Number", "value": number})
    return {"productId": pid, "name": name, "extendedData": ext}


def price(pid, sub, market):
    scale = (lambda k: None if market is None else round(market * k, 2))
    return {"productId": pid, "subTypeName": sub, "marketPrice": market, "lowPrice": scale(0.8),
            "midPrice": market, "highPrice": scale(2), "directLowPrice": None}


GROUPS = {
    1: ("SV: 151", [
        product(101, "Charizard ex - 199/165", "199/165"),
        product(102, "Pikachu - 025/165", "025/165"),
        product(103, "Pikachu - 025/165 (Staff)", "025/165"),
        product(104, "151 Booster Pack", None),                         # sealed: no card number
        product(105, "Mew ex - 151/165 [Code Card]", "151/165"),         # code card: not a card
    ], [price(101, "Holofoil", 50.0), price(102, "Normal", 1.0), price(102, "Reverse Holofoil", 2.0),
        price(103, "Holofoil", 100.0), price(104, "Normal", 5.0), price(105, "Normal", 0.1)]),
    2: ("SV: Promo Cards", [
        product(201, "Fuecoco - 002", "002", "Promo"),
        product(202, "Quaxly - 003", "003", "Promo"),
    ], [price(201, "Holofoil", 3.0), price(202, "Holofoil", 4.0)]),
    3: ("ME: Classic Collection", [
        product(301, "Charizard", "4/102", "Classic Collection"),       # the ORIGINAL print's number
        product(302, "Pikachu & Zekrom GX", "33/181", "Classic Collection"),
    ], [price(301, "Holofoil", 135.0), price(302, "Holofoil", 27.0)]),
    4: ("EX Dragon Frontiers", [
        product(401, "Altaria ex (Delta Species)", "90/101", "Ultra Rare"),
        product(402, "Ampharos (Delta Species)", "1/101", "Holo Rare"),
        product(403, "Boost Energy", "87/101", "Uncommon"),
    ], [price(401, "Holofoil", 40.0), price(402, "Holofoil", 5.0), price(403, "Normal", None)]),
    5: ("Base Set", [
        product(501, "Charizard - 4/102", "4/102"),
        product(502, "Blastoise - 2/102", "2/102"),
        product(503, "Venusaur - 15/102", "15/102"),
    ], [price(501, "Unlimited Holofoil", 400.0), price(501, "1st Edition Holofoil", 9000.0),
        price(502, "Unlimited Holofoil", 226.0), price(503, "Unlimited Holofoil", 168.0)]),
}


def make_feed() -> Feed:
    return Feed([parse_group({"groupId": gid, "name": name}, prods, prices)
                 for gid, (name, prods, prices) in GROUPS.items()], updated="2026-10-15T20:00:00+0000")


def card(cid, set_id, name, local, official=None):
    return CardRow(cid, set_id, name, local_key(local), official)


CARDS = [
    card("mew-199", "mew", "Charizard ex", "199", 165),
    card("mew-25", "mew", "Pikachu", "025", 165),
    card("mew-999", "mew", "Some Card Not In The Feed", "999", 165),
    card("svp-2", "svp", "Fuecoco", "002", None),
    card("svp-3", "svp", "Quaxly", "003", None),
    card("clc-1", "clc", "Charizard", "001", 30),
    card("clc-2", "clc", "Pikachu & Zekrom-GX", "002", 30),
    card("df-90", "df", "Altaria ex δ", "90", 101),
    card("df-1", "df", "Ampharos δ", "1", 101),
    card("df-87", "df", "Boost Energy", "87", 101),
    card("base1-4", "base1", "Charizard", "4", 102),
    card("base1-2", "base1", "Blastoise", "2", 102),
    card("base1-15", "base1", "Venusaur", "15", 102),
]


# ---- parsing ----

@pytest.mark.parametrize("raw,expected", [
    ("021/128", ("21", 128)), ("4/102", ("4", 102)), ("TG01/TG30", ("TG1", 30)),
    ("XY01", ("XY1", None)), ("002", ("2", None)), ("SWSH001", ("SWSH1", None)), ("", None), (None, None), ("n/a", None),
    ("43a/124", ("43A", 124)), ("RC4/RC32", ("RC4", 32)), ("043A", ("43A", None)),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.parametrize("name,expected,qualified", [
    ("Greninja ex - 021/128", "greninja ex", False),
    ("Fuecoco - 002", "fuecoco", False),
    ("Chespin - XY01", "chespin", False),
    ("Altaria ex (Delta Species)", "altaria ex", True),
    ("Pikachu - 025/165 (Staff)", "pikachu", True),
    ("Charizard", "charizard", False),
])
def test_loose_name(name, expected, qualified):
    assert loose_name(name) == (expected, qualified)


def test_tcgdex_delta_symbol_matches_feed_qualifier():
    assert name_key("Altaria ex δ") == loose_name("Altaria ex (Delta Species)")[0]


def test_non_cards_are_dropped_at_parse_time():
    ids = {p.product_id for g in make_feed().groups for p in g.products}
    assert 104 not in ids and 105 not in ids              # sealed pack, code card
    assert {101, 102, 103, 201}.issubset(ids)


def test_subtypes_map_to_tcgdex_variant_keys():
    base = next(g for g in make_feed().groups if g.group_id == 5)
    charizard = next(p for p in base.products if p.product_id == 501)
    assert {p.variant for p in charizard.prices} == {"unlimited-holofoil", "1st-edition-holofoil"}


# ---- matching ----

@pytest.fixture()
def matched():
    matches, groups = match_cards(CARDS, make_feed())
    return {m.card_id: m for m in matches}, groups


def test_number_and_name_match(matched):
    by_card, _ = matched
    assert (by_card["mew-199"].product.product_id, by_card["mew-199"].method) == (101, "number+name")
    assert by_card["base1-4"].product.product_id == 501


def test_plain_print_beats_qualified_variant(matched):
    by_card, _ = matched
    assert by_card["mew-25"].product.product_id == 102        # not "(Staff)" at $100


def test_promos_match_by_number_without_set_size(matched):
    by_card, groups = matched
    assert by_card["svp-2"].product.product_id == 201 and by_card["svp-3"].product.product_id == 202
    assert groups["svp"] == [2]


def test_classic_collection_matches_by_name_when_numbers_cannot(matched):
    by_card, groups = matched
    assert by_card["clc-1"].product.product_id == 301 and by_card["clc-1"].method == "name"
    assert by_card["clc-2"].product.product_id == 302          # "Zekrom-GX" vs "Zekrom GX"
    assert groups["clc"] == [3]


def test_delta_species_qualifier_does_not_break_matching(matched):
    by_card, _ = matched
    assert by_card["df-90"].product.product_id == 401
    assert by_card["df-1"].product.product_id == 402


def test_reprint_group_does_not_hijack_the_original_set(matched):
    """Charizard 4/102 appears in both Base Set and the Classic Collection; each set keeps its own group."""
    _, groups = matched
    assert groups["base1"] == [5]
    assert 5 not in groups["clc"]


def test_staff_stamped_promo_never_beats_the_ordinary_print():
    """Real case: SM130 has '(Prerelease) [Staff]' ($92.62) and '(Prerelease)' ($8.22); the Staff one has
    the lower product id, so a plain 'earliest product' tie-break picked the wrong card."""
    group = parse_group({"groupId": 9, "name": "SM Promos"}, [
        product(172932, "Manectric - SM130 (Prerelease) [Staff]", "SM130", "Promo"),
        product(172933, "Manectric - SM130 (Prerelease)", "SM130", "Promo"),
        product(900, "Rillaboom - SWSH006 (Staff)", "SWSH006", "Promo"),
        product(901, "Rillaboom - SWSH006 (Cosmos Holo) (Pokemon Center)", "SWSH006", "Promo"),
    ], [price(172932, "Holofoil", 92.62), price(172933, "Holofoil", 8.22),
        price(900, "Holofoil", 147.5), price(901, "Holofoil", 5.8)])
    feed = Feed([group], "t")
    cards = [card("sm-130", "smp", "Manectric", "SM130"), card("sm-131", "smp", "Rillaboom", "SWSH006"),
             card("sm-9", "smp", "Filler A", "SM900"), card("sm-10", "smp", "Filler B", "SM901")]
    matches, _ = match_cards(cards, feed)
    got = {m.card_id: m.product.product_id for m in matches}
    assert got["sm-130"] == 172933                     # the ordinary prerelease promo, $8.22
    assert got["sm-131"] == 901                        # two qualifiers still beats a Staff stamp


def test_base_set_gets_1st_edition_and_shadowless_prices_from_the_shadowless_group(tmp_path):
    """Base Set's 1st Editions and Shadowless prints are listed only under 'Base Set (Shadowless)'. They become
    their own variants and must NEVER be mixed into the ordinary (unlimited) print's price."""
    base = parse_group({"groupId": 40, "name": "Base Set"},
                       [product(5000 + i, f"Mon{i} - {i}/102", f"{i}/102") for i in range(1, 13)],
                       [price(5000 + i, "Holofoil", 10.0 + i) for i in range(1, 13)])
    shadow = parse_group({"groupId": 41, "name": "Base Set (Shadowless)"},
                         [product(6000 + i, f"Mon{i} - {i}/102", f"{i}/102") for i in range(1, 13)],
                         [price(6000 + i, "1st Edition Holofoil", 900.0 + i) for i in range(1, 12)]   # #12: none
                         + [price(6000 + i, "Unlimited Holofoil", 104.0 + i) for i in range(1, 13)])
    conn = connect(tmp_path / "base.db")
    conn.execute("INSERT INTO sets (id,name,name_key,official_count) VALUES ('base1','Base Set','base set',102)")
    for i in range(1, 13):
        conn.execute("INSERT INTO cards (id,set_id,local_id,local_key,name,name_key) VALUES (?,?,?,?,?,?)",
                     (f"base1-{i}", "base1", str(i), str(i), f"Mon{i}", f"mon{i}"))
    conn.commit()
    report = asyncio.run(refresh_prices(conn, feed=Feed([shadow, base], "t"), progress=lambda m: None))

    assert rows_for(conn, "base1-6") == {"holofoil": (16.0, "tcgcsv"),                  # the ordinary Base Set print
                                         "1st-edition-holofoil": (906.0, "tcgcsv"),     # 1st Edition, from Shadowless
                                         "shadowless-holofoil": (110.0, "tcgcsv")}      # the Shadowless print itself
    assert "unlimited-holofoil" not in rows_for(conn, "base1-6")      # its own name, never the lookalike's
    pid = lambda variant: conn.execute("SELECT product_id FROM card_prices WHERE card_id='base1-6' AND variant=?",
                                       (variant,)).fetchone()[0]
    assert (pid("holofoil"), pid("1st-edition-holofoil"), pid("shadowless-holofoil")) == (5006, 6006, 6006)
    assert rows_for(conn, "base1-12") == {"holofoil": (22.0, "tcgcsv"),                 # no 1st Edition price for #12
                                          "shadowless-holofoil": (116.0, "tcgcsv")}
    assert report.stamped_added == 12


def test_group_name_scoring():
    from pokedeals.tcgcsv import _name_score
    assert _name_score("Base Set", "base set") == 3
    assert _name_score("Base Set (Shadowless)", "base set") == 2
    assert _name_score("SWSH01: Sword & Shield Base Set", "base set") == 2
    assert _name_score("Jungle", "base set") == 0
    assert _name_score("Base Set", "") == 0


def test_lookalike_group_does_not_win_or_fill_gaps():
    """Real case: 'Base Set' and 'Base Set (Shadowless)' hold the same cards. The Shadowless group had MORE
    priced products, so it won on coverage and Base Set Charizard got the Shadowless price ($2,257 vs $954)."""
    numbers = range(1, 13)
    # Base Set: Charizard (#4) and #5 have no price rows (e.g. no recent sales)
    base_prods = [product(3000 + i, f"Mon{i} - {i}/102", f"{i}/102") for i in numbers]
    base_prices = [price(3000 + i, "Holofoil", 10.0 + i) for i in numbers if i not in (4, 5)]
    shadow_prods = [product(4000 + i, f"Mon{i} - {i}/102", f"{i}/102") for i in numbers]
    shadow_prices = [price(4000 + i, "Unlimited Holofoil", 100.0 + i) for i in numbers]
    feed = Feed([parse_group({"groupId": 31, "name": "Base Set (Shadowless)"}, shadow_prods, shadow_prices),
                 parse_group({"groupId": 30, "name": "Base Set"}, base_prods, base_prices)], "t")
    cards = [CardRow(f"base1-{i}", "base1", f"Mon{i}", str(i), 102, name_key("Base Set")) for i in numbers]
    matches, groups = match_cards(cards, feed)
    assert groups["base1"] == [30]                                  # the same-named group, alone
    by_card = {m.card_id: m.product for m in matches}
    assert by_card["base1-6"].product_id == 3006                    # priced in Base Set
    assert by_card["base1-4"].product_id == 3004                    # unpriced in Base Set: still THIS group's
    assert by_card["base1-4"].prices == []                          #   product, not the Shadowless $104


def test_a_set_spanning_two_feed_groups_is_matched_in_both():
    """Real case: TCGdex 'Generations' includes the Radiant Collection; the feed has it as its own group."""
    main = parse_group({"groupId": 11, "name": "Generations"},
                       [product(1100 + i, f"Mon{i} - {i}/83", f"{i}/83") for i in range(1, 9)],
                       [price(1100 + i, "Normal", 1.0 + i) for i in range(1, 9)])
    radiant = parse_group({"groupId": 12, "name": "Generations: Radiant Collection"},
                          [product(1200 + i, f"Shiny{i} - RC{i}/RC32", f"RC{i}/RC32") for i in range(1, 5)],
                          [price(1200 + i, "Holofoil", 10.0 + i) for i in range(1, 5)])
    feed = Feed([main, radiant], "t")
    cards = ([card(f"g-{i}", "gen", f"Mon{i}", str(i), 83) for i in range(1, 9)]
             + [card(f"g-rc{i}", "gen", f"Shiny{i}", f"RC{i}", 83) for i in range(1, 5)])
    matches, groups = match_cards(cards, feed)
    assert sorted(groups["gen"]) == [11, 12]
    assert {m.card_id for m in matches} == {c.id for c in cards}


def test_a_single_reprint_does_not_attach_a_second_group():
    base = parse_group({"groupId": 21, "name": "Base"},
                       [product(2100 + i, f"Mon{i} - {i}/102", f"{i}/102") for i in range(1, 13)],
                       [price(2100 + i, "Normal", 2.0) for i in range(1, 13)])
    reprints = parse_group({"groupId": 22, "name": "Classic"},
                           [product(2201, "Mon1", "1/102"), product(2202, "Other", "9/9")],
                           [price(2201, "Holofoil", 99.0), price(2202, "Holofoil", 5.0)])
    cards = [card(f"b-{i}", "base", f"Mon{i}", str(i), 102) for i in range(1, 13)]
    matches, groups = match_cards(cards, Feed([base, reprints], "t"))
    assert groups["base"] == [21]
    assert next(m for m in matches if m.card_id == "b-1").product.product_id == 2101     # not the $99 reprint


def test_half_deck_qualifier_is_resolved_by_the_set_name():
    """Real case: 'Potion (Latias)' and 'Potion (Latios)' share a number; the set name says which."""
    group = parse_group({"groupId": 7, "name": "EX Trainer Kit"}, [
        product(701, "Potion (Latias)", "8/10", "Common"),
        product(702, "Potion (Latios)", "8/10", "Common"),
        product(703, "Energy Search (Latias)", "9/10", "Common"),
        product(704, "Energy Search (Latios)", "9/10", "Common"),
    ], [price(701, "Normal", 0.15), price(702, "Normal", 0.17), price(703, "Normal", 0.21), price(704, "Normal", 0.20)])
    feed = Feed([group], "t")
    cards = [CardRow("k-lat-8", "kl", "Potion", "8", 10, name_key("EX trainer Kit (Latias)")),
             CardRow("k-lat-9", "kl", "Energy Search", "9", 10, name_key("EX trainer Kit (Latias)")),
             CardRow("k-lio-8", "ki", "Potion", "8", 10, name_key("EX trainer Kit (Latios)")),
             CardRow("k-lio-9", "ki", "Energy Search", "9", 10, name_key("EX trainer Kit (Latios)"))]
    got = {m.card_id: m.product.product_id for m in match_cards(cards, feed)[0]}
    assert got == {"k-lat-8": 701, "k-lat-9": 703, "k-lio-8": 702, "k-lio-9": 704}


def test_unknown_card_stays_unmatched(matched):
    by_card, _ = matched
    assert "mew-999" not in by_card


def test_set_with_no_evidence_is_not_mapped():
    matches, groups = match_cards([card("x-1", "x", "Nonexistent", "1", 10), card("x-2", "x", "Other", "2", 10),
                                   card("x-3", "x", "Third", "3", 10), card("x-4", "x", "Fourth", "4", 10)],
                                  make_feed())
    assert matches == [] and groups == {}


# ---- storing: the feed overrides TCGdex ----

@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "feed.db")
    c.execute("INSERT INTO sets (id,name,name_key,official_count) VALUES ('mew','151','151',165)")
    c.execute("INSERT INTO sets (id,name,name_key,official_count) VALUES ('df','Dragon Frontiers','dragon frontiers',101)")
    for cid, sid, name, local in [("mew-199", "mew", "Charizard ex", "199"), ("mew-25", "mew", "Pikachu", "025"),
                                  ("df-87", "df", "Boost Energy", "87"), ("df-90", "df", "Altaria ex δ", "90"),
                                  ("df-1", "df", "Ampharos δ", "1")]:
        c.execute("INSERT INTO cards (id,set_id,local_id,local_key,name,name_key) VALUES (?,?,?,?,?,?)",
                  (cid, sid, local, local_key(local), name, name_key(name)))
    rows = [("mew-199", "holofoil", 40.0),                 # TCGdex says $40, feed says $50  -> corrected
            ("mew-25", "normal", 1.0), ("mew-25", "reverse-holofoil", 2.0), ("mew-25", "holofoil", 77.0)]
    for cid, variant, market in rows:
        c.execute("INSERT INTO card_prices (card_id,variant,market,source) VALUES (?,?,?,'tcgdex')", (cid, variant, market))
    c.commit()
    return c


def rows_for(conn, cid):
    return {r["variant"]: (r["market"], r["source"]) for r in
            conn.execute("SELECT variant, market, source FROM card_prices WHERE card_id=?", (cid,))}


def test_feed_overrides_differing_tcgdex_price_and_fills_gaps(conn):
    report = asyncio.run(refresh_prices(conn, feed=make_feed(), progress=lambda m: None))
    assert rows_for(conn, "mew-199") == {"holofoil": (50.0, "tcgcsv")}      # was $40 from TCGdex
    assert rows_for(conn, "df-90") == {"holofoil": (40.0, "tcgcsv")}        # had no price at all
    assert report.changed >= 1 and report.filled >= 2


def test_a_matched_card_takes_the_feeds_prices_wholesale(conn):
    """TCGdex had a stale 'holofoil' $77 the feed does not list. Keeping it would show two prices for one
    print (this is what happened to Base Set Charizard), so a matched card gets exactly the feed's rows."""
    asyncio.run(refresh_prices(conn, feed=make_feed(), progress=lambda m: None))
    assert rows_for(conn, "mew-25") == {"normal": (1.0, "tcgcsv"), "reverse-holofoil": (2.0, "tcgcsv")}


def test_product_with_no_market_price_leaves_card_alone(conn):
    asyncio.run(refresh_prices(conn, feed=make_feed(), progress=lambda m: None))
    assert rows_for(conn, "df-87") == {}                 # Boost Energy: feed row has market None


def test_rerun_replaces_instead_of_stacking(conn):
    for _ in range(3):
        asyncio.run(refresh_prices(conn, feed=make_feed(), progress=lambda m: None))
    assert conn.execute("SELECT count(*) FROM card_prices WHERE card_id='mew-199'").fetchone()[0] == 1
    assert rows_for(conn, "mew-199") == {"holofoil": (50.0, "tcgcsv")}


def test_prices_updated_is_stamped_from_the_feed(conn):
    asyncio.run(refresh_prices(conn, feed=make_feed(), progress=lambda m: None))
    assert conn.execute("SELECT prices_updated FROM cards WHERE id='mew-199'").fetchone()[0] == "2026-10-15T20:00:00+0000"
    assert conn.execute("SELECT product_id FROM card_prices WHERE card_id='mew-199'").fetchone()[0] == 101


def test_report_unpriced_after(conn):
    report = asyncio.run(refresh_prices(conn, feed=make_feed(), progress=lambda m: None))
    assert report.unpriced_after == 1                      # df-87 only


# ---- migration ----

def test_old_database_gains_source_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE card_prices (card_id TEXT NOT NULL, variant TEXT NOT NULL, market REAL, low REAL, mid REAL,
            high REAL, direct_low REAL, currency TEXT, updated TEXT, PRIMARY KEY (card_id, variant));
        INSERT INTO card_prices (card_id, variant, market) VALUES ('c', 'normal', 1.5);""")
    old.commit()
    old.close()
    c = connect(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(card_prices)")}
    assert {"source", "product_id"} <= cols
    assert c.execute("SELECT source FROM card_prices").fetchone()[0] == "tcgdex"   # existing rows are TCGdex's


# ---- network: fetching the feed ----

def feed_transport(fail_group=None, broken=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if broken:
            return httpx.Response(503)
        path = request.url.path
        if path == "/last-updated.txt":
            return httpx.Response(200, text="2026-10-15T20:00:00+0000\n")
        if path == "/tcgplayer/3/groups":
            return httpx.Response(200, json={"results": [{"groupId": g, "name": n} for g, (n, _, _) in GROUPS.items()]})
        gid = int(path.split("/")[3])
        if gid == fail_group:
            return httpx.Response(500)
        _, prods, prices = GROUPS[gid]
        return httpx.Response(200, json={"results": prods if path.endswith("/products") else prices})
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr("pokedeals.tcgcsv.asyncio.sleep", instant)


def client(**kw):
    return httpx.AsyncClient(base_url="https://tcgcsv.test", transport=feed_transport(**kw))


def test_fetch_feed_parses_every_group_and_reads_the_timestamp():
    feed = asyncio.run(fetch_feed(client()))
    assert len(feed.groups) == 5 and feed.updated == "2026-10-15T20:00:00+0000"
    assert sum(len(g.products) for g in feed.groups) == 13       # 15 products less the sealed pack + code card


def test_one_failing_group_is_skipped_not_fatal():
    messages = []
    feed = asyncio.run(fetch_feed(client(fail_group=2), progress=messages.append))
    assert [g.group_id for g in feed.groups] == [1, 3, 4, 5]
    assert any("could not fetch 1 price groups" in m for m in messages)


# ---- sync integration ----

def tcgdex_transport():
    sets = {"base1": {"id": "base1", "name": "Base Set", "serie": {"id": "base", "name": "Base"},
                      "cardCount": {"official": 102, "total": 102},
                      "cards": [{"id": "base1-4"}, {"id": "base1-2"}, {"id": "base1-15"}]}}
    cards = {
        "base1-4": {"id": "base1-4", "localId": "4", "name": "Charizard", "category": "Pokemon",
                    "pricing": {"tcgplayer": {"unit": "USD", "updated": "x", "holofoil": {"marketPrice": 300.0}}}},
        "base1-2": {"id": "base1-2", "localId": "2", "name": "Blastoise", "category": "Pokemon", "pricing": None},
        "base1-15": {"id": "base1-15", "localId": "15", "name": "Venusaur", "category": "Pokemon",
                     "pricing": {"tcgplayer": {"unit": "USD", "updated": "x", "holofoil": {"marketPrice": 168.0}}}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v2/en")
        if path == "/sets":
            return httpx.Response(200, json=[{"id": "base1"}])
        if path.startswith("/sets/"):
            return httpx.Response(200, json=sets[path.split("/")[-1]])
        return httpx.Response(200, json=cards[path.split("/")[-1]])
    return httpx.MockTransport(handler)


def tcgdex_client():
    return httpx.AsyncClient(base_url="https://tcgdex.test/v2/en", transport=tcgdex_transport())


def test_sync_lays_the_feed_over_tcgdex(tmp_path):
    settings = replace(Settings(), db_path=tmp_path / "s.db")
    messages = []
    stats = asyncio.run(sync(settings, progress=messages.append, client=tcgdex_client(), feed_client=client()))
    c = connect(settings.db_path)
    assert rows_for(c, "base1-4")["unlimited-holofoil"] == (400.0, "tcgcsv")       # feed price, not TCGdex's $300
    assert rows_for(c, "base1-2")["unlimited-holofoil"] == (226.0, "tcgcsv")       # TCGdex had nothing
    assert stats["feed_matched"] == 3 and stats["feed_filled"] >= 1
    assert any("TCGplayer feed: matched 3 cards" in m for m in messages)


def test_sync_survives_a_dead_price_feed(tmp_path):
    settings = replace(Settings(), db_path=tmp_path / "s.db")
    messages = []
    asyncio.run(sync(settings, progress=messages.append, client=tcgdex_client(), feed_client=client(broken=True)))
    c = connect(settings.db_path)
    assert rows_for(c, "base1-4") == {"holofoil": (300.0, "tcgdex")}                # TCGdex data intact
    assert any("price feed unavailable" in m for m in messages)
