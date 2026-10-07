from sqlalchemy import select

from app.fixtures import load_fixture
from app.models import Approval, AuditLog, Budget, PurchaseOrder, StockTransfer
from app.tools import call_tool
from app.tools.act import resolve_approval
from app.tools.compute import build_decision


def _ok(name, args, ctx):
    r = call_tool(name, args, ctx)
    assert r.ok, r.error
    return r.output


def _decide(ctx, option_id, basis="forecast"):
    t = ctx.state.trigger
    _ok("generate_options", {"node": t["node"], "sku": t["sku"], "demand_basis": basis}, ctx)
    ctx.state.decision = build_decision(ctx, option_id, investigate=False, information_needed=[]).model_dump(
        mode="json")


def _committed(ctx, category, currency):
    return ctx.session.scalars(select(Budget).filter_by(category=category, currency=currency)).one().committed


MILK = {"node": "BOG-01", "sku": "LECHE-ALQ-1L", "supplier_id": "SUP-ALQ"}


def test_s1_create_and_submit_bound_po_auto(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    _decide(ctx, "BUY:SUP-ALQ:240")
    draft = _ok("create_po_draft", {**MILK, "deliveries": [{"day": 3, "qty": 240}], "idempotency_key": "r1:create"}, ctx)
    assert draft == {"po_id": "PO-9001", "status": "DRAFT", "value": 1008000.0, "submit_will_need": "AUTO",
                     "approval_reasons": [], "warnings": []}
    sub = _ok("submit_po", {"po_id": "PO-9001", "idempotency_key": "r1:submit"}, ctx)
    assert sub["status"] == "SUBMITTED"
    po = ctx.session.get(PurchaseOrder, "PO-9001")
    assert [e.type for e in po.events] == ["CREATED", "SUBMITTED"]
    assert _committed(ctx, "dairy", "COP") == 12000000 + 1008000
    assert [a.action for a in ctx.session.scalars(select(AuditLog))] == ["CREATE_PO_DRAFT", "SUBMIT_PO"]


def test_retry_with_same_key_replays_and_different_request_conflicts(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    _decide(ctx, "BUY:SUP-ALQ:240")
    args = {**MILK, "deliveries": [{"day": 3, "qty": 240}], "idempotency_key": "k1"}
    first = _ok("create_po_draft", args, ctx)
    again = _ok("create_po_draft", args, ctx)
    assert again == {**first, "replayed": True}
    assert ctx.session.query(PurchaseOrder).filter_by(created_by="agent").count() == 1
    clash = call_tool("create_po_draft", {**args, "deliveries": [{"day": 3, "qty": 252}]}, ctx)
    assert clash.error["code"] == "IDEMPOTENCY_CONFLICT"


def test_injected_quantity_is_blocked_and_never_persisted(tool_ctx) -> None:
    ctx = tool_ctx("x_prompt_injection")
    _decide(ctx, "BUY:SUP-ALQ:240")
    r = call_tool("create_po_draft", {**MILK, "deliveries": [{"day": 3, "qty": 10000}], "idempotency_key": "inj"}, ctx)
    assert r.error["code"] == "BLOCKED" and r.error["details"]["reasons"] == ["DECISION_BINDING_MISMATCH"]
    assert ctx.session.query(PurchaseOrder).filter_by(created_by="agent").count() == 0
    audit = ctx.session.scalars(select(AuditLog)).one()
    assert audit.action == "BLOCKED_PURCHASE" and audit.details["action"]["qty"] == 10000


def test_acting_without_decision_is_blocked(tool_ctx) -> None:
    r = call_tool("create_po_draft", {**MILK, "deliveries": [{"day": 3, "qty": 240}], "idempotency_key": "x"},
                  tool_ctx("s1_overstock"))
    assert r.error["details"]["reasons"] == ["NO_DECISION"]


def test_two_blocked_attempts_escalate(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    _decide(ctx, "BUY:SUP-ALQ:240")
    for i, qty in enumerate((800, 10000)):
        r = call_tool("create_po_draft", {**MILK, "deliveries": [{"day": 3, "qty": qty}],
                                          "idempotency_key": f"b{i}"}, ctx)
        assert r.error["code"] == "BLOCKED"
    r = call_tool("create_po_draft", {**MILK, "deliveries": [{"day": 3, "qty": 240}], "idempotency_key": "b3"}, ctx)
    assert r.error["code"] == "ESCALATION_REQUIRED" and r.error["details"]["reasons"] == ["VALIDATION_FAILED_TWICE"]


def test_s4_budget_override_approval_shows_both_options_and_executes_on_approve(tool_ctx) -> None:
    fx = load_fixture("s4_budget_binding")
    ctx = tool_ctx(fx.id)
    _decide(ctx, "BUY:SUP-FEMSA-CO:714")
    coke = {"node": "BOG-02", "sku": "COCA-1.5L", "supplier_id": "SUP-FEMSA-CO"}
    draft = _ok("create_po_draft", {**coke, "deliveries": [{"day": 2, "qty": 714}], "idempotency_key": "c"}, ctx)
    assert draft["submit_will_need"] == "APPROVAL"
    sub = _ok("submit_po", {"po_id": draft["po_id"], "idempotency_key": "s"}, ctx)
    assert sub["status"] == "PENDING_APPROVAL"
    assert set(sub["approval_reasons"]) == set(fx.expected.approval_reasons)
    alts = [{k: a[k] for k in ("qty", "value", "over_budget", "stockout_day")} for a in sub["alternatives"]]
    assert alts == [{k: e[k] for k in ("qty", "value", "over_budget", "stockout_day")}
                    for e in fx.expected.approval_alternatives]
    assert sub["alternatives"][1]["unmet_units"] == 128

    out = resolve_approval(ctx, sub["approval_id"], approve=True, decided_by="category.manager")
    assert out.result["status"] == "SUBMITTED"
    assert ctx.state.approved_overrides == ["BUDGET_EXCEEDED"]
    assert ctx.session.get(Approval, sub["approval_id"]).status == "APPROVED"


def test_s4_rejected_override_is_never_proposed_again(tool_ctx) -> None:
    ctx = tool_ctx("s4_budget_override_rejected")
    _decide(ctx, "BUY:SUP-FEMSA-CO:714")
    coke = {"node": "BOG-02", "sku": "COCA-1.5L", "supplier_id": "SUP-FEMSA-CO"}
    draft = _ok("create_po_draft", {**coke, "deliveries": [{"day": 2, "qty": 714}], "idempotency_key": "c"}, ctx)
    sub = _ok("submit_po", {"po_id": draft["po_id"], "idempotency_key": "s"}, ctx)
    out = resolve_approval(ctx, sub["approval_id"], approve=False, decided_by="cm", comment="no budget left")
    assert out.result["status"] == "CANCELLED"
    assert ctx.state.refused_overrides == ["BUDGET_EXCEEDED"]
    assert ctx.state.excluded_option_ids == ["BUY:SUP-FEMSA-CO:714"]
    opts = _ok("generate_options", {"node": "BOG-02", "sku": "COCA-1.5L"}, ctx)
    assert opts["options"][0]["id"] == "BUY:SUP-FEMSA-CO:342"
    assert not any(o["needs_override"] for o in opts["options"])


def test_s2_acknowledge_partial_then_alternate_needs_approval(tool_ctx) -> None:
    ctx = tool_ctx("s2_partial_needs_alternate")
    before = _committed(ctx, "dairy", "MXN")
    ack = _ok("update_po_line", {"po_id": "PO-2002", "sku": "LECHE-LALA-1L", "new_qty": 250,
                                 "reason": "supplier confirmed 250", "idempotency_key": "ack"}, ctx)
    assert ack == {"po_id": "PO-2002", "status": "CONFIRMED", "qty": 250, "changed": True}
    assert _committed(ctx, "dairy", "MXN") == before - 250 * 26.5

    _decide(ctx, "BUY:SUP-CEDA:204")
    draft = _ok("create_po_draft", {"node": "CDMX-01", "sku": "LECHE-LALA-1L", "supplier_id": "SUP-CEDA",
                                    "deliveries": [{"day": 2, "qty": 204}], "idempotency_key": "c"}, ctx)
    sub = _ok("submit_po", {"po_id": draft["po_id"], "idempotency_key": "s"}, ctx)
    assert sub["approval_reasons"] == ["ALTERNATE_SUPPLIER", "PRICE_VARIANCE"]


def test_s3_increase_open_po_matches_decision(tool_ctx) -> None:
    ctx = tool_ctx("s3_real_surge")
    _decide(ctx, "INCREASE:PO-3001:108", basis="recent_run_rate")
    wrong = call_tool("update_po_line", {"po_id": "PO-3001", "sku": "AGUA-CIEL-1L", "new_qty": 500,
                                         "reason": "surge", "idempotency_key": "w"}, ctx)
    assert wrong.error["details"]["reasons"] == ["DECISION_BINDING_MISMATCH"]
    out = _ok("update_po_line", {"po_id": "PO-3001", "sku": "AGUA-CIEL-1L", "new_qty": 308, "reason": "surge",
                                 "idempotency_key": "i"}, ctx)
    assert out == {"po_id": "PO-3001", "status": "SUBMITTED", "qty": 308, "changed": True}


def test_s2_transfer_within_city_bound_to_decision(tool_ctx) -> None:
    ctx = tool_ctx("s2_alt_moq_exceeds_gap")
    _decide(ctx, "TRANSFER:CDMX-02:80")
    base = {"to_node": "CDMX-01", "sku": "LECHE-LALA-1L"}
    assert call_tool("create_transfer", {**base, "from_node": "BOG-01", "qty": 80, "idempotency_key": "x"},
                     ctx).error["code"] == "INVALID_ARGUMENTS"
    out = _ok("create_transfer", {**base, "from_node": "CDMX-02", "qty": 80, "idempotency_key": "t"}, ctx)
    assert out == {"transfer_id": "TR-001", "status": "PLANNED", "qty": 80, "arrival_day": 1}
    assert ctx.session.get(StockTransfer, "TR-001").expected_arrival.isoformat() == "2026-10-08"


def test_cancellation_always_goes_to_a_human(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    _decide(ctx, "BUY:SUP-ALQ:240")
    out = _ok("cancel_po_line", {"po_id": "PO-1001", "sku": "LECHE-ALQ-1L", "reason": "test",
                                 "idempotency_key": "cx"}, ctx)
    assert out["approval_reasons"] == ["CANCELLATION"]
    resolve_approval(ctx, out["approval_id"], approve=True, decided_by="buyer")
    assert ctx.session.get(PurchaseOrder, "PO-1001").status == "CANCELLED"


def test_escalate_records_reason(tool_ctx) -> None:
    ctx = tool_ctx("s1_stale_inventory")
    out = _ok("escalate", {"reason_code": "STALE_DATA", "summary": "count is 72h old",
                           "information_needed": ["fresh cycle count"], "idempotency_key": "e"}, ctx)
    assert out == {"escalated": True, "reason_code": "STALE_DATA"} and ctx.state.escalated


def test_under_delivering_supplier_stays_excluded_after_acknowledgement(tool_ctx) -> None:
    ctx = tool_ctx("s2_partial_needs_alternate")
    _ok("update_po_line", {"po_id": "PO-2002", "sku": "LECHE-LALA-1L", "new_qty": 250,
                           "reason": "supplier confirmed 250", "idempotency_key": "ack"}, ctx)
    ids = [o["id"] for o in _ok("generate_options", {"node": "CDMX-01", "sku": "LECHE-LALA-1L"}, ctx)["options"]]
    assert ids[0] == "BUY:SUP-CEDA:204" and not any("SUP-LALA" in i for i in ids)
