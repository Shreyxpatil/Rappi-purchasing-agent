"""Provider preflight tests: fake transports and fake SDK clients only. No network, no key, no quota."""

import httpx
import httpx2
import pytest
from google.genai import errors, types

from app.config import Settings
from app.llm import preflight
from app.llm.base import LLMError
from app.llm.preflight import Check, check_anthropic, check_gemini, check_openai_compat, classify, is_listed

KEY = "gsk_test_key_never_printed_123456"


def _settings(**kw) -> Settings:
    base = dict(_env_file=None, gemini_api_key="AIza-test-key-0000000000", gemini_model="gemini-3.8-flash",
                openai_compat_base_url="https://api.groq.com/openai/v1", openai_compat_api_key=KEY,
                openai_compat_model="qwen/qwen3.8-27b", anthropic_api_key="sk-ant-test-000000000000",
                anthropic_model="claude-haiku-4-5")
    return Settings(**{**base, **kw})


@pytest.mark.parametrize("code,text,status", [
    (400, "Your credit balance is too low to access the Anthropic API", "NO_CREDITS"),
    (402, "payment required", "NO_CREDITS"),
    (401, "invalid x-api-key", "INVALID_KEY"),
    (400, "INVALID_ARGUMENT API key not valid. Please pass a valid API key.", "INVALID_KEY"),
    (404, "model not found", "MODEL_NOT_AVAILABLE"),
    (400, "The model `x` has been decommissioned", "MODEL_NOT_AVAILABLE"),
    (429, "RESOURCE_EXHAUSTED check your plan and billing details", "QUOTA_EXHAUSTED"),
    (500, "boom", "HTTP_500"),
])
def test_classify(code, text, status) -> None:
    assert classify(code, text) == status


def test_an_alias_matches_its_dated_snapshot() -> None:
    assert is_listed("claude-haiku-4-5", ["claude-haiku-4-5-20251001"])
    assert not is_listed("claude-haiku-4", ["claude-haiku-4-5-20251001"])


# --------------------------------------------------------------------------- OpenAI-compatible (Groq)


def _groq(models=("qwen/qwen3.8-27b", "llama-3.1-8b-instant"), chat=None, models_status=200):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, req.content))
        if req.url.path.endswith("/models"):
            return httpx.Response(models_status, json={"data": [{"id": m} for m in models]}
                                  if models_status == 200 else {"error": {"message": "Invalid API Key"}})
        return chat or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    return httpx.MockTransport(handler), seen


def test_groq_ok_lists_then_makes_one_tiny_call() -> None:
    transport, seen = _groq()
    c = check_openai_compat(_settings(), transport)
    assert (c.status, c.listed, c.callable) == ("OK", True, True)
    assert [s[:2] for s in seen] == [("GET", "/openai/v1/models"), ("POST", "/openai/v1/chat/completions")]
    assert b'"max_tokens":5' in seen[1][2].replace(b" ", b"")


def test_groq_model_missing_reports_the_closest_models() -> None:
    transport, _ = _groq(models=("qwen/qwen3-32b", "llama-3.1-8b-instant"),
                         chat=httpx.Response(404, json={"error": {"code": "model_not_found"}}))
    c = check_openai_compat(_settings(), transport)
    assert (c.status, c.listed, c.callable) == ("MODEL_NOT_AVAILABLE", False, False)
    assert c.closest[0] == "qwen/qwen3-32b"


def test_groq_bad_key_stops_before_the_call() -> None:
    transport, seen = _groq(models_status=401)
    c = check_openai_compat(_settings(), transport)
    assert (c.status, c.listed, c.callable) == ("INVALID_KEY", None, None) and len(seen) == 1


def test_groq_quota_and_network() -> None:
    transport, _ = _groq(chat=httpx.Response(429, json={"error": {"message": "Rate limit reached (TPD)"}}))
    assert check_openai_compat(_settings(), transport).status == "QUOTA_EXHAUSTED"

    def down(req):
        raise httpx.ConnectError("Temporary failure in name resolution")
    c = check_openai_compat(_settings(), httpx.MockTransport(down))
    assert c.status == "NETWORK" and c.listed is None


# --------------------------------------------------------------------------- Anthropic


