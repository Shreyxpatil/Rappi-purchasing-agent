"""Free-tier survival: a client-side pacer plus bounded exponential backoff with jitter.

Waiting is always bounded: a call gives up when its retries are spent, when the total time it has spent
waiting would exceed `max_total_s`, or immediately when the provider says the wait is longer than that
(or the quota is daily). Every wait is reported through `on_wait` so the run's trace can show it.
Both take injectable `sleep`/`now`/`rng` so tests run instantly and deterministically.
"""

import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")
log = logging.getLogger("app.llm")
WaitHook = Callable[[dict[str, Any]], None]


class Pacer:
    """Keeps requests at or below `max_rpm` by spacing them at least 60/max_rpm seconds apart."""

    def __init__(self, max_rpm: int, *, now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.interval = 60.0 / max_rpm if max_rpm > 0 else 0.0
        self.now, self.sleep = now, sleep
        self._next = 0.0

    def wait(self, on_wait: WaitHook | None = None) -> float:
        """Block until the next request may go out; returns the seconds waited."""
        t = self.now()
        delay = max(0.0, self._next - t)
        if delay:
            log.info("pacer: waiting %.1f s to stay under the request rate limit", delay)
            if on_wait and delay >= 1:
                on_wait({"reason": "pacing", "delay_s": round(delay, 1),
                         "message": "spacing requests to stay under LLM_MAX_RPM"})
            self.sleep(delay)
        self._next = max(t, self._next) + self.interval
        return delay


class RetryableError(Exception):
    """A provider call that may succeed later. `kind` is rate_limit | network | server; `retry_after` is the
    server's hint in seconds; `daily` marks a quota that will not reset within any reasonable wait."""

    def __init__(self, message: str, retry_after: float | None = None, *, kind: str = "server",
                 daily: bool = False) -> None:
        super().__init__(message)
        self.retry_after, self.kind, self.daily = retry_after, kind, daily


class GiveUp(Exception):
    """Waiting longer is pointless or not allowed. `reason`: retries | budget | daily | retry_after."""

    def __init__(self, cause: RetryableError, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.cause, self.reason = cause, reason


def with_backoff(fn: Callable[[], T], *, max_retries: int = 5, base: float = 2.0, cap: float = 60.0,
                 max_total_s: float | None = None, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.monotonic, rng: random.Random | None = None,
                 on_wait: WaitHook | None = None) -> T:
    """Call `fn`; on RetryableError wait base^attempt seconds (capped, full jitter), or the server's
    retry-after hint if larger, and try again. Raises GiveUp instead of waiting past the limits."""
    rng = rng or random.Random()
    start = now()
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except RetryableError as e:
            if e.daily:
                raise GiveUp(e, "daily", f"daily quota exhausted: {e}") from e
            if attempt == max_retries:
                log.warning("giving up after %d retries: %s", max_retries, e)
                raise GiveUp(e, "retries", f"gave up after {max_retries} retries: {e}") from e
            delay = rng.uniform(0, min(cap, base ** (attempt + 1)))
            if e.retry_after is not None:
                delay = max(delay, e.retry_after)
            if max_total_s is not None:
                left = max_total_s - (now() - start)
                if e.retry_after is not None and e.retry_after > left:
                    raise GiveUp(e, "retry_after", f"provider asks to wait {e.retry_after:.0f} s, "
                                                   f"more than the {left:.0f} s left for this call: {e}") from e
                if delay > left:
                    raise GiveUp(e, "budget", f"waiting {delay:.0f} s would exceed the {max_total_s:.0f} s "
                                              f"limit for one call: {e}") from e
            log.warning("retry %d/%d in %.1f s: %s", attempt + 1, max_retries, delay, e)
            if on_wait:
                on_wait({"reason": e.kind, "delay_s": round(delay, 1), "attempt": attempt + 1,
                         "max_retries": max_retries, "message": str(e)[:300]})
            sleep(delay)
    raise AssertionError("unreachable")


def give_up_code(g: GiveUp) -> str:
    """Map a give-up to the run's failure code."""
    if g.cause.kind == "rate_limit":
        return "LLM_QUOTA_EXHAUSTED"
    if g.reason == "budget":
        return "LLM_TIMEOUT"
    return "NETWORK" if g.cause.kind == "network" else "LLM_UNAVAILABLE"
