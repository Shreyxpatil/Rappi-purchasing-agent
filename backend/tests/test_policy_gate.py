from app.engine.types import Delivery, ValidationResult, Violation
from app.engine.projection import project_inventory
from app.engine.types import ConstraintReport
from app.policy import get_policy
from app.policy.gate import ProposedAction, evaluate

POLICY = get_policy()
BUY_240 = {"id": "BUY:SUP-ALQ:240", "kind": "PURCHASE", "supplier_id": "SUP-ALQ", "po_id": None,
           "deliveries": [{"day": 3, "qty": 240}], "qty": 240}


def _purchase(qty=240, supplier="SUP-ALQ", unit=4200.0, ref=4200.0, primary=True, day=3, currency="COP"):
    return ProposedAction(kind="PURCHASE", sku="LECHE-ALQ-1L", supplier_id=supplier, deliveries=[Delivery(day=day, qty=qty)],
                          qty=qty, unit_cost=unit, value=qty * unit, currency=currency, is_primary_supplier=primary,
                          reference_unit_cost=ref)


def _validation(*violations: Violation, overridden=()) -> ValidationResult:
    report = ConstraintReport(deliveries=[], total_qty=0, value=0, violations=list(violations),
                              projection=project_inventory(0, [0], [], 1), cover_at_arrival=[])
    hard = [v for v in violations if v.hard and v.code not in overridden]
    return ValidationResult(ok=not hard, violations=list(violations), overridden=list(overridden), report=report)


def _gate(action, option=BUY_240, validation=None, failures=0, replans=0, approved=False):
    return evaluate(action, policy=POLICY, decision_option=option, validation=validation or _validation(),
                    validation_failures=failures, replans=replans, human_approved=approved)


def test_bound_primary_purchase_under_limit_is_auto() -> None:
    assert _gate(_purchase()).verdict == "AUTO"


def test_injected_quantity_is_blocked_by_decision_binding() -> None:
    r = _gate(_purchase(qty=10000))
    assert (r.verdict, r.reasons) == ("BLOCK", ["DECISION_BINDING_MISMATCH"])
    assert r.details["expected"]["deliveries"] == [(3, 240)] and r.details["got"]["deliveries"] == [(3, 10000)]


def test_acting_without_a_decision_is_blocked() -> None:
    assert _gate(_purchase(), option=None).reasons == ["NO_DECISION"]


def test_non_overridable_violation_blocks_overridable_one_needs_approval() -> None:
    storage = Violation(code="STORAGE_EXCEEDED", hard=True, detail={"max": 672})
    assert _gate(_purchase(), validation=_validation(storage)).reasons == ["VALIDATION_FAILED"]
    budget = Violation(code="BUDGET_EXCEEDED", hard=True, overridable=True, detail={"over_by": 1})
    r = _gate(_purchase(), validation=_validation(budget))
    assert (r.verdict, r.reasons) == ("APPROVAL", ["BUDGET_OVERRIDE"])


def test_s2_alternate_supplier_with_price_variance() -> None:
    option = {"id": "BUY:SUP-CEDA:204", "kind": "PURCHASE", "supplier_id": "SUP-CEDA", "po_id": None,
              "deliveries": [{"day": 2, "qty": 204}], "qty": 204}
    action = _purchase(qty=204, supplier="SUP-CEDA", unit=28.0, ref=26.5, primary=False, day=2, currency="MXN")
    r = _gate(action, option=option)
    assert (r.verdict, r.reasons) == ("APPROVAL", ["ALTERNATE_SUPPLIER", "PRICE_VARIANCE"])
    assert r.details["price_variance_pct"] == 5.66
    assert _gate(action, option=option, approved=True).reasons == ["APPROVED_BY_HUMAN"]


def test_s4_full_buy_needs_override_and_is_over_the_auto_limit() -> None:
    option = {"id": "BUY:SUP-FEMSA-CO:714", "kind": "PURCHASE", "supplier_id": "SUP-FEMSA-CO", "po_id": None,
              "deliveries": [{"day": 2, "qty": 714}], "qty": 714}
    budget = Violation(code="BUDGET_EXCEEDED", hard=True, overridable=True, detail={})
    r = _gate(_purchase(qty=714, supplier="SUP-FEMSA-CO", unit=5800, ref=5800, day=2), option=option,
              validation=_validation(budget))
    assert r.reasons == ["OVER_AUTO_LIMIT", "BUDGET_OVERRIDE"]


def test_limits_escalate_before_anything_else() -> None:
    assert _gate(_purchase(), failures=2).reasons == ["VALIDATION_FAILED_TWICE"]
    assert _gate(_purchase(), replans=4).reasons == ["MAX_REPLANS_REACHED"]
    assert _gate(_purchase(), replans=3).verdict == "AUTO"


def test_acknowledging_a_partial_needs_no_decision_or_approval() -> None:
    ack = ProposedAction(kind="ACKNOWLEDGE_PARTIAL", sku="LECHE-LALA-1L", po_id="PO-2001", qty=250, currency="MXN")
    assert evaluate(ack, policy=POLICY, decision_option=None, validation=None, validation_failures=0,
                    replans=0).verdict == "AUTO"


def test_cancellation_and_decrease_always_need_approval() -> None:
    for kind in ("CANCEL", "DECREASE"):
        a = ProposedAction(kind=kind, sku="X", po_id="PO-1", qty=10, currency="COP")
        assert _gate(a, validation=None).reasons == ["CANCELLATION"]


def test_transfer_binding() -> None:
    option = {"id": "TRANSFER:CDMX-02:80", "kind": "TRANSFER", "from_node": "CDMX-02", "qty": 80}
    ok = ProposedAction(kind="TRANSFER", sku="LECHE-LALA-1L", from_node="CDMX-02", qty=80, currency="MXN")
    assert _gate(ok, option=option).verdict == "AUTO"
    assert _gate(ok.model_copy(update={"qty": 200}), option=option).verdict == "BLOCK"


def test_action_of_the_wrong_shape_is_blocked() -> None:
    increase = ProposedAction(kind="INCREASE", sku="LECHE-ALQ-1L", po_id="PO-1001", qty=240, currency="COP")
    r = _gate(increase)  # the decision was a new PO, not an increase
    assert r.reasons == ["DECISION_BINDING_MISMATCH"] and r.details == {"decided_option": "BUY:SUP-ALQ:240",
                                                                         "action": "INCREASE"}
