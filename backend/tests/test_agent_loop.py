from sqlalchemy import select

from app.models import Approval, PurchaseOrder
from fixture_inputs import script_turns


def _transitions(run):
    return [st.name for st in run.steps if st.kind == "transition"]


def _tool_errors(run):
    return [(st.name, st.output["error"]["code"]) for st in run.steps if st.kind == "tool" and not st.ok]


def test_s1_overstock_end_to_end(run_case) -> None:
    _, run, s, fx = run_case("s1_overstock")
    assert run.status == "COMPLETED"
    assert _transitions(run) == ["INTAKE->INVESTIGATE", "INVESTIGATE->DECIDE", "DECIDE->POLICY_GATE",
                                 "POLICY_GATE->EXECUTE", "EXECUTE->VALIDATE", "VALIDATE->AWAIT_SUPPLIER",
                                 "AWAIT_SUPPLIER->VERIFY_OUTCOME", "VERIFY_OUTCOME->REPORT", "REPORT->DONE"]
    diff = next(st for st in run.steps if st.kind == "validation")
    assert diff.ok and all(c["ok"] for c in diff.output["checks"])
    assert (run.decision["outcome"], run.decision["quantity"]) == (fx.expected.outcome, fx.expected.qty_min)
    po = s.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).one()
    assert (po.supplier_id, po.status, po.lines[0].qty_ordered) == ("SUP-ALQ", "CONFIRMED", 240)
    assert po.idempotency_key == f"run{run.id}:create"  # keys are scoped to the run
    assert "240" in run.narrative and _tool_errors(run) == []
    policy = next(st for st in run.steps if st.kind == "policy")
    assert policy.name == "AUTO"


def test_s1_already_covered_rejects_without_acting(run_case) -> None:
    _, run, s, _ = run_case("s1_already_covered")
    assert (run.status, run.decision["outcome"], run.decision["quantity"]) == ("COMPLETED", "REJECT", 0)
    assert "POLICY_GATE" not in " ".join(_transitions(run))
    assert s.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).first() is None


def test_s1_stale_inventory_investigates_and_escalates(run_case) -> None:
    _, run, _, _ = run_case("s1_stale_inventory")
    assert (run.status, run.decision["outcome"]) == ("ESCALATED", "INVESTIGATE")
    assert run.decision["information_needed"][0] == "fresh cycle count of LECHE-ALQ-1L at BOG-02"
    esc = next(st for st in run.steps if st.kind == "escalation")
    assert esc.name == "INSUFFICIENT_DATA"


def test_s4_budget_pauses_for_approval_then_resumes(run_case) -> None:
    agent, run, s, fx = run_case("s4_budget_binding")
    assert (run.status, run.state) == ("AWAITING_APPROVAL", "EXECUTE")
    approval = s.scalars(select(Approval).filter_by(run_id=run.id)).one()
    assert set(approval.reasons) == set(fx.expected.approval_reasons)
    assert [a["qty"] for a in approval.alternatives] == [714, 342]

    agent.resolve_and_resume(run, approval.id, approve=True, decided_by="category.manager")
    assert run.status == "COMPLETED"
    po = s.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).one()
    assert (po.status, po.lines[0].qty_ordered) == ("CONFIRMED", 714)


def test_rejected_override_replans_to_the_budget_fallback(run_case) -> None:
    agent, run, s, fx = run_case("s4_budget_override_rejected")
    approval = s.scalars(select(Approval).filter_by(run_id=run.id)).one()
    agent.resolve_and_resume(run, approval.id, approve=False, decided_by="cm", comment="no budget left this month")
    assert run.status == "COMPLETED" and run.replan_count == 1
    assert (run.decision["outcome"], run.decision["quantity"]) == ("MODIFY", 342)
    assert run.decision["residual_risk"] == fx.expected.residual_risk
    statuses = {p.lines[0].qty_ordered: p.status for p in s.scalars(select(PurchaseOrder).filter_by(run_id=run.id))}
    assert statuses == {714: "CANCELLED", 342: "CONFIRMED"}
    assert {"EXECUTE->REPLAN", "REPLAN->INVESTIGATE"} <= set(_transitions(run))  # replan after the rejection


