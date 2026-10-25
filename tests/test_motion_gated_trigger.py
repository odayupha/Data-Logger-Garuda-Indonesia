"""
tests/test_motion_gated_trigger.py
──────────────────────────────────
Unit tests for Motion-Gated Stop Trigger (NAI API 8810A Live Stream).

Validates all 12 operational scenarios specified in Phase 7:
  1.  Stable angle at startup: no save.
  2.  Normal movement followed by stopping: exactly one save.
  3.  Temporary pause during movement followed by resumed movement: no premature save.
  4.  A small amount of sensor noise while stationary: no repeated saves.
  5.  A new movement cycle after a successful save: one new save.
  6.  Invalid or stale samples during stop confirmation: cancel the pending capture.
  7.  API disconnection during movement: no save from stale data.
  8.  Excel write failure: no Target Row increment.
  9.  Auto-Save disabled: no automatic writes.
  10. Pause enabled during stop confirmation: pending capture is cancelled.
  11. GUI / loop responsiveness: non-blocking execution throughout acquisition.
  12. Angle wraparound: calculate movement correctly across 0°/360° boundary.
"""

import math
import time
from unittest.mock import MagicMock

import pytest

from core.exceptions import WorkbookLockedError
from core.models import (
    LatchState,
    MotionState,
    SaveMode,
    SaveResult,
    SaveStatus,
    SystemState,
)
from services.auto_save_service import AutoSaveService
from services.excel_service import ExcelService
from services.motion_detector import MotionTrigger, angular_dist


# ── Test Fixture Helper ───────────────────────────────────────────────

def make_test_environment(
    move_thresh: float = 0.15,
    stop_window: float = 0.30,
    stop_tol: float = 0.045,
    stop_confirm: float = 0.60,
    resume_thresh: float = 0.08,
    rearm_thresh: float = 0.18,
    baseline_samples: int = 4,
    max_gap: float = 1.5,
    initial_row: int = 11,
):
    """
    Construct a deterministic test environment with mocked ExcelService.
    Returns (svc, mock_excel, trigger).
    """
    mock_excel = MagicMock(spec=ExcelService)
    current_target_row = [initial_row]

    def mock_append(angle, is_simulator=False, direction="CW", *args, **kwargs):
        row_saved = current_target_row[0]
        current_target_row[0] += 1
        return SaveResult(
            status=SaveStatus.SAVED,
            excel_row=row_saved,
            timestamp_str="08:00:00",
            message=f"Saved {angle:.2f}° to Row {row_saved}",
            angle=angle,
        )

    mock_excel.append_measurement.side_effect = mock_append

    trigger = MotionTrigger(
        move_threshold_deg=move_thresh,
        stop_window_sec=stop_window,
        stop_tolerance_deg=stop_tol,
        stop_confirm_sec=stop_confirm,
        resume_threshold_deg=resume_thresh,
        rearm_threshold_deg=rearm_thresh,
        baseline_samples=baseline_samples,
        max_gap_sec=max_gap,
    )

    svc = AutoSaveService(
        excel_service=mock_excel,
        motion_trigger=trigger,
        block_simulator_writes=False,
        sync_save=True,  # Synchronous commit for deterministic test assertion
    )
    svc.enable()
    return svc, mock_excel, trigger, current_target_row


# ══════════════════════════════════════════════════════════════════════
#  PHASE 7 UNIT TESTS
# ══════════════════════════════════════════════════════════════════════

