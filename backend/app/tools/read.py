"""Read tools. Every response carries `freshness` so the agent can tell stale data from fresh.

Supplier-provided free text is only ever returned in fields named `untrusted_text`: it is data
to report, never instructions to follow (decision D3).
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from app.models import OPEN_PO_STATUSES, POEvent, Promotion, SalesDaily, Supplier, SupplierProduct
from app.tools import data
from app.tools.registry import Args, ToolContext, ToolError, tool


class Freshness(BaseModel):
    source: str
    updated_at: datetime | None
    age_hours: float | None
    limit_hours: float | None
    stale: bool


def freshness(ctx: ToolContext, source: str, updated_at: datetime | None) -> Freshness:
    age = data.age_hours(ctx.clock, updated_at)
    limit = ctx.policy.freshness_limits_hours.get(source)
    stale = age is None or (limit is not None and age > limit)
    return Freshness(source=source, updated_at=updated_at, age_hours=age, limit_hours=limit, stale=stale)


class NodeSku(Args):
    node: str
    sku: str


class InventoryOut(BaseModel):
    node: str
    sku: str
    product: str
    on_hand: int
    reserved: int
    available: int
    units_sold_since_count: int
    freshness: Freshness


@tool("get_inventory", "read", "Stock at one node: on_hand, reserved, available, sales recorded since the count.")
def get_inventory(ctx: ToolContext, args: NodeSku) -> InventoryOut:
    row = data.get_inventory_row(ctx.session, args.node, args.sku)
    return InventoryOut(
        node=args.node, sku=args.sku, product=data.get_product(ctx.session, args.sku).name,
        on_hand=row.on_hand, reserved=row.reserved, available=row.on_hand - row.reserved,
        units_sold_since_count=data.units_sold_since(ctx.session, ctx.clock, args.node, args.sku, row.updated_at),
        freshness=freshness(ctx, "inventory", row.updated_at),
    )


class OtherNodeStock(BaseModel):
    node: str
    name: str
    on_hand: int
    reserved: int
    available: int
    freshness: Freshness


class OtherNodesOut(BaseModel):
    city: str
    nodes: list[OtherNodeStock]


@tool("get_inventory_other_nodes", "read", "Stock of the SKU at other nodes in the same city (transfer sources).")
def get_inventory_other_nodes(ctx: ToolContext, args: NodeSku) -> OtherNodesOut:
    out = []
    for n in data.same_city_nodes(ctx.session, args.node):
        try:
            row = data.get_inventory_row(ctx.session, n.id, args.sku)
        except ToolError:  # no record at that node
            continue
        out.append(OtherNodeStock(node=n.id, name=n.name, on_hand=row.on_hand, reserved=row.reserved,
                                  available=row.on_hand - row.reserved,
                                  freshness=freshness(ctx, "inventory", row.updated_at)))
    return OtherNodesOut(city=data.get_node(ctx.session, args.node).city, nodes=out)


class SalesArgs(NodeSku):
    days: int = 28


class SalesOut(BaseModel):
    node: str
    sku: str
    days: list[int]  # offsets, -1 = yesterday
    units: list[int]
    orders: list[int]
    max_order_units: list[int]  # largest single customer order that day
    stockout_days: list[int]
    freshness: Freshness


@tool("get_sales_history", "read", "Daily sales for the last N days (default 28), with largest single order and stockouts.")
def get_sales_history(ctx: ToolContext, args: SalesArgs) -> SalesOut:
    rows = data.sales_days(ctx.session, ctx.clock, args.node, args.sku, args.days)
    latest = ctx.session.scalars(select(SalesDaily.updated_at).filter_by(node_id=args.node, sku=args.sku)
                                 .order_by(SalesDaily.updated_at.desc())).first()
    return SalesOut(node=args.node, sku=args.sku, days=[r.day for r in rows], units=[r.units for r in rows],
                    orders=[r.orders for r in rows], max_order_units=[r.max_order_units for r in rows],
                    stockout_days=[r.day for r in rows if r.stockout], freshness=freshness(ctx, "sales", latest))


class ForecastArgs(NodeSku):
    days: int = 14


class ForecastOut(BaseModel):
    node: str
    sku: str
    daily: list[float]  # day 0 = today
    freshness: Freshness


@tool("get_forecast", "read", "Daily demand forecast from today (default 14 days).")
def get_forecast(ctx: ToolContext, args: ForecastArgs) -> ForecastOut:
    rows = data.forecast_rows(ctx.session, ctx.clock, args.node, args.sku, args.days)
    return ForecastOut(node=args.node, sku=args.sku, daily=[r.units for r in rows],
                       freshness=freshness(ctx, "forecast", min(r.generated_at for r in rows)))


class POLineOut(BaseModel):
    sku: str
    qty_ordered: int
    qty_confirmed: int | None
    unit_cost: float
    eta_day: int


class POEventOut(BaseModel):
    type: str
    source: str
    hours_ago: float
    data: dict[str, Any]
    untrusted_text: str | None = None


class POOut(BaseModel):
    po_id: str
    supplier_id: str
    status: str
    lines: list[POLineOut]
    events: list[POEventOut]


class OpenPOsOut(BaseModel):
    node: str
    sku: str
    purchase_orders: list[POOut]


@tool("get_open_pos", "read", "Open purchase orders for the SKU at the node, with lines and recent status events.")
def get_open_pos(ctx: ToolContext, args: NodeSku) -> OpenPOsOut:
    seen, out = set(), []
    for po, _ in data.open_po_lines(ctx.session, args.node, args.sku, statuses=OPEN_PO_STATUSES):
        if po.id in seen:
            continue
        seen.add(po.id)
        events = ctx.session.scalars(select(POEvent).filter_by(po_id=po.id).order_by(POEvent.id.desc()).limit(5))
        out.append(POOut(
            po_id=po.id, supplier_id=po.supplier_id, status=po.status,
            lines=[POLineOut(sku=l.sku, qty_ordered=l.qty_ordered, qty_confirmed=l.qty_confirmed,
                             unit_cost=l.unit_cost, eta_day=ctx.clock.day_offset(l.expected_arrival))
                   for l in po.lines if l.sku == args.sku and l.status == "OPEN"],
            events=[_event(ctx, e) for e in reversed(list(events))],
        ))
    return OpenPOsOut(node=args.node, sku=args.sku, purchase_orders=out)


def _event(ctx: ToolContext, e: POEvent) -> POEventOut:
    payload = dict(e.payload or {})
    message = payload.pop("message", None)
    return POEventOut(type=e.type, source=e.source, hours_ago=data.age_hours(ctx.clock, e.at) or 0.0,
                      data=payload, untrusted_text=message)


class SupplierSku(Args):
    supplier_id: str
    sku: str


class SupplierTermsOut(BaseModel):
    supplier_id: str
    name: str
    active: bool
    reliability_score: float
    is_primary: bool
    unit_cost: float
    currency: str
    moq: int
    case_pack: int
    lead_time_days: int
    hist_fill_rate: float
    untrusted_text: str
    freshness: Freshness


def _terms_out(ctx: ToolContext, sp: SupplierProduct, sup: Supplier) -> SupplierTermsOut:
    return SupplierTermsOut(
        supplier_id=sup.id, name=sup.name, active=sup.active, reliability_score=sup.reliability_score,
        is_primary=sp.is_primary, unit_cost=sp.unit_cost, currency=sp.currency, moq=sp.moq,
        case_pack=sp.case_pack, lead_time_days=sp.lead_time_days, hist_fill_rate=sp.hist_fill_rate,
        untrusted_text=sp.notes or "", freshness=freshness(ctx, "supplier_terms", sp.updated_at),
    )


@tool("get_supplier_terms", "read", "One supplier's terms for the SKU: cost, MOQ, case pack, lead time, reliability.")
def get_supplier_terms(ctx: ToolContext, args: SupplierSku) -> SupplierTermsOut:
    row = ctx.session.execute(select(SupplierProduct, Supplier).join(Supplier).filter(
        SupplierProduct.supplier_id == args.supplier_id, SupplierProduct.sku == args.sku)).one_or_none()
    if row is None:
        data.supplier_terms(ctx.session, args.supplier_id, args.sku)  # raises NOT_FOUND
    return _terms_out(ctx, *row)


class SkuArgs(Args):
    sku: str


class AlternateSupplier(SupplierTermsOut):
    price_variance_pct: float  # vs the primary supplier


class AlternatesOut(BaseModel):
    sku: str
    primary_supplier_id: str
    alternates: list[AlternateSupplier]


@tool("list_alternate_suppliers", "read", "Every non-primary supplier of the SKU, with price variance vs the primary.")
def list_alternate_suppliers(ctx: ToolContext, args: SkuArgs) -> AlternatesOut:
    primary = data.primary_terms(ctx.session, args.sku)
    rows = ctx.session.execute(select(SupplierProduct, Supplier).join(Supplier).filter(
        SupplierProduct.sku == args.sku, SupplierProduct.supplier_id != primary.supplier_id).order_by(Supplier.id))
    alts = []
    for sp, sup in rows:
        base = _terms_out(ctx, sp, sup).model_dump()
        variance = round((sp.unit_cost - primary.unit_cost) / primary.unit_cost * 100, 2)
        alts.append(AlternateSupplier(**base, price_variance_pct=variance))
    return AlternatesOut(sku=args.sku, primary_supplier_id=primary.supplier_id, alternates=alts)


class BudgetOut(BaseModel):
    category: str
    currency: str
    period: str
    limit: float
    committed: float
    spent: float
    remaining: float
    freshness: Freshness


@tool("get_budget", "read", "Purchasing budget for the SKU's category and the node's currency, this month.")
def get_budget(ctx: ToolContext, args: NodeSku) -> BudgetOut:
    b = data.budget_row(ctx.session, ctx.clock, args.node, args.sku)
    if b is None:
        raise ToolError("MISSING_DATA", "no budget for this category/currency/month", {"source": "budget"})
    return BudgetOut(category=b.category, currency=b.currency, period=b.period, limit=b.limit_amount,
                     committed=b.committed, spent=b.spent, remaining=b.limit_amount - b.committed - b.spent,
                     freshness=freshness(ctx, "budget", b.updated_at))


class StorageOut(BaseModel):
    node: str
    zone: str
    capacity_units: int
    used_units: int
    sku_on_hand: int
    sku_capacity: int  # most units of this SKU the zone can hold if other SKUs stay flat
    freshness: Freshness


@tool("get_storage_capacity", "read", "Capacity and current use of the SKU's temperature zone at the node.")
def get_storage_capacity(ctx: ToolContext, args: NodeSku) -> StorageOut:
    row = data.storage_row(ctx.session, args.node, args.sku)
    info = data.storage_info(ctx.session, args.node, args.sku)
    if row is None or info is None:
        raise ToolError("MISSING_DATA", "no storage capacity record", {"source": "storage"})
    return StorageOut(node=args.node, zone=row.temp_zone, capacity_units=row.capacity_units,
                      used_units=row.used_units, sku_on_hand=info.sku_on_hand, sku_capacity=info.sku_capacity,
                      freshness=freshness(ctx, "storage", row.updated_at))


class PromoOut(BaseModel):
    id: str
    start_day: int
    end_day: int
    uplift_pct: float
    description: str


class PromotionsOut(BaseModel):
    node: str
    sku: str
    promotions: list[PromoOut]


@tool("get_promotions", "read", "Promotions for the SKU at the node (day offsets; end_day inclusive).")
def get_promotions(ctx: ToolContext, args: NodeSku) -> PromotionsOut:
    rows = ctx.session.scalars(select(Promotion).filter_by(node_id=args.node, sku=args.sku))
    return PromotionsOut(node=args.node, sku=args.sku, promotions=[
        PromoOut(id=p.id, start_day=ctx.clock.day_offset(p.start_day), end_day=ctx.clock.day_offset(p.end_day),
                 uplift_pct=p.uplift_pct, description=p.description) for p in rows])
