"""
core/exceptions.py
──────────────────
Application-specific exceptions for the Flap Sensor Data Logger.
Catching these instead of bare Exception gives callers precise control.
"""

from __future__ import annotations


class FlapSensorError(Exception):
    """Base class for all application exceptions."""


# ── Excel / Repository ─────────────────────────────────────────────────

class ExcelRepositoryError(FlapSensorError):
    """Raised when an Excel workbook operation fails."""


class WorkbookNotLoadedError(ExcelRepositoryError):
    """Raised when an operation is attempted without a loaded workbook."""


class WorksheetNotFoundError(ExcelRepositoryError):
    """Raised when the requested worksheet does not exist in the workbook."""


class WorkbookLockedError(ExcelRepositoryError):
    """Raised when the workbook file is locked by another process (PermissionError)."""


class WorkbookCorruptError(ExcelRepositoryError):
    """Raised when the workbook file is unreadable or corrupt."""


class WorkbookSaveError(ExcelRepositoryError):
    """Raised when saving the workbook fails for a non-lock reason."""


# ── Stability / Sensor ─────────────────────────────────────────────────

class SensorError(FlapSensorError):
    """Raised when a sensor value cannot be processed."""


class InvalidAngleError(SensorError):
    """Raised when an angle value is NaN, infinite, or otherwise invalid."""


# ── Hardware ───────────────────────────────────────────────────────────

class HardwareConnectionError(FlapSensorError):
    """Raised when the NAI 8810A cannot be reached over the network."""


# ── Auto-Save ─────────────────────────────────────────────────────────

class AutoSaveError(FlapSensorError):
    """Raised when auto-save cannot complete even after retries."""
