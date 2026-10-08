"""LLM judge for explanation quality only, against evals/rubric.md. Everything else is graded in code."""

import json
import re
from pathlib import Path
from typing import Any

from app.llm.base import LLMClient, LLMError, Message

RUBRIC = (Path(__file__).parent / "rubric.md").read_text(encoding="utf-8")
CRITERIA = ("decision_clarity", "reasons", "risk_and_handoff", "faithfulness", "concision")


def judge_narrative(client: LLMClient, narrative: str, decision: dict[str, Any]) -> dict[str, Any]:
    compact = {k: decision.get(k) for k in ("outcome", "quantity", "option_id", "residual_risk", "information_needed")}
    compact["factors"] = [f"{f['name']}: {f['value']} ({f['effect']})" for f in decision.get("factors", [])]
    messages = [
        Message(role="system", content="You grade explanations written for a purchasing buyer. Reply with JSON only."),
        Message(role="user", content=f"{RUBRIC}\n\nStructured decision:\n{json.dumps(compact)}\n\n"
                                     f"Explanation to grade:\n{narrative}"),
    ]
    try:
        text = client.complete(messages, tools=[]).text
    except LLMError as e:
        return {"error": f"{e.code}: {e}"}
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
        scores = {c: int(data["scores"][c]) for c in CRITERIA}
    except (ValueError, KeyError, TypeError):
        return {"error": "unparseable judge output", "raw": text[:300]}
    return {"scores": scores, "average": round(sum(scores.values()) / len(scores), 2),
            "comment": str(data.get("comment", ""))[:300]}
