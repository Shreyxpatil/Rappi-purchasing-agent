"""The LLM contract the agent depends on. Providers translate to and from these types."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)  # assistant only
    tool_call_id: str | None = None  # tool result only
    name: str | None = None  # tool result: which tool produced it
    # Provider-specific payload that must be sent back verbatim on the next turn
    # (e.g. Gemini thought signatures). JSON-serialisable so a paused run can be resumed.
    provider_data: dict[str, Any] | None = None


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tokens_in: int | None = None
    tokens_out: int | None = None
    model: str = ""
    provider_data: dict[str, Any] | None = None

    def as_message(self) -> Message:
        return Message(role="assistant", content=self.text, tool_calls=self.tool_calls,
                       provider_data=self.provider_data)


class LLMError(Exception):
    """A provider failure the agent cannot recover from inside the current step."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class LLMClient(ABC):
    name: str = "base"
    # Set by the agent before each call: receives {"reason", "delay_s", ...} for every retry or pacing wait,
    # so the run's trace shows the wait while it happens.
    on_wait: Callable[[dict[str, Any]], None] | None = None

    @abstractmethod
    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMResponse:
        """One model turn. `tools` are JSON-schema function declarations ({name, description, parameters})."""
