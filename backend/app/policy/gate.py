"""The policy gate. Every action tool calls `evaluate` before writing anything.

Verdicts, checked in this order:
  ESCALATE  the run has hit a limit (validation failed twice, replan budget spent)
  BLOCK     no decision to act on, the action does not match the decision (decision binding),
            or pre-action validation found a hard violation nobody can override
  APPROVAL  value over the auto limit, alternate supplier, price variance, budget override,
            cancellation or quantity reduction below what the supplier confirmed
  AUTO      everything else
The LLM cannot reach the database except through tools that call this function.
"""

from typing import Any, Literal

from pydantic import BaseModel

from app.engine.types import Delivery, ValidationResult
from app.policy import Policy

Verdict = Literal["AUTO", "APPROVAL", "ESCALATE", "BLOCK"]

# Action kinds the act tools produce.
#   PURCHASE             new PO for the decided option
#   INCREASE             add quantity to an existing open PO line
#   ACKNOWLEDGE_PARTIAL  reduce a line to exactly what the supplier confirmed (reflects reality)
#   DECREASE / CANCEL    reduce below confirmed / cancel a line
#   TRANSFER             move stock from another node
#   PRICE_CHANGE         accept a supplier's new price on a submitted PO


class ProposedAction(BaseModel):
    kind: str
    sku: str
    supplier_id: str | None = None
    po_id: str | None = None
    from_node: str | None = None
    deliveries: list[Delivery] = []
    qty: int = 0
    unit_cost: float = 0.0
    value: float = 0.0
    currency: str = ""
    is_primary_supplier: bool = True
    reference_unit_cost: float | None = None  # the primary supplier's price


class GateResult(BaseModel):
    verdict: Verdict
    reasons: list[str]
    details: dict[str, Any] = {}


def evaluate(action: ProposedAction, *, policy: Policy, decision_option: dict[str, Any] | None,
             validation: ValidationResult | None, validation_failures: int, replans: int,
             human_approved: bool = False) -> GateResult:
    if validation_failures >= policy.max_validation_failures:
        return GateResult(verdict="ESCALATE", reasons=["VALIDATION_FAILED_TWICE"],
                          details={"validation_failures": validation_failures})
    if replans > policy.max_replans:
        return GateResult(verdict="ESCALATE", reasons=["MAX_REPLANS_REACHED"], details={"replans": replans})

    if action.kind != "ACKNOWLEDGE_PARTIAL":
        if decision_option is None:
            return GateResult(verdict="BLOCK", reasons=["NO_DECISION"],
                              details={"hint": "propose a decision before acting"})
        mismatch = _binding_mismatch(action, decision_option)
        if mismatch:
            return GateResult(verdict="BLOCK", reasons=["DECISION_BINDING_MISMATCH"], details=mismatch)

    if validation is not None:
        blocking = [v for v in validation.violations
                    if v.hard and not v.overridable and v.code not in validation.overridden]
        if blocking:
            return GateResult(verdict="BLOCK", reasons=["VALIDATION_FAILED"],
                              details={"violations": [v.model_dump() for v in blocking]})

    reasons = _approval_reasons(action, policy, validation)
    if reasons and not human_approved:
        return GateResult(verdict="APPROVAL", reasons=reasons, details=_approval_details(action, policy))
    return GateResult(verdict="AUTO", reasons=["APPROVED_BY_HUMAN"] if reasons else [])


def _binding_mismatch(action: ProposedAction, option: dict[str, Any]) -> dict[str, Any]:
    """Return what differs between the action and the decided option (empty = bound)."""
    if action.kind in ("CANCEL", "DECREASE", "PRICE_CHANGE"):
        return {}  # allowed under any decision, but always reviewed by a human (see _approval_reasons)

    # Which option shape each action kind may execute.
    shape = {"PURCHASE": ("PURCHASE", False), "INCREASE": ("PURCHASE", True), "TRANSFER": ("TRANSFER", False)}
    kind, changes_existing_po = shape[action.kind]
    if option.get("kind") != kind or (option.get("po_id") is not None) != changes_existing_po:
        return {"decided_option": option.get("id"), "action": action.kind}

    if action.kind == "PURCHASE":
        expected = {"supplier_id": option.get("supplier_id"),
                    "deliveries": [(d["day"], d["qty"]) for d in option.get("deliveries", [])]}
        got = {"supplier_id": action.supplier_id, "deliveries": [(d.day, d.qty) for d in action.deliveries]}
    elif action.kind == "INCREASE":
        expected = {"po_id": option.get("po_id"), "qty": option.get("qty")}
        got = {"po_id": action.po_id, "qty": action.qty}
    else:
        expected = {"from_node": option.get("from_node"), "qty": option.get("qty")}
        got = {"from_node": action.from_node, "qty": action.qty}
    return {} if expected == got else {"decided_option": option.get("id"), "expected": expected, "got": got}


def _approval_reasons(action: ProposedAction, policy: Policy, validation: ValidationResult | None) -> list[str]:
    reasons = []
    limit = policy.auto_execute_max_value.get(action.currency)
    if limit is not None and action.value > limit:
        reasons.append("OVER_AUTO_LIMIT")
    if action.kind in ("PURCHASE", "INCREASE") and not action.is_primary_supplier:
        reasons.append("ALTERNATE_SUPPLIER")
    if action.kind in ("PURCHASE", "INCREASE", "PRICE_CHANGE") and action.reference_unit_cost:
        variance = (action.unit_cost - action.reference_unit_cost) / action.reference_unit_cost * 100
        if variance > policy.price_variance_pct:
            reasons.append("PRICE_VARIANCE")
    if validation is not None and any(v.hard and v.overridable for v in validation.violations):
        reasons.append("BUDGET_OVERRIDE")
    if action.kind in ("CANCEL", "DECREASE"):
        reasons.append("CANCELLATION")
    return sorted(set(reasons), key=reasons.index)


def _approval_details(action: ProposedAction, policy: Policy) -> dict[str, Any]:
    d: dict[str, Any] = {"value": action.value, "currency": action.currency,
                         "auto_limit": policy.auto_execute_max_value.get(action.currency)}
    if action.reference_unit_cost:
        d["price_variance_pct"] = round(
            (action.unit_cost - action.reference_unit_cost) / action.reference_unit_cost * 100, 2)
    return d
