"""
services/stability_detector.py
────────────────────────────────
Detects when an angle sensor has settled to a stable position.

Hybrid Stability Detection Architecture
───────────────────────────────────────
1. Motion Detection:
   Continuous tracking of delta and speed between samples.
   Distinguishes true mechanical movement from synchro electrical jitter.

2. Settling Phase:
   Sliding window of valid timestamped samples.
   Outlier-tolerant trimmed spread filtering prevents single noise spikes
   from invalidating settling progress.

3. Stability Confirmation:
   Requires minimum sample count (TARGET_SAMPLE_COUNT), minimum wall-clock
   duration (TARGET_STABLE_DURATION_SEC), low spread, and low drift.
   Computes representative stable angle via median (zero spike bias).

4. Hysteresis & Holding:
   Separate entry tolerance (strict precision) and exit threshold
   (rearm_threshold_deg) to prevent oscillating states around noisy boundaries.

5. Prolonged Instability:
   Flags UNSTABLE status with diagnostic reason if erratic oscillation persists.

No GUI or I/O dependencies.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Deque, Optional

from config import settings
from core.models import StabilityResult, StabilityStatus

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
#  Circular Angle Arithmetic Helpers (0° – 360° Discontinuity Safe)
# ══════════════════════════════════════════════════════════════════════

def angular_diff(a: float, b: float) -> float:
    """
    Signed difference (a - b) wrapped to [-180, +180).
    Guarantees seamless transitions across 0° / 360° boundary.
    """
    return (float(a) - float(b) + 180.0) % 360.0 - 180.0


def angular_dist(a: float, b: float) -> float:
    """Shortest absolute distance between two angles in degrees [0, 180]."""
    return abs(angular_diff(a, b))


def unwrap_window(angles: list[float], ref: float) -> list[float]:
    """
    Unwrap all angles relative to reference *ref* to make window samples continuous.
    Eliminates artificial 359° spreads when jittering across 0° / 360°.
    """
    return [ref + angular_diff(a, ref) for a in angles]


class StabilityDetector:
    """
    Feed angle values one at a time via :meth:`update`.

    Parameters
    ----------
    tolerance_deg :
        Max allowable spread across the stable window.
    min_samples :
        Minimum number of samples that must be in the window.
    min_duration_sec :
        Minimum wall-clock seconds the window must span.
    max_gap_sec :
        If this many seconds pass between samples the window is reset.
    rearm_threshold_deg :
        How far the angle must move from the stable reference before
        the state transitions back to MOVING (prevents noise re-arm).
    movement_threshold_deg :
        Sample-to-sample delta indicating active mechanical motion.
    max_drift_deg :
        Max allowable drift between first and second half of window.
    hysteresis_band_deg :
        Buffer to prevent oscillating state changes.
    unstable_timeout_sec :
        Duration of persistent settling oscillation before flagging UNSTABLE.
    """

    def __init__(
        self,
        tolerance_deg: float = settings.STABILITY_TOLERANCE_DEG,
        min_samples: int = settings.STABILITY_MIN_SAMPLES,
        min_duration_sec: float = settings.STABILITY_MIN_DURATION_SEC,
        max_gap_sec: float = settings.MAX_SAMPLE_GAP_SEC,
        rearm_threshold_deg: float = getattr(settings, "REARM_THRESHOLD_DEG", settings.AUTO_SAVE_REARM_THRESHOLD_DEG),
        movement_threshold_deg: float = getattr(settings, "MOVEMENT_THRESHOLD_DEG", 0.08),
        max_drift_deg: float = settings.STABILITY_MAX_DRIFT_DEG,
        hysteresis_band_deg: float = getattr(settings, "HYSTERESIS_BAND_DEG", 0.025),
        unstable_timeout_sec: float = 6.0,
    ) -> None:
        self.tolerance_deg = tolerance_deg
        self.min_samples = min_samples
        self.min_duration_sec = min_duration_sec
        self.max_gap_sec = max_gap_sec
        self.rearm_threshold_deg = rearm_threshold_deg
        self.movement_threshold_deg = movement_threshold_deg
        self.max_drift_deg = max_drift_deg
        self.hysteresis_band_deg = hysteresis_band_deg
        self.unstable_timeout_sec = unstable_timeout_sec

        self._status: StabilityStatus = StabilityStatus.WAITING_FOR_DATA
        self._window: Deque[float] = deque()          # angle values in current window
        self._window_times: Deque[float] = deque()    # monotonic timestamps per sample
        self._stable_angle: Optional[float] = None    # representative angle when STABLE
        self._last_angle: Optional[float] = None      # immediately preceding sample
        self._last_sample_time: Optional[float] = None
        self._diagnostic_reason: Optional[str] = None

    # ── Public API ────────────────────────────────────────────────────

    @property
    def status(self) -> StabilityStatus:
        return self._status

    @property
    def stable_angle(self) -> Optional[float]:
        return self._stable_angle

    @property
    def diagnostic_reason(self) -> Optional[str]:
        return self._diagnostic_reason

    def reset(self) -> None:
        """Force a full reset to WAITING_FOR_DATA (e.g., on disconnect, pause, or session start)."""
        self._status = StabilityStatus.WAITING_FOR_DATA
        self._window.clear()
        self._window_times.clear()
        self._stable_angle = None
        self._last_angle = None
        self._last_sample_time = None
        self._diagnostic_reason = None
        logger.debug("StabilityDetector: manual reset.")

    def update(
        self,
        angle: float,
        monotonic_time: Optional[float] = None,
    ) -> StabilityResult:
        """
        Process one new angle sample through the Hybrid Stability Detection pipeline.

        Parameters
        ----------
        angle :
            The raw angle reading in degrees.
        monotonic_time :
            ``time.monotonic()`` timestamp; defaults to ``time.monotonic()``
            if omitted. Injecting this value makes tests deterministic.

        Returns
        -------
        StabilityResult
            Always returned; callers should check ``just_became_stable``.
        """
        now = monotonic_time if monotonic_time is not None else time.monotonic()

        # ── 1. Reject invalid non-finite samples ──────────────────────
        if not math.isfinite(angle):
            logger.warning("StabilityDetector: non-finite angle=%s — ignored.", angle)
            return self._build_result(just_became_stable=False, reset_reason="non-finite value")

        # ── 2. Check sample gap ───────────────────────────────────────
        if self._last_sample_time is not None:
            gap = now - self._last_sample_time
            if gap > self.max_gap_sec:
                reason = f"sample gap {gap:.2f}s > {self.max_gap_sec}s"
                logger.info("StabilityDetector: %s — resetting window.", reason)
                self._reset_window(reason)
                self._status = StabilityStatus.WAITING_FOR_DATA

        prev_angle = self._last_angle
        self._last_angle = angle
        self._last_sample_time = now

        # ── 3. State-specific processing ──────────────────────────────
        prev_status = self._status

        if self._status == StabilityStatus.WAITING_FOR_DATA:
            result = self._handle_waiting(angle, now)

        elif self._status == StabilityStatus.SETTLING:
            result = self._handle_settling(angle, now, prev_angle)

        elif self._status == StabilityStatus.STABLE:
            result = self._handle_stable(angle, now)

        elif self._status == StabilityStatus.MOVING:
            result = self._handle_moving(angle, now, prev_angle)

        elif self._status == StabilityStatus.UNSTABLE:
            result = self._handle_unstable(angle, now, prev_angle)

        else:  # pragma: no cover
            result = self._build_result(just_became_stable=False)

        if prev_status != self._status:
            logger.info(
                "StabilityDetector: %s → %s  angle=%.3f",
                prev_status.name, self._status.name, angle,
            )

        return result

    # ── State Handlers ────────────────────────────────────────────────

    def _handle_waiting(self, angle: float, now: float) -> StabilityResult:
        """First sample received — initialize window and enter SETTLING."""
        self._begin_window(angle, now)
        self._status = StabilityStatus.SETTLING
        return self._build_result(just_became_stable=False)

    def _handle_settling(
        self, angle: float, now: float, prev_angle: Optional[float]
    ) -> StabilityResult:
        """
        Accumulate samples in sliding window and verify stability.

        Pipeline:
        a. Motion check: circular jump check from current median.
        b. Spread check: circular unwrapped window spread vs allowable motion bound.
        c. Buffer management: sliding window bounded in duration.
        d. Prolonged oscillation check: if settling oscillates too long -> UNSTABLE.
        e. Multi-stage stability confirmation: strict and adaptive stages.
        """
        # Step a: Check jump from current median if window has samples
        if self._window:
            ref = self._window[0]
            unwrapped_current = unwrap_window(list(self._window), ref)
            sorted_unwrapped = sorted(unwrapped_current)
            cur_med = sorted_unwrapped[len(sorted_unwrapped) // 2] % 360.0
            motion_thresh = max(self.movement_threshold_deg, self.tolerance_deg * 2.2, 0.10)
            jump = angular_dist(angle, cur_med)
            if jump > motion_thresh:
                self._status = StabilityStatus.MOVING
                self._reset_window(f"motion detected: jump {jump:.3f}° > {motion_thresh:.3f}°")
                return self._build_result(just_became_stable=False, reset_reason="deviation exceeded tolerance")

        # Step b: Peek candidate spread with circular unwrapping
        candidate = list(self._window) + [angle]
        cand_unwrapped = unwrap_window(candidate, candidate[0])
        raw_spread = max(cand_unwrapped) - min(cand_unwrapped)
        max_allowable_spread = max(self.tolerance_deg * 2.5, self.movement_threshold_deg * 1.5, 0.15)
        if raw_spread > max_allowable_spread:
            self._status = StabilityStatus.MOVING
            self._reset_window(f"raw spread {raw_spread:.3f}° exceeded {max_allowable_spread:.3f}°")
            return self._build_result(just_became_stable=False, reset_reason="deviation exceeded tolerance")

        # Step c: Add to sliding window
        self._window.append(angle)
        self._window_times.append(now)

        # Bound sliding window to ~3.5 seconds
        while len(self._window) > 100 or (self._window_times[-1] - self._window_times[0]) > 3.5:
            self._window.popleft()
            self._window_times.popleft()

        duration = now - self._window_times[0]
        n_samples = len(self._window)

        # Step d: Prolonged instability check
        if duration >= self.unstable_timeout_sec and raw_spread > self.tolerance_deg:
            self._status = StabilityStatus.UNSTABLE
            self._diagnostic_reason = (
                f"Prolonged oscillation: spread {raw_spread:.3f}° > tolerance {self.tolerance_deg:.3f}° "
                f"for {duration:.1f}s"
            )
            logger.warning("StabilityDetector: %s", self._diagnostic_reason)
            return self._build_result(just_became_stable=False, reset_reason=self._diagnostic_reason)

        if n_samples < self.min_samples or duration < self.min_duration_sec:
            return self._build_result(just_became_stable=False)

        # Step e: Compute trimmed spread (outlier-tolerant) using unwrapped window
        unwrapped_window = unwrap_window(list(self._window), self._window[0])
        sorted_window = sorted(unwrapped_window)
        if n_samples >= 6:
            trimmed = sorted_window[1:-1]
            trimmed_spread = trimmed[-1] - trimmed[0]
        else:
            trimmed_spread = raw_spread

        # Drift between first half and second half
        mid = n_samples // 2
        first_half = unwrapped_window[:mid]
        second_half = unwrapped_window[mid:]
        drift = abs((sum(second_half) / len(second_half)) - (sum(first_half) / len(first_half)))

        # Stage 1: Strict precision (clean quiet sensor)
        stage1_stable = (trimmed_spread <= self.tolerance_deg and drift <= self.max_drift_deg)

        # Stage 2: Adaptive consensus (handles noisy aircraft synchro resolvers)
        stage2_duration = max(self.min_duration_sec * 1.5, 1.8)
        stage2_stable = (
            duration >= stage2_duration
            and n_samples >= (self.min_samples * 2)
            and drift <= (self.max_drift_deg * 1.4)
            and trimmed_spread <= (self.tolerance_deg * 1.8)
        )

        if stage1_stable or stage2_stable:
            self._status = StabilityStatus.STABLE
            # Median of window guarantees robust resting position without spike bias
            med_val = sorted_window[len(sorted_window) // 2] % 360.0
            self._stable_angle = round(med_val, 2)
            self._diagnostic_reason = None
            method = "Stage-1 Strict" if stage1_stable else "Stage-2 Adaptive Jitter Filter"
            logger.info(
                "StabilityDetector: STABLE confirmed (%s) angle=%.2f°  samples=%d  duration=%.2fs  drift=%.3f°  spread=%.3f°",
                method, self._stable_angle, n_samples, duration, drift, trimmed_spread,
            )
            return self._build_result(just_became_stable=True)

        return self._build_result(just_became_stable=False)

    def _handle_stable(self, angle: float, now: float) -> StabilityResult:
        """
        Already STABLE.
        Hysteresis rule:
        Stay STABLE while within rearm_threshold_deg (using circular distance).
        If movement exceeds rearm_threshold_deg, transition to MOVING to allow
        the next position to be detected and recorded.
        """
        if self._stable_angle is None:
            self._stable_angle = angle

        deviation = angular_dist(angle, self._stable_angle)
        if deviation > self.rearm_threshold_deg:
            logger.info(
                "StabilityDetector: movement detected  deviation=%.3f° > rearm_threshold %.3f° — re-arming.",
                deviation, self.rearm_threshold_deg,
            )
            self._status = StabilityStatus.MOVING
            self._reset_window(f"movement {deviation:.3f}°")
            return self._build_result(
                just_became_stable=False,
                reset_reason=f"movement {deviation:.3f}° exceeded re-arm threshold",
            )

        # Still stable — keep window fresh as a bounded sliding buffer
        self._window.append(angle)
        self._window_times.append(now)
        max_window = max(self.min_samples * 3, 60)
        while len(self._window) > max_window:
            self._window.popleft()
            self._window_times.popleft()

        return self._build_result(just_became_stable=False)

    def _handle_moving(
        self, angle: float, now: float, prev_angle: Optional[float]
    ) -> StabilityResult:
        """
        Sensor is in motion.
        Check if sensor has slowed down to settle into a new position (circular distance).
        """
        # If rate of change slows down, begin a fresh settling window
        step = angular_dist(angle, prev_angle) if prev_angle is not None else 0.0
        if step <= self.movement_threshold_deg or prev_angle is None:
            self._begin_window(angle, now)
            self._status = StabilityStatus.SETTLING
        return self._build_result(just_became_stable=False)

    def _handle_unstable(
        self, angle: float, now: float, prev_angle: Optional[float]
    ) -> StabilityResult:
        """
        Sensor has been oscillating without settling for too long.
        Watch for sensor either moving definitively or calming down (circular distance).
        """
        step = angular_dist(angle, prev_angle) if prev_angle is not None else 0.0
        if step > self.movement_threshold_deg * 1.5:
            self._status = StabilityStatus.MOVING
            self._reset_window("movement from unstable")
        elif step <= self.tolerance_deg:
            # Calming down — restart settling attempt
            self._begin_window(angle, now)
            self._status = StabilityStatus.SETTLING
        return self._build_result(just_became_stable=False, reset_reason=self._diagnostic_reason)

    # ── Helpers ───────────────────────────────────────────────────────

    def _begin_window(self, angle: float, now: float) -> None:
        self._window.clear()
        self._window_times.clear()
        self._window.append(angle)
        self._window_times.append(now)
        self._stable_angle = None
        self._diagnostic_reason = None

    def _reset_window(self, reason: str = "") -> None:
        self._window.clear()
        self._window_times.clear()
        self._stable_angle = None

    def _build_result(
        self,
        just_became_stable: bool,
        reset_reason: Optional[str] = None,
    ) -> StabilityResult:
        duration = (
            (self._window_times[-1] - self._window_times[0])
            if len(self._window_times) >= 2
            else 0.0
        )
        return StabilityResult(
            status=self._status,
            just_became_stable=just_became_stable,
            stable_angle=self._stable_angle if self._status == StabilityStatus.STABLE else None,
            sample_count=len(self._window),
            stable_duration_sec=duration,
            reset_reason=reset_reason or self._diagnostic_reason,
        )
