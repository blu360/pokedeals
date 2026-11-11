"""Listing sources. Both expose `search(query) -> SearchResult` so the pipeline doesn't care which.

- FixtureSource: reads a JSON file (for tests and the bundled samples).
- EbaySource:    eBay Browse API (OAuth client credentials). It pages through results (offset/limit) and
                 reports how many matched in total, so a caller can tell "that is everything" from
                 "the first N of many".
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from .config import SETTINGS, Settings
from .models import Listing

_HOSTS = {"production": "https://api.ebay.com", "sandbox": "https://api.sandbox.ebay.com"}
_TOKEN_PATH = "/identity/v1/oauth2/token"
_SEARCH_PATH = "/buy/browse/v1/item_summary/search"
_SCOPE = "https://api.ebay.com/oauth/api_scope"      # the same scope string is used in the sandbox

# The seller's "Card Condition" item specific, as eBay spells it, mapped to our codes (best to worst).
_CARD_CONDITIONS = {
    "Near Mint or Better": "NM",
    "Lightly Played (Excellent)": "LP",
    "Moderately Played (Very Good)": "MP",
    "Heavily Played (Poor)": "HP",
}
_CONDITION_ORDER = ["NM", "LP", "MP", "HP"]


def card_condition_code(text: str | None) -> str | None:
    """'Moderately played (Very good)' / 'Near mint or better' / ... -> NM/LP/MP/HP/DMG (None if unrecognised)."""
    t = (text or "").lower()
    for word, code in (("near mint", "NM"), ("lightly", "LP"), ("moderately", "MP"), ("heavily", "HP"), ("damaged", "DMG")):
        if word in t:
            return code
    return None

_PAGE_SIZE = 200          # Browse API maximum per request
_API_RESULT_WINDOW = 10_000   # the Browse API cannot page past offset + limit = 10,000


@dataclass(frozen=True)
class SearchResult:
    listings: list[Listing] = field(default_factory=list)
    total: int | None = None    # how many results matched in total; None = unknown

    @property
    def truncated(self) -> bool:
        """True when more results matched than we fetched."""
        return self.total is not None and self.total > len(self.listings)


@dataclass(frozen=True)
class ItemInfo:
    """What eBay's getItem says about one listing (cheaper and more exact than a search for it)."""
    gone: bool                          # sold, ended or removed: stop tracking it
    card_condition: str | None = None   # seller-stated Card Condition as NM/LP/MP/HP
    english: bool | None = None         # the seller's Language item specific, when given
    sold: int = 0                       # eBay's estimated sold quantity
    price: float | None = None          # current price (can change)


class ListingSource(Protocol):
    def search(self, query: str, *, max_results: int | None = None) -> SearchResult: ...


class FixtureSource:
    def __init__(self, path: Path):
        self.path = Path(path)

    def search(self, query: str = "", *, max_results: int | None = None) -> SearchResult:
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        listings = [Listing.from_dict(r) for r in rows]
        return SearchResult(listings[:max_results] if max_results else listings, total=len(listings))


