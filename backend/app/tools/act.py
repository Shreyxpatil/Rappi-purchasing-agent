"""Action tools: the only way the agent changes anything.

Every action:
  1. replays its first response if the idempotency key was seen before (retries are safe);
  2. re-validates against current data (validate_po: the pre-action layer of the feedback loop);
  3. passes the policy gate (AUTO / APPROVAL / ESCALATE / BLOCK) with decision binding;
  4. writes PO events and an audit row, including for blocked attempts.
"""

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.engine.constraints import evaluate_constraints
from app.engine.replenishment import calculate_net_requirement
from app.engine.types import Delivery, PODraft, ValidationResult, Violation
from app.engine.validation import validate_po
from app.models import (
    Approval,
    AuditLog,
    IdempotencyRecord,
    POEvent,
    POLine,
    PurchaseOrder,
    StockTransfer,
)
from app.policy.gate import GateResult, ProposedAction, evaluate
from app.tools import data
from app.tools.compute import DeliveryArg, open_po_refs, plan_context
from app.tools.registry import Args, ToolContext, ToolError, tool


class ActArgs(Args):
    idempotency_key: str


# --------------------------------------------------------------------------- plumbing


def _idempotent(ctx: ToolContext, name: str, args: ActArgs, run) -> dict[str, Any]:
    request = args.model_dump(mode="json", exclude={"idempotency_key"})
    rec = ctx.session.get(IdempotencyRecord, args.idempotency_key)
    if rec is not None:
        if rec.tool != name or rec.request != request:
            raise ToolError("IDEMPOTENCY_CONFLICT", "key already used for a different request",
                            {"key": args.idempotency_key, "tool": rec.tool, "request": rec.request})
        return {**rec.response, "replayed": True}
    response = run()
    ctx.session.add(IdempotencyRecord(key=args.idempotency_key, tool=name, request=request, response=response,
                                      run_id=ctx.run_id, created_at=ctx.clock.now()))
    ctx.session.flush()
    return response


def _audit(ctx: ToolContext, action: str, entity: str, entity_id: str, details: dict[str, Any],
           actor: str = "agent") -> None:
    ctx.session.add(AuditLog(at=ctx.clock.now(), actor=actor, action=action, entity=entity, entity_id=entity_id,
                             run_id=ctx.run_id, details=details))


def _event(ctx: ToolContext, po: PurchaseOrder, type_: str, payload: dict[str, Any], source: str = "agent") -> None:
    po.events.append(POEvent(at=ctx.clock.now(), type=type_, source=source, payload=payload))
    po.updated_at = ctx.clock.now()


def decided_option(ctx: ToolContext) -> dict[str, Any] | None:
    d = ctx.state.decision
    if not d or not d.get("option_id") or not ctx.state.options:
        return None
    return next((o for o in ctx.state.options["options"] if o["id"] == d["option_id"]), None)


def _gate(ctx: ToolContext, action: ProposedAction, validation: ValidationResult | None, entity: str,
          entity_id: str, human_approved: bool = False) -> GateResult:
    result = evaluate(action, policy=ctx.policy, decision_option=decided_option(ctx), validation=validation,
                      validation_failures=ctx.state.validation_failures, replans=ctx.state.replans,
                      human_approved=human_approved)
    if result.verdict in ("BLOCK", "ESCALATE"):
        escalate = result.verdict == "ESCALATE"
        if result.verdict == "BLOCK":
            ctx.state.validation_failures += 1
            # The second blocked action in a run escalates (policy max_validation_failures, D19/D20).
            escalate = ctx.state.validation_failures >= ctx.policy.max_validation_failures
        _audit(ctx, f"{result.verdict}ED_{action.kind}", entity, entity_id,
               {"reasons": result.reasons, "details": result.details, "action": action.model_dump(mode="json")})
        ctx.session.flush()
        code = "ESCALATION_REQUIRED" if escalate else "BLOCKED"
        raise ToolError(code, f"policy gate: {', '.join(result.reasons)}",
                        {"reasons": result.reasons, **result.details})
    return result


def _get_po(ctx: ToolContext, po_id: str) -> PurchaseOrder:
    return data.get_po(ctx.session, po_id)