def _anthropic(message_response):
    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [
                {"id": "claude-haiku-4-5-20251001", "type": "model", "display_name": "Haiku",
                 "created_at": "2025-10-01T00:00:00Z"}], "has_more": False, "first_id": None, "last_id": None})
        return message_response
    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_anthropic_without_credits() -> None:
    low = httpx2.Response(400, json={"type": "error", "error": {
        "type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API."}})
    c = check_anthropic(_settings(), _anthropic(low))
    assert (c.status, c.listed, c.callable) == ("NO_CREDITS", True, False)


def test_anthropic_ok() -> None:
    ok = httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
                                    "content": [{"type": "text", "text": "ok"}], "stop_reason": "max_tokens",
                                    "usage": {"input_tokens": 9, "output_tokens": 1}})
    assert check_anthropic(_settings(), _anthropic(ok)).status == "OK"


# --------------------------------------------------------------------------- Gemini


class FakeGemini:
    def __init__(self, listing, outcome):
        self.listing, self.outcome, self.calls = listing, outcome, []
        self.models = self

    def list(self):
        if isinstance(self.listing, Exception):
            raise self.listing
        return [types.Model(name=f"models/{n}", supported_actions=["generateContent"]) for n in self.listing]

    def generate_content(self, model, contents, config):
        self.calls.append((model, config.max_output_tokens))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _gemini_error(code, status, message):
    return errors.APIError(code, {"error": {"code": code, "status": status, "message": message}})


def test_gemini_ok_and_unavailable_model() -> None:
    fake = FakeGemini(["gemini-3.8-flash", "gemini-3.8-pro"], types.GenerateContentResponse())
    assert check_gemini(_settings(), fake).status == "OK" and fake.calls == [("gemini-3.8-flash", 5)]
    fake = FakeGemini(["gemini-3.5-flash"], _gemini_error(404, "NOT_FOUND", "models/gemini-3.8-flash is not found"))
    c = check_gemini(_settings(), fake)
    assert c.status == "MODEL_NOT_AVAILABLE" and c.closest == ["gemini-3.5-flash"]


def test_gemini_bad_key_and_quota() -> None:
    bad = FakeGemini(_gemini_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."), None)
    assert check_gemini(_settings(), bad).status == "INVALID_KEY"
    quota = FakeGemini(["gemini-3.8-flash"], _gemini_error(429, "RESOURCE_EXHAUSTED", "You exceeded your quota"))
    assert check_gemini(_settings(), quota).status == "QUOTA_EXHAUSTED"


# --------------------------------------------------------------------------- gate and CLI


def test_require_ready_refuses_and_caches_only_ok(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "_ok_until", {})
    results = [Check("gemini", "gemini-3.8-flash", True, False, "QUOTA_EXHAUSTED", "429"),
               Check("gemini", "gemini-3.8-flash", True, True)]
    monkeypatch.setitem(preflight.CHECKS, "gemini", lambda s: results.pop(0))
    preflight.require_ready("scripted")  # never checked
    with pytest.raises(LLMError) as e:
        preflight.require_ready("gemini", _settings(), now=lambda: 0)
    assert e.value.code == "PREFLIGHT_QUOTA_EXHAUSTED"
    preflight.require_ready("gemini", _settings(), now=lambda: 0)
    preflight.require_ready("gemini", _settings(), now=lambda: 100)  # cached: no third check
    assert results == []


def test_main_prints_a_table_without_keys(monkeypatch, capsys) -> None:
    transport, _ = _groq(models_status=401)
    monkeypatch.setattr(preflight, "get_settings", lambda: _settings(gemini_api_key="", anthropic_api_key=""))
    monkeypatch.setitem(preflight.CHECKS, "openai_compat", lambda s: check_openai_compat(s, transport))
    assert preflight.main([]) == 1
    out = capsys.readouterr().out
    assert "openai_compat | qwen/qwen3.8-27b | -      | -        | INVALID_KEY" in out
    assert KEY not in out and "gemini" not in out


def test_eval_runner_refuses_before_any_run(monkeypatch) -> None:
    import evals.run_evals as run_evals
    monkeypatch.setattr(preflight, "_ok_until", {})
    monkeypatch.setattr(preflight, "get_settings", lambda: _settings())
    monkeypatch.setitem(preflight.CHECKS, "openai_compat",
                        lambda s: Check("openai_compat", s.openai_compat_model, False, False, "MODEL_NOT_AVAILABLE"))
    with pytest.raises(SystemExit, match="PREFLIGHT_MODEL_NOT_AVAILABLE"):
        run_evals.preflight(["scripted", "openai_compat"])