def test_s2_acknowledges_partial_and_sources_alternate_with_approval(run_case) -> None:
    agent, run, s, _ = run_case("s2_partial_needs_alternate")
    assert run.status == "AWAITING_APPROVAL"
    approval = s.scalars(select(Approval).filter_by(run_id=run.id)).one()
    agent.resolve_and_resume(run, approval.id, approve=True, decided_by="buyer")
    assert run.status == "COMPLETED"
    assert s.get(PurchaseOrder, "PO-2002").status == "CONFIRMED"
    ceda = s.scalars(select(PurchaseOrder).filter_by(run_id=run.id, supplier_id="SUP-CEDA")).one()
    assert (ceda.status, ceda.lines[0].qty_ordered) == ("CONFIRMED", 204)


READS = {"tool_calls": [{"name": "get_inventory", "args": {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}}]}


def test_decision_without_evidence_is_refused(run_case) -> None:
    turns = [READS,
             {"tool_calls": [{"name": "propose_decision", "args": {"option_id": "BUY:SUP-ALQ:240"}}]}]
    _, run, _, _ = run_case("s1_overstock", turns)
    err = next(st for st in run.steps if st.name == "propose_decision")
    assert err.output["error"]["code"] == "MISSING_EVIDENCE"
    assert set(err.output["error"]["details"]["missing"]) == {"get_forecast", "get_open_pos",
                                                              "calculate_net_requirement", "generate_options"}
    assert run.status == "FAILED"  # script ran out: no decision was ever accepted


S1 = {"node": "BOG-01", "sku": "LECHE-ALQ-1L"}
BASE_READS = {"tool_calls": [{"name": n, "args": S1} for n in ("get_inventory", "get_forecast", "get_open_pos")]}
COMPUTE = {"tool_calls": [{"name": n, "args": S1} for n in ("calculate_net_requirement", "generate_options")]}
ACTION_READS = {"tool_calls": [{"name": "get_supplier_terms", "args": {"supplier_id": "SUP-ALQ", "sku": "LECHE-ALQ-1L"}},
                               {"name": "get_budget", "args": S1}, {"name": "get_storage_capacity", "args": S1}]}
BUY_240 = {"tool_calls": [{"name": "propose_decision", "args": {"option_id": "BUY:SUP-ALQ:240"}}]}


def test_a_purchase_needs_supplier_terms_budget_and_storage_read_first(run_case) -> None:
    _, run, _, _ = run_case("s1_overstock", [BASE_READS, COMPUTE, BUY_240, ACTION_READS, BUY_240])
    first, second = [st for st in run.steps if st.name == "propose_decision"]
    assert first.output["error"]["code"] == "MISSING_EVIDENCE"
    assert first.output["error"]["details"]["missing"] == ["get_budget", "get_storage_capacity", "get_supplier_terms"]
    assert second.ok and (run.decision["outcome"], run.decision["quantity"]) == ("MODIFY", 240)


def test_investigating_needs_only_the_base_evidence(run_case) -> None:
    investigate = {"tool_calls": [{"name": "propose_decision", "args": {
        "investigate": True, "information_needed": ["a fresh stock count"]}}]}
    _, run, _, _ = run_case("s1_overstock", [BASE_READS, COMPUTE, investigate])
    step = next(st for st in run.steps if st.name == "propose_decision")
    assert step.ok and run.decision["outcome"] == "INVESTIGATE"


def test_action_tools_are_not_available_while_investigating(run_case) -> None:
    turns = [{"tool_calls": [{"name": "create_po_draft", "args": {"node": "BOG-01", "sku": "LECHE-ALQ-1L",
              "supplier_id": "SUP-ALQ", "deliveries": [{"day": 3, "qty": 10000}], "idempotency_key": "x"}}]}]
    _, run, s, _ = run_case("x_prompt_injection", turns)
    assert _tool_errors(run) == [("create_po_draft", "TOOL_NOT_ALLOWED_IN_STATE")]
    assert s.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).first() is None


def test_stale_data_blocks_acting_and_forces_investigation(run_case) -> None:
    script = script_turns("s1_stale_inventory")
    turns = script[:2] + [{"tool_calls": [{"name": "propose_decision", "args": {"option_id": "NO_ACTION"}}]}]
    _, run, _, _ = run_case("s1_stale_inventory", turns)
    assert _tool_errors(run) == [("propose_decision", "DATA_BLOCKS_DECISION")]


