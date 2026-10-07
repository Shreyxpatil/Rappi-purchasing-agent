"""Free-tier survival: a client-side pacer plus exponential backoff with jitter.

Both take injectable `sleep`/`now`/`rng` so tests run instantly and deterministically.
"""

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class Pacer:
    """Keeps requests at or below `max_rpm` by spacing them at least 60/max_rpm seconds apart."""

    def __init__(self, max_rpm: int, *, now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.interval = 60.0 / max_rpm if max_rpm > 0 else 0.0
        self.now, self.sleep = now, sleep
        self._next = 0.0

    def wait(self) -> float:
        """Block until the next request may go out; returns the seconds waited."""
        t = self.now()
        delay = max(0.0, self._next - t)
        if delay:
            self.sleep(delay)
        self._next = max(t, self._next) + self.interval
        return delay


class RetryableError(Exception):
    """Raised by a provider call that may succeed later (rate limit, overload). `retry_after` is a server hint."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def with_backoff(fn: Callable[[], T], *, max_retries: int = 5, base: float = 2.0, cap: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None,
                 on_retry: Callable[[int, float, RetryableError], None] | None = None) -> T:
    """Call `fn`; on RetryableError wait base^attempt seconds (capped, full jitter), or the server's
    retry-after hint if larger, and try again up to `max_retries` times."""
    rng = rng or random.Random()
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except RetryableError as e:
            if attempt == max_retries:
                raise
            delay = rng.uniform(0, min(cap, base ** (attempt + 1)))
            if e.retry_after is not None:
                delay = max(delay, e.retry_after)
            if on_retry:
                on_retry(attempt + 1, delay, e)
            sleep(delay)
    raise AssertionError("unreachable")
