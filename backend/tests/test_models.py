from datetime import date, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Inventory, Node, POEvent, POLine, Product, PurchaseOrder, Supplier

NOW = datetime(2026, 10, 7, 7, 0)


def _catalog(session) -> None:
    session.add_all(
        [
            Node(id="BOG-01", name="Chapinero", city="Bogotá", country="CO", currency="COP"),
            Product(sku="LECHE", name="Leche 1L", category="dairy", temp_zone="chilled",
                    safety_days=2, review_period_days=2, max_days_cover=10),
            Supplier(id="SUP-A", name="A", country="CO", reliability_score=0.9, updated_at=NOW),
        ]
    )
    session.flush()


def test_po_with_lines_and_events_round_trips(session) -> None:
    _catalog(session)
    po = PurchaseOrder(id="PO-1", node_id="BOG-01", supplier_id="SUP-A", status="DRAFT",
                       currency="COP", created_by="agent", created_at=NOW, updated_at=NOW)
    po.lines.append(POLine(sku="LECHE", qty_ordered=240, unit_cost=4200,
                           expected_arrival=date(2026, 10, 10)))
    po.events.append(POEvent(at=NOW, type="CREATED", source="agent", payload={"qty": 240}))
    session.add(po)
    session.commit()

    loaded = session.get(PurchaseOrder, "PO-1")
    assert [l.qty_ordered for l in loaded.lines] == [240]
    assert loaded.events[0].payload == {"qty": 240}


def test_inventory_is_unique_per_node_and_sku(session) -> None:
    _catalog(session)
    session.add(Inventory(node_id="BOG-01", sku="LECHE", on_hand=10, updated_at=NOW))
    session.add(Inventory(node_id="BOG-01", sku="LECHE", on_hand=20, updated_at=NOW))
    with pytest.raises(IntegrityError):
        session.flush()


def test_foreign_keys_are_enforced(session) -> None:
    session.add(Inventory(node_id="NOPE", sku="NOPE", on_hand=1, updated_at=NOW))
    with pytest.raises(IntegrityError):
        session.flush()


def test_idempotency_key_is_unique(session) -> None:
    _catalog(session)
    for po_id in ("PO-1", "PO-2"):
        session.add(PurchaseOrder(id=po_id, node_id="BOG-01", supplier_id="SUP-A", status="DRAFT",
                                  currency="COP", idempotency_key="run1:create", created_by="agent",
                                  created_at=NOW, updated_at=NOW))
    with pytest.raises(IntegrityError):
        session.flush()
