from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class Listing:
    id: str
    title: str
    price: float
    currency: str = "USD"
    shipping: float | None = None          # None = unknown (not the same as free)
    url: str = ""
    image: str | None = None
    buying_options: tuple[str, ...] = ("FIXED_PRICE",)
    end_time: datetime | None = None
    seller_feedback_pct: float | None = None
    seller_feedback_score: int | None = None
    condition: str | None = None            # eBay's "Ungraded" / "Graded" (not the card's NM/LP/...)
    graded: bool = False                    # eBay says this is a graded slab (conditionId 2750)
    pickup_only: bool = False               # no shipping offered, local pickup only
    card_condition: str | None = None       # seller-stated Card Condition aspect as NM/LP/MP/HP, when known
    seller_name: str | None = None
    item_country: str | None = None         # ISO country the item ships from (eBay's itemLocation), e.g. "US", "HK"

    @property
    def is_auction_only(self) -> bool:
        return "AUCTION" in self.buying_options and "FIXED_PRICE" not in self.buying_options

    @classmethod
    def from_browse_item(cls, item: dict[str, Any]) -> "Listing":
        """Build from an eBay Browse API `itemSummaries[]` entry (shape verified against live responses)."""
        price = item.get("price") or {}
        ship_opts = item.get("shippingOptions") or []
        # CALCULATED shipping has no cost unless the request carried the buyer's location
        ship_cost = ((ship_opts[0].get("shippingCost") or {}) if ship_opts else {}).get("value")
        seller = item.get("seller") or {}
        end = item.get("itemEndDate")
        return cls(
            id=item["itemId"],
            title=item.get("title", ""),
            price=float(price.get("value", 0)),
            currency=price.get("currency", "USD"),
            shipping=float(ship_cost) if ship_cost is not None else None,
            url=item.get("itemWebUrl", ""),
            image=(item.get("image") or {}).get("imageUrl"),
            buying_options=tuple(item.get("buyingOptions") or ("FIXED_PRICE",)),
            end_time=datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None,
            seller_feedback_pct=float(seller["feedbackPercentage"]) if seller.get("feedbackPercentage") else None,
            seller_feedback_score=int(seller["feedbackScore"]) if seller.get("feedbackScore") is not None else None,
            condition=item.get("condition"),
            # the condition NAME is translated for non-US viewers ("Valutata", "Gradée"); the id is not
            graded=str(item.get("conditionId") or "") == "2750",
            pickup_only=not ship_opts and bool(item.get("pickupOptions")),
            seller_name=seller.get("username"),
            item_country=(item.get("itemLocation") or {}).get("country"),
        )

    def to_json(self) -> dict[str, Any]:
        """Everything needed to rebuild this listing later (what the listing store keeps)."""
        d = asdict(self)
        d["end_time"] = self.end_time.isoformat() if self.end_time else None
        d["buying_options"] = list(self.buying_options)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Listing":
        known = {f for f in cls.__dataclass_fields__}
        d = {k: v for k, v in d.items() if k in known}
        d["buying_options"] = tuple(d.get("buying_options") or ("FIXED_PRICE",))
        if d.get("end_time"):
            d["end_time"] = datetime.fromisoformat(d["end_time"])
        return cls(**d)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Listing":
        """Flat fixture format (see data/sample_listings.json). `ends_in_hours` is relative to now."""
        end = None
        if d.get("ends_in_hours") is not None:
            end = datetime.now(timezone.utc) + timedelta(hours=float(d["ends_in_hours"]))
        return cls(
            id=str(d["id"]),
            title=d["title"],
            price=float(d["price"]),
            currency=d.get("currency", "USD"),
            shipping=d.get("shipping"),
            url=d.get("url", ""),
            buying_options=tuple(d.get("buying_options") or ("FIXED_PRICE",)),
            end_time=end,
            seller_feedback_pct=d.get("seller_feedback_pct"),
            seller_feedback_score=d.get("seller_feedback_score"),
            condition=d.get("condition"),
            graded=bool(d.get("graded", False)),
            pickup_only=bool(d.get("pickup_only", False)),
            card_condition=d.get("card_condition"),
            seller_name=d.get("seller_name"),
            item_country=d.get("item_country"),
        )
