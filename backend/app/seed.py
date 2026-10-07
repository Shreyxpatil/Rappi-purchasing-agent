"""Deterministic seeding: base catalog + one scenario fixture -> database.

Seeding a scenario wipes the domain tables (catalog, stock, purchasing, constraints)
and rebuilds them. Agent runs, steps, approvals and the audit log are kept, so the
history of earlier runs stays visible.
"""

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.clock import Clock
from app.fixtures import ScenarioFixture
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
    Recommendation,
    SalesDaily,
    StockTransfer,
    StorageCapacity,
    Supplier,
    SupplierProduct,
    Workspace,
)

CATALOG_PATH = Path(__file__).parent / "seed_data" / "catalog.json"

# Child tables first so foreign keys are never violated.
_DOMAIN_TABLES = [
    POEvent, POLine, PurchaseOrder, StockTransfer, Recommendation, SalesDaily, Forecast, Inventory,
    Promotion, StorageCapacity, Budget, SupplierProduct, Supplier, Product, Node, Workspace,
]


def reset_domain(session: Session) -> None:
    for model in _DOMAIN_TABLES:
        session.execute(delete(model))


def seed_workspace(session: Session, fx: ScenarioFixture) -> Clock:
    clock = Clock(fx.as_of)
    reset_domain(session)
    _seed_catalog(session, fx, clock)
    _seed_stock_and_demand(session, fx, clock)
    _seed_constraints(session, fx, clock)
    _seed_purchasing(session, fx, clock)
    session.add(
        Workspace(
            id=1,
            scenario_id=fx.id,
            as_of=fx.as_of,
            seeded_at=fx.as_of,
            config={
                "trigger": fx.trigger.model_dump(),
                "supplier_behaviour": {
                    k: [r.model_dump(exclude_none=True) for r in v] for k, v in fx.supplier_behaviour.items()
                },
                "approval_responses": list(fx.approval_responses),
            },
        )
    )
    session.flush()
    return clock


def _hours_ago(clock: Clock, hours: float) -> datetime:
    return clock.now() - timedelta(hours=hours)


def _seed_catalog(session: Session, fx: ScenarioFixture, clock: Clock) -> None:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    session.add_all(Node(**n) for n in catalog["nodes"])
    session.add_all(Product(**p) for p in catalog["products"])

    overrides = {o.id: o for o in fx.seed.suppliers}
    for s in catalog["suppliers"]:
        o = overrides.get(s["id"])
        if o is not None:
            s.update(o.model_dump(exclude_none=True, exclude={"id"}))
        session.add(Supplier(**s, updated_at=_hours_ago(clock, 24)))

    country_currency = {n["country"]: n["currency"] for n in catalog["nodes"]}
    supplier_country = {s["id"]: s["country"] for s in catalog["suppliers"]}
    terms = {(t["supplier_id"], t["sku"]): t for t in catalog["supplier_products"]}
    for t in terms.values():
        t["updated_at"] = _hours_ago(clock, 72)
    for o in fx.seed.supplier_terms:
        key = (o.supplier, o.sku)
        patch = o.model_dump(exclude_none=True, exclude={"supplier", "sku", "updated_hours_ago"})
        if o.updated_hours_ago is not None:
            patch["updated_at"] = _hours_ago(clock, o.updated_hours_ago)
        if key in terms:
            terms[key].update(patch)
        else:  # a new supplier/SKU pair must carry full terms
            terms[key] = {"supplier_id": o.supplier, "sku": o.sku, "updated_at": _hours_ago(clock, 72),
                          "currency": country_currency[supplier_country[o.supplier]], **patch}
    session.add_all(SupplierProduct(**t) for t in terms.values())
    session.flush()


def _seed_stock_and_demand(session: Session, fx: ScenarioFixture, clock: Clock) -> None:
    for inv in fx.seed.inventory:
        session.add(
            Inventory(node_id=inv.node, sku=inv.sku, on_hand=inv.on_hand, reserved=inv.reserved,
                      updated_at=_hours_ago(clock, inv.updated_hours_ago))
        )

    for f in fx.seed.forecasts:
        generated_at = _hours_ago(clock, f.generated_hours_ago)
        for offset, units in enumerate(f.values()):
            session.add(Forecast(node_id=f.node, sku=f.sku, day=clock.day(offset), units=units,
                                 generated_at=generated_at))

    for s in fx.seed.sales:
        values = s.values()
        n = len(values)
        for i, units in enumerate(values):
            offset = i - n  # last value is yesterday (-1)
            units = int(units)
            orders = s.orders[i] if s.orders else max(1, math.ceil(units / 1.5)) if units else 0
            max_order = s.max_order_units[i] if s.max_order_units else min(units, 4)
            session.add(
                SalesDaily(node_id=s.node, sku=s.sku, day=clock.day(offset), units=units, orders=orders,
                           max_order_units=max_order, stockout=offset in s.stockout_days,
                           updated_at=_hours_ago(clock, 2))
            )


def _seed_constraints(session: Session, fx: ScenarioFixture, clock: Clock) -> None:
    for b in fx.seed.budgets:
        session.add(
            Budget(category=b.category, currency=b.currency, period=b.period or clock.today().strftime("%Y-%m"),
                   limit_amount=b.limit, committed=b.committed, spent=b.spent, updated_at=_hours_ago(clock, 1))
        )
    for st in fx.seed.storage:
        session.add(StorageCapacity(node_id=st.node, temp_zone=st.zone, capacity_units=st.capacity,
                                    used_units=st.used, updated_at=_hours_ago(clock, 1)))
    for p in fx.seed.promotions:
        session.add(Promotion(id=p.id, node_id=p.node, sku=p.sku, start_day=clock.day(p.start_day),
                              end_day=clock.day(p.end_day), uplift_pct=p.uplift_pct, description=p.description))


def _seed_purchasing(session: Session, fx: ScenarioFixture, clock: Clock) -> None:
    currency = {n.id: n.currency for n in session.query(Node)}
    cost = {(t.supplier_id, t.sku): t.unit_cost for t in session.query(SupplierProduct)}

    for r in fx.seed.recommendations:
        session.add(Recommendation(id=r.id, node_id=r.node, sku=r.sku, supplier_id=r.supplier, qty=r.qty,
                                   created_at=_hours_ago(clock, 1)))

    for p in fx.seed.purchase_orders:
        created = clock.now() - timedelta(days=p.created_days_ago)
        po = PurchaseOrder(id=p.id, node_id=p.node, supplier_id=p.supplier, status=p.status,
                           currency=currency[p.node], created_by="system", created_at=created,
                           updated_at=created)
        for line in p.lines:
            po.lines.append(
                POLine(sku=line.sku, qty_ordered=line.qty, qty_confirmed=line.qty_confirmed,
                       unit_cost=line.unit_cost if line.unit_cost is not None else cost[(p.supplier, line.sku)],
                       expected_arrival=clock.day(line.eta_day))
            )
        po.events.append(POEvent(at=created, type="CREATED", source="system",
                                 payload={"lines": [l.model_dump(exclude_none=True) for l in p.lines]}))
        for e in p.events:
            at = _hours_ago(clock, e.hours_ago)
            po.events.append(POEvent(at=at, type=e.type, source=e.source, payload=e.payload))
            po.updated_at = max(po.updated_at, at)
        session.add(po)
