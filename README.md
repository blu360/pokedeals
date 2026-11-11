# pokedeals

**Find Pokémon cards on eBay that are listed for less than they're worth.**

pokedeals watches new eBay listings for Pokémon TCG singles, works out which card each one is, compares the price with
what that card actually sells for on TCGplayer (in the listing's condition), and shows you the bargains as they appear.
It runs on your own computer and uses your own free eBay developer keys, so there is nothing to sign up for and nothing
to pay.

## What you get

- **A live list of deals**, updated about every minute, sorted newest first.
- **Honest labels** on every listing: *Steal*, *Deal*, *Fair*, *Overpriced*, or *Uncertain* when there isn't enough
  sales data to trust the price.
- **Fake and scam filtering.** Listings that are suspiciously cheap or from brand-new sellers are marked
  *Suspicious* instead of shown as deals.
- **Condition-aware prices.** A Heavily Played card is compared with Heavily Played prices, not Near Mint ones.
- **Alerts** to your desktop, email, Discord, Telegram or ntfy when a new deal appears.
- **Search, sets and a watchlist** to look up any card and follow the ones you care about.
- **A usage bar** so you always know how much of eBay's daily request limit you've used.

## Get started

You need **Python 3.11 or newer** and a **free eBay developer account** (the first start walks you through it).

```bash
git clone https://github.com/blu360/pokedeals.git
cd pokedeals
python start.py
```

That's all. The first time, `start.py`:

1. sets itself up (a private Python environment and the few libraries it needs),
2. asks for your eBay keys (see below) and, if you like, your ZIP code,
3. opens the site in your browser and downloads every card and its price in the background. A progress bar shows how
   far along it is; it takes a few minutes, once.

Next time, run `python start.py` again and it opens straight away.

> **Windows tip:** keep the folder path short (for example `C:\pokedeals`). Very long paths can cause errors.

### Getting your eBay keys

1. Sign in (or create an account) at [developer.ebay.com/my/keys](https://developer.ebay.com/my/keys).
2. Under **Production**, create a keyset.
3. Copy the **App ID** (also called Client ID) and the **Cert ID** (also called Client Secret).
4. Paste them when `start.py` asks. They are checked with eBay, then saved in your computer's secure keyring (or an
   owner-only file if you don't have one). They are never shown again or sent anywhere except to eBay.

New production keysets may need one extra step on eBay's side (the "Marketplace Account Deletion" notice; opting out
is fine, since this app stores no eBay user data). eBay's page tells you if it's needed.

Skipped the prompt? Run `python start.py` again whenever you're ready. You can also set `EBAY_CLIENT_ID` and
`EBAY_CLIENT_SECRET` as environment variables.

### Using Docker instead

```bash
cp .env.example .env        # put your eBay keys in .env
docker compose up -d
```

Then open <http://localhost:8765>. Your data is kept in a Docker volume, so updates don't lose your watchlist.

## Using the site

The site has six tabs.

### Deals

The main page. New listings appear on their own, with a short animation and a pop-up when one is a bargain.

| Label | Meaning |
|---|---|
| **Steal** | 25% or more under what the card is worth |
| **Deal** | 10–25% under |
| **Uncertain** | Looks cheap, but TCGplayer has too few recent sales to trust the price. Check eBay's sold listings (a link is provided). |
| **Fair** | About what it's worth |
| **Overpriced** | More than it's worth |
| **Suspicious** | Too good to be true, or a risky seller. Hidden unless you ask for it. |

Click any listing to see its photo next to the matching card, how the price was worked out, the seller, and the
seller's stated condition. Buttons open it on eBay, TCGplayer, or eBay's sold listings.

Use the filters to narrow things down: price range, auction or Buy It Now, condition, minimum savings, seller-confirmed
condition only, and international sellers (US sellers only by default).

### Search cards, Sets, Watchlist

- **Search cards:** look up any card and compare all of its current eBay listings.
- **Sets:** browse every set, then open a card to see its prices by condition.
- **Watchlist:** star the cards you care about. Tick "Only my watchlist" on the Deals page to see just those.

### Status

How much of eBay's daily limit you've used (the bar), which data sources are working, when prices were last updated,
your backups, and whether a newer version of pokedeals exists.

### Tools

- **Your ZIP code.** Lets eBay price "calculated" shipping, so deals use real shipping costs.
- **Notifications** (below).
- **Try a listing.** Paste a title and price to see how pokedeals would read and value it.

## Notifications

Under **Tools → Notifications**, choose whether you want *steals only* or *steals and deals*, then switch on any of:

| Channel | What you need |
|---|---|
| **Desktop** | Just click Turn on and allow notifications. Works while the page is open in a browser tab, even in the background. |
| **Email** | Any SMTP server (Gmail needs an app password). |
| **Discord** | A webhook address: Server settings → Integrations → Webhooks. |
| **Telegram** | A bot token from @BotFather, plus your chat ID. |
| **ntfy** | A topic name (pick a long random one) and the ntfy app. |

Every channel has a **Send a test** button. Each deal is announced once, and only if it showed up in the last half hour,
so turning a channel on won't flood you with old listings. Passwords and tokens are stored privately and never shown
again.

## Everyday commands

```bash
python start.py                # run the site
python start.py update         # get the newest version (nothing updates by itself)
python start.py backup         # save your watchlist and settings now
python start.py restore        # put the newest backup back
python start.py --port 9000    # use a different port
python start.py --no-browser   # don't open a browser tab
```

Your watchlist and settings are backed up automatically once a day (a few kilobytes) into `data/backups`. Card data
isn't backed up because it downloads again when needed.

## Questions

**How many eBay requests does it use?** The app keeps itself under 4,000 a day (eBay's default limit is 5,000), leaving
room for your own clicks. The Status tab shows exactly where they go.

**Why is a deal marked "Uncertain"?** TCGplayer has fewer than 5 recent sales of that card in that condition, so its
price may not be reliable. Use the "Sold on eBay" link to see what it really sells for.

**Why does a listing say the condition is "Unknown"?** The seller didn't say. The app assumes Lightly Played until it
has asked eBay, so the price shown may be off. Listings that state their condition in the title are never improved by
asking.

**Why are some cards missing?** Only English cards are shown, and graded cards (PSA, CGC and so on) are skipped for
now. Japanese cards and graded cards are planned.

**Is it safe to put on the internet?** No. The site has no login and is meant for your own computer only. Docker
publishes it to `localhost` only by default; keep it that way.

**It says the card database is empty.** Run `python start.py` and let the first download finish.

**Something looks wrong.** Open the **Status** tab. It shows whether eBay, the TCGplayer price feed or the condition
prices are having trouble, and a notice appears on the Deals page too.

## Where the data comes from

| Source | Used for |
|---|---|
| [eBay Browse API](https://developer.ebay.com/api-docs/buy/browse/overview.html) | Live listings (your own keys; 5,000 requests a day by default) |
| [TCGdex](https://tcgdex.dev) | The card catalogue: names, sets, numbers, images |
| [tcgcsv.com](https://tcgcsv.com) | Daily TCGplayer market prices |
| TCGplayer's website | Prices by condition. These are **unofficial** endpoints and could change without notice; if they stop working, the app falls back to the daily price and marks it as an estimate. |

Everything else (your keys, watchlist, ZIP, saved listings) stays on your computer. The only other requests go to
the notification services you turn on, and once a day to GitHub to check for a newer version (switch it off with
`update_check` in `config.py`).

## For developers

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Windows (.venv/bin/pip on macOS and Linux)
.venv/Scripts/python -m pytest                    # about 500 tests, no network needed
```

Built with Python, FastAPI, SQLite and a single plain HTML/JavaScript page. Settings live in
`pokedeals/config.py`. For how matching, valuation, the live feed and eBay API usage work in detail, see
[docs/how-it-works.md](docs/how-it-works.md). Optional extra price sources (for example graded cards) plug in through
`pokedeals/pricesources.py`.

## License

MIT. See [LICENSE](LICENSE). Pokémon and related names are trademarks of their owners; this project is not affiliated
with Nintendo, The Pokémon Company, eBay or TCGplayer.
