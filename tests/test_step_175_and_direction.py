"""
Unit and integration tests for:
1. Step 175 Transition and 0°/360° Boundary Stability
2. CW / CCW Direction Mode Selection & Tracking
3. Full Precision Angle Parsing in Serial Handler
4. Circular Angle Evaluation and Prominent PASS / FAIL Classification
"""
import pytest
from unittest.mock import MagicMock

from core.models import (
    MeasurementRecord,
    MeasurementEvaluation,
    DirectionMode,
    EvaluationStatus,
    SaveStatus,
    SaveResult,
    StabilityStatus,
)
from services.stability_detector import (
    StabilityDetector,
    angular_diff,
    angular_dist,
    unwrap_window,
)
from services.auto_save_service import AutoSaveService
from services.evaluation_service import EvaluationService
from services.excel_service import ExcelService
from controllers.measurement_controller import MeasurementController
from serial_handler import SerialReader


class TestCircularAngleMathAndStep175Stability:
    """Tests verifying circular math preventing lockup at Step 175-179 (around 0°/360°)."""

    def test_angular_diff_and_dist(self):
        # Normal small diff
        assert pytest.approx(angular_diff(1.59, 1.50), abs=1e-4) == 0.09
        assert pytest.approx(angular_dist(1.59, 1.50), abs=1e-4) == 0.09

        # Crossing 0° / 360° boundary
        # A: 0.05°, B: 359.95° -> actual physical separation is only 0.10°!
        # With linear abs(0.05 - 359.95) it would be 359.90° (which caused UNSTABLE lockup in Step 175-179)
        assert pytest.approx(angular_dist(0.05, 359.95), abs=1e-4) == 0.10
        assert pytest.approx(angular_diff(0.05, 359.95), abs=1e-4) == 0.10
        assert pytest.approx(angular_diff(359.95, 0.05), abs=1e-4) == -0.10

    def test_unwrap_window_across_zero(self):
        # Angles jittering around zero
        raw_angles = [0.08, 0.02, 359.96, 359.92, 0.04]
        unwrapped = unwrap_window(raw_angles, ref=0.08)
        # 359.96 unwrapped relative to 0.08 should be -0.04
        assert pytest.approx(unwrapped[0], abs=1e-4) == 0.08
        assert pytest.approx(unwrapped[1], abs=1e-4) == 0.02
        assert pytest.approx(unwrapped[2], abs=1e-4) == -0.04
        assert pytest.approx(unwrapped[3], abs=1e-4) == -0.08
        assert pytest.approx(unwrapped[4], abs=1e-4) == 0.04
        # Range of unwrapped is only 0.16°, NOT 359.94°!
        assert max(unwrapped) - min(unwrapped) <= 0.20

    def test_stability_detector_at_step_175_to_179(self):
        """Simulate stability detection at Step 175 (1.59°) and Step 178-179 (0.00° / 359.13°)."""
        detector = StabilityDetector(
            tolerance_deg=0.05,
            min_samples=5,
            min_duration_sec=0.5,
            max_gap_sec=0.5,
            rearm_threshold_deg=0.20,
        )

        # Step 175: stable at 1.59°
        t = 0.0
        res = None
        for _ in range(10):
            res = detector.update(1.59, monotonic_time=t)
            t += 0.1
        assert res.status == StabilityStatus.STABLE
        assert pytest.approx(res.stable_angle, abs=0.01) == 1.59

        # Move to Step 176: 1.30° (delta 0.29° > 0.20° rearm)
        res = detector.update(1.30, monotonic_time=t)
        t += 0.1
        assert res.status == StabilityStatus.MOVING

        for _ in range(10):
            res = detector.update(1.34, monotonic_time=t)
            t += 0.1
        assert res.status == StabilityStatus.STABLE

        # Move towards Step 178 (0.00°) and Step 179 (-0.87° or 359.13°)
        # Jitter right at 0° crossing: e.g. 0.02, 359.98, 0.01, 359.99
        # In the old code, this produced a spread of 359.97° and locked the system in UNSTABLE.
        t += 0.5
        for angle in [0.02, 359.98, 0.01, 359.99, 0.00, 359.99, 0.01, 0.00]:
            res = detector.update(angle, monotonic_time=t)
            t += 0.1

        # MUST BE STABLE, NOT UNSTABLE!
        assert res.status == StabilityStatus.STABLE


class TestAutoSaveStep174To176Transition:
    """Verifies that AutoSave transitions smoothly across Step 174 -> 175 -> 176 without skipping or duplicate."""

    def test_step_sequence_progression(self):
        mock_excel = MagicMock(spec=ExcelService)
        saved_records = []

        def fake_append(**kwargs):
            saved_records.append(kwargs)
            return SaveResult(
                status=SaveStatus.SAVED,
                excel_row=183 + len(saved_records),
                timestamp_str="12:00:00",
                message="Saved",
            )

        mock_excel.append_measurement.side_effect = fake_append

        detector = StabilityDetector(
            tolerance_deg=0.05,
            min_samples=5,
            min_duration_sec=0.5,
            max_gap_sec=0.5,
            rearm_threshold_deg=0.20,
        )

        auto_save = AutoSaveService(
            excel_service=mock_excel,
            stability_detector=detector,
            block_simulator_writes=False,
        )
        auto_save.direction = "CW"
        auto_save.enable()

        t = 0.0
        # Step 174: Target ~ 2.05°
        for _ in range(10):
            auto_save.feed(2.05, monotonic_time=t)
            t += 0.1
        assert len(saved_records) == 1
        assert saved_records[0]["angle"] == 2.05
        assert saved_records[0]["direction"] == "CW"

        # Arming prevents duplicate save at same position
        for _ in range(5):
            auto_save.feed(2.05, monotonic_time=t)
            t += 0.1
        assert len(saved_records) == 1

        # Move to Step 175: 1.59° (delta = 0.46° > rearm 0.20°)
        auto_save.feed(1.80, monotonic_time=t)
        t += 0.1

        # Reach Step 175: 1.59°
        for _ in range(10):
            auto_save.feed(1.59, monotonic_time=t)
            t += 0.1
        assert len(saved_records) == 2
        assert saved_records[1]["angle"] == 1.59
        assert saved_records[1]["direction"] == "CW"

        # Move to Step 176: 1.34° (delta = 0.25° > rearm 0.20°)
        auto_save.feed(1.45, monotonic_time=t)
        t += 0.1

        # Reach Step 176: 1.34°
        for _ in range(10):
            auto_save.feed(1.34, monotonic_time=t)
            t += 0.1
        assert len(saved_records) == 3
        assert saved_records[2]["angle"] == 1.34
        assert saved_records[2]["direction"] == "CW"


