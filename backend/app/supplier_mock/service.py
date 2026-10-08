"""The supplier side of the feedback loop (layer 3).

A submitted PO gets the next scripted response for its supplier from the scenario
(workspace.config["supplier_behaviour"]), in order; with nothing scripted the supplier confirms.
Responses are written to the PO and its po_events exactly as a supplier integration would, so
the agent learns about them the same way it would in production: by reading the PO back.
A cursor in the workspace makes the sequence reproducible and survives a paused run.
"""

from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.clock import Clock
from app.models import POEvent, PurchaseOrder, Workspace
from app.tools import data

RESPONSE_TYPES = ("CONFIRMED", "PARTIAL", "REJECTED", "PRICE_CHANGE", "DELAYED")


class SupplierEvent(BaseModel):
    po_id: str
    supplier_id: str
    type: str
    qty_ordered: int
    qty_confirmed: int
    fill_rate: float  # confirmed / ordered, the input to the reliability score
    details: dict[str, Any] = {}
    message: str = ""


def next_response(session: Session, supplier_id: str) -> dict[str, Any]:
    ws = session.get(Workspace, 1)
    config = dict(ws.config or {})
    scripted = config.get("supplier_behaviour", {}).get(supplier_id, [])
    cursor = dict(config.get("supplier_cursor", {}))
    i = cursor.get(supplier_id, 0)
    cursor[supplier_id] = i + 1
    ws.config = {**config, "supplier_cursor": cursor}  # reassign: JSON columns are not change-tracked in place
    return scripted[i] if i < len(scripted) else {"type": "CONFIRMED"}


def respond(session: Session, clock: Clock, po: PurchaseOrder) -> SupplierEvent:
    """Apply the supplier's answer to a SUBMITTED PO and record it as a PO event."""
    r = next_response(session, po.supplier_id)
    kind = r["type"]
    ordered = sum(l.qty_ordered for l in po.lines)
    details: dict[str, Any] = {}

    if kind == "CONFIRMED":
        for l in po.lines:
            l.qty_confirmed = l.qty_ordered
        po.status = "CONFIRMED"
    elif kind == "PARTIAL":
        remaining = r["qty"]
        for l in po.lines:  # fill lines in delivery order
            l.qty_confirmed = min(l.qty_ordered, remaining)
            remaining -= l.qty_confirmed
        po.status = "PARTIALLY_CONFIRMED"
        _release(session, clock, po, sum((l.qty_ordered - l.qty_confirmed) * l.unit_cost for l in po.lines))
        details = {"qty_confirmed": r["qty"]}
    elif kind == "REJECTED":
        for l in po.lines:
            l.qty_confirmed = 0
        po.status = "REJECTED"
        _release(session, clock, po, sum(l.qty_ordered * l.unit_cost for l in po.lines))
    elif kind == "PRICE_CHANGE":
        # Quantities are confirmed; the new price waits for the agent and the gate (it may need approval).
        for l in po.lines:
            l.qty_confirmed = l.qty_ordered
        old = po.lines[0].unit_cost
        details = {"pct": r["pct"], "old_unit_cost": old, "proposed_unit_cost": round(old * (1 + r["pct"] / 100), 2)}
    elif kind == "DELAYED":
        for l in po.lines:
            l.qty_confirmed = l.qty_ordered
            l.expected_arrival = l.expected_arrival + timedelta(days=r["days"])
        po.status = "CONFIRMED"
        details = {"days": r["days"], "new_arrival_day": clock.day_offset(po.lines[0].expected_arrival)}
    else:
        raise ValueError(f"unknown supplier response {kind}")

    confirmed = sum(l.qty_confirmed or 0 for l in po.lines)
    po.events.append(POEvent(at=clock.now(), type=kind, source="supplier",
                             payload={**details, "qty_confirmed": confirmed, "message": r.get("message", "")}))
    po.updated_at = clock.now()
    session.flush()
    return SupplierEvent(po_id=po.id, supplier_id=po.supplier_id, type=kind, qty_ordered=ordered,
                         qty_confirmed=confirmed, fill_rate=round(confirmed / ordered, 4) if ordered else 0.0,
                         details=details, message=r.get("message", ""))


def _release(session: Session, clock: Clock, po: PurchaseOrder, value: float) -> None:
    """Return the value the supplier will not ship to the budget's committed amount."""
    b = data.budget_row(session, clock, po.node_id, po.lines[0].sku)
    if b is not None and value:
        b.committed = round(b.committed - value, 2)
        b.updated_at = clock.now()


def update_reliability(session: Session, clock: Clock, supplier_id: str, fill_rate: float,
                       alpha: float) -> tuple[float, float]:
    """EWMA of fill rates: recent outcomes count, one bad week does not erase a good year.
    Returns (old, new)."""
    from app.models import Supplier

    s = session.get(Supplier, supplier_id)
    old = s.reliability_score
    s.reliability_score = round((1 - alpha) * old + alpha * fill_rate, 4)
    s.updated_at = clock.now()
    return old, s.reliability_score
