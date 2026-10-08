"""The graders must pass every good trajectory."""

import pytest
from app.cli import run_case
from app.fixtures import list_fixtures, load_fixture
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
