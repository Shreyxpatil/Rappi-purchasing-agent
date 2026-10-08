import pytest
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import create_schema, make_engine, make_session_factory


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Tests never read the developer's .env: no real keys, no real provider, no quota spent."""
    monkeypatch.setattr("app.llm.factory.get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr("app.llm.preflight.get_settings", lambda: Settings(_env_file=None))


@pytest.fixture
def session() -> Session:
    engine = make_engine("sqlite://")
    create_schema(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture
def tool_ctx():
    """Factory: seed a scenario into a fresh in-memory DB and return a ToolContext for it."""
    from app.fixtures import load_fixture
    from app.models import AgentRun
    from app.policy import get_policy
    from app.seed import seed_workspace
    from app.tools.registry import RunState, ToolContext

    sessions = []

    def make(fixture_id: str) -> ToolContext:
        engine = make_engine("sqlite://")
        create_schema(engine)
        s = make_session_factory(engine)()
        sessions.append(s)
        fx = load_fixture(fixture_id)
        clock = seed_workspace(s, fx)
        run = AgentRun(scenario_id=fx.id, provider="test", trigger=fx.trigger.model_dump(), status="RUNNING",
                       state="INVESTIGATE", started_at=clock.now())
        s.add(run)
        s.commit()
        return ToolContext(session=s, clock=clock, policy=get_policy(), run_id=run.id,
                           state=RunState(trigger=fx.trigger.model_dump()))

    yield make
    for s in sessions:
        s.close()


@pytest.fixture
def run_case():
    """Factory: seed a case in a fresh DB and run the agent with its script (or the given turns)."""
    from app.agent.loop import PurchasingAgent
    from app.fixtures import load_fixture
    from app.llm.scripted import ScriptedClient
    from app.seed import seed_workspace

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
