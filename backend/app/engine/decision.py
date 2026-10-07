"""From a chosen option to a structured decision: explicit outcome rules, factors, checks, confidence.

Outcome rules (decision D9 + D15):
  INVESTIGATE  the agent chose to investigate, or chose to escalate before acting
  ACCEPT       the recommendation is executed as is, or a partial delivery is accepted, or
               (no recommendation) the current plan is kept
  REJECT       a recommendation exists and the right action is to buy nothing
  MODIFY       anything else: a different quantity, supplier, delivery split or source
"""

from app.engine.types import (
    ConstraintCheck,
    DataIssue,
    Decision,
    DemandSignal,
    Factor,
    Option,
    OptionSet,
    Recommendation,
    Violation,
)

_CHECKS = {
    "MOQ": ("BELOW_MOQ",),
    "CASE_PACK": ("CASE_PACK_MISMATCH",),
    "STORAGE": ("STORAGE_EXCEEDED",),
    "BUDGET": ("BUDGET_EXCEEDED",),
    "COVER": ("OVERSTOCK",),
    "SUPPLIER_ACTIVE": ("SUPPLIER_INACTIVE", "SUPPLIER_MISMATCH"),
    "LEAD_TIME": ("LEAD_TIME_INFEASIBLE", "LEAD_TIME_MISSES_NEED_DATE"),
    "DUPLICATE_PO": ("DUPLICATE_OPEN_PO",),
    "DATA": ("DATA_MISSING",),
}
_PURCHASE_ONLY = {"MOQ", "CASE_PACK", "SUPPLIER_ACTIVE", "LEAD_TIME", "DUPLICATE_PO", "BUDGET"}


def derive_outcome(chosen: Option | None, *, investigate: bool, recommendation: Recommendation | None) -> str:
    if investigate or chosen is None or chosen.kind == "ESCALATE":
        return "INVESTIGATE"
    if chosen.is_recommendation or chosen.kind == "ACCEPT_PARTIAL":
        return "ACCEPT"
    if (recommendation and chosen.kind == "PURCHASE" and chosen.po_id is None
            and chosen.supplier_id == recommendation.supplier_id and chosen.qty == recommendation.qty
            and len(chosen.deliveries) == 1):
        return "ACCEPT"
    if chosen.kind == "NO_ACTION":
        return "REJECT" if recommendation else "ACCEPT"
    return "MODIFY"


def check_constraints(option: Option, overridden: frozenset[str] = frozenset()) -> list[ConstraintCheck]:
    out = []
    for name, codes in _CHECKS.items():
        hits: list[Violation] = [v for v in option.violations if v.code in codes]
        if not hits:
            status = "n/a" if option.kind != "PURCHASE" and name in _PURCHASE_ONLY else "pass"
            out.append(ConstraintCheck(name=name, status=status))
            continue
        v = hits[0]
        if v.code in overridden:
            status = "overridden"
        elif v.hard:
            status = "fail"
        else:
            status = "warning"
        out.append(ConstraintCheck(name=name, status=status, detail={"code": v.code, **v.detail}))
    return out


def build_decision(options: OptionSet, chosen: Option | None, *, investigate: bool = False,
                   recommendation: Recommendation | None = None, data_issues: list[DataIssue] | None = None,
                   demand_signal: DemandSignal | None = None, information_needed: list[str] | None = None,
                   overridden: frozenset[str] = frozenset()) -> Decision:
    data_issues = data_issues or []
    outcome = derive_outcome(chosen, investigate=investigate, recommendation=recommendation)
    acting = outcome != "INVESTIGATE" and chosen is not None
    rec_option = next((o for o in options.options if o.is_recommendation), None)

    info = list(information_needed or [])
    if outcome == "INVESTIGATE":
        for issue in data_issues:
            if issue.code == "STALE_DATA":
                info.append(f"fresh {issue.source} data (last updated {issue.detail['age_hours']:g}h ago, "
                            f"limit {issue.detail['limit_hours']:g}h)")
            elif issue.code == "MISSING_DATA":
                info.append(f"{issue.source} data (missing)")
            else:
                info.append(f"reconciled {issue.source} data ({issue.code})")

    return Decision(
        outcome=outcome,
        quantity=chosen.qty if acting else 0,
        option_id=chosen.id if acting else None,
        option_kind=chosen.kind if acting else "INVESTIGATE",
        supplier_id=chosen.supplier_id if acting else None,
        deliveries=chosen.deliveries if acting else [],
        value=chosen.value if acting else 0.0,
        factors=_factors(options, chosen if acting else None, recommendation, demand_signal, data_issues),
        constraints_checked=check_constraints(chosen, overridden) if acting else [],
        recommendation_check=check_constraints(rec_option) if rec_option else None,
        confidence=_confidence(outcome, chosen, data_issues, demand_signal),
        residual_risk=_residual_risk(chosen) if acting else None,
        information_needed=info,
        data_issues=data_issues,
        alternatives=[o.id for o in options.options if chosen is None or o.id != chosen.id][:2],
    )


