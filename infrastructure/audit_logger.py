"""
infrastructure/audit_logger.py
───────────────────────────────
Thread-safe audit logger for critical measurement and workbook operations.

Records:
  - Timestamp (ISO 8601 & wall-clock)
  - Operation type (SAVE, EDIT, CLEAR_ROW, DELETE_ROW, CLEAR_RANGE, RESTART)
  - Target row or range
  - Mode (AUTO, MANUAL, SINGLE_ROW, ROW_TO_END, RESUME)
  - Before / After values
  - Result status (SUCCESS / FAILED)

Rules:
  - Non-blocking (writes efficiently to dedicated rotating log or audit file).
  - Preserves history across application restarts.
  - Thread-safe via internal lock.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

AUDIT_LOG_FILE = Path("audit_trail.log")


class AuditLogger:
    """Thread-safe persistent audit logger for data changes."""

    _lock = threading.Lock()
    _instance: Optional[AuditLogger] = None

    def __init__(self, log_path: Path = AUDIT_LOG_FILE) -> None:
        self.log_path = log_path

    @classmethod
    def get_instance(cls) -> AuditLogger:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def log_event(
        self,
        operation: str,
        details: dict[str, Any],
        status: str = "SUCCESS",
        user: str = "Operator",
    ) -> dict[str, Any]:
        """
        Record an audit event to the persistent audit log file.

        Parameters
        ----------
        operation : str
            e.g. "EDIT_ROW", "CLEAR_ROW", "DELETE_ROW", "CLEAR_RANGE", "RESTART", "SAVE"
        details : dict
            Contextual details (rows, old_value, new_value, mode, sheet, etc.)
        status : str
            "SUCCESS" or "FAILED"
        user : str
            Identifier for the operator/system
        """
        now = datetime.now()
        event = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "iso_time": now.isoformat(),
            "operation": operation,
            "status": status,
            "user": user,
            "details": details,
        }

        entry_line = (
            f"[{event['timestamp']}] [{event['status']}] [{event['operation']}] "
            f"{json.dumps(event['details'], ensure_ascii=False)}\n"
        )

        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(entry_line)
            except Exception as exc:
                logger.error("AuditLogger: failed to write audit entry: %s", exc)

        logger.info("AUDIT [%s] %s: %s", status, operation, details)
        return event


# Convenience global functions
def audit_log(operation: str, details: dict[str, Any], status: str = "SUCCESS") -> dict[str, Any]:
    return AuditLogger.get_instance().log_event(operation=operation, details=details, status=status)

audit_logger = AuditLogger.get_instance()

