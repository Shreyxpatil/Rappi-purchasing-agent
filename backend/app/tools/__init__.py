"""Typed tools the agent can call: read (DB), compute (engine), act (DB writes behind the policy gate).

Importing this package registers every tool in REGISTRY.
"""

from app.tools import compute, read  # noqa: F401  (registration side effect)
from app.tools.registry import REGISTRY, ToolContext, ToolResult, call_tool

__all__ = ["REGISTRY", "ToolContext", "ToolResult", "call_tool"]
