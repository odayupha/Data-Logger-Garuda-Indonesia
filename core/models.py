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
#  Stability State Machine States
# ══════════════════════════════════════════════════════════════════════

class StabilityStatus(Enum):
    """States of the StabilityDetector state machine."""
    WAITING_FOR_DATA = auto()   # No samples received yet
    MOVING           = auto()   # Angle is actively changing beyond tolerance
    SETTLING         = auto()   # Angle within tolerance, accumulating confirmation
    STABLE           = auto()   # Confirmed stable — auto-save may fire
    UNSTABLE         = auto()   # Prolonged oscillation / erratic jitter detected


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
#  Measurement Record
# ══════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════
#  Save Operation Result
# ══════════════════════════════════════════════════════════════════════

class SaveStatus(Enum):
    """Outcome of an attempt to persist a MeasurementRecord."""
    SAVED    = auto()   # Successfully written and flushed to disk
    PENDING  = auto()   # Not yet attempted (in queue)
    RETRYING = auto()   # Failed at least once; retry in progress
    FAILED   = auto()   # Permanently failed after max retries


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