def _line(po: PurchaseOrder, sku: str) -> POLine:
    line = next((l for l in po.lines if l.sku == sku and l.status == "OPEN"), None)
    if line is None:
        raise ToolError("NOT_FOUND", f"{po.id} has no open line for {sku}", {"po_id": po.id, "sku": sku})
    return line


def _adjust_budget(ctx: ToolContext, po: PurchaseOrder, sku: str, delta_value: float) -> None:
    b = data.budget_row(ctx.session, ctx.clock, po.node_id, sku)
    if b is not None:
        b.committed = round(b.committed + delta_value, 2)
        b.updated_at = ctx.clock.now()


def _purchase_action(ctx: ToolContext, node: str, sku: str, supplier_id: str, deliveries: list[Delivery],
                     kind: str = "PURCHASE", po_id: str | None = None) -> ProposedAction:
    terms = data.supplier_terms(ctx.session, supplier_id, sku)
    qty = sum(d.qty for d in deliveries)
    return ProposedAction(
        kind=kind, sku=sku, supplier_id=supplier_id, po_id=po_id, deliveries=deliveries, qty=qty,
        unit_cost=terms.unit_cost, value=round(qty * terms.unit_cost, 2), currency=data.get_node(ctx.session, node).currency,
        is_primary_supplier=terms.is_primary, reference_unit_cost=data.primary_terms(ctx.session, sku).unit_cost)


def _validate(ctx: ToolContext, node: str, sku: str, supplier_id: str, deliveries: list[Delivery],
              po_id: str | None = None, base_qty: int = 0) -> ValidationResult:
    terms = data.supplier_terms(ctx.session, supplier_id, sku)
    draft = PODraft(supplier_id=supplier_id, sku=sku, unit_cost=terms.unit_cost, deliveries=deliveries,
                    po_id=po_id, base_qty=base_qty)
    pc = plan_context(ctx, node, sku, supplier_id, ctx.state.demand_basis)  # type: ignore[arg-type]
    refs = [r for r in open_po_refs(ctx, node, sku) if r.po_id != po_id]
    return validate_po(draft, terms, pc, refs, approved_overrides=frozenset(ctx.state.approved_overrides))


def _alternatives(ctx: ToolContext) -> list[dict[str, Any]]:
    """The decided option next to the best option that needs no human: what refusing would cost."""
    chosen = decided_option(ctx)
    if chosen is None or not ctx.state.options:
        return []

    currency = data.get_node(ctx.session, ctx.state.trigger["node"]).currency
    limit = ctx.policy.auto_execute_max_value.get(currency, float("inf"))

    def needs_human(o: dict[str, Any]) -> bool:
        return o["needs_override"] or o["is_alternate_supplier"] or o["value"] > limit

    fallback = next((o for o in ctx.state.options["options"]
                     if o["id"] != chosen["id"] and not o["blocked"] and not needs_human(o)
                     and o["kind"] != "ESCALATE"), None)
    return [_summary(o) for o in (chosen, fallback) if o is not None]


def _summary(o: dict[str, Any]) -> dict[str, Any]:
    over = next((v["detail"]["over_by"] for v in o["violations"] if v["code"] == "BUDGET_EXCEEDED"), 0)
    return {"option_id": o["id"], "label": o["label"], "qty": o["qty"], "value": o["value"], "over_budget": over,
            "stockout_day": o["stockout_day"], "unmet_units": o["unmet_units"], "tradeoffs": o["tradeoffs"]}


def _request_approval(ctx: ToolContext, gate: GateResult, action: dict[str, Any], summary: str,
                      with_alternatives: bool = False) -> Approval:
    if ctx.run_id is None:
        raise ToolError("NO_RUN", "approvals belong to an agent run")
    approval = Approval(run_id=ctx.run_id, reasons=gate.reasons, action=action, summary=summary,
                        alternatives=_alternatives(ctx) if with_alternatives else [], requested_at=ctx.clock.now())
    ctx.session.add(approval)
    ctx.session.flush()
    ctx.state.pending_approval_id = approval.id
    _audit(ctx, "APPROVAL_REQUESTED", "approval", str(approval.id), {"reasons": gate.reasons, "action": action})
    return approval