def test_one_malformed_call_gets_the_schema_error_and_the_run_recovers(run_case) -> None:
    bad = {"tool_calls": [{"name": "get_inventory", "args": {"node": "BOG-01"}}]}  # sku missing
    _, run, _, _ = run_case("s1_overstock", [bad] + script_turns("s1_overstock"))
    err = next(st for st in run.steps if st.kind == "tool" and not st.ok)
    assert err.output["error"]["code"] == "INVALID_ARGUMENTS"
    assert err.output["error"]["details"]["errors"] == [{"loc": "sku", "msg": "Field required"}]
    assert run.status == "COMPLETED" and run.decision["quantity"] == 240


def test_two_malformed_turns_in_a_row_fail_the_step_and_escalate(run_case) -> None:
    turns = [{"tool_calls": [{"name": "get_inventory", "args": {"node": "BOG-01", "qty": 10000}}]},
             {"tool_calls": [{"name": "buy_now", "args": {}}]}]
    _, run, _, _ = run_case("s1_overstock", turns)
    assert run.status == "ESCALATED"
    assert next(st for st in run.steps if st.kind == "escalation").name == "MALFORMED_TOOL_CALLS"
    assert _tool_errors(run) == [("get_inventory", "INVALID_ARGUMENTS"), ("buy_now", "TOOL_NOT_ALLOWED_IN_STATE")]


def test_text_instead_of_a_tool_call_counts_as_malformed(run_case) -> None:
    turns = [{"text": "I think we should buy 800."}, {"text": "Buy 800."}]
    _, run, _, _ = run_case("s1_overstock", turns)
    assert run.status == "ESCALATED"
    assert [st.kind for st in run.steps].count("nudge") == 2


def test_control_tool_results_are_recorded_under_the_state_that_ran_them(run_case) -> None:
    _, run, _, _ = run_case("s1_overstock")
    by_name = {st.name: st.state for st in run.steps if st.kind == "tool"}
    assert by_name["propose_decision"] == "INVESTIGATE"
    assert by_name["finish_execution"] == "EXECUTE"


def test_option_id_schema_becomes_an_enum_of_generated_options(run_case) -> None:
    from app.agent.loop import PurchasingAgent
    from app.llm.base import LLMClient
    from app.llm.scripted import ScriptedClient
    from app.seed import seed_workspace

    seen = []

    class Spy(LLMClient):
        name = "spy"

        def __init__(self, inner):
            self.inner = inner

        def complete(self, messages, tools):
            seen.append({t["name"]: t for t in tools})
            return self.inner.complete(messages, tools)

    _, _, s, fx = run_case("s1_overstock")  # reuse its database, then rerun the case through the spy
    seed_workspace(s, fx)
    s.commit()
    spy_agent = PurchasingAgent(s, Spy(ScriptedClient.for_case("s1_overstock")))
    spy_agent.run(spy_agent.start(fx.id, fx.trigger.model_dump()))
    before, after = seen[0]["propose_decision"], seen[2]["propose_decision"]  # turn 3 follows generate_options
    assert "enum" not in before["parameters"]["properties"]["option_id"]
    assert after["parameters"]["properties"]["option_id"]["enum"][:2] == ["BUY:SUP-ALQ:240", "BUY:SUP-ANDINA:144"]


def test_missing_option_id_error_lists_the_valid_ids(run_case) -> None:
    turns = script_turns("s1_overstock")[:2] + [
        {"tool_calls": [{"name": "propose_decision", "args": {"reasons": ["240 is right"]}}]}]
    _, run, _, _ = run_case("s1_overstock", turns)
    err = next(st for st in run.steps if st.name == "propose_decision").output["error"]
    assert err["code"] == "INVALID_ARGUMENTS" and err["details"]["valid_option_ids"][0] == "BUY:SUP-ALQ:240"


