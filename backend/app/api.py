"""HTTP API for the UI: scenarios, runs (with their full step trace), approvals and purchase orders."""

import logging
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import runner
from app.fixtures import SupplierResponse, list_fixtures
from app.llm.base import LLMError
from app.llm.scripted import SCRIPTS_DIR
from app.models import AgentRun, Approval, PurchaseOrder, StockTransfer, Workspace

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)


def get_session(request: Request):
    with request.app.state.session_factory() as s:
        yield s


# --------------------------------------------------------------------------- scenarios & workspace


@router.get("/scenarios")
def scenarios() -> list[dict[str, Any]]:
    return [{"id": f.id, "scenario": f.scenario, "title": f.title, "description": f.description,
             "trigger": f.trigger.model_dump(), "scripted": (SCRIPTS_DIR / f"{f.id}.json").exists()}
            for f in list_fixtures()]


@router.get("/workspace")
def workspace(s: Session = Depends(get_session)) -> dict[str, Any]:
    ws = s.get(Workspace, 1)
    return {"scenario_id": ws.scenario_id, "as_of": ws.as_of} if ws else {"scenario_id": None}


# --------------------------------------------------------------------------- runs


class StartRun(BaseModel):
    scenario_id: str
    provider: Literal["scripted", "gemini", "openai_compat"] = "scripted"
    # Optional: replace the scenario's supplier answers, e.g. {"SUP-ALQ": [{"type": "REJECTED"}]}.
    # Meant for real providers; a scripted trajectory only follows the failures its scenario scripts.
    supplier_behaviour: dict[str, list[SupplierResponse]] | None = None


@router.post("/runs", status_code=202)
def start_run(body: StartRun, background: BackgroundTasks, request: Request,
              s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        behaviour = None if body.supplier_behaviour is None else {
            k: [r.model_dump(exclude_none=True) for r in v] for k, v in body.supplier_behaviour.items()}
        run = runner.prepare_run(s, body.scenario_id, body.provider, behaviour)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown scenario {body.scenario_id}")
    except LLMError as e:
        raise HTTPException(400, {"code": e.code, "message": str(e)})
    background.add_task(_in_new_session, request, runner.execute_run, run.id)
    return {"run_id": run.id, "status": run.status}


@router.get("/runs")
def list_runs(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    runs = s.scalars(select(AgentRun).order_by(AgentRun.id.desc()))
    return [_run_summary(r) for r in runs]


@router.get("/runs/{run_id}")
def get_run(run_id: int, s: Session = Depends(get_session)) -> dict[str, Any]:
    run = s.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    approvals = s.scalars(select(Approval).filter_by(run_id=run_id).order_by(Approval.id))
    return {**_run_summary(run), "trigger": run.trigger, "decision": run.decision, "narrative": run.narrative,
            "narrative_source": (run.context or {}).get("narrative_source"), "error": (run.context or {}).get("error"),
            "steps": [{"seq": st.seq, "state": st.state, "kind": st.kind, "name": st.name, "input": st.input,
                       "output": st.output, "ok": st.ok, "latency_ms": st.latency_ms, "tokens_in": st.tokens_in,
                       "tokens_out": st.tokens_out} for st in run.steps],
            "approvals": [_approval(a) for a in approvals]}


def _run_summary(r: AgentRun) -> dict[str, Any]:
    return {"id": r.id, "scenario_id": r.scenario_id, "provider": r.provider, "status": r.status, "state": r.state,
            "outcome": (r.decision or {}).get("outcome"), "quantity": (r.decision or {}).get("quantity"),
            "replans": r.replan_count, "started_at": r.started_at, "finished_at": r.finished_at}


# --------------------------------------------------------------------------- approvals


@router.get("/approvals")
def list_approvals(status: str | None = "PENDING", s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = select(Approval).order_by(Approval.id.desc())
    if status:
        q = q.filter_by(status=status)
    return [_approval(a) for a in s.scalars(q)]


class Answer(BaseModel):
    approve: bool
    decided_by: str = "buyer"
    comment: str = ""


@router.post("/approvals/{approval_id}", status_code=202)
def answer_approval(approval_id: int, body: Answer, background: BackgroundTasks, request: Request,
                    s: Session = Depends(get_session)) -> dict[str, Any]:
    a = s.get(Approval, approval_id)
    if a is None:
        raise HTTPException(404, "unknown approval")
    if a.status != "PENDING":
        raise HTTPException(409, f"approval is {a.status}")
    background.add_task(_in_new_session, request, runner.answer_approval, approval_id, body.approve,
                        body.decided_by, body.comment)
    return {"approval_id": approval_id, "run_id": a.run_id, "accepted": True}


def _approval(a: Approval) -> dict[str, Any]:
    return {"id": a.id, "run_id": a.run_id, "status": a.status, "reasons": a.reasons, "summary": a.summary,
            "action": a.action, "alternatives": a.alternatives, "requested_at": a.requested_at,
            "decided_at": a.decided_at, "decided_by": a.decided_by, "comment": a.comment}


# --------------------------------------------------------------------------- purchase orders


@router.get("/purchase-orders")
def purchase_orders(s: Session = Depends(get_session)) -> dict[str, Any]:
    pos = s.scalars(select(PurchaseOrder).order_by(PurchaseOrder.id))
    transfers = s.scalars(select(StockTransfer).order_by(StockTransfer.id))
    return {
        "scenario_id": runner.current_scenario(s),
        "purchase_orders": [{
            "id": p.id, "node": p.node_id, "supplier": p.supplier_id, "status": p.status, "currency": p.currency,
            "created_by": p.created_by, "run_id": p.run_id,
            "lines": [{"sku": l.sku, "qty_ordered": l.qty_ordered, "qty_confirmed": l.qty_confirmed,
                       "unit_cost": l.unit_cost, "expected_arrival": l.expected_arrival, "status": l.status}
                      for l in p.lines],
            "events": [{"at": e.at, "type": e.type, "source": e.source, "payload": e.payload} for e in p.events],
        } for p in pos],
        "transfers": [{"id": t.id, "from": t.from_node_id, "to": t.to_node_id, "sku": t.sku, "qty": t.qty,
                       "expected_arrival": t.expected_arrival, "status": t.status} for t in transfers],
    }


def _in_new_session(request: Request, fn, *args) -> None:
    with request.app.state.session_factory() as s:
        try:
            fn(s, *args)
        except Exception:  # the runner has already marked the run FAILED with the error
            log.exception("background run task failed")
