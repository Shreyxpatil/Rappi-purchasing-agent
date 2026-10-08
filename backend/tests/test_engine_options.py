import pytest

from app.engine.demand import apply_promotions, detect_demand_shift, run_rate_forecast
from app.engine.options import generate_options
from app.fixtures import load_fixture
from fixture_inputs import forecast, options_input, promo_windows, sales_days


def _basis(fx):
    """Demand basis the engine's own demand signal suggests (what a sensible agent would pick)."""
    node, sku = fx.trigger.node, fx.trigger.sku
    if fx.scenario != "S3":
        return None
    sig = detect_demand_shift(sales_days(fx, node, sku), promo_windows(fx, node, sku))
    if sig.suggested_basis == "recent_run_rate":
        return run_rate_forecast(sig.adjusted_daily, 14)
    if sig.suggested_basis == "promo_adjusted":
        return apply_promotions(forecast(fx, node, sku), promo_windows(fx, node, sku))
    return None


# Cases decided on the first plan (no replan needed); the top-ranked option must be the expected action.
FIRST_PLAN = ["s1_overstock", "s1_accept_correct", "s1_already_covered", "s2_partial_enough",
              "s2_partial_needs_alternate", "s2_alt_moq_exceeds_gap", "s3_real_surge", "s3_one_off_outlier",
              "s3_promo_uplift", "s4_budget_binding", "s4_storage_binding", "x_prompt_injection"]


@pytest.mark.parametrize("fx_id", FIRST_PLAN)
def test_top_option_is_the_expected_action(fx_id) -> None:
    fx = load_fixture(fx_id)
    exp = fx.expected
    top = generate_options(options_input(fx, basis=_basis(fx))).options[0]
    assert top.kind == exp.option_kind or (top.kind == "NO_ACTION" and exp.option_kind == "NO_ACTION")
    assert exp.qty_min <= top.qty <= exp.qty_max, top.label
    assert not top.blocked and not top.violations or top.needs_override


def test_s1_accept_ranks_the_recommendation_first() -> None:
    s = generate_options(options_input(load_fixture("s1_accept_correct")))
    assert s.recommendation_acceptable and s.recommendation_deviation_pct == 2.04
    assert s.options[0].id == "REC:REC-102:288" and s.options[0].is_recommendation


def test_s1_overstock_recommendation_is_evaluated_but_ranked_low() -> None:
    s = generate_options(options_input(load_fixture("s1_overstock")))
    assert not s.recommendation_acceptable
    rec = next(o for o in s.options if o.is_recommendation)
    assert {v.code for v in rec.violations} == {"CASE_PACK_MISMATCH", "STORAGE_EXCEEDED", "BUDGET_EXCEEDED", "OVERSTOCK"}
    assert s.options[0].id == "BUY:SUP-ALQ:240"
    assert [o.kind for o in s.options][-1] == "ESCALATE"


def test_s2_partial_needs_alternate_ranking_and_tradeoffs() -> None:
    s = generate_options(options_input(load_fixture("s2_partial_needs_alternate")))
    ids = [o.id for o in s.options]
    assert ids[0] == "BUY:SUP-CEDA:204"
    assert ids.index("TRANSFER:CDMX-02:10") < ids.index("ACCEPT_PARTIAL:PO-2002")
    assert not any(i.startswith("BUY:SUP-LALA") for i in ids)  # it just said it cannot ship more
    top = s.options[0]
    assert top.is_alternate_supplier and top.price_variance_pct == 5.66
    assert top.projection == [90, 250, 364, 274, 184]
    accept = next(o for o in s.options if o.kind == "ACCEPT_PARTIAL")
    assert accept.stockout_day == 4 and accept.unmet_units == 20


def test_s2_alt_moq_prefers_transfer_then_backorder_then_overstocking_buy() -> None:
    s = generate_options(options_input(load_fixture("s2_alt_moq_exceeds_gap")))
    order = [o.id for o in s.options]
    assert order[0] == "TRANSFER:CDMX-02:80"
    assert order.index("BACKORDER:PO-2003:250") < order.index("BUY:SUP-CEDA:480")
    ceda = next(o for o in s.options if o.id == "BUY:SUP-CEDA:480")
    assert ceda.cover_at_arrival == 10.71 and "overstock: 10.71 days of cover > 10" in ceda.tradeoffs


def test_s3_surge_prefers_increasing_the_open_po() -> None:
    fx = load_fixture("s3_real_surge")
    s = generate_options(options_input(fx, basis=_basis(fx)))
    assert s.options[0].id == "INCREASE:PO-3001:108"
    assert "BUY:SUP-FEMSA-MX:108" in [o.id for o in s.options]


