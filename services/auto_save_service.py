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
import math
import threading
import time
from typing import Callable, Optional

from config import settings
from core.exceptions import ExcelRepositoryError, WorkbookLockedError
from core.models import (
    AcquisitionEvent,
    LatchState,
    MeasurementRecord,
    MotionState,
    MotionTriggerResult,
    SaveMode,
    SaveResult,
    SaveStatus,
    StabilityResult,
    StabilityStatus,
    SystemState,
    TWDAMotionStatus,
)
from services.excel_service import ExcelService
from services.motion_detector import MotionTrigger
from services.stability_detector import StabilityDetector, angular_dist
from services.twda_motion_service import TWDAMotionTrigger, TWDAMotionTriggerState

logger = logging.getLogger(__name__)

# Callback type aliases
StabilityCallback = Callable[[StabilityResult | MotionTriggerResult], None]

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
        motion_trigger: Optional[MotionTrigger] = None,
        twda_trigger: Optional[TWDAMotionTrigger] = None,
        block_simulator_writes: bool = settings.BLOCK_SIMULATOR_WRITES,
        on_stability_changed: Optional[StabilityCallback] = None,
        on_save_complete: Optional[SaveCompleteCallback] = None,
        on_error: Optional[ErrorCallback] = None,
        sync_save: bool = False,
    ) -> None:
        self._excel = excel_service
        self._detector = stability_detector or StabilityDetector()
        self._motion_trigger = motion_trigger or MotionTrigger()
        self._twda_trigger = twda_trigger or TWDAMotionTrigger()
        self._block_sim = block_simulator_writes
        self._sync_save = sync_save

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
        self._latest_angle: Optional[float] = None
        self._latest_angle_time: float = 0.0
        self._latest_is_simulator: bool = False
        self._pending_twda_event: Optional[AcquisitionEvent] = None
        # Background save thread — kept as daemon so it doesn't block app exit
        self._save_thread: Optional[threading.Thread] = None

    # ── State Machine & Control ───────────────────────────────────────

    @property
    def state(self) -> SystemState:
        with self._lock:
            return self._state

    @property
    def twda_trigger(self) -> TWDAMotionTrigger:
        return self._twda_trigger

    @property
    def twda_state(self) -> str:
        return self._twda_trigger.state

    @property
    def motion_trigger(self) -> MotionTrigger:
        return self._motion_trigger

    @property
    def motion_state(self) -> MotionState:
        return self._motion_trigger.state

    @property
    def sync_save(self) -> bool:
        return self._sync_save

    @sync_save.setter
    def sync_save(self, val: bool) -> None:
        self._sync_save = bool(val)

    def wait_for_save(self, timeout: float = 3.0) -> None:
        """Wait for background save thread to finish."""
        t = self._save_thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

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
                self._motion_trigger.reset()
                logger.info("AutoSaveService: direction → %s (latch re-armed, motion trigger reset)", val)


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
                self._motion_trigger.reset()
                self._latch = LatchState.OPEN
                self._last_stable_angle = None
                logger.info("AutoSaveService: mode → AUTO (motion trigger re-armed fresh)")

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
            self._motion_trigger.reset()
            logger.info("AutoSaveService: state → RUNNING (gen=%d)", self._session_generation)

    def disable(self) -> None:
        """Stop processing feeds (transitions to STOPPED)."""
        with self._lock:
            self._state = SystemState.STOPPED
            self._session_generation += 1
            self._pending_candidate = None
            self._detector.reset()
            self._motion_trigger.reset()
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
            self._motion_trigger.reset()
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
            self._motion_trigger.reset()
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
            self._motion_trigger.reset()
            self._twda_trigger.reset()
            self._latch = LatchState.OPEN
            self._last_stable_angle = None
            self._pending_candidate = None
            self._pending_twda_event = None
            logger.info("AutoSaveService: reset — latch OPEN, motion and TWDA triggers reset.")

    def rearm(self, reason: str = "Explicit re-arm") -> None:
        """Explicitly re-arm latch and motion triggers (e.g. for Step 182 CW -> CCW turnaround)."""
        with self._lock:
            self._latch = LatchState.OPEN
            self._last_stable_angle = None
            self._motion_trigger.rearm(reason)
            self._pending_twda_event = None
            logger.info("AutoSaveService: explicitly re-armed (%s).", reason)

    def notify_controller_motion(
        self,
        status: TWDAMotionStatus | str,
        target_row: Optional[int] = None,
        step_number: Optional[int] = None,
        direction: Optional[str] = None,
        angle_override: Optional[float] = None,
        monotonic_time: Optional[float] = None,
    ) -> Optional[SaveResult]:
        """
        Primary trigger mechanism: Process TWDA Motion status from the Dynamix controller.

        When the controller transitions from MOVING to STOPPED, this method immediately
        schedules data acquisition from the NAI API 8810A for the current step.
        """
        now = monotonic_time if monotonic_time is not None else time.monotonic()
        eff_dir = (direction or self.direction).upper()

        with self._lock:
            event = self._twda_trigger.update_controller_status(
                status=status,
                target_row=target_row,
                step_number=step_number,
                direction=eff_dir,
                monotonic_time=now,
            )
            stat_str = str(getattr(status, "value", status)).upper()
            if stat_str == "MOVING":
                self._pending_twda_event = None
                return None

            if event is None:
                return None

            # When a verified controller stop occurs, always re-arm latch!
            self._latch = LatchState.OPEN
            self._last_stable_angle = None
            self._motion_trigger.rearm(f"Controller stop cycle {event.event_id}")

            if self._state != SystemState.RUNNING or self._save_mode == SaveMode.MANUAL:
                logger.info(
                    "AutoSaveService: Controller stop event %s received, but auto-save is %s (mode=%s)",
                    event.event_id, self._state.name, self._save_mode.name,
                )
                return None

            current_gen = self._session_generation
            meas_angle = angle_override

            if meas_angle is not None and math.isfinite(meas_angle):
                event.sensor_read_mono = now
                event.sensor_read_time = time.strftime("%H:%M:%S")
                event.measured_angle = round(float(meas_angle), 2)
                event.is_valid = True

                self._latch = LatchState.LATCHED
                self._last_stable_angle = event.measured_angle
                self._last_save_time = now
                self._twda_trigger.notify_acquisition_dispatched(event)

                return self._dispatch_save_async(
                    angle=event.measured_angle,
                    is_simulator=self._latest_is_simulator,
                    generation_id=current_gen,
                    event=event,
                    target_row=target_row,
                    direction=eff_dir,
                    step_number=step_number,
                )
            else:
                self._pending_twda_event = event
                logger.info("AutoSaveService: Event %s armed; awaiting fresh API 8810A reading", event.event_id)
                return None

    def notify_manual_save(self, angle: float, row: Optional[int] = None) -> None:
        """Synchronize manual save event to prevent immediate auto-save re-trigger."""
        with self._lock:
            self._latch = LatchState.LATCHED
            self._last_stable_angle = angle
            self._last_save_time = time.monotonic()
            self._motion_trigger.notify_save_success(saved_angle=angle, row=row)
            logger.info("AutoSaveService: manual save synced at %.2f° — trigger waiting for next motion.", angle)


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
            self._latest_angle = angle
            self._latest_angle_time = (monotonic_time if monotonic_time is not None else time.monotonic())
            self._latest_is_simulator = is_simulator

            # ── 0. Fulfill pending TWDA controller stop event if waiting for sample ──
            if self._pending_twda_event is not None and math.isfinite(angle):
                event = self._pending_twda_event
                self._pending_twda_event = None
                now = monotonic_time if monotonic_time is not None else time.monotonic()
                event.sensor_read_mono = now
                event.sensor_read_time = time.strftime("%H:%M:%S")
                event.measured_angle = round(float(angle), 2)
                event.is_valid = True

                self._latch = LatchState.LATCHED
                self._last_stable_angle = event.measured_angle
                self._last_save_time = now
                self._twda_trigger.notify_acquisition_dispatched(event)

                return self._dispatch_save_async(
                    angle=event.measured_angle,
                    is_simulator=is_simulator,
                    generation_id=current_gen,
                    event=event,
                    target_row=event.excel_row,
                    direction=event.direction,
                    step_number=event.step_number,
                )

        # ── 1. Feed Motion-Gated Stop Trigger & Stability Detector ─────
        motion_res: MotionTriggerResult = self._motion_trigger.update(
            angle, monotonic_time=monotonic_time
        )
        self._detector.update(angle, monotonic_time=monotonic_time)

        # Notify GUI of motion state on every sample (motion_res is duck-typed for StabilityResult)
        if self._on_stability:
            try:
                self._on_stability(motion_res)
            except Exception as exc:
                logger.error("on_stability_changed callback error: %s", exc)

        with self._lock:
            # Re-check state after trigger update
            if self._state != SystemState.RUNNING or current_gen != self._session_generation:
                return None

            # Prevent queued event pile-up while a save is already in progress
            if self._saving_in_progress:
                return None

            # In MANUAL save mode, motion state updates telemetry only — auto-commit is suppressed
            if self._save_mode == SaveMode.MANUAL:
                return None

            # ── 2. Check re-arm condition when LATCHED ─────────────────
            # Re-arm on EITHER angular delta exceeding threshold OR active MOVING state
            rearm_thresh = getattr(self._motion_trigger, "rearm_threshold_deg", settings.MOTION_TRIGGER_REARM_THRESH_DEG)
            if self._latch == LatchState.LATCHED:
                rearm = False
                if self._last_stable_angle is not None:
                    delta = angular_dist(angle, self._last_stable_angle)
                    if delta > rearm_thresh:
                        rearm = True
                        logger.info(
                            "AutoSaveService: re-arm — angle moved %.3f° from stable ref %.3f° (> threshold %.3f°).",
                            delta, self._last_stable_angle, rearm_thresh,
                        )
                if motion_res.motion_state == MotionState.MOVING:
                    rearm = True
                if rearm:
                    self._latch = LatchState.OPEN

            # ── 3. Fire save on confirmed motion-gated stop ────────────
            if motion_res.trigger_capture and self._latch == LatchState.OPEN:
                now = monotonic_time if monotonic_time is not None else time.monotonic()
                target_angle = (
                    motion_res.captured_angle
                    if motion_res.captured_angle is not None
                    else angle
                )

                # Debounce protection: prevent rapid duplicate triggers at the same position
                if self._last_save_time > 0 and (now - self._last_save_time) < self._min_interval_sec:
                    if self._last_stable_angle is not None and angular_dist(target_angle, self._last_stable_angle) < rearm_thresh:
                        logger.debug(
                            "AutoSaveService: debounce active (%.2fs < %.2fs) for position %.3f° — skipping duplicate.",
                            now - self._last_save_time, self._min_interval_sec, target_angle,
                        )
                        return None

                # Create logical candidate record
                self._latch = LatchState.LATCHED
                self._last_stable_angle = target_angle
                self._last_save_time = now

                # Block simulator writes if configured
                if is_simulator and self._block_sim:
                    msg = (
                        f"AutoSave skipped: simulator reading "
                        f"{target_angle:.3f}° not written to workbook."
                    )
                    logger.info(msg)
                    if self._on_error:
                        self._on_error(msg)
                    return None

                return self._dispatch_save_async(
                    angle=target_angle,
                    is_simulator=is_simulator,
                    generation_id=current_gen,
                )

        return None

    # ── Async Save Dispatcher ─────────────────────────────────────────

    def _dispatch_save_async(
        self,
        angle: Optional[float],
        is_simulator: bool,
        generation_id: int,
        event: Optional[AcquisitionEvent] = None,
        target_row: Optional[int] = None,
        direction: Optional[str] = None,
        step_number: Optional[int] = None,
    ) -> Optional[SaveResult]:
        """
        Execute Excel write. Runs synchronously if sync_save is True (for deterministic testing),
        otherwise spawns a background daemon thread so the Tkinter GUI never freezes.
        """
        if angle is None:
            return None

        if self._sync_save:
            return self._attempt_save(
                angle,
                is_simulator,
                generation_id,
                event=event,
                target_row=target_row,
                direction=direction,
                step_number=step_number,
            )

        t = threading.Thread(
            target=self._attempt_save,
            args=(angle, is_simulator, generation_id),
            kwargs={
                "event": event,
                "target_row": target_row,
                "direction": direction,
                "step_number": step_number,
            },
            daemon=True,
            name="AutoSaveWorker",
        )
        t.start()
        self._save_thread = t
        return None

    # ── Storage Commit Execution ──────────────────────────────────────

    def _attempt_save(
        self,
        angle: Optional[float],
        is_simulator: bool,
        generation_id: int,
        event: Optional[AcquisitionEvent] = None,
        target_row: Optional[int] = None,
        direction: Optional[str] = None,
        step_number: Optional[int] = None,
    ) -> Optional[SaveResult]:
        """
        Commit candidate record to Excel with state validation and retry logic.
        Runs in a dedicated background daemon thread (or synchronously in test mode).

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
        record_id = event.event_id if event else f"{time.monotonic():.6f}"
        save_result: Optional[SaveResult] = None
        last_error: Optional[Exception] = None

        max_retries = settings.EXCEL_WRITE_MAX_RETRIES
        delay = settings.EXCEL_WRITE_RETRY_DELAY_SEC
        eff_dir = direction or self._direction

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
                        target_row=target_row,
                        is_simulator=is_simulator,
                        direction=eff_dir,
                        step_number=step_number,
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
                if event:
                    self._twda_trigger.notify_storage_failed(event, str(last_error))
                self._motion_trigger.notify_save_failed(reason=str(last_error))
                # Re-open latch so operator can retry after unlocking file
                self._latch = LatchState.OPEN
            if self._on_error:
                self._on_error(msg)
            return None

        # Successful commit — update triggers and notify callbacks
        with self._lock:
            saved_ang = save_result.angle if save_result.angle is not None else angle
            if event:
                self._twda_trigger.notify_storage_completed(event, save_result)
            self._motion_trigger.notify_save_success(
                saved_angle=saved_ang,
                row=save_result.excel_row,
            )
            self._last_stable_angle = saved_ang
            save_result.event = event

        if self._on_save_complete:
            try:
                self._on_save_complete(save_result)
            except Exception as exc:
                logger.error("on_save_complete callback error: %s", exc)

        return save_result

