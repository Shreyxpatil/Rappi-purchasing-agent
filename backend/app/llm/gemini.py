"""Gemini via the official google-genai SDK with native function calling.

* model, thinking level and pacing come from settings (GEMINI_MODEL, GEMINI_THINKING_LEVEL, LLM_MAX_RPM);
* temperature is left at the provider default: Google advises 1.0 for Gemini 3 models (decision D24);
* the model's raw parts, including thought signatures, are kept in Message.provider_data and sent back
  verbatim, which Gemini 3 requires for multi-turn function calling;
* 429 / RESOURCE_EXHAUSTED / 5xx are retried with backoff, honouring the server's retryDelay.
"""

import re
from typing import Any

from google import genai
from google.genai import errors, types

from app.llm.base import LLMClient, LLMError, LLMResponse, Message, ToolCall
from app.llm.pacing import Pacer, RetryableError, with_backoff

RETRYABLE_CODES = {429, 500, 502, 503, 504}


class GeminiClient(LLMClient):
    name = "gemini"

    def __init__(self, api_key: str, model: str, *, thinking_level: str = "low", max_rpm: int = 8,
                 client: Any = None, pacer: Pacer | None = None, max_retries: int = 5, sleep=None) -> None:
        if not model:
            raise LLMError("CONFIG", "GEMINI_MODEL is not set")
        if client is None and not api_key:
            raise LLMError("CONFIG", "GEMINI_API_KEY is not set")
        self.model = model
        self.thinking_level = thinking_level
        self.client = client or genai.Client(api_key=api_key)
        self.pacer = pacer or Pacer(max_rpm)
        self.max_retries = max_retries
        self._sleep = sleep

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMResponse:
        system, contents = to_contents(messages)
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"],
                                          parameters_json_schema=t["parameters"]) for t in tools])] if tools else None,
            thinking_config=types.ThinkingConfig(thinking_level=self.thinking_level.upper()),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        def call():
            self.pacer.wait()
            try:
                return self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except errors.APIError as e:
                if e.code in RETRYABLE_CODES:
                    raise RetryableError(f"{e.code} {e.status}: {e.message}", _retry_after(e)) from e
                raise LLMError(f"HTTP_{e.code}", f"{e.status}: {e.message}") from e

        kwargs = {"sleep": self._sleep} if self._sleep else {}
        try:
            resp = with_backoff(call, max_retries=self.max_retries, **kwargs)
        except RetryableError as e:
            raise LLMError("RATE_LIMITED", f"gave up after {self.max_retries} retries: {e}") from e
        return from_response(resp, self.model)


def to_contents(messages: list[Message]) -> tuple[str, list[types.Content]]:
    """Our messages -> Gemini contents. Consecutive tool results become one user turn (parallel calls)."""
    system = "\n\n".join(m.content for m in messages if m.role == "system")
    contents: list[types.Content] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=m.content)]))
        elif m.role == "assistant":
            if m.provider_data and m.provider_data.get("gemini_parts"):
                parts = [types.Part.model_validate(p) for p in m.provider_data["gemini_parts"]]
            else:  # e.g. a turn produced by another provider: rebuild without signatures
                parts = ([types.Part(text=m.content)] if m.content else []) + [
                    types.Part(function_call=types.FunctionCall(id=c.id, name=c.name, args=c.args))
                    for c in m.tool_calls]
            contents.append(types.Content(role="model", parts=parts))
        else:
            part = types.Part(function_response=types.FunctionResponse(
                id=m.tool_call_id, name=m.name, response={"result": m.content}))
            if contents and contents[-1].role == "user" and contents[-1].parts \
                    and contents[-1].parts[0].function_response is not None:
                contents[-1].parts.append(part)
            else:
                contents.append(types.Content(role="user", parts=[part]))
    return system, contents


def from_response(resp: Any, model: str) -> LLMResponse:
    candidate = (resp.candidates or [None])[0]
    parts = list(candidate.content.parts or []) if candidate and candidate.content else []
    text = "".join(p.text for p in parts if p.text and not p.thought)
    calls = [ToolCall(id=p.function_call.id or f"gemini_{i}", name=p.function_call.name,
                      args=dict(p.function_call.args or {}))
             for i, p in enumerate(parts) if p.function_call is not None]
    usage = resp.usage_metadata
    return LLMResponse(
        text=text, tool_calls=calls, model=model,
        tokens_in=usage.prompt_token_count if usage else None,
        tokens_out=usage.candidates_token_count if usage else None,
        provider_data={"gemini_parts": [p.model_dump(mode="json", exclude_none=True) for p in parts]},
    )


def _retry_after(e: errors.APIError) -> float | None:
    """Gemini puts a RetryInfo {'retryDelay': '17s'} in the error details on 429."""
    details = (e.details or {}).get("error", {}).get("details", []) if isinstance(e.details, dict) else []
    for d in details:
        m = re.fullmatch(r"([\d.]+)s", str(d.get("retryDelay", "")))
        if m:
            return float(m.group(1))
    return None
