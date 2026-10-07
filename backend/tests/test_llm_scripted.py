import json

import pytest

from app.llm.base import LLMError, Message
from app.llm.scripted import ScriptedClient

TURNS = [
    {"tool_calls": [{"name": "create_po_draft", "args": {"qty": 240}}]},
    {"tool_calls": [{"name": "submit_po", "args": {"po_id": "$ref:create_po_draft.po_id", "nested": ["$ref:create_po_draft.status"]}}]},
    {"text": "done"},
]


def _tool_msg(name, output, ok=True):
    return Message(role="tool", name=name, tool_call_id="x", content=json.dumps({"ok": ok, "output": output}))


def test_turn_is_chosen_by_assistant_messages_so_far() -> None:
    client = ScriptedClient(TURNS, label="t")
    first = client.complete([Message(role="user", content="go")], tools=[])
    assert [c.name for c in first.tool_calls] == ["create_po_draft"] and first.tool_calls[0].id == "call_0_0"

    history = [Message(role="user", content="go"), first.as_message(),
               _tool_msg("create_po_draft", {"po_id": "PO-9001", "status": "DRAFT"})]
    second = ScriptedClient(TURNS, label="t").complete(history, tools=[])  # a fresh client resumes correctly
    assert second.tool_calls[0].args == {"po_id": "PO-9001", "nested": ["DRAFT"]}


def test_refs_use_the_latest_successful_output_only() -> None:
    history = [Message(role="assistant"),
               _tool_msg("create_po_draft", {"po_id": "PO-9001", "status": "DRAFT"}),
               _tool_msg("create_po_draft", {"po_id": "PO-9002", "status": "DRAFT"}),
               _tool_msg("create_po_draft", {"code": "BLOCKED"}, ok=False)]
    out = ScriptedClient(TURNS).complete(history, tools=[])
    assert out.tool_calls[0].args["po_id"] == "PO-9002"


def test_unresolved_ref_and_exhausted_script_are_typed_errors() -> None:
    with pytest.raises(LLMError) as e:
        ScriptedClient(TURNS).complete([Message(role="assistant")], tools=[])
    assert e.value.code == "UNRESOLVED_REF"
    with pytest.raises(LLMError) as e:
        ScriptedClient(TURNS).complete([Message(role="assistant")] * 3, tools=[])
    assert e.value.code == "SCRIPT_EXHAUSTED"


def test_missing_script_file() -> None:
    with pytest.raises(LLMError) as e:
        ScriptedClient.for_case("no_such_case")
    assert e.value.code == "NO_SCRIPT"
