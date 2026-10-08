"""Tool registry: one place that validates arguments, runs the tool, and turns every failure
into a structured, machine-readable error the agent can react to."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from app.clock import Clock
from app.engine.types import EngineInputError
from app.policy import Policy

ToolKind = Literal["read", "compute", "act"]
log = logging.getLogger("app.tools")


class ToolError(Exception):
    """Expected, business-level failure: returned to the agent as {code, message, details}."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


class Args(BaseModel):
    """Base for tool inputs: unknown arguments are an error, not silently ignored."""

    model_config = ConfigDict(extra="forbid")


class RunState(BaseModel):
    """What the tools need to know about the run so far. Persisted with the run (agent_runs.context)."""

    trigger: dict[str, Any] = {}
    demand_basis: str = "forecast"
    excluded_suppliers: list[str] = []
    excluded_option_ids: list[str] = []
    refused_overrides: list[str] = []
    approved_overrides: list[str] = []
    options: dict[str, Any] | None = None  # last full OptionSet (engine output)
    decision: dict[str, Any] | None = None  # the decision act tools are bound to
    validation_failures: int = 0
    replans: int = 0
    escalated: bool = False


@dataclass
class ToolContext:
    session: Session
    clock: Clock
    policy: Policy
    run_id: int | None = None
    state: RunState = field(default_factory=RunState)


@dataclass(frozen=True)
class Tool:
    name: str
    kind: ToolKind
    description: str
    args_model: type[Args]
    fn: Callable[[ToolContext, Any], Any]


class ToolResult(BaseModel):
    ok: bool
    tool: str
    output: Any = None
    error: dict[str, Any] | None = None
    latency_ms: int = 0


REGISTRY: dict[str, Tool] = {}


def tool(name: str, kind: ToolKind, description: str):
    """Register a function `fn(ctx, args)` whose second parameter is annotated with its Args model."""

    def deco(fn):
        args_model = fn.__annotations__["args"]
        REGISTRY[name] = Tool(name=name, kind=kind, description=description, args_model=args_model, fn=fn)
        return fn

    return deco


def call_tool(name: str, raw_args: dict[str, Any] | None, ctx: ToolContext) -> ToolResult:
    start = time.perf_counter()

    def done(**kw) -> ToolResult:
        return ToolResult(tool=name, latency_ms=int((time.perf_counter() - start) * 1000), **kw)

    t = REGISTRY.get(name)
    if t is None:
        return done(ok=False, error={"code": "UNKNOWN_TOOL", "message": f"no tool named {name!r}",
                                     "details": {"available": sorted(REGISTRY)}})
    try:
        args = t.args_model.model_validate(raw_args or {})
    except ValidationError as e:
        return done(ok=False, error={"code": "INVALID_ARGUMENTS", "message": "arguments do not match the schema",
                                     "details": {"errors": _compact_errors(e)}})
    try:
        out = t.fn(ctx, args)
    except ToolError as e:
        return done(ok=False, error={"code": e.code, "message": e.message, "details": e.details})
    except EngineInputError as e:
        return done(ok=False, error={"code": e.code, "message": str(e), "details": {}})
    except Exception as e:  # a bug in a tool must reach the model as an error, never crash the run
        log.exception("tool %s raised", name)
        return done(ok=False, error={"code": "INTERNAL_ERROR", "message": f"{type(e).__name__}: {e}"[:300],
                                     "details": {}})
    return done(ok=True, output=out.model_dump(mode="json") if isinstance(out, BaseModel) else out)


def tool_schema(t: Tool) -> dict[str, Any]:
    """Compact JSON schema for function-calling APIs (titles stripped to save tokens)."""
    return {"name": t.name, "description": t.description, "parameters": schema_for(t.args_model)}


def schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return _strip(model.model_json_schema())


def _strip(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip(v) for v in schema]
    return schema


def _compact_errors(e: ValidationError) -> list[dict[str, Any]]:
    return [{"loc": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]} for err in e.errors()]
