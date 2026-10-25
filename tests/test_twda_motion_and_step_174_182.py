"""
tests/test_twda_motion_and_step_174_182.py
──────────────────────────────────────────
Comprehensive verification test suite auditing:
1. TWDA Motion Trigger state transitions (MOVING → STOPPED primary trigger).
2. Step 174–182 (Rows 184–201 in CAM OUTPUT) including turnaround Step 182 (-6.00°) CW to CCW.
3. Position range 174°–182° (Rows 71–75 CW, Rows 310–314 CCW, Row 15 ACCURACY 180°).
4. Connection lifecycle (Connect / Disconnect / Reconnect, session generation isolation).
5. Sensor validation, anomaly rejection, and full acquisition event traceability.
"""

import math
import time
import pytest
from unittest.mock import MagicMock

from core.models import (
    AcquisitionEvent,
    ConnectionState,
    EvaluationStatus,
    MeasurementEvaluation,
    MeasurementRecord,
    MotionState,
    SaveResult,
    SaveStatus,
    TWDAMotionStatus,
)
from services.twda_motion_service import TWDAMotionTrigger, SimulatedTWDAController
from services.auto_save_service import AutoSaveService
from services.evaluation_service import EvaluationService
from services.excel_service import ExcelService
from controllers.measurement_controller import MeasurementController


# ══════════════════════════════════════════════════════════════════════
#  TEST CLASS 1: TWDA MOTION STATE TRANSITIONS
# ══════════════════════════════════════════════════════════════════════

class TestTWDAMotionStateTransitions:
    """Verifies that TWDA Motion status drives event-based acquisition strictly."""

    def test_normal_move_to_stop_transition(self):
        events_dispatched = []
        trigger = TWDAMotionTrigger(
            on_acquisition_event=lambda evt: events_dispatched.append(evt)
        )

        assert trigger.status == TWDAMotionStatus.UNKNOWN

        # Step 1: Controller starts moving
        evt1 = trigger.feed_controller_status(
            TWDAMotionStatus.MOVING, direction="CW", step_number=174, excel_row=184, setpoint=1.84
        )
        assert evt1 is None
        assert trigger.status == TWDAMotionStatus.MOVING
        assert len(events_dispatched) == 0

        # Step 2: Controller comes to a verified stop
        evt2 = trigger.feed_controller_status(
            TWDAMotionStatus.STOPPED, direction="CW", step_number=174, excel_row=184, setpoint=1.84
        )
        assert evt2 is not None
        assert evt2.event_id.startswith("EVT_")
        assert evt2.direction == "CW"
        assert evt2.step_number == 174
        assert evt2.excel_row == 184
        assert evt2.setpoint == 1.84
        assert evt2.controller_stop_mono > 0
        assert len(events_dispatched) == 1

    def test_repeated_stop_rejection(self):
        """Repeated STOPPED status without intervening MOVING must produce only ONE acquisition event."""
        events_dispatched = []
        trigger = TWDAMotionTrigger(
            on_acquisition_event=lambda evt: events_dispatched.append(evt)
        )

        # Move then stop
        trigger.feed_controller_status(TWDAMotionStatus.MOVING, direction="CW", step_number=1)
        evt_first = trigger.feed_controller_status(TWDAMotionStatus.STOPPED, direction="CW", step_number=1)
        assert evt_first is not None
        assert len(events_dispatched) == 1

        # Repeated STOPPED signals while resting must be rejected
        for _ in range(10):
            evt_dup = trigger.feed_controller_status(TWDAMotionStatus.STOPPED, direction="CW", step_number=1)
            assert evt_dup is None

        assert len(events_dispatched) == 1

    def test_unknown_and_error_safety(self):
        """Controller UNKNOWN or ERROR status must NEVER trigger an acquisition event."""
        events_dispatched = []
        trigger = TWDAMotionTrigger(
            on_acquisition_event=lambda evt: events_dispatched.append(evt)
        )

        trigger.feed_controller_status(TWDAMotionStatus.MOVING)
        assert trigger.status == TWDAMotionStatus.MOVING

        # Comm dropped / error
        evt_err = trigger.feed_controller_status(TWDAMotionStatus.ERROR)
        assert evt_err is None
        assert trigger.status == TWDAMotionStatus.ERROR

        # Unknown status
        evt_unk = trigger.feed_controller_status(TWDAMotionStatus.UNKNOWN)
        assert evt_unk is None
        assert trigger.status == TWDAMotionStatus.UNKNOWN

        assert len(events_dispatched) == 0

    def test_prolonged_motion_and_delayed_stop(self):
        """Prolonged movement must remain in MOVING without false timeout stop."""
        events_dispatched = []
        trigger = TWDAMotionTrigger(
            on_acquisition_event=lambda evt: events_dispatched.append(evt)
        )

        trigger.feed_controller_status(TWDAMotionStatus.MOVING)
        for _ in range(20):
            trigger.feed_controller_status(TWDAMotionStatus.MOVING)

        assert trigger.status == TWDAMotionStatus.MOVING
        assert len(events_dispatched) == 0

        # Eventual stop triggers exactly once
        evt = trigger.feed_controller_status(TWDAMotionStatus.STOPPED)
        assert evt is not None
        assert len(events_dispatched) == 1


