"""Compute tools: thin wrappers that build engine inputs from the DB and call the pure engine.

The agent chooses *what* to compute (which supplier, which demand basis, which option to
project); the engine does every calculation.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.engine import decision as engine_decision
from app.engine.demand import apply_promotions, detect_demand_shift, run_rate_forecast
from app.engine.options import generate_options as engine_generate_options
from app.engine.projection import project_inventory as engine_project
from app.engine.quality import assess_freshness, check_consistency, data_blocks_decision, inventory_sensitivity
from app.engine.replenishment import calculate_net_requirement as engine_net_requirement
from app.engine.types import (
    DataIssue,
    Delivery,
    DemandSignal,
    InventorySensitivity,
    NetRequirement,
    OpenPORef,
    OptionsInput,
    OptionSet,
    PlanContext,
    PODraft,
    Projection,
    Receipt,
    Recommendation,
    ValidationResult,
)
from app.engine.validation import validate_po
from sqlalchemy import select

from app.models import OPEN_PO_STATUSES, AuditLog, PurchaseOrder, StockTransfer
from app.models import Recommendation as RecommendationRow
from app.tools import data
from app.tools.registry import Args, ToolContext, ToolError, tool

DemandBasis = Literal["forecast", "recent_run_rate", "promo_adjusted"]
FORECAST_DAYS = 14


class NodeSku(Args):
    node: str
    sku: str


class BasisArgs(NodeSku):
    demand_basis: DemandBasis = "forecast"


class DeliveryArg(Args):
    day: int = Field(ge=0, le=60, description="days from today")
    qty: int = Field(gt=0, le=100_000)


# --------------------------------------------------------------------------- shared assembly


def demand_signal(ctx: ToolContext, node: str, sku: str) -> DemandSignal:
    p = ctx.policy.demand_shift
    return detect_demand_shift(
        data.sales_days(ctx.session, ctx.clock, node, sku), data.promo_windows(ctx.session, ctx.clock, node, sku),
        recent_days=p.recent_days, elevated_pct=p.elevated_pct, sustained_min_days=p.sustained_min_days,
        bulk_share=p.bulk_share)


def basis_forecast(ctx: ToolContext, node: str, sku: str, basis: DemandBasis) -> list[float]:
    """Daily demand to plan on. A run-rate basis needs evidence: the guardrail is here, not in the prompt."""
    forecast = data.forecast_values(ctx.session, ctx.clock, node, sku, FORECAST_DAYS)
    if basis == "forecast":
        return forecast
    if basis == "promo_adjusted":
        return apply_promotions(forecast, data.promo_windows(ctx.session, ctx.clock, node, sku))
    signal = demand_signal(ctx, node, sku)
    if signal.classification != "SUSTAINED_SHIFT":
        raise ToolError("INSUFFICIENT_EVIDENCE",
                        f"recent run-rate can only be used for a SUSTAINED_SHIFT, signal is {signal.classification}",
                        {"classification": signal.classification, "elevated_days": signal.elevated_days,
                         "bulk_days": signal.bulk_days, "promo_days": signal.promo_days})
    return run_rate_forecast(signal.adjusted_daily, len(forecast))


def assess_data(ctx: ToolContext, node: str, sku: str) -> tuple[list[DataIssue], InventorySensitivity | None]:
    s, clock = ctx.session, ctx.clock
    inv = data.get_inventory_row(s, node, sku)
    forecast = data.forecast_rows(s, clock, node, sku, FORECAST_DAYS)
    storage = data.storage_row(s, node, sku)
    budget = data.budget_row(s, clock, node, sku)
    ages = {
        "inventory": data.age_hours(clock, inv.updated_at),
        "forecast": data.age_hours(clock, min(r.generated_at for r in forecast)),
        "storage": data.age_hours(clock, storage.updated_at) if storage else None,
        "budget": data.age_hours(clock, budget.updated_at) if budget else None,
    }
    issues = assess_freshness(ages, ctx.policy.freshness_limits_hours) + check_consistency(inv.on_hand, inv.reserved)
    sensitivity = None
    if any(i.code == "STALE_DATA" and i.source == "inventory" for i in issues):
        sold = data.units_sold_since(s, clock, node, sku, inv.updated_at)
        sensitivity = inventory_sensitivity(
            data.net_input(s, clock, node, sku, data.primary_terms(s, sku), [r.units for r in forecast]), sold)
    return issues, sensitivity


def recommendation_for(ctx: ToolContext) -> Recommendation | None:
    rec_id = ctx.state.trigger.get("recommendation_id")
    row = ctx.session.get(RecommendationRow, rec_id) if rec_id else None
    return Recommendation(id=row.id, supplier_id=row.supplier_id, qty=row.qty) if row else None


def build_options_input(ctx: ToolContext, node: str, sku: str, basis: DemandBasis) -> OptionsInput:
    s, clock = ctx.session, ctx.clock
    p = data.get_product(s, sku)
    inv = data.get_inventory_row(s, node, sku)
    trigger_po = ctx.state.trigger.get("po_id")
    excluded = list(ctx.state.excluded_suppliers)
    if ctx.state.trigger.get("type") == "supplier_response" and trigger_po:
        # The supplier that just under-delivered stays out of new purchases for the whole run, even after its
        # partial has been acknowledged (the PO is then CONFIRMED and no longer looks partial).
        excluded.append(data.get_po(s, trigger_po).supplier_id)
    return OptionsInput(
        forecast=basis_forecast(ctx, node, sku, basis), on_hand=inv.on_hand, reserved=inv.reserved,
        receipts=data.receipts(s, clock, node, sku), review_period_days=p.review_period_days,
        safety_days=p.safety_days, max_days_cover=p.max_days_cover, storage=data.storage_info(s, node, sku),
        budget_remaining=data.budget_remaining(s, clock, node, sku), suppliers=data.supplier_terms_list(s, sku),
        recommendation=recommendation_for(ctx),
        partial_fill=data.partial_fill(s, clock, trigger_po, sku) if trigger_po else None,
        open_lines=data.open_lines(s, clock, node, sku, exclude_po=trigger_po),
        transfer_sources=data.transfer_sources(s, clock, node, sku),
        transfer_lead_days=ctx.policy.transfer_lead_days, accept_tolerance_pct=ctx.policy.accept_tolerance_pct,
        excluded_suppliers=sorted(set(excluded)), excluded_option_ids=ctx.state.excluded_option_ids,
        refused_overrides=ctx.state.refused_overrides,
    )


def plan_context(ctx: ToolContext, node: str, sku: str, supplier_id: str | None, basis: DemandBasis) -> PlanContext:
    s = ctx.session
    terms = data.supplier_terms(s, supplier_id, sku) if supplier_id else data.primary_terms(s, sku)
    forecast = basis_forecast(ctx, node, sku, basis)
    req = engine_net_requirement(data.net_input(s, ctx.clock, node, sku, terms, forecast))
    return PlanContext(
        available=req.available, forecast=forecast, horizon=req.horizon, avg_daily=req.avg_daily,
        safety_stock=req.safety_stock, existing_receipts=data.receipts(s, ctx.clock, node, sku),
        storage=data.storage_info(s, node, sku), budget_remaining=data.budget_remaining(s, ctx.clock, node, sku),
        max_days_cover=data.get_product(s, sku).max_days_cover)


def current_options(ctx: ToolContext) -> OptionSet:
    if ctx.state.options is None:
        raise ToolError("NO_OPTIONS", "call generate_options first")
    return OptionSet.model_validate(ctx.state.options)


# --------------------------------------------------------------------------- tools


class RequirementArgs(BasisArgs):
    supplier_id: str | None = None  # default: the primary supplier


class RequirementOut(BaseModel):
    supplier_id: str
    demand_basis: str
    requirement: NetRequirement
    data_issues: list[DataIssue]
    inventory_sensitivity: InventorySensitivity | None
    data_blocks_decision: bool


@tool("calculate_net_requirement", "compute",
      "Own requirement: demand + safety stock - available - inbound, rounded to MOQ/case pack. Also data-quality checks.")
def calculate_net_requirement(ctx: ToolContext, args: RequirementArgs) -> RequirementOut:
    s = ctx.session
    terms = data.supplier_terms(s, args.supplier_id, args.sku) if args.supplier_id else data.primary_terms(s, args.sku)
    forecast = basis_forecast(ctx, args.node, args.sku, args.demand_basis)
    req = engine_net_requirement(data.net_input(s, ctx.clock, args.node, args.sku, terms, forecast))
    issues, sens = assess_data(ctx, args.node, args.sku)
    return RequirementOut(supplier_id=terms.supplier_id, demand_basis=args.demand_basis, requirement=req,
                          data_issues=issues, inventory_sensitivity=sens,
                          data_blocks_decision=data_blocks_decision(issues, sens))


@tool("detect_demand_shift", "compute",
      "Classify recent sales vs baseline: SUSTAINED_SHIFT, ONE_OFF_OUTLIER, PROMO, STOCKOUT_CENSORED, INCONCLUSIVE, NO_SHIFT.")
def detect_demand_shift_tool(ctx: ToolContext, args: NodeSku) -> DemandSignal:
    return demand_signal(ctx, args.node, args.sku)


class ProjectArgs(BasisArgs):
    option_id: str | None = None  # project a generated option
    hypothetical: list[DeliveryArg] = []  # or what-if receipts
    days: int | None = Field(None, ge=1, le=FORECAST_DAYS, description="default: the horizon")


class ProjectOut(BaseModel):
    demand_basis: str
    horizon: int
    safety_stock: float
    projection: Projection


@tool("project_inventory", "compute",
      "Day-by-day stock projection with existing inbound plus an option's deliveries or hypothetical receipts.")
def project_inventory(ctx: ToolContext, args: ProjectArgs) -> ProjectOut:
    pc = plan_context(ctx, args.node, args.sku, None, args.demand_basis)
    receipts = list(pc.existing_receipts)
    extra = [Receipt(day=d.day, qty=d.qty, source="hypothetical") for d in args.hypothetical]
    if args.option_id:
        opt = next((o for o in current_options(ctx).options if o.id == args.option_id), None)
        if opt is None:
            raise ToolError("UNKNOWN_OPTION", f"no option {args.option_id}", {"known": _option_ids(ctx)})
        receipts, option_receipts = _without_executed(ctx, opt, receipts, args.sku)
        extra += option_receipts
    days = args.days or pc.horizon
    return ProjectOut(demand_basis=args.demand_basis, horizon=pc.horizon, safety_stock=pc.safety_stock,
                      projection=engine_project(pc.available, pc.forecast, receipts + extra, days))


def _without_executed(ctx: ToolContext, opt, receipts: list[Receipt], sku: str) -> tuple[list[Receipt], list[Receipt]]:
    """Projecting "with this option" must count it exactly once, also after it was executed.

    Inbound already holds whatever this run put on record (a submitted PO, a planned transfer), so those
    receipts are taken out and the option's deliveries are added back once. An increase of an existing PO
    adds only the part the supplier has not confirmed yet.
    """
    option_receipts = [Receipt(day=d.day, qty=d.qty, source=opt.id) for d in opt.deliveries]
    if opt.kind == "PURCHASE" and opt.po_id:
        line = next((l for _, l in data.open_po_lines(ctx.session, ctx.state.trigger["node"], sku,
                                                      statuses=OPEN_PO_STATUSES) if l.po_id == opt.po_id), None)
        executed = ctx.run_id is not None and ctx.session.scalars(select(AuditLog).filter_by(
            run_id=ctx.run_id, action="INCREASE_PO_LINE", entity_id=opt.po_id)).first() is not None
        if executed and line is not None:
            pending = line.qty_ordered - (line.qty_confirmed if line.qty_confirmed is not None else line.qty_ordered)
            # an increase is a single delivery on the existing line's arrival day
            option_receipts = [Receipt(day=opt.deliveries[0].day, qty=pending, source=opt.id)] if pending > 0 else []
        return receipts, option_receipts
    mine = set()
    if ctx.run_id is not None:
        if opt.kind == "PURCHASE":
            mine = {po.id for po in ctx.session.scalars(select(PurchaseOrder).filter_by(
                run_id=ctx.run_id, supplier_id=opt.supplier_id))}
        elif opt.kind == "TRANSFER":
            mine = {t.id for t in ctx.session.scalars(select(StockTransfer).filter_by(run_id=ctx.run_id))}
    return [r for r in receipts if r.source not in mine], option_receipts


class ConstraintArgs(BasisArgs):
    supplier_id: str
    deliveries: list[DeliveryArg]


@tool("evaluate_constraints", "compute",
      "What-if check of a purchase (supplier + deliveries): MOQ, case pack, storage, budget, cover, lead time, duplicates.")
def evaluate_constraints(ctx: ToolContext, args: ConstraintArgs) -> ValidationResult:
    terms = data.supplier_terms(ctx.session, args.supplier_id, args.sku)
    draft = PODraft(supplier_id=args.supplier_id, sku=args.sku, unit_cost=terms.unit_cost,
                    deliveries=[Delivery(day=d.day, qty=d.qty) for d in args.deliveries])
    pc = plan_context(ctx, args.node, args.sku, args.supplier_id, args.demand_basis)
    return validate_po(draft, terms, pc, open_po_refs(ctx, args.node, args.sku),
                       approved_overrides=frozenset(ctx.state.approved_overrides))


def open_po_refs(ctx: ToolContext, node: str, sku: str) -> list[OpenPORef]:
    return [OpenPORef(po_id=po.id, supplier_id=po.supplier_id, sku=sku, status=po.status)
            for po, _ in data.open_po_lines(ctx.session, node, sku, statuses=OPEN_PO_STATUSES)]


class OptionSummary(BaseModel):
    id: str
    rank: int
    kind: str
    label: str
    qty: int
    value: float
    deliveries: list[Delivery]
    blocked: bool
    needs_override: bool
    stockout_day: int | None
    unmet_units: float
    safety_shortfall: float
    cover_at_arrival: float | None
    tradeoffs: list[str]


class RecommendationCheck(BaseModel):
    id: str
    qty: int
    deviation_pct: float | None
    acceptable_as_is: bool


class OptionsOut(BaseModel):
    demand_basis: str
    reference_supplier_id: str
    reference: NetRequirement
    recommendation: RecommendationCheck | None
    options: list[OptionSummary]


@tool("generate_options", "compute",
      "Ranked candidate actions (buy, split, increase PO, transfer, backorder, accept partial, do nothing, escalate) "
      "with trade-offs. Choose one by id.")
def generate_options(ctx: ToolContext, args: BasisArgs) -> OptionsOut:
    result = engine_generate_options(build_options_input(ctx, args.node, args.sku, args.demand_basis))
    ctx.state.options = result.model_dump(mode="json")
    ctx.state.demand_basis = args.demand_basis
    rec = recommendation_for(ctx)
    return OptionsOut(
        demand_basis=args.demand_basis, reference_supplier_id=result.reference_supplier_id, reference=result.reference,
        recommendation=RecommendationCheck(id=rec.id, qty=rec.qty, deviation_pct=result.recommendation_deviation_pct,
                                           acceptable_as_is=result.recommendation_acceptable) if rec else None,
        options=[OptionSummary(**o.model_dump(include=set(OptionSummary.model_fields))) for o in result.options],
    )


def build_decision(ctx: ToolContext, option_id: str | None, investigate: bool, information_needed: list[str]):
    """Used by the agent's propose_decision step: the decision is built by the engine, not the model."""
    options = current_options(ctx)
    chosen = None
    if option_id is not None:
        chosen = next((o for o in options.options if o.id == option_id), None)
        if chosen is None:
            raise ToolError("UNKNOWN_OPTION", f"no option {option_id}", {"known": _option_ids(ctx)})
    trig = ctx.state.trigger
    issues, sens = assess_data(ctx, trig["node"], trig["sku"])
    signal = demand_signal(ctx, trig["node"], trig["sku"]) if trig.get("type") == "demand_alert" else None
    return engine_decision.build_decision(
        options, chosen, investigate=investigate, recommendation=recommendation_for(ctx), data_issues=issues,
        demand_signal=signal, information_needed=information_needed,
        overridden=frozenset(ctx.state.approved_overrides), sensitivity=sens)


def _option_ids(ctx: ToolContext) -> list[str]:
    return [o["id"] for o in (ctx.state.options or {}).get("options", [])]