def _factors(options: OptionSet, chosen: Option | None, rec: Recommendation | None,
             signal: DemandSignal | None, issues: list[DataIssue]) -> list[Factor]:
    r = options.reference
    f = [
        Factor(name="demand over horizon", value=f"{r.demand:g} units over {r.horizon} days ({r.avg_daily:g}/day)",
               effect="raises need"),
        Factor(name="safety stock", value=f"{r.safety_stock:g} units", effect="raises need"),
        Factor(name="available stock", value=f"{r.available} units", effect="lowers need"),
        Factor(name="inbound within horizon", value=f"{r.inbound} units", effect="lowers need"),
        Factor(name="own net requirement", value=f"{r.net:g} units -> order {r.order_qty}",
               effect="sets the quantity" if r.net > 0 else "nothing to buy"),
    ]
    if signal is not None:
        f.append(Factor(name="demand signal", value=f"{signal.classification} (recent {signal.recent_daily:g}/day "
                        f"vs baseline {signal.baseline_daily:g}/day)", effect=f"basis: {signal.suggested_basis}"))
    if rec is not None:
        dev = options.recommendation_deviation_pct
        rec_option = next((o for o in options.options if o.is_recommendation), None)
        failed = [v.code for v in rec_option.violations] if rec_option else []
        effect = "accepted as is" if options.recommendation_acceptable else (
            "blocked by " + ", ".join(failed) if failed else "outside tolerance")
        value = f"{rec.qty} units" + (f" ({dev:g}% from own)" if dev is not None else "")
        f.append(Factor(name="recommendation", value=value, effect=effect))
    if chosen is not None:
        if chosen.stockout_day is not None:
            f.append(Factor(name="projected stockout", value=f"day {chosen.stockout_day}, "
                            f"{chosen.unmet_units:g} units unmet", effect="residual risk"))
        if chosen.cover_at_arrival is not None:
            f.append(Factor(name="cover at arrival", value=f"{chosen.cover_at_arrival:g} days", effect="within limit"
                            if not any(v.code == "OVERSTOCK" for v in chosen.violations) else "overstock"))
        if chosen.is_alternate_supplier:
            f.append(Factor(name="alternate supplier", value=f"{chosen.supplier_id}, price "
                            f"{chosen.price_variance_pct:+g}% vs primary", effect="needs approval"))
    for i in issues:
        f.append(Factor(name=f"{i.source} data", value=i.code, effect="weakens evidence"))
    return f


def _confidence(outcome: str, chosen: Option | None, issues: list[DataIssue], signal: DemandSignal | None) -> str:
    if outcome == "INVESTIGATE" or issues or (signal and signal.classification in ("INCONCLUSIVE", "STOCKOUT_CENSORED")):
        return "low"
    if chosen and (chosen.stockout_day is not None or chosen.safety_shortfall > 0 or chosen.is_alternate_supplier
                   or chosen.needs_override or chosen.violations):
        return "medium"
    return "high"


def _residual_risk(chosen: Option) -> dict[str, float | int] | None:
    if chosen.stockout_day is not None:
        return {"stockout_day": chosen.stockout_day, "unmet_units": chosen.unmet_units}
    if chosen.safety_shortfall > 0:
        return {"safety_shortfall": chosen.safety_shortfall}
    return None
