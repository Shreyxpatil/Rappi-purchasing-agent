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


def test_infrastructure_failures_are_kept_out_of_pass_rates(tmp_path) -> None:
    from evals.run_evals import infra_error

    model_miss = {"case": "s1_overstock", "scenario": "S1", "provider": "openai_compat", "run": 1, "passed": False,
                  "dimensions": {"decision": False}, "extras": {}, "failures": {"decision": "got REJECT"},
                  "duration_s": 1.0, "status": "COMPLETED"}
    quota = {**model_miss, "case": "s2_partial_needs_alternate", "status": "FAILED", "failures": {},
             "run_error": "LLM_QUOTA_EXHAUSTED: daily quota exhausted"}
    crash = {**model_miss, "case": "s4_budget_binding", "error": "ConnectError: name resolution", "failures": {}}
    assert [infra_error(r) for r in (model_miss, quota, crash)] == [None, "LLM_QUOTA_EXHAUSTED", "ConnectError"]
    for r in (model_miss, quota, crash):
        r["infra_error"] = infra_error(r)
    store("openai_compat", [model_miss, quota, crash], tmp_path)
    report = write_report(tmp_path, tmp_path / "report.md").read_text()
    assert "**0% (0/1)**" in report  # only the model miss is graded
    assert "## Did not complete (infrastructure, excluded from pass rates)" in report
    assert "`s2_partial_needs_alternate` on Groq (OpenAI-compatible) (run 1): `LLM_QUOTA_EXHAUSTED`" in report