def _next_po_id(ctx: ToolContext) -> str:
    """Deterministic ids for agent-created POs (PO-9001, PO-9002, ...) so traces are reproducible."""
    n = ctx.session.scalar(select(func.count()).select_from(PurchaseOrder).filter_by(created_by="agent"))
    return f"PO-{9001 + (n or 0)}"


def _next_transfer_id(ctx: ToolContext) -> str:
    n = ctx.session.scalar(select(func.count()).select_from(StockTransfer))
    return f"TR-{1 + (n or 0):03d}"


def preview_gate(ctx: ToolContext, option: dict[str, Any]) -> dict[str, Any]:
    """What the gate will say about executing `option`, before anything is written (shown in POLICY_GATE)."""
    trig = ctx.state.trigger
    node, sku = trig["node"], trig["sku"]
    deliveries = [Delivery(**d) for d in option.get("deliveries", [])]
    validation: ValidationResult | None = None
    if option["kind"] == "PURCHASE" and option.get("po_id"):
        line = _line(_get_po(ctx, option["po_id"]), sku)
        action = _purchase_action(ctx, node, sku, option["supplier_id"], deliveries, "INCREASE", option["po_id"])
        validation = _validate(ctx, node, sku, option["supplier_id"], deliveries, po_id=option["po_id"],
                               base_qty=line.qty_ordered)
    elif option["kind"] == "PURCHASE":
        action = _purchase_action(ctx, node, sku, option["supplier_id"], deliveries)
        validation = _validate(ctx, node, sku, option["supplier_id"], deliveries)
    elif option["kind"] == "TRANSFER":
        action = ProposedAction(kind="TRANSFER", sku=sku, from_node=option["from_node"], qty=option["qty"],
                                currency=data.get_node(ctx.session, node).currency)
    else:  # accept partial / backorder: nothing new is committed
        return {"verdict": "AUTO", "reasons": [], "details": {}}
    return evaluate(action, policy=ctx.policy, decision_option=option, validation=validation,
                    validation_failures=ctx.state.validation_failures, replans=ctx.state.replans).model_dump()


# --------------------------------------------------------------------------- tools


class CreatePOArgs(ActArgs):
    node: str
    sku: str
    supplier_id: str
    deliveries: list[DeliveryArg]


@tool("create_po_draft", "act",
      "Create a DRAFT PO for the decided purchase option (supplier + deliveries). Validated and policy-gated.")
def create_po_draft(ctx: ToolContext, args: CreatePOArgs) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        deliveries = [Delivery(day=d.day, qty=d.qty) for d in args.deliveries]
        action = _purchase_action(ctx, args.node, args.sku, args.supplier_id, deliveries)
        validation = _validate(ctx, args.node, args.sku, args.supplier_id, deliveries)
        gate = _gate(ctx, action, validation, "purchase_order", "new")
        po = PurchaseOrder(id=_next_po_id(ctx), node_id=args.node,
                           supplier_id=args.supplier_id, status="DRAFT", currency=action.currency,
                           idempotency_key=args.idempotency_key, created_by="agent", run_id=ctx.run_id,
                           created_at=ctx.clock.now(), updated_at=ctx.clock.now())
        for d in deliveries:
            po.lines.append(POLine(sku=args.sku, qty_ordered=d.qty, unit_cost=action.unit_cost,
                                   expected_arrival=ctx.clock.day(d.day)))
        ctx.session.add(po)
        _event(ctx, po, "CREATED", {"deliveries": [d.model_dump() for d in deliveries], "value": action.value})
        _audit(ctx, "CREATE_PO_DRAFT", "purchase_order", po.id, {"value": action.value, "gate": gate.model_dump()})
        return {"po_id": po.id, "status": po.status, "value": action.value,
                "submit_will_need": gate.verdict, "approval_reasons": gate.reasons,
                "warnings": [v.code for v in validation.violations if not v.hard]}

    return _idempotent(ctx, "create_po_draft", args, run)


class POArgs(ActArgs):
    po_id: str


