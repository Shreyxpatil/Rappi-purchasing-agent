from app.engine.constraints import capacity_caps, evaluate_constraints
from app.engine.types import Delivery, Receipt
from app.fixtures import load_fixture
from fixture_inputs import plan_context, terms


def _codes(report) -> set[str]:
    return {v.code for v in report.violations}


def test_s1_overstock_caps_match_hand_computation() -> None:
    fx = load_fixture("s1_overstock")
    c = fx.expected.computed
    caps = capacity_caps(plan_context(fx), day=3, unit_cost=4200, case_pack=12)
    assert caps.level_before_arrival == c["pre_arrival_level"]
    assert caps.storage_free == c["storage_free_at_arrival"]
    assert (caps.max_qty_storage, caps.max_qty_budget, caps.max_qty_cover) == (
        c["max_qty_storage"], c["max_qty_budget"], c["max_qty_cover"])


def test_s1_recommendation_of_800_breaks_storage_budget_and_cover() -> None:
    fx = load_fixture("s1_overstock")
    r = evaluate_constraints([Delivery(day=3, qty=800)], 4200, 12, plan_context(fx))
    assert _codes(r) == {"STORAGE_EXCEEDED", "BUDGET_EXCEEDED", "OVERSTOCK"}
    storage = next(v for v in r.violations if v.code == "STORAGE_EXCEEDED")
    assert storage.detail["max"] == 672 and storage.hard and not storage.overridable
    assert r.cover_at_arrival == [fx.expected.computed["rec_cover"]]
    assert r.value == fx.expected.computed["rec_value"]
    assert r.blocked


def test_s1_order_of_240_passes_and_removes_the_stockout() -> None:
    fx = load_fixture("s1_overstock")
    r = evaluate_constraints([Delivery(day=3, qty=240)], 4200, 12, plan_context(fx))
    assert r.violations == []
    assert r.value == fx.expected.computed["order_value"]
    assert r.cover_at_arrival == [fx.expected.computed["cover_after_order"]]
    assert r.projection.stockout_day is None and r.projection.end_levels == [100, 40, 100, 280, 220]


def test_s4_budget_violation_is_overridable_and_reports_the_gap() -> None:
    fx = load_fixture("s4_budget_binding")
    r = evaluate_constraints([Delivery(day=2, qty=714)], 5800, 6, plan_context(fx))
    assert _codes(r) == {"BUDGET_EXCEEDED"}
    v = r.violations[0]
    assert v.overridable and v.detail["over_by"] == 2141200 and v.detail["max_qty"] == 342
    assert r.needs_override and not r.blocked


def test_s4_storage_single_delivery_blocked_split_fits() -> None:
    fx = load_fixture("s4_storage_binding")
    c = fx.expected.computed
    ctx = plan_context(fx)
    single = evaluate_constraints([Delivery(day=3, qty=444)], 4200, 12, ctx)
    assert single.blocked and single.violations[0].detail["max"] == c["max_qty_storage"]

    split = evaluate_constraints([Delivery(**d) for d in c["split_deliveries"]], 4200, 12, ctx)
    assert split.violations == []
    assert split.projection.end_levels == c["projection_with_split"]
    after_first = plan_context(fx, extra_receipts=[Receipt(day=3, qty=360, source="plan")])
    assert capacity_caps(after_first, day=4, unit_cost=4200, case_pack=12).storage_free == c["storage_free_day4"]


def test_s2_alternate_moq_order_overstocks_but_fits_storage() -> None:
    fx = load_fixture("s2_alt_moq_exceeds_gap")
    c = fx.expected.computed
    t = terms(fx, "SUP-CEDA", "LECHE-LALA-1L")
    r = evaluate_constraints([Delivery(day=2, qty=c["alt_order_qty"])], t["unit_cost"], t["case_pack"],
                             plan_context(fx, supplier="SUP-CEDA"))
    assert _codes(r) == {"OVERSTOCK"}
    assert r.cover_at_arrival == [c["alt_cover_after"]]
    assert not r.blocked and not r.needs_override
    caps = capacity_caps(plan_context(fx, supplier="SUP-CEDA"), 2, t["unit_cost"], t["case_pack"])
    assert caps.storage_free == c["alt_storage_free"]


def test_missing_constraint_data_is_a_hard_violation() -> None:
    fx = load_fixture("s1_overstock")
    ctx = plan_context(fx).model_copy(update={"storage": None, "budget_remaining": None})
    r = evaluate_constraints([Delivery(day=3, qty=240)], 4200, 12, ctx)
    assert [v.detail["what"] for v in r.violations] == ["storage_capacity", "budget"]
    assert r.blocked
