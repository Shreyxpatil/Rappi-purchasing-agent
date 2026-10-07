"""Storage, budget and days-of-cover constraints for a purchase plan.

A plan is one or more deliveries of the same SKU. Every check returns a Violation with a
machine-readable code and the numbers the agent needs to replan (e.g. the max that fits).
"""

import math

from app.engine.projection import cover_days, project_inventory
from app.engine.replenishment import floor_to
from app.engine.types import Caps, ConstraintReport, Delivery, PlanContext, Receipt, Violation


def capacity_caps(ctx: PlanContext, day: int, unit_cost: float, case_pack: int) -> Caps:
    """Largest single delivery on `day` allowed by storage, budget and the cover limit."""
    before = level_before(ctx, day, extra=[])
    storage_free = max_storage = None
    if ctx.storage is not None:
        storage_free = ctx.storage.sku_capacity - before
        max_storage = floor_to(storage_free, case_pack)
    max_budget = None
    if ctx.budget_remaining is not None:
        max_budget = floor_to(math.floor(ctx.budget_remaining / unit_cost), case_pack)
    max_cover = floor_to(ctx.max_days_cover * ctx.avg_daily - before, case_pack)
    return Caps(day=day, level_before_arrival=before, storage_free=storage_free, max_qty_storage=max_storage,
                max_qty_budget=max_budget, max_qty_cover=max_cover)


def evaluate_constraints(deliveries: list[Delivery], unit_cost: float, case_pack: int,
                         ctx: PlanContext) -> ConstraintReport:
    violations: list[Violation] = []
    receipts = [Receipt(day=d.day, qty=d.qty, source="plan") for d in deliveries]
    total = sum(d.qty for d in deliveries)
    value = round(total * unit_cost, 2)

    if ctx.storage is None:
        violations.append(Violation(code="DATA_MISSING", hard=True, detail={"what": "storage_capacity"}))
    covers = []
    for i, d in enumerate(deliveries):
        earlier = receipts[:i]  # this plan's earlier deliveries are already on the shelf
        before = level_before(ctx, d.day, extra=earlier)
        after = before + d.qty
        if ctx.storage is not None and after > ctx.storage.sku_capacity:
            free = ctx.storage.sku_capacity - before
            violations.append(Violation(code="STORAGE_EXCEEDED", hard=True, detail={
                "day": d.day, "qty": d.qty, "free": free, "max": floor_to(free, case_pack)}))
        cover = cover_days(after, ctx.avg_daily)
        covers.append(cover)
        if cover > ctx.max_days_cover:
            violations.append(Violation(code="OVERSTOCK", hard=False, detail={
                "day": d.day, "cover_days": cover, "max_days": ctx.max_days_cover,
                "max_qty": floor_to(ctx.max_days_cover * ctx.avg_daily - before, case_pack)}))

    if ctx.budget_remaining is None and value > 0:  # transfers cost nothing: budget is irrelevant
        violations.append(Violation(code="DATA_MISSING", hard=True, detail={"what": "budget"}))
    elif ctx.budget_remaining is not None and value > ctx.budget_remaining:
        violations.append(Violation(code="BUDGET_EXCEEDED", hard=True, overridable=True, detail={
            "value": value, "remaining": ctx.budget_remaining, "over_by": round(value - ctx.budget_remaining, 2),
            "max_qty": floor_to(math.floor(ctx.budget_remaining / unit_cost), case_pack)}))

    projection = project_inventory(ctx.available, ctx.forecast, ctx.existing_receipts + receipts, ctx.horizon)
    return ConstraintReport(deliveries=deliveries, total_qty=total, value=value, violations=violations,
                            projection=projection, cover_at_arrival=covers)


def level_before(ctx: PlanContext, day: int, extra: list[Receipt]) -> float:
    """Stock on hand at the start of `day`, after other receipts that day, before this delivery."""
    if day == 0:
        return float(ctx.available + sum(r.qty for r in ctx.existing_receipts + extra if r.day == 0))
    p = project_inventory(ctx.available, ctx.forecast, ctx.existing_receipts + extra, day + 1)
    return p.start_levels[day]
