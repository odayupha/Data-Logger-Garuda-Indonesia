"""
tests/test_stability_detector.py
──────────────────────────────────
Unit tests for StabilityDetector.

All tests use injected monotonic_time so they are fully deterministic
and run in milliseconds (no real sleep calls).
"""

import pytest
from services.stability_detector import StabilityDetector
from core.models import StabilityStatus
from config import settings


# ── Helpers ────────────────────────────────────────────────────────────

def make_detector(**kwargs) -> StabilityDetector:
    """Create a detector with tight parameters for fast tests."""
    defaults = dict(
        tolerance_deg=0.05,
        min_samples=5,
        min_duration_sec=1.0,
        max_gap_sec=0.5,
        rearm_threshold_deg=0.20,
    )
    defaults.update(kwargs)
    return StabilityDetector(**defaults)


def feed_n(detector: StabilityDetector, angle: float, n: int, start_time: float, step: float):
    """
    Feed *n* identical angle samples with evenly spaced timestamps.

    Returns the last StabilityResult.
    """
    result = None
    for i in range(n):
        result = detector.update(angle, monotonic_time=start_time + i * step)
    return result


# ══════════════════════════════════════════════════════════════════════
#  Initial state
# ══════════════════════════════════════════════════════════════════════

class TestInitialState:
    def test_starts_in_waiting_for_data(self):
        d = make_detector()
        assert d.status == StabilityStatus.WAITING_FOR_DATA

    def test_stable_angle_is_none_initially(self):
        d = make_detector()
        assert d.stable_angle is None


# ══════════════════════════════════════════════════════════════════════
#  First sample → SETTLING
# ══════════════════════════════════════════════════════════════════════

class TestFirstSample:
    def test_first_sample_transitions_to_settling(self):
        d = make_detector()
        result = d.update(10.0, monotonic_time=0.0)
        assert result.status == StabilityStatus.SETTLING

    def test_first_sample_not_just_became_stable(self):
        d = make_detector()
        result = d.update(10.0, monotonic_time=0.0)
        assert result.just_became_stable is False


# ══════════════════════════════════════════════════════════════════════
#  SETTLING → STABLE happy path
# ══════════════════════════════════════════════════════════════════════

class TestSettlingToStable:
    def test_becomes_stable_after_min_samples_and_duration(self):
        d = make_detector(min_samples=5, min_duration_sec=1.0)
        # Feed 5 samples over 1.1 seconds (just enough)
        result = feed_n(d, angle=25.0, n=5, start_time=0.0, step=0.275)
        assert result.status == StabilityStatus.STABLE
        assert result.just_became_stable is True

    def test_stable_angle_is_mean_of_window(self):
        d = make_detector(min_samples=5, min_duration_sec=1.0)
        angles = [25.01, 24.99, 25.02, 24.98, 25.00]
        for i, a in enumerate(angles):
            result = d.update(a, monotonic_time=float(i) * 0.3)
        assert result.status == StabilityStatus.STABLE
        assert result.stable_angle == pytest.approx(25.0, abs=0.01)

    def test_not_stable_if_too_few_samples(self):
        d = make_detector(min_samples=10, min_duration_sec=0.0)
        result = feed_n(d, angle=25.0, n=4, start_time=0.0, step=0.5)
        assert result.status == StabilityStatus.SETTLING

    def test_not_stable_if_duration_too_short(self):
        d = make_detector(min_samples=3, min_duration_sec=5.0)
        result = feed_n(d, angle=25.0, n=10, start_time=0.0, step=0.1)
        assert result.status == StabilityStatus.SETTLING

    def test_just_became_stable_fires_only_once(self):
        d = make_detector(min_samples=5, min_duration_sec=1.0)
        fires = 0
        t = 0.0
        for i in range(20):
            result = d.update(25.0, monotonic_time=t)
            if result.just_became_stable:
                fires += 1
            t += 0.3
        assert fires == 1


# ══════════════════════════════════════════════════════════════════════
#  SETTLING → MOVING on deviation
# ══════════════════════════════════════════════════════════════════════

class TestDeviationReset:
    def test_large_deviation_transitions_to_moving(self):
        d = make_detector(tolerance_deg=0.05, min_samples=10)
        d.update(10.0, monotonic_time=0.0)
        d.update(10.02, monotonic_time=0.1)
        result = d.update(10.20, monotonic_time=0.2)   # spread = 0.20 > 0.05
        assert result.status == StabilityStatus.MOVING

    def test_moving_resets_window(self):
        d = make_detector(tolerance_deg=0.05)
        d.update(10.0, monotonic_time=0.0)
        d.update(10.02, monotonic_time=0.1)
        d.update(10.20, monotonic_time=0.2)
        assert d.stable_angle is None


