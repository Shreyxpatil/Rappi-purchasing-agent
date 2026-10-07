import json

from app.agent.narrative import numbers_in, template_narrative, ungrounded_numbers
from fixture_inputs import script_turns

DECISION = {"outcome": "MODIFY", "quantity": 240, "option_id": "BUY:SUP-ALQ:240", "value": 1008000.0,
            "factors": [{"name": "own net requirement", "value": "140 units -> order 240", "effect": "sets"},
                        {"name": "cover at arrival", "value": "5.67 days", "effect": "within limit"}],
            "residual_risk": None, "information_needed": []}


def test_identifiers_are_not_numbers_and_commas_are_normalised() -> None:
    assert numbers_in("PO-1002 at BOG-02 for COCA-1.5L: 1,008,000 COP and 5.67 days") == {1008000.0, 5.67}


def test_grounding_flags_only_invented_figures() -> None:
    ok = "Ordered 240 instead of 800? No: 240 (net 140), 1,008,000 COP, 5.67 days of cover, 3 checks."
    assert ungrounded_numbers(ok, DECISION, {"recommendation": 800}) == []
    assert ungrounded_numbers("Ordered 250 units for 1,050,000 COP.", DECISION) == [250.0, 1050000.0]


def test_every_scripted_narrative_is_grounded(run_case) -> None:
    for case in ("s1_overstock", "s1_already_covered", "s1_stale_inventory"):
        _, run, _, _ = run_case(case)
        check = [st for st in run.steps if st.kind == "narrative_check"]
        assert [c.name for c in check] == ["grounded"], case
        assert run.context["narrative_source"] == "model"


def test_ungrounded_narrative_is_retried_once_then_replaced_by_template(run_case) -> None:
    turns = script_turns("s1_overstock")[:-1] + [{"text": "Bought 250 units for 999,999 COP."},
                                           {"text": "Bought 260 units."}]
    _, run, _, _ = run_case("s1_overstock", turns)
    assert [st.name for st in run.steps if st.kind == "narrative_check"] == ["ungrounded", "ungrounded"]
    assert run.context["narrative_source"] == "template"
    assert run.narrative.startswith("MODIFY: 240 units via BUY:SUP-ALQ:240.")
    assert "250" not in run.narrative and run.status == "COMPLETED"


def test_retry_that_fixes_the_numbers_is_accepted(run_case) -> None:
    turns = script_turns("s1_overstock")[:-1] + [{"text": "Bought 250 units."}, {"text": "Bought 240 units."}]
    _, run, _, _ = run_case("s1_overstock", turns)
    assert run.narrative == "Bought 240 units." and run.context["narrative_source"] == "model_retry"


def test_template_uses_only_decision_fields() -> None:
    text = template_narrative(DECISION, {"purchase_orders": [{"po_id": "PO-9001", "qty": 240, "supplier": "SUP-ALQ",
                                                              "status": "SUBMITTED"}], "transfers": []})
    assert ungrounded_numbers(text, DECISION, {"po": "PO-9001"}) == []
    assert "PO-9001: 240 units from SUP-ALQ, SUBMITTED" in text and json.dumps(DECISION)