@tool("submit_po", "act", "Submit a DRAFT PO to the supplier. Re-validated; may require human approval.")
def submit_po(ctx: ToolContext, args: POArgs) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        po = _get_po(ctx, args.po_id)
        if po.status != "DRAFT":
            raise ToolError("INVALID_STATE", f"{po.id} is {po.status}, only DRAFT can be submitted",
                            {"status": po.status})
        return _submit(ctx, po)

    return _idempotent(ctx, "submit_po", args, run)


def _submit(ctx: ToolContext, po: PurchaseOrder, human_approved: bool = False) -> dict[str, Any]:
    sku = po.lines[0].sku
    deliveries = [Delivery(day=ctx.clock.day_offset(l.expected_arrival), qty=l.qty_ordered) for l in po.lines]
    action = _purchase_action(ctx, po.node_id, sku, po.supplier_id, deliveries)
    validation = _validate(ctx, po.node_id, sku, po.supplier_id, deliveries, po_id=po.id)
    gate = _gate(ctx, action, validation, "purchase_order", po.id, human_approved=human_approved)
    if gate.verdict == "APPROVAL":
        po.status = "PENDING_APPROVAL"
        approval = _request_approval(ctx, gate, {"tool": "submit_po", "po_id": po.id},
                                     f"Submit {po.id}: {action.qty} x {sku} from {po.supplier_id}, "
                                     f"{action.value:,.2f} {action.currency}", with_alternatives=True)
        _event(ctx, po, "APPROVAL_REQUESTED", {"approval_id": approval.id, "reasons": gate.reasons})
        return {"po_id": po.id, "status": po.status, "approval_id": approval.id, "approval_reasons": gate.reasons,
                "alternatives": approval.alternatives}
    po.status = "SUBMITTED"
    _adjust_budget(ctx, po, sku, action.value)
    _event(ctx, po, "SUBMITTED", {"value": action.value, "approved_by_human": human_approved})
    _audit(ctx, "SUBMIT_PO", "purchase_order", po.id, {"value": action.value, "gate": gate.model_dump()})
    return {"po_id": po.id, "status": po.status, "value": action.value}


class UpdateLineArgs(ActArgs):
    po_id: str
    sku: str
    new_qty: int
    reason: str


@tool("update_po_line", "act",
      "Change a PO line quantity. Reducing to the supplier-confirmed qty acknowledges a partial (auto); "
      "an increase must match the decided option; other reductions need approval.")
def update_po_line(ctx: ToolContext, args: UpdateLineArgs) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        po = _get_po(ctx, args.po_id)
        line = _line(po, args.sku)
        old = line.qty_ordered
        if args.new_qty <= 0:
            raise ToolError("INVALID_ARGUMENTS", "use cancel_po_line to remove a line", {"new_qty": args.new_qty})
        if args.new_qty == old:
            return {"po_id": po.id, "status": po.status, "qty": old, "changed": False}

        if line.qty_confirmed is not None and args.new_qty == line.qty_confirmed < old:
            action = ProposedAction(kind="ACKNOWLEDGE_PARTIAL", sku=args.sku, po_id=po.id, qty=args.new_qty,
                                    currency=po.currency)
            _gate(ctx, action, None, "purchase_order", po.id)
            line.qty_ordered = args.new_qty
            po.status = "CONFIRMED"
            _adjust_budget(ctx, po, args.sku, -(old - args.new_qty) * line.unit_cost)
            _event(ctx, po, "LINE_REDUCED_TO_CONFIRMED", {"from": old, "to": args.new_qty, "reason": args.reason})
            _audit(ctx, "ACKNOWLEDGE_PARTIAL", "purchase_order", po.id, {"from": old, "to": args.new_qty})
            return {"po_id": po.id, "status": po.status, "qty": args.new_qty, "changed": True}

        if args.new_qty > old:
            inc = args.new_qty - old
            delivery = [Delivery(day=ctx.clock.day_offset(line.expected_arrival), qty=inc)]
            action = _purchase_action(ctx, po.node_id, args.sku, po.supplier_id, delivery, "INCREASE", po.id)
            validation = _validate(ctx, po.node_id, args.sku, po.supplier_id, delivery, po_id=po.id, base_qty=old)
            gate = _gate(ctx, action, validation, "purchase_order", po.id)
            if gate.verdict == "APPROVAL":
                approval = _request_approval(ctx, gate, {"tool": "update_po_line", **args.model_dump(
                    exclude={"idempotency_key"})}, f"Increase {po.id} {args.sku} {old} -> {args.new_qty}",
                    with_alternatives=True)
                return {"po_id": po.id, "status": "PENDING_APPROVAL", "approval_id": approval.id,
                        "approval_reasons": gate.reasons}
            return _apply_increase(ctx, po, line, args.new_qty, args.reason)

        action = ProposedAction(kind="DECREASE", sku=args.sku, po_id=po.id, qty=args.new_qty, currency=po.currency)
        gate = _gate(ctx, action, None, "purchase_order", po.id)
        approval = _request_approval(ctx, gate, {"tool": "update_po_line", **args.model_dump(
            exclude={"idempotency_key"})}, f"Reduce {po.id} {args.sku} {old} -> {args.new_qty}")
        return {"po_id": po.id, "status": "PENDING_APPROVAL", "approval_id": approval.id,
                "approval_reasons": gate.reasons}

    return _idempotent(ctx, "update_po_line", args, run)