# ══════════════════════════════════════════════════════════════════════
#  TEST CLASS 2: STEP 174–182 TURNAROUND & SEQUENCE AUDIT
# ══════════════════════════════════════════════════════════════════════

class TestStep174To182TurnaroundAndSequence:
    """
    Specifically targets the root causes identified in Steps 174–182:
    - Small angular step progression (1.84° -> 1.59° -> 1.34°).
    - Turnaround at Step 182 (-6.00°) CW -> Step 182 (-6.00°) CCW where sensor delta is 0°.
    """

    @pytest.fixture
    def setup_services(self):
        mock_excel = MagicMock(spec=ExcelService)
        saved_records = []

        def fake_append(angle: float, target_row: int = 11, is_simulator: bool = False, direction: str = "CW", step_number: int = None, **kwargs):
            rec = MeasurementRecord(
                excel_row=target_row,
                angle=angle,
                timestamp_str=time.strftime("%H:%M:%S"),
                direction=direction,
                step_number=step_number,
            )
            saved_records.append(rec)
            return SaveResult(
                status=SaveStatus.SAVED,
                excel_row=target_row,
                timestamp_str=rec.timestamp_str,
                message=f"Saved {angle:.2f}° to Row {target_row}",
                record=rec,
                angle=angle,
                direction=direction,
                step_number=step_number,
            )

        mock_excel.append_measurement.side_effect = fake_append

        auto_save = AutoSaveService(
            excel_service=mock_excel,
            block_simulator_writes=False,
            sync_save=True,
        )
        auto_save.enable()
        return auto_save, mock_excel, saved_records

    def test_cw_step_174_to_182_cam_output(self, setup_services):
        """
        Verify all 9 CW steps (174 through 182 / Rows 184 to 192) are recorded.
        Step 175 -> 176 has a very small delta (0.25°), which previously risked lockup.
        """
        auto_save, mock_excel, saved_records = setup_services

        cw_test_steps = [
            (174, 184, 1.84),
            (175, 185, 1.59),
            (176, 186, 1.34),  # Small delta = 0.25°
            (177, 187, 1.09),
            (178, 188, 0.84),
            (179, 189, 0.59),
            (180, 190, 0.34),
            (181, 191, 0.09),
            (182, 192, -6.00), # Turnaround step CW
        ]

        for step_no, row_no, sp in cw_test_steps:
            # Controller reports MOVING
            auto_save.notify_controller_motion(
                TWDAMotionStatus.MOVING, direction="CW", step_number=step_no, target_row=row_no
            )
            # Controller stops
            auto_save.notify_controller_motion(
                TWDAMotionStatus.STOPPED, direction="CW", step_number=step_no, target_row=row_no
            )
            # Sensor reading delivered
            res = auto_save.feed(sp, is_simulator=False)
            assert res is not None
            assert res.status == SaveStatus.SAVED
            assert res.excel_row == row_no

        assert len(saved_records) == 9
        for i, (step_no, row_no, sp) in enumerate(cw_test_steps):
            assert saved_records[i].excel_row == row_no
            assert saved_records[i].step_number == step_no
            assert saved_records[i].direction == "CW"
            assert pytest.approx(saved_records[i].angle, abs=1e-3) == sp

    def test_turnaround_step_182_cw_to_ccw_no_skip(self, setup_services):
        """
        CRITICAL ROOT CAUSE TEST:
        Step 182 CW (Row 192, Setpoint = -6.00°) immediately followed by
        Step 182 CCW (Row 193, Setpoint = -6.00°).
        Sensor delta between them is 0.00°.
        With TWDA Motion Trigger, Row 193 must NOT be skipped!
        """
        auto_save, mock_excel, saved_records = setup_services

        # 1. Execute Row 192 (Step 182 CW, -6.00°)
        auto_save.notify_controller_motion(
            TWDAMotionStatus.MOVING, direction="CW", step_number=182, target_row=192
        )
        auto_save.notify_controller_motion(
            TWDAMotionStatus.STOPPED, direction="CW", step_number=182, target_row=192
        )
        res_cw = auto_save.feed(-6.00, is_simulator=False)
        assert res_cw is not None
        assert res_cw.excel_row == 192
        assert res_cw.direction == "CW"

        # 2. Advance to Row 193 (Step 182 CCW, -6.00°)
        # Sensor remains at -6.00°
        auto_save.notify_controller_motion(
            TWDAMotionStatus.MOVING, direction="CCW", step_number=182, target_row=193
        )
        auto_save.notify_controller_motion(
            TWDAMotionStatus.STOPPED, direction="CCW", step_number=182, target_row=193
        )
        res_ccw = auto_save.feed(-6.00, is_simulator=False)

        # MUST SAVE ROW 193 SUCCESSFULLY
        assert res_ccw is not None, "Row 193 (Turnaround CCW) must NOT be skipped!"
        assert res_ccw.excel_row == 193
        assert res_ccw.direction == "CCW"
        assert len(saved_records) == 2

    def test_ccw_step_182_to_174_cam_output(self, setup_services):
        """
        Verify all 9 CCW steps (182 down to 174 / Rows 193 to 201) are recorded.
        """
        auto_save, mock_excel, saved_records = setup_services

        ccw_test_steps = [
            (182, 193, -6.00), # Turnaround step CCW
            (181, 194, 0.09),
            (180, 195, 0.34),
            (179, 196, 0.59),
            (178, 197, 0.84),
            (177, 198, 1.09),
            (176, 199, 1.34),  # Small delta = 0.25°
            (175, 200, 1.59),
            (174, 201, 1.84),
        ]

        for step_no, row_no, sp in ccw_test_steps:
            auto_save.notify_controller_motion(
                TWDAMotionStatus.MOVING, direction="CCW", step_number=step_no, target_row=row_no
            )
            auto_save.notify_controller_motion(
                TWDAMotionStatus.STOPPED, direction="CCW", step_number=step_no, target_row=row_no
            )
            res = auto_save.feed(sp, is_simulator=False)
            assert res is not None
            assert res.status == SaveStatus.SAVED
            assert res.excel_row == row_no

        assert len(saved_records) == 9
        for i, (step_no, row_no, sp) in enumerate(ccw_test_steps):
            assert saved_records[i].excel_row == row_no
            assert saved_records[i].step_number == step_no
            assert saved_records[i].direction == "CCW"


