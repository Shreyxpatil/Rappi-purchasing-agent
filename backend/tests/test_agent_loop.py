import json

import pytest
from sqlalchemy import select

from app.agent.loop import PurchasingAgent
from app.db import create_schema, make_engine, make_session_factory
from app.fixtures import load_fixture
from app.llm.scripted import ScriptedClient
from app.models import Approval, PurchaseOrder
from app.seed import seed_workspace


@pytest.fixture
def run_case():
    sessions = []

    def run(case_id: str, turns: list | None = None):
        engine = make_engine("sqlite://")
        create_schema(engine)
        s = make_session_factory(engine)()
        sessions.append(s)
        fx = load_fixture(case_id)
        seed_workspace(s, fx)
        s.commit()
        llm = ScriptedClient(turns, label=case_id) if turns is not None else ScriptedClient.for_case(case_id)
        agent = PurchasingAgent(s, llm)
        r = agent.start(fx.id, fx.trigger.model_dump())
        agent.run(r)
        return agent, r, s, fx

    yield run
    for s in sessions:
        s.close()


def _transitions(run):
    return [st.name for st in run.steps if st.kind == "transition"]


def _tool_errors(run):
    return [(st.name, st.output["error"]["code"]) for st in run.steps if st.kind == "tool" and not st.ok]


def test_s1_overstock_end_to_end(run_case) -> None:
    _, run, s, fx = run_case("s1_overstock")
    assert run.status == "COMPLETED"
    assert _transitions(run) == ["INTAKE->INVESTIGATE", "INVESTIGATE->DECIDE", "DECIDE->POLICY_GATE",
                                 "POLICY_GATE->EXECUTE", "EXECUTE->REPORT", "REPORT->DONE"]
    assert (run.decision["outcome"], run.decision["quantity"]) == (fx.expected.outcome, fx.expected.qty_min)
    po = s.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).one()
    assert (po.supplier_id, po.status, po.lines[0].qty_ordered) == ("SUP-ALQ", "SUBMITTED", 240)
    assert po.idempotency_key == f"run{run.id}:create"  # keys are scoped to the run
    assert "240" in run.narrative and _tool_errors(run) == []
    policy = next(st for st in run.steps if st.kind == "policy")
    assert policy.name == "AUTO"


def test_s1_already_covered_rejects_without_acting(run_case) -> None:
    _, run, s, _ = run_case("s1_already_covered")
    assert (run.status, run.decision["outcome"], run.decision["quantity"]) == ("COMPLETED", "REJECT", 0)
    assert "POLICY_GATE" not in " ".join(_transitions(run))
    assert s.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).first() is None


def test_s1_stale_inventory_investigates_and_escalates(run_case) -> None:
    _, run, _, _ = run_case("s1_stale_inventory")
    assert (run.status, run.decision["outcome"]) == ("ESCALATED", "INVESTIGATE")
    assert run.decision["information_needed"][0] == "fresh cycle count of LECHE-ALQ-1L at BOG-02"
    esc = next(st for st in run.steps if st.kind == "escalation")
    assert esc.name == "INSUFFICIENT_DATA"


def test_s4_budget_pauses_for_approval_then_resumes(run_case) -> None:
    agent, run, s, fx = run_case("s4_budget_binding")
    assert (run.status, run.state) == ("AWAITING_APPROVAL", "EXECUTE")
    approval = s.scalars(select(Approval).filter_by(run_id=run.id)).one()
    assert set(approval.reasons) == set(fx.expected.approval_reasons)
    assert [a["qty"] for a in approval.alternatives] == [714, 342]

    agent.resolve_and_resume(run, approval.id, approve=True, decided_by="category.manager")
    assert run.status == "COMPLETED"
    po = s.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).one()
    assert (po.status, po.lines[0].qty_ordered) == ("SUBMITTED", 714)


def test_rejected_override_replans_to_the_budget_fallback(run_case) -> None:
    agent, run, s, fx = run_case("s4_budget_override_rejected")
    approval = s.scalars(select(Approval).filter_by(run_id=run.id)).one()
    agent.resolve_and_resume(run, approval.id, approve=False, decided_by="cm", comment="no budget left this month")
    assert run.status == "COMPLETED" and run.replan_count == 1
    assert (run.decision["outcome"], run.decision["quantity"]) == ("MODIFY", 342)
    assert run.decision["residual_risk"] == fx.expected.residual_risk
    statuses = {p.lines[0].qty_ordered: p.status for p in s.scalars(select(PurchaseOrder).filter_by(run_id=run.id))}
    assert statuses == {714: "CANCELLED", 342: "SUBMITTED"}
    assert "EXECUTE->INVESTIGATE" in _transitions(run)  # the replan after the rejection


def test_s2_acknowledges_partial_and_sources_alternate_with_approval(run_case) -> None:
    agent, run, s, _ = run_case("s2_partial_needs_alternate")
    assert run.status == "AWAITING_APPROVAL"
    approval = s.scalars(select(Approval).filter_by(run_id=run.id)).one()
    agent.resolve_and_resume(run, approval.id, approve=True, decided_by="buyer")
    assert run.status == "COMPLETED"
    assert s.get(PurchaseOrder, "PO-2002").status == "CONFIRMED"
    ceda = s.scalars(select(PurchaseOrder).filter_by(run_id=run.id, supplier_id="SUP-CEDA")).one()
    assert (ceda.status, ceda.lines[0].qty_ordered) == ("SUBMITTED", 204)


READS = {"tool_calls": [{"name": "get_inventory", "args": {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}}]}


def test_decision_without_evidence_is_refused(run_case) -> None:
    turns = [READS,
             {"tool_calls": [{"name": "propose_decision", "args": {"option_id": "BUY:SUP-ALQ:240"}}]}]
    _, run, _, _ = run_case("s1_overstock", turns)
    err = next(st for st in run.steps if st.name == "propose_decision")
    assert err.output["error"]["code"] == "MISSING_EVIDENCE"
    assert set(err.output["error"]["details"]["missing"]) == {"get_forecast", "get_open_pos",
                                                              "calculate_net_requirement", "generate_options"}
    assert run.status == "FAILED"  # script ran out: no decision was ever accepted


def test_action_tools_are_not_available_while_investigating(run_case) -> None:
    turns = [{"tool_calls": [{"name": "create_po_draft", "args": {"node": "BOG-01", "sku": "LECHE-ALQ-1L",
              "supplier_id": "SUP-ALQ", "deliveries": [{"day": 3, "qty": 10000}], "idempotency_key": "x"}}]}]
    _, run, s, _ = run_case("x_prompt_injection", turns)
    assert _tool_errors(run) == [("create_po_draft", "TOOL_NOT_ALLOWED_IN_STATE")]
    assert s.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).first() is None


def test_stale_data_blocks_acting_and_forces_investigation(run_case) -> None:
    script = json.loads(open("evals/scripts/s1_stale_inventory.json").read())["turns"]
    turns = script[:2] + [{"tool_calls": [{"name": "propose_decision", "args": {"option_id": "NO_ACTION"}}]}]
    _, run, _, _ = run_case("s1_stale_inventory", turns)
    assert _tool_errors(run) == [("propose_decision", "DATA_BLOCKS_DECISION")]
