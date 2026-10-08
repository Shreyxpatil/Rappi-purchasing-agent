import logging

import httpx
from google.genai import types

from app.llm.base import Message
from app.logs import RUN_ID, _RunIdFilter
from test_llm_gemini import _client, _response


def test_every_llm_call_is_logged_with_its_run_id(run_case, caplog) -> None:
    caplog.set_level(logging.INFO)
    _, run, _, _ = run_case("s1_overstock")
    calls = [r for r in caplog.records if r.name == "app.agent" and r.getMessage().startswith("llm done")]
    assert len(calls) == sum(1 for st in run.steps if st.kind == "llm")
    assert "provider=scripted" in calls[0].getMessage() and "latency_ms=" in calls[0].getMessage()
    record = calls[0]
    _RunIdFilter().filter(record)
    assert record.run_id == run.id


def test_retries_and_waits_are_logged_with_reason_and_delay(caplog) -> None:
    caplog.set_level(logging.INFO)
    RUN_ID.set(7)
    client, _, _ = _client([httpx.ConnectError("Temporary failure in name resolution"), _response(types.Part(text="ok"))])
    client.complete([Message(role="user", content="x")], [])
    retry = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert retry.getMessage().startswith("retry 1/5 in ") and "name resolution" in retry.getMessage()


def test_no_key_appears_in_logs(run_case, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    GeminiKey = "AIzaSy-THIS-MUST-NEVER-BE-LOGGED"
    from app.llm.gemini import GeminiClient

    GeminiClient(GeminiKey, "gemini-3.8-flash")
    assert GeminiKey not in caplog.text