# ══════════════════════════════════════════════════════════════════════
#  STABLE → MOVING on re-arm
# ══════════════════════════════════════════════════════════════════════

class TestReArm:
    def _stabilize(self, d: StabilityDetector, angle: float = 25.0) -> float:
        """Return the time at which STABLE was reached."""
        t = 0.0
        while d.status != StabilityStatus.STABLE:
            d.update(angle, monotonic_time=t)
            if d.status == StabilityStatus.STABLE:
                return t
            t += 0.3
        return t

    def test_movement_beyond_threshold_returns_to_moving(self):
        d = make_detector(rearm_threshold_deg=0.20)
        t = self._stabilize(d)
        result = d.update(25.0 + 0.25, monotonic_time=t + 0.3)
        assert result.status == StabilityStatus.MOVING

    def test_small_noise_does_not_rearm(self):
        d = make_detector(rearm_threshold_deg=0.20)
        t = self._stabilize(d)
        result = d.update(25.0 + 0.10, monotonic_time=t + 0.3)
        assert result.status == StabilityStatus.STABLE


# ══════════════════════════════════════════════════════════════════════
#  Sample gap reset
# ══════════════════════════════════════════════════════════════════════

class TestSampleGap:
    def test_large_gap_resets_window(self):
        d = make_detector(max_gap_sec=0.5, min_samples=10)
        d.update(25.0, monotonic_time=0.0)
        d.update(25.0, monotonic_time=0.2)
        # Gap of 2s — should reset
        result = d.update(25.0, monotonic_time=2.2)
        # After reset, it starts accumulating fresh
        assert result.sample_count == 1


# ══════════════════════════════════════════════════════════════════════
#  Non-finite inputs
# ══════════════════════════════════════════════════════════════════════

class TestNonFiniteInputs:
    def test_nan_is_ignored(self):
        import math
        d = make_detector()
        d.update(25.0, monotonic_time=0.0)
        result = d.update(math.nan, monotonic_time=0.1)
        assert result.status == StabilityStatus.SETTLING

    def test_inf_is_ignored(self):
        d = make_detector()
        result = d.update(float("inf"), monotonic_time=0.0)
        assert result.status == StabilityStatus.WAITING_FOR_DATA

    def test_neg_inf_is_ignored(self):
        d = make_detector()
        result = d.update(float("-inf"), monotonic_time=0.0)
        assert result.status == StabilityStatus.WAITING_FOR_DATA


# ══════════════════════════════════════════════════════════════════════
#  Manual reset
# ══════════════════════════════════════════════════════════════════════

class TestManualReset:
    def test_reset_clears_to_waiting(self):
        d = make_detector(min_samples=5, min_duration_sec=1.0)
        feed_n(d, 25.0, n=8, start_time=0.0, step=0.3)
        d.reset()
        assert d.status == StabilityStatus.WAITING_FOR_DATA
        assert d.stable_angle is None


# ══════════════════════════════════════════════════════════════════════
#  Creep Rejection & Quiescent Precision
# ══════════════════════════════════════════════════════════════════════

class TestCreepRejection:
    def test_creeping_angle_does_not_trigger_stable(self):
        """Verify that monotonic creep (e.g. 0.04°/s) remains in SETTLING."""
        d = make_detector(tolerance_deg=0.06, min_samples=5, min_duration_sec=1.0, max_drift_deg=0.015)
        # Drift across window: 10.00 -> 10.04 (drift between halves > 0.015)
        angles = [10.00, 10.01, 10.02, 10.03, 10.04]
        for i, a in enumerate(angles):
            res = d.update(a, monotonic_time=float(i) * 0.25)
        assert res.status == StabilityStatus.SETTLING

    def test_quiescent_angle_matches_true_resting_position(self):
        """Verify that when sensor stops, the stable angle reflects the resting position."""
        d = make_detector(tolerance_deg=0.04, min_samples=5, min_duration_sec=1.0, max_drift_deg=0.015)
        # Flap settles into exactly 15.00°
        angles = [14.99, 15.00, 15.00, 15.00, 15.00, 15.00]
        for i, a in enumerate(angles):
            res = d.update(a, monotonic_time=float(i) * 0.25)
        assert res.status == StabilityStatus.STABLE
        assert res.stable_angle == 15.00

