import pytest

from app.engine.types import Delivery, OpenPORef, PODraft, SupplierTerms
from app.engine.validation import validate_po
from app.fixtures import list_fixtures, load_fixture
from fixture_inputs import plan_context, terms

WITH_REC_VIOLATIONS = [f for f in list_fixtures() if f.expected.recommendation_violations]


def _terms(fx, supplier) -> SupplierTerms:
    t = terms(fx, supplier, fx.trigger.sku)
    return SupplierTerms(supplier_id=supplier, unit_cost=t["unit_cost"], moq=t["moq"], case_pack=t["case_pack"],
                         lead_time_days=t["lead_time_days"], is_primary=t["is_primary"])


def _draft(fx, supplier, *deliveries, **kw) -> PODraft:
    t = _terms(fx, supplier)
    return PODraft(supplier_id=supplier, sku=fx.trigger.sku, unit_cost=t.unit_cost,
                   deliveries=[Delivery(day=d, qty=q) for d, q in deliveries], **kw)


@pytest.mark.parametrize("fx", WITH_REC_VIOLATIONS, ids=lambda f: f.id)
def test_recommendation_violations_match_fixture_exactly(fx) -> None:
    rec = next(r for r in fx.seed.recommendations if r.id == fx.trigger.recommendation_id)
    t = _terms(fx, rec.supplier)
    res = validate_po(_draft(fx, rec.supplier, (t.lead_time_days, rec.qty)), t, plan_context(fx), [])
    assert {v.code for v in res.violations} == set(fx.expected.recommendation_violations)
    assert not res.ok


def test_own_quantity_passes_validation() -> None:
    fx = load_fixture("s1_overstock")
    res = validate_po(_draft(fx, "SUP-ALQ", (3, 240)), _terms(fx, "SUP-ALQ"), plan_context(fx), [])
    assert res.ok and res.violations == []


def test_below_moq_and_case_pack_and_lead_time() -> None:
    fx = load_fixture("s1_overstock")
    res = validate_po(_draft(fx, "SUP-ALQ", (2, 100)), _terms(fx, "SUP-ALQ"), plan_context(fx), [])
    codes = {v.code: v.detail for v in res.violations}
    assert codes["BELOW_MOQ"] == {"moq": 240, "qty": 100}
    assert codes["CASE_PACK_MISMATCH"]["nearest_valid"] == 96
    assert codes["LEAD_TIME_INFEASIBLE"] == {"day": 2, "earliest_day": 3}


def test_increase_on_existing_line_applies_moq_to_the_total() -> None:
    fx = load_fixture("s3_real_surge")
    t = _terms(fx, "SUP-FEMSA-MX")
    draft = _draft(fx, "SUP-FEMSA-MX", (2, 108), po_id="PO-3001", base_qty=200)
    res = validate_po(draft, t, plan_context(fx, basis=[64.0] * 14), [])
    assert "BELOW_MOQ" not in {v.code for v in res.violations}


def test_inactive_supplier_and_duplicate_open_po_block() -> None:
    fx = load_fixture("s1_overstock")
    t = _terms(fx, "SUP-ALQ").model_copy(update={"active": False})
    open_pos = [OpenPORef(po_id="PO-9", supplier_id="SUP-ALQ", sku="LECHE-ALQ-1L", status="SUBMITTED"),
                OpenPORef(po_id="PO-1001", supplier_id="SUP-ALQ", sku="LECHE-ALQ-1L", status="CONFIRMED")]
    res = validate_po(_draft(fx, "SUP-ALQ", (3, 240)), t, plan_context(fx), open_pos)
    codes = [v.code for v in res.violations]
    assert codes == ["SUPPLIER_INACTIVE", "DUPLICATE_OPEN_PO"]  # the confirmed PO is inbound, not a duplicate
    assert not res.ok


def test_budget_override_only_counts_when_approved() -> None:
    fx = load_fixture("s4_budget_binding")
    t = _terms(fx, "SUP-FEMSA-CO")
    draft = _draft(fx, "SUP-FEMSA-CO", (2, 714))
    assert not validate_po(draft, t, plan_context(fx), []).ok
    approved = validate_po(draft, t, plan_context(fx), [], approved_overrides=frozenset({"BUDGET_EXCEEDED"}))
    assert approved.ok and approved.overridden == ["BUDGET_EXCEEDED"]
    # Storage can never be overridden, even if someone tries.
    fx2 = load_fixture("s4_storage_binding")
    res = validate_po(_draft(fx2, "SUP-ALQ", (3, 444)), _terms(fx2, "SUP-ALQ"), plan_context(fx2), [],
                      approved_overrides=frozenset({"STORAGE_EXCEEDED"}))
    assert not res.ok and res.overridden == []


def test_arrival_after_stockout_is_flagged_but_not_blocking() -> None:
    fx = load_fixture("s2_partial_needs_alternate")  # stockout on day 4 without action
    t = SupplierTerms(supplier_id="SUP-SLOW", unit_cost=28.0, moq=12, case_pack=12, lead_time_days=5)
    draft = PODraft(supplier_id="SUP-SLOW", sku="LECHE-LALA-1L", unit_cost=28.0, deliveries=[Delivery(day=5, qty=24)])
    ctx = plan_context(fx).model_copy(update={"horizon": 6, "forecast": [90.0] * 14})
    res = validate_po(draft, t, ctx, [])
    flag = next(v for v in res.violations if v.code == "LEAD_TIME_MISSES_NEED_DATE")
    assert flag.detail == {"need_day": 4, "arrival_day": 5} and not flag.hard
