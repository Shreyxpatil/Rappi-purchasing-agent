"""Start and continue runs: shared by the API and the CLI.

A run must never stay RUNNING after the task driving it has died: any exception in the task marks it
FAILED with a trace step, and runs left RUNNING by a previous server process are failed at startup.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.loop import PurchasingAgent
from app.agent.states import RunStatus
from app.fixtures import load_fixture
from app.llm.factory import make_client
from app.llm.preflight import require_ready
from app.models import AgentRun, AgentStep, Approval, Workspace
from app.seed import seed_workspace
from app.tools.registry import ToolError

log = logging.getLogger("app.runner")


def prepare_run(session: Session, case_id: str, provider: str,
                supplier_behaviour: dict[str, list[dict]] | None = None) -> AgentRun:
    """Load the scenario into the workspace and create a run (not started yet).

    The LLM client is built and a real provider's preflight passed first (key, credits and model available),
    so a misconfigured or unavailable provider fails before anything is reset.
    `supplier_behaviour` replaces the scenario's scripted supplier answers (demo: inject a failure).
    """
    fx = load_fixture(case_id)
    llm = make_client(provider, case_id=case_id)  # validates provider config (raises LLMError) before any reset
    require_ready(provider)
    for run in session.scalars(select(AgentRun).filter_by(status=RunStatus.AWAITING_APPROVAL)):
        run.status = RunStatus.SUPERSEDED  # its workspace is about to be replaced (decision D7)
        for a in session.scalars(select(Approval).filter_by(run_id=run.id, status="PENDING")):
            a.status = "SUPERSEDED"
    seed_workspace(session, fx)
    if supplier_behaviour is not None:
        ws = session.get(Workspace, 1)
        ws.config = {**ws.config, "supplier_behaviour": supplier_behaviour}
    session.commit()
    return PurchasingAgent(session, llm).start(fx.id, fx.trigger.model_dump())


def execute_run(session: Session, run_id: int) -> AgentRun:
    run = session.get(AgentRun, run_id)
    try:
        agent = PurchasingAgent(session, make_client(run.provider, case_id=run.scenario_id))
        return agent.run(run)
    except BaseException as e:  # including KeyboardInterrupt / SystemExit: never leave the run RUNNING
        session.rollback()
        fail_run(session, run_id, type(e).__name__, str(e))
        raise


def answer_approval(session: Session, approval_id: int, approve: bool, decided_by: str, comment: str) -> AgentRun:
    approval = session.get(Approval, approval_id)
    run_id = approval.run_id
    if approval.status != "PENDING":  # e.g. a double click: the first answer already resumed the run
        log.warning("approval %s is already %s; ignoring the second answer", approval_id, approval.status)
        return session.get(AgentRun, run_id)
    try:
        run = session.get(AgentRun, run_id)
        agent = PurchasingAgent(session, make_client(run.provider, case_id=run.scenario_id))
        return agent.resolve_and_resume(run, approval_id, approve, decided_by, comment)
    except ToolError as e:
        session.rollback()
        if e.code == "INVALID_STATE":  # lost a race with another answer; nothing was changed
            log.warning("approval %s: %s; ignoring", approval_id, e.message)
            return session.get(AgentRun, run_id)
        fail_run(session, run_id, e.code, e.message)
        raise
    except BaseException as e:
        session.rollback()
        fail_run(session, run_id, type(e).__name__, str(e))
        raise


def fail_run(session: Session, run_id: int, code: str, message: str) -> None:
    """Mark the run FAILED and put the error on its trace, where the UI shows it."""
    log.error("run %s failed: %s: %s", run_id, code, message)
    run = session.get(AgentRun, run_id)
    if run is None or run.status not in (RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL):
        return
    seq = (session.scalar(select(func.max(AgentStep.seq)).filter_by(run_id=run_id)) or 0) + 1
    session.add(AgentStep(run_id=run_id, seq=seq, state=run.state, kind="error", name=code,
                          output={"message": message[:2000]}, ok=False, created_at=run.started_at))
    run.status = RunStatus.FAILED
    run.context = {**(run.context or {}), "error": f"{code}: {message}"[:2000]}
    session.commit()


def fail_orphaned_runs(session: Session) -> list[int]:
    """At startup: a RUNNING run has no task in this new process, so it can never finish."""
    orphans = [r.id for r in session.scalars(select(AgentRun).filter_by(status=RunStatus.RUNNING))]
    for run_id in orphans:
        fail_run(session, run_id, "TASK_LOST", "the server stopped while this run was in progress")
    return orphans


def current_scenario(session: Session) -> str | None:
    ws = session.get(Workspace, 1)
    return ws.scenario_id if ws else None
