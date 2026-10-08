"""Logging setup. Every record carries the current run id (a context variable set by the agent), so a
retry or wait deep inside an LLM client is traceable to the run it blocked. Keys are never logged."""

import logging
from contextvars import ContextVar

RUN_ID: ContextVar[int | None] = ContextVar("run_id", default=None)


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = RUN_ID.get() if RUN_ID.get() is not None else "-"
        return True


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_rappi", False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler._rappi = True  # type: ignore[attr-defined]
    handler.addFilter(_RunIdFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s run=%(run_id)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines would repeat every request URL
