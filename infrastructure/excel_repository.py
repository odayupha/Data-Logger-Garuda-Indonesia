"""
infrastructure/excel_repository.py
────────────────────────────────────
Low-level openpyxl operations.

Responsibilities:
  - Open / reload an existing workbook.
  - Write a single value to a cell.
  - Read a column range.
  - Save the workbook to disk.
  - Translate openpyxl/OS exceptions into application exceptions.

Rules:
  - No business logic (row-selection policy belongs in ExcelService).
  - No GUI dependencies.
  - No knowledge of the Stability Detector or AutoSave state.
  - Thread-safe for concurrent callers via an internal Lock.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

import openpyxl
from openpyxl import Workbook
from openpyxl.utils.exceptions import InvalidFileException

from config import settings
from core.exceptions import (
    WorkbookCorruptError,
    WorkbookLockedError,
    WorkbookNotLoadedError,
    WorkbookSaveError,
    WorksheetNotFoundError,
)
from infrastructure.live_excel_adapter import LiveExcelAdapter

logger = logging.getLogger(__name__)


class ExcelRepository:
    """
    Manages read/write access to a single .xlsx workbook.

    Usage::

        repo = ExcelRepository()
        repo.load(path)
        repo.write_cell("Sheet1", "G", 11, 12.34)
        repo.save()

    The same workbook object is kept in memory between operations,
    consistent with the existing application pattern (``self._workbook``
    in ``main.py``).  Callers supply the path; this class does NOT
    decide which file to open.
    """

    def __init__(self) -> None:
        self._workbook: Optional[Workbook] = None
        self._path: Optional[Path] = None
        self._lock = threading.Lock()
        self._last_op: Optional[tuple] = None

    # ── Public API ────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._workbook is not None

    @property
    def path(self) -> Optional[Path]:
        return self._path

    @property
    def sheet_names(self) -> list[str]:
        if self._workbook is None:
            return []
        return self._workbook.sheetnames

    def load(self, path: str | Path, data_only: bool = False) -> list[str]:
        """
        Open an existing workbook from *path*.

        Returns the list of sheet names on success.
        Raises :exc:`WorkbookCorruptError` if the file cannot be parsed,
        or :exc:`WorkbookLockedError` if OS denies access.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Workbook not found: {p}")

        try:
            wb = openpyxl.load_workbook(str(p), data_only=data_only)
        except PermissionError as exc:
            logger.error("load: PermissionError on %s — %s", p, exc)
            raise WorkbookLockedError(str(exc)) from exc
        except (InvalidFileException, Exception) as exc:
            logger.error("load: Cannot parse workbook %s — %s", p, exc)
            raise WorkbookCorruptError(str(exc)) from exc

        with self._lock:
            # Auto-heal: Ensure all workbook views are visible (heals grey-screen bug)
            try:
                if hasattr(wb, "views"):
                    for v in wb.views:
                        if getattr(v, "visibility", None) == "hidden":
                            v.visibility = "visible"
            except Exception:
                pass

            self._workbook = wb
            self._path = p

        logger.info("Loaded workbook: %s  sheets=%s", p.name, wb.sheetnames)
        return wb.sheetnames

    def inject(self, workbook: Workbook, path: str | Path) -> None:
        """
        Accept an already-loaded workbook (e.g., from main.py's ``_workbook``).

        This keeps the repository in sync with the GUI without re-loading
        from disk and avoids duplicate file open operations.
        """
        with self._lock:
            # Auto-heal: Ensure all workbook views are visible
            try:
                if hasattr(workbook, "views"):
                    for v in workbook.views:
                        if getattr(v, "visibility", None) == "hidden":
                            v.visibility = "visible"
            except Exception:
                pass

            self._workbook = workbook
            self._path = Path(path)
        logger.debug("Injected workbook: %s", Path(path).name)

    def write_cell(self, sheet_name: str, column: str, row: int, value: Any) -> None:
        """
        Write *value* to ``{column}{row}`` in *sheet_name*.

        Guarantees numeric values are stored as Python float with explicit
        two-decimal format ('0.00') so Excel desktop and downstream systems
        read them as numeric values with preserved decimal presentation.

        Raises :exc:`WorkbookNotLoadedError`, :exc:`WorksheetNotFoundError`.
        Does **not** save to disk — call :meth:`save` separately.
        """
        with self._lock:
            self._assert_loaded()
            ws = self._get_sheet(sheet_name)
            cell_ref = f"{column}{row}"
            cell = ws[cell_ref]
            if value is None:
                cell.value = None
            else:
                try:
                    num_val = float(value)
                    cell.value = num_val
                    # Preserve existing custom format if any, otherwise set standard 0.00
                    if cell.number_format in (None, "", "General"):
                        cell.number_format = "0.00"
                except (ValueError, TypeError):
                    cell.value = value

            self._last_op = ("WRITE", sheet_name, column, row, cell.value)
            logger.debug("write_cell: %s!%s = %s (type=%s, fmt=%s)", sheet_name, cell_ref, cell.value, type(cell.value).__name__, cell.number_format)

    def read_cell(self, sheet_name: str, column: str, row: int) -> Any:
        """Return the value of ``{column}{row}`` in *sheet_name*."""
        with self._lock:
            self._assert_loaded()
            ws = self._get_sheet(sheet_name)
            return ws[f"{column}{row}"].value

    def read_column_range(
        self,
        sheet_name: str,
        column: str,
        start_row: int,
        max_consecutive_empty: int = 1,
    ) -> dict[int, Any]:
        """
        Read a contiguous block of values from *column* starting at *start_row*.

        Stops at the first run of *max_consecutive_empty* consecutive empty
        cells, preventing runaway scans on sparse worksheets.

        Returns a dict mapping row_number → cell_value for non-None cells.
        """
        with self._lock:
            self._assert_loaded()
            ws = self._get_sheet(sheet_name)
            result: dict[int, Any] = {}
            empty_streak = 0
            row = start_row
            while empty_streak < max_consecutive_empty:
                val = ws[f"{column}{row}"].value
                if val is None or str(val).strip() == "":
                    empty_streak += 1
                else:
                    empty_streak = 0
                    result[row] = val
                row += 1
            return result

    def find_next_empty_row(
        self, sheet_name: str, column: str, start_row: int
    ) -> int:
        """
        Scan *column* downward from *start_row* and return the first row
        whose cell is None or blank.
        """
        with self._lock:
            self._assert_loaded()
            ws = self._get_sheet(sheet_name)
            row = start_row
            while ws[f"{column}{row}"].value is not None and str(ws[f"{column}{row}"].value).strip() != "":
                row += 1
            return row

    def read_sheet_rows(
        self,
        sheet_name: str,
        result_column: str = settings.EXCEL_RESULT_COLUMN,
        start_row: int = settings.EXCEL_START_ROW,
        end_row: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """
        Read physical worksheet rows from *start_row* to *end_row* (or last populated row).

        Returns a list of dicts:
          [{"excel_row": r, "value": num_val, "raw_value": raw}, ...]
        """
        with self._lock:
            self._assert_loaded()
            ws = self._get_sheet(sheet_name)

            actual_max = max(ws.max_row or start_row, start_row)
            limit_row = end_row if end_row is not None else actual_max

            rows_data: list[dict[str, Any]] = []
            for r in range(start_row, limit_row + 1):
                raw = ws[f"{result_column}{r}"].value
                num_val = None
                if raw is not None and str(raw).strip() != "":
                    try:
                        num_val = round(float(raw), 2)
                    except (ValueError, TypeError):
                        num_val = raw
                rows_data.append({
                    "excel_row": r,
                    "value": num_val,
                    "raw_value": raw,
                })
            return rows_data

    def get_sheet(self, sheet_name: str):
        """Thread-safe access to openpyxl worksheet object for inspection."""
        with self._lock:
            self._assert_loaded()
            return self._get_sheet(sheet_name)


    def save(self) -> None:
        """
        Flush the in-memory workbook to disk.

        If the workbook is currently open in Microsoft Excel desktop, writes
        and saves directly inside the live Excel session without file locking errors.
        Raises :exc:`WorkbookLockedError` on ``PermissionError``,
        :exc:`WorkbookSaveError` on any other OS error.
        """
        with self._lock:
            self._assert_loaded()

            # Ensure views are visible before saving (guarantees no hidden grey screen)
            try:
                if hasattr(self._workbook, "views"):
                    for v in self._workbook.views:
                        if getattr(v, "visibility", None) == "hidden":
                            v.visibility = "visible"
            except Exception:
                pass

            # Primary Path: Standard openpyxl save directly to disk
            # Fast, pure Python, zero COM overhead when file is not open by user.
            try:
                self._workbook.save(str(self._path))
                self._last_op = None
                logger.debug("Workbook saved: %s", self._path.name)
                return
            except PermissionError as exc:
                # File is locked because user has it open in Microsoft Excel!
                # Fall back to Live COM adapter to update live without lock errors.
                logger.info(
                    "save: PermissionError on disk save — file open in Excel desktop, attempting Live COM save"
                )
                if self._path and hasattr(self, "_last_op") and self._last_op:
                    op = self._last_op
                    if op[0] == "WRITE":
                        _, s_name, col, r, val = op
                        if LiveExcelAdapter.write_cell_live(str(self._path), s_name, col, r, val):
                            logger.info(
                                "save: live save succeeded on PermissionError fallback (%s!%s%s)",
                                s_name, col, r
                            )
                            self._last_op = None
                            return
                    elif op[0] == "CLEAR":
                        _, s_name, start_r, end_r = op
                        if LiveExcelAdapter.clear_range_live(str(self._path), s_name, start_r, end_r):
                            logger.info("save: live clear succeeded on PermissionError fallback")
                            self._last_op = None
                            return
                logger.error("save: PermissionError — %s", exc)
                raise WorkbookLockedError(str(exc)) from exc
            except Exception as exc:
                logger.error("save: Unexpected error — %s", exc)
                raise WorkbookSaveError(str(exc)) from exc

    def save_with_retry(
        self,
        max_retries: int = settings.EXCEL_WRITE_MAX_RETRIES,
        delay_sec: float = settings.EXCEL_WRITE_RETRY_DELAY_SEC,
    ) -> None:
        """
        Attempt :meth:`save` up to *max_retries* times with *delay_sec*
        between attempts.

        Only retries on :exc:`WorkbookLockedError` (transient file lock).
        Re-raises the last exception if all attempts fail.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                self.save()
                return
            except WorkbookLockedError as exc:
                last_exc = exc
                logger.warning(
                    "save_with_retry: attempt %d/%d — file locked: %s",
                    attempt, max_retries, exc,
                )
                if attempt < max_retries:
                    time.sleep(delay_sec)
            except WorkbookSaveError:
                # Non-transient error — don't retry.
                raise

        raise last_exc  # type: ignore[misc]

    def close(self) -> None:
        """Discard the in-memory workbook reference (does not delete the file)."""
        with self._lock:
            self._workbook = None
            self._path = None
        logger.debug("ExcelRepository: workbook reference released.")

    # ── Internal Helpers ──────────────────────────────────────────────

    def _assert_loaded(self) -> None:
        if self._workbook is None:
            raise WorkbookNotLoadedError("No workbook is currently loaded.")

    def _get_sheet(self, sheet_name: str):
        if sheet_name not in self._workbook.sheetnames:
            raise WorksheetNotFoundError(
                f"Sheet '{sheet_name}' not found. "
                f"Available: {self._workbook.sheetnames}"
            )
        return self._workbook[sheet_name]
