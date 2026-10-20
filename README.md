# pokedeals

eBay deal finder for Pokémon TCG singles. Cards come from [TCGdex](https://tcgdex.dev); TCGplayer market
prices come from TCGplayer's own daily price feed (via [tcgcsv.com](https://tcgcsv.com)) with TCGdex's
snapshot as the fallback; listings come from the eBay Browse API.

```
TCGdex ──sync──▶ SQLite (cards, prices) ◀──prices── TCGplayer feed (tcgcsv.com, daily)
                     │
eBay listings ──▶ parser ──▶ matcher ──▶ scorer ──▶ deals
 (or fixture)    title →      card +      value vs
                 fields       variant     price+ship
```

### Where prices come from

1. `sync` pulls cards and TCGdex's TCGplayer snapshot.
2. It then lays the **TCGplayer feed over it**: each of our sets is mapped to a feed group by voting on
   matching cards, then cards are matched by printed number + name (falling back to number, then to a
   unique name for promos and Classic Collections). Where a card matches, the feed's price **replaces**
   TCGdex's, per variant; each price row records its `source` and TCGplayer `product_id`.
3. A matched card takes the feed's prices **wholesale** (not mixed with TCGdex's, which would show two
   prices for one print). Cards the feed can't match keep their TCGdex price. About 99.4% are priced.
4. Near-identical groups are told apart by name: "Base Set" wins over "Base Set (Shadowless)" for our
   Base Set. TCGplayer lists Base Set's **1st Editions** only under the Shadowless group, so just those
   1st Edition rows are taken from it (they are the "stamped" price); its Unlimited rows are a different
   print and are never used.

Why both: TCGdex has gaps (e.g. all of 30th Celebration is null) and sometimes maps a card to the wrong
TCGplayer product. When the two disagree on the *same* product, the feed is the fresher one.
The feed updates once a day, so prices can be about a day old.

The running app keeps these current by itself: once an hour it asks the feed whether it has published a newer day
(it publishes once a day, around 20:05 UTC) and, if so, pulls it in and re-values everything stored. You can also run
`python -m pokedeals prices` by hand (about 10 seconds, no TCGdex re-sync). The "prices as of" date in the header is
the feed's own publish time.

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Windows
```

> Keep the venv path short on Windows (the 260-char path limit bites deep project folders).

## Usage

```bash
python -m pokedeals sync                  # cards + prices from TCGdex, then the price feed (~8 min)
python -m pokedeals sync base1 swsh3      # only some sets
python -m pokedeals sync --no-feed        # TCGdex only, skip the TCGplayer feed step
python -m pokedeals prices                # refresh prices from the TCGplayer feed only (~10 s, daily)
python -m pokedeals stats                 # what is in the DB, and where prices came from

# debug how one title is read and valued
python -m pokedeals match "Charizard 4/102 Base Set Holo NM" --price 620 --shipping 5

# evaluate listings
python -m pokedeals scan --fixture data/sample_listings.json -v      # offline
python -m pokedeals scan -q "pokemon charizard" -q "pokemon pikachu" # live (needs eBay key)
```

### eBay credentials

Put your keys in `keyset.txt` in the project folder (it is git-ignored), in the format eBay's developer
portal shows:

```
APP ID
<your app id>