def _approve_all(agent, run, s, answer=True):
    """Answer every approval the run raises, like the eval runner does from the fixture."""
    while run.status == "AWAITING_APPROVAL":
        approval = s.scalars(select(Approval).filter_by(run_id=run.id, status="PENDING")).one()
        agent.resolve_and_resume(run, approval.id, approve=answer, decided_by="buyer")


def test_supplier_rejection_replans_to_an_alternate(run_case) -> None:
    agent, run, s, fx = run_case("x_supplier_rejects")
    _approve_all(agent, run, s)
    assert (run.status, run.replan_count) == ("COMPLETED", 1)
    assert (run.decision["outcome"], run.decision["quantity"]) == (fx.expected.outcome, 144)
    pos = {p.supplier_id: p.status for p in s.scalars(select(PurchaseOrder).filter_by(run_id=run.id))}
    assert pos == {"SUP-ALQ": "REJECTED", "SUP-ANDINA": "CONFIRMED"}
    verify = [st for st in run.steps if st.kind == "verification"]
    assert [v.ok for v in verify] == [False, True]
    assert verify[0].output["failures"][0]["code"] == "SUPPLIER_REJECTED"
    assert {"VERIFY_OUTCOME->REPLAN", "REPLAN->INVESTIGATE"} <= set(_transitions(run))


def test_price_change_above_policy_needs_approval_then_confirms(run_case) -> None:
    agent, run, s, _ = run_case("x_price_change")
    assert (run.status, run.state) == ("AWAITING_APPROVAL", "VERIFY_OUTCOME")
    approval = s.scalars(select(Approval).filter_by(run_id=run.id)).one()
    assert approval.reasons == ["PRICE_VARIANCE"] and approval.action["proposed_unit_cost"] == 6264.0
    agent.resolve_and_resume(run, approval.id, approve=True, decided_by="cm")
    po = s.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).one()
    assert (run.status, run.replan_count, po.status, po.lines[0].unit_cost) == ("COMPLETED", 0, "CONFIRMED", 6264.0)


def test_replans_are_capped_then_escalated(run_case) -> None:
    agent, run, s, fx = run_case("x_replans_exhausted")
    _approve_all(agent, run, s)
    assert run.status == "ESCALATED" and run.replan_count == 4
    assert next(st for st in run.steps if st.kind == "escalation").name == "MAX_REPLANS_REACHED"
    assert run.context["narrative_source"] == "model" and "replan budget is spent" in run.narrative
    pos = {p.supplier_id: p.status for p in s.scalars(select(PurchaseOrder).filter_by(run_id=run.id))}
    assert pos == {e.supplier: "REJECTED" for e in fx.expected.final_pos}


def test_injected_delay_that_causes_a_stockout_triggers_a_replan(run_case) -> None:
    from app.models import Workspace

    agent, run, s, _ = run_case("s1_overstock", turns=[])  # seed only; run with an injected supplier delay
    ws = s.get(Workspace, 1)
    ws.config = {**ws.config, "supplier_behaviour": {"SUP-ALQ": [{"type": "DELAYED", "days": 2}]}}
    s.commit()
    from app.agent.loop import PurchasingAgent
    from app.llm.scripted import ScriptedClient

    turns = script_turns("s1_overstock")[:-1] + [{"tool_calls": [{"name": "generate_options", "args": {
        "node": "BOG-01", "sku": "LECHE-ALQ-1L"}}]}]
    a = PurchasingAgent(s, ScriptedClient(turns))
    r = a.run(a.start("s1_overstock", run.trigger))
    verify = next(st for st in r.steps if st.kind == "verification")
    assert not verify.ok and verify.output["failures"][0]["code"] == "OUTCOME_WORSE_THAN_PREDICTED"
    assert verify.output["stockout_day"] == 4 and "SUP-ALQ" in r.context["state"]["excluded_suppliers"]


def test_supplier_outcomes_update_reliability(run_case) -> None:
    from app.models import Supplier

    agent, run, s, fx = run_case("x_supplier_rejects")
    _approve_all(agent, run, s)
    events = {st.input["supplier_id"]: st.output["reliability"] for st in run.steps if st.kind == "supplier"}
    assert events["SUP-ALQ"] == {"before": 0.95, "after": fx.expected.computed["alq_reliability_after"]}
    assert events["SUP-ANDINA"] == {"before": 0.86, "after": 0.888}  # 0.8 x 0.86 + 0.2 x 1.0
    assert s.get(Supplier, "SUP-ALQ").reliability_score == 0.76


