"""Candidate actions with their trade-offs, ranked.

The LLM never invents a quantity: it chooses one of these option ids. Each option is fully
evaluated here (constraints, projection, cost), so the choice is between known outcomes.

Ranking follows the documented constraint priority:
  hard constraints (unless a human can override) > avoid stockout > avoid overstock
  > keep safety stock > avoid needing an override > prefer the primary supplier > cost.
A recommendation that is within tolerance, passes validate_po AND leaves no projected stockout
before the next cycle is ranked first: the agent should not churn a plan that is already right.
"""

import math

from app.engine.constraints import evaluate_constraints, level_before
from app.engine.projection import project_inventory
from app.engine.replenishment import calculate_net_requirement, ceil_to, floor_to
from app.engine.types import (
    ConstraintReport,
    Delivery,
    EngineInputError,
    NetRequirement,
    NetRequirementInput,
    Option,
    OptionsInput,
    OptionSet,
    PlanContext,
    PODraft,
    Receipt,
    SupplierTerms,
    Violation,
)
from app.engine.validation import validate_po


def generate_options(inp: OptionsInput) -> OptionSet:
    if not inp.suppliers:
        raise EngineInputError("NO_SUPPLIERS", "no supplier sells this SKU")
    ref_terms = next((t for t in inp.suppliers if t.is_primary), inp.suppliers[0])
    ref_req = calculate_net_requirement(_net_input(inp, ref_terms))
    ref_ctx = _ctx(inp, ref_req)
    builder = _Builder(inp, ref_terms, ref_req)

    pf = inp.partial_fill
    if pf:
        builder.no_purchase("ACCEPT_PARTIAL", f"ACCEPT_PARTIAL:{pf.po_id}", pf.qty_confirmed, ref_ctx,
                            label=f"Accept {pf.qty_confirmed} of {pf.qty_ordered} from {pf.supplier_id}, close the rest",
                            po_id=pf.po_id, supplier_id=pf.supplier_id)
        remainder = pf.qty_ordered - pf.qty_confirmed
        if remainder > 0 and pf.backorder_eta_day is not None:
            builder.no_purchase("BACKORDER", f"BACKORDER:{pf.po_id}:{remainder}", remainder, ref_ctx,
                                deliveries=[Delivery(day=pf.backorder_eta_day, qty=remainder)],
                                label=f"Keep remaining {remainder} on backorder (day {pf.backorder_eta_day})",
                                po_id=pf.po_id, supplier_id=pf.supplier_id, unit_cost=pf.unit_cost)
    else:
        builder.no_purchase("NO_ACTION", "NO_ACTION", 0, ref_ctx, label="Do nothing: keep the current plan")

    if ref_req.net > 0:
        builder.purchases()
        builder.increases()
        builder.transfers(ref_ctx)
    builder.recommendation()
    builder.no_purchase("ESCALATE", "ESCALATE", 0, ref_ctx, label="Escalate to a buyer")

    ranked = builder.ranked()
    return OptionSet(
        reference_supplier_id=ref_terms.supplier_id,
        reference=ref_req,
        options=ranked,
        recommendation_acceptable=builder.rec_acceptable,
        recommendation_deviation_pct=builder.rec_deviation_pct,
    )


