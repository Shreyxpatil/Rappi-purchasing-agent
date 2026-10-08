"""Provider preflight: a key with credits is not enough, the configured model must be available.

For every provider whose key is set: list the provider's models, check the configured model is listed, then
make ONE minimal request (5 output tokens) with that exact model. Real runs call `require_ready` first and
refuse to start unless the check is OK, so a run never begins on a provider that cannot finish it.

    uv run python -m app.llm.preflight                 # every provider with a key
    uv run python -m app.llm.preflight gemini          # one provider

Keys are never printed: errors are reduced to a status code and a short provider message.
"""

import argparse
import difflib
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.llm.base import LLMError

PROVIDERS = ("gemini", "openai_compat", "anthropic")
PING = "Reply with: ok"
MAX_OUTPUT_TOKENS = 5
TIMEOUT_S = 30.0
OK_TTL_S = 600.0  # an OK result is reused for 10 minutes, so starting runs does not spend quota on checks


@dataclass
class Check:
    provider: str
    model: str
    listed: bool | None = None  # None: not checked (an earlier step failed)
    callable: bool | None = None
    status: str = "OK"  # OK | INVALID_KEY | MODEL_NOT_AVAILABLE | NO_CREDITS | QUOTA_EXHAUSTED | NETWORK | ...
    detail: str = ""
    closest: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "OK"


class ProviderError(Exception):
    """A failed preflight request, already classified."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def classify(code: int | None, text: str) -> str:
    """HTTP status + provider message -> preflight status. Order matters: credit and key messages come
    with generic 400s on some providers."""
    t = text.lower()
    if code == 402 or "credit balance" in t or "insufficient" in t or ("billing" in t and code != 429):
        return "NO_CREDITS"
    if code in (401, 403) or "api key not valid" in t or "api_key_invalid" in t or "invalid api key" in t:
        return "INVALID_KEY"
    if code == 404 or "model_not_found" in t or "decommissioned" in t or "does not exist" in t:
        return "MODEL_NOT_AVAILABLE"
    if code == 429:
        return "QUOTA_EXHAUSTED"
    return f"HTTP_{code}" if code else "PROVIDER_ERROR"


def is_listed(model: str, ids: list[str]) -> bool:
    """Exact id, or an alias of a dated snapshot (claude-haiku-4-5 -> claude-haiku-4-5-20251001)."""
    return model in ids or any(re.fullmatch(re.escape(model) + r"-\d{8}", i) for i in ids)


def closest(model: str, ids: list[str], n: int = 3) -> list[str]:
    return difflib.get_close_matches(model, ids, n=n, cutoff=0.4)


def run_check(provider: str, model: str, list_models: Callable[[], list[str]], ping: Callable[[], None]) -> Check:
    """List, then ping. A failed listing (bad key, no network) stops the check; a model missing from the list
    is still pinged, so `callable` reports what actually happened."""
    c = Check(provider, model)
    if not model:
        c.status, c.detail = "NOT_CONFIGURED", "model name is not set"
        return c
    try:
        ids = list_models()
    except ProviderError as e:
        c.status, c.detail = e.status, e.detail
        return c
    c.listed = is_listed(model, ids)
    try:
        ping()
        c.callable = True
    except ProviderError as e:
        c.callable = False
        c.status, c.detail = e.status, e.detail
    if not c.listed:  # the listing worked, so the key and network are fine: the model is the problem
        call = "callable anyway" if c.callable else f"call failed: {c.detail}"
        c.status, c.detail = "MODEL_NOT_AVAILABLE", f"not among the {len(ids)} listed models ({call})"
        c.closest = closest(model, ids)
    return c


# --------------------------------------------------------------------------- providers


def check_gemini(s: Settings, client: Any = None) -> Check:
    from google import genai
    from google.genai import errors, types

    client = client or genai.Client(api_key=s.gemini_api_key,
                                    http_options=types.HttpOptions(timeout=int(TIMEOUT_S * 1000)))

    def guard(fn):
        try:
            return fn()
        except errors.APIError as e:
            raise ProviderError(classify(e.code, f"{e.status} {e.message}"), _short(f"{e.code} {e.status}: {e.message}")) from e
        except (httpx.TransportError, OSError) as e:
            raise ProviderError("NETWORK", _short(f"{type(e).__name__}: {e}")) from e

    def list_models() -> list[str]:
        return guard(lambda: [m.name.removeprefix("models/") for m in client.models.list()
                              if "generateContent" in (m.supported_actions or ["generateContent"])])

    def ping() -> None:
        config = types.GenerateContentConfig(
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_level=s.gemini_thinking_level.upper()),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        guard(lambda: client.models.generate_content(model=s.gemini_model, contents=PING, config=config))

    return run_check("gemini", s.gemini_model, list_models, ping)


def check_openai_compat(s: Settings, transport: httpx.BaseTransport | None = None) -> Check:
    http = httpx.Client(base_url=s.openai_compat_base_url.rstrip("/"), timeout=TIMEOUT_S, transport=transport,
                        headers={"Authorization": f"Bearer {s.openai_compat_api_key}"})

    def request(method: str, path: str, **kw) -> dict[str, Any]:
        try:
            r = http.request(method, path, **kw)
        except httpx.TransportError as e:
            raise ProviderError("NETWORK", _short(f"{type(e).__name__}: {e}")) from e
        if r.status_code >= 400:
            raise ProviderError(classify(r.status_code, r.text), _short(f"HTTP {r.status_code}: {r.text}"))
        try:
            return r.json()
        except ValueError as e:
            raise ProviderError("PROVIDER_ERROR", _short(f"non-JSON response: {r.text}")) from e

    def list_models() -> list[str]:
        return [m["id"] for m in request("GET", "/models").get("data", [])]

    def ping() -> None:
        request("POST", "/chat/completions", json={"model": s.openai_compat_model, "max_tokens": MAX_OUTPUT_TOKENS,
                                                   "messages": [{"role": "user", "content": PING}]})

    with http:
        return run_check("openai_compat", s.openai_compat_model, list_models, ping)


def check_anthropic(s: Settings, http_client: Any = None) -> Check:
    import anthropic

    client = anthropic.Anthropic(api_key=s.anthropic_api_key, http_client=http_client, max_retries=0,
                                 timeout=TIMEOUT_S)

    def guard(fn):
        try:
            return fn()
        except anthropic.APIStatusError as e:
            raise ProviderError(classify(e.status_code, e.message), _short(f"HTTP {e.status_code}: {e.message}")) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("NETWORK", _short(f"{type(e).__name__}: {e}")) from e

    def list_models() -> list[str]:
        return guard(lambda: [m.id for m in client.models.list(limit=1000)])

    def ping() -> None:
        guard(lambda: client.messages.create(model=s.anthropic_model, max_tokens=MAX_OUTPUT_TOKENS,
                                             messages=[{"role": "user", "content": PING}]))

    return run_check("anthropic", s.anthropic_model, list_models, ping)


CHECKS = {"gemini": check_gemini, "openai_compat": check_openai_compat, "anthropic": check_anthropic}
KEYS = {"gemini": "gemini_api_key", "openai_compat": "openai_compat_api_key", "anthropic": "anthropic_api_key"}


def configured(s: Settings) -> list[str]:
    return [p for p in PROVIDERS if getattr(s, KEYS[p])]


def check(provider: str, s: Settings | None = None) -> Check:
    s = s or get_settings()
    if not getattr(s, KEYS[provider]):
        return Check(provider, "", status="NOT_CONFIGURED", detail=f"{KEYS[provider].upper()} is not set")
    return CHECKS[provider](s)


_ok_until: dict[str, float] = {}


def require_ready(provider: str, s: Settings | None = None, *, now: Callable[[], float] = time.monotonic) -> None:
    """Refuse a real run unless the provider's preflight is OK. OK results are cached for OK_TTL_S."""
    if provider == "scripted" or _ok_until.get(provider, 0) > now():
        return
    c = check(provider, s)
    if not c.ok:
        hint = f"; closest available: {', '.join(c.closest)}" if c.closest else ""
        raise LLMError(f"PREFLIGHT_{c.status}", f"{provider} {c.model or ''}: {c.detail}{hint}".strip())
    _ok_until[provider] = now() + OK_TTL_S