def test_tool_steps_carry_measured_latency(run_case, monkeypatch) -> None:
    import app.agent.loop as loop

    real = loop.call_tool
    monkeypatch.setattr(loop, "call_tool", lambda *a, **k: real(*a, **k).model_copy(update={"latency_ms": 42}))
    _, run, _, _ = run_case("s1_overstock")
    registry_tools = [st for st in run.steps if st.kind == "tool" and st.name not in ("propose_decision",
                                                                                       "finish_execution")]
    assert registry_tools and all(st.latency_ms == 42 for st in registry_tools)
    assert all(st.latency_ms is not None and st.latency_ms >= 0 for st in run.steps)


def test_partial_that_is_enough_is_accepted_without_sourcing(run_case) -> None:
    _, run, s, fx = run_case("s2_partial_enough")
    assert (run.status, run.decision["outcome"], run.decision["quantity"]) == ("COMPLETED", "ACCEPT", 250)
    po = s.get(PurchaseOrder, "PO-2001")
    assert (po.status, po.lines[0].qty_ordered, po.lines[0].qty_confirmed) == ("CONFIRMED", 250, 250)
    assert s.scalars(select(PurchaseOrder).filter_by(run_id=run.id)).first() is None
    verify = next(st for st in run.steps if st.kind == "verification")
    assert verify.ok and verify.output["actual_end_levels"] == fx.expected.computed["projection_no_order"]


def test_partial_gap_covered_by_transfer_when_alternate_moq_overstocks(run_case) -> None:
    from app.models import StockTransfer

    _, run, s, fx = run_case("s2_alt_moq_exceeds_gap")
    assert (run.status, run.decision["outcome"], run.decision["option_kind"]) == ("COMPLETED", "MODIFY", "TRANSFER")
    t = s.scalars(select(StockTransfer).filter_by(run_id=run.id)).one()
    assert (t.from_node_id, t.to_node_id, t.qty, t.status) == ("CDMX-02", "CDMX-01", 80, "PLANNED")
    assert s.get(PurchaseOrder, "PO-2003").lines[0].qty_ordered == 250
    verify = next(st for st in run.steps if st.kind == "verification")
    assert verify.output["actual_end_levels"] == fx.expected.computed["projection_with_transfer"]


def test_a_run_over_its_time_budget_ends_failed_with_run_timeout(run_case) -> None:
    from app.agent.loop import PurchasingAgent
    from app.llm.scripted import ScriptedClient

    _, _, s, fx = run_case("s1_overstock", turns=[])
    ticks = iter(range(0, 10_000, 100))  # every state step "takes" 100 s
    agent = PurchasingAgent(s, ScriptedClient.for_case("s1_overstock"), max_run_seconds=250,
                            now=lambda: float(next(ticks)))
    run = agent.run(agent.start(fx.id, fx.trigger.model_dump()))
    assert run.status == "FAILED"
    err = next(st for st in run.steps if st.kind == "error")
    assert err.name == "RUN_TIMEOUT" and "RUN_MAX_SECONDS" in err.output["message"]


def test_time_waiting_for_approval_does_not_count(run_case) -> None:
    agent, run, s, _ = run_case("s4_budget_binding")
    assert run.status == "AWAITING_APPROVAL"
    used = run.context["active_s"]
    approval = s.scalars(select(Approval).filter_by(run_id=run.id)).one()
    agent.max_run_seconds = used + 5  # a long human pause would blow a wall-clock budget, not an active one
    agent.resolve_and_resume(run, approval.id, approve=True, decided_by="cm")
    assert run.status == "COMPLETED"


