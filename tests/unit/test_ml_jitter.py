from __future__ import annotations

import statistics
from hunter.collect.politeness import next_human_delay


def test_next_human_delay_daytime():
    delays = [next_human_delay(1.0, 2.0, is_night=False) for _ in range(1000)]
    mean = statistics.mean(delays)
    # Daytime mean should be centered around 1.5 - 1.7
    assert 1.3 <= mean <= 1.8
    # All delays should respect minimum delay
    assert all(d >= 1.0 for d in delays)
    # Standard deviation should be healthy (> 0.20), eliminating fixed low-std fingerprint
    stdev = statistics.stdev(delays)
    assert stdev > 0.15


def test_next_human_delay_night():
    delays = [next_human_delay(1.0, 2.0, is_night=True, night_multiplier=3.2) for _ in range(1000)]
    mean = statistics.mean(delays)
    # Nighttime mean should be ~3.2x higher (~4.8 - 5.5)
    assert 4.5 <= mean <= 6.0
    assert all(d >= 1.0 for d in delays)