class TestCWAndCCWDirectionHandling:
    """Verifies CW/CCW direction controls, models, and excel assignment."""

    def test_direction_enum_and_models(self):
        assert DirectionMode.CW.value == "CW"
        assert DirectionMode.CCW.value == "CCW"

        rec_cw = MeasurementRecord(
            excel_row=185,
            angle=10.543,
            timestamp_str="12:00:00",
            step_number=1,
            direction="CW",
        )
        assert rec_cw.direction == "CW"
        assert rec_cw.angle == 10.543

        rec_ccw = MeasurementRecord(
            excel_row=186,
            angle=10.543,
            timestamp_str="12:00:00",
            step_number=183,
            direction="CCW",
        )
        assert rec_ccw.direction == "CCW"

    def test_measurement_controller_direction(self):
        mock_excel = MagicMock()
        mock_excel.is_loaded = True
        mock_eval = MagicMock()
        mock_repo = MagicMock()
        mock_repo.is_loaded = True
        controller = MeasurementController()
        controller._repo = mock_repo
        controller._excel = mock_excel
        controller._evaluation = mock_eval

        assert controller.direction == "CW"
        controller.set_direction("CCW")
        assert controller.direction == "CCW"

        # Save measurement passes CCW
        mock_excel.append_measurement.return_value = MagicMock(success=True)
        controller.save_measurement(angle=15.123, direction="CCW", step_number=185)
        mock_excel.append_measurement.assert_called_once()
        call_kwargs = mock_excel.append_measurement.call_args[1]
        assert call_kwargs["direction"] == "CCW"
        assert call_kwargs["step_number"] == 185
        assert pytest.approx(call_kwargs["angle"], abs=1e-4) == 15.123


class TestAnglePrecisionAndSerialHandler:
    """Verifies that full instrument precision is maintained without premature rounding."""

    def test_serial_handler_angle_precision(self):
        handler = SerialReader(port="COM1", baudrate=9600)
        # Simulate response with high precision float (e.g., 3-4 decimal places)
        raw_response = "1.589\r\n"
        parsed = handler._parse_angle(raw_response)
        assert parsed is not None
        assert pytest.approx(parsed, abs=1e-4) == 1.589

        # Negative angle
        neg_response = "-0.874\r\n"
        parsed_neg = handler._parse_angle(neg_response)
        assert parsed_neg is not None
        assert pytest.approx(parsed_neg, abs=1e-4) == -0.874

        # Padded response
        padded_response = "  359.1260  \r\n"
        parsed_padded = handler._parse_angle(padded_response)
        assert parsed_padded is not None
        assert pytest.approx(parsed_padded, abs=1e-4) == 359.126


class TestEvaluationAndPassFailBadge:
    """Verifies mathematical evaluation and PASS/FAIL classification at boundary and wrap."""

    def test_evaluation_pass_fail(self):
        eval_service = EvaluationService()

        # Normal PASS: target 1.59°, actual 1.62°, tol ±0.20° -> dev +0.03° -> PASS
        eval1 = eval_service.evaluate(measured_angle=1.62, setpoint=1.59, tolerance=0.20, direction="CW", step_number=175)
        assert eval1.passed is True
        assert eval1.status == EvaluationStatus.PASS
        assert pytest.approx(eval1.deviation, abs=1e-3) == 0.03
        assert eval1.direction == "CW"
        assert eval1.step_number == 175

        # Normal FAIL: target 1.59°, actual 1.95°, tol ±0.20° -> dev +0.36° -> FAIL
        eval2 = eval_service.evaluate(measured_angle=1.95, setpoint=1.59, tolerance=0.20, direction="CW", step_number=175)
        assert eval2.passed is False
        assert eval2.status == EvaluationStatus.FAIL
        assert pytest.approx(eval2.deviation, abs=1e-3) == 0.36

        # Step 179 Boundary: Setpoint -0.87°, Instrument reads 359.13° (circular wrap)
        # angular diff(359.13, -0.87) is 0.00°!
        eval_wrap = eval_service.evaluate(measured_angle=359.13, setpoint=-0.87, tolerance=0.20, direction="CW", step_number=179)
        assert eval_wrap.passed is True
        assert eval_wrap.status == EvaluationStatus.PASS
        assert pytest.approx(abs(eval_wrap.deviation), abs=1e-3) == 0.00

        # Wrap FAIL: Setpoint -0.87°, Instrument reads 357.00° -> dev ~ -2.13° -> FAIL
        eval_wrap_fail = eval_service.evaluate(measured_angle=357.00, setpoint=-0.87, tolerance=0.20, direction="CW", step_number=179)
        assert eval_wrap_fail.passed is False
        assert eval_wrap_fail.status == EvaluationStatus.FAIL
        assert pytest.approx(abs(eval_wrap_fail.deviation), abs=1e-2) == 2.13