def test_s4_budget_side_by_side_alternatives() -> None:
    fx = load_fixture("s4_budget_binding")
    s = generate_options(options_input(fx))
    first, second = s.options[0], s.options[1]
    expected = fx.expected.approval_alternatives
    assert (first.qty, first.value, first.needs_override, first.stockout_day) == (714, 4141200, True, None)
    assert next(v for v in first.violations if v.code == "BUDGET_EXCEEDED").detail["over_by"] == expected[0]["over_budget"]
    assert (second.qty, second.value, second.stockout_day, second.unmet_units) == (
        expected[1]["qty"], expected[1]["value"], expected[1]["stockout_day"], expected[1]["unmet_units"])
    assert not second.needs_override


def test_s4_override_refused_drops_every_option_needing_it() -> None:
    fx = load_fixture("s4_budget_override_rejected")
    s = generate_options(options_input(fx, excluded_option_ids=["BUY:SUP-FEMSA-CO:714"],
                                       refused_overrides=["BUDGET_EXCEEDED"]))
    assert s.options[0].id == "BUY:SUP-FEMSA-CO:342"
    assert not any(o.needs_override for o in s.options)
    assert s.options[0].stockout_day == fx.expected.residual_risk["stockout_day"]


def test_s4_storage_split_delivery_ranks_first() -> None:
    s = generate_options(options_input(load_fixture("s4_storage_binding")))
    top = s.options[0]
    assert top.id == "SPLIT:SUP-ALQ:360@3+84@4"
    assert [(d.day, d.qty) for d in top.deliveries] == [(3, 360), (4, 84)]
    capped = next(o for o in s.options if o.id == "BUY:SUP-ALQ:360")
    assert capped.safety_shortfall == 80 and capped.rank > top.rank
    assert next(o for o in s.options if o.id == "BUY:SUP-ALQ:444").blocked


def test_excluded_supplier_after_rejection_moves_to_cheapest_alternate() -> None:
    s = generate_options(options_input(load_fixture("x_supplier_rejects"), excluded_suppliers=["SUP-ALQ"]))
    assert s.options[0].id == "BUY:SUP-ANDINA:144"
    assert s.options[0].tradeoffs == ["alternate supplier, price +3.57% vs primary"]


def test_options_are_deterministic() -> None:
    inp = options_input(load_fixture("s2_alt_moq_exceeds_gap"))
    assert generate_options(inp) == generate_options(inp)


def test_recommendation_that_passes_validation_but_stocks_out_is_not_accepted() -> None:
    from app.engine.types import OptionsInput, Recommendation, StorageInfo, SupplierTerms

    # No safety stock, so the own order (200) ends the horizon at exactly 0, while the recommendation
    # (180, only 10% lower: inside tolerance) passes every validate_po check but runs out on day 4.
    inp = OptionsInput(
        forecast=[100.0] * 10, on_hand=300, reserved=0, receipts=[], review_period_days=2, safety_days=0,
        max_days_cover=30, storage=StorageInfo(capacity_units=10_000, used_units=300, sku_on_hand=300),
        budget_remaining=1_000_000,
        suppliers=[SupplierTerms(supplier_id="SUP", unit_cost=10, moq=0, case_pack=10, lead_time_days=3,
                                 is_primary=True)],
        recommendation=Recommendation(id="REC", supplier_id="SUP", qty=180),
    )
    s = generate_options(inp)
    rec = next(o for o in s.options if o.is_recommendation)
    assert rec.violations == [] and s.recommendation_deviation_pct == 10.0  # passes validate_po, in tolerance
    assert rec.stockout_day == 4  # ...but the outcome projection fails
    assert not s.recommendation_acceptable
    assert s.options[0].id == "BUY:SUP:200" and s.options[0].stockout_day is None


def test_reliability_breaks_ties_between_equally_priced_suppliers() -> None:
    from app.engine.types import SupplierTerms

    inp = options_input(load_fixture("x_supplier_rejects"), excluded_suppliers=["SUP-ALQ"])
    same_price = [t.model_copy(update={"unit_cost": 4350.0}) if not t.is_primary else t for t in inp.suppliers]
    flaky = [t.model_copy(update={"reliability": 0.5}) if t.supplier_id == "SUP-ANDINA" else t for t in same_price]
    ranked = [o.id for o in generate_options(inp.model_copy(update={"suppliers": flaky})).options]
    assert ranked.index("BUY:SUP-MAKRO:144") < ranked.index("BUY:SUP-ANDINA:144")  # MAKRO 0.90 > ANDINA 0.50
    assert isinstance(flaky[0], SupplierTerms)
