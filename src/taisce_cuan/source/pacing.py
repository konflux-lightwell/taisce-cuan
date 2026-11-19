"""Pace outbound GitLab requests so a run stays under a per-minute quota."""

from __future__ import annotations

import math
import time

import httpx


class RequestPacer:
    """Space request starts so a client never exceeds a per-minute rate.

    Use an instance as an httpx ``request`` event hook. Each call reserves the
    next start slot and sleeps only until that slot opens. The first request
    never waits.
    """

    def __init__(self, max_per_minute: int) -> None:
        """Create a pacer that allows one request start per interval.

        Args:
            max_per_minute: Maximum request starts per minute; must be positive.
        """
        self._interval = 60 / max_per_minute
        self._next_allowed = -math.inf

    def __call__(self, request: httpx.Request) -> None:
        """Wait for this request's start slot before httpx sends it.

        Args:
            request: Outgoing request. Pacing does not inspect its contents.
        """
        now = time.monotonic()
        if now < self._next_allowed:
            time.sleep(self._next_allowed - now)
        # 🧭 Use the later of the two so a late call does not bank extra starts.
        self._next_allowed = max(now, self._next_allowed) + self._interval
