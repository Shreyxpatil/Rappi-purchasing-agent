"""OpenAI-compatible client tests against an in-process mock transport: no network, no key."""

import json

import httpx
import pytest

from app.llm.base import LLMError, Message, ToolCall
from app.llm.openai_compat import OpenAICompatibleClient
from app.llm.pacing import Pacer


def _client(handler, **kw):
    sleeps = []
    c = OpenAICompatibleClient("https://api.groq.com/openai/v1/", "test-key", "llama-3.3-70b-versatile",
                               transport=httpx.MockTransport(handler), pacer=Pacer(0), sleep=sleeps.append, **kw)
    return c, sleeps


def _ok(message, usage=None):
    return httpx.Response(200, json={"model": "llama-3.3-70b-versatile", "choices": [{"message": message}],
                                     "usage": usage or {"prompt_tokens": 50, "completion_tokens": 9}})


TOOLS = [{"name": "get_inventory", "description": "stock", "parameters": {"type": "object", "properties": {}}}]


def test_request_shape_and_tool_call_parsing() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return _ok({"content": None, "tool_calls": [{"id": "t1", "type": "function", "function": {
            "name": "get_inventory", "arguments": '{"node": "BOG-01"}'}}]})

    client, _ = _client(handler)
    history = [Message(role="system", content="rules"), Message(role="user", content="go"),
               Message(role="assistant", tool_calls=[ToolCall(id="a", name="x", args={"k": 1})]),
               Message(role="tool", name="x", tool_call_id="a", content='{"ok":true}')]
    out = client.complete(history, TOOLS)
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    body = seen["body"]
    assert body["tool_choice"] == "auto" and body["tools"][0]["function"]["name"] == "get_inventory"
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"k": 1}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "a", "content": '{"ok":true}'}
    assert out.tool_calls == [ToolCall(id="t1", name="get_inventory", args={"node": "BOG-01"})]
    assert (out.tokens_in, out.tokens_out) == (50, 9)


def test_invalid_json_arguments_reach_schema_validation() -> None:
    client, _ = _client(lambda req: _ok({"tool_calls": [{"id": "t", "function": {
        "name": "get_inventory", "arguments": "{node: BOG-01"}}]}))
    out = client.complete([Message(role="user", content="x")], TOOLS)
    assert out.tool_calls[0].args == {"_unparseable_arguments": "{node: BOG-01"}


def test_429_retries_with_retry_after_then_succeeds() -> None:
    responses = [httpx.Response(429, headers={"retry-after": "3"}, text="slow down"),
                 _ok({"content": "hi"})]
    client, sleeps = _client(lambda req: responses.pop(0))
    assert client.complete([Message(role="user", content="x")], []).text == "hi"
    assert len(sleeps) == 1 and sleeps[0] >= 3


def test_client_error_fails_fast_and_config_is_validated() -> None:
    calls = []
    client, sleeps = _client(lambda req: calls.append(1) or httpx.Response(401, text="bad key"))
    with pytest.raises(LLMError) as e:
        client.complete([Message(role="user", content="x")], [])
    assert e.value.code == "HTTP_401" and len(calls) == 1 and sleeps == []
    with pytest.raises(LLMError, match="OPENAI_COMPAT_API_KEY"):
        OpenAICompatibleClient("https://x", "", "m")


def test_groq_daily_token_limit_fails_fast() -> None:
    body = ('{"error":{"message":"Rate limit reached for model qwen/qwen3.8-27b on tokens per day (TPD): '
            'Limit 500000, Used 499800","type":"tokens","code":"rate_limit_exceeded"}}')
    client, sleeps = _client(lambda req: httpx.Response(429, headers={"retry-after": "3600"}, text=body))
    with pytest.raises(LLMError) as e:
        client.complete([Message(role="user", content="x")], [])
    assert e.value.code == "LLM_QUOTA_EXHAUSTED" and "daily" in str(e.value) and sleeps == []


def test_network_outage_is_reported_as_network() -> None:
    def down(req):
        raise httpx.ConnectError("Temporary failure in name resolution")

    client, sleeps = _client(down, max_retries=2)
    with pytest.raises(LLMError) as e:
        client.complete([Message(role="user", content="x")], [])
    assert e.value.code == "NETWORK" and len(sleeps) == 2
