"""The purchasing agent: an explicit state machine. Code owns every transition and every guardrail;
the model works only inside INVESTIGATE (gather evidence, choose an option), EXECUTE (call action
tools for the chosen option) and REPORT (explain the structured decision).

    INTAKE -> INVESTIGATE -> DECIDE -> POLICY_GATE -> EXECUTE -> VALIDATE -> AWAIT_SUPPLIER
           -> VERIFY_OUTCOME -> REPORT -> DONE
    VERIFY_OUTCOME failure or a rejected approval -> REPLAN -> INVESTIGATE (max replans, then escalate)
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import prompts
from app.agent.narrative import template_narrative, ungrounded_numbers
from app.agent.control import CONTROL_TOOLS, FinishExecutionArgs, ProposeDecisionArgs
from app.agent.persistence import Recorder, load_context
from app.agent.validation import diff_against_intent
from app.agent.states import ACTION_EVIDENCE, ACTION_KINDS, ALLOWED_TOOLS, MAX_TURNS, REQUIRED_EVIDENCE, RunStatus, State
from app.clock import clock_for
from app.config import get_settings
from app.engine.quality import data_blocks_decision
from app.logs import RUN_ID
from app.llm.base import LLMClient, LLMError, Message, ToolCall
from app.models import AgentRun, Approval, AuditLog, POLine, PurchaseOrder, StockTransfer
from app.policy import Policy, get_policy
from app.tools import REGISTRY, ToolContext, call_tool
from app.engine.projection import project_inventory
from app.supplier_mock import service as supplier
from app.tools.act import decided_option, gate_price_change, preview_gate, resolve_approval
from app.tools.compute import assess_data, build_decision, plan_context
from app.tools.registry import ToolError, schema_for, tool_schema


log = logging.getLogger("app.agent")

# Errors that mean "the call itself was malformed", as opposed to business outcomes such as BLOCKED.
MALFORMED = {"INVALID_ARGUMENTS", "UNKNOWN_TOOL", "TOOL_NOT_ALLOWED_IN_STATE", "NO_TOOL_CALL"}
# A turn is malformed when it made malformed calls and no successful one. The errors go back to the model
# once; a second malformed turn in a row fails the step (D23).
MAX_CONSECUTIVE_MALFORMED_TURNS = 2


@dataclass
class _Run:
    run: AgentRun
    ctx: ToolContext
    rec: Recorder
    messages: list[Message]
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> State:
        return State(self.run.state)


class PurchasingAgent:
    def __init__(self, session: Session, llm: LLMClient, policy: Policy | None = None,
                 max_run_seconds: float | None = None, now=time.monotonic) -> None:
        self.session, self.llm, self.policy = session, llm, policy or get_policy()
        self.max_run_seconds = max_run_seconds if max_run_seconds is not None else get_settings().run_max_seconds
        self._now = now

    # ------------------------------------------------------------------ public API

    def start(self, scenario_id: str, trigger: dict[str, Any]) -> AgentRun:
        clock = clock_for(self.session)
        run = AgentRun(scenario_id=scenario_id, provider=self.llm.name, trigger=trigger,
                       status=RunStatus.RUNNING, state=State.INTAKE, started_at=clock.now())
        self.session.add(run)
        self.session.commit()
        return run

    def run(self, run: AgentRun) -> AgentRun:
        """Advance until the run finishes or pauses for a human."""
        r = self._load(run)
        handlers = {State.INTAKE: self._intake, State.INVESTIGATE: self._investigate, State.DECIDE: self._decide,
                    State.POLICY_GATE: self._policy_gate, State.EXECUTE: self._execute,
                    State.VALIDATE: self._validate, State.AWAIT_SUPPLIER: self._await_supplier,
                    State.VERIFY_OUTCOME: self._verify_outcome, State.REPLAN: self._replan_state,
                    State.REPORT: self._report}
        start, used_before = self._now(), r.extra.get("active_s", 0.0)
        while run.status == RunStatus.RUNNING and r.state != State.DONE:
            # Active time only: a pause for human approval does not count against the run's budget.
            r.extra["active_s"] = round(used_before + self._now() - start, 1)
            if r.extra["active_s"] > self.max_run_seconds:
                self._fail(r, "RUN_TIMEOUT", f"the run used {r.extra['active_s']:.0f} s of its "
                                             f"{self.max_run_seconds:.0f} s budget (RUN_MAX_SECONDS) in {r.state}")
                break
            handlers[r.state](r)
            turn = r.extra.pop("turn", None)
            if turn is not None:  # a model turn just ran
                if turn["ok"]:
                    r.extra["invalid_turns"] = 0
                elif turn["bad"]:
                    r.extra["invalid_turns"] = r.extra.get("invalid_turns", 0) + 1
            if r.extra.get("invalid_turns", 0) >= MAX_CONSECUTIVE_MALFORMED_TURNS and r.state in MAX_TURNS:
                r.extra["invalid_turns"] = 0
                self._escalate(r, "MALFORMED_TOOL_CALLS", f"the model sent {MAX_CONSECUTIVE_MALFORMED_TURNS} "
                                                          f"malformed turns in a row in {r.state}")
                r.rec.transition(State.REPORT, "malformed tool calls")
            r.rec.save(r.ctx.state, r.messages, r.extra)
        return run

    def resolve_and_resume(self, run: AgentRun, approval_id: int, approve: bool, decided_by: str,
                           comment: str = "") -> AgentRun:
        """Apply a human's answer to the pending approval, then continue the run."""
        if run.status != RunStatus.AWAITING_APPROVAL:
            raise ToolError("INVALID_STATE", f"run {run.id} is {run.status}, not awaiting approval")
        r = self._load(run)
        outcome = resolve_approval(r.ctx, approval_id, approve, decided_by, comment)
        r.rec.step("approval", f"APPROVAL_{outcome.status}", {"by": decided_by, "comment": comment},
                   outcome.model_dump(mode="json"))
        run.status = RunStatus.RUNNING
        if r.state == State.VERIFY_OUTCOME:
            pass  # a price-change decision: VERIFY_OUTCOME re-reads the PO and carries on (or replans)
        elif approve:
            r.messages.append(Message(role="user", content=(
                f"Approval {approval_id} granted by {decided_by}. Result: {json.dumps(outcome.result)}. "
                "Complete any remaining action for the decided option, then call finish_execution.")))
        else:
            self._replan(r, f"Approval {approval_id} rejected by {decided_by}"
                            + (f": {comment}" if comment else "") + ". That option, and any override it needed, "
                            "is excluded. Call generate_options again and propose a new decision.")
        r.rec.save(r.ctx.state, r.messages, r.extra)
        return self.run(run)

    # ------------------------------------------------------------------ states

    def _intake(self, r: _Run) -> None:
        trigger = r.run.trigger
        r.ctx.state.trigger = trigger
        r.messages[:] = [Message(role="system", content=prompts.SYSTEM),
                         Message(role="user", content=prompts.trigger_message(trigger))]
        r.extra.update(evidence=[], turns={}, invalid_turns=0, incomplete_finishes=0)
        r.rec.step("intake", trigger["type"], trigger)
        r.rec.transition(State.INVESTIGATE)

    def _investigate(self, r: _Run) -> None:
        resp = self._model_turn(r)
        if resp is None:
            return
        if not resp.tool_calls:
            self._nudge(r, "Use the tools to investigate, then call propose_decision.")
            return
        for call in resp.tool_calls:
            if r.state != State.INVESTIGATE:
                self._tool_result(r, call, _skipped())
            elif call.name == "propose_decision":
                start = time.perf_counter()
                payload = self._propose(r, call)
                self._tool_result(r, call, payload, latency_ms=int((time.perf_counter() - start) * 1000))
            else:
                self._dispatch(r, call)

    def _decide(self, r: _Run) -> None:
        d = r.ctx.state.decision or {}
        r.rec.step("decision", d.get("outcome", "?"), output=d)
        option = decided_option(r.ctx)
        if d.get("outcome") == "INVESTIGATE":
            # The model chose the ESCALATE option itself, or decided the data cannot support a decision.
            explicit = d.get("option_kind") == "ESCALATE"
            reason = "AGENT_ESCALATED" if explicit else "INSUFFICIENT_DATA"
            why = (d.get("reasons") if explicit else d.get("information_needed")) or []
            self._escalate(r, reason, "Decision needs a human: " + ("; ".join(why) or "no reason given"),
                           d.get("information_needed") or [])
            r.rec.transition(State.REPORT, reason)
        elif option is None or option["kind"] == "NO_ACTION":
            r.rec.transition(State.REPORT, "nothing to execute")
        else:
            r.rec.transition(State.POLICY_GATE)

    def _policy_gate(self, r: _Run) -> None:
        option = decided_option(r.ctx)
        assert option is not None
        start = time.perf_counter()
        preview = preview_gate(r.ctx, option)
        r.rec.step("policy", preview["verdict"], {"option_id": option["id"]}, preview,
                   latency_ms=int((time.perf_counter() - start) * 1000))
        if preview["verdict"] in ("BLOCK", "ESCALATE"):
            self._escalate(r, "POLICY_" + preview["verdict"], f"Gate refused {option['id']}: {preview['reasons']}")
            r.rec.transition(State.REPORT, f"gate {preview['verdict']}")
            return
        r.messages.append(Message(role="user", content=prompts.execute_message(option, preview, r.ctx.state.trigger)))
        r.rec.transition(State.EXECUTE)

    def _execute(self, r: _Run) -> None:
        resp = self._model_turn(r)
        if resp is None:
            return
        if not resp.tool_calls:
            self._nudge(r, "Execute the decided option with the action tools, then call finish_execution.")
            return
        for call in resp.tool_calls:
            if r.state != State.EXECUTE or r.run.status != RunStatus.RUNNING:
                self._tool_result(r, call, _skipped())
            elif call.name == "finish_execution":
                start = time.perf_counter()
                payload = self._finish(r, call)
                self._tool_result(r, call, payload, latency_ms=int((time.perf_counter() - start) * 1000))
            else:
                payload = self._dispatch(r, call)
                output = payload.get("output") or {}
                if payload["ok"] and output.get("status") == "PENDING_APPROVAL" and self._approval_open(output):
                    r.run.status = RunStatus.AWAITING_APPROVAL
                    r.rec.step("approval", "REQUESTED", call.args, output)
                elif not payload["ok"] and payload["error"]["code"] == "ESCALATION_REQUIRED":
                    r.ctx.state.escalated = True
                    r.rec.transition(State.REPORT, "gate escalated")

    def _validate(self, r: _Run) -> None:
        """Layer 2: the database, read back, must match the decided option field by field."""
        start = time.perf_counter()
        checks = diff_against_intent(self.session, r.ctx.clock, r.run.id, decided_option(r.ctx), r.ctx.state.trigger)
        failed = [c for c in checks if not c["ok"]]
        r.rec.step("validation", "post_action_diff", {"option_id": r.ctx.state.decision["option_id"]},
                   {"checks": checks}, ok=not failed, latency_ms=int((time.perf_counter() - start) * 1000))
        if failed:
            self._escalate(r, "POST_ACTION_MISMATCH", "the database does not match the decision",
                           [f"{c['check']}: intended {c['intended']}, found {c['actual']}" for c in failed])
            r.rec.transition(State.REPORT, "post-action mismatch")
            return
        r.rec.transition(State.AWAIT_SUPPLIER)

    def _await_supplier(self, r: _Run) -> None:
        """Layer 3: each PO submitted for this decision gets the supplier's answer, written as a PO event."""
        option = decided_option(r.ctx)
        pos = [po for po in self.session.scalars(select(PurchaseOrder).filter_by(status="SUBMITTED"))
               if po.run_id == r.run.id or po.id == option.get("po_id")]
        r.extra["attempt_pos"] = []
        for po in pos:
            start = time.perf_counter()
            ev = supplier.respond(self.session, r.ctx.clock, po)
            old, new = supplier.update_reliability(self.session, r.ctx.clock, po.supplier_id, ev.fill_rate,
                                                   self.policy.reliability_ewma_alpha)
            r.extra["attempt_pos"].append(po.id)
            r.rec.step("supplier", ev.type, {"po_id": po.id, "supplier_id": po.supplier_id},
                       {**ev.model_dump(), "reliability": {"before": old, "after": new}},
                       ok=ev.type in ("CONFIRMED", "DELAYED"), latency_ms=int((time.perf_counter() - start) * 1000))
        r.rec.transition(State.VERIFY_OUTCOME)

    def _verify_outcome(self, r: _Run) -> None:
        """Layer 4: read the POs back, settle price changes through the gate, then re-project inventory with
        what the supplier actually confirmed and compare it with what the chosen option predicted (D12)."""
        start = time.perf_counter()
        option = decided_option(r.ctx)
        failures: list[dict[str, Any]] = []
        for po_id in r.extra.get("attempt_pos", []):
            po = self.session.get(PurchaseOrder, po_id)
            last = po.events[-1]
            if po.status == "REJECTED":
                failures.append({"code": "SUPPLIER_REJECTED", "supplier": po.supplier_id, "po_id": po.id,
                                 "message": (last.payload or {}).get("message", "")})
                self._exclude(r, po.supplier_id)
            elif po.status == "CANCELLED":
                failures.append({"code": "PRICE_CHANGE_REFUSED", "supplier": po.supplier_id, "po_id": po.id})
                self._exclude(r, po.supplier_id)
            elif po.status == "SUBMITTED" and last.type == "PRICE_CHANGE":
                try:
                    gate = gate_price_change(r.ctx, po, last.payload["proposed_unit_cost"])
                except ToolError as e:  # the gate refused outright (e.g. repeated blocks): a human decides
                    r.rec.step("policy", "ESCALATE", {"po_id": po.id, "price_change": last.payload},
                               {"code": e.code, "details": e.details}, ok=False)
                    self._escalate(r, e.code, f"price change on {po.id} refused by the gate: {e.message}")
                    r.rec.transition(State.REPORT, "price change refused by the gate")
                    return
                r.rec.step("policy", gate.verdict, {"po_id": po.id, "price_change": last.payload}, gate.model_dump())
                if gate.verdict == "APPROVAL":
                    r.run.status = RunStatus.AWAITING_APPROVAL  # resumes here once a human answers
                    return

        trig = r.ctx.state.trigger
        pc = plan_context(r.ctx, trig["node"], trig["sku"], None, r.ctx.state.demand_basis)  # type: ignore[arg-type]
        actual = project_inventory(pc.available, pc.forecast, pc.existing_receipts, pc.horizon)
        predicted = {"stockout_day": option["stockout_day"], "unmet_units": option["unmet_units"],
                     "end_levels": option["projection"]}
        worse = (actual.stockout_day is not None
                 and (option["stockout_day"] is None or actual.stockout_day < option["stockout_day"])) \
            or actual.unmet_units > option["unmet_units"] + 0.5
        if worse and not failures:
            failures.append({"code": "OUTCOME_WORSE_THAN_PREDICTED", "predicted": predicted,
                             "actual": {"stockout_day": actual.stockout_day, "unmet_units": actual.unmet_units}})
            for po_id in r.extra.get("attempt_pos", []):  # a short or late supplier is not asked again
                po = self.session.get(PurchaseOrder, po_id)
                if po.events[-1].type in ("PARTIAL", "DELAYED"):
                    self._exclude(r, po.supplier_id)
        r.rec.step("verification", "outcome_check", {"option_id": option["id"], "predicted": predicted},
                   {"actual_end_levels": actual.end_levels, "stockout_day": actual.stockout_day,
                    "unmet_units": actual.unmet_units, "failures": failures},
                   ok=not failures, latency_ms=int((time.perf_counter() - start) * 1000))
        if failures:
            self._replan(r, "; ".join(f"{f['code']}" + (f" ({f['supplier']})" if f.get("supplier") else "")
                                      for f in failures))
        else:
            r.rec.transition(State.REPORT, "outcome as predicted")

    def _report(self, r: _Run) -> None:
        d = r.ctx.state.decision or r.run.decision  # after a spent replan budget, explain the last decision
        if d is None:  # escalated before any decision (e.g. turn limit)
            r.run.narrative = "Escalated before a decision was reached; see the trace."
        else:
            facts = self._execution_facts(r)
            narrative = self._grounded_narrative(r, d, facts)
            if narrative is None:
                return
            r.run.narrative = narrative
        r.run.status = RunStatus.ESCALATED if r.ctx.state.escalated else RunStatus.COMPLETED
        r.run.finished_at = r.ctx.clock.now()
        r.rec.transition(State.DONE, r.run.status)

    def _grounded_narrative(self, r: _Run, decision: dict[str, Any], facts: dict[str, Any]) -> str | None:
        """Ask the model to explain the decision; accept it only if every number is grounded (one retry)."""
        r.messages.append(Message(role="user", content=prompts.report_message(decision, facts)))
        for attempt in (1, 2):
            resp = self._model_turn(r, tools=False)
            if resp is None:
                return None
            text = resp.text.strip()
            bad = ungrounded_numbers(text, decision, facts)
            r.rec.step("narrative_check", "grounded" if text and not bad else "ungrounded",
                       {"attempt": attempt}, {"ungrounded_numbers": bad, "empty": not text}, ok=bool(text) and not bad)
            if text and not bad:
                r.extra["narrative_source"] = "model" if attempt == 1 else "model_retry"
                return text
            r.messages.append(Message(role="user", content=(
                f"These numbers are not in the decision: {bad}. Rewrite using only numbers from the JSON above."
                if text else "The explanation was empty. Write it now.")))
        r.extra["narrative_source"] = "template"
        return template_narrative(decision, facts)

    # ------------------------------------------------------------------ decision & execution checks

    def _propose(self, r: _Run, call: ToolCall) -> dict[str, Any]:
        try:
            args = ProposeDecisionArgs.model_validate(call.args)
        except ValidationError as e:
            return _error("INVALID_ARGUMENTS", "arguments do not match the schema", {"errors": e.errors()})
        trigger = r.ctx.state.trigger
        missing = sorted(self._missing_evidence(r, args))
        if missing:
            return _error("MISSING_EVIDENCE", "gather this evidence before deciding", {"missing": missing})
        if not args.investigate:
            known = [o["id"] for o in (r.ctx.state.options or {}).get("options", [])]
            if not args.option_id:
                return _error("INVALID_ARGUMENTS", "option_id is required unless investigate=true: pass the id "
                              "of the option you chose", {"valid_option_ids": known})
            issues, sens = assess_data(r.ctx, trigger["node"], trigger["sku"])
            if data_blocks_decision(issues, sens):
                return _error("DATA_BLOCKS_DECISION", "the data cannot support acting; investigate instead",
                              {"issues": [i.model_dump() for i in issues],
                               "sensitivity": sens.model_dump(mode="json") if sens else None})
            option = next((o for o in r.ctx.state.options["options"] if o["id"] == args.option_id), None)
            if option is not None and option["blocked"]:
                return _error("OPTION_BLOCKED", "this option violates a hard constraint",
                              {"violations": option["violations"]})
        try:
            decision = build_decision(r.ctx, None if args.investigate else args.option_id, args.investigate,
                                      args.information_needed)
        except ToolError as e:
            return _error(e.code, e.message, e.details)
        d = decision.model_dump(mode="json")
        d["reasons"] = args.reasons
        r.ctx.state.decision = d
        r.run.decision = d
        r.extra["next_state"] = State.DECIDE
        return {"ok": True, "output": {"outcome": d["outcome"], "quantity": d["quantity"], "option_id": d["option_id"]}}

    def _missing_evidence(self, r: _Run, args: ProposeDecisionArgs) -> set[str]:
        """Reads the decision still needs. Always the trigger's checklist; for a purchase or transfer also
        supplier terms, budget and storage."""
        have = set(r.extra["evidence"])
        missing = REQUIRED_EVIDENCE[r.ctx.state.trigger["type"]] - have
        if args.investigate:
            return missing
        option = next((o for o in (r.ctx.state.options or {}).get("options", []) if o["id"] == args.option_id), None)
        if option is not None and option["kind"] in ACTION_KINDS:
            missing |= ACTION_EVIDENCE - have
        return missing

    def _finish(self, r: _Run, call: ToolCall) -> dict[str, Any]:
        try:
            FinishExecutionArgs.model_validate(call.args)
        except ValidationError as e:
            return _error("INVALID_ARGUMENTS", "arguments do not match the schema", {"errors": e.errors()})
        gaps = self._execution_gaps(r)
        if gaps:
            r.extra["incomplete_finishes"] += 1
            if r.extra["incomplete_finishes"] >= 2:
                self._escalate(r, "EXECUTION_INCOMPLETE", "decided option not fully executed", gaps)
                r.extra["next_state"] = State.REPORT
            return _error("EXECUTION_INCOMPLETE", "the decided option is not fully executed", {"missing": gaps})
        r.extra["next_state"] = State.VALIDATE
        return {"ok": True, "output": {"executed": True}}

    def _approval_open(self, output: dict[str, Any]) -> bool:
        """Pause only for an approval that is still pending. A retried call replays its first response
        (idempotency), which still says PENDING_APPROVAL after the human has already answered."""
        approval = self.session.get(Approval, output.get("approval_id"))
        return approval is not None and approval.status == "PENDING"

    def _execution_gaps(self, r: _Run) -> list[str]:
        """What the decided option still needs, read back from the database (not from the model's claims)."""
        option = decided_option(r.ctx)
        if option is None:
            return []
        done = {(a.action, a.entity_id) for a in self.session.scalars(
            select(AuditLog).filter_by(run_id=r.run.id))}
        actions = {a for a, _ in done}
        gaps = []
        if option["kind"] == "PURCHASE" and option["po_id"] is None:
            submitted = [po for po in self.session.scalars(select(PurchaseOrder).filter_by(
                run_id=r.run.id, supplier_id=option["supplier_id"])) if ("SUBMIT_PO", po.id) in done]
            if not submitted:
                gaps.append(f"submit a PO to {option['supplier_id']} for {option['qty']} units")
        elif option["kind"] == "PURCHASE":
            if ("INCREASE_PO_LINE", option["po_id"]) not in done:
                gaps.append(f"increase {option['po_id']} by {option['qty']} units")
        elif option["kind"] == "TRANSFER":
            if not self.session.scalars(select(StockTransfer).filter_by(run_id=r.run.id)).first():
                gaps.append(f"create the transfer of {option['qty']} from {option['from_node']}")
        trigger = r.ctx.state.trigger
        if trigger.get("type") == "supplier_response" and option["kind"] != "BACKORDER":
            line = self.session.scalars(select(POLine).filter_by(po_id=trigger["po_id"], sku=trigger["sku"])).first()
            if line is not None and line.qty_confirmed is not None and line.qty_ordered != line.qty_confirmed \
                    and "ACKNOWLEDGE_PARTIAL" not in actions:
                gaps.append(f"acknowledge the partial on {trigger['po_id']} ({line.qty_confirmed} confirmed)")
        return gaps

    def _execution_facts(self, r: _Run) -> dict[str, Any]:
        pos = self.session.scalars(select(PurchaseOrder).filter_by(run_id=r.run.id))
        transfers = self.session.scalars(select(StockTransfer).filter_by(run_id=r.run.id))
        return {"purchase_orders": [{"po_id": p.id, "supplier": p.supplier_id, "status": p.status,
                                     "qty": sum(l.qty_ordered for l in p.lines)} for p in pos],
                "transfers": [{"id": t.id, "from": t.from_node_id, "qty": t.qty} for t in transfers],
                "escalated": r.ctx.state.escalated}

    def _replan(self, r: _Run, reason: str) -> None:
        r.extra["replan_reason"] = reason
        r.rec.transition(State.REPLAN, reason)

    def _replan_state(self, r: _Run) -> None:
        """Count the replan; within budget go back to INVESTIGATE with the failure, else escalate."""
        reason = r.extra.pop("replan_reason", "")
        r.ctx.state.replans += 1
        r.run.replan_count = r.ctx.state.replans
        r.ctx.state.decision = None
        r.ctx.state.options = None  # options must be regenerated with the new exclusions
        r.extra["evidence"] = [e for e in r.extra.get("evidence", []) if e != "generate_options"]
        r.extra["turns"] = {}
        if r.ctx.state.replans > self.policy.max_replans:
            self._escalate(r, "MAX_REPLANS_REACHED", f"{self.policy.max_replans} replans spent; last failure: {reason}")
            r.rec.transition(State.REPORT, "max replans")
            return
        excluded = ", ".join(r.ctx.state.excluded_suppliers) or "none"
        r.messages.append(Message(role="user", content=(
            f"Replan {r.ctx.state.replans} of {self.policy.max_replans}: {reason}. Excluded suppliers: {excluded}. "
            "Call generate_options again and propose a new decision.")))
        r.rec.transition(State.INVESTIGATE, f"replan {r.ctx.state.replans}")

    def _exclude(self, r: _Run, supplier_id: str) -> None:
        r.ctx.state.excluded_suppliers = sorted({*r.ctx.state.excluded_suppliers, supplier_id})

    # ------------------------------------------------------------------ plumbing

    def _load(self, run: AgentRun) -> _Run:
        RUN_ID.set(run.id)  # every log line below, including inside the LLM clients, carries this run id
        state, messages, extra = load_context(run)
        clock = clock_for(self.session)
        ctx = ToolContext(session=self.session, clock=clock, policy=self.policy, run_id=run.id, state=state)
        return _Run(run=run, ctx=ctx, rec=Recorder(self.session, run, clock.now()), messages=messages, extra=extra)

    def _model_turn(self, r: _Run, tools: bool = True):
        state = r.state
        turns = r.extra.setdefault("turns", {})
        turns[state] = turns.get(state, 0) + 1
        if state in MAX_TURNS and turns[state] > MAX_TURNS[state]:
            self._escalate(r, "TURN_LIMIT", f"no progress after {MAX_TURNS[state]} turns in {state}")
            r.rec.transition(State.REPORT, "turn limit")
            return None
        schemas = self._schemas(r) if tools else []
        start = time.perf_counter()
        model = getattr(self.llm, "model", self.llm.name)
        log.info("llm call: provider=%s model=%s state=%s messages=%d", self.llm.name, model, state, len(r.messages))
        self.llm.on_wait = lambda w: self._record_wait(r, w)
        try:
            resp = self.llm.complete(r.messages, schemas)
        except LLMError as e:
            log.warning("llm call failed: provider=%s model=%s state=%s after %d ms: %s", self.llm.name, model, state,
                        int((time.perf_counter() - start) * 1000), e)
            r.rec.step("llm", self.llm.name, {"state": state}, {"code": e.code, "error": str(e)}, ok=False,
                       latency_ms=int((time.perf_counter() - start) * 1000))
            self._fail(r, e.code, str(e))
            return None
        finally:
            self.llm.on_wait = None
        log.info("llm done: provider=%s model=%s state=%s latency_ms=%d tokens=%s/%s tool_calls=%d", self.llm.name,
                 model, state, int((time.perf_counter() - start) * 1000), resp.tokens_in, resp.tokens_out,
                 len(resp.tool_calls))
        r.rec.step("llm", self.llm.name, {"state": state, "messages": len(r.messages), "tools": len(schemas)},
                   {"text": resp.text, "tool_calls": [c.model_dump() for c in resp.tool_calls], "model": resp.model},
                   latency_ms=int((time.perf_counter() - start) * 1000), tokens_in=resp.tokens_in,
                   tokens_out=resp.tokens_out)
        r.messages.append(resp.as_message())
        return resp

    _WAIT_LABEL = {"rate_limit": "rate limited", "network": "network error", "server": "provider unavailable",
                   "pacing": "pacing requests"}

    def _record_wait(self, r: _Run, wait: dict[str, Any]) -> None:
        """Put a retry or pacing wait on the trace *before* sleeping, so a blocked run is visibly waiting."""
        label = self._WAIT_LABEL.get(wait["reason"], wait["reason"])
        verb = "next request" if wait["reason"] == "pacing" else "retrying"
        r.rec.step("wait", f"waiting: {label}, {verb} in {wait['delay_s']:g}s", {"state": r.state}, wait,
                   latency_ms=int(wait["delay_s"] * 1000))

    def _schemas(self, r: _Run) -> list[dict[str, Any]]:
        out = []
        for name in sorted(ALLOWED_TOOLS.get(r.state, set())):
            if name in CONTROL_TOOLS:
                model, description = CONTROL_TOOLS[name]
                params = schema_for(model)
                if name == "propose_decision" and r.ctx.state.options:
                    # Once options exist, the model can only name one of them: an enum, not a free string.
                    params["properties"]["option_id"] = {
                        "type": "string", "enum": [o["id"] for o in r.ctx.state.options["options"]],
                        "description": "id from generate_options; omit only when investigate=true"}
                out.append({"name": name, "description": description, "parameters": params})
            else:
                out.append(tool_schema(REGISTRY[name]))
        return out

    def _dispatch(self, r: _Run, call: ToolCall) -> dict[str, Any]:
        latency = 0
        if call.name not in ALLOWED_TOOLS.get(r.state, set()) or call.name in CONTROL_TOOLS:
            payload = _error("TOOL_NOT_ALLOWED_IN_STATE", f"{call.name} cannot be used in {r.state}",
                             {"allowed": sorted(ALLOWED_TOOLS.get(r.state, set()))})
        else:
            args = dict(call.args)
            if REGISTRY[call.name].kind == "act" and isinstance(args.get("idempotency_key"), str):
                args["idempotency_key"] = f"run{r.run.id}:{args['idempotency_key']}"  # keys are scoped to the run
            res = call_tool(call.name, args, r.ctx)
            latency = res.latency_ms
            payload = {"ok": res.ok, "output": res.output, "error": res.error}
            if res.ok and call.name not in r.extra["evidence"]:
                r.extra["evidence"].append(call.name)
        self._tool_result(r, call, payload, latency_ms=latency)
        return payload

    def _tool_result(self, r: _Run, call: ToolCall, payload: dict[str, Any], latency_ms: int = 0) -> None:
        r.rec.step("tool", call.name, call.args, payload, ok=payload["ok"], latency_ms=latency_ms)
        turn = r.extra.setdefault("turn", {"ok": False, "bad": False})
        if payload["ok"]:
            turn["ok"] = True
        elif payload["error"]["code"] in MALFORMED:
            turn["bad"] = True
        r.messages.append(Message(role="tool", name=call.name, tool_call_id=call.id,
                                  content=json.dumps(payload, separators=(",", ":"), default=str)))
        next_state = r.extra.pop("next_state", None)
        if next_state:  # control tools move the machine only after their own result is on the record
            r.rec.transition(next_state)

    def _nudge(self, r: _Run, text: str) -> None:
        """The model answered with text where a tool call was required: tell it once (counts as malformed)."""
        r.messages.append(Message(role="user", content=text))
        r.rec.step("nudge", r.state, {"text": text, "code": "NO_TOOL_CALL"}, ok=False)
        r.extra.setdefault("turn", {"ok": False, "bad": False})["bad"] = True

    def _escalate(self, r: _Run, reason: str, summary: str, info: list[str] | None = None) -> None:
        key = f"run{r.run.id}:escalate:{len(r.run.steps)}"
        res = call_tool("escalate", {"reason_code": reason, "summary": summary,
                                     "information_needed": info or [], "idempotency_key": key}, r.ctx)
        r.rec.step("escalation", reason, {"summary": summary, "information_needed": info or []}, res.output, ok=res.ok)

    def _fail(self, r: _Run, code: str, message: str) -> None:
        r.run.status = RunStatus.FAILED
        r.run.finished_at = r.ctx.clock.now()
        r.rec.step("error", code, output={"message": message}, ok=False)
        r.rec.transition(State.DONE, f"failed: {code}")


def _error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": False, "output": None, "error": {"code": code, "message": message, "details": details or {}}}


def _skipped() -> dict[str, Any]:
    return _error("SKIPPED", "not executed: the run moved on before this call")