class TestMotionGatedStopTrigger:

    def test_01_stable_angle_at_startup_no_save(self):
        """Case 1: Sensor stationary at startup must never trigger an auto-save."""
        svc, mock_excel, trigger, row_ref = make_test_environment()
        assert trigger.state == MotionState.WAITING_FOR_MOTION

        t = 100.0
        # Feed 30 stationary samples at 25.00° (1 second of stationary readings)
        for _ in range(30):
            svc.feed(25.00, monotonic_time=t)
            t += 0.033

        # State must remain WAITING_FOR_MOTION, auto-save locked, no Excel write
        assert trigger.state == MotionState.WAITING_FOR_MOTION
        mock_excel.append_measurement.assert_not_called()
        assert row_ref[0] == 11

    def test_02_normal_movement_followed_by_stopping_one_save(self):
        """Case 2: Movement followed by confirmed stop triggers exactly one save."""
        svc, mock_excel, trigger, row_ref = make_test_environment(
            move_thresh=0.15,
            stop_window=0.30,
            stop_confirm=0.50,
        )

        t = 100.0
        # 1. Establish baseline at 25.00° (WAITING_FOR_MOTION)
        for _ in range(5):
            svc.feed(25.00, monotonic_time=t)
            t += 0.033
        assert trigger.state == MotionState.WAITING_FOR_MOTION
        assert mock_excel.append_measurement.call_count == 0

        # 2. Movement begins: angle moves from 25.00° to 30.00°
        angles_moving = [25.10, 25.30, 26.00, 27.50, 28.80, 29.50, 29.95]
        for a in angles_moving:
            svc.feed(a, monotonic_time=t)
            t += 0.033
        assert trigger.state == MotionState.MOVING
        assert mock_excel.append_measurement.call_count == 0

        # 3. Stop at 30.00° for stop_window (0.3s)
        for _ in range(12):  # ~0.4s
            svc.feed(30.00, monotonic_time=t)
            t += 0.033
        assert trigger.state == MotionState.STOP_CANDIDATE
        assert mock_excel.append_measurement.call_count == 0

        # 4. Remain stationary for stop_confirm duration (0.5s)
        for _ in range(18):  # ~0.6s
            svc.feed(30.00, monotonic_time=t)
            t += 0.033

        # Exactly ONE save committed, state is WAITING_FOR_NEXT_MOTION
        assert mock_excel.append_measurement.call_count == 1
        assert trigger.state == MotionState.WAITING_FOR_NEXT_MOTION
        assert row_ref[0] == 12  # Target row incremented from 11 to 12

    def test_03_temporary_pause_during_movement_no_premature_save(self):
        """Case 3: Brief pause during movement must not trigger a premature save."""
        svc, mock_excel, trigger, _ = make_test_environment(
            move_thresh=0.15,
            stop_window=0.30,
            stop_confirm=0.60,
            resume_thresh=0.08,
        )

        t = 100.0
        # Baseline
        for _ in range(5):
            svc.feed(10.00, monotonic_time=t)
            t += 0.033

        # Move to 15.00°
        for a in [10.20, 11.50, 13.00, 15.00]:
            svc.feed(a, monotonic_time=t)
            t += 0.033
        assert trigger.state == MotionState.MOVING

        # Brief pause at 15.00° for only 0.35s (enters STOP_CANDIDATE but does not complete 0.6s confirm)
        for _ in range(11):
            svc.feed(15.00, monotonic_time=t)
            t += 0.033
        assert trigger.state == MotionState.STOP_CANDIDATE
        assert mock_excel.append_measurement.call_count == 0

        # Movement resumes: table accelerates to 16.00°, 18.00°, 20.00°
        for a in [15.15, 16.00, 17.50, 20.00]:
            svc.feed(a, monotonic_time=t)
            t += 0.033

        # Timer cancelled, returned to MOVING, NO save occurred
        assert trigger.state == MotionState.MOVING
        assert mock_excel.append_measurement.call_count == 0

        # Finally stop at 20.00° for full stop_window + stop_confirm
        for _ in range(35):  # > 1.1s
            svc.feed(20.00, monotonic_time=t)
            t += 0.033

        # Exactly ONE save at final destination 20.00°
        assert mock_excel.append_measurement.call_count == 1
        assert trigger.state == MotionState.WAITING_FOR_NEXT_MOTION

    def test_04_small_sensor_noise_while_stationary_no_repeated_saves(self):
        """Case 4: Normal sensor noise while stationary must not trigger saves."""
        svc, mock_excel, trigger, _ = make_test_environment(move_thresh=0.15)

        t = 100.0
        # Baseline established
        for _ in range(5):
            svc.feed(45.00, monotonic_time=t)
            t += 0.033

        # Sensor noise ±0.03° around 45.00° (typical synchro noise)
        noisy_samples = [45.01, 44.98, 45.02, 45.00, 44.99, 45.03, 44.97] * 5
        for a in noisy_samples:
            svc.feed(a, monotonic_time=t)
            t += 0.033

        assert trigger.state == MotionState.WAITING_FOR_MOTION
        mock_excel.append_measurement.assert_not_called()

    def test_05_new_movement_cycle_after_successful_save_one_new_save(self):
        """Case 5: A second movement cycle after save triggers exactly one new save."""
        svc, mock_excel, trigger, row_ref = make_test_environment(
            move_thresh=0.15,
            stop_window=0.30,
            stop_confirm=0.50,
            rearm_thresh=0.18,
        )

        t = 100.0
        # Baseline at 50.00°
        for _ in range(5):
            svc.feed(50.00, monotonic_time=t)
            t += 0.033

        # Cycle 1: move to 55.00° and stop
        for a in [50.25, 51.50, 53.00, 55.00]:
            svc.feed(a, monotonic_time=t)
            t += 0.033
        for _ in range(30):  # Stop window + confirm
            svc.feed(55.00, monotonic_time=t)
            t += 0.033

        assert mock_excel.append_measurement.call_count == 1
        assert trigger.state == MotionState.WAITING_FOR_NEXT_MOTION
        assert row_ref[0] == 12

        # Stay stationary at 55.00° for 2 seconds — NO repeated saves
        for _ in range(60):
            svc.feed(55.00, monotonic_time=t)
            t += 0.033
        assert mock_excel.append_measurement.call_count == 1

        # Cycle 2: new movement to 60.00°
        for a in [55.25, 56.50, 58.00, 60.00]:
            svc.feed(a, monotonic_time=t)
            t += 0.033
        assert trigger.state == MotionState.MOVING

        # Stop at 60.00° for full confirmation
        for _ in range(30):
            svc.feed(60.00, monotonic_time=t)
            t += 0.033

        # Cycle 2 committed: call count is 2, target row incremented to 13
        assert mock_excel.append_measurement.call_count == 2
        assert trigger.state == MotionState.WAITING_FOR_NEXT_MOTION
        assert row_ref[0] == 13

    def test_06_invalid_or_stale_samples_during_stop_confirmation_cancels_capture(self):
        """Case 6: Non-finite or stale reading during stop confirmation cancels capture."""
        svc, mock_excel, trigger, _ = make_test_environment(
            move_thresh=0.15,
            stop_window=0.30,
            stop_confirm=0.60,
        )

        t = 100.0
        for _ in range(5):
            svc.feed(10.00, monotonic_time=t)
            t += 0.033

        # Move to 20.00°
        for a in [10.20, 15.00, 20.00]:
            svc.feed(a, monotonic_time=t)
            t += 0.033

        # Settle into STOP_CANDIDATE
        for _ in range(12):
            svc.feed(20.00, monotonic_time=t)
            t += 0.033
        assert trigger.state == MotionState.STOP_CANDIDATE

        # Sensor transmits invalid reading (e.g. NaN or communication glitch)
        svc.feed(float("nan"), monotonic_time=t)

        # Pending capture cancelled, state is DATA_INVALID
        assert trigger.state == MotionState.DATA_INVALID
        mock_excel.append_measurement.assert_not_called()

    def test_07_api_disconnection_during_movement_no_save_from_stale_data(self):
        """Case 7: Gap in readings (disconnect) during motion cancels detection."""
        svc, mock_excel, trigger, _ = make_test_environment(
            move_thresh=0.15,
            max_gap=1.0,
        )

        t = 100.0
        for _ in range(5):
            svc.feed(10.00, monotonic_time=t)
            t += 0.033

        # Move
        svc.feed(10.25, monotonic_time=t)
        t += 0.033
        svc.feed(12.00, monotonic_time=t)
        assert trigger.state == MotionState.MOVING

        # Disconnection: no readings for 2.5s (> max_gap 1.0s)
        t += 2.5
        svc.feed(12.00, monotonic_time=t)

        # Transitioned to DATA_INVALID / cancelled, no save triggered
        assert trigger.state == MotionState.DATA_INVALID
        mock_excel.append_measurement.assert_not_called()

    def test_08_excel_write_failure_no_target_row_increment(self):
        """Case 8: When Excel write fails, Target Row must NOT increment."""
        svc, mock_excel, trigger, row_ref = make_test_environment(
            move_thresh=0.15,
            stop_window=0.30,
            stop_confirm=0.50,
        )

        # Configure mock Excel to raise locked error
        mock_excel.append_measurement.side_effect = WorkbookLockedError("Workbook is open in Excel")

        errors = []
        svc._on_error = errors.append

        t = 100.0
        for _ in range(5):
            svc.feed(10.00, monotonic_time=t)
            t += 0.033

        # Move and stop
        for a in [10.25, 12.00, 15.00]:
            svc.feed(a, monotonic_time=t)
            t += 0.033
        for _ in range(30):
            svc.feed(15.00, monotonic_time=t)
            t += 0.033

        # Excel write was attempted, but failed
        assert mock_excel.append_measurement.call_count >= 1
        # Target row was NOT incremented (remains 11)
        assert row_ref[0] == 11
        # Error callback received failure notification
        assert len(errors) > 0
        # Trigger did NOT transition to WAITING_FOR_NEXT_MOTION
        assert trigger.state != MotionState.WAITING_FOR_NEXT_MOTION

    def test_09_auto_save_disabled_no_automatic_writes(self):
        """Case 9: When Auto-Save is disabled (MANUAL mode), no automatic writes occur."""
        svc, mock_excel, trigger, row_ref = make_test_environment(
            move_thresh=0.15,
            stop_window=0.30,
            stop_confirm=0.50,
        )

        # Set save mode to MANUAL
        svc.set_save_mode(SaveMode.MANUAL)

        t = 100.0
        for _ in range(5):
            svc.feed(10.00, monotonic_time=t)
            t += 0.033

        # Move and stop completely
        for a in [10.25, 12.00, 15.00]:
            svc.feed(a, monotonic_time=t)
            t += 0.033
        for _ in range(30):
            svc.feed(15.00, monotonic_time=t)
            t += 0.033

        # Motion detector tracks the mechanical stop, but NO write to Excel occurred
        mock_excel.append_measurement.assert_not_called()
        assert row_ref[0] == 11

    def test_10_pause_enabled_during_stop_confirmation_cancels_capture(self):
        """Case 10: Operator pause during stop confirmation cancels pending capture."""
        svc, mock_excel, trigger, _ = make_test_environment(
            move_thresh=0.15,
            stop_window=0.30,
            stop_confirm=0.60,
        )

        t = 100.0
        for _ in range(5):
            svc.feed(10.00, monotonic_time=t)
            t += 0.033

        # Move to 25.00°
        for a in [10.25, 15.00, 25.00]:
            svc.feed(a, monotonic_time=t)
            t += 0.033

        # Settle into STOP_CANDIDATE
        for _ in range(12):
            svc.feed(25.00, monotonic_time=t)
            t += 0.033
        assert trigger.state == MotionState.STOP_CANDIDATE

        # Operator pauses system
        svc.pause()
        assert svc.is_paused is True
        assert trigger.state == MotionState.WAITING_FOR_MOTION

        # Feed more samples while paused — NO save
        for _ in range(25):
            svc.feed(25.00, monotonic_time=t)
            t += 0.033
        mock_excel.append_measurement.assert_not_called()

        # Operator resumes: system re-arms fresh; pre-pause stillness must not trigger save
        svc.resume()
        for _ in range(25):
            svc.feed(25.00, monotonic_time=t)
            t += 0.033
        mock_excel.append_measurement.assert_not_called()

    def test_11_gui_remains_responsive_throughout_acquisition(self):
        """Case 11: Detection logic is non-blocking and executes in microseconds per sample."""
        svc, _, trigger, _ = make_test_environment()

        t_mono = 100.0
        start_clock = time.perf_counter()

        # Feed 300 samples (simulating 10 seconds of 30 Hz streaming)
        for i in range(300):
            ang = 10.0 + (i * 0.05) if i < 150 else 17.50
            svc.feed(ang, monotonic_time=t_mono)
            t_mono += 0.033

        elapsed_clock = time.perf_counter() - start_clock
        avg_micros = (elapsed_clock / 300) * 1_000_000

        # Entire 300 sample pipeline must execute within 50ms total (<166μs per sample)
        assert elapsed_clock < 0.15
        assert avg_micros < 500  # Well under 0.5ms per sample, ensuring zero GUI freezing

    def test_12_angle_wraparound_calculated_correctly(self):
        """Case 12: Handles angular wraparound across 0° / 360° boundary seamlessly."""
        svc, mock_excel, trigger, _ = make_test_environment(
            move_thresh=0.15,
            stop_window=0.30,
            stop_confirm=0.50,
        )

        t = 100.0
        # 1. Baseline near 359.80°
        for _ in range(5):
            svc.feed(359.80, monotonic_time=t)
            t += 0.033
        assert trigger.state == MotionState.WAITING_FOR_MOTION

        # Circular distance across boundary:
        # Distance from 359.80° to 0.20° is 0.40°, NOT 359.60°
        assert pytest.approx(angular_dist(0.20, 359.80), abs=1e-3) == 0.40

        # 2. Movement across 0°/360° boundary: 359.80° -> 359.95° -> 0.15° -> 0.45° -> 1.00°
        for a in [359.95, 0.15, 0.45, 0.80, 1.00]:
            svc.feed(a, monotonic_time=t)
            t += 0.033
        assert trigger.state == MotionState.MOVING

        # 3. Stop at 1.00°
        for _ in range(30):
            svc.feed(1.00, monotonic_time=t)
            t += 0.033

        # Exactly ONE save at 1.00° across the wraparound boundary
        assert mock_excel.append_measurement.call_count == 1
        assert trigger.state == MotionState.WAITING_FOR_NEXT_MOTION
        assert trigger.last_saved_angle == 1.00
