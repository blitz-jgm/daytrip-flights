"""Polite request pacing: random jitter between requests, exponential backoff, budget cap,
and a circuit breaker that stops a source for the rest of the run once it is blocked."""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

log = logging.getLogger(__name__)
T = TypeVar("T")

_BLOCKED_RE = re.compile(r"\bHTTP (403|429)\b")


class SourceBlocked(Exception):
    """The site refused us (403/429). Do not retry during this run."""


class BudgetExceeded(Exception):
    """The per-run request cap for this source is used up."""


def is_blocked(exc: BaseException) -> bool:
    if isinstance(exc, SourceBlocked):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (403, 429)
    return bool(_BLOCKED_RE.search(str(exc)))


class Pacer:
    def __init__(
        self,
        name: str,
        *,
        max_requests: int,
        delay: tuple[float, float],
        retries: int,
        backoff_base: float,
        sleep: Callable[[float], None] = time.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.name = name
        self.max_requests = max_requests
        self.delay = delay
        self.retries = retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._uniform = uniform
        self.requests = 0
        self.blocked = False

    def call(self, fn: Callable[..., T], *args, **kwargs) -> T:
        """Run one logical request (`fn`) with jitter, retries and budget accounting."""
        if self.blocked:
            raise SourceBlocked(f"{self.name}: circuit open for this run")
        for attempt in range(self.retries + 1):
            if self.requests >= self.max_requests:
                raise BudgetExceeded(f"{self.name}: {self.max_requests} requests used")
            if self.requests > 0:
                self._sleep(self._uniform(*self.delay))
            self.requests += 1
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if is_blocked(exc):
                    self.blocked = True
                    raise SourceBlocked(f"{self.name}: {exc}") from exc
                if attempt == self.retries:
                    raise
                wait = self.backoff_base * 2**attempt
                log.warning(
                    "%s: attempt %d failed (%s); retrying in %.0fs",
                    self.name,
                    attempt + 1,
                    exc,
                    wait,
                )
                self._sleep(wait)
        raise AssertionError("unreachable")
