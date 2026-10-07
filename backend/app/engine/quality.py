"""Data-quality checks that decide whether the data can support a decision at all."""

from app.engine.replenishment import calculate_net_requirement
from app.engine.types import DataIssue, InventorySensitivity, NetRequirementInput


def assess_freshness(ages_hours: dict[str, float | None], limits_hours: dict[str, float]) -> list[DataIssue]:
    """One issue per source that is missing (age None) or older than its limit."""
    issues = []
    for source, age in ages_hours.items():
        limit = limits_hours.get(source)
        if age is None:
            issues.append(DataIssue(code="MISSING_DATA", source=source, detail={}))
        elif limit is not None and age > limit:
            issues.append(DataIssue(code="STALE_DATA", source=source,
                                    detail={"age_hours": round(age, 1), "limit_hours": limit}))
    return issues


def inventory_sensitivity(inp: NetRequirementInput, units_sold_since_count: int) -> InventorySensitivity:
    """Re-run the requirement as if the stock count had been reduced by sales recorded since it was taken.
    If the two answers differ, the decision depends on data we cannot trust: investigate."""
    as_recorded = calculate_net_requirement(inp)
    adjusted_inp = inp.model_copy(update={"on_hand": max(0, inp.on_hand - units_sold_since_count)})
    adjusted = calculate_net_requirement(adjusted_inp)
    return InventorySensitivity(
        units_sold_since_count=units_sold_since_count,
        as_recorded=as_recorded,
        adjusted=adjusted,
        decision_flips=as_recorded.order_qty != adjusted.order_qty,
    )


def check_consistency(on_hand: int, reserved: int) -> list[DataIssue]:
    issues = []
    if on_hand < 0:
        issues.append(DataIssue(code="CONFLICTING_DATA", source="inventory", detail={"on_hand": on_hand}))
    elif reserved > on_hand:
        issues.append(DataIssue(code="CONFLICTING_DATA", source="inventory",
                                detail={"on_hand": on_hand, "reserved": reserved}))
    return issues


def data_blocks_decision(issues: list[DataIssue], sensitivity: InventorySensitivity | None) -> bool:
    """Should the agent INVESTIGATE instead of acting?

    * missing or conflicting data always blocks;
    * a stale stock count blocks only if the order changes once sales since the count are deducted
      (or the sensitivity could not be computed). Otherwise the agent proceeds and records the staleness;
    * other stale sources (forecast, supplier terms) are recorded as weaker evidence but do not block.
    """
    if any(i.code in ("MISSING_DATA", "CONFLICTING_DATA") for i in issues):
        return True
    stale_inventory = any(i.code == "STALE_DATA" and i.source == "inventory" for i in issues)
    return stale_inventory and (sensitivity is None or sensitivity.decision_flips)
