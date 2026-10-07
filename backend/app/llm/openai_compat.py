"""Any OpenAI-compatible chat-completions API (Groq preset in the README) over plain httpx.

Same contract as the Gemini client: paced, 429/5xx retried with jittered backoff (honouring Retry-After),
other errors fail fast. Tool arguments the model sends as invalid JSON are passed on as an
`_unparseable_arguments` field, so the agent's schema validation returns a precise error once.
"""

import json
from typing import Any

import httpx

from app.llm.base import LLMClient, LLMError, LLMResponse, Message, ToolCall
from app.llm.pacing import Pacer, RetryableError, with_backoff

RETRYABLE_CODES = {429, 500, 502, 503, 504}


class OpenAICompatibleClient(LLMClient):
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, *, max_rpm: int = 8,
                 transport: httpx.BaseTransport | None = None, pacer: Pacer | None = None, max_retries: int = 5,
                 sleep=None, timeout: float = 60.0) -> None:
        missing = [n for n, v in (("OPENAI_COMPAT_BASE_URL", base_url), ("OPENAI_COMPAT_API_KEY", api_key),
                                  ("OPENAI_COMPAT_MODEL", model)) if not v]
        if missing:
            raise LLMError("CONFIG", f"{', '.join(missing)} not set")
        self.model = model
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                 headers={"Authorization": f"Bearer {api_key}"})
        self.pacer = pacer or Pacer(max_rpm)
        self.max_retries = max_retries
        self._sleep = sleep

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "messages": [to_openai(m) for m in messages]}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
            body["tool_choice"] = "auto"

        def call() -> dict[str, Any]:
            self.pacer.wait()
            try:
                r = self.http.post("/chat/completions", json=body)
            except httpx.TransportError as e:
                raise RetryableError(f"transport: {e}") from e
            if r.status_code in RETRYABLE_CODES:
                retry_after = r.headers.get("retry-after")
                raise RetryableError(f"HTTP {r.status_code}: {r.text[:200]}",
                                     float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else None)
            if r.status_code >= 400:
                raise LLMError(f"HTTP_{r.status_code}", r.text[:300])
            return r.json()

        kwargs = {"sleep": self._sleep} if self._sleep else {}
        try:
            data = with_backoff(call, max_retries=self.max_retries, **kwargs)
        except RetryableError as e:
            raise LLMError("RATE_LIMITED", f"gave up after {self.max_retries} retries: {e}") from e
        return from_openai(data, self.model)


def to_openai(m: Message) -> dict[str, Any]:
    if m.role == "assistant":
        out: dict[str, Any] = {"role": "assistant", "content": m.content or None}
        if m.tool_calls:
            out["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name, "arguments": json.dumps(c.args)}} for c in m.tool_calls]
        return out
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    return {"role": m.role, "content": m.content}


def from_openai(data: dict[str, Any], model: str) -> LLMResponse:
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    calls = []
    for i, c in enumerate(msg.get("tool_calls") or []):
        raw = c.get("function", {}).get("arguments") or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            args = {"_unparseable_arguments": raw}
        calls.append(ToolCall(id=c.get("id") or f"call_{i}", name=c["function"]["name"], args=args))
    usage = data.get("usage") or {}
    return LLMResponse(text=msg.get("content") or "", tool_calls=calls, model=data.get("model", model),
                       tokens_in=usage.get("prompt_tokens"), tokens_out=usage.get("completion_tokens"))