# ══════════════════════════════════════════════════════════════════════
#  TEST CLASS 3: POSITION RANGE 174°–182° & CIRCULAR 180° EVALUATION
# ══════════════════════════════════════════════════════════════════════

class TestPositionRange174To182CrossingBoundary:
    """
    Tests evaluation and acquisition across the 180° boundary and
    the angle setpoint range 174°–182° (Rows 71–75 CW, Rows 310–314 CCW).
    """

    def test_circular_boundary_evaluation_at_180(self):
        """
        At 180°, signed sensors might return -180.00° while workbook has 180.00°.
        Verify that evaluation wraps shortest angular distance (deviation = 0.00° and PASS).
        """
        eval_service = EvaluationService()

        # Case 1: sensor reports -180.00°, setpoint is 180.00°
        eval1 = eval_service.evaluate(measured_angle=-180.00, setpoint=180.00, tolerance=1.50)
        assert eval1.status == EvaluationStatus.PASS
        assert pytest.approx(eval1.deviation, abs=1e-3) == 0.00
        assert pytest.approx(eval1.absolute_error, abs=1e-3) == 0.00

        # Case 2: sensor reports 179.95°, setpoint is 180.00°
        eval2 = eval_service.evaluate(measured_angle=179.95, setpoint=180.00, tolerance=1.50)
        assert eval2.status == EvaluationStatus.PASS
        assert pytest.approx(eval2.deviation, abs=1e-3) == -0.05

        # Case 3: sensor reports -179.90°, setpoint is 180.00°
        eval3 = eval_service.evaluate(measured_angle=-179.90, setpoint=180.00, tolerance=1.50)
        assert eval3.status == EvaluationStatus.PASS
        assert pytest.approx(eval3.deviation, abs=1e-3) == 0.10

    def test_position_range_174_to_182_cw_and_ccw(self):
        """
        CAM OUTPUT Steps 61–65:
        CW  Rows 71–75: 174.00°, 176.00°, 178.00°, 180.00°, 182.00°
        CCW Rows 310–314: 182.00°, 180.00°, 178.00°, 176.00°, 174.00°
        """
        eval_service = EvaluationService()

        # CW Setpoints
        cw_setpoints = [174.00, 176.00, 178.00, 180.00, 182.00]
        for sp in cw_setpoints:
            eval_cw = eval_service.evaluate(measured_angle=sp + 0.15, setpoint=sp, tolerance=1.50, direction="CW")
            assert eval_cw.status == EvaluationStatus.PASS
            assert pytest.approx(eval_cw.deviation, abs=1e-3) == 0.15

        # CCW Setpoints
        ccw_setpoints = [182.00, 180.00, 178.00, 176.00, 174.00]
        for sp in ccw_setpoints:
            eval_ccw = eval_service.evaluate(measured_angle=sp - 0.12, setpoint=sp, tolerance=1.50, direction="CCW")
            assert eval_ccw.status == EvaluationStatus.PASS
            assert pytest.approx(eval_ccw.deviation, abs=1e-3) == -0.12


