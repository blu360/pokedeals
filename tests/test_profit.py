"""Profit calculator. Expected numbers are worked out by hand from eBay's published fee schedule."""
import pytest

from pokedeals.profit import FeeSchedule, break_even_price, compute_profit, select_tier


def test_standard_envelope_under_20():
    # $8 card, paid $3. FVF 13.25% = 1.06; order fee 0.30 (order <= $10); postage 0.84; packaging 0.50
    r = compute_profit(8.00, 3.00)
    assert r.final_value_fee == pytest.approx(1.06)
    assert r.order_fee == 0.30
    assert r.net_proceeds == pytest.approx(8.00 - 1.06 - 0.30)
    assert r.postage == 0.84 and r.postage_verified and "Standard Envelope" in r.postage_label
    assert r.profit == pytest.approx(2.30)
    assert r.roi == pytest.approx(2.30 / 3.00)


def test_twenty_dollars_and_up_cannot_use_standard_envelope():
    assert "Standard Envelope" in select_tier(19.99).label
    assert "Standard Envelope" not in select_tier(20.00).label
    assert select_tier(20.00).cost == 5.00 and not select_tier(20.00).verified
    # crossing the line costs more postage: $20 nets less than $19.99 after postage differences
    assert compute_profit(20.00, 0).postage > compute_profit(19.99, 0).postage


def test_tracked_tier_hundred_dollar_card():
    # $100, paid $40: FVF 13.25, order fee 0.40 (> $10), postage 5.00 (est), packaging 0.50
    r = compute_profit(100.00, 40.00)
    assert r.final_value_fee == pytest.approx(13.25)
    assert r.order_fee == 0.40
    assert r.net_proceeds == pytest.approx(86.35)
    assert r.profit == pytest.approx(40.85)
    assert any("estimate" in w for w in r.warnings)


def test_signature_tier_over_750():
    r = compute_profit(800.00, 500.00)
    assert r.postage == 8.50 and "signature" in r.postage_label.lower()
    assert any("signature" in w.lower() for w in r.warnings)


def test_fee_drops_above_7500():
    r = compute_profit(10000.00, 0)
    assert r.final_value_fee == pytest.approx(0.1325 * 7500 + 0.0235 * 2500)   # 993.75 + 58.75


def test_buyer_shipping_and_tax_are_in_the_fee_base_but_tax_is_not_proceeds():
    # $100 + $5 shipping charged, 8% tax -> fees on 105 * 1.08 = 113.40
    r = compute_profit(100.00, 0, shipping_charged=5.00, sales_tax_rate=0.08)
    assert r.fee_base == pytest.approx(113.40)
    assert r.final_value_fee == pytest.approx(0.1325 * 113.40)
    assert r.net_proceeds == pytest.approx(105.00 - r.total_fees)    # tax is remitted by eBay, not paid out


def test_ad_fee_and_overrides():
    r = compute_profit(100.00, 40.00, ad_rate=0.03, packaging=1.00, postage=4.00)
    assert r.ad_fee == pytest.approx(3.00)
    assert r.postage == 4.00 and r.postage_overridden and r.postage_verified
    assert r.profit == pytest.approx(100 - 13.25 - 0.40 - 3.00 - 4.00 - 1.00 - 40.00)
    assert not r.warnings


def test_roi_none_when_nothing_paid():
    assert compute_profit(50.00, 0).roi is None


def test_break_even_in_tracked_tier():
    # P*(1-0.1325) - 0.40 - 5.00 - 0.50 - 40 = 0  ->  P = 45.90 / 0.8675 = 52.9107..., so 52.92 to the cent
    be = break_even_price(40.00)
    assert be == 52.92
    assert compute_profit(52.91, 40.00).profit < 0          # one cent lower still loses (by $0.0006)
    assert compute_profit(be, 40.00).profit >= 0


def test_break_even_in_envelope_tier_and_matches_profit_field():
    be = break_even_price(3.00)               # needs to clear ~ $4.7: still under $20
    assert 4.0 < be < 8.0
    assert compute_profit(be, 3.00).profit >= 0
    assert compute_profit(8.00, 3.00).break_even_price == be


def test_break_even_skips_over_the_tier_cliff():
    # Paid $18.50: in the envelope tier the break-even would be ~ $22 (> $19.99), so it must be
    # found in the tracked tier, where postage is higher.
    be = break_even_price(18.50)
    assert be >= 20.00
    assert compute_profit(be, 18.50).profit >= 0
    assert compute_profit(round(be - 0.01, 2), 18.50).profit < 0


def test_custom_schedule():
    cheap = FeeSchedule(fvf_rate=0.10, order_fee_large=0.0, packaging=0.0)
    r = compute_profit(100.00, 0, fees=cheap)
    assert r.final_value_fee == pytest.approx(10.00) and r.order_fee == 0.0
