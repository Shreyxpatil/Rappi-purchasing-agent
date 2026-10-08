"""Layer 2 of the feedback loop: after acting, read the database back and compare it with the intent.

The model's tool results say what it *thinks* happened. This check says what actually happened,
field by field, so a lost write, a wrong supplier or a changed quantity is caught before the run
moves on to the supplier.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.models import AuditLog, POLine, PurchaseOrder, StockTransfer


def check(name: str, intended: Any, actual: Any) -> dict[str, Any]:
    return {"check": name, "intended": intended, "actual": actual, "ok": intended == actual}


def diff_against_intent(session: Session, clock: Clock, run_id: int, option: dict[str, Any],
                        trigger: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per field compared; the run is valid only if every entry is ok."""
    checks: list[dict[str, Any]] = []
    if option["kind"] == "PURCHASE" and option.get("po_id") is None:
        pos = [p for p in session.scalars(select(PurchaseOrder).filter_by(run_id=run_id, supplier_id=option["supplier_id"]))
               if p.status not in ("CANCELLED", "REJECTED")]
        checks.append(check("live POs for the option", 1, len(pos)))
        if len(pos) == 1:
            po = pos[0]
            checks += [check("node", trigger["node"], po.node_id),
                       check("status", "SUBMITTED", po.status),
                       check("sku", [trigger["sku"]] * len(po.lines), [l.sku for l in po.lines]),
                       check("deliveries (day, qty)", [(d["day"], d["qty"]) for d in option["deliveries"]],
                             [(clock.day_offset(l.expected_arrival), l.qty_ordered) for l in po.lines]),
                       check("unit_cost", option["unit_cost"], po.lines[0].unit_cost)]
    elif option["kind"] == "PURCHASE":
        audit = session.scalars(select(AuditLog).filter_by(run_id=run_id, action="INCREASE_PO_LINE",
                                                           entity_id=option["po_id"])).first()
        increase = (audit.details["to"] - audit.details["from"]) if audit else 0
        checks.append(check(f"increase on {option['po_id']}", option["qty"], increase))
        po = session.get(PurchaseOrder, option["po_id"])
        checks.append(check("status", "SUBMITTED", po.status if po else None))
    elif option["kind"] == "TRANSFER":
        t = session.scalars(select(StockTransfer).filter_by(run_id=run_id)).first()
        checks += [check("transfer source", option["from_node"], t.from_node_id if t else None),
                   check("transfer qty", option["qty"], t.qty if t else None),
                   check("transfer status", "PLANNED", t.status if t else None)]

    if trigger.get("type") == "supplier_response" and option["kind"] != "BACKORDER":
        line = session.scalars(select(POLine).filter_by(po_id=trigger["po_id"], sku=trigger["sku"])).first()
        if line is not None:
            checks.append(check(f"{trigger['po_id']} reduced to the confirmed qty", line.qty_confirmed, line.qty_ordered))
    return checks
