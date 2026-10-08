from sqlalchemy import select

from app.models import Budget, PurchaseOrder, Workspace
from app.supplier_mock.service import respond
from app.tools import call_tool
from app.tools.compute import build_decision


def _submitted_po(ctx, behaviour=None):
    """s1_overstock: decide BUY:SUP-ALQ:240, create and submit it; optionally override the supplier script."""
    if behaviour is not None:
        ws = ctx.session.get(Workspace, 1)
        ws.config = {**ws.config, "supplier_behaviour": {"SUP-ALQ": behaviour}}
    ns = {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}
    call_tool("generate_options", ns, ctx)
    ctx.state.decision = build_decision(ctx, "BUY:SUP-ALQ:240", False, []).model_dump(mode="json")
    po_id = call_tool("create_po_draft", {**ns, "supplier_id": "SUP-ALQ", "deliveries": [{"day": 3, "qty": 240}],
                                          "idempotency_key": "c"}, ctx).output["po_id"]
    call_tool("submit_po", {"po_id": po_id, "idempotency_key": "s"}, ctx)
    return ctx.session.get(PurchaseOrder, po_id)


def _committed(ctx):
    return ctx.session.scalars(select(Budget)).one().committed


def test_scripted_confirmation(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")  # fixture scripts SUP-ALQ: CONFIRMED
    po = _submitted_po(ctx)
    ev = respond(ctx.session, ctx.clock, po)
    assert (ev.type, ev.qty_confirmed, ev.fill_rate, po.status) == ("CONFIRMED", 240, 1.0, "CONFIRMED")
    assert po.events[-1].type == "CONFIRMED" and po.events[-1].source == "supplier"


def test_rejection_releases_the_budget_commitment(tool_ctx) -> None:
    ctx = tool_ctx("x_supplier_rejects")
    po = _submitted_po(ctx)
    committed = _committed(ctx)
    ev = respond(ctx.session, ctx.clock, po)
    assert (ev.type, ev.fill_rate, po.status) == ("REJECTED", 0.0, "REJECTED")
    assert ev.message.startswith("Mantenimiento")
    assert _committed(ctx) == committed - 240 * 4200


def test_partial_confirms_part_and_releases_the_rest(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    po = _submitted_po(ctx, [{"type": "PARTIAL", "qty": 120}])
    committed = _committed(ctx)
    ev = respond(ctx.session, ctx.clock, po)
    assert (ev.qty_confirmed, ev.fill_rate, po.status) == (120, 0.5, "PARTIALLY_CONFIRMED")
    assert po.lines[0].qty_confirmed == 120 and _committed(ctx) == committed - 120 * 4200


def test_price_change_confirms_quantity_and_proposes_a_price(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    po = _submitted_po(ctx, [{"type": "PRICE_CHANGE", "pct": 8}])
    ev = respond(ctx.session, ctx.clock, po)
    assert ev.details == {"pct": 8, "old_unit_cost": 4200, "proposed_unit_cost": 4536.0}
    assert po.status == "SUBMITTED" and po.lines[0].unit_cost == 4200  # nothing accepted yet


def test_delay_moves_the_arrival(tool_ctx) -> None:
    ctx = tool_ctx("s1_overstock")
    po = _submitted_po(ctx, [{"type": "DELAYED", "days": 2}])
    ev = respond(ctx.session, ctx.clock, po)
    assert ev.details == {"days": 2, "new_arrival_day": 5} and po.lines[0].expected_arrival.isoformat() == "2026-10-12"


def test_responses_are_consumed_in_order_then_default_to_confirmed(tool_ctx) -> None:
    from app.supplier_mock.service import next_response

    ctx = tool_ctx("x_replans_exhausted")
    assert next_response(ctx.session, "SUP-ANDINA")["type"] == "REJECTED"
    assert next_response(ctx.session, "SUP-ANDINA") == {"type": "CONFIRMED"}
    assert ctx.session.get(Workspace, 1).config["supplier_cursor"] == {"SUP-ANDINA": 2}