def test_retries_and_waits_appear_on_the_trace(run_case) -> None:
    from app.agent.loop import PurchasingAgent
    from app.llm.base import LLMClient
    from app.llm.scripted import ScriptedClient

    class Flaky(LLMClient):
        """Reports a rate-limit wait on its first call, like a real client hitting a 429."""
        name = "flaky"

        def __init__(self):
            self.inner, self.calls = ScriptedClient.for_case("s1_overstock"), 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1 and self.on_wait:
                self.on_wait({"reason": "rate_limit", "delay_s": 7.0, "attempt": 1, "message": "429"})
            return self.inner.complete(messages, tools)

    _, _, s, fx = run_case("s1_overstock", turns=[])
    agent = PurchasingAgent(s, Flaky())
    run = agent.run(agent.start(fx.id, fx.trigger.model_dump()))
    wait = next(st for st in run.steps if st.kind == "wait")
    assert wait.name == "waiting: rate limited, retrying in 7s" and wait.state == "INVESTIGATE"
    assert wait.seq < next(st.seq for st in run.steps if st.kind == "llm")  # recorded before the call returned
    assert run.status == "COMPLETED" and agent.llm.on_wait is None


def test_a_replayed_submit_after_approval_does_not_pause_the_run_again(run_case) -> None:
    turns = script_turns("s4_budget_binding")
    retry = {"tool_calls": [{"name": "submit_po", "args": {"po_id": "$ref:create_po_draft.po_id",
                                                           "idempotency_key": "submit"}}]}  # same key: replayed
    agent, run, s, _ = run_case("s4_budget_binding", turns[:5] + [retry] + turns[5:])
    approval = s.scalars(select(Approval).filter_by(run_id=run.id)).one()
    agent.resolve_and_resume(run, approval.id, approve=True, decided_by="cm")
    assert run.status == "COMPLETED"
    replay = [st for st in run.steps if st.name == "submit_po"][-1]
    assert replay.output["output"]["replayed"] is True


def test_a_discretionary_approval_request_pauses_the_run(run_case) -> None:
    turns = script_turns("s1_overstock")
    ask = {"tool_calls": [{"name": "request_approval", "args": {"summary": "please double-check", "reasons": ["NEW_SKU"],
                                                                "idempotency_key": "ask"}}]}
    agent, run, s, _ = run_case("s1_overstock", turns[:5] + [ask] + turns[5:])
    assert run.status == "AWAITING_APPROVAL"
    approval = s.scalars(select(Approval).filter_by(run_id=run.id)).one()
    assert approval.reasons == ["AGENT_REQUESTED", "NEW_SKU"]
    agent.resolve_and_resume(run, approval.id, approve=True, decided_by="cm")
    assert run.status == "COMPLETED"


def test_one_turn_with_some_malformed_parallel_calls_is_not_escalated(run_case) -> None:
    turns = script_turns("s1_overstock")
    mixed = {"tool_calls": turns[0]["tool_calls"] + [{"name": "get_inventory", "args": {"node": "BOG-01"}},
                                                     {"name": "buy_now", "args": {}}]}
    _, run, _, _ = run_case("s1_overstock", [mixed] + turns[1:])
    assert run.status == "COMPLETED" and run.decision["quantity"] == 240
    assert not any(st.kind == "escalation" for st in run.steps)


def test_choosing_escalate_is_reported_as_an_agent_escalation(run_case) -> None:
    turns = script_turns("s1_overstock")[:2] + [
        {"tool_calls": [{"name": "propose_decision", "args": {"option_id": "ESCALATE",
                                                              "reasons": ["supplier relationship issue"]}}]},
        {"text": "Escalated to a buyer."}]
    _, run, _, _ = run_case("s1_overstock", turns)
    esc = next(st for st in run.steps if st.kind == "escalation")
    assert run.status == "ESCALATED" and esc.name == "AGENT_ESCALATED"
    assert "supplier relationship issue" in esc.input["summary"]
    assert run.decision["option_kind"] == "ESCALATE"


def test_a_price_change_the_gate_refuses_escalates_instead_of_crashing(run_case, monkeypatch) -> None:
    import app.agent.loop as loop
    from app.tools.registry import ToolError

    def refuse(*a, **k):
        raise ToolError("ESCALATION_REQUIRED", "policy gate: VALIDATION_FAILED_TWICE", {})

    monkeypatch.setattr(loop, "gate_price_change", refuse)
    _, run, _, _ = run_case("x_price_change")
    assert run.status == "ESCALATED"
    assert next(st for st in run.steps if st.kind == "escalation").name == "ESCALATION_REQUIRED"
