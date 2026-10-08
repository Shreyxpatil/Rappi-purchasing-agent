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
    scripted = {r["id"] for r in rows if r["scripted"]}
    assert {"s1_overstock", "s2_partial_needs_alternate", "s4_budget_binding"} <= scripted


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
    assert client.post("/api/runs", json={"scenario_id": "s3_promo_uplift"}).status_code == 400  # no script yet
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
