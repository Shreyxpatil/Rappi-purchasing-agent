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
