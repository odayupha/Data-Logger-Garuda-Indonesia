"""
tests/test_auto_save_service.py
────────────────────────────────
Unit tests for AutoSaveService.

ExcelService is mocked so tests run without any disk I/O.
StabilityDetector is used with injected timestamps for determinism.
"""

import pytest
from unittest.mock import MagicMock, call

from services.auto_save_service import AutoSaveService
from services.stability_detector import StabilityDetector
from services.excel_service import ExcelService
from core.models import (
    LatchState,
    MeasurementRecord,
    SaveResult,
    SaveStatus,
    StabilityStatus,
)
from core.exceptions import WorkbookLockedError


# ── Fixture helpers ────────────────────────────────────────────────────

def make_service(
    block_sim: bool = True,
    **detector_kwargs,
) -> tuple[AutoSaveService, MagicMock]:
    """
    Return an (AutoSaveService, mock_excel) pair.

    The mock_excel.append_measurement returns a successful SaveResult
    by default.
    """
    mock_excel = MagicMock(spec=ExcelService)
    mock_excel.append_measurement.return_value = SaveResult(
        status=SaveStatus.SAVED,
        excel_row=11,
        timestamp_str="09:00:00",
        message="Saved 25.00° → G11",
    )

    defaults = dict(
        tolerance_deg=0.05,
        min_samples=5,
        min_duration_sec=1.0,
        max_gap_sec=0.5,
        rearm_threshold_deg=0.20,
    )
    defaults.update(detector_kwargs)
    detector = StabilityDetector(**defaults)

    svc = AutoSaveService(
        excel_service=mock_excel,
        stability_detector=detector,
        block_simulator_writes=block_sim,
    )
    svc.enable()
    return svc, mock_excel


def feed_n(
    svc: AutoSaveService,
    angle: float,
    n: int = 20,
    start_t: float = 0.0,
    step: float = 0.3,
    is_simulator: bool = False,
) -> float:
    """Feed n samples with incrementing timestamps and return ending time."""
    t = start_t
    for _ in range(n):
        svc.feed(angle, is_simulator=is_simulator, monotonic_time=t)
        t += step
    return t


# ══════════════════════════════════════════════════════════════════════
#  Enable / Disable
# ══════════════════════════════════════════════════════════════════════

class TestEnableDisable:
    def test_feed_does_nothing_when_disabled(self):
        svc, mock_excel = make_service()
        svc.disable()
        result = svc.feed(25.0, monotonic_time=0.0)
        assert result is None
        mock_excel.append_measurement.assert_not_called()

    def test_enabled_initially_after_enable(self):
        svc, _ = make_service()
        assert svc.enabled is True

    def test_latch_is_open_after_enable(self):
        svc, _ = make_service()
        assert svc.latch_state == LatchState.OPEN

    def test_reset_reopens_latch(self):
        svc, _ = make_service()
        svc._latch = LatchState.LATCHED
        svc.reset()
        assert svc.latch_state == LatchState.OPEN


# ══════════════════════════════════════════════════════════════════════
#  Latch behaviour via feed()
# ══════════════════════════════════════════════════════════════════════

class TestLatch:
    def test_save_fires_on_stable_with_open_latch(self):
        svc, mock_excel = make_service()
        feed_n(svc, 25.0, n=20)
        assert mock_excel.append_measurement.call_count == 1

    def test_latch_prevents_second_save_without_movement(self):
        svc, mock_excel = make_service()
        feed_n(svc, 25.0, n=30)
        assert mock_excel.append_measurement.call_count == 1

    def test_latch_state_becomes_latched_after_save(self):
        svc, _ = make_service()
        feed_n(svc, 25.0, n=20)
        assert svc.latch_state == LatchState.LATCHED


# ══════════════════════════════════════════════════════════════════════
#  Re-arm after movement
# ══════════════════════════════════════════════════════════════════════

class TestReArm:
    def test_movement_opens_latch(self):
        svc, _ = make_service()
        # Stabilize
        t = feed_n(svc, 25.0, n=20, start_t=0.0)
        assert svc.latch_state == LatchState.LATCHED
        # Move beyond re-arm threshold (rearm_threshold_deg=0.20)
        svc.feed(25.0 + 0.25, monotonic_time=t)
        assert svc.latch_state == LatchState.OPEN

    def test_second_stable_position_triggers_second_save(self):
        svc, mock_excel = make_service()
        # First stable
        t = feed_n(svc, 25.0, n=20, start_t=0.0)
        assert mock_excel.append_measurement.call_count == 1

        # Move to new position
        for i in range(5):
            svc.feed(30.0 + i * 0.5, monotonic_time=t)
            t += 0.3

        # Second stable at 32.0
        feed_n(svc, 32.0, n=20, start_t=t)
        assert mock_excel.append_measurement.call_count == 2


# ══════════════════════════════════════════════════════════════════════
#  Simulator block
# ══════════════════════════════════════════════════════════════════════

class TestSimulatorBlock:
    def test_simulator_writes_blocked_when_configured(self):
        svc, mock_excel = make_service(block_sim=True)
        feed_n(svc, 25.0, n=30, is_simulator=True)
        mock_excel.append_measurement.assert_not_called()

    def test_simulator_writes_allowed_when_not_blocked(self):
        svc, mock_excel = make_service(block_sim=False)
        feed_n(svc, 25.0, n=30, is_simulator=True)
        assert mock_excel.append_measurement.call_count >= 1


# ══════════════════════════════════════════════════════════════════════
#  Error handling — latch re-opens on failure
# ══════════════════════════════════════════════════════════════════════

class TestErrorHandling:
    def test_latch_reopens_on_excel_failure(self):
        svc, mock_excel = make_service()
        mock_excel.append_measurement.side_effect = WorkbookLockedError("locked")
        feed_n(svc, 25.0, n=30)
        assert svc.latch_state == LatchState.OPEN

    def test_error_callback_called_on_failure(self):
        svc, mock_excel = make_service()
        errors = []
        svc._on_error = errors.append
        mock_excel.append_measurement.side_effect = WorkbookLockedError("locked")
        feed_n(svc, 25.0, n=30)
        assert any("locked" in e or "FAILED" in e or "AutoSave" in e for e in errors)

