"""Pre-action validation of a PO draft: the gate every create/update/submit passes through.

Returns machine-readable reason codes so the agent can replan (STORAGE_EXCEEDED{max: 672}),
not prose it has to interpret.
"""

from app.engine.constraints import evaluate_constraints
from app.engine.projection import project_inventory
from app.engine.types import OpenPORef, PlanContext, PODraft, SupplierTerms, ValidationResult, Violation

# A second PO in one of these states for the same supplier + SKU is a duplicate. Confirmed POs are
# not duplicates: they are inbound stock the requirement already counts.
_DUPLICATE_STATUSES = {"DRAFT", "PENDING_APPROVAL", "SUBMITTED"}


def validate_po(draft: PODraft, terms: SupplierTerms, ctx: PlanContext, open_pos: list[OpenPORef],
                approved_overrides: frozenset[str] = frozenset()) -> ValidationResult:
    v: list[Violation] = []

    if terms.supplier_id != draft.supplier_id:
        v.append(Violation(code="SUPPLIER_MISMATCH", hard=True,
                           detail={"draft": draft.supplier_id, "terms": terms.supplier_id}))
    if not terms.active:
        v.append(Violation(code="SUPPLIER_INACTIVE", hard=True, detail={"supplier": terms.supplier_id}))
    if not draft.deliveries or any(d.qty <= 0 for d in draft.deliveries):
        v.append(Violation(code="NON_POSITIVE_QTY", hard=True,
                           detail={"qtys": ",".join(str(d.qty) for d in draft.deliveries)}))

    total = draft.base_qty + sum(d.qty for d in draft.deliveries)
    if total < terms.moq:
        v.append(Violation(code="BELOW_MOQ", hard=True, detail={"moq": terms.moq, "qty": total}))
    for d in draft.deliveries:
        if d.qty % terms.case_pack:
            v.append(Violation(code="CASE_PACK_MISMATCH", hard=True, detail={
                "case_pack": terms.case_pack, "qty": d.qty,
                "nearest_valid": max(terms.case_pack, round(d.qty / terms.case_pack) * terms.case_pack)}))
        if d.day < terms.lead_time_days and draft.po_id is None:
            v.append(Violation(code="LEAD_TIME_INFEASIBLE", hard=True,
                               detail={"day": d.day, "earliest_day": terms.lead_time_days}))

    first_day = min((d.day for d in draft.deliveries), default=0)
    baseline = project_inventory(ctx.available, ctx.forecast, ctx.existing_receipts, ctx.horizon)
    if baseline.stockout_day is not None and first_day > baseline.stockout_day:
        # Still worth ordering, but this PO alone cannot prevent the stockout: a transfer or expedite is needed.
        v.append(Violation(code="LEAD_TIME_MISSES_NEED_DATE", hard=False,
                           detail={"need_day": baseline.stockout_day, "arrival_day": first_day}))

    for po in open_pos:
        if (po.supplier_id, po.sku) == (draft.supplier_id, draft.sku) and po.po_id != draft.po_id \
                and po.status in _DUPLICATE_STATUSES:
            v.append(Violation(code="DUPLICATE_OPEN_PO", hard=True, detail={"po_id": po.po_id, "status": po.status}))

    report = evaluate_constraints(draft.deliveries, draft.unit_cost, terms.case_pack, ctx)
    v.extend(report.violations)

    overridden = sorted({x.code for x in v if x.hard and x.overridable and x.code in approved_overrides})
    ok = not any(x.hard and x.code not in overridden for x in v)
    return ValidationResult(ok=ok, violations=v, overridden=overridden, report=report)