# ══════════════════════════════════════════════════════════════════════
#  TEST CLASS 4: CONNECTION LIFECYCLE & EVENT SYNCHRONIZATION
# ══════════════════════════════════════════════════════════════════════

class TestConnectionLifecycleAndSynchronization:
    """Verifies connection lifecycle states, session isolation, and event metadata."""

    def test_connection_state_enum_and_lifecycle(self):
        assert ConnectionState.DISCONNECTED.value == "DISCONNECTED"
        assert ConnectionState.CONNECTING.value == "CONNECTING"
        assert ConnectionState.CONNECTED.value == "CONNECTED"
        assert ConnectionState.DISCONNECTING.value == "DISCONNECTING"
        assert ConnectionState.CONNECTION_ERROR.value == "CONNECTION ERROR"

    def test_reconnect_generates_new_session_id(self):
        """Reconnecting must produce a new unique session ID to isolate old events."""
        trigger = TWDAMotionTrigger()
        session1 = trigger.session_id
        assert session1.startswith("SES_")

        # Disconnect & reset
        trigger.reset_session()
        session2 = trigger.session_id
        assert session2.startswith("SES_")
        assert session1 != session2

        # Verify event counter reset
        evt = trigger.feed_controller_status(TWDAMotionStatus.MOVING)
        assert evt is None
        evt_stop = trigger.feed_controller_status(TWDAMotionStatus.STOPPED)
        assert evt_stop is not None
        assert evt_stop.session_id == session2
        assert evt_stop.event_id == "EVT_0001"

    def test_sensor_timeout_and_nan_sample_rejection(self):
        """Sensor returning NaN or Inf must not produce a valid saved measurement."""
        mock_excel = MagicMock(spec=ExcelService)
        auto_save = AutoSaveService(excel_service=mock_excel, block_simulator_writes=False, sync_save=True)
        auto_save.enable()

        # Trigger stop
        auto_save.notify_controller_motion(TWDAMotionStatus.MOVING, target_row=11)
        auto_save.notify_controller_motion(TWDAMotionStatus.STOPPED, target_row=11)

        # Feed NaN
        res_nan = auto_save.feed(float("nan"))
        assert res_nan is None
        mock_excel.append_measurement.assert_not_called()

        # Feed Inf
        res_inf = auto_save.feed(float("inf"))
        assert res_inf is None
        mock_excel.append_measurement.assert_not_called()

    def test_full_traceability_metadata_on_saved_event(self):
        """Verifies that every saved record contains complete acquisition event metadata."""
        mock_excel = MagicMock(spec=ExcelService)
        saved_records = []

        def capture_record(angle: float, target_row: int = 11, is_simulator: bool = False, direction: str = "CW", step_number: int = None, **kwargs):
            rec = MeasurementRecord(
                excel_row=target_row,
                angle=angle,
                timestamp_str=time.strftime("%H:%M:%S"),
                direction=direction,
                step_number=step_number,
            )
            saved_records.append(rec)
            return SaveResult(
                status=SaveStatus.SAVED,
                excel_row=target_row,
                timestamp_str=rec.timestamp_str,
                message="OK",
                record=rec,
                angle=angle,
                direction=direction,
                step_number=step_number,
            )

        mock_excel.append_measurement.side_effect = capture_record

        auto_save = AutoSaveService(excel_service=mock_excel, block_simulator_writes=False, sync_save=True)
        auto_save.enable()

        auto_save.notify_controller_motion(
            TWDAMotionStatus.MOVING, direction="CW", step_number=174, target_row=184
        )
        auto_save.notify_controller_motion(
            TWDAMotionStatus.STOPPED, direction="CW", step_number=174, target_row=184
        )
        res = auto_save.feed(1.84, is_simulator=False)

        assert res.status == SaveStatus.SAVED
        assert res.event is not None
        evt = res.event

        # Check all required traceability fields
        assert evt.session_id.startswith("SES_")
        assert evt.event_id == "EVT_0001"
        assert evt.trigger_source == "TWDA_MOTION"
        assert evt.step_number == 174
        assert evt.excel_row == 184
        assert evt.direction == "CW"
        assert evt.controller_stop_mono > 0
        assert evt.sensor_read_mono > 0
        assert evt.controller_stop_time != ""
        assert evt.sensor_read_time != ""
        assert pytest.approx(evt.measured_angle, abs=1e-3) == 1.84
        assert evt.is_valid is True
        assert evt.save_status == SaveStatus.SAVED
