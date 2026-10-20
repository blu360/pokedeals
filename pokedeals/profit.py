"""Resale profit estimate for selling ONE card on eBay (US, seller without a store).

What is verified (taken from eBay's own seller pages, Oct 2026) and what is only an estimate is
tracked per shipping tier (`verified`) so the UI can say so. Re-check these against eBay before
relying on them; they change:

verified
  * Final value fee, trading cards: 13.25% of the total sale up to $7,500, 2.35% above. The "total
    sale" is item price + shipping charged to the buyer + sales tax.
  * Per-order fee: $0.30 if the order is $10.00 or less, $0.40 above.
  * eBay Standard Envelope for single cards under $20: $0.60 / $0.84 / $1.08 for 1 / 2 / 3 oz.
  * Cards from $20 need a tracked service (eBay Labels); over $750 needs signature confirmation.
estimates (set in DEFAULT_TIERS, editable)
  * What tracked USPS Ground Advantage costs for a card in a bubble mailer (~$4.50-$5.50 depending
    on source/zone), and the signature confirmation fee.
  * Packaging (top loader, sleeve, mailer).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ShippingTier:
    upto: float | None      # highest item price (inclusive) this tier applies to; None = no limit
    label: str
    cost: float
    verified: bool          # False = estimate: confirm in eBay's label tool
    note: str = ""


# Prices are in cents, so 19.99 is "under $20". Boundaries follow eBay's trading card shipping page.
DEFAULT_TIERS: tuple[ShippingTier, ...] = (
    ShippingTier(19.99, "eBay Standard Envelope (tracked, up to 2 oz)", 0.84, True,
                 "Single cards under $20 only. $0.60 for 1 oz, $0.84 for 2 oz, $1.08 for 3 oz."),
    ShippingTier(250.00, "Tracked USPS Ground Advantage (eBay Labels)", 5.00, False,
                 "Cards $20 and up cannot use the Standard Envelope. Cost is an estimate."),
    ShippingTier(750.00, "Tracked USPS Ground Advantage + insurance", 5.00, False,
                 "Insurance is recommended above $250 but its cost is not included. Raw cards over "
                 "$250 are routed through eBay's Authenticity Guarantee, which can change shipping."),
    ShippingTier(None, "Tracked + signature confirmation", 8.50, False,
                 "eBay requires signature confirmation above $750. Fee ($3.50) is an estimate."),
)


@dataclass(frozen=True)
class FeeSchedule:
    fvf_rate: float = 0.1325
    fvf_cap: float = 7500.0
    fvf_rate_above_cap: float = 0.0235
    order_fee_threshold: float = 10.00      # "$10.00 or less"
    order_fee_small: float = 0.30
    order_fee_large: float = 0.40
    packaging: float = 0.50                 # estimate: top loader + sleeve + mailer
    tiers: tuple[ShippingTier, ...] = DEFAULT_TIERS


DEFAULT_FEES = FeeSchedule()


def select_tier(price: float, fees: FeeSchedule = DEFAULT_FEES) -> ShippingTier:
    for tier in fees.tiers:
        if tier.upto is None or price <= tier.upto + 1e-9:
            return tier
    return fees.tiers[-1]


@dataclass(frozen=True)
class ProfitBreakdown:
    sale_price: float
    shipping_charged: float
    sales_tax: float
    fee_base: float                 # what eBay's fees are charged on
    final_value_fee: float
    order_fee: float
    ad_fee: float
    total_fees: float
    net_proceeds: float             # what eBay pays out, before your postage and costs
    postage: float
    postage_label: str
    postage_verified: bool
    postage_overridden: bool
    packaging: float
    cost_basis: float
    profit: float
    roi: float | None               # profit / cost_basis; None when you paid nothing
    break_even_price: float | None  # lowest sale price that does not lose money
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["warnings"] = list(self.warnings)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 2) if k != "roi" else round(v, 4)
        return d


def _fees(fee_base: float, total_sale: float, ad_rate: float, fees: FeeSchedule) -> tuple[float, float, float]:
    fvf = fees.fvf_rate * min(fee_base, fees.fvf_cap) + fees.fvf_rate_above_cap * max(fee_base - fees.fvf_cap, 0.0)
    order = fees.order_fee_small if fee_base <= fees.order_fee_threshold else fees.order_fee_large
    return fvf, order, ad_rate * total_sale


def compute_profit(
    sale_price: float,
    cost_basis: float = 0.0,
    *,
    shipping_charged: float = 0.0,
    sales_tax_rate: float = 0.0,
    ad_rate: float = 0.0,
    packaging: float | None = None,
    postage: float | None = None,
    fees: FeeSchedule = DEFAULT_FEES,
    with_break_even: bool = True,
) -> ProfitBreakdown:
    """Net result of selling a card for `sale_price` that you bought for `cost_basis`.

    shipping_charged: what the buyer pays for shipping (0 = free shipping, priced into the card).
    sales_tax_rate:   buyer's sales tax as a fraction; eBay charges its fee on tax-inclusive totals.
    ad_rate:          promoted-listing fee as a fraction of the sale (0 = not promoted).
    postage/packaging: overrides for your real costs; otherwise the tier table / schedule is used.
    """
    total_sale = sale_price + shipping_charged
    sales_tax = total_sale * sales_tax_rate
    fee_base = total_sale + sales_tax
    fvf, order, ad = _fees(fee_base, total_sale, ad_rate, fees)
    total_fees = fvf + order + ad
    net = total_sale - total_fees          # buyer's sales tax is collected and remitted by eBay

    tier = select_tier(sale_price, fees)
    post_cost = tier.cost if postage is None else postage
    pack = fees.packaging if packaging is None else packaging
    profit = net - post_cost - pack - cost_basis

    warnings: list[str] = []
    if postage is None:
        if not tier.verified:
            warnings.append(f"Postage ${tier.cost:.2f} is an estimate: confirm it in eBay's label tool.")
        if sale_price > 250:          # insurance / Authenticity Guarantee / signature rules kick in
            warnings.append(tier.note)

    result = ProfitBreakdown(
        sale_price=sale_price, shipping_charged=shipping_charged, sales_tax=sales_tax, fee_base=fee_base,
        final_value_fee=fvf, order_fee=order, ad_fee=ad, total_fees=total_fees, net_proceeds=net,
        postage=post_cost, postage_label="Your own postage cost" if postage is not None else tier.label,
        postage_verified=postage is not None or tier.verified, postage_overridden=postage is not None,
        packaging=pack, cost_basis=cost_basis, profit=profit,
        roi=(profit / cost_basis) if cost_basis > 0 else None,
        break_even_price=None, warnings=tuple(warnings),
    )
    if not with_break_even:
        return result
    be = break_even_price(cost_basis, shipping_charged=shipping_charged, sales_tax_rate=sales_tax_rate,
                          ad_rate=ad_rate, packaging=packaging, postage=postage, fees=fees)
    return ProfitBreakdown(**{**result.__dict__, "break_even_price": be})


def break_even_price(
    cost_basis: float,
    *,
    shipping_charged: float = 0.0,
    sales_tax_rate: float = 0.0,
    ad_rate: float = 0.0,
    packaging: float | None = None,
    postage: float | None = None,
    fees: FeeSchedule = DEFAULT_FEES,
) -> float | None:
    """Lowest sale price (to the cent) at which the sale does not lose money, or None if there is none.

    Postage steps up with price (envelope -> tracked), so profit is not monotonic across the whole
    range; within a tier it is, so each tier is searched on its own, cheapest tier first.
    """
    def profit_at(price: float) -> float:
        return compute_profit(price, cost_basis, shipping_charged=shipping_charged,
                              sales_tax_rate=sales_tax_rate, ad_rate=ad_rate, packaging=packaging,
                              postage=postage, fees=fees, with_break_even=False).profit

    lower = 0.0
    for tier in fees.tiers:
        upper = tier.upto if tier.upto is not None else max(cost_basis * 3, 100.0) + 100.0
        if profit_at(upper) < 0:
            lower = upper
            continue
        a, b = lower, upper                       # profit(a) < 0 <= profit(b) (or a is the tier start)
        for _ in range(60):
            mid = (a + b) / 2
            if profit_at(mid) >= 0:
                b = mid
            else:
                a = mid
        cents = math.ceil(b * 100 - 1e-9) / 100
        while profit_at(cents) < 0 and cents < upper + 1:
            cents = round(cents + 0.01, 2)
        return round(cents, 2)
    return None
