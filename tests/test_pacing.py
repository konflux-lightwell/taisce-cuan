"""Verify the request pacer keeps GitLab calls under a per-minute rate."""

import httpx
import pytest


def test_pacer_sleeps_only_until_the_next_request_slot(fake_clock):
    """Space back-to-back starts by one interval and never bank extra starts."""
    # Arrange
    from taisce_cuan.source.pacing import RequestPacer

    pacer = RequestPacer(60)
    interval = 1.0
    request = httpx.Request("GET", "https://example.com/api/v4/projects")

    # Act
    for _ in range(4):
        pacer(request)
    burst_sleeps = list(fake_clock.sleeps)
    fake_clock.sleeps.clear()
    fake_clock.advance(2 * interval)
    pacer(request)
    gap_sleeps = list(fake_clock.sleeps)
    fake_clock.sleeps.clear()
    pacer(request)

    # Assert
    assert burst_sleeps == pytest.approx([interval] * 3)  # 🆓 First start is free.
    assert gap_sleeps == []  # 🕒 A call after a gap starts without waiting.
    assert fake_clock.sleeps == pytest.approx([interval])  # 🚫 No banked starts.
