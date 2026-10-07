"""Build engine inputs straight from fixture JSON + the catalog, with no database.

Engine tests use this to prove the engine is pure: the same numbers a reviewer reads in
a fixture go in, and the hand-computed `expected.computed` values must come out.
"""

import json

from app.engine.types import NetRequirementInput, Receipt
from app.fixtures import ScenarioFixture
from app.seed import CATALOG_PATH

CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
INBOUND_STATUSES = {"SUBMITTED", "CONFIRMED", "PARTIALLY_CONFIRMED"}


def product(sku: str) -> dict:
    return next(p for p in CATALOG["products"] if p["sku"] == sku)


def terms(fx: ScenarioFixture, supplier: str, sku: str) -> dict:
    base = next((t for t in CATALOG["supplier_products"] if t["supplier_id"] == supplier and t["sku"] == sku), {})
    merged = dict(base)
    for o in fx.seed.supplier_terms:
        if (o.supplier, o.sku) == (supplier, sku):
            merged.update(o.model_dump(exclude_none=True, exclude={"supplier", "sku", "updated_hours_ago"}))
    return merged


def primary_supplier(sku: str) -> str:
    return next(t["supplier_id"] for t in CATALOG["supplier_products"] if t["sku"] == sku and t["is_primary"])


def inventory(fx: ScenarioFixture, node: str, sku: str):
    return next(i for i in fx.seed.inventory if (i.node, i.sku) == (node, sku))


def forecast(fx: ScenarioFixture, node: str, sku: str) -> list[float]:
    return next(f for f in fx.seed.forecasts if (f.node, f.sku) == (node, sku)).values()


def receipts(fx: ScenarioFixture, node: str, sku: str) -> list[Receipt]:
    out = []
    for po in fx.seed.purchase_orders:
        if po.node != node or po.status not in INBOUND_STATUSES:
            continue
        for line in po.lines:
            if line.sku == sku:
                qty = line.qty_confirmed if line.qty_confirmed is not None else line.qty
                out.append(Receipt(day=line.eta_day, qty=qty, source=po.id))
    return out


def net_input(fx: ScenarioFixture, *, node: str | None = None, sku: str | None = None,
              supplier: str | None = None, basis: list[float] | None = None) -> NetRequirementInput:
    node = node or fx.trigger.node
    sku = sku or fx.trigger.sku
    t = terms(fx, supplier or primary_supplier(sku), sku)
    p = product(sku)
    inv = inventory(fx, node, sku)
    return NetRequirementInput(
        forecast=basis if basis is not None else forecast(fx, node, sku),
        on_hand=inv.on_hand,
        reserved=inv.reserved,
        inbound=receipts(fx, node, sku),
        lead_time_days=t["lead_time_days"],
        review_period_days=p["review_period_days"],
        safety_days=p["safety_days"],
        moq=t["moq"],
        case_pack=t["case_pack"],
    )
