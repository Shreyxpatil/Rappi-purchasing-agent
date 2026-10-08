from evals.report import write_report
from evals.run_evals import evaluate, store


def test_runner_grades_stores_and_reports(tmp_path) -> None:
    rows = [evaluate("scripted", "s1_overstock", 1, None), evaluate("scripted", "x_prompt_injection", 1, None)]
    assert all(r["passed"] for r in rows) and rows[0]["dimensions"]["decision"] is True
    assert rows[1]["extras"] == {"model_resisted": True, "system_safe": True}
    store("scripted", rows, tmp_path)
    store("scripted", [evaluate("scripted", "s1_overstock", 1, None)], tmp_path)  # re-run replaces only that case
    report = write_report(tmp_path, tmp_path / "report.md").read_text()
    assert "| scripted | `scripted trajectories` | 2 × 1 | **100% (2/2)**" in report
    assert "| `x_prompt_injection` | scripted | 1 | 1/1 |" in report and "## Failures\n\nNone." in report


def test_a_provider_error_is_a_failed_run_not_a_crash() -> None:
    row = evaluate("gemini", "s1_overstock", 1, None)  # tests have no key configured
    assert row["passed"] is False and "CONFIG" in row["error"]


def test_a_run_that_fails_records_its_own_error(monkeypatch) -> None:
    import evals.run_evals as runner_mod
    from app.agent.loop import PurchasingAgent
    from app.db import create_schema, make_engine, make_session_factory
    from app.fixtures import load_fixture
    from app.llm.scripted import ScriptedClient
    from app.seed import seed_workspace
    from fixture_inputs import script_turns

    def truncated_run(case, provider):  # the model stops answering half way, like a provider outage
        engine = make_engine("sqlite://")
        create_schema(engine)
        session = make_session_factory(engine)()
        fx = load_fixture(case)
        seed_workspace(session, fx)
        session.commit()
        agent = PurchasingAgent(session, ScriptedClient(script_turns(case)[:2]))
        return session, agent.run(agent.start(fx.id, fx.trigger.model_dump()))

    monkeypatch.setattr(runner_mod, "run_case", truncated_run)
    row = evaluate("scripted", "s1_overstock", 1, None)
    assert row["passed"] is False and row["status"] == "FAILED"
    assert row["run_error"].startswith("SCRIPT_EXHAUSTED")
