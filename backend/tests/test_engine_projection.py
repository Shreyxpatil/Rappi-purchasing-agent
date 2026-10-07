import pytest

from app.engine.projection import cover_days, project_inventory
from app.engine.types import EngineInputError, Receipt
from app.fixtures import load_fixture
from fixture_inputs import forecast, inventory, receipts


def _available(fx, node=None, sku=None):
    inv = inventory(fx, node or fx.trigger.node, sku or fx.trigger.sku)
    return inv.on_hand - inv.reserved


# (fixture, computed key, extra hypothetical receipts, demand basis override)
CASES = [
    ("s1_overstock", "projection_no_order", [], None),
    ("s2_partial_enough", "projection_no_order", [], None),
    ("s2_partial_needs_alternate", "projection_no_order", [], None),
    ("s2_partial_needs_alternate", "projection_with_order", [Receipt(day=2, qty=204, source="new")], None),
    ("s2_alt_moq_exceeds_gap", "projection_no_order", [], None),
    ("s2_alt_moq_exceeds_gap", "projection_with_transfer", [Receipt(day=1, qty=80, source="TRANSFER")], None),
    ("s4_budget_override_rejected", "projection_with_fallback", [Receipt(day=2, qty=342, source="new")], None),
    ("s4_storage_binding", "projection_with_split",
     [Receipt(day=3, qty=360, source="new"), Receipt(day=4, qty=84, source="new")], None),
    ("s3_real_surge", "projection_no_order_adjusted", [], [64.0] * 14),
]


@pytest.mark.parametrize("fx_id,key,extra,basis", CASES, ids=[f"{c[0]}:{c[1]}" for c in CASES])
def test_matches_fixture_projection(fx_id, key, extra, basis) -> None:
    fx = load_fixture(fx_id)
    expected = fx.expected.computed[key]
    p = project_inventory(
        _available(fx),
        basis or forecast(fx, fx.trigger.node, fx.trigger.sku),
        receipts(fx, fx.trigger.node, fx.trigger.sku) + extra,
        days=len(expected),
    )
    assert p.end_levels == expected


def test_budget_case_prefix_and_stockout() -> None:
    fx = load_fixture("s4_budget_binding")
    p = project_inventory(_available(fx), forecast(fx, "BOG-02", "COCA-1.5L"), [], days=9)
    assert p.end_levels[:4] == fx.expected.computed["projection_no_order"]
    assert p.stockout_day == 3


def test_stockout_day_and_unmet_units() -> None:
    fx = load_fixture("s4_budget_override_rejected")
    p = project_inventory(_available(fx), forecast(fx, "BOG-02", "COCA-1.5L"), [Receipt(day=2, qty=342)], days=9)
    assert p.stockout_day == fx.expected.computed["fallback_stockout_day"]
    assert p.unmet_units == fx.expected.computed["fallback_unmet_units"]


def test_start_level_includes_same_day_receipts() -> None:
    p = project_inventory(100, [60, 60, 60], [Receipt(day=1, qty=50), Receipt(day=1, qty=30)], days=3)
    assert p.start_levels == [100, 120, 60]
    assert p.end_levels == [40, 60, 0]
    assert p.stockout_day is None and p.unmet_units == 0


def test_rejects_receipts_in_the_past() -> None:
    with pytest.raises(EngineInputError):
        project_inventory(100, [10] * 3, [Receipt(day=-1, qty=5)], days=3)


def test_cover_days() -> None:
    assert cover_days(340, 60) == 5.67
    assert cover_days(10, 0) == float("inf")