def _apply_increase(ctx: ToolContext, po: PurchaseOrder, line: POLine, new_qty: int, reason: str) -> dict[str, Any]:
    old = line.qty_ordered
    line.qty_ordered = new_qty
    po.status = "SUBMITTED"  # the supplier must confirm the change
    _adjust_budget(ctx, po, line.sku, (new_qty - old) * line.unit_cost)
    _event(ctx, po, "LINE_INCREASED", {"from": old, "to": new_qty, "reason": reason})
    _audit(ctx, "INCREASE_PO_LINE", "purchase_order", po.id, {"from": old, "to": new_qty})
    return {"po_id": po.id, "status": po.status, "qty": new_qty, "changed": True}


class CancelLineArgs(ActArgs):
    po_id: str
    sku: str
    reason: str


@tool("cancel_po_line", "act", "Request cancellation of a PO line. Always needs human approval.")
def cancel_po_line(ctx: ToolContext, args: CancelLineArgs) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        po = _get_po(ctx, args.po_id)
        line = _line(po, args.sku)
        action = ProposedAction(kind="CANCEL", sku=args.sku, po_id=po.id, qty=line.qty_ordered, currency=po.currency)
        gate = _gate(ctx, action, None, "purchase_order", po.id)
        approval = _request_approval(ctx, gate, {"tool": "cancel_po_line", **args.model_dump(
            exclude={"idempotency_key"})}, f"Cancel {po.id} line {args.sku} ({line.qty_ordered} units): {args.reason}")
        return {"po_id": po.id, "status": "PENDING_APPROVAL", "approval_id": approval.id,
                "approval_reasons": gate.reasons}

    return _idempotent(ctx, "cancel_po_line", args, run)


class TransferArgs(ActArgs):
    from_node: str
    to_node: str
    sku: str
    qty: int = Field(gt=0, le=100_000)


