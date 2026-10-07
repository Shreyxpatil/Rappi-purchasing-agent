import pytest

from app.engine.decision import build_decision, derive_outcome
from app.engine.options import generate_options
from app.engine.quality import assess_freshness, check_consistency, inventory_sensitivity
from app.fixtures import load_fixture
from fixture_inputs import net_input, options_input, sales_days
from test_engine_options import FIRST_PLAN, _basis


def _decide(fx, **opts_overrides):
    inp = options_input(fx, basis=_basis(fx), **opts_overrides)
    options = generate_options(inp)
    return options, build_decision(options, options.options[0], recommendation=inp.recommendation)


@pytest.mark.parametrize("fx_id", FIRST_PLAN)
def test_top_option_yields_expected_outcome_and_quantity(fx_id) -> None:
    fx = load_fixture(fx_id)
    _, d = _decide(fx)
    assert d.outcome == fx.expected.outcome
    assert fx.expected.qty_min <= d.quantity <= fx.expected.qty_max


def test_s1_overstock_decision_explains_the_recommendation_failure() -> None:
    _, d = _decide(load_fixture("s1_overstock"))
    rec = next(f for f in d.factors if f.name == "recommendation")
    assert rec.value == "800 units (233.33% from own)"
    assert rec.effect.startswith("blocked by ") and "STORAGE_EXCEEDED" in rec.effect
    failed = {c.name for c in d.recommendation_check if c.status == "fail"}
    assert failed == {"CASE_PACK", "STORAGE", "BUDGET"} and any(
        c.name == "COVER" and c.status == "warning" for c in d.recommendation_check)
    assert all(c.status == "pass" for c in d.constraints_checked)
    assert d.confidence == "high" and d.residual_risk is None
    assert d.alternatives[0] == "BUY:SUP-ANDINA:144"


def test_s4_budget_decision_shows_override_and_fallback() -> None:
    options, d = _decide(load_fixture("s4_budget_binding"))
    assert d.outcome == "MODIFY" and d.quantity == 714 and d.confidence == "medium"
    assert next(c for c in d.constraints_checked if c.name == "BUDGET").status == "fail"
    approved = build_decision(options, options.options[0], overridden=frozenset({"BUDGET_EXCEEDED"}))
    assert next(c for c in approved.constraints_checked if c.name == "BUDGET").status == "overridden"
    assert d.alternatives[0] == "BUY:SUP-FEMSA-CO:342"


def test_override_refused_carries_residual_risk() -> None:
    fx = load_fixture("s4_budget_override_rejected")
    _, d = _decide(fx, excluded_option_ids=["BUY:SUP-FEMSA-CO:714"], refused_overrides=["BUDGET_EXCEEDED"])
    assert (d.outcome, d.quantity) == ("MODIFY", 342)
    assert d.residual_risk == fx.expected.residual_risk


def test_stale_inventory_flips_the_decision_so_investigate() -> None:
    fx = load_fixture("s1_stale_inventory")
    c = fx.expected.computed
    issues = assess_freshness({"inventory": 72, "forecast": 6, "supplier_terms": 72},
                              {"inventory": 24, "forecast": 48, "supplier_terms": 720})
    assert [(i.code, i.source) for i in issues] == [("STALE_DATA", "inventory")]
    sold = sum(s.units for s in sales_days(fx, "BOG-02", "LECHE-ALQ-1L") if s.day >= -3)
    sens = inventory_sensitivity(net_input(fx), sold)
    assert sold == c["sales_since_count"]
    assert (sens.as_recorded.net, sens.adjusted.net, sens.adjusted.order_qty) == (
        c["net"], c["net_adjusted"], c["order_qty_adjusted"])
    assert sens.decision_flips

    options = generate_options(options_input(fx))
    d = build_decision(options, None, investigate=True, data_issues=issues,
                       information_needed=["fresh cycle count of LECHE-ALQ-1L at BOG-02"])
    assert (d.outcome, d.quantity, d.option_id, d.confidence) == ("INVESTIGATE", 0, None, "low")
    assert d.information_needed == ["fresh cycle count of LECHE-ALQ-1L at BOG-02",
                                    "fresh inventory data (last updated 72h ago, limit 24h)"]


def test_choosing_the_own_quantity_equal_to_recommendation_is_accept() -> None:
    fx = load_fixture("x_supplier_rejects")  # recommendation 240 == own order 240
    inp = options_input(fx)
    options = generate_options(inp)
    buy = next(o for o in options.options if o.id == "BUY:SUP-ALQ:240")
    assert derive_outcome(buy, investigate=False, recommendation=inp.recommendation) == "ACCEPT"


def test_escalate_and_no_recommendation_rules() -> None:
    fx = load_fixture("s3_real_surge")
    options = generate_options(options_input(fx, basis=_basis(fx)))
    escalate = next(o for o in options.options if o.kind == "ESCALATE")
    no_action = next(o for o in options.options if o.kind == "NO_ACTION")
    assert derive_outcome(escalate, investigate=False, recommendation=None) == "INVESTIGATE"
    assert derive_outcome(no_action, investigate=False, recommendation=None) == "ACCEPT"


def test_conflicting_inventory_is_flagged() -> None:
    assert [i.code for i in check_consistency(on_hand=-5, reserved=0)] == ["CONFLICTING_DATA"]
    assert check_consistency(on_hand=10, reserved=20)[0].detail == {"on_hand": 10, "reserved": 20}
    assert check_consistency(on_hand=10, reserved=2) == []


def test_stale_data_that_does_not_change_the_order_proceeds_and_is_recorded() -> None:
    from app.engine.quality import data_blocks_decision

    fx = load_fixture("s1_overstock")  # same position, but pretend the count is 30h old
    issues = assess_freshness({"inventory": 30}, {"inventory": 24})
    sens = inventory_sensitivity(net_input(fx), units_sold_since_count=60)  # one day of sales since the count
    assert sens.as_recorded.net == 140 and sens.adjusted.net == 200
    assert sens.as_recorded.order_qty == sens.adjusted.order_qty == 240  # MOQ absorbs the uncertainty
    assert not data_blocks_decision(issues, sens)

    inp = options_input(fx)
    options = generate_options(inp)
    d = build_decision(options, options.options[0], recommendation=inp.recommendation, data_issues=issues,
                       sensitivity=sens)
    assert (d.outcome, d.quantity) == ("MODIFY", 240)  # proceeds
    names = {f.name: f for f in d.factors}
    assert names["inventory data"].value == "STALE_DATA: 30h old (limit 24h)"
    assert names["stock count sensitivity"].effect == "decision unchanged: proceed"
    assert d.residual_risk == {"stale_inventory_age_hours": 30}
    assert d.confidence == "medium"


def test_data_blocks_decision_rules() -> None:
    from app.engine.quality import data_blocks_decision

    fx = load_fixture("s1_stale_inventory")
    stale = assess_freshness({"inventory": 72}, {"inventory": 24})
    flips = inventory_sensitivity(net_input(fx), 180)
    assert data_blocks_decision(stale, flips)  # order changes 0 -> 240: investigate
    assert data_blocks_decision(stale, None)  # cannot tell: investigate
    assert data_blocks_decision(assess_freshness({"budget": None}, {}), None)  # missing data
    assert data_blocks_decision(check_consistency(on_hand=-1, reserved=0), None)  # conflicting data
    stale_forecast = assess_freshness({"forecast": 60}, {"forecast": 48})
    assert not data_blocks_decision(stale_forecast, None)  # recorded as weaker evidence, does not block