class EbaySource:
    def __init__(self, settings: Settings = SETTINGS, client: httpx.Client | None = None):
        if not (settings.ebay_client_id and settings.ebay_client_secret):
            raise RuntimeError(
                "Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (eBay developer 'App ID' and 'Cert ID')."
            )
        self.s = settings
        self.env = settings.ebay_env
        self._host = _HOSTS[self.env]
        self._client = client or httpx.Client(timeout=30)
        self._token: str | None = None
        self._token_expiry = 0.0
        self.buyer_zip = settings.ebay_buyer_zip.strip()     # changeable at runtime (the UI's ZIP field)
        self.calls = 0                                       # HTTP requests made, for the daily budget

    @property
    def is_sandbox(self) -> bool:
        return self.env == "sandbox"

    @staticmethod
    def _check(r: httpx.Response) -> None:
        """raise_for_status, but with eBay's own explanation (never the request, which holds credentials)."""
        if not r.is_error:
            return
        detail = ""
        try:
            body = r.json()
            err = (body.get("errors") or [{}])[0]
            detail = err.get("message") or err.get("longMessage") or body.get("error_description") or ""
        except ValueError:
            pass
        raise httpx.HTTPStatusError(f"eBay returned {r.status_code}" + (f": {detail}" if detail else ""),
                                    request=r.request, response=r)

    def _auth(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        basic = base64.b64encode(f"{self.s.ebay_client_id}:{self.s.ebay_client_secret}".encode()).decode()
        r = self._client.post(
            self._host + _TOKEN_PATH,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": _SCOPE},
        )
        self._check(r)
        body = r.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 7200))
        return self._token

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._auth()}",
            "X-EBAY-C-MARKETPLACE-ID": self.s.ebay_marketplace,
        }
        zip_code = self.buyer_zip.strip()
        if re.fullmatch(r"\d{5}", zip_code):
            # A destination lets eBay price "calculated" shipping. The value must be URL-encoded; the raw
            # "country=US,zip=..." form is silently ignored.
            headers["X-EBAY-C-ENDUSERCTX"] = "contextualLocation=" + quote(f"country=US,zip={zip_code}", safe="")
        return headers

    def _filter(self) -> str:
        parts = ["priceCurrency:USD"]
        if self.s.ebay_ungraded_only:
            parts.append("conditionIds:{4000}")      # 4000 = Ungraded, 2750 = Graded (CCG singles)
        return ",".join(parts)

    def _page(self, query: str, offset: int, limit: int, aspect: str | None = None) -> dict:
        r = self._client.get(
            self._host + _SEARCH_PATH,
            headers=self._headers(),
            params={
                "q": query,
                **({"category_ids": self.s.ebay_category_ids} if self.s.ebay_category_ids else {}),
                "sort": "newlyListed",
                "limit": limit,
                "offset": offset,
                "filter": self._filter(),
                # restrict to one seller-stated Card Condition (used to label listings, see _label_conditions)
                **({"aspect_filter": self._aspect_filter(aspect)} if self._aspect_filter(aspect) else {}),
            },
        )
        self.calls += 1
        self._check(r)
        return r.json()

    def get_item(self, item_id: str) -> ItemInfo:
        """Is this listing still for sale, and what condition does the seller say?

        A 404 means it is gone (sold, ended or removed). Other errors raise, so a hiccup is never mistaken for
        a sale.
        """
        r = self._client.get(self._host + "/buy/browse/v1/item/" + quote(item_id, safe=""), headers=self._headers())
        self.calls += 1
        if r.status_code == 404:
            return ItemInfo(gone=True)
        self._check(r)
        j = r.json()
        avail = (j.get("estimatedAvailabilities") or [{}])[0]
        aspects = {a.get("name"): a.get("value") for a in j.get("localizedAspects") or []}
        # The page's "Card Condition" is a condition DESCRIPTOR; older listings carry it as an item specific,
        # so look in both.
        stated = next((v.get("content") for d in j.get("conditionDescriptors") or []
                       if d.get("name") == "Card Condition" for v in d.get("values") or []), None)
        language = aspects.get("Language")
        price = (j.get("price") or {}).get("value")
        return ItemInfo(
            gone=avail.get("estimatedAvailabilityStatus") in ("OUT_OF_STOCK", "UNAVAILABLE"),
            card_condition=card_condition_code(stated) or card_condition_code(aspects.get("Card Condition")),
            english=None if language is None else language.strip().lower() == "english",
            sold=int(avail.get("estimatedSoldQuantity") or 0),
            price=float(price) if price is not None else None,
        )

    def get_item_details(self, item_id: str) -> dict | None:
        """Everything the listing popup shows: all photos, the seller's condition notes and item specifics
        (set, number, rarity, finish, language). None when eBay no longer has the listing."""
        r = self._client.get(self._host + "/buy/browse/v1/item/" + quote(item_id, safe=""), headers=self._headers())
        self.calls += 1
        if r.status_code == 404:
            return None
        self._check(r)
        j = r.json()
        images = [(j.get("image") or {}).get("imageUrl"), *((i or {}).get("imageUrl") for i in j.get("additionalImages") or [])]
        notes = [n for d in j.get("conditionDescriptors") or [] if d.get("name") == "Card Condition"
                 for v in d.get("values") or [] for n in v.get("additionalInfo") or []]
        seller = j.get("seller") or {}
        aspects = {a.get("name"): a.get("value") for a in j.get("localizedAspects") or []}
        stated = next((v.get("content") for d in j.get("conditionDescriptors") or []
                       if d.get("name") == "Card Condition" for v in d.get("values") or []), None)
        return {
            "card_condition": card_condition_code(stated) or card_condition_code(aspects.get("Card Condition")),
            "images": list(dict.fromkeys(i for i in images if i)),
            "condition_notes": notes,
            "aspects": {a.get("name"): a.get("value") for a in j.get("localizedAspects") or []},
            "seller": {"name": seller.get("username"), "feedback_pct": seller.get("feedbackPercentage"),
                       "feedback_score": seller.get("feedbackScore")},
            "location": (j.get("itemLocation") or {}).get("country"),
            "returns": (j.get("returnTerms") or {}).get("returnsAccepted"),
            "description": (j.get("shortDescription") or "")[:400] or None,
        }

    def _aspect_filter(self, card_condition: str | None = None) -> str | None:
        """eBay's aspect_filter: the languages we want, plus (when labelling) one seller-stated Card Condition."""
        parts = []
        if card_condition:
            parts.append(f"Card Condition:{{{card_condition}}}")
        if self.s.ebay_languages:
            parts.append("Language:{" + "|".join(self.s.ebay_languages) + "}")
        return f"categoryId:{self._category()}," + ",".join(parts) if parts and self._category() else None

    def _category(self) -> str:
        return self.s.ebay_category_ids.split(",")[0].strip()

    def _label_conditions(self, query: str, listings: list[Listing], cap: int) -> dict[str, str]:
        """Which of `listings` the seller marked NM / LP / MP / HP.

        eBay only exposes the seller's "Card Condition" as a FILTER, not on each result. So search once per
        condition (same query, newest first) and tag the IDs that come back: about 5 calls per card instead of 1,
        and 96% of real raw listings get a label. A listing matching several values keeps the worst. ("Not
        Specified" is a no-op filter, so it is not used; unlabelled listings stay None.)
        """
        wanted = {listing.id for listing in listings}
        labels: dict[str, str] = {}
        for value, code in _CARD_CONDITIONS.items():
            offset = 0
            while offset < cap:
                page = self._page(query, offset, min(_PAGE_SIZE, cap - offset), aspect=value)
                items = page.get("itemSummaries") or []
                if not items:
                    break
                for item in items:
                    iid = item["itemId"]
                    if iid in wanted and (iid not in labels
                                          or _CONDITION_ORDER.index(code) > _CONDITION_ORDER.index(labels[iid])):
                        labels[iid] = code
                offset += len(items)
                total = page.get("total")
                if total is not None and offset >= total:
                    break
        return labels

    def search_newest(self, query: str, is_known, max_pages: int = 3) -> SearchResult:
        """Newest listings first, stopping at the first page with one we already know.

        The cheap way to catch everything new since the last look: usually ONE call. Only if a whole page is new
        (200+ appeared since last time) does it fetch more, up to `max_pages`. `is_known(listing_id)` says whether
        a listing is already stored.
        """
        listings: list[Listing] = []
        seen: set[str] = set()
        total: int | None = None
        for page_no in range(max_pages):
            page = self._page(query, page_no * _PAGE_SIZE, _PAGE_SIZE)
            total = page.get("total", total)
            items = page.get("itemSummaries") or []
            fresh = [Listing.from_browse_item(i) for i in items]
            listings += [l for l in fresh if not (l.id in seen or seen.add(l.id))]
            if len(items) < _PAGE_SIZE or any(is_known(l.id) for l in fresh):
                break
        return SearchResult(listings, total)

    def search(self, query: str, *, max_results: int | None = None, label: bool | None = None) -> SearchResult:
        """Fetch up to `max_results` listings (default: settings.ebay_max_listings_per_query).

        `label` overrides settings.ebay_label_conditions: the background feed skips the 4 extra calls per search
        and checks likely deals with `get_item` instead.
        """
        cap = min(max_results or self.s.ebay_max_listings_per_query, _API_RESULT_WINDOW)
        listings: list[Listing] = []
        seen: set[str] = set()
        total: int | None = None
        offset = 0
        while len(listings) < cap:
            page = self._page(query, offset, min(_PAGE_SIZE, cap - len(listings)))
            total = page.get("total", total)
            items = page.get("itemSummaries") or []
            if not items:
                break
            for item in items:
                listing = Listing.from_browse_item(item)
                if listing.id not in seen:     # new listings shift offsets between calls
                    seen.add(listing.id)
                    listings.append(listing)
            offset += len(items)
            if total is not None and offset >= total:
                break
        listings = listings[:cap]
        if (self.s.ebay_label_conditions if label is None else label) and listings and self._category():
            try:
                labels = self._label_conditions(query, listings, cap)
            except httpx.HTTPError:
                labels = {}          # labelling is an enhancement: never lose the listings because it failed
            listings = [replace(l, card_condition=labels.get(l.id)) for l in listings]
        return SearchResult(listings, total)