@tool("create_transfer", "act", "Plan a stock transfer between nodes in the same city for the decided option.")
def create_transfer(ctx: ToolContext, args: TransferArgs) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        if args.from_node not in {n.id for n in data.same_city_nodes(ctx.session, args.to_node)}:
            raise ToolError("INVALID_ARGUMENTS", "transfers are only allowed within a city",
                            {"from": args.from_node, "to": args.to_node})
        action = ProposedAction(kind="TRANSFER", sku=args.sku, from_node=args.from_node, qty=args.qty,
                                currency=data.get_node(ctx.session, args.to_node).currency)
        source = data.net_input(ctx.session, ctx.clock, args.from_node, args.sku,
                                data.primary_terms(ctx.session, args.sku))
        spare = max(0, int(-calculate_net_requirement(source).net))
        day = ctx.policy.transfer_lead_days
        report = evaluate_constraints([Delivery(day=day, qty=args.qty)], 0.0, 1,
                                      plan_context(ctx, args.to_node, args.sku, None, ctx.state.demand_basis))
        violations = list(report.violations)
        if args.qty > spare:
            violations.append(Violation(code="TRANSFER_EXCEEDS_SPARE", hard=True,
                                        detail={"spare": spare, "qty": args.qty}))
        validation = ValidationResult(ok=not any(v.hard for v in violations), violations=violations,
                                      overridden=[], report=report)
        _gate(ctx, action, validation, "stock_transfer", "new")
        t = StockTransfer(id=_next_transfer_id(ctx), from_node_id=args.from_node,
                          to_node_id=args.to_node, sku=args.sku, qty=args.qty, expected_arrival=ctx.clock.day(day),
                          status="PLANNED", idempotency_key=args.idempotency_key, run_id=ctx.run_id,
                          created_at=ctx.clock.now())
        ctx.session.add(t)
        _audit(ctx, "CREATE_TRANSFER", "stock_transfer", t.id, {"qty": args.qty, "spare_at_source": spare})
        return {"transfer_id": t.id, "status": t.status, "qty": t.qty, "arrival_day": day}

    return _idempotent(ctx, "create_transfer", args, run)


class RequestApprovalArgs(ActArgs):
    summary: str
    reasons: list[str]


@tool("request_approval", "act", "Ask a human to review the decision even though policy does not require it.")
def request_approval(ctx: ToolContext, args: RequestApprovalArgs) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        gate = GateResult(verdict="APPROVAL", reasons=["AGENT_REQUESTED", *args.reasons])
        approval = _request_approval(ctx, gate, {"tool": "review"}, args.summary)
        return {"approval_id": approval.id, "status": "PENDING_APPROVAL"}  # pauses the run like any approval

    return _idempotent(ctx, "request_approval", args, run)


class EscalateArgs(ActArgs):
    reason_code: str
    summary: str
    information_needed: list[str] = []


@tool("escalate", "act", "Hand the case to a human buyer with a reason code and what information is needed.")
def escalate(ctx: ToolContext, args: EscalateArgs) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        ctx.state.escalated = True
        _audit(ctx, "ESCALATE", "agent_run", str(ctx.run_id or 0), args.model_dump(exclude={"idempotency_key"}))
        return {"escalated": True, "reason_code": args.reason_code}

    return _idempotent(ctx, "escalate", args, run)


def gate_price_change(ctx: ToolContext, po: PurchaseOrder, proposed_unit_cost: float) -> GateResult:
    """A supplier proposed a new price on a submitted PO: accept it automatically within the variance
    policy, otherwise create an approval. Returns the gate result."""
    sku = po.lines[0].sku
    qty = sum(l.qty_ordered for l in po.lines)
    action = ProposedAction(kind="PRICE_CHANGE", sku=sku, supplier_id=po.supplier_id, po_id=po.id, qty=qty,
                            unit_cost=proposed_unit_cost, value=round(qty * proposed_unit_cost, 2),
                            currency=po.currency,
                            reference_unit_cost=data.primary_terms(ctx.session, sku).unit_cost)
    gate = _gate(ctx, action, None, "purchase_order", po.id)
    if gate.verdict == "APPROVAL":
        approval = _request_approval(ctx, gate, {"tool": "accept_price_change", "po_id": po.id,
                                                 "proposed_unit_cost": proposed_unit_cost},
                                     f"Accept {po.supplier_id}'s new price on {po.id}: {po.lines[0].unit_cost:g} -> "
                                     f"{proposed_unit_cost:g} {po.currency} ({action.value:,.2f} total)")
        _event(ctx, po, "APPROVAL_REQUESTED", {"approval_id": approval.id, "reasons": gate.reasons})
    else:
        _accept_price(ctx, po, proposed_unit_cost, "agent")
    return gate


def _accept_price(ctx: ToolContext, po: PurchaseOrder, unit_cost: float, actor: str) -> None:
    delta = sum(l.qty_ordered * (unit_cost - l.unit_cost) for l in po.lines)
    for l in po.lines:
        l.unit_cost = unit_cost
    po.status = "CONFIRMED"
    _adjust_budget(ctx, po, po.lines[0].sku, delta)
    _event(ctx, po, "PRICE_ACCEPTED", {"unit_cost": unit_cost}, source=actor)
    _audit(ctx, "ACCEPT_PRICE_CHANGE", "purchase_order", po.id, {"unit_cost": unit_cost}, actor=actor)