Cert ID
<your cert id>
```

or set `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` (these win over the file). The environment is detected from
the key itself: `-SBX-` / `SBX-` keys are **sandbox**, `-PRD-` / `PRD-` keys are **production**.

The file may hold both keysets. Put a `SANDBOX` / `PRODUCTION` header line before each block (labels may
be `APP ID`, `Cert ID`, `Dev ID` or the short `APP:` / `CERT:` forms; `Label: value` and
label-then-value-on-the-next-line both work). With both present, production is used; set
`EBAY_ENV=sandbox` to use the sandbox block instead. **Restart the app after changing keys.**

- **Sandbox keys** work for development but eBay's sandbox holds *test data only*: no real Pokémon
  listings. The UI says "eBay sandbox" so this is never mistaken for real results.
- **For real deals you need a production keyset.** On developer.ebay.com, open *Application Keys* and
  generate the Production keyset. New production keysets must first deal with eBay's *Marketplace Account
  Deletion* requirement: either subscribe to the notifications, or opt out with an exemption (this app does
  not store eBay user data, so opting out applies). That step is what activates the keyset.
- `python -m pokedeals ebay-check` verifies the credentials end to end (token, a real search, how real
  listings parse and match). Run it after adding a production keyset.
- `python -m pokedeals serve --sample` (or `scan --sample`) uses the bundled sample listings even when
  keys are present.

**Add your ZIP code**: enter it on the **Tools** tab ("Shipping destination", live mode only), or put
`ZIP: 12345` on its own line in `keyset.txt`, or set `EBAY_BUYER_ZIP`. A ZIP saved in the UI wins over the file.
About a third of eBay listings use *calculated* shipping, and eBay only returns a cost when it knows where the
card is going. Without a ZIP those listings get an assumed `assumed_shipping` ($4.50, flagged
"shipping-assumed"). A ZIP entered in the UI is saved in the local database (`app_settings`); it is only ever
sent to eBay, with the listing request.

**API cost.** Each search is 1 call per 200 listings, plus 4 more to label the seller's stated condition
(see below), so roughly 5 calls per card; a 50-card watchlist scan is ~250+. Set
`ebay_label_conditions = False` in `config.py` to use 1 call per search and read condition from titles only.
Check your daily call limit on developer.ebay.com.

### Web UI

```bash
python -m pokedeals serve            # http://127.0.0.1:8765
```

Five tabs. Clicking any card's image or name opens a larger image with every variant's TCGplayer price
and a resale calculator (see below). Pokémon TCG Pocket (digital) cards are not part of the physical game
and are never synced; `sync` also removes any that an older sync stored.

- **Deals**: a carousel of the best deals at the top, then every tracked listing sorted into four **price
  categories** by how far under what the card is worth it is (the smaller of the saving against TCGplayer and
  against what other eBay sellers ask):

  | Category | Saving |
  |---|---|
  | **Steal** | 25% or more under |
  | **Deal** | 10% to 25% under |
  | **Fair** | from 10% under to 5% over |
  | **Overpriced** | more than 5% over |

  A Steal or Deal measured against a price with little behind it is called **Uncertain** instead: when TCGplayer has
  fewer than 5 sales of that card in that condition in the last 3 months (`thin_market_sales`), or none. It has its own
  chip (on by default), says why in the row and popup, and points to eBay's sold listings, which are a better guide for
  thin cards. Deals are not re-checked for "has it sold?" (that was the costliest call for the least value); opening a
  listing checks it live, and deals expire after 3 days.

  Only Steal and Deal show by default; click the chips to show the others (each shows how many listings it holds).
  "Also show" adds Suspicious listings (likely fakes), **Held back** ones (cheap, but from a weak or brand-new seller,
  an unsure match, or an auction far from ending) and Skipped ones (graded, lots, proxies). Sellers outside the US are
  left out unless you tick **Include international sellers**. Other filters: format (Buy It Now / Auction / offers),
  condition, minimum savings, seller-confirmed condition, and a price range. Each row has the listing's photo, and a
  click opens a popup with the eBay photo beside our card, a tile for each fact, and a check of the listing's item
  specifics against our card.
  - **Cost filter**: min/max on total cost (price + shipping), applied in the browser and remembered.
  - **Watchlist only**: restricts the feed to cards on your watchlist (★ marks watched cards otherwise).
- **Search**: find any card in the DB (`charizard base`, `pikachu 58/102`, `umbreon vmax`), Add it to the
  watchlist, or open **Listings** to pull current listings for that card and compare each against the
  TCGplayer market price (cheapest / median / % under or over market). Graded slabs, lots and proxies
  are hidden behind "show excluded". In live mode this is one eBay call per card per click.
  Results can be sorted by best match, price high to low, or price low to high. The sort is applied by
  the server across every page, and unpriced cards always come last.
  Each card shows **one market price** (the average of its regular prints, which for nearly every card
  is a single print) and, beside it, its **reverse holo or 1st Edition (stamped) price**, or N/A when
  that print doesn't exist. They are separate on purpose: averaging them together would blur them
  (Legendary Collection Slowpoke is $1.65 normal and $1,000 reverse holo). Sorting and set values use
  the same numbers. Open a card for every print's price.
- **Sets**: every set, sortable by newest / oldest / A–Z / most cards / set value (one copy of each card at
  its cheapest print). Open a set to see its cards sorted by card number, price (either way) or name.
  Each card has the same Listings button, so eBay is only queried when you ask for a specific card.
- **Watchlist**: your watched cards, each with the same Listings comparison. In live mode a watchlist
  scan issues one eBay search per watched card (`Charizard 4/102`), capped by `max_watchlist_queries`.
- **Resale profit** (Tools tab, the card dialog, and a "Resale" column on listings): net after eBay's
  13.25% final value fee (2.35% above $7,500), the $0.30/$0.40 per-order fee, postage and packaging.
  Postage depends on the card's price: under $20 the eBay Standard Envelope ($0.84, verified rate);
  $20 and up needs tracked shipping, and over $750 signature confirmation. The Standard Envelope and fee
  figures come from eBay's seller pages; the tracked-shipping and signature figures are **estimates**
  (marked in the UI) because sources disagree. Edit them in `pokedeals/profit.py`.
- **Tools**: "Try a listing" (paste a title + price to see how it is parsed, matched and valued) and
  the current thresholds.

Paging and "is this everything?":

- **Card search** returns one page at a time with the true match count (`Showing 20 of 288 matches`)
  and a "Show 20 more" button until every match is loaded.
- **eBay fetches** page through results 200 at a time up to `ebay_max_listings_per_query` (default
  400; each page is one API call). When eBay matched more than were fetched, the UI says so
  (`eBay matched 1,243 results… only the first 400 were fetched`) and offers "Fetch up to 1,000".
  The deals feed flags cut-off searches in its summary line.
- **Listing tables** show 25 rows at a time with "Show 25 more".

Notes:

- Shows a `SAMPLE DATA` badge until eBay keys are set, then switches to `LIVE EBAY` automatically.
  In sample mode the listings comparison only has the bundled sample listings (a few cards).
- The watchlist lives in `data/pokedeals.db` next to the card cache. `sync` never touches it, but
  deleting the DB file to start fresh would lose it.
- Binds to localhost only and has no auth. Do not expose it on a network.

JSON endpoints behind it: `GET /api/status`, `GET /api/scan?q=...&scope=all|watchlist`,
`POST /api/evaluate`, `GET /api/cards?q=...&limit=&offset=`,
`GET /api/cards/{card_id}/listings?max_results=`,
`GET|POST /api/watchlist`, `DELETE /api/watchlist/{card_id}`.

## The live feed (real-time deals)

The Deals tab is no longer a single search you wait for. While the app runs, a background feed:

1. **Fast lane, every minute:** asks eBay for the newest Pokémon listings and stops at the first one it already knows.
   About 37 new listings appear per minute and one page holds 200, so this is normally a single eBay call and
   misses nothing; if a whole page is new it fetches another (up to 3). New listings are valued and the likeliest
   deals checked straight away, so a fresh deal shows up within about a minute of being listed.
2. **Full cycle, hourly:** about a dozen broader queries, your watchlist cards, and two set names that
   rotate each cycle, to catch anything the fast lane's query does not reach;
3. stores every listing in the database, so deals **build up** instead of vanishing on the next refresh;
4. checks the deals with eBay's item call: it returns the seller's stated condition and whether the listing is
   still for sale. Sold or removed listings are deleted from the feed (and never re-added);
5. forgets what is no longer worth keeping: ended auctions, listings that are not deals after 48 hours, anything
   older than 30 days, and, once there are more than 300 deals, the **oldest** deals (newer ones push them out).

The page re-reads the stored deals every 20 seconds, so new deals appear on their own; "Check eBay now" runs a
cycle immediately. Deals sort by biggest savings, newest, or biggest resale profit.

### eBay API calls

eBay's published default for the Browse API is **5,000 calls a day** (every method except `getItems`, which has its own
5,000; see [API Call Limits](https://developer.ebay.com/develop/api/buy/api_call_limits)). A higher limit comes from
eBay's free *Application Growth Check*. The feed keeps itself to a budget of 4,000 (`feed_daily_call_budget`), half for
searching and half for item checks, so your own clicks have room. `GET /api/feed` shows today's calls per task
(`calls_by_task`).

| Task | Calls each | How often | About per day |
|---|---|---|---|
| Fast lane: newest listings | 1 (more only if over 200 appear in a minute) | about every 70 s | 1,200 |
| Full cycle: 15 broader queries | 15 | hourly | 360 |
| Check a new deal (condition, still for sale?) | 1 | the biggest savings first; small ones wait | up to the check budget |
| Re-check a deal ("has it sold?") | 1 | every 3 hours, paced | a few hundred |
| Open a listing popup | 1, cached for 10 minutes; also counts as a check | when you click | 1 each |
| A card's "eBay listings" panel | 5 (1 search + 4 to read sellers' conditions) | when you click | 5 each |

How it stays cheap: a listing's condition is only looked up when it could change its value (the valuation uses the
worse of the seller's condition and the title's, so a title that already states one can never be improved by asking);
checks go to the biggest savings first; and anything the popup learns is saved, so the feed does not repeat it.

eBay also offers `getItems` (up to 20 listings per call), but it is a *Limited Release* method for select partners
(scope `buy.item.bulk`); with the keys we have it answers `403 Insufficient permissions`. It would also return only
availability and price, not the seller's condition.

`POKEDEALS_FEED=0` turns the feed off (the Deals tab then runs a one-off scan instead).

## Prices by condition

The price feed has one price per print, so condition used to be a guess (Lightly Played = 85% of market). Real cards
are not that tidy: Leafeon (Call of Legends) is $51.66 Near Mint but $30.81 Lightly Played; a Base Set Charizard is
$972 NM, $529 LP, $247 HP.

tcgplayer.com's product page shows a **Market Price for each condition**, built from recent sales, with the number sold
in the last three months. The app reads the same data (one request per product returns every print and condition) and
values a listing at the market price of *its* condition. A condition with fewer than 2 sales in the period is not
trusted and keeps the old estimate. Results are cached for 12 hours, and only listings that could be deals are looked
up. The card dialog shows the full table: market price, sales, cheapest listing now, and how many are for sale.

Asking prices from TCGplayer's live listings were tried first and were wrong for thin cards (sellers ask far more than
cards sell for), so they are shown for context only and never used to value anything.

This uses the endpoints behind tcgplayer.com's product pages. They are not a documented public API and could change or
refuse requests; if that happens the app quietly keeps the estimates. Switch it off with `POKEDEALS_TCG_CONDITIONS=0`.

## How a listing is judged

This pipeline was tuned against real eBay listings (an audit of ~1,000 of them took the share flagged as
deals from 11.6% to 4.4%, almost all of it false positives that are now filtered). In order:

1. **Fetch**: raw (ungraded) cards only, filtered by eBay itself. Each listing is labelled with the seller's
   stated Card Condition (NM / LP / MP / HP) via eBay's aspect filter.
2. **Parse** the title: card number (`136/189`), 1st Edition / Shadowless / reverse / holo, condition.
   Skipped, with the reason shown: graded, lots (`2x`, `lot`, `bundle`), proxies, non-English, local
   pickup, and prints we hold no price for (World Championship decks, staff/prerelease/league stamped,
   miscuts, Pokémon Center, cosmos/cracked-ice holos).
3. **Match** number + set size to a card; the name must appear in the title (`Mega-Gengar-EX` and
   `DeoxysEX` are understood), otherwise confidence drops. **Reprints** are caught by wording: a title
   saying "30th Anniversary Celebrations" or "Celebrations Classic Collection" is matched inside that
   reprint set, never as Base Set's card.
4. **Value** against TCGplayer's market price *for that print* (Shadowless, 1st Edition and unlimited are
   priced apart), × a condition factor (the seller's stated condition, or the title's; when they
   disagree the worse one wins; unstated is assumed LP).
5. **Fake / scam check** (`pokedeals/suspicion.py`). A cheap listing is marked **Suspicious** and kept out of
   the deals when it has one *strong* warning sign or two *weak* ones:
   - strong: priced more than 65% under market; or a valuable card (over $50) shipping from China / Hong Kong
     when it is also cheap;
   - weak: 45-65% under market; a seller with under 50 feedback on a cheap listing; priced 60%+ under what
     other eBay sellers ask for the card; shipping from China / Hong Kong on a cheaper card.

   One weak sign alone is only a "Heads-up" on a real deal. Titles with fake wording (`metal`, `custom`,
   `inspired`, `fantasy`, `art card`, `not official`, ...) are skipped outright. Suspicious listings never set the
   "cheapest" or "typical" price and never count toward the eBay cross-check below. Thresholds are in
   `config.py` (`suspicious_discount`, `max_plausible_discount`, `new_seller_feedback`, ...).
6. **Gates**, each with a short label and a "Why?" explanation: match confidence; seller quality; auction
   window; discount ≥ 10% **and** ≥ $3; and an **eBay cross-check**: a deal must
   also beat the median of the other eBay listings of that card (put on a Near Mint footing), because
   TCGplayer can price a card well above where eBay trades it. The resale-profit estimate is capped at that
   typical eBay price for the same reason.

All thresholds live in `pokedeals/config.py`. Unknown shipping is assumed (and flagged), never treated as free.

## Tests

```bash
python -m pytest
```

## Status

| Part | State |
|---|---|
| TCGdex sync, SQLite | working, ~21k physical cards (digital TCG Pocket excluded) |
| TCGplayer price feed | working, ~99.4% of cards priced, unit-tested |
| Parser, matcher, scorer, resale calculator | working, unit-tested |
| `EbaySource` (live eBay) | working against **production** eBay: token, search, paging, condition labelling, shipping, parsing; matching audited on ~1,000 real listings |
| Web UI (Deals, Search, Sets, Watchlist, Tools) | working on sample data and on live production eBay (checked in the browser) |
| Fake / scam detection | working; checked on ~1,000 real listings (44 flagged) and a live Charizard search (68 of 197 flagged, nearly all new sellers at 90%+ off) |
| Alerts (Discord/email), scheduler | not started |

## Known limits

- Prices are a daily snapshot (the feed updates once a day), not real time.
- A few hundred cards stay unpriced where neither source has a match (odd promos, star cards).
- The feed is a third-party mirror of TCGplayer's data, not TCGplayer itself. If it is unreachable,
  `sync` keeps TCGdex's prices and says so.
- Cards without a printed number in the title are not matched (name-only matching is too ambiguous).
- Base Set Charizard etc.: TCGplayer lumps shadowless/unlimited under one price here; check `variant`.
