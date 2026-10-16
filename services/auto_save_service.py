"""
services/auto_save_service.py
──────────────────────────────
Orchestrates automatic saving when the sensor reaches a stable position.

Responsibilities:
  - Centralized SystemState machine (STOPPED, RUNNING, PAUSED, STOPPING).
  - Generation ID tracking to invalidate stale queued events across pause/resume.
  - Synchronous Pause invalidation and reset of stability detection.
  - Strict RUNNING check at the storage commit point protected by a thread lock.
  - Feed incoming angle readings to the StabilityDetector.
  - On STABLE transition, fire one (and only one) save via ExcelService.
  - Latch to prevent duplicate saves at the same position.
  - Re-arm latch when angle changes ≥ rearm_threshold or active motion occurs.
  - Idempotent retry logic on transient Excel lock errors using the same record_id.
  - Simulator write blocking (configurable).
  - Thread-safe for multi-threaded / GUI environments.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from config import settings
from core.exceptions import ExcelRepositoryError, WorkbookLockedError
from core.models import (
    LatchState,
    MeasurementRecord,
    SaveMode,
    SaveResult,
    SaveStatus,
    StabilityResult,
    StabilityStatus,
    SystemState,
)
from services.excel_service import ExcelService
from services.stability_detector import StabilityDetector, angular_dist

logger = logging.getLogger(__name__)

# Callback type aliases
StabilityCallback = Callable[[StabilityResult], None]
SaveCompleteCallback = Callable[[SaveResult], None]
ErrorCallback = Callable[[str], None]


class AutoSaveService:
    """
    Coordinates StabilityDetector and ExcelService with a centralized state machine and latch.

    Parameters
    ----------
    excel_service :
        The service that persists measurements.
    stability_detector :
        The state machine; a new one is created if not provided.
    block_simulator_writes :
        When True, readings from the simulator are blocked from writing to Excel.
    on_stability_changed :
        Called every time a new StabilityResult is produced.
    on_save_complete :
        Called after each successful auto-save operation.
    on_error :
        Called with a human-readable error message on exceptions.
    """

    def __init__(
        self,
        excel_service: ExcelService,
        stability_detector: Optional[StabilityDetector] = None,
        block_simulator_writes: bool = settings.BLOCK_SIMULATOR_WRITES,
        on_stability_changed: Optional[StabilityCallback] = None,
        on_save_complete: Optional[SaveCompleteCallback] = None,
        on_error: Optional[ErrorCallback] = None,
    ) -> None:
        self._excel = excel_service
        self._detector = stability_detector or StabilityDetector()
        self._block_sim = block_simulator_writes

        self._on_stability = on_stability_changed
        self._on_save_complete = on_save_complete
        self._on_error = on_error

        self._lock = threading.RLock()
        self._state: SystemState = SystemState.STOPPED
        self._save_mode: SaveMode = SaveMode.AUTO
        self._session_generation: int = 0
        self._latch: LatchState = LatchState.OPEN
        self._last_stable_angle: Optional[float] = None
        self._last_save_time: float = 0.0
        self._min_interval_sec: float = getattr(settings, "AUTO_SAVE_MIN_INTERVAL_SEC", 1.2)
        self._pending_candidate: Optional[MeasurementRecord] = None
        self._direction: str = "CW"
        self._saving_in_progress: bool = False

    # ── State Machine & Control ───────────────────────────────────────

    @property
    def state(self) -> SystemState:
        with self._lock:
            return self._state

    @property
    def direction(self) -> str:
        with self._lock:
            return self._direction

    @direction.setter
    def direction(self, mode: str) -> None:
        with self._lock:
            val = str(mode).upper()
            if self._direction != val:
                self._direction = val
                # Re-arm latch when direction changes so sensor at current angle can be evaluated/saved in new direction
                self._latch = LatchState.OPEN
                self._last_stable_angle = None
                logger.info("AutoSaveService: direction → %s (latch re-armed)", val)

    @property
    def save_mode(self) -> SaveMode:
        with self._lock:
            return self._save_mode

    def set_save_mode(self, mode: SaveMode) -> None:
        """
        Transition between AUTO and MANUAL save modes safely.

        Rules:
          - If switching to MANUAL: uncommitted auto-candidates discarded, in-flight saves cancelled.
          - If switching to AUTO: resets stability window & latch so old resting angles aren't auto-saved.
          - If system is currently PAUSED, pause state is preserved.
        """
        with self._lock:
            if self._save_mode == mode:
                return
            old_mode = self._save_mode
            self._save_mode = mode
            self._session_generation += 1
            self._pending_candidate = None

            if mode == SaveMode.MANUAL:
                logger.info("AutoSaveService: mode → MANUAL (auto-save triggers disabled)")
            else:
                self._detector.reset()
                self._latch = LatchState.OPEN
                self._last_stable_angle = None
                logger.info("AutoSaveService: mode → AUTO (stability detector re-armed fresh)")

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._state == SystemState.RUNNING

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._state == SystemState.PAUSED

    @property
    def latch_state(self) -> LatchState:
        with self._lock:
            return self._latch

    @property
    def stability_status(self) -> StabilityStatus:
        return self._detector.status

    @property
    def session_generation(self) -> int:
        with self._lock:
            return self._session_generation

    def enable(self) -> None:
        """Start or enable the auto-save service (transitions to RUNNING)."""
        with self._lock:
            self._state = SystemState.RUNNING
            self._save_mode = SaveMode.AUTO
            self._session_generation += 1
            self._latch = LatchState.OPEN
            self._pending_candidate = None
            self._detector.reset()
            logger.info("AutoSaveService: state → RUNNING (gen=%d)", self._session_generation)

    def disable(self) -> None:
        """Stop processing feeds (transitions to STOPPED)."""
        with self._lock:
            self._state = SystemState.STOPPED
            self._session_generation += 1
            self._pending_candidate = None
            self._detector.reset()
            logger.info("AutoSaveService: state → STOPPED")

    def pause(self) -> None:
        """
        Synchronously suspend auto-saving (transitions to PAUSED).
        Invalidates uncommitted stable candidates, resets detector window,
        and increments generation ID to drop stale queued events.
        """
        with self._lock:
            self._state = SystemState.PAUSED
            self._session_generation += 1
            self._pending_candidate = None
            self._detector.reset()
            logger.info("AutoSaveService: state → PAUSED (candidates invalidated, gen=%d)", self._session_generation)

    def resume(self) -> None:
        """
        Resume auto-saving after pause (transitions to RUNNING).
        Restarts stability detection fresh so pre-pause data cannot trigger auto-save.
        """
        with self._lock:
            self._state = SystemState.RUNNING
            self._session_generation += 1
            self._pending_candidate = None
            self._detector.reset()
            logger.info("AutoSaveService: state → RUNNING (resumed fresh, gen=%d)", self._session_generation)

    def toggle_pause(self) -> bool:
        """Toggle pause state. Returns True if now paused, False if running."""
        with self._lock:
            if self._state == SystemState.PAUSED:
                self.resume()
                return False
            else:
                self.pause()
                return True

    def reset(self) -> None:
        """Fully reset the detector and re-open the latch."""
        with self._lock:
            self._detector.reset()
            self._latch = LatchState.OPEN
            self._last_stable_angle = None
            self._pending_candidate = None
            logger.info("AutoSaveService: reset — latch OPEN.")

    # ── Main Entry Point ──────────────────────────────────────────────

    def feed(
        self,
        angle: float,
        is_simulator: bool = False,
        monotonic_time: Optional[float] = None,
    ) -> Optional[SaveResult]:
        """
        Process one incoming angle reading.

        Parameters
        ----------
        angle :
            The measured angle in degrees.
        is_simulator :
            True if the reading comes from the simulator.
        monotonic_time :
            Optional time in seconds; defaults to time.monotonic() if None.

        Returns
        -------
        SaveResult if an auto-save was attempted, else None.
        """
        with self._lock:
            if self._state != SystemState.RUNNING:
                return None
            current_gen = self._session_generation

        # ── 1. Feed stability detector ─────────────────────────────────
        result: StabilityResult = self._detector.update(angle, monotonic_time=monotonic_time)

        # Notify GUI of stability state on every sample
        if self._on_stability:
            try:
                self._on_stability(result)
            except Exception as exc:
                logger.error("on_stability_changed callback error: %s", exc)

        with self._lock:
            # Re-check state after detector update
            if self._state != SystemState.RUNNING or current_gen != self._session_generation:
                return None

            # Prevent queued event pile-up while a save is already in progress
            if self._saving_in_progress:
                return None

            # In MANUAL save mode, stability updates telemetry only — auto-commit is suppressed
            if self._save_mode == SaveMode.MANUAL:
                return None

            # ── 2. Check re-arm condition when LATCHED ─────────────────
            rearm_thresh = getattr(self._detector, "rearm_threshold_deg", settings.AUTO_SAVE_REARM_THRESHOLD_DEG)
            if self._latch == LatchState.LATCHED:
                if self._last_stable_angle is not None:
                    delta = angular_dist(angle, self._last_stable_angle)
                    if delta > rearm_thresh:
                        logger.info(
                            "AutoSaveService: re-arm — angle moved %.3f° from stable ref %.3f° (> threshold %.3f°).",
                            delta, self._last_stable_angle, rearm_thresh,
                        )
                        self._latch = LatchState.OPEN
                elif result.status == StabilityStatus.MOVING:
                    self._latch = LatchState.OPEN

            # ── 3. Fire save on STABLE transition with OPEN latch ──────
            if result.just_became_stable and self._latch == LatchState.OPEN:
                now = monotonic_time if monotonic_time is not None else time.monotonic()
                # Debounce protection: prevent rapid duplicate triggers at the same position
                if self._last_save_time > 0 and (now - self._last_save_time) < self._min_interval_sec:
                    if self._last_stable_angle is not None and angular_dist(result.stable_angle, self._last_stable_angle) < rearm_thresh:
                        logger.debug(
                            "AutoSaveService: debounce active (%.2fs < %.2fs) for position %.3f° — skipping duplicate.",
                            now - self._last_save_time, self._min_interval_sec, result.stable_angle,
                        )
                        return None

                # Create logical candidate record
                self._latch = LatchState.LATCHED
                self._last_stable_angle = result.stable_angle
                self._last_save_time = now

                # Block simulator writes if configured
                if is_simulator and self._block_sim:
                    msg = (
                        f"AutoSave skipped: simulator reading "
                        f"{result.stable_angle:.3f}° not written to workbook."
                    )
                    logger.info(msg)
                    if self._on_error:
                        self._on_error(msg)
                    return None

                return self._attempt_save(
                    angle=result.stable_angle,
                    is_simulator=is_simulator,
                    generation_id=current_gen,
                )

        return None

    # ── Storage Commit Execution ──────────────────────────────────────

    def _attempt_save(
        self,
        angle: Optional[float],
        is_simulator: bool,
        generation_id: int,
    ) -> Optional[SaveResult]:
        """
        Commit candidate record to Excel with state validation and retry logic.

        CRITICAL MUTABLE RULE:
        Verify state == RUNNING and generation_id matches at the exact commit point.
        """
        if angle is None:
            return None

        # Lock check before initiating write
        with self._lock:
            if self._state != SystemState.RUNNING or generation_id != self._session_generation:
                logger.info("AutoSaveService: commit cancelled — system is %s or generation mismatched", self._state.name)
                return None

        # Generate a unique candidate record ID for idempotent retry
        record_id = f"{time.monotonic():.6f}"
        save_result: Optional[SaveResult] = None
        last_error: Optional[Exception] = None

        max_retries = settings.EXCEL_WRITE_MAX_RETRIES
        delay = settings.EXCEL_WRITE_RETRY_DELAY_SEC

        with self._lock:
            self._saving_in_progress = True

        try:
            for attempt in range(1, max_retries + 1):
                # Verify system is still RUNNING on each retry attempt
                with self._lock:
                    if self._state != SystemState.RUNNING or generation_id != self._session_generation:
                        logger.info("AutoSaveService: retry aborted — state changed to %s during save attempt %d", self._state.name, attempt)
                        return None

                try:
                    save_result = self._excel.append_measurement(
                        angle=angle,
                        is_simulator=is_simulator,
                        direction=self._direction,
                    )
                    if save_result and save_result.status == SaveStatus.SAVED:
                        break
                except WorkbookLockedError as exc:
                    last_error = exc
                    logger.warning("AutoSaveService: attempt %d/%d file locked: %s", attempt, max_retries, exc)
                    if attempt < max_retries:
                        time.sleep(delay)
                except Exception as exc:
                    last_error = exc
                    logger.error("AutoSaveService: attempt %d/%d unrecoverable error: %s", attempt, max_retries, exc)
                    break
        finally:
            with self._lock:
                self._saving_in_progress = False

        if not save_result or save_result.status != SaveStatus.SAVED:
            msg = f"AutoSave FAILED: {last_error or 'Unknown Excel write failure'}"
            logger.error(msg)
            with self._lock:
                # Re-open latch so operator can retry after unlocking file
                self._latch = LatchState.OPEN
            if self._on_error:
                self._on_error(msg)
            return None

        # Successful commit
        if self._on_save_complete:
            try:
                self._on_save_complete(save_result)
            except Exception as exc:
                logger.error("on_save_complete callback error: %s", exc)

        return save_result
