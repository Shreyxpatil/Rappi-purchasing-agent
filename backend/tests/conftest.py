import pytest
from sqlalchemy.orm import Session

from app.db import create_schema, make_engine, make_session_factory


@pytest.fixture
def session() -> Session:
    engine = make_engine("sqlite://")
    create_schema(engine)
    with make_session_factory(engine)() as s:
        yield s