# --------------------------------------------------------------------------- human decisions on approvals


class ApprovalOutcome(BaseModel):
    approval_id: int
    status: str
    result: dict[str, Any]


def resolve_approval(ctx: ToolContext, approval_id: int, approve: bool, decided_by: str,
                     comment: str = "") -> ApprovalOutcome:
    """Apply a human's answer. Approving executes exactly the stored action; rejecting records the refusal so
    the refused option (and any override it needed) is never proposed again in this run (decision D12)."""
    approval = ctx.session.get(Approval, approval_id)
    if approval is None or approval.status != "PENDING":
        raise ToolError("INVALID_STATE", "approval is not pending", {"approval_id": approval_id})
    approval.status = "APPROVED" if approve else "REJECTED"
    approval.decided_at, approval.decided_by, approval.comment = ctx.clock.now(), decided_by, comment
    ctx.state.pending_approval_id = None
    _audit(ctx, f"APPROVAL_{approval.status}", "approval", str(approval.id),
           {"reasons": approval.reasons, "comment": comment}, actor="human")

    action = approval.action
    result: dict[str, Any] = {}
    if approve:
        if "BUDGET_OVERRIDE" in approval.reasons:
            ctx.state.approved_overrides = sorted({*ctx.state.approved_overrides, "BUDGET_EXCEEDED"})
        if action["tool"] == "submit_po":
            result = _submit(ctx, _get_po(ctx, action["po_id"]), human_approved=True)
        elif action["tool"] == "update_po_line":
            po = _get_po(ctx, action["po_id"])
            line = _line(po, action["sku"])
            if action["new_qty"] > line.qty_ordered:
                result = _apply_increase(ctx, po, line, action["new_qty"], action["reason"])
            else:
                old = line.qty_ordered
                line.qty_ordered = action["new_qty"]
                _adjust_budget(ctx, po, line.sku, -(old - action["new_qty"]) * line.unit_cost)
                _event(ctx, po, "LINE_REDUCED", {"from": old, "to": action["new_qty"]}, source="human")
                result = {"po_id": po.id, "qty": action["new_qty"]}
        elif action["tool"] == "accept_price_change":
            po = _get_po(ctx, action["po_id"])
            _accept_price(ctx, po, action["proposed_unit_cost"], "human")
            result = {"po_id": po.id, "status": po.status, "unit_cost": action["proposed_unit_cost"]}
        elif action["tool"] == "cancel_po_line":
            po = _get_po(ctx, action["po_id"])
            line = _line(po, action["sku"])
            line.status = "CANCELLED"
            _adjust_budget(ctx, po, line.sku, -line.qty_ordered * line.unit_cost)
            if all(l.status == "CANCELLED" for l in po.lines):
                po.status = "CANCELLED"
            _event(ctx, po, "LINE_CANCELLED", {"sku": line.sku, "reason": action["reason"]}, source="human")
            result = {"po_id": po.id, "status": po.status}
    else:
        option = decided_option(ctx)
        if option is not None:
            ctx.state.excluded_option_ids = sorted({*ctx.state.excluded_option_ids, option["id"]})
        if "BUDGET_OVERRIDE" in approval.reasons:
            ctx.state.refused_overrides = sorted({*ctx.state.refused_overrides, "BUDGET_EXCEEDED"})
        if action["tool"] in ("submit_po", "accept_price_change"):
            po = _get_po(ctx, action["po_id"])
            if action["tool"] == "accept_price_change":  # it was committed when submitted
                _adjust_budget(ctx, po, po.lines[0].sku, -sum(l.qty_ordered * l.unit_cost for l in po.lines))
            po.status = "CANCELLED"
            _event(ctx, po, "APPROVAL_REJECTED", {"approval_id": approval.id, "comment": comment}, source="human")
            result = {"po_id": po.id, "status": po.status}
    ctx.session.flush()
    return ApprovalOutcome(approval_id=approval.id, status=approval.status, result=result)
