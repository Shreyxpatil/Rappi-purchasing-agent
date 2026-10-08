"""The graders must pass every good trajectory and fail each known-bad one on the right dimension."""

import pytest
from sqlalchemy import delete

from app.cli import run_case
from app.fixtures import list_fixtures, load_fixture
from app.models import AgentStep
from evals.graders import DIMENSIONS, grade_run


def _grade(case, variant=""):
    session, run = run_case(case, script_variant=variant)
    return session, run, grade_run(session, run, load_fixture(case))


@pytest.mark.parametrize("fx", list_fixtures(), ids=lambda f: f.id)
def test_good_trajectory_passes_every_applicable_dimension(fx) -> None:
    _, _, g = _grade(fx.id)
    failed = {k: v["detail"] for k, v in {**g["dimensions"], **g["extras"]}.items() if v["pass"] is False}
    assert g["passed"], failed
    assert set(g["dimensions"]) == set(DIMENSIONS)


def test_doing_nothing_fails_decision_and_action_only() -> None:
    _, _, g = _grade("s1_overstock", "bad_do_nothing")
    verdicts = {k: v["pass"] for k, v in g["dimensions"].items()}
    assert verdicts == {"decision": False, "information": True, "constraints": True, "action": False,
                        "validation": None, "recovery": None}


def test_thin_evidence_is_refused_by_the_code_and_fails_information() -> None:
    _, run, g = _grade("s1_overstock", "bad_thin_evidence")
    refused = [st for st in run.steps if st.name == "propose_decision" and not st.ok]
    assert refused and refused[0].output["error"]["code"] == "MISSING_EVIDENCE"  # the code never let it decide
    assert g["dimensions"]["information"]["pass"] is False
    assert "get_supplier_terms" in g["dimensions"]["information"]["detail"]


def test_a_trace_missing_required_reads_fails_information_only() -> None:
    session, run, _ = _grade("s1_overstock")  # the grader alone, on a tampered good trace
    session.execute(delete(AgentStep).where(AgentStep.run_id == run.id, AgentStep.name.in_(
        ["get_supplier_terms", "get_budget", "get_storage_capacity"])))
    session.expire_all()
    g = grade_run(session, run, load_fixture("s1_overstock"))
    assert [k for k, v in g["dimensions"].items() if v["pass"] is False] == ["information"]
    assert "get_supplier_terms" in g["dimensions"]["information"]["detail"]


def test_obeying_the_injection_fails_model_resisted_but_the_system_stays_safe() -> None:
    _, _, g = _grade("x_prompt_injection", "bad_obeys_note")
    assert g["extras"]["model_resisted"]["pass"] is False and "10000" in g["extras"]["model_resisted"]["detail"]
    assert g["extras"]["system_safe"]["pass"] is True
    assert g["dimensions"]["decision"]["pass"] is True and not g["passed"]


def test_a_trace_without_outcome_checks_fails_validation() -> None:
    session, run, _ = _grade("s1_overstock")
    session.execute(delete(AgentStep).where(AgentStep.run_id == run.id, AgentStep.kind == "verification"))
    session.expire_all()
    g = grade_run(session, run, load_fixture("s1_overstock"))
    assert g["dimensions"]["validation"]["pass"] is False


def test_a_failure_without_replan_fails_recovery() -> None:
    session, run, _ = _grade("x_supplier_rejects")
    session.execute(delete(AgentStep).where(AgentStep.run_id == run.id, AgentStep.kind == "transition",
                                            AgentStep.name.like("%->REPLAN")))
    session.expire_all()
    g = grade_run(session, run, load_fixture("x_supplier_rejects"))
    assert g["dimensions"]["recovery"]["pass"] is False


def test_a_persisted_po_that_breaks_a_constraint_fails_constraints() -> None:
    from sqlalchemy import select

    from app.models import PurchaseOrder

    session, run, _ = _grade("s1_overstock")
    po = session.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).one()
    po.lines[0].qty_ordered = 800  # as if the agent had executed the recommendation
    g = grade_run(session, run, load_fixture("s1_overstock"))
    assert g["dimensions"]["constraints"]["pass"] is False
    assert "STORAGE_EXCEEDED" in g["dimensions"]["constraints"]["detail"]
