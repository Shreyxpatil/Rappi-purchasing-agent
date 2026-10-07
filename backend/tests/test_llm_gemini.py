"""Gemini client tests with a fake SDK client: no network, no key, no quota."""

import json

import pytest
from google.genai import errors, types

from app.llm.base import LLMError, Message, ToolCall
from app.llm.gemini import GeminiClient, from_response, to_contents
from app.llm.pacing import Pacer, RetryableError, with_backoff

SIG = bytes([0, 255, 10, 200, 7])


def _response(*parts, prompt=12, out=5):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=prompt,
                                                                  candidates_token_count=out))


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


class FakeClient:
    def __init__(self, outcomes):
        self.models = FakeModels(outcomes)


def _client(outcomes, **kw):
    fake = FakeClient(outcomes)
    sleeps = []
    c = GeminiClient("", "gemini-3.8-flash", client=fake, pacer=Pacer(0), sleep=sleeps.append, **kw)
    return c, fake, sleeps


def _api_error(code, status, retry_delay=None):
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}] if retry_delay else []
    return errors.APIError(code, {"error": {"code": code, "status": status, "message": "boom", "details": details}})


TOOLS = [{"name": "get_inventory", "description": "stock", "parameters": {"type": "object", "properties": {
    "node": {"type": "string"}}, "required": ["node"]}}]


def test_function_call_response_keeps_signature_and_strips_thoughts() -> None:
    resp = _response(types.Part(text="thinking...", thought=True),
                     types.Part(function_call=types.FunctionCall(id="c1", name="get_inventory", args={"node": "BOG-01"}),
                                thought_signature=SIG))
    out = from_response(resp, "gemini-3.8-flash")
    assert out.text == "" and out.tool_calls == [ToolCall(id="c1", name="get_inventory", args={"node": "BOG-01"})]
    assert (out.tokens_in, out.tokens_out) == (12, 5)
    stored = json.loads(json.dumps(out.provider_data))  # survives persistence while a run waits for approval
    _, contents = to_contents([Message(role="user", content="go"), Message(**out.as_message().model_dump()
                                                                            | {"provider_data": stored})])
    assert contents[1].role == "model" and contents[1].parts[1].thought_signature == SIG


def test_parallel_tool_results_become_one_user_turn() -> None:
    msgs = [Message(role="system", content="rules"), Message(role="user", content="go"),
            Message(role="assistant", tool_calls=[ToolCall(id="a", name="x"), ToolCall(id="b", name="y")]),
            Message(role="tool", name="x", tool_call_id="a", content='{"ok":true}'),
            Message(role="tool", name="y", tool_call_id="b", content='{"ok":false}')]
    system, contents = to_contents(msgs)
    assert system == "rules"
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert [p.function_response.id for p in contents[2].parts] == ["a", "b"]


def test_config_uses_thinking_level_default_temperature_and_manual_function_calling() -> None:
    client, fake, _ = _client([_response(types.Part(text="hi"))])
    out = client.complete([Message(role="system", content="rules"), Message(role="user", content="hello")], TOOLS)
    cfg = fake.models.calls[0]["config"]
    assert out.text == "hi"
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert cfg.temperature is None  # provider default (1.0), per Google's Gemini 3 guidance
    assert cfg.automatic_function_calling.disable is True
    assert cfg.tools[0].function_declarations[0].parameters_json_schema["required"] == ["node"]


def test_rate_limit_is_retried_honouring_retry_delay() -> None:
    client, fake, sleeps = _client([_api_error(429, "RESOURCE_EXHAUSTED", "7s"), _api_error(503, "UNAVAILABLE"),
                                    _response(types.Part(text="ok"))])
    assert client.complete([Message(role="user", content="x")], []).text == "ok"
    assert len(fake.models.calls) == 3 and len(sleeps) == 2 and sleeps[0] >= 7


def test_gives_up_after_max_retries_and_fails_fast_on_client_errors() -> None:
    client, _, _ = _client([_api_error(429, "RESOURCE_EXHAUSTED")] * 3, max_retries=2)
    with pytest.raises(LLMError) as e:
        client.complete([Message(role="user", content="x")], [])
    assert e.value.code == "RATE_LIMITED"
    client, fake, sleeps = _client([_api_error(400, "INVALID_ARGUMENT")])
    with pytest.raises(LLMError) as e:
        client.complete([Message(role="user", content="x")], [])
    assert e.value.code == "HTTP_400" and len(fake.models.calls) == 1 and sleeps == []


def test_missing_configuration_is_a_clear_error() -> None:
    with pytest.raises(LLMError, match="GEMINI_MODEL"):
        GeminiClient("key", "")
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiClient("", "gemini-3.8-flash")


def test_pacer_spaces_requests() -> None:
    t, waits = [0.0], []
    pacer = Pacer(6, now=lambda: t[0], sleep=lambda d: (waits.append(d), t.__setitem__(0, t[0] + d)))
    for _ in range(3):
        pacer.wait()
    assert waits == [10.0, 10.0]  # 6 requests/minute = one every 10 s


def test_backoff_is_exponential_capped_and_jittered() -> None:
    import random

    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 4:
            raise RetryableError("429")
        return "done"

    assert with_backoff(flaky, base=2, cap=5, sleep=sleeps.append, rng=random.Random(1)) == "done"
    assert len(sleeps) == 3 and all(0 <= s <= 5 for s in sleeps)


def test_factory_selects_provider_from_settings() -> None:
    from app.config import Settings
    from app.llm.factory import make_client
    from app.llm.scripted import ScriptedClient

    assert isinstance(make_client("scripted", case_id="s1_overstock"), ScriptedClient)
    with pytest.raises(LLMError, match="case id"):
        make_client("scripted")
    s = Settings(_env_file=None, gemini_api_key="k", gemini_model="gemini-3.8-flash", gemini_thinking_level="minimal")
    g = make_client("gemini", settings=s)
    assert isinstance(g, GeminiClient) and (g.model, g.thinking_level) == ("gemini-3.8-flash", "minimal")
    with pytest.raises(LLMError, match="unknown"):
        make_client("claude-by-mistake", settings=s)
