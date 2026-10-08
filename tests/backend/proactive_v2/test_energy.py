"""Energy decay and cadence regression tests for the Shiori migration."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from proactive_v2.energy import compute_energy, d_energy, d_recent, next_tick_from_score

NOW = datetime(2026, 10, 8, 4, tzinfo=timezone.utc)


def test_multiscale_energy_recharges_and_decays():
    energies = [
        compute_energy(NOW - timedelta(minutes=m), NOW)
        for m in (0, 30, 60, 240, 1440, 4320)
    ]
    assert energies[0] == 1
    assert all(a > b for a, b in zip(energies, energies[1:]))
    assert energies[2] < 0.5
    assert energies[-2] < 0.2
    assert 0 <= energies[-1] < 0.05
    assert compute_energy(None, NOW) == 0


def test_energy_handles_future_and_legacy_naive_timestamps():
    assert compute_energy(NOW + timedelta(minutes=1), NOW) == 1
    assert compute_energy(NOW.replace(tzinfo=None), NOW) == 1
    assert compute_energy(NOW, NOW.replace(tzinfo=None)) == 1
    assert (
        compute_energy(
            NOW - timedelta(minutes=30), NOW, tau1_min=1, tau2_min=2, tau3_min=5
        )
        < 0.01
    )


@pytest.mark.parametrize("energy,expected", [(1, 0), (0.2, 0.8), (-1, 1), (2, 0)])
def test_interaction_desire_is_bounded(energy, expected):
    assert d_energy(energy) == expected


def test_recent_density_is_logarithmic_and_bounded():
    assert d_recent(-1) == 0
    assert 0 < d_recent(5) < d_recent(9) < 1
    assert d_recent(10) == 1


def test_cadence_threshold_and_jitter():
    cadence = {"tick_s0": 480, "tick_s1": 240, "tick_jitter": 0.2}
    assert next_tick_from_score(0.2, **{**cadence, "tick_jitter": 0}) == 480
    assert next_tick_from_score(0.21, **{**cadence, "tick_jitter": 0}) == 240
    rng = random.Random(7)
    intervals = [next_tick_from_score(0.3, rng=rng, **cadence) for _ in range(50)]
    assert all(192 <= interval <= 288 for interval in intervals)
    assert len(set(intervals)) > 1
