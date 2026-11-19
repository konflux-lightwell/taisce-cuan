"""Verify the request pacer keeps GitLab calls under a per-minute rate."""

import httpx
import pytest


@pytest.mark.parametrize("rate", [60, 120])
def test_pacer_sleeps_only_until_the_next_request_slot(fake_clock, rate):
    """Space back-to-back starts by one interval and never wait after a gap."""
    # Arrange
    from taisce_cuan.source.pacing import RequestPacer

    pacer = RequestPacer(rate)
    interval = 60 / rate
    request = httpx.Request("GET", "https://example.com/api/v4/projects")

    # Act
    for _ in range(4):
        pacer(request)
    burst_sleeps = list(fake_clock.sleeps)
    fake_clock.sleeps.clear()
    fake_clock.advance(2 * interval)
    pacer(request)

    # Assert
    assert burst_sleeps == pytest.approx([interval] * 3)  # First start is free.
    assert fake_clock.sleeps == []