def _short(text: str, n: int = 160) -> str:
    """One line, bounded, and never a key: providers sometimes echo the request, so mask key-like tokens."""
    text = re.sub(r"(AIza[\w-]{10,}|gsk_\w{10,}|sk-[\w-]{10,})", "***", " ".join(str(text).split()))
    return text[:n]


def table(checks: list[Check]) -> str:
    rows = [("provider", "model", "listed", "callable", "status")]
    mark = {True: "yes", False: "no", None: "-"}
    rows += [(c.provider, c.model or "-", mark[c.listed], mark[c.callable], c.status) for c in checks]
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    lines = [" | ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in rows]
    lines.insert(1, "-+-".join("-" * w for w in widths))
    for c in checks:
        if not c.ok:
            lines.append(f"{c.provider}: {c.detail}")
            if c.closest:
                lines.append(f"{c.provider}: closest available models: {', '.join(c.closest)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("providers", nargs="*", help=f"any of {', '.join(PROVIDERS)}; default: every provider with a key")
    args = p.parse_args(argv)
    if unknown := set(args.providers) - set(PROVIDERS):
        p.error(f"unknown provider(s): {', '.join(sorted(unknown))}")
    s = get_settings()
    providers = args.providers or configured(s)
    if not providers:
        print("no provider key is set (scripted mode needs none)")
        return 0
    checks = [check(prov, s) for prov in providers]
    print(table(checks))
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
