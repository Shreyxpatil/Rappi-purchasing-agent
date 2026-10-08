"""States of a run and which tools the model may use in each.

INTAKE -> INVESTIGATE -> DECIDE -> POLICY_GATE -> EXECUTE -> VALIDATE -> AWAIT_SUPPLIER
       -> VERIFY_OUTCOME -> REPORT -> DONE, with VERIFY_OUTCOME -> REPLAN -> INVESTIGATE on failure
       (max replans from policy.yaml, then escalate).
Code owns every transition; the model only works inside INVESTIGATE, EXECUTE and REPORT.
"""

from enum import StrEnum

from app.tools import REGISTRY


class State(StrEnum):
    INTAKE = "INTAKE"
    INVESTIGATE = "INVESTIGATE"
    DECIDE = "DECIDE"
    POLICY_GATE = "POLICY_GATE"
    EXECUTE = "EXECUTE"
    VALIDATE = "VALIDATE"  # layer 2: database read back and diffed against the decision
    AWAIT_SUPPLIER = "AWAIT_SUPPLIER"  # layer 3: the supplier answers each submitted PO
    VERIFY_OUTCOME = "VERIFY_OUTCOME"  # layer 4: re-project with what was confirmed, compare with the prediction
    REPLAN = "REPLAN"  # back to INVESTIGATE with the failure, or escalate when the budget is spent
    REPORT = "REPORT"
    DONE = "DONE"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


def _tools(kind: str) -> set[str]:
    return {name for name, t in REGISTRY.items() if t.kind == kind}


ALLOWED_TOOLS: dict[State, set[str]] = {
    State.INVESTIGATE: _tools("read") | _tools("compute") | {"propose_decision"},
    State.EXECUTE: _tools("act") | {"finish_execution"},
    State.REPORT: set(),
}

# Evidence the model must have gathered (successful calls) before a decision is accepted.
REQUIRED_EVIDENCE: dict[str, set[str]] = {
    "recommendation_review": {"get_inventory", "get_forecast", "get_open_pos", "calculate_net_requirement",
                              "generate_options"},
    "supplier_response": {"get_open_pos", "get_inventory", "get_forecast", "calculate_net_requirement",
                          "generate_options"},
    "demand_alert": {"get_sales_history", "detect_demand_shift", "get_open_pos", "calculate_net_requirement",
                     "generate_options"},
}

MAX_TURNS = {State.INVESTIGATE: 12, State.EXECUTE: 8}
