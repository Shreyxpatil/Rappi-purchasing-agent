"""Database -> engine inputs. The only place that knows how rows become engine values.

Read tools show these values to the agent; compute tools feed them to the engine. Using one
adapter for both guarantees the agent reasons about the same numbers the engine computes with.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.engine.types import (
    NetRequirementInput,
    OpenLine,
    PartialFill,
    PromoWindow,
    Receipt,
    SalesDay,
    StorageInfo,
    SupplierTerms,
    TransferSource,
)
from app.models import (
    Budget,
    Forecast,
    Inventory,
    Node,
    POEvent,
    POLine,
    Product,
    Promotion,
    PurchaseOrder,
    SalesDaily,
    StockTransfer,
    StorageCapacity,
    Supplier,
    SupplierProduct,
)
from app.tools.registry import ToolError

# PO statuses whose quantities are expected to arrive. DRAFT/PENDING_APPROVAL are not commitments yet.
INBOUND_STATUSES = ("SUBMITTED", "CONFIRMED", "PARTIALLY_CONFIRMED")


def age_hours(clock: Clock, at: datetime | None) -> float | None:
    return None if at is None else round((clock.now() - at).total_seconds() / 3600, 1)


def get_node(s: Session, node_id: str) -> Node:
    n = s.get(Node, node_id)
    if n is None:
        raise ToolError("NOT_FOUND", f"unknown node {node_id}", {"node": node_id})
    return n


def get_product(s: Session, sku: str) -> Product:
    p = s.get(Product, sku)
    if p is None:
        raise ToolError("NOT_FOUND", f"unknown sku {sku}", {"sku": sku})
    return p


def get_inventory_row(s: Session, node_id: str, sku: str) -> Inventory:
    get_node(s, node_id)  # an unknown id is the caller's mistake (NOT_FOUND), not missing business data
    get_product(s, sku)
    row = s.scalars(select(Inventory).filter_by(node_id=node_id, sku=sku)).one_or_none()
    if row is None:
        raise ToolError("MISSING_DATA", f"no inventory record for {sku} at {node_id}", {"source": "inventory"})
    return row


def units_sold_since(s: Session, clock: Clock, node_id: str, sku: str, since: datetime) -> int:
    """Sales recorded from the count's calendar day up to yesterday (conservative: includes the count day)."""
    rows = s.scalars(select(SalesDaily).filter(
        SalesDaily.node_id == node_id, SalesDaily.sku == sku,
        SalesDaily.day >= since.date(), SalesDaily.day < clock.today()))
    return sum(r.units for r in rows)


def forecast_rows(s: Session, clock: Clock, node_id: str, sku: str, days: int) -> list[Forecast]:
    rows = list(s.scalars(select(Forecast).filter(
        Forecast.node_id == node_id, Forecast.sku == sku, Forecast.day >= clock.today()).order_by(Forecast.day)))
    if not rows:
        raise ToolError("MISSING_DATA", f"no forecast for {sku} at {node_id}", {"source": "forecast"})
    return rows[:days]


def forecast_values(s: Session, clock: Clock, node_id: str, sku: str, days: int = 14) -> list[float]:
    return [r.units for r in forecast_rows(s, clock, node_id, sku, days)]


def sales_days(s: Session, clock: Clock, node_id: str, sku: str, days: int = 28) -> list[SalesDay]:
    rows = s.scalars(select(SalesDaily).filter(
        SalesDaily.node_id == node_id, SalesDaily.sku == sku,
        SalesDaily.day >= clock.day(-days), SalesDaily.day < clock.today()).order_by(SalesDaily.day))
    return [SalesDay(day=clock.day_offset(r.day), units=r.units, orders=r.orders,
                     max_order_units=r.max_order_units, stockout=r.stockout) for r in rows]


def open_po_lines(s: Session, node_id: str, sku: str, statuses=INBOUND_STATUSES) -> list[tuple[PurchaseOrder, POLine]]:
    q = (select(PurchaseOrder, POLine).join(POLine).filter(
        PurchaseOrder.node_id == node_id, POLine.sku == sku, POLine.status == "OPEN",
        PurchaseOrder.status.in_(statuses)).order_by(PurchaseOrder.id, POLine.id))
    return [(po, line) for po, line in s.execute(q)]


def receipts(s: Session, clock: Clock, node_id: str, sku: str) -> list[Receipt]:
    """Inbound stock: supplier-confirmed quantity when confirmed, ordered quantity otherwise; plus transfers in."""
    out = [Receipt(day=clock.day_offset(line.expected_arrival),
                   qty=line.qty_confirmed if line.qty_confirmed is not None else line.qty_ordered, source=po.id)
           for po, line in open_po_lines(s, node_id, sku)]
    for t in s.scalars(select(StockTransfer).filter_by(to_node_id=node_id, sku=sku, status="PLANNED")):
        out.append(Receipt(day=clock.day_offset(t.expected_arrival), qty=t.qty, source=t.id))
    for t in s.scalars(select(StockTransfer).filter_by(from_node_id=node_id, sku=sku, status="PLANNED")):
        out.append(Receipt(day=0, qty=-t.qty, source=t.id))  # stock leaving this node today
    return out


