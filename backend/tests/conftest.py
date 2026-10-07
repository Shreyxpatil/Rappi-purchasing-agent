import pytest
from sqlalchemy.orm import Session

from app.db import create_schema, make_engine, make_session_factory


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
        s.commit()
        return ToolContext(session=s, clock=clock, policy=get_policy(),
                           state=RunState(trigger=fx.trigger.model_dump()))

    yield make
    for s in sessions:
        s.close()
