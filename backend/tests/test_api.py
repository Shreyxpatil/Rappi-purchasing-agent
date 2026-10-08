import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app("sqlite://")) as c:  # background tasks run before each call returns
        yield c


def test_scenarios_list_marks_which_have_scripts(client) -> None:
    rows = client.get("/api/scenarios").json()
    assert len(rows) == 17
    assert all(r["scripted"] for r in rows)  # every scenario has a scripted trajectory


def test_run_s1_scripted_end_to_end(client) -> None:
    started = client.post("/api/runs", json={"scenario_id": "s1_overstock", "provider": "scripted"})
    assert started.status_code == 202
    run = client.get(f"/api/runs/{started.json()['run_id']}").json()
    assert (run["status"], run["outcome"], run["quantity"]) == ("COMPLETED", "MODIFY", 240)
    assert run["narrative_source"] == "model" and run["steps"][0]["kind"] == "intake"
    assert [s["name"] for s in run["steps"] if s["kind"] == "transition"][-1] == "REPORT->DONE"
    pos = client.get("/api/purchase-orders").json()
    agent_po = next(p for p in pos["purchase_orders"] if p["created_by"] == "agent")
    assert (agent_po["status"], agent_po["lines"][0]["qty_ordered"]) == ("CONFIRMED", 240)
    assert [e["type"] for e in agent_po["events"]] == ["CREATED", "SUBMITTED", "CONFIRMED"]


def test_approval_inbox_resumes_the_run(client) -> None:
    run_id = client.post("/api/runs", json={"scenario_id": "s4_budget_binding"}).json()["run_id"]
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "AWAITING_APPROVAL"
    inbox = client.get("/api/approvals").json()
    assert len(inbox) == 1 and [a["qty"] for a in inbox[0]["alternatives"]] == [714, 342]
    r = client.post(f"/api/approvals/{inbox[0]['id']}", json={"approve": True, "decided_by": "cm"})
    assert r.status_code == 202
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "COMPLETED"
    assert client.get("/api/approvals").json() == []
    assert client.post(f"/api/approvals/{inbox[0]['id']}", json={"approve": True}).status_code == 409


def test_loading_another_scenario_supersedes_a_paused_run(client) -> None:
    paused = client.post("/api/runs", json={"scenario_id": "s4_budget_binding"}).json()["run_id"]
    approval = client.get("/api/approvals").json()[0]
    client.post("/api/runs", json={"scenario_id": "s1_overstock"})
    assert client.get(f"/api/runs/{paused}").json()["status"] == "SUPERSEDED"
    assert client.post(f"/api/approvals/{approval['id']}", json={"approve": True}).status_code == 409


def test_bad_requests(client) -> None:
    assert client.post("/api/runs", json={"scenario_id": "nope"}).status_code == 404
    r = client.post("/api/runs", json={"scenario_id": "s1_overstock", "provider": "gemini"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "CONFIG"  # no key/model configured in tests
    assert client.get("/api/runs/999").status_code == 404


def test_supplier_failure_can_be_injected_for_a_run(client) -> None:
    from app.models import Workspace

    body = {"scenario_id": "x_supplier_rejects",
            "supplier_behaviour": {"SUP-ALQ": [{"type": "REJECTED", "message": "injected"}],
                                   "SUP-ANDINA": [{"type": "CONFIRMED"}]}}
    run_id = client.post("/api/runs", json=body).json()["run_id"]
    with client.app.state.session_factory() as s:
        cfg = s.get(Workspace, 1).config
    assert cfg["supplier_behaviour"]["SUP-ALQ"] == [{"type": "REJECTED", "message": "injected"}]
    run = client.get(f"/api/runs/{run_id}").json()
    supplier_steps = [st for st in run["steps"] if st["kind"] == "supplier"]
    assert supplier_steps[0]["output"]["message"] == "injected"
    bad = client.post("/api/runs", json={**body, "supplier_behaviour": {"SUP-ALQ": [{"type": "EXPLODED"}]}})
    assert bad.status_code == 422


def test_cli_runs_a_case_and_answers_approvals_from_the_fixture() -> None:
    from app.cli import run_case

    session, run = run_case("s4_budget_binding")  # fixture approval_responses: ["APPROVE"]
    assert (run.status, run.decision["quantity"]) == ("COMPLETED", 714)
    session, run = run_case("s4_budget_override_rejected")  # ["REJECT"]: falls back to 342
    assert (run.status, run.decision["quantity"], run.replan_count) == ("COMPLETED", 342, 1)


def test_run_detail_includes_projection_for_the_chart(client) -> None:
    run_id = client.post("/api/runs", json={"scenario_id": "s1_overstock"}).json()["run_id"]
    proj = client.get(f"/api/runs/{run_id}").json()["projection"]
    assert proj["do_nothing"] == [100, 40, 100, 40, -20]
    assert proj["chosen"] == proj["confirmed"] == [100, 40, 100, 280, 220]
    assert proj["safety_stock"] == 120 and proj["chosen_label"].startswith("Buy 240 from SUP-ALQ")


def test_scenarios_carry_primary_supplier_and_eval_results_are_served(client) -> None:
    rows = {r["id"]: r for r in client.get("/api/scenarios").json()}
    assert rows["s1_overstock"]["primary_supplier"] == "SUP-ALQ"
    assert rows["s1_overstock"]["expected"] == {"outcome": "MODIFY", "qty_min": 240, "qty_max": 240}
    evals = client.get("/api/evals").json()
    scripted = next(e for e in evals if e["provider"] == "scripted")
    assert len(scripted["runs"]) == 51 and "narrative" not in scripted["runs"][0]