def supplier_terms_list(s: Session, sku: str) -> list[SupplierTerms]:
    q = select(SupplierProduct, Supplier).join(Supplier).filter(SupplierProduct.sku == sku).order_by(Supplier.id)
    return [SupplierTerms(supplier_id=sup.id, unit_cost=sp.unit_cost, moq=sp.moq, case_pack=sp.case_pack,
                          lead_time_days=sp.lead_time_days, active=sup.active, is_primary=sp.is_primary,
                          reliability=sup.reliability_score) for sp, sup in s.execute(q)]


def supplier_terms(s: Session, supplier_id: str, sku: str) -> SupplierTerms:
    t = next((t for t in supplier_terms_list(s, sku) if t.supplier_id == supplier_id), None)
    if t is None:
        raise ToolError("NOT_FOUND", f"{supplier_id} does not sell {sku}", {"supplier": supplier_id, "sku": sku})
    return t


def primary_terms(s: Session, sku: str) -> SupplierTerms:
    terms = supplier_terms_list(s, sku)
    if not terms:
        raise ToolError("MISSING_DATA", f"no supplier sells {sku}", {"source": "supplier_terms"})
    return next((t for t in terms if t.is_primary), terms[0])


def storage_row(s: Session, node_id: str, sku: str) -> StorageCapacity | None:
    zone = get_product(s, sku).temp_zone
    return s.scalars(select(StorageCapacity).filter_by(node_id=node_id, temp_zone=zone)).one_or_none()


def storage_info(s: Session, node_id: str, sku: str) -> StorageInfo | None:
    row = storage_row(s, node_id, sku)
    if row is None:
        return None
    inv = s.scalars(select(Inventory).filter_by(node_id=node_id, sku=sku)).one_or_none()
    return StorageInfo(capacity_units=row.capacity_units, used_units=row.used_units,
                       sku_on_hand=inv.on_hand if inv else 0)


def budget_row(s: Session, clock: Clock, node_id: str, sku: str) -> Budget | None:
    category = get_product(s, sku).category
    currency = get_node(s, node_id).currency
    return s.scalars(select(Budget).filter_by(category=category, currency=currency,
                                              period=clock.today().strftime("%Y-%m"))).one_or_none()


def budget_remaining(s: Session, clock: Clock, node_id: str, sku: str) -> float | None:
    b = budget_row(s, clock, node_id, sku)
    return None if b is None else b.limit_amount - b.committed - b.spent


def promo_windows(s: Session, clock: Clock, node_id: str, sku: str) -> list[PromoWindow]:
    rows = s.scalars(select(Promotion).filter_by(node_id=node_id, sku=sku).order_by(Promotion.start_day))
    return [PromoWindow(id=p.id, start_day=clock.day_offset(p.start_day), end_day=clock.day_offset(p.end_day),
                        uplift_pct=p.uplift_pct) for p in rows]


def partial_fill(s: Session, clock: Clock, po_id: str, sku: str) -> PartialFill | None:
    po = s.get(PurchaseOrder, po_id)
    if po is None or po.status != "PARTIALLY_CONFIRMED":
        return None
    line = next((l for l in po.lines if l.sku == sku and l.status == "OPEN"), None)
    if line is None or line.qty_confirmed is None or line.qty_confirmed >= line.qty_ordered:
        return None
    ev = s.scalars(select(POEvent).filter_by(po_id=po_id, type="PARTIAL").order_by(POEvent.id.desc())).first()
    return PartialFill(po_id=po.id, supplier_id=po.supplier_id, qty_ordered=line.qty_ordered,
                       qty_confirmed=line.qty_confirmed, eta_day=clock.day_offset(line.expected_arrival),
                       unit_cost=line.unit_cost,
                       backorder_eta_day=(ev.payload or {}).get("backorder_eta_day") if ev else None)


def open_lines(s: Session, clock: Clock, node_id: str, sku: str, exclude_po: str | None) -> list[OpenLine]:
    return [OpenLine(po_id=po.id, supplier_id=po.supplier_id, qty=line.qty_ordered,
                     eta_day=clock.day_offset(line.expected_arrival))
            for po, line in open_po_lines(s, node_id, sku, statuses=("SUBMITTED", "CONFIRMED"))
            if po.id != exclude_po]


def same_city_nodes(s: Session, node_id: str) -> list[Node]:
    city = get_node(s, node_id).city
    return list(s.scalars(select(Node).filter(Node.city == city, Node.id != node_id).order_by(Node.id)))


def net_input(s: Session, clock: Clock, node_id: str, sku: str, terms: SupplierTerms,
              forecast: list[float] | None = None) -> NetRequirementInput:
    p = get_product(s, sku)
    inv = get_inventory_row(s, node_id, sku)
    return NetRequirementInput(
        forecast=forecast if forecast is not None else forecast_values(s, clock, node_id, sku),
        on_hand=inv.on_hand, reserved=inv.reserved, inbound=receipts(s, clock, node_id, sku),
        lead_time_days=terms.lead_time_days, review_period_days=p.review_period_days,
        safety_days=p.safety_days, moq=terms.moq, case_pack=terms.case_pack,
    )


def transfer_sources(s: Session, clock: Clock, node_id: str, sku: str) -> list[TransferSource]:
    ref = primary_terms(s, sku)
    out = []
    for other in same_city_nodes(s, node_id):
        if s.scalars(select(Inventory).filter_by(node_id=other.id, sku=sku)).one_or_none() is None:
            continue
        try:
            out.append(TransferSource(node_id=other.id, requirement=net_input(s, clock, other.id, sku, ref)))
        except ToolError:  # no forecast there: cannot tell how much it can spare, so not a source
            continue
    return out