class _Builder:
    def __init__(self, inp: OptionsInput, ref_terms: SupplierTerms, ref_req: NetRequirement) -> None:
        self.inp = inp
        self.ref_terms = ref_terms
        self.ref_req = ref_req
        self.terms = {t.supplier_id: t for t in inp.suppliers}
        self.unavailable = set(inp.excluded_suppliers)
        if inp.partial_fill:  # it just told us it cannot ship more; only its backorder is an option
            self.unavailable.add(inp.partial_fill.supplier_id)
        self.options: list[dict] = []
        self.rec_acceptable = False
        self.rec_deviation_pct: float | None = None

    # ------------------------------------------------------------------ option producers

    def purchases(self) -> None:
        for t in self.inp.suppliers:
            if t.supplier_id in self.unavailable or not t.active:
                continue
            req = calculate_net_requirement(_net_input(self.inp, t))
            if req.order_qty == 0:
                continue
            ctx = _ctx(self.inp, req)
            full = self.purchase(t, ctx, [Delivery(day=t.lead_time_days, qty=req.order_qty)])
            codes = {v.code for v in full["violations"]}
            if "STORAGE_EXCEEDED" in codes:
                self.storage_variants(t, ctx, req.order_qty)
            if "BUDGET_EXCEEDED" in codes:
                cap = floor_to(math.floor(ctx.budget_remaining / t.unit_cost), t.case_pack)
                if cap > 0:
                    self.purchase(t, ctx, [Delivery(day=t.lead_time_days, qty=cap)], only_if_valid=True,
                                  note="capped to remaining budget")

    def storage_variants(self, t: SupplierTerms, ctx: PlanContext, target: int) -> None:
        """Single delivery capped to what fits, and a split across consecutive days to reach the target."""
        assert ctx.storage is not None
        first = floor_to(ctx.storage.sku_capacity - level_before(ctx, t.lead_time_days, []), t.case_pack)
        if first > 0:
            self.purchase(t, ctx, [Delivery(day=t.lead_time_days, qty=first)], only_if_valid=True,
                          note="capped to storage")
        deliveries: list[Delivery] = []
        remaining = target
        for day in range(t.lead_time_days, ctx.horizon):
            receipts = [Receipt(day=d.day, qty=d.qty, source="plan") for d in deliveries]
            fits = floor_to(ctx.storage.sku_capacity - level_before(ctx, day, receipts), t.case_pack)
            qty = min(remaining, fits)
            if qty > 0:
                deliveries.append(Delivery(day=day, qty=qty))
                remaining -= qty
            if remaining == 0:
                break
        if remaining == 0 and len(deliveries) > 1:
            self.purchase(t, ctx, deliveries, only_if_valid=True, note="split delivery")

    def increases(self) -> None:
        for line in self.inp.open_lines:
            t = self.terms.get(line.supplier_id)
            if t is None or t.supplier_id in self.unavailable or not t.active:
                continue
            req = calculate_net_requirement(_net_input(self.inp, t))
            # The supplier needs its lead time to load more, and the extra must land inside the horizon.
            if not (t.lead_time_days <= line.eta_day < req.horizon) or req.net <= 0:
                continue
            inc = ceil_to(req.net, t.case_pack)
            self.purchase(t, _ctx(self.inp, req), [Delivery(day=line.eta_day, qty=inc)], po_id=line.po_id,
                          base_qty=line.qty)

    def transfers(self, ref_ctx: PlanContext) -> None:
        for src in self.inp.transfer_sources:
            excess = math.floor(max(0.0, -calculate_net_requirement(src.requirement).net))
            if excess <= 0:
                continue
            qty = min(math.ceil(self.ref_req.net), excess)
            day = self.inp.transfer_lead_days
            report = evaluate_constraints([Delivery(day=day, qty=qty)], 0.0, 1, ref_ctx)
            self.add(kind="TRANSFER", id=f"TRANSFER:{src.node_id}:{qty}", qty=qty, unit_cost=0.0, report=report,
                     ctx=ref_ctx, from_node=src.node_id, deliveries=report.deliveries,
                     label=f"Transfer {qty} from {src.node_id} (day {day}); it can spare {excess}")

    def recommendation(self) -> None:
        rec = self.inp.recommendation
        if rec is None or rec.supplier_id not in self.terms:
            return
        t = self.terms[rec.supplier_id]
        req = calculate_net_requirement(_net_input(self.inp, t))
        opt = self.purchase(t, _ctx(self.inp, req), [Delivery(day=t.lead_time_days, qty=rec.qty)],
                            is_recommendation=True, id=f"REC:{rec.id}:{rec.qty}")
        if rec.supplier_id in self.unavailable and opt:
            # e.g. that supplier rejected our PO earlier in this run: the recommendation cannot be executed
            opt["violations"].append(Violation(code="SUPPLIER_EXCLUDED", hard=True,
                                               detail={"supplier": rec.supplier_id}))
            opt["blocked"] = True
        own = self.ref_req.order_qty
        if own > 0:
            self.rec_deviation_pct = round(abs(rec.qty - own) / own * 100, 2)
        self.rec_acceptable = (
            own > 0
            and self.rec_deviation_pct is not None
            and self.rec_deviation_pct <= self.inp.accept_tolerance_pct
            and not opt["violations"]  # validate_po: MOQ, case pack, storage, budget, cover <= max, ...
            and opt["stockout_day"] is None  # outcome projection: no stockout before the next cycle
            and rec.supplier_id not in self.unavailable
        )

    def no_purchase(self, kind: str, id: str, qty: int, ctx: PlanContext, *, label: str,
                    deliveries: list[Delivery] | None = None, po_id: str | None = None,
                    supplier_id: str | None = None, unit_cost: float = 0.0) -> None:
        """Options that commit no new spend: do nothing, accept a partial, keep a backorder, escalate."""
        report = evaluate_constraints(deliveries or [], 0.0, 1, ctx) if deliveries else None
        self.add(kind=kind, id=id, qty=qty, unit_cost=unit_cost, report=report, ctx=ctx, label=label,
                 deliveries=deliveries or [], po_id=po_id, supplier_id=supplier_id)

    def purchase(self, t: SupplierTerms, ctx: PlanContext, deliveries: list[Delivery], *,
                 only_if_valid: bool = False, note: str = "", is_recommendation: bool = False,
                 po_id: str | None = None, base_qty: int = 0, id: str | None = None) -> dict:
        draft = PODraft(supplier_id=t.supplier_id, sku="", deliveries=deliveries, unit_cost=t.unit_cost,
                        po_id=po_id, base_qty=base_qty)
        result = validate_po(draft, t, ctx, open_pos=[])
        blocked = any(v.hard and not v.overridable for v in result.violations)
        if only_if_valid and blocked:
            return {}
        qty = sum(d.qty for d in deliveries)
        if id is None:
            if po_id:
                id = f"INCREASE:{po_id}:{qty}"
            elif len(deliveries) > 1:
                id = f"SPLIT:{t.supplier_id}:" + "+".join(f"{d.qty}@{d.day}" for d in deliveries)
            else:
                id = f"BUY:{t.supplier_id}:{qty}"
        when = " + ".join(f"{d.qty} on day {d.day}" for d in deliveries)
        if po_id:
            label = f"Increase {po_id} by {qty} ({when})"
        elif is_recommendation:
            label = f"Recommendation as is: {qty} from {t.supplier_id} ({when})"
        else:
            label = f"Buy {qty} from {t.supplier_id} ({when})" + (f", {note}" if note else "")
        return self.add(kind="PURCHASE", id=id, qty=qty, unit_cost=t.unit_cost, report=result.report, ctx=ctx,
                        label=label, deliveries=deliveries, supplier_id=t.supplier_id, po_id=po_id,
                        violations=result.violations, is_recommendation=is_recommendation)

    # ------------------------------------------------------------------ evaluation & ranking

    def add(self, *, kind: str, id: str, qty: int, unit_cost: float, report: ConstraintReport | None,
            ctx: PlanContext, label: str, deliveries: list[Delivery], supplier_id: str | None = None,
            po_id: str | None = None, from_node: str | None = None, violations: list[Violation] | None = None,
            is_recommendation: bool = False) -> dict:
        if report is None:
            projection = project_inventory(ctx.available, ctx.forecast, ctx.existing_receipts, ctx.horizon)
            value, covers = 0.0, []
        else:
            projection = report.projection
            value = report.value if kind == "PURCHASE" else 0.0
            covers = report.cover_at_arrival
        violations = list(violations if violations is not None else (report.violations if report else []))
        end_level = projection.end_levels[-1]
        purchase = kind == "PURCHASE"
        ref_cost = self.ref_terms.unit_cost
        opt = dict(
            id=id, kind=kind, label=label, supplier_id=supplier_id, po_id=po_id, from_node=from_node,
            is_recommendation=is_recommendation, deliveries=deliveries, qty=qty, unit_cost=unit_cost, value=value,
            violations=violations,
            blocked=any(v.hard and not v.overridable for v in violations),
            needs_override=any(v.hard and v.overridable for v in violations),
            is_alternate_supplier=purchase and supplier_id is not None and not self.terms[supplier_id].is_primary,
            price_variance_pct=round((unit_cost - ref_cost) / ref_cost * 100, 2) if purchase else None,
            stockout_day=projection.stockout_day, unmet_units=projection.unmet_units, end_level=end_level,
            safety_shortfall=round(max(0.0, ctx.safety_stock - end_level), 2),
            cover_at_arrival=covers[0] if covers else None, projection=projection.end_levels,
        )
        if any(v.code == "BUDGET_EXCEEDED" for v in violations) and "BUDGET_EXCEEDED" in self.inp.refused_overrides:
            return opt  # a human already refused this override in this run
        if id in self.inp.excluded_option_ids or any(o["id"] == id for o in self.options):
            return opt
        self.options.append(opt)
        return opt

    def ranked(self) -> list[Option]:
        def group(o: dict) -> int:
            if o["kind"] == "ESCALATE":
                return 3
            if o["is_recommendation"]:
                return 0 if self.rec_acceptable else 2
            return 1

        def key(o: dict):
            overstock = any(v.code == "OVERSTOCK" for v in o["violations"])
            new_deliveries = 0 if o["po_id"] else len(o["deliveries"])
            return (group(o), o["blocked"], o["unmet_units"], overstock, o["safety_shortfall"], o["needs_override"],
                    o["is_alternate_supplier"], o["unit_cost"], new_deliveries,
                    abs(o["qty"] - self.ref_req.order_qty), o["id"])

        out = []
        for rank, o in enumerate(sorted(self.options, key=key), start=1):
            out.append(Option(rank=rank, tradeoffs=_tradeoffs(o), **o))
        return out


