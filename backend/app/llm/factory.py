"""Pick the LLM client from LLM_PROVIDER (or an explicit provider name)."""

from app.config import Settings, get_settings
from app.llm.base import LLMClient, LLMError
from app.llm.scripted import ScriptedClient


def make_client(provider: str | None = None, *, case_id: str | None = None, script_variant: str = "",
                settings: Settings | None = None) -> LLMClient:
    s = settings or get_settings()
    provider = provider or s.llm_provider
    if provider == "scripted":
        if not case_id:
            raise LLMError("CONFIG", "the scripted provider needs a case id")
        return ScriptedClient.for_case(case_id, script_variant)
    if provider == "gemini":
        from app.llm.gemini import GeminiClient

        return GeminiClient(s.gemini_api_key, s.gemini_model, thinking_level=s.gemini_thinking_level,
                            max_rpm=s.llm_max_rpm, max_call_s=s.llm_max_call_seconds)
    if provider == "openai_compat":
        from app.llm.openai_compat import OpenAICompatibleClient

        return OpenAICompatibleClient(s.openai_compat_base_url, s.openai_compat_api_key, s.openai_compat_model,
                                      max_rpm=s.llm_max_rpm, max_call_s=s.llm_max_call_seconds)
    raise LLMError("CONFIG", f"unknown LLM provider {provider!r}")
