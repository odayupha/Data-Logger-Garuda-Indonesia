"""
services/excel_service.py
──────────────────────────
Business-logic layer for Excel recording.

Responsibilities:
  - Maintain clearly decoupled row states:
      * selected_history_row: table index currently selected by user.
      * selected_excel_row: physical Excel row number of the selected record.
      * next_append_row: physical Excel row targeted for the next APPEND write.
      * active_write_row: physical Excel row targeted for a write in progress.
      * write_mode: APPEND or EDIT_SELECTED mode.
  - Validate measurement values (enforce float precision and 0.00 presentation).
  - Protect against silent overwriting if workbook changed externally.
  - Keep next_append_row unchanged after an edit operation.
  - Safely recalculate next_append_row after an undo operation.
  - Never interact with the GUI directly.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from config import settings
from core.exceptions import (
    ExcelRepositoryError,
    WorkbookNotLoadedError,
    WorkbookLockedError,
)
from core.models import (
    EvaluationStatus,
    MeasurementEvaluation,
    MeasurementRecord,
    SaveMode,
    SaveResult,
    SaveStatus,
    WriteMode,
)
from infrastructure.excel_repository import ExcelRepository
from services.evaluation_service import EvaluationService

logger = logging.getLogger(__name__)

# Type alias for the optional GUI notification callback
SaveCallback = Callable[[SaveResult], None]


class ExcelService:
    """
    Manages in-session measurement state and delegates persistence to
    :class:`ExcelRepository`.
    """

    def __init__(
        self,
        repository: ExcelRepository,
        on_save_complete: Optional[SaveCallback] = None,
    ) -> None:
        self._repo = repository
        self._on_save_complete = on_save_complete

        self._active_sheet: Optional[str] = None
        self._next_append_row: int = settings.EXCEL_START_ROW
        self._selected_history_row: Optional[int] = None
        self._selected_excel_row: Optional[int] = None
        self._active_write_row: Optional[int] = None
        self._write_mode: WriteMode = WriteMode.APPEND

        self._history: list[MeasurementRecord] = []
        self._session_stack: list[MeasurementRecord] = []  # For undo

    # ── Decoupled Row & State Management ──────────────────────────────

    @property
    def next_append_row(self) -> int:
        """Physical Excel row where the next APPEND operation will write."""
        return self._next_append_row

    @next_append_row.setter
    def next_append_row(self, row: int) -> None:
        if row < 1:
            raise ValueError(f"Row must be ≥ 1, got {row}")
        self._next_append_row = row

    @property
    def current_row(self) -> int:
        """Backward-compatible alias for next_append_row."""
        return self._next_append_row

    @current_row.setter
    def current_row(self, row: int) -> None:
        self.next_append_row = row

    @property
    def selected_history_row(self) -> Optional[int]:
        """Index of the currently selected record in history table (None if none)."""
        return self._selected_history_row

    @property
    def selected_excel_row(self) -> Optional[int]:
        """Physical Excel row of the currently selected record (None if none)."""
        return self._selected_excel_row

    @property
    def active_write_row(self) -> Optional[int]:
        """Target Excel row for the write operation currently in progress."""
        return self._active_write_row

    @property
    def write_mode(self) -> WriteMode:
        """Current write strategy (APPEND or EDIT_SELECTED)."""
        return self._write_mode

    @property
    def active_sheet(self) -> Optional[str]:
        return self._active_sheet

    @property
    def history(self) -> list[MeasurementRecord]:
        """Ordered list of all recorded measurements (oldest first)."""
        return list(self._history)

    @property
    def session_stack(self) -> list[MeasurementRecord]:
        """Records saved in the current session — used by undo."""
        return list(self._session_stack)

    @property
    def save_count(self) -> int:
        return len(self._history)

    def select_row(self, history_index: Optional[int], excel_row: Optional[int] = None) -> None:
        """
        Record table row selection without affecting next_append_row or append mode.

        Selecting a row MUST NOT switch the write mode to EDIT or mutate next_append_row.
        """
        self._selected_history_row = history_index
        if excel_row is not None:
            self._selected_excel_row = excel_row
        elif history_index is not None and 0 <= history_index < len(self._history):
            self._selected_excel_row = self._history[history_index].excel_row
        else:
            self._selected_excel_row = None

        logger.debug(
            "ExcelService: selection updated (history_idx=%s, excel_row=%s, next_append_row=%d)",
            self._selected_history_row, self._selected_excel_row, self._next_append_row,
        )

    def clear_selection(self) -> None:
        """Deselect currently selected row."""
        self._selected_history_row = None
        self._selected_excel_row = None

    # ── Sheet Management ──────────────────────────────────────────────

    def activate_sheet(self, sheet_name: str) -> None:
        """
        Switch active sheet and reload history from the workbook.

        Safe to call even if the workbook is not loaded.
        """
        if not self._repo.is_loaded:
            logger.warning("activate_sheet: workbook not loaded.")
            return
        if sheet_name not in self._repo.sheet_names:
            logger.error(
                "activate_sheet: sheet '%s' not found in %s",
                sheet_name, self._repo.sheet_names,
            )
            return

        self._active_sheet = sheet_name
        self._session_stack.clear()
        self.clear_selection()
        self._reload_history()
        logger.info("ExcelService: active sheet → '%s'", sheet_name)

    def _reload_history(self) -> None:
        """Read column G from the active sheet and rebuild internal history."""
        if not self._active_sheet or not self._repo.is_loaded:
            return

        data = self._repo.read_column_range(
            sheet_name=self._active_sheet,
            column=settings.EXCEL_RESULT_COLUMN,
            start_row=settings.EXCEL_START_ROW,
            max_consecutive_empty=1,
        )

        ts = time.strftime("%H:%M:%S")
        self._history = []
        for row_num, raw_val in sorted(data.items()):
            try:
                angle = round(float(raw_val), 2)
            except (TypeError, ValueError):
                angle = 0.0

            self._history.append(
                MeasurementRecord(
                    excel_row=row_num,
                    angle=angle,
                    timestamp_str=ts,
                )
            )

        # Determine the first empty row after existing data
        if data:
            last_row = max(data.keys())
            self._next_append_row = last_row + 1
        else:
            self._next_append_row = settings.EXCEL_START_ROW

        logger.info(
            "ExcelService: history loaded — %d records, next append row=%d",
            len(self._history), self._next_append_row,
        )

    # ── Write Operations ──────────────────────────────────────────────

    def append_measurement(
        self,
        angle: float,
        target_row: Optional[int] = None,
        is_simulator: bool = False,
        save_mode: str | SaveMode = "AUTO",
        direction: str = "CW",
        step_number: Optional[int] = None,
        setpoint: Optional[float] = None,
        tolerance: Optional[float] = None,
    ) -> SaveResult:
        """
        Write *angle* to Excel in APPEND mode.

        Parameters
        ----------
        angle :
            Measured angle in degrees. Stored as valid numeric float.
        target_row :
            Explicit row override, or None to use ``next_append_row``.
        is_simulator :
            Whether the reading originated from the simulator.
        save_mode :
            SaveMode.AUTO, SaveMode.MANUAL, or string label.

        Behaviors:
          - Validates numeric type (float).
          - Checks for external data conflicts if target_row is auto-determined.
          - On success: updates history, session stack, advances next_append_row.
          - On failure: active_write_row cleared, next_append_row NOT advanced.
        """
        mode_str = save_mode.name if isinstance(save_mode, SaveMode) else str(save_mode).upper()

        if not self._repo.is_loaded:
            msg = "No workbook loaded."
            logger.error("append_measurement: %s", msg)
            result = SaveResult(
                status=SaveStatus.FAILED,
                excel_row=None,
                timestamp_str=time.strftime("%H:%M:%S"),
                message=msg,
                error=msg,
                save_mode=mode_str,
            )
            self._notify(result)
            raise WorkbookNotLoadedError(msg)

        if not self._active_sheet:
            msg = "No active sheet selected."
            logger.error("append_measurement: %s", msg)
            result = SaveResult(
                status=SaveStatus.FAILED,
                excel_row=None,
                timestamp_str=time.strftime("%H:%M:%S"),
                message=msg,
                error=msg,
                save_mode=mode_str,
            )
            self._notify(result)
            raise WorkbookNotLoadedError(msg)

        # Set write mode
        self._write_mode = WriteMode.APPEND

        # Determine target row
        row = target_row if target_row is not None else self._next_append_row

        # External data validation (Requirement 10):
        # If in automatic append mode (no manual target_row override), verify that
        # target cell hasn't been filled externally. If filled, advance to next empty row.
        if target_row is None:
            existing_val = self._repo.read_cell(self._active_sheet, settings.EXCEL_RESULT_COLUMN, row)
            if existing_val is not None and str(existing_val).strip() != "":
                scanned_empty = self.scan_next_empty_row()
                if scanned_empty > row:
                    logger.info(
                        "append_measurement: external data detected at row %d (val=%s); advancing to %d",
                        row, existing_val, scanned_empty,
                    )
                    row = scanned_empty

        self._active_write_row = row
        ts = time.strftime("%H:%M:%S")

        # Convert to strict Python float with 2 decimal precision
        numeric_angle = round(float(angle), 2)

        # Determine step number from column A if not provided
        step_val = step_number
        if step_val is None and self._repo.is_loaded and self._active_sheet:
            try:
                ws_curr = self._repo.get_sheet(self._active_sheet)
                raw_step = ws_curr.cell(row=row, column=1).value
                if raw_step is not None:
                    step_val = int(raw_step) if str(raw_step).isdigit() else None
            except Exception:
                pass

        record = MeasurementRecord(
            excel_row=row,
            angle=numeric_angle,
            timestamp_str=ts,
            is_simulator=is_simulator,
            save_mode=mode_str,
            direction=direction,
            step_number=step_val,
            setpoint=setpoint,
            tolerance=tolerance,
        )

        try:
            self._repo.write_cell(
                sheet_name=self._active_sheet,
                column=settings.EXCEL_RESULT_COLUMN,
                row=row,
                value=numeric_angle,
            )
            self._repo.save_with_retry()
        except WorkbookLockedError as exc:
            self._active_write_row = None
            result = SaveResult(
                status=SaveStatus.FAILED,
                excel_row=row,
                timestamp_str=ts,
                message=f"File terkunci: {exc}",
                error=str(exc),
                record_id=record.record_id,
                save_mode=mode_str,
            )
            self._notify(result)
            raise
        except ExcelRepositoryError as exc:
            self._active_write_row = None
            result = SaveResult(
                status=SaveStatus.FAILED,
                excel_row=row,
                timestamp_str=ts,
                message=f"Excel error: {exc}",
                error=str(exc),
                record_id=record.record_id,
                save_mode=mode_str,
            )
            self._notify(result)
            raise
        except Exception as exc:
            self._active_write_row = None
            result = SaveResult(
                status=SaveStatus.FAILED,
                excel_row=row,
                timestamp_str=ts,
                message=f"Unexpected error: {exc}",
                error=str(exc),
                record_id=record.record_id,
                save_mode=mode_str,
            )
            self._notify(result)
            raise

        # Commit succeeded
        self._active_write_row = None

        # Update in-memory state on success
        existing = next(
            (h for h in self._history if h.excel_row == row), None
        )
        if existing:
            existing.angle = record.angle
            existing.timestamp_str = ts
            existing.save_mode = mode_str
        else:
            self._history.append(record)
            self._history.sort(key=lambda r: r.excel_row)

        self._session_stack.append(record)
        # Advance next_append_row based on the row successfully written
        self._next_append_row = row + 1

        result = SaveResult(
            status=SaveStatus.SAVED,
            excel_row=row,
            timestamp_str=ts,
            message=f"Saved {record.angle:.2f}° → {settings.EXCEL_RESULT_COLUMN}{row}",
            record_id=record.record_id,
            angle=record.angle,
            record=record,
            save_mode=mode_str,
            direction=direction,
            step_number=step_val,
        )
        self._notify(result)

        logger.info(
            "ExcelService: %s (total=%d, next_append_row=%d, mode=%s)",
            result.message, len(self._history), self._next_append_row, mode_str,
        )
        return result

    def update_record(self, excel_row: int, new_angle: float) -> MeasurementRecord:
        """
        Overwrite an existing record (EDIT_SELECTED mode).

        Behaviors:
          - Writes ONLY to the designated *excel_row*.
          - Does NOT advance or change *next_append_row*.
          - Restores write_mode to APPEND on completion.
        """
        if not self._active_sheet:
            raise WorkbookNotLoadedError("No active sheet selected.")

        self._write_mode = WriteMode.EDIT_SELECTED
        self._active_write_row = excel_row

        numeric_angle = round(float(new_angle), 2)
        ts = time.strftime("%H:%M:%S")

        try:
            self._repo.write_cell(
                sheet_name=self._active_sheet,
                column=settings.EXCEL_RESULT_COLUMN,
                row=excel_row,
                value=numeric_angle,
            )
            self._repo.save_with_retry()
        finally:
            self._active_write_row = None
            self._write_mode = WriteMode.APPEND

        # Update in-memory history
        for h in self._history:
            if h.excel_row == excel_row:
                h.angle = numeric_angle
                h.timestamp_str = ts
                h.save_mode = "EDIT"
                updated = h
                break
        else:
            updated = MeasurementRecord(
                excel_row=excel_row,
                angle=numeric_angle,
                timestamp_str=ts,
                save_mode="EDIT",
            )
            self._history.append(updated)
            self._history.sort(key=lambda r: r.excel_row)

        # Mirror update in session stack if present
        for r in self._session_stack:
            if r.excel_row == excel_row:
                r.angle = numeric_angle
                break

        # Note: self._next_append_row is intentionally left untouched!
        logger.info(
            "ExcelService: edited row %d → %.2f° (next_append_row remains %d)",
            excel_row, numeric_angle, self._next_append_row,
        )
        return updated

    def undo_last(self) -> Optional[MeasurementRecord]:
        """
        Remove the most recent session record from the workbook.

        Returns the removed record, or None if the session stack is empty.
        Safely recalculates next_append_row to the undone row.
        """
        if not self._session_stack:
            return None

        record = self._session_stack[-1]
        self._active_write_row = record.excel_row

        try:
            self._repo.write_cell(
                sheet_name=self._active_sheet,
                column=settings.EXCEL_RESULT_COLUMN,
                row=record.excel_row,
                value=None,
            )
            self._repo.save_with_retry()
        finally:
            self._active_write_row = None

        self._session_stack.pop()
        self._history = [h for h in self._history if h.excel_row != record.excel_row]

        # Rewind append pointer safely
        self._next_append_row = record.excel_row

        # If undone row was selected, clear selection
        if self._selected_excel_row == record.excel_row:
            self.clear_selection()

        logger.info(
            "ExcelService: undo row %d — next_append_row reset to %d",
            record.excel_row, self._next_append_row,
        )
        return record

    def scan_next_empty_row(self) -> int:
        """Delegate to repository to find next empty row in column G."""
        return self._repo.find_next_empty_row(
            sheet_name=self._active_sheet,
            column=settings.EXCEL_RESULT_COLUMN,
            start_row=settings.EXCEL_START_ROW,
        )

    def clear_all_data(self) -> int:
        """
        Erase all measurement cells in column G from row 11 onward.

        Returns the number of cells cleared.
        """
        if not self._active_sheet:
            return 0

        data = self._repo.read_column_range(
            sheet_name=self._active_sheet,
            column=settings.EXCEL_RESULT_COLUMN,
            start_row=settings.EXCEL_START_ROW,
            max_consecutive_empty=1,
        )

        for row_num in data:
            self._repo.write_cell(
                sheet_name=self._active_sheet,
                column=settings.EXCEL_RESULT_COLUMN,
                row=row_num,
                value=None,
            )

        if data:
            self._repo._last_op = ("CLEAR", self._active_sheet, min(data.keys()), max(data.keys()))

        self._repo.save_with_retry()

        cleared = len(data)
        self._history.clear()
        self._session_stack.clear()
        self.clear_selection()
        self._next_append_row = settings.EXCEL_START_ROW

        logger.info("ExcelService: cleared %d rows — next_append_row reset to %d", cleared, self._next_append_row)
        return cleared

    # ── Live Worksheet State & Evaluation ─────────────────────────────

    def get_live_worksheet_records(
        self,
        evaluation_service: Optional[EvaluationService] = None,
        target_setpoint: Optional[float] = None,
        target_tolerance: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """
        Produce a list of live records directly representing the physical Excel worksheet.
        Combines committed cells in column G with session metadata and evaluation status.
        """
        if not self._active_sheet or not self._repo.is_loaded:
            return []

        eval_svc = evaluation_service or EvaluationService(default_tolerance_deg=target_tolerance)
        if target_tolerance is not None:
            eval_svc.default_tolerance_deg = target_tolerance

        ws = self._repo.get_sheet(self._active_sheet)
        col_map = eval_svc.detect_columns_in_sheet(ws)
        sp_col = col_map.get("setpoint")
        tol_col = col_map.get("tolerance")

        # Determine last populated row or next append row
        last_history_row = max((h.excel_row for h in self._history), default=settings.EXCEL_START_ROW - 1)
        max_sheet_row = max(ws.max_row or settings.EXCEL_START_ROW, settings.EXCEL_START_ROW)
        limit_row = max(last_history_row, max_sheet_row, self._next_append_row - 1)

        raw_rows = self._repo.read_sheet_rows(
            sheet_name=self._active_sheet,
            result_column=settings.EXCEL_RESULT_COLUMN,
            start_row=settings.EXCEL_START_ROW,
            end_row=limit_row,
        )

        history_by_row = {h.excel_row: h for h in self._history}
        records = []

        for idx, item in enumerate(raw_rows):
            r = item["excel_row"]
            val = item["value"]
            hist_rec = history_by_row.get(r)

            ts = hist_rec.timestamp_str if hist_rec else "--:--:--"
            save_mode_str = hist_rec.save_mode if hist_rec else ("AUTO" if val is not None else "--")

            # Extract row setpoint and tolerance
            sp, tol = eval_svc.extract_row_reference(ws, r, setpoint_col=sp_col, tolerance_col=tol_col)
            if sp is None and target_setpoint is not None:
                sp = target_setpoint
            if tol is None and target_tolerance is not None:
                tol = target_tolerance

            # Perform evaluation
            if val is not None and isinstance(val, (int, float)):
                eval_res = eval_svc.evaluate(measured_angle=float(val), setpoint=sp, tolerance=tol)
            else:
                eval_res = MeasurementEvaluation(
                    measured_angle=0.0,
                    setpoint=sp,
                    tolerance=tol,
                    status=EvaluationStatus.NOT_EVALUATED,
                    reason="Row is empty / no measurement recorded",
                )

            # Extract physical worksheet cells from columns A through K
            tp_cell = ws.cell(row=r, column=1).value
            turns_cell = ws.cell(row=r, column=2).value
            shaft_cell = ws.cell(row=r, column=3).value
            syncro_cell = ws.cell(row=r, column=4).value
            top_lim_cell = ws.cell(row=r, column=5).value
            bot_lim_cell = ws.cell(row=r, column=6).value
            diff_cell = ws.cell(row=r, column=9).value
            ref_cell = ws.cell(row=r, column=10).value
            real_val_cell = ws.cell(row=r, column=11).value

            # Determine direction from history record or sheet row convention
            if hist_rec and getattr(hist_rec, "direction", None):
                row_dir = hist_rec.direction
            elif "CAM OUTPUT" in str(self._active_sheet).upper():
                row_dir = "CW" if r <= 192 else "CCW"
            elif "ACCURACY" in str(self._active_sheet).upper():
                row_dir = "CW" if r <= 21 else "CCW"
            else:
                row_dir = "CW"

            records.append({
                "no": idx + 1,
                "excel_row": r,
                "timestamp": ts,
                "measured_angle": val,
                "setpoint": eval_res.setpoint,
                "deviation": eval_res.deviation,
                "tolerance": eval_res.tolerance,
                "status": eval_res.status,
                "save_mode": save_mode_str,
                "save_status": "SAVED" if val is not None else "EMPTY",
                "reason": eval_res.reason,
                "direction": row_dir,
                # Physical Excel Columns A-K
                "test_point": tp_cell,
                "input_turns": turns_cell,
                "shaft_angle": shaft_cell,
                "syncro": syncro_cell,
                "top_limit": top_lim_cell,
                "bottom_limit": bot_lim_cell,
                "difference": diff_cell,
                "reference": ref_cell,
                "real_value": real_val_cell,
            })

        return records

    # ── Internal ──────────────────────────────────────────────────────

    def _notify(self, result: SaveResult) -> None:
        if self._on_save_complete:
            try:
                self._on_save_complete(result)
            except Exception as exc:
                logger.error("on_save_complete callback error: %s", exc)
