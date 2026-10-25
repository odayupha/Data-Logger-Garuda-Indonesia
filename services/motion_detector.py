"""
services/motion_detector.py
───────────────────────────
Motion-Gated Stop Trigger for Flap Sensor Data Logger (NAI API 8810A).

Replaces the unstable resting-angle trigger with an inferred mechanical
movement cycle detector based strictly on measured angle changes.

States:
  • WAITING_FOR_MOTION       : Initial/armed state. Initial stationary readings
                               never trigger save. Gathers baseline and awaits
                               observable movement exceeding threshold.
  • MOVING                   : Shaft angle in motion. Auto-save strictly locked.
                               Monitors recent samples for cessation of motion.
  • STOP_CANDIDATE           : Motion appears ceased. Nonblocking dwell timer running.
                               Resumed motion cancels timer and returns to MOVING.
                               Invalid or stale readings cancel pending capture.
  • CAPTURE_PENDING          : Inferred mechanical stop confirmed by elapsed dwell.
                               Signals exactly one capture of the latest fresh reading.
  • WAITING_FOR_NEXT_MOTION  : Save succeeded. Auto-save locked at this position.
                               Rearms only when a new movement cycle is observed.
  • DATA_INVALID             : Disconnect, timeout, or non-finite readings detected.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Deque, Optional, Tuple

from config import settings
from core.models import MotionState, MotionTriggerResult
from services.stability_detector import angular_diff, angular_dist, unwrap_window

logger = logging.getLogger(__name__)


class MotionTrigger:
    """
    Finite state machine detecting a complete (movement -> stopping) cycle.

    Parameters
    ----------
    move_threshold_deg :
        Angular displacement from baseline required to declare MOVING.
    stop_window_sec :
        Sliding duration over which samples must stay within tolerance to declare STOP_CANDIDATE.
    stop_tolerance_deg :
        Maximum allowable angular spread across the stop window.
    stop_confirm_sec :
        Non-blocking duration sensor must remain stationary in STOP_CANDIDATE before capture.
    resume_threshold_deg :
        Angular displacement from candidate stop angle that aborts STOP_CANDIDATE and returns to MOVING.
    rearm_threshold_deg :
        Angular displacement from saved position required to initiate a new movement cycle.
    baseline_samples :
        Number of initial valid readings required to anchor the stationary baseline.
    max_gap_sec :
        Maximum allowable elapsed time between samples before declaring data invalid.
    """

    def __init__(
        self,
        move_threshold_deg: float = settings.MOTION_TRIGGER_MOVE_THRESH_DEG,
        stop_window_sec: float = settings.MOTION_TRIGGER_STOP_WINDOW_SEC,
        stop_tolerance_deg: float = settings.MOTION_TRIGGER_STOP_TOLERANCE_DEG,
        stop_confirm_sec: float = settings.MOTION_TRIGGER_STOP_CONFIRM_SEC,
        resume_threshold_deg: float = settings.MOTION_TRIGGER_RESUME_THRESH_DEG,
        rearm_threshold_deg: float = settings.MOTION_TRIGGER_REARM_THRESH_DEG,
        baseline_samples: int = settings.MOTION_TRIGGER_BASELINE_SAMPLES,
        max_gap_sec: float = settings.MOTION_TRIGGER_MAX_GAP_SEC,
    ) -> None:
        self.move_threshold_deg = move_threshold_deg
        self.stop_window_sec = stop_window_sec
        self.stop_tolerance_deg = stop_tolerance_deg
        self.stop_confirm_sec = stop_confirm_sec
        self.resume_threshold_deg = resume_threshold_deg
        self.rearm_threshold_deg = rearm_threshold_deg
        self.baseline_samples = baseline_samples
        self.max_gap_sec = max_gap_sec

        # State tracking
        self._state: MotionState = MotionState.WAITING_FOR_MOTION
        self._state_enter_time: float = time.monotonic()

        # Baseline tracking (WAITING_FOR_MOTION)
        self._baseline_window: Deque[float] = deque()
        self._baseline_angle: Optional[float] = None

        # Motion & stopping window (MOVING)
        self._sample_window: Deque[Tuple[float, float]] = deque()  # (angle, timestamp)

        # Candidate stop tracking (STOP_CANDIDATE)
        self._candidate_angle: Optional[float] = None
        self._candidate_start_time: Optional[float] = None

        # Capture tracking
        self._final_angle: Optional[float] = None
        self._last_saved_angle: Optional[float] = None
        self._last_saved_row: Optional[int] = None

        # Diagnostic & telemetry
        self._last_sample_time: Optional[float] = None
        self._last_angle: Optional[float] = None
        self._diagnostic_reason: Optional[str] = None

    # ── Properties ────────────────────────────────────────────────────

    @property
    def state(self) -> MotionState:
        return self._state

    @property
    def baseline_angle(self) -> Optional[float]:
        return self._baseline_angle

    @property
    def candidate_angle(self) -> Optional[float]:
        return self._candidate_angle

    @property
    def last_saved_angle(self) -> Optional[float]:
        return self._last_saved_angle

    @property
    def diagnostic_reason(self) -> Optional[str]:
        return self._diagnostic_reason

    # ── Reset & Lifecycle ─────────────────────────────────────────────

    def reset(self) -> None:
        """Reset state machine cleanly to WAITING_FOR_MOTION."""
        self._transition_to(MotionState.WAITING_FOR_MOTION, "Reset triggered")
        self._baseline_window.clear()
        self._baseline_angle = None
        self._sample_window.clear()
        self._candidate_angle = None
        self._candidate_start_time = None
        self._final_angle = None
        self._last_sample_time = None
        self._last_angle = None
        self._diagnostic_reason = None
        logger.debug("MotionTrigger: Reset to WAITING_FOR_MOTION.")

    def rearm(self, reason: str = "Re-arm triggered") -> None:
        """Explicitly re-arm trigger for next cycle (essential at turnaround steps e.g. Step 182)."""
        self._last_saved_angle = None
        self._candidate_angle = None
        self._candidate_start_time = None
        self._final_angle = None
        self._sample_window.clear()
        self._baseline_window.clear()
        self._baseline_angle = None
        self._transition_to(MotionState.WAITING_FOR_MOTION, reason)
        logger.debug("MotionTrigger: Re-armed (%s).", reason)


    def notify_save_success(self, saved_angle: float, row: Optional[int] = None) -> None:
        """Called when Excel persistence confirms success for the captured angle."""
        self._last_saved_angle = round(float(saved_angle), 2)
        self._last_saved_row = row
        self._candidate_angle = None
        self._candidate_start_time = None
        self._final_angle = None
        self._sample_window.clear()
        self._transition_to(
            MotionState.WAITING_FOR_NEXT_MOTION,
            f"Saved row {row} ({self._last_saved_angle:.2f}°). Awaiting next movement cycle.",
        )

    def notify_save_failed(self, reason: Optional[str] = None) -> None:
        """Called when Excel persistence fails — target row is preserved and trigger rearms."""
        self._candidate_angle = None
        self._candidate_start_time = None
        self._final_angle = None
        self._sample_window.clear()
        self._baseline_window.clear()
        self._baseline_angle = None
        self._transition_to(
            MotionState.WAITING_FOR_MOTION,
            f"Save failed ({reason or 'unknown error'}). Re-armed for motion/retry.",
        )

    # ── Main Processing Pipeline ──────────────────────────────────────

    def update(
        self,
        angle: float,
        monotonic_time: Optional[float] = None,
    ) -> MotionTriggerResult:
        """
        Process one incoming angle reading from the NAI API 8810A.

        Parameters
        ----------
        angle :
            Measured shaft angle in degrees (0.0° - 360.0°).
        monotonic_time :
            Monotonic timestamp in seconds (defaults to time.monotonic()).
        """
        now = monotonic_time if monotonic_time is not None else time.monotonic()

        # ── 1. Validate sample integrity ───────────────────────────────
        if not math.isfinite(angle):
            self._diagnostic_reason = f"Non-finite angle reading: {angle}"
            logger.warning("MotionTrigger: %s", self._diagnostic_reason)
            self._handle_invalid_reading(now)
            return self._build_result(now, is_valid=False, trigger_capture=False)

        # Check sample gap / communication timeout
        if self._last_sample_time is not None:
            gap = now - self._last_sample_time
            if gap > self.max_gap_sec:
                self._diagnostic_reason = f"Communication gap {gap:.2f}s > {self.max_gap_sec:.2f}s"
                logger.warning("MotionTrigger: %s", self._diagnostic_reason)
                self._handle_invalid_reading(now)
                self._last_sample_time = now
                self._last_angle = angle
                return self._build_result(now, is_valid=False, trigger_capture=False)

        self._last_sample_time = now
        self._last_angle = angle

        # Recover from DATA_INVALID upon receiving fresh valid samples
        if self._state == MotionState.DATA_INVALID:
            self._transition_to(MotionState.WAITING_FOR_MOTION, "Resumed valid sensor readings")
            self._baseline_window.clear()
            self._baseline_angle = None

        # ── 2. State-specific processing ──────────────────────────────
        trigger_capture = False

        if self._state == MotionState.WAITING_FOR_MOTION:
            self._handle_waiting_for_motion(angle, now)

        elif self._state == MotionState.MOVING:
            self._handle_moving(angle, now)

        elif self._state == MotionState.STOP_CANDIDATE:
            trigger_capture = self._handle_stop_candidate(angle, now)

        elif self._state == MotionState.CAPTURE_PENDING:
            # Capture is already dispatched; await save completion callback
            pass

        elif self._state == MotionState.WAITING_FOR_NEXT_MOTION:
            self._handle_waiting_for_next_motion(angle, now)

        return self._build_result(now, is_valid=True, trigger_capture=trigger_capture)

    # ── State Handlers ────────────────────────────────────────────────

    def _handle_waiting_for_motion(self, angle: float, now: float) -> None:
        """
        Accumulates initial baseline readings.
        Initial stable readings NEVER trigger a save.
        Transitions to MOVING only after measured movement exceeds move_threshold_deg.
        """
        self._baseline_window.append(angle)
        while len(self._baseline_window) > self.baseline_samples:
            self._baseline_window.popleft()

        # Compute baseline reference as median of unwrapped samples
        if len(self._baseline_window) >= self.baseline_samples:
            unwrapped = unwrap_window(list(self._baseline_window), self._baseline_window[0])
            sorted_unwrapped = sorted(unwrapped)
            med = sorted_unwrapped[len(sorted_unwrapped) // 2] % 360.0
            self._baseline_angle = round(med, 2)

            # Check if measured angle has moved significantly from baseline
            dist = angular_dist(angle, self._baseline_angle)
            if dist > self.move_threshold_deg:
                self._transition_to(
                    MotionState.MOVING,
                    f"Shaft motion detected: delta={dist:.3f}° > threshold={self.move_threshold_deg:.3f}°",
                )
                self._sample_window.clear()
                self._sample_window.append((angle, now))

    def _handle_moving(self, angle: float, now: float) -> None:
        """
        Shaft is in active motion. Auto-save is strictly locked.
        Maintains sliding window and checks whether angle has ceased changing.
        """
        self._sample_window.append((angle, now))

        # Prune samples so that window spans approximately stop_window_sec
        while len(self._sample_window) > 3 and (now - self._sample_window[1][1]) >= self.stop_window_sec:
            self._sample_window.popleft()

        duration = now - self._sample_window[0][1]
        n_samples = len(self._sample_window)

        # Check if motion has ceased across the stop window
        if duration >= (self.stop_window_sec * 0.95) and n_samples >= 3:

            angles = [s[0] for s in self._sample_window]
            unwrapped = unwrap_window(angles, angles[0])
            spread = max(unwrapped) - min(unwrapped)

            if spread <= self.stop_tolerance_deg:
                # Shaft motion has ceased; propose stop candidate
                sorted_unwrapped = sorted(unwrapped)
                cand = sorted_unwrapped[len(sorted_unwrapped) // 2] % 360.0
                self._candidate_angle = round(cand, 2)
                self._candidate_start_time = now
                self._transition_to(
                    MotionState.STOP_CANDIDATE,
                    f"Motion ceased (spread={spread:.3f}° <= {self.stop_tolerance_deg:.3f}°). "
                    f"Candidate stop: {self._candidate_angle:.2f}°",
                )

    def _handle_stop_candidate(self, angle: float, now: float) -> bool:
        """
        Verifies stop candidate over the nonblocking stop_confirm_sec interval.
        - If significant movement resumes, returns to MOVING.
        - Once stop_confirm_sec elapses, transitions to CAPTURE_PENDING.
        """
        if self._candidate_angle is None or self._candidate_start_time is None:
            self._transition_to(MotionState.MOVING, "Missing candidate reference")
            return False

        # 1. Check if movement resumed
        dev_from_cand = angular_dist(angle, self._candidate_angle)
        if dev_from_cand > self.resume_threshold_deg:
            self._transition_to(
                MotionState.MOVING,
                f"Motion resumed during stop confirmation: delta={dev_from_cand:.3f}° > "
                f"threshold={self.resume_threshold_deg:.3f}°",
            )
            self._sample_window.clear()
            self._sample_window.append((angle, now))
            self._candidate_angle = None
            self._candidate_start_time = None
            return False

        # 2. Check elapsed dwell time
        dwell = now - self._candidate_start_time
        if dwell >= self.stop_confirm_sec:
            self._final_angle = round(float(angle), 2)
            self._transition_to(
                MotionState.CAPTURE_PENDING,
                f"Inferred mechanical stop confirmed (dwell={dwell:.2f}s >= {self.stop_confirm_sec:.2f}s, "
                f"final angle={self._final_angle:.2f}°)",
            )
            return True

        return False

    def _handle_waiting_for_next_motion(self, angle: float, now: float) -> None:
        """
        Awaits a new movement cycle after successful capture.
        Auto-save is locked while stationary at the saved position.
        Rearms to MOVING only after movement exceeds rearm_threshold_deg.
        """
        if self._last_saved_angle is None:
            self._last_saved_angle = angle

        delta = angular_dist(angle, self._last_saved_angle)
        if delta > self.rearm_threshold_deg:
            self._transition_to(
                MotionState.MOVING,
                f"New movement cycle initiated: delta={delta:.3f}° from saved ref "
                f"{self._last_saved_angle:.2f}° > {self.rearm_threshold_deg:.3f}°",
            )
            self._sample_window.clear()
            self._sample_window.append((angle, now))

    def _handle_invalid_reading(self, now: float) -> None:
        """Handle sensor disconnection, communication gaps, or non-finite readings."""
        if self._state in (MotionState.STOP_CANDIDATE, MotionState.CAPTURE_PENDING):
            logger.info("MotionTrigger: Pending capture cancelled due to invalid/stale sensor reading.")
        self._candidate_angle = None
        self._candidate_start_time = None
        self._final_angle = None
        self._sample_window.clear()
        self._baseline_window.clear()
        self._baseline_angle = None
        self._transition_to(MotionState.DATA_INVALID, self._diagnostic_reason or "Sensor reading invalid/stale")

    # ── Helpers ───────────────────────────────────────────────────────

    def _transition_to(self, new_state: MotionState, reason: str = "") -> None:
        old_state = self._state
        self._state = new_state
        self._state_enter_time = time.monotonic()
        if old_state != new_state:
            logger.info("MotionTrigger: %s → %s | %s", old_state.name, new_state.name, reason)

    def _build_result(
        self,
        now: float,
        is_valid: bool = True,
        trigger_capture: bool = False,
    ) -> MotionTriggerResult:
        elapsed = now - self._state_enter_time
        return MotionTriggerResult(
            motion_state=self._state,
            trigger_capture=trigger_capture,
            captured_angle=self._final_angle if self._state == MotionState.CAPTURE_PENDING else None,
            current_angle=self._last_angle,
            baseline_angle=self._baseline_angle,
            candidate_angle=self._candidate_angle,
            elapsed_in_state=elapsed,
            message=self._diagnostic_reason or self._state.value,
            is_valid=is_valid,
            reset_reason=self._diagnostic_reason,
        )
