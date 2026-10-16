"""
controllers/measurement_controller.py
───────────────────────────────────────
Bridge between the GUI (main.py) and all backend services.

This controller is the unified facade for GUI → Backend communication.
It orchestrates:
  - ExcelRepository, ExcelService, StabilityDetector, and AutoSaveService.
  - Decoupled row states (selected_history_row, selected_excel_row, next_append_row, active_write_row, write_mode).
  - Centralized SystemState machine (STOPPED, RUNNING, PAUSED, STOPPING).
  - Clean error propagation without crashing the UI.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

from openpyxl import Workbook

from config import settings
from core.exceptions import ExcelRepositoryError, WorkbookLockedError
from core.models import (
    DirectionMode,
    EvaluationStatus,
    LatchState,
    MeasurementEvaluation,
    MeasurementRecord,
    SaveMode,
    SaveResult,
    SaveStatus,
    StabilityResult,
    StabilityStatus,
    SystemState,
    WriteMode,
)
from infrastructure.excel_repository import ExcelRepository
from services.auto_save_service import AutoSaveService
from services.evaluation_service import EvaluationService
from services.excel_service import ExcelService
from services.stability_detector import StabilityDetector, angular_dist

logger = logging.getLogger(__name__)


class MeasurementController:
    """
    Unified facade for GUI → Backend communication.

    Exposes:
      - Workbook management (load, inject, sheet select, clear, export, scan)
      - Decoupled row state (next_append_row, selected_history_row, selected_excel_row, write_mode)
      - Manual save / undo / edit record
      - Centralized auto-save lifecycle (enable, disable, pause, resume)
      - Save mode selection (AUTO vs MANUAL) with Enter key debouncing
      - Real-time angle feed and Excel Live View evaluation
    """

    def __init__(
        self,
        on_auto_save: Optional[Callable[[SaveResult], None]] = None,
        on_stability: Optional[Callable[[StabilityResult], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_auto_save = on_auto_save
        self._on_stability = on_stability
        self._on_error = on_error

        # Infrastructure
        self._repo = ExcelRepository()

        # Services
        self._evaluation = EvaluationService()
        self._excel = ExcelService(
            repository=self._repo,
            on_save_complete=self._handle_save_complete,
        )
        self._auto_save = AutoSaveService(
            excel_service=self._excel,
            stability_detector=StabilityDetector(),
            on_stability_changed=self._handle_stability,
            on_save_complete=self._handle_auto_save_complete,
            on_error=self._handle_error,
        )

        # Manual Save tracking & deduplication
        self._last_manual_save_time: float = 0.0
        self._last_manual_save_angle: Optional[float] = None
        self._last_manual_save_row: Optional[int] = None

    # ══════════════════════════════════════════════════════════════════
    #  Workbook Management
    # ══════════════════════════════════════════════════════════════════

    def load_workbook(self, path: str) -> list[str]:
        """
        Open an Excel workbook.

        Returns a list of sheet names on success.
        Raises :exc:`ExcelRepositoryError` subclasses on failure.
        """
        sheets = self._repo.load(path)
        if sheets:
            first_sheet = sheets[0]
            self._excel.activate_sheet(first_sheet)
        logger.info("Controller: workbook loaded — %s", Path(path).name)
        return sheets

    def inject_workbook(self, workbook: Workbook, path: str) -> None:
        """
        Accept an already-open workbook from the GUI without re-reading from disk.
        """
        self._repo.inject(workbook, path)
        sheets = self._repo.sheet_names
        if sheets:
            self._excel.activate_sheet(sheets[0])

    def select_sheet(self, sheet_name: str) -> None:
        """Switch the active recording sheet."""
        self._excel.activate_sheet(sheet_name)

    def export_copy(self, dest_path: str) -> None:
        """Save a copy of the current workbook to *dest_path*."""
        try:
            if hasattr(self._repo._workbook, "views"):
                for v in self._repo._workbook.views:
                    if getattr(v, "visibility", None) == "hidden":
                        v.visibility = "visible"
        except Exception:
            pass
        self._repo._workbook.save(dest_path)
        logger.info("Controller: exported copy → %s", dest_path)

    # ══════════════════════════════════════════════════════════════════
    #  Decoupled Row State Management
    # ══════════════════════════════════════════════════════════════════

    @property
    def next_append_row(self) -> int:
        return self._excel.next_append_row

    @next_append_row.setter
    def next_append_row(self, row: int) -> None:
        self._excel.next_append_row = row

    @property
    def current_row(self) -> int:
        return self._excel.current_row

    @current_row.setter
    def current_row(self, row: int) -> None:
        self._excel.current_row = row

    @property
    def selected_history_row(self) -> Optional[int]:
        return self._excel.selected_history_row

    @property
    def selected_excel_row(self) -> Optional[int]:
        return self._excel.selected_excel_row

    @property
    def active_write_row(self) -> Optional[int]:
        return self._excel.active_write_row

    @property
    def write_mode(self) -> WriteMode:
        return self._excel.write_mode

    def select_row(self, history_index: Optional[int], excel_row: Optional[int] = None) -> None:
        """
        Select a row in history table.

        Crucially: does NOT alter next_append_row or change mode to edit.
        """
        self._excel.select_row(history_index, excel_row)

    def clear_selection(self) -> None:
        """Clear currently selected row."""
        self._excel.clear_selection()

    @property
    def save_count(self) -> int:
        return self._excel.save_count

    @property
    def history(self) -> list[MeasurementRecord]:
        return self._excel.history

    @property
    def session_stack(self) -> list[MeasurementRecord]:
        return self._excel.session_stack

    def scan_next_empty_row(self) -> int:
        """Return the first empty row in column G."""
        if not self._repo.is_loaded:
            return settings.EXCEL_START_ROW
        return self._excel.scan_next_empty_row()

    def clear_sampling_data(self) -> int:
        """Erase all measurements from column G row 11+."""
        return self._excel.clear_all_data()

    # ══════════════════════════════════════════════════════════════════
    #  Save Mode Control (AUTO vs MANUAL)
    # ══════════════════════════════════════════════════════════════════

    @property
    def save_mode(self) -> SaveMode:
        """Current save mode (AUTO or MANUAL)."""
        return self._auto_save.save_mode

    def set_save_mode(self, mode: SaveMode) -> None:
        """Switch between AUTO and MANUAL save mode."""
        self._auto_save.set_save_mode(mode)
        if mode == SaveMode.AUTO:
            if self._repo.is_loaded and not self.is_auto_save_paused:
                self.enable_auto_save()
        elif mode == SaveMode.MANUAL:
            self._auto_save.disable()

    # ══════════════════════════════════════════════════════════════════
    #  Rotation / Test Direction Control (CW vs CCW)
    # ══════════════════════════════════════════════════════════════════

    @property
    def direction(self) -> str:
        """Active flap test rotation direction ('CW' or 'CCW')."""
        return self._auto_save.direction

    @direction.setter
    def direction(self, mode: DirectionMode | str) -> None:
        val = mode.value if isinstance(mode, DirectionMode) else str(mode).upper()
        self._auto_save.direction = val
        logger.info("Controller: direction set to %s", val)

    def set_direction(self, mode: DirectionMode | str) -> None:
        """Explicitly switch active test direction (CW or CCW)."""
        self.direction = mode

    # ══════════════════════════════════════════════════════════════════
    #  Manual Save / Undo / Edit
    # ══════════════════════════════════════════════════════════════════

    def save_measurement(
        self,
        angle: Optional[float],
        target_row: Optional[int] = None,
        is_simulator: bool = False,
        force: bool = False,
        direction: Optional[str] = None,
        step_number: Optional[int] = None,
        setpoint: Optional[float] = None,
        tolerance: Optional[float] = None,
        *args,
        **kwargs,
    ) -> SaveResult:
        """
        Manually save a measurement (triggered by Enter key or SAVE DATA button).

        Rules:
          - Validates angle is not None (rejects with error if no reading).
          - Validates workbook is loaded.
          - In AUTO mode: prevents duplicate save of already auto-saved position.
          - Debounce protection: ignores rapid key repeats or double clicks (<0.35s).
          - Sets save_mode to MANUAL on committed record.
          - Re-latches auto-save at this angle so switching to AUTO won't double-save.
          - Records test direction (CW or CCW) and sequential step number.
        """
        ts = time.strftime("%H:%M:%S")
        target_row_to_write = target_row or self.next_append_row
        eff_dir = (direction or self.direction).upper()

        if angle is None:
            msg = "Gagal simpan: Tidak ada pembacaan sensor yang valid."
            logger.warning("save_measurement: %s", msg)
            return SaveResult(
                status=SaveStatus.FAILED,
                excel_row=target_row_to_write,
                timestamp_str=ts,
                message=msg,
                error="No sensor data",
                save_mode="MANUAL",
                direction=eff_dir,
                step_number=step_number,
            )

        if not self.is_workbook_loaded:
            msg = "Gagal simpan: File Excel master belum dimuat."
            logger.warning("save_measurement: %s", msg)
            return SaveResult(
                status=SaveStatus.FAILED,
                excel_row=target_row_to_write,
                timestamp_str=ts,
                message=msg,
                error="Workbook not loaded",
                save_mode="MANUAL",
                direction=eff_dir,
                step_number=step_number,
            )

        now = time.monotonic()

        # 1. Double-click & key repeat debounce protection (<0.35s) on the same reading
        if not force and (now - self._last_manual_save_time) < 0.35:
            if self._last_manual_save_angle is not None and angular_dist(angle, self._last_manual_save_angle) < 0.001:
                logger.debug("save_measurement: debounce active (<0.35s) — skipping rapid repeat of same reading")
                return SaveResult(
                    status=SaveStatus.FAILED,
                    excel_row=target_row_to_write,
                    timestamp_str=ts,
                    message="Penyimpanan diabaikan (debounce anti-duplikasi aktif).",
                    error="Debounce active",
                    save_mode="MANUAL",
                    direction=eff_dir,
                    step_number=step_number,
                )

        # 2. AUTO-SAVE ON duplicate protection:
        # If auto-save is enabled and mode is AUTO, verify this exact position was not just saved
        if not force and self.save_mode == SaveMode.AUTO and self._auto_save.enabled:
            if self._auto_save.latch_state == LatchState.LATCHED:
                if self._auto_save._last_stable_angle is not None:
                    delta = angular_dist(angle, self._auto_save._last_stable_angle)
                    rearm_th = getattr(settings, "AUTO_SAVE_REARM_THRESHOLD_DEG", 0.18)
                    if delta < rearm_th:
                        msg = f"Posisi {angle:.2f}° sudah tersimpan otomatis. Duplikasi dicegah."
                        logger.info("save_measurement (Auto ON): %s", msg)
                        return SaveResult(
                            status=SaveStatus.FAILED,
                            excel_row=target_row_to_write,
                            timestamp_str=ts,
                            message=msg,
                            error="Already saved",
                            save_mode="AUTO",
                            direction=eff_dir,
                            step_number=step_number,
                        )

        try:
            res = self._excel.append_measurement(
                angle=angle,
                target_row=target_row,
                is_simulator=is_simulator,
                save_mode=SaveMode.MANUAL,
                direction=eff_dir,
                step_number=step_number,
                setpoint=setpoint,
                tolerance=tolerance,
            )
            if res.status == SaveStatus.SAVED:
                self._last_manual_save_time = time.monotonic()
                self._last_manual_save_angle = angle
                self._last_manual_save_row = res.excel_row

                # Re-latch auto-save at this angle so auto-save won't double-save
                if self._auto_save.enabled:
                    self._auto_save._latch = LatchState.LATCHED
                    self._auto_save._last_stable_angle = angle

            # Structured diagnostic logging for every step
            eff_step = step_number or (res.step_number if res else None) or target_row_to_write
            logger.info(
                "DIAGNOSTIC [Step %s | Row %s]: Dir=%s | Target=%s | Actual=%.3f° | Status=%s | Saved=%s | Mode=%s",
                eff_step,
                target_row_to_write,
                eff_dir,
                f"{setpoint:.2f}°" if setpoint is not None else "N/A",
                angle,
                res.status.name if res else "UNKNOWN",
                "YES" if (res and res.status == SaveStatus.SAVED) else "NO",
                res.save_mode if res else "MANUAL",
            )
            return res
        except ExcelRepositoryError as exc:
            return SaveResult(
                status=SaveStatus.FAILED,
                excel_row=target_row_to_write,
                timestamp_str=ts,
                message=str(exc),
                error=str(exc),
                save_mode="MANUAL",
                direction=eff_dir,
                step_number=step_number,
            )

    def save_manual(
        self,
        angle: Optional[float],
        target_row: Optional[int] = None,
        is_simulator: bool = False,
        force: bool = False,
        direction: Optional[str] = None,
        step_number: Optional[int] = None,
        setpoint: Optional[float] = None,
        tolerance: Optional[float] = None,
        *args,
        **kwargs,
    ) -> SaveResult:
        """Alias for save_measurement with manual semantics."""
        return self.save_measurement(
            angle=angle,
            target_row=target_row,
            is_simulator=is_simulator,
            force=force,
            direction=direction,
            step_number=step_number,
            setpoint=setpoint,
            tolerance=tolerance,
            *args,
            **kwargs,
        )

    def undo_last(self) -> Optional[MeasurementRecord]:
        """
        Remove the most recent session record.
        Safely rewinds next_append_row to the undone row.
        """
        try:
            return self._excel.undo_last()
        except ExcelRepositoryError as exc:
            self._handle_error(str(exc))
            return None

    def edit_record(self, excel_row: int, new_angle: float) -> Optional[MeasurementRecord]:
        """
        Explicitly overwrite an existing record without changing next_append_row.
        """
        try:
            return self._excel.update_record(excel_row, new_angle)
        except ExcelRepositoryError as exc:
            self._handle_error(str(exc))
            return None

    # ══════════════════════════════════════════════════════════════════
    #  Live Worksheet Data & Setpoint Evaluation
    # ══════════════════════════════════════════════════════════════════

    def get_live_worksheet_records(
        self,
        target_setpoint: Optional[float] = None,
        target_tolerance: Optional[float] = None,
        override_setpoint: Optional[float] = None,
        override_tolerance: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """
        Return real-time snapshot of the actual physical Excel worksheet
        with engineering evaluation (PASS/FAIL, Deviation, Setpoint, Status).
        """
        eff_setpoint = override_setpoint if override_setpoint is not None else target_setpoint
        eff_tolerance = override_tolerance if override_tolerance is not None else target_tolerance
        return self._excel.get_live_worksheet_records(
            evaluation_service=self._evaluation,
            target_setpoint=eff_setpoint,
            target_tolerance=eff_tolerance,
        )

    def evaluate_measurement(
        self,
        measured_angle: Optional[float],
        setpoint: Optional[float],
        tolerance: Optional[float] = None,
        direction: str = "CW",
        step_number: Optional[int] = None,
        *args,
        **kwargs,
    ) -> MeasurementEvaluation:
        """Evaluate a measured angle against a setpoint reference."""
        return self._evaluation.evaluate(
            measured_angle,
            setpoint,
            tolerance,
            direction=direction,
            step_number=step_number,
        )

    # ══════════════════════════════════════════════════════════════════
    #  Auto-Save & Centralized Lifecycle Control
    # ══════════════════════════════════════════════════════════════════

    @property
    def system_state(self) -> SystemState:
        return self._auto_save.state

    @property
    def auto_save_enabled(self) -> bool:
        return self._auto_save.enabled

    @property
    def stability_status(self) -> StabilityStatus:
        return self._auto_save.stability_status

    def enable_auto_save(self) -> None:
        """Activate auto-save (requires workbook to be loaded)."""
        if not self._repo.is_loaded:
            self._handle_error("Cannot enable auto-save: no workbook loaded.")
            return
        self._auto_save.enable()

    def disable_auto_save(self) -> None:
        """Deactivate auto-save and reset detector."""
        self._auto_save.disable()

    @property
    def is_auto_save_paused(self) -> bool:
        return self._auto_save.is_paused

    def pause_auto_save(self) -> None:
        """Synchronously pause auto-save and invalidate pending candidates."""
        self._auto_save.pause()

    def resume_auto_save(self) -> None:
        """Resume auto-save with clean stability detection."""
        self._auto_save.resume()

    def toggle_auto_save_pause(self) -> bool:
        """Toggle pause state. Returns True if now paused, False if running."""
        return self._auto_save.toggle_pause()

    def reset_auto_save(self) -> None:
        """Re-open latch and reset detector."""
        self._auto_save.reset()

    # ══════════════════════════════════════════════════════════════════
    #  Real-Time Feed
    # ══════════════════════════════════════════════════════════════════

    def feed_angle(self, angle: float, is_simulator: bool = False) -> None:
        """Process incoming hardware or simulator angle."""
        self._auto_save.feed(angle, is_simulator=is_simulator)

    # ══════════════════════════════════════════════════════════════════
    #  Repository Passthrough
    # ══════════════════════════════════════════════════════════════════

    @property
    def is_workbook_loaded(self) -> bool:
        return self._repo.is_loaded

    @property
    def active_sheet(self) -> Optional[str]:
        return self._excel.active_sheet

    def get_cell_value(self, sheet_name: str, column: str, row: int):
        """Read a single cell value."""
        return self._repo.read_cell(sheet_name, column, row)

    # ══════════════════════════════════════════════════════════════════
    #  Internal Callbacks
    # ══════════════════════════════════════════════════════════════════

    def _handle_save_complete(self, result: SaveResult) -> None:
        logger.debug("Controller._handle_save_complete: %s", result.message)

    def _handle_auto_save_complete(self, result: SaveResult) -> None:
        rec = result.record
        step_val = (rec.step_number if rec else None) or result.step_number or result.excel_row
        logger.info(
            "DIAGNOSTIC AUTO [Step %s | Row %s]: Dir=%s | Actual=%.3f° | Saved=%s | Msg=%s",
            step_val,
            result.excel_row,
            result.direction,
            result.angle or 0.0,
            result.status.name,
            result.message,
        )
        if self._on_auto_save:
            try:
                self._on_auto_save(result)
            except Exception as exc:
                logger.error("on_auto_save callback error: %s", exc)

    def _handle_stability(self, result: StabilityResult) -> None:
        if self._on_stability:
            try:
                self._on_stability(result)
            except Exception as exc:
                logger.error("on_stability callback error: %s", exc)

    def _handle_error(self, msg: str) -> None:
        logger.error("MeasurementController error: %s", msg)
        if self._on_error:
            try:
                self._on_error(msg)
            except Exception:
                pass
