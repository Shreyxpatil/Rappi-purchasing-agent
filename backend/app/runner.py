"""Start and continue runs: shared by the API (and the eval runner in P6)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.loop import PurchasingAgent
from app.agent.states import RunStatus
from app.fixtures import load_fixture
from app.llm.factory import make_client
from app.models import AgentRun, Approval, Workspace
from app.seed import seed_workspace


def prepare_run(session: Session, case_id: str, provider: str) -> AgentRun:
    """Load the scenario into the workspace and create a run (not started yet).

    The LLM client is built first, so a misconfigured provider fails before anything is reset.
    """
    fx = load_fixture(case_id)
    make_client(provider, case_id=case_id)  # validates provider config (raises LLMError)
    for run in session.scalars(select(AgentRun).filter_by(status=RunStatus.AWAITING_APPROVAL)):
        run.status = RunStatus.SUPERSEDED  # its workspace is about to be replaced (decision D7)
        for a in session.scalars(select(Approval).filter_by(run_id=run.id, status="PENDING")):
            a.status = "SUPERSEDED"
    seed_workspace(session, fx)
    session.commit()
    agent = PurchasingAgent(session, make_client(provider, case_id=case_id))
    return agent.start(fx.id, fx.trigger.model_dump())


def execute_run(session: Session, run_id: int) -> AgentRun:
    run = session.get(AgentRun, run_id)
    agent = PurchasingAgent(session, make_client(run.provider, case_id=run.scenario_id))
    try:
        return agent.run(run)
    except Exception as e:  # a bug must not leave a run "RUNNING" forever
        session.rollback()
        _mark_failed(session, run, e)
        raise


def answer_approval(session: Session, approval_id: int, approve: bool, decided_by: str, comment: str) -> AgentRun:
    approval = session.get(Approval, approval_id)
    run = session.get(AgentRun, approval.run_id)
    agent = PurchasingAgent(session, make_client(run.provider, case_id=run.scenario_id))
    try:
        return agent.resolve_and_resume(run, approval_id, approve, decided_by, comment)
    except Exception as e:
        session.rollback()
        _mark_failed(session, run, e)
        raise


def current_scenario(session: Session) -> str | None:
    ws = session.get(Workspace, 1)
    return ws.scenario_id if ws else None


def _mark_failed(session: Session, run: AgentRun, e: Exception) -> None:
    run = session.get(AgentRun, run.id)
    run.status = RunStatus.FAILED
    run.state = "DONE"
    run.context = {**(run.context or {}), "error": f"{type(e).__name__}: {e}"}
    session.commit()
