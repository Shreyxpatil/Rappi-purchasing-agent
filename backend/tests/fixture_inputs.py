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


NODE_CURRENCY = {n["id"]: n["currency"] for n in CATALOG["nodes"]}


def plan_context(fx: ScenarioFixture, *, supplier: str | None = None, basis: list[float] | None = None,
                 extra_receipts: list[Receipt] | None = None):
    from app.engine.replenishment import calculate_net_requirement
    from app.engine.types import PlanContext, StorageInfo

    node, sku = fx.trigger.node, fx.trigger.sku
    p = product(sku)
    req = calculate_net_requirement(net_input(fx, supplier=supplier, basis=basis))
    inv = inventory(fx, node, sku)
    st = next((s for s in fx.seed.storage if (s.node, s.zone) == (node, p["temp_zone"])), None)
    bud = next((b for b in fx.seed.budgets
                if (b.category, b.currency) == (p["category"], NODE_CURRENCY[node])), None)
    return PlanContext(
        available=inv.on_hand - inv.reserved,
        forecast=basis if basis is not None else forecast(fx, node, sku),
        horizon=req.horizon,
        avg_daily=req.avg_daily,
        safety_stock=req.safety_stock,
        existing_receipts=receipts(fx, node, sku) + (extra_receipts or []),
        storage=StorageInfo(capacity_units=st.capacity, used_units=st.used, sku_on_hand=inv.on_hand) if st else None,
        budget_remaining=(bud.limit - bud.committed - bud.spent) if bud else None,
        max_days_cover=p["max_days_cover"],
    )


def sales_days(fx: ScenarioFixture, node: str, sku: str):
    """Same defaults as app.seed for orders / largest order."""
    import math

    from app.engine.types import SalesDay

    s = next(x for x in fx.seed.sales if (x.node, x.sku) == (node, sku))
    values = s.values()
    out = []
    for i, u in enumerate(values):
        u = int(u)
        day = i - len(values)
        out.append(SalesDay(
            day=day, units=u,
            orders=s.orders[i] if s.orders else (max(1, math.ceil(u / 1.5)) if u else 0),
            max_order_units=s.max_order_units[i] if s.max_order_units else min(u, 4),
            stockout=day in s.stockout_days))
    return out


def promo_windows(fx: ScenarioFixture, node: str, sku: str):
    from app.engine.types import PromoWindow

    return [PromoWindow(id=p.id, start_day=p.start_day, end_day=p.end_day, uplift_pct=p.uplift_pct)
            for p in fx.seed.promotions if (p.node, p.sku) == (node, sku)]


def supplier_terms_list(fx: ScenarioFixture, sku: str):
    from app.engine.types import SupplierTerms

    overrides = {o.id: o for o in fx.seed.suppliers}
    suppliers = {s["id"]: s for s in CATALOG["suppliers"]}
    pairs = {t["supplier_id"] for t in CATALOG["supplier_products"] if t["sku"] == sku}
    pairs |= {o.supplier for o in fx.seed.supplier_terms if o.sku == sku}
    out = []
    for sid in sorted(pairs):
        t = terms(fx, sid, sku)
        s = suppliers[sid]
        o = overrides.get(sid)
        out.append(SupplierTerms(
            supplier_id=sid, unit_cost=t["unit_cost"], moq=t["moq"], case_pack=t["case_pack"],
            lead_time_days=t["lead_time_days"], is_primary=t.get("is_primary", False),
            active=o.active if o and o.active is not None else s["active"],
            reliability=o.reliability_score if o and o.reliability_score is not None else s["reliability_score"]))
    return out


def options_input(fx: ScenarioFixture, *, basis: list[float] | None = None, **overrides):
    from app.engine.types import (NetRequirementInput, OpenLine, OptionsInput, PartialFill, Recommendation,
                                  TransferSource)

    node, sku = fx.trigger.node, fx.trigger.sku
    ctx = plan_context(fx, basis=basis)
    inv = inventory(fx, node, sku)
    p = product(sku)
    fc = basis if basis is not None else forecast(fx, node, sku)

    rec = None
    if fx.trigger.recommendation_id:
        r = next(r for r in fx.seed.recommendations if r.id == fx.trigger.recommendation_id)
        rec = Recommendation(id=r.id, supplier_id=r.supplier, qty=r.qty)

    partial = None
    open_lines = []
    for po in fx.seed.purchase_orders:
        if po.node != node:
            continue
        line = next((l for l in po.lines if l.sku == sku), None)
        if line is None:
            continue
        if po.id == fx.trigger.po_id and po.status == "PARTIALLY_CONFIRMED":
            ev = next(e for e in po.events if e.type == "PARTIAL")
            partial = PartialFill(po_id=po.id, supplier_id=po.supplier, qty_ordered=line.qty,
                                  qty_confirmed=line.qty_confirmed, eta_day=line.eta_day,
                                  unit_cost=terms(fx, po.supplier, sku)["unit_cost"],
                                  backorder_eta_day=ev.payload.get("backorder_eta_day"))
        elif po.status in ("SUBMITTED", "CONFIRMED"):
            open_lines.append(OpenLine(po_id=po.id, supplier_id=po.supplier, qty=line.qty, eta_day=line.eta_day))

    city = next(n["city"] for n in CATALOG["nodes"] if n["id"] == node)
    ref = terms(fx, primary_supplier(sku), sku)
    sources = []
    for other in [n["id"] for n in CATALOG["nodes"] if n["city"] == city and n["id"] != node]:
        if not any((i.node, i.sku) == (other, sku) for i in fx.seed.inventory):
            continue
        oi = inventory(fx, other, sku)
        sources.append(TransferSource(node_id=other, requirement=NetRequirementInput(
            forecast=forecast(fx, other, sku), on_hand=oi.on_hand, reserved=oi.reserved,
            inbound=receipts(fx, other, sku), lead_time_days=ref["lead_time_days"],
            review_period_days=p["review_period_days"], safety_days=p["safety_days"],
            moq=ref["moq"], case_pack=ref["case_pack"])))

    fields = dict(
        forecast=fc, on_hand=inv.on_hand, reserved=inv.reserved, receipts=receipts(fx, node, sku),
        review_period_days=p["review_period_days"], safety_days=p["safety_days"], max_days_cover=p["max_days_cover"],
        storage=ctx.storage, budget_remaining=ctx.budget_remaining, suppliers=supplier_terms_list(fx, sku),
        recommendation=rec, partial_fill=partial, open_lines=open_lines, transfer_sources=sources,
    )
    fields.update(overrides)
    return OptionsInput(**fields)


def script_turns(case_id: str) -> list[dict]:
    from app.llm.scripted import SCRIPTS_DIR

    return json.loads((SCRIPTS_DIR / f"{case_id}.json").read_text(encoding="utf-8"))["turns"]