def _tradeoffs(o: dict) -> list[str]:
    t: list[str] = []
    for v in o["violations"]:
        d = v.detail
        if v.code == "BUDGET_EXCEEDED":
            t.append(f"needs budget override: {d['over_by']:,.0f} over the remaining {d['remaining']:,.0f}")
        elif v.code == "STORAGE_EXCEEDED":
            t.append(f"does not fit storage on day {d['day']}: max {d['max']}")
        elif v.code == "OVERSTOCK":
            t.append(f"overstock: {d['cover_days']} days of cover > {d['max_days']}")
        else:
            t.append(v.code)
    if o["stockout_day"] is not None:
        t.append(f"stockout on day {o['stockout_day']} ({o['unmet_units']:g} units unmet)")
    elif o["safety_shortfall"] > 0:
        t.append(f"ends {o['safety_shortfall']:g} below safety stock")
    if o["is_alternate_supplier"]:
        t.append(f"alternate supplier, price {o['price_variance_pct']:+g}% vs primary")
    if len(o["deliveries"]) > 1:
        t.append(f"{len(o['deliveries'])} deliveries")
    return t


def _net_input(inp: OptionsInput, t: SupplierTerms) -> NetRequirementInput:
    return NetRequirementInput(
        forecast=inp.forecast, on_hand=inp.on_hand, reserved=inp.reserved, inbound=inp.receipts,
        lead_time_days=t.lead_time_days, review_period_days=inp.review_period_days, safety_days=inp.safety_days,
        moq=t.moq, case_pack=t.case_pack,
    )


def _ctx(inp: OptionsInput, req: NetRequirement) -> PlanContext:
    return PlanContext(
        available=req.available, forecast=inp.forecast, horizon=req.horizon, avg_daily=req.avg_daily,
        safety_stock=req.safety_stock, existing_receipts=inp.receipts, storage=inp.storage,
        budget_remaining=inp.budget_remaining, max_days_cover=inp.max_days_cover,
    )

