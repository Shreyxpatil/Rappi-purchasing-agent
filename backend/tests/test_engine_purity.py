"""The engine must stay pure: no database, no LLM, no clock, no I/O (design principle 1)."""

import ast
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "app" / "engine"
ALLOWED = {"math", "pydantic", "app.engine"}


def test_engine_imports_only_math_pydantic_and_itself() -> None:
    offenders = []
    for path in ENGINE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if not any(name == a or name.startswith(a + ".") for a in ALLOWED):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []
