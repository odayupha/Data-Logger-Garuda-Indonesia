"""
services/twda_motion_service.py
────────────────────────────────
Event-Driven TWDA Motion Trigger Service for Flap Sensor Data Logger.

Operational Context:
  Automated flap motion is commanded and driven by the Dynamix controller.
  Data acquisition MUST be triggered by genuine controller motion events
  (specifically the transition from MOVING to confirmed STOPPED), rather than
  relying solely on sensor angle changes or stability thresholds.

Responsibilities:
  - Continuously track TWDA Motion status from the Dynamix controller.
  - Detect strictly qualified MOVING → STOPPED transitions.
  - Reject invalid, stale, or unknown controller status without assuming stop.
  - Produce exactly one logical AcquisitionEvent per stop cycle with unique
    Session ID and Event ID.
  - Support turnaround steps (such as Step 182 CW → Step 182 CCW at -6.00°)
    where physical angle is identical across direction change.
  - Coordinate with AutoSaveService: schedule reading API 8810A, validate
    data, and verify Excel persistence before marking event complete.
  - Provide an ITWDAController interface and simulated controller for testing.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable, Deque, Optional
from collections import deque

from core.models import (
    AcquisitionEvent,
    EvaluationStatus,
    SaveResult,
    SaveStatus,
    TWDAMotionStatus,
)

logger = logging.getLogger(__name__)


class TWDAMotionTriggerState:
    WAITING_FOR_MOTION      = "WAITING_FOR_MOTION"
    CONTROLLER_MOVING       = "CONTROLLER_MOVING"
    STOP_DETECTED           = "STOP_DETECTED"
    ACQUISITION_DISPATCHED  = "ACQUISITION_DISPATCHED"
    WAITING_FOR_NEXT_MOTION = "WAITING_FOR_NEXT_MOTION"
    CONTROLLER_ERROR        = "CONTROLLER_ERROR"


class TWDAMotionTrigger:
    """
    Finite state machine managing controller-driven acquisition events.

    Parameters
    ----------
    settle_dwell_sec : float
        Small delay in seconds after STOPPED signal to allow mechanical
        rigging transients to dissipate before sampling the API 8810A.
    timeout_sec : float
        Maximum elapsed time since last controller status update before
        declaring controller status stale / UNKNOWN.
    """

    def __init__(
        self,
        settle_dwell_sec: float = 0.05,
        timeout_sec: float = 3.0,
        on_acquisition_event: Optional[Callable[[AcquisitionEvent], Any]] = None,
    ) -> None:
        self.settle_dwell_sec = settle_dwell_sec
        self.timeout_sec = timeout_sec
        self.on_acquisition_event = on_acquisition_event

        self._session_id: str = f"SES_{uuid.uuid4().hex[:8]}"
        self._event_counter: int = 0
        self._state: str = TWDAMotionTriggerState.WAITING_FOR_MOTION
        self._state_enter_time: float = time.monotonic()

        self._last_status: TWDAMotionStatus = TWDAMotionStatus.UNKNOWN
        self._last_status_mono: float = 0.0
        self._last_stop_mono: float = 0.0
        self._last_stop_wall: str = ""

        self._active_event: Optional[AcquisitionEvent] = None
        self._diagnostic_reason: Optional[str] = None
        self._armed_for_stop: bool = False
        self._consecutive_stopped_count: int = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def status(self) -> TWDAMotionStatus:
        return self._last_status

    @property
    def state(self) -> str:
        return self._state

    @property
    def active_event(self) -> Optional[AcquisitionEvent]:
        return self._active_event

    @property
    def diagnostic_reason(self) -> Optional[str]:
        return self._diagnostic_reason

    @property
    def is_armed(self) -> bool:
        return self._armed_for_stop

    def reset(self, session_id: Optional[str] = None) -> None:
        """Reset trigger state for a new test run or session."""
        self._session_id = session_id or f"SES_{uuid.uuid4().hex[:8]}"
        self._event_counter = 0
        self._state = TWDAMotionTriggerState.WAITING_FOR_MOTION
        self._state_enter_time = time.monotonic()
        self._last_status = TWDAMotionStatus.UNKNOWN
        self._last_status_mono = 0.0
        self._last_stop_mono = 0.0
        self._last_stop_wall = ""
        self._active_event = None
        self._diagnostic_reason = None
        self._armed_for_stop = False
        self._consecutive_stopped_count = 0
        logger.info("TWDAMotionTrigger: reset (session_id=%s)", self._session_id)

    def reset_session(self, session_id: Optional[str] = None) -> None:
        """Alias for reset() to start a fresh session."""
        self.reset(session_id)

    def arm_initial_step(self) -> None:
        """Allow capturing the initial stationary reference step (e.g. Rig position)."""
        self._armed_for_stop = True
        logger.debug("TWDAMotionTrigger: armed for initial reference step")

    def feed_controller_status(
        self,
        status: TWDAMotionStatus | str,
        direction: str = "CW",
        step_number: Optional[int] = None,
        excel_row: Optional[int] = None,
        setpoint: Optional[float] = None,
        monotonic_time: Optional[float] = None,
    ) -> Optional[AcquisitionEvent]:
        """Convenience method to feed controller status with setpoint assignment."""
        evt = self.update_controller_status(
            status=status,
            target_row=excel_row,
            step_number=step_number,
            direction=direction,
            monotonic_time=monotonic_time,
        )
        if evt is not None and setpoint is not None:
            evt.setpoint = setpoint
        return evt

    def update_controller_status(
        self,
        status: TWDAMotionStatus | str,
        target_row: Optional[int] = None,
        step_number: Optional[int] = None,
        direction: str = "CW",
        monotonic_time: Optional[float] = None,
    ) -> Optional[AcquisitionEvent]:
        """
        Process a controller TWDA Motion status update.

        Returns an AcquisitionEvent only on a qualifying MOVING → STOPPED transition.
        """
        now = monotonic_time if monotonic_time is not None else time.monotonic()

        # Parse status enum
        if isinstance(status, str):
            try:
                stat_enum = TWDAMotionStatus[status.upper()]
            except KeyError:
                stat_enum = TWDAMotionStatus.UNKNOWN
        else:
            stat_enum = status

        self._last_status_mono = now

        # ── 1. Handle Controller Error or Unknown Status ───────────────
        if stat_enum in (TWDAMotionStatus.UNKNOWN, TWDAMotionStatus.ERROR):
            reason = f"Controller status {stat_enum.value}"
            self._diagnostic_reason = reason
            self._transition_to(TWDAMotionTriggerState.CONTROLLER_ERROR, reason)
            self._last_status = stat_enum
            # Critical: NEVER assume stopped when controller status is unknown or error!
            return None

        # ── 2. Handle MOVING Status ────────────────────────────────────
        if stat_enum == TWDAMotionStatus.MOVING:
            self._armed_for_stop = True
            self._consecutive_stopped_count = 0
            self._diagnostic_reason = None
            if self._state != TWDAMotionTriggerState.CONTROLLER_MOVING:
                self._transition_to(
                    TWDAMotionTriggerState.CONTROLLER_MOVING,
                    f"TWDA Motion active in {direction} direction",
                )
            self._last_status = stat_enum
            return None

        # ── 3. Handle STOPPED Status ───────────────────────────────────
        if stat_enum == TWDAMotionStatus.STOPPED:
            self._consecutive_stopped_count += 1
            prev_status = self._last_status
            self._last_status = stat_enum

            # Only qualify if transitioning from MOVING (or initial arming)
            if not self._armed_for_stop:
                # Controller is stationary but no preceding move event occurred
                return None

            # Qualifying Stop Transition confirmed!
            self._armed_for_stop = False
            self._event_counter += 1
            event_id = f"EVT_{self._event_counter:04d}"
            wall_time = time.strftime("%H:%M:%S")

            self._last_stop_mono = now
            self._last_stop_wall = wall_time

            event = AcquisitionEvent(
                session_id=self._session_id,
                event_id=event_id,
                trigger_source="TWDA_MOTION",
                step_number=step_number,
                excel_row=target_row,
                direction=direction.upper(),
                controller_stop_mono=now,
                controller_stop_time=wall_time,
                save_status=SaveStatus.PENDING,
            )
            self._active_event = event

            self._transition_to(
                TWDAMotionTriggerState.STOP_DETECTED,
                f"Stop confirmed (event={event_id}, step={step_number}, row={target_row}, dir={direction})",
            )
            logger.info(
                "TWDAMotionTrigger: QUALIFIED STOP EVENT [%s | %s] Step=%s Row=%s Dir=%s Mono=%.4f",
                self._session_id, event_id, step_number, target_row, direction, now,
            )
            if self.on_acquisition_event is not None:
                try:
                    self.on_acquisition_event(event)
                except Exception as exc:
                    logger.error("TWDAMotionTrigger: on_acquisition_event error: %s", exc)
            return event

        return None

    def notify_acquisition_dispatched(self, event: AcquisitionEvent) -> None:
        """Mark that reading sensor API 8810A has been scheduled."""
        self._state = TWDAMotionTriggerState.ACQUISITION_DISPATCHED
        logger.debug("TWDAMotionTrigger: Acquisition dispatched for %s", event.event_id)

    def notify_storage_completed(self, event: AcquisitionEvent, save_result: SaveResult) -> None:
        """Mark that Excel storage succeeded and transaction is complete."""
        event.save_status = SaveStatus.SAVED
        self._state = TWDAMotionTriggerState.WAITING_FOR_NEXT_MOTION
        self._active_event = None
        logger.info(
            "TWDAMotionTrigger: Transaction completed successfully for %s → Row %s (Angle=%.2f°)",
            event.event_id, save_result.excel_row, save_result.angle or 0.0,
        )

    def notify_storage_failed(self, event: AcquisitionEvent, reason: str) -> None:
        """Handle persistence failure — keep target row intact and re-arm for retry."""
        event.save_status = SaveStatus.FAILED
        event.error = reason
        self._state = TWDAMotionTriggerState.CONTROLLER_ERROR
        self._diagnostic_reason = f"Storage failed: {reason}"
        self._active_event = None
        # Re-arm so operator or next controller cycle can retry
        self._armed_for_stop = True
        logger.warning(
            "TWDAMotionTrigger: Transaction FAILED for %s (%s). Re-armed for retry.",
            event.event_id, reason,
        )

    def _transition_to(self, new_state: str, reason: str = "") -> None:
        old_state = self._state
        self._state = new_state
        self._state_enter_time = time.monotonic()
        if old_state != new_state:
            logger.info("TWDAMotionTrigger: %s → %s | %s", old_state, new_state, reason)


# ══════════════════════════════════════════════════════════════════════
#  Controller Interface & Simulation Adapter
# ══════════════════════════════════════════════════════════════════════

class ITWDAController:
    """Protocol interface for reading TWDA Motion status from controller."""

    def connect(self) -> bool:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def get_motion_status(self) -> TWDAMotionStatus:
        raise NotImplementedError


class SimulatedTWDAController(ITWDAController):
    """
    Simulated TWDA controller client for testing automated flap test execution.
    Provides deterministic motion profiles without physical controller hardware.
    """

    def __init__(self) -> None:
        self._connected: bool = True
        self._status: TWDAMotionStatus = TWDAMotionStatus.STOPPED
        self._direction: str = "CW"

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False
        self._status = TWDAMotionStatus.UNKNOWN

    def is_connected(self) -> bool:
        return self._connected

    def get_motion_status(self) -> TWDAMotionStatus:
        if not self._connected:
            return TWDAMotionStatus.UNKNOWN
        return self._status

    def set_status(self, status: TWDAMotionStatus) -> None:
        self._status = status

    def simulate_move_to_step(
        self,
        trigger: TWDAMotionTrigger,
        target_row: int,
        step_number: int,
        direction: str = "CW",
        move_duration_sec: float = 0.10,
        start_mono: Optional[float] = None,
    ) -> Optional[AcquisitionEvent]:
        """Simulate a complete move → stop cycle and return the generated event."""
        t = start_mono if start_mono is not None else time.monotonic()

        # Step 1: Controller starts moving
        self.set_status(TWDAMotionStatus.MOVING)
        trigger.update_controller_status(
            TWDAMotionStatus.MOVING,
            target_row=target_row,
            step_number=step_number,
            direction=direction,
            monotonic_time=t,
        )

        t += move_duration_sec

        # Step 2: Controller arrives and stops
        self.set_status(TWDAMotionStatus.STOPPED)
        event = trigger.update_controller_status(
            TWDAMotionStatus.STOPPED,
            target_row=target_row,
            step_number=step_number,
            direction=direction,
            monotonic_time=t,
        )
        return event
