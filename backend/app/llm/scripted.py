"""Deterministic client that replays a scripted trajectory. No network, no key, no cost.

Script format (evals/scripts/<case>.json):
    {"case": "s1_overstock", "turns": [
        {"tool_calls": [{"name": "get_inventory", "args": {...}}, ...]},
        {"tool_calls": [{"name": "submit_po", "args": {"po_id": "$ref:create_po_draft.po_id", ...}}]},
        {"text": "final narrative"}]}

The turn to play is the number of assistant messages already in the conversation, so a run that
paused for approval resumes at the right turn even in a new process. "$ref:<tool>.<field>" is
replaced with that field of the latest successful output of <tool> in the conversation.
"""

import json
from typing import Any

from app.config import REPO_ROOT
from app.llm.base import LLMClient, LLMError, LLMResponse, Message, ToolCall

SCRIPTS_DIR = REPO_ROOT / "evals" / "scripts"


class ScriptedClient(LLMClient):
    name = "scripted"

    def __init__(self, turns: list[dict[str, Any]], label: str = "") -> None:
        self.turns = turns
        self.label = label

    @classmethod
    def for_case(cls, case_id: str, variant: str = "") -> "ScriptedClient":
        path = SCRIPTS_DIR / f"{case_id}{'__' + variant if variant else ''}.json"
        if not path.exists():
            raise LLMError("NO_SCRIPT", f"no scripted trajectory at {path.relative_to(REPO_ROOT)}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["turns"], label=path.stem)

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMResponse:
        index = sum(1 for m in messages if m.role == "assistant")
        if index >= len(self.turns):
            raise LLMError("SCRIPT_EXHAUSTED", f"{self.label}: no turn {index + 1} (script has {len(self.turns)})")
        turn = self.turns[index]
        calls = [ToolCall(id=f"call_{index}_{i}", name=c["name"], args=_resolve(c.get("args", {}), messages))
                 for i, c in enumerate(turn.get("tool_calls", []))]
        return LLMResponse(text=turn.get("text", ""), tool_calls=calls, model=f"scripted:{self.label}")


def _resolve(value: Any, messages: list[Message]) -> Any:
    if isinstance(value, dict):
        return {k: _resolve(v, messages) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, messages) for v in value]
    if isinstance(value, str) and value.startswith("$ref:"):
        tool, _, field = value.removeprefix("$ref:").partition(".")
        for m in reversed(messages):
            if m.role == "tool" and m.name == tool:
                payload = json.loads(m.content)
                if payload.get("ok") and field in (payload.get("output") or {}):
                    return payload["output"][field]
        raise LLMError("UNRESOLVED_REF", f"{value}: no successful {tool} output with field {field!r}")
    return value
