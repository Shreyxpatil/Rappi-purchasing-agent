"""Every step of a run is written to agent_steps as it happens, so the UI can show it live and a
paused run can be resumed from agent_runs.context."""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.llm.base import Message
from app.models import AgentRun, AgentStep
from app.tools.registry import RunState


class Recorder:
    def __init__(self, session: Session, run: AgentRun, now: datetime) -> None:
        self.session, self.run, self.now = session, run, now
        self.seq = len(run.steps)

    def step(self, kind: str, name: str, input: Any = None, output: Any = None, *, ok: bool = True,
             latency_ms: int = 0, tokens_in: int | None = None, tokens_out: int | None = None) -> AgentStep:
        self.seq += 1
        s = AgentStep(run_id=self.run.id, seq=self.seq, state=self.run.state, kind=kind, name=name,
                      input=input if input is not None else {}, output=output if output is not None else {},
                      ok=ok, latency_ms=latency_ms, tokens_in=tokens_in, tokens_out=tokens_out, created_at=self.now)
        self.run.steps.append(s)
        self.session.commit()
        return s

    def transition(self, to: str, reason: str = "") -> None:
        frm = self.run.state
        self.run.state = to
        self.step("transition", f"{frm}->{to}", {"reason": reason} if reason else {})

    def save(self, state: RunState, messages: list[Message], extra: dict[str, Any] | None = None) -> None:
        self.run.context = {"state": state.model_dump(mode="json"),
                            "messages": [m.model_dump(mode="json") for m in messages],
                            **(extra or {})}
        self.session.commit()


def load_context(run: AgentRun) -> tuple[RunState, list[Message], dict[str, Any]]:
    ctx = run.context or {}
    extra = {k: v for k, v in ctx.items() if k not in ("state", "messages")}
    return (RunState.model_validate(ctx.get("state", {})),
            [Message.model_validate(m) for m in ctx.get("messages", [])], extra)
