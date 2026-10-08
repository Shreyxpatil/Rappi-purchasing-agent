from app.agent.validation import diff_against_intent
from app.models import PurchaseOrder
from app.tools import call_tool
from app.tools.act import decided_option
from app.tools.compute import build_decision

NS = {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}


def _executed(ctx):
    call_tool("generate_options", NS, ctx)
    ctx.state.decision = build_decision(ctx, "BUY:SUP-ALQ:240", False, []).model_dump(mode="json")
    po_id = call_tool("create_po_draft", {**NS, "supplier_id": "SUP-ALQ", "deliveries": [{"day": 3, "qty": 240}],
                                          "idempotency_key": "c"}, ctx).output["po_id"]
    call_tool("submit_po", {"po_id": po_id, "idempotency_key": "s"}, ctx)
    return ctx.session.get(PurchaseOrder, po_id)


def _failed(ctx):
    checks = diff_against_intent(ctx.session, ctx.clock, ctx.run_id, decided_option(ctx), ctx.state.trigger)
    return {c["check"]: (c["intended"], c["actual"]) for c in checks if not c["ok"]}


def test_executed_option_matches_the_database(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    _executed(ctx)
    assert _failed(ctx) == {}


def test_a_changed_quantity_or_status_is_caught(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    po = _executed(ctx)
    po.lines[0].qty_ordered = 252  # someone edited the PO after the agent acted
    po.status = "DRAFT"
    assert _failed(ctx) == {"deliveries (day, qty)": ([(3, 240)], [(3, 252)]), "status": ("SUBMITTED", "DRAFT")}


def test_missing_po_is_caught(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    call_tool("generate_options", NS, ctx)
    ctx.state.decision = build_decision(ctx, "BUY:SUP-ALQ:240", False, []).model_dump(mode="json")
    assert _failed(ctx) == {"live POs for the option": (1, 0)}


def test_unacknowledged_partial_is_caught(tool_ctx) -> None:
    ctx = tool_ctx("s2_alt_moq_exceeds_gap")
    call_tool("generate_options", {"node": "CDMX-01", "sku": "LECHE-LALA-1L"}, ctx)
    ctx.state.decision = build_decision(ctx, "TRANSFER:CDMX-02:80", False, []).model_dump(mode="json")
    call_tool("create_transfer", {"from_node": "CDMX-02", "to_node": "CDMX-01", "sku": "LECHE-LALA-1L", "qty": 80,
                                  "idempotency_key": "t"}, ctx)
    assert _failed(ctx) == {"PO-2003 reduced to the confirmed qty": (250, 500)}
