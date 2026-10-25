"""
core/models.py
──────────────
Shared dataclass models used across all backend layers.

Rules:
- No GUI imports.
- No I/O imports (openpyxl, serial, socket).
- Pure data containers with light validation only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ══════════════════════════════════════════════════════════════════════
#  Centralized System State Machine States
# ══════════════════════════════════════════════════════════════════════

class SystemState(Enum):
    """Centralized lifecycle state for logging and auto-save coordination."""
    STOPPED  = auto()   # Service is inactive / stopped
    RUNNING  = auto()   # Actively polling, detecting stability, and committing saves
    PAUSED   = auto()   # Suspended by operator; all auto-saves strictly blocked
    STOPPING = auto()   # Transitioning to stopped state


# ══════════════════════════════════════════════════════════════════════
#  Excel Write Modes
# ══════════════════════════════════════════════════════════════════════

class WriteMode(Enum):
    """Excel writing strategy."""
    APPEND        = auto()   # Writes to next available append row
    EDIT_SELECTED = auto()   # Explicitly modifies selected row only


# ══════════════════════════════════════════════════════════════════════
#  Stability State Machine States (Legacy / Telemetry)
# ══════════════════════════════════════════════════════════════════════

class StabilityStatus(Enum):
    """States of the StabilityDetector state machine."""
    WAITING_FOR_DATA = auto()   # No samples received yet
    MOVING           = auto()   # Angle is actively changing beyond tolerance
    SETTLING         = auto()   # Angle within tolerance, accumulating confirmation
    STABLE           = auto()   # Confirmed stable — auto-save may fire
    UNSTABLE         = auto()   # Prolonged oscillation / erratic jitter detected


# ══════════════════════════════════════════════════════════════════════
#  Motion-Gated Stop Trigger State Machine States
# ══════════════════════════════════════════════════════════════════════

class MotionState(Enum):
    """
    States for the Motion-Gated Stop Trigger.
    Inferred mechanical stop based on measured NAI API 8810A angle stream.
    """
    WAITING_FOR_MOTION       = "WAITING FOR MOTION"
    MOVING                   = "MOVING — AUTO-SAVE LOCKED"
    STOP_CANDIDATE           = "STOP CANDIDATE"
    CAPTURE_PENDING          = "CAPTURING DATA"
    WAITING_FOR_NEXT_MOTION  = "WAITING FOR NEXT MOTION"
    DATA_INVALID             = "SENSOR DISCONNECTED / DATA INVALID"


# ══════════════════════════════════════════════════════════════════════
#  TWDA Controller Motion States & Connection Lifecycle
# ══════════════════════════════════════════════════════════════════════

class TWDAMotionStatus(Enum):
    """
    Status of the Dynamix controller TWDA Motion signal.
    Primary trigger source for event-driven data acquisition.
    """
    STOPPED = "STOPPED"
    MOVING  = "MOVING"
    UNKNOWN = "UNKNOWN"
    ERROR   = "ERROR"


class ConnectionState(Enum):
    """Lifecycle state of instrument / controller hardware connections."""
    DISCONNECTED     = "DISCONNECTED"
    CONNECTING       = "CONNECTING"
    CONNECTED        = "CONNECTED"
    DISCONNECTING    = "DISCONNECTING"
    CONNECTION_ERROR = "CONNECTION ERROR"


# ══════════════════════════════════════════════════════════════════════
#  Stability Update Result
# ══════════════════════════════════════════════════════════════════════


@dataclass
class StabilityResult:
    """
    Output produced by ``StabilityDetector.update()`` on each sample.

    Callers (AutoSaveService) should key on ``just_became_stable`` to
    trigger exactly one save per transition.
    """
    status: StabilityStatus
    just_became_stable: bool        # True only on the SETTLING → STABLE transition
    stable_angle: Optional[float]   # Representative angle when status == STABLE, else None
    sample_count: int               # Number of samples in the current stable window
    stable_duration_sec: float      # Elapsed seconds since window began
    reset_reason: Optional[str]     # Human-readable reason if state was reset, else None


# ══════════════════════════════════════════════════════════════════════
#  Motion-Gated Trigger Result
# ══════════════════════════════════════════════════════════════════════

@dataclass
class MotionTriggerResult:
    """
    Output produced by MotionTrigger on each live angle sample.
    Provides duck-typed backward compatibility with StabilityResult.
    """
    motion_state: MotionState
    trigger_capture: bool = False
    captured_angle: Optional[float] = None
    current_angle: Optional[float] = None
    baseline_angle: Optional[float] = None
    candidate_angle: Optional[float] = None
    elapsed_in_state: float = 0.0
    message: str = ""
    is_valid: bool = True
    reset_reason: Optional[str] = None

    @property
    def status(self) -> StabilityStatus:
        if self.motion_state == MotionState.WAITING_FOR_MOTION:
            return StabilityStatus.WAITING_FOR_DATA
        elif self.motion_state == MotionState.MOVING:
            return StabilityStatus.MOVING
        elif self.motion_state == MotionState.STOP_CANDIDATE:
            return StabilityStatus.SETTLING
        elif self.motion_state in (MotionState.CAPTURE_PENDING, MotionState.WAITING_FOR_NEXT_MOTION):
            return StabilityStatus.STABLE
        elif self.motion_state == MotionState.DATA_INVALID:
            return StabilityStatus.UNSTABLE
        return StabilityStatus.WAITING_FOR_DATA

    @property
    def just_became_stable(self) -> bool:
        return self.trigger_capture

    @property
    def stable_angle(self) -> Optional[float]:
        return self.captured_angle or self.candidate_angle or self.current_angle

    @property
    def sample_count(self) -> int:
        return 1

    @property
    def stable_duration_sec(self) -> float:
        return self.elapsed_in_state


# ══════════════════════════════════════════════════════════════════════
#  Auto-Save Latch State
# ══════════════════════════════════════════════════════════════════════

class LatchState(Enum):
    """State of the AutoSaveService latch."""
    OPEN     = auto()   # Ready to fire on next STABLE transition
    LATCHED  = auto()   # Already fired; waiting for movement to re-arm



# ══════════════════════════════════════════════════════════════════════
#  User-Selectable Save Mode
# ══════════════════════════════════════════════════════════════════════

class SaveMode(Enum):
    """User-selectable saving mode."""
    AUTO   = auto()   # Saves automatically on sensor stability confirmation
    MANUAL = auto()   # Requires operator Enter key or SAVE DATA button trigger


class DirectionMode(Enum):
    """Flap sensor rotation / test direction."""
    CW  = "CW"   # Clockwise
    CCW = "CCW"  # Counterclockwise


# ══════════════════════════════════════════════════════════════════════
#  Pass / Fail Evaluation Status & Model
# ══════════════════════════════════════════════════════════════════════

class EvaluationStatus(Enum):
    """Setpoint comparison outcome."""
    PASS          = auto()   # Absolute error <= tolerance
    FAIL          = auto()   # Absolute error > tolerance
    NOT_EVALUATED = auto()   # Missing setpoint, missing tolerance, or unstable


@dataclass
class MeasurementEvaluation:
    """
    Mathematical evaluation comparing measured angle with a reference setpoint.

    Rules:
      - deviation = measured_angle - setpoint
      - absolute_error = abs(deviation)
      - PASS if absolute_error <= tolerance
      - FAIL if absolute_error > tolerance
      - Numeric evaluation uses exact float precision without premature rounding.
    """
    measured_angle: float
    setpoint: Optional[float] = None
    deviation: Optional[float] = None
    absolute_error: Optional[float] = None
    tolerance: Optional[float] = None
    status: EvaluationStatus = EvaluationStatus.NOT_EVALUATED
    reason: Optional[str] = None
    direction: str = "CW"
    step_number: Optional[int] = None

    @property
    def passed(self) -> bool:
        return self.status == EvaluationStatus.PASS

    @property
    def failed(self) -> bool:
        return self.status == EvaluationStatus.FAIL


# ══════════════════════════════════════════════════════════════════════
#  Save Operation Status
# ══════════════════════════════════════════════════════════════════════

class SaveStatus(Enum):
    """Outcome of an attempt to persist a MeasurementRecord."""
    SAVED    = auto()   # Successfully written and flushed to disk
    PENDING  = auto()   # Not yet attempted (in queue)
    RETRYING = auto()   # Failed at least once; retry in progress
    FAILED   = auto()   # Permanently failed after max retries


# ══════════════════════════════════════════════════════════════════════
#  Measurement Record & Acquisition Event
# ══════════════════════════════════════════════════════════════════════

@dataclass
class AcquisitionEvent:
    """
    Complete event tracking data container for an acquisition transaction.
    Guarantees full traceability:
    - Unique Session ID & Monotonic Event ID
    - Controller stop event timing & source
    - NAI API 8810A sensor reading timing & values
    - Validation outcome & Excel storage confirmation
    """
    session_id: str
    event_id: str
    trigger_source: str = "TWDA_MOTION"   # "TWDA_MOTION", "MANUAL", "INFERRED_MOTION"
    step_number: Optional[int] = None
    excel_row: Optional[int] = None
    direction: str = "CW"
    controller_stop_mono: float = 0.0
    controller_stop_time: str = ""
    sensor_read_mono: float = 0.0
    sensor_read_time: str = ""
    measured_angle: Optional[float] = None
    setpoint: Optional[float] = None
    tolerance: Optional[float] = None
    deviation: Optional[float] = None
    is_valid: bool = False
    validation_status: EvaluationStatus = EvaluationStatus.NOT_EVALUATED
    save_status: SaveStatus = SaveStatus.PENDING
    error: Optional[str] = None


@dataclass
class MeasurementRecord:
    """
    A single angle measurement destined for (or already in) the workbook.

    ``record_id`` is a unique token generated at creation time so that
    retry logic can guarantee idempotency — the same record is never
    written twice.
    """
    excel_row: int
    angle: float
    timestamp_str: str              # HH:MM:SS wall-clock at recording time
    is_simulator: bool = False
    record_id: str = field(default_factory=lambda: f"{time.monotonic():.6f}")
    generation_id: int = 0          # Tracks session/pause generation to reject stale jobs
    save_mode: str = "AUTO"         # "AUTO", "MANUAL", "EDIT"
    setpoint: Optional[float] = None
    tolerance: Optional[float] = None
    deviation: Optional[float] = None
    evaluation_status: EvaluationStatus = EvaluationStatus.NOT_EVALUATED
    direction: str = "CW"
    step_number: Optional[int] = None
    event: Optional[AcquisitionEvent] = None


# ══════════════════════════════════════════════════════════════════════
#  Save Operation Result
# ══════════════════════════════════════════════════════════════════════

@dataclass
class SaveResult:
    """
    Result returned by ``ExcelService.append_measurement()``.

    The GUI uses ``status`` to determine which log color / message to show.
    ``excel_row`` lets callers advance the row pointer only after success.
    """
    status: SaveStatus
    excel_row: Optional[int]        # Row actually written to (None if FAILED/PENDING)
    timestamp_str: str              # Timestamp of the write operation
    message: str                    # Human-readable outcome description
    error: Optional[str] = None     # Exception detail string if status != SAVED
    record_id: Optional[str] = None # Links back to the originating MeasurementRecord
    angle: Optional[float] = None   # The exact angle value that was recorded
    record: Optional[MeasurementRecord] = None # Full measurement record
    save_mode: str = "AUTO"         # "AUTO" or "MANUAL"
    direction: str = "CW"           # "CW" or "CCW"
    step_number: Optional[int] = None # Sequential step or test point number
    evaluation: Optional[MeasurementEvaluation] = None
    event: Optional[AcquisitionEvent] = None




