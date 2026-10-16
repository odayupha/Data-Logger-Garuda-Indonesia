"""
services/evaluation_service.py
───────────────────────────────
Evaluates measured angle readings against engineering setpoints and tolerances.

Core Rules & Equations:
  - deviation = measured_angle - setpoint
  - absolute_error = abs(deviation)
  - PASS: absolute_error <= tolerance (exact float comparison, unrounded)
  - FAIL: absolute_error > tolerance
  - NOT_EVALUATED: when setpoint or tolerance is missing/undefined.

Responsibility:
  - Pure calculation and evaluation logic.
  - Worksheet header detection for setpoint & tolerance columns if present in workbook.
  - Zero modification to existing workbook structures or template definitions.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import re
import openpyxl
from openpyxl.utils import get_column_letter

from core.models import EvaluationStatus, MeasurementEvaluation

logger = logging.getLogger(__name__)


class EvaluationService:
    """
    Evaluates flap sensor measurements against setpoint targets.
    """

    def __init__(self, default_tolerance_deg: Optional[float] = None) -> None:
        """
        Parameters
        ----------
        default_tolerance_deg :
            Optional fallback tolerance in degrees (e.g. 0.10°). If None and
            no workbook tolerance is found, status becomes NOT_EVALUATED.
        """
        self._default_tolerance_deg = default_tolerance_deg

    @property
    def default_tolerance_deg(self) -> Optional[float]:
        return self._default_tolerance_deg

    @default_tolerance_deg.setter
    def default_tolerance_deg(self, value: Optional[float]) -> None:
        self._default_tolerance_deg = value

    def evaluate(
        self,
        measured_angle: Optional[float],
        setpoint: Optional[float],
        tolerance: Optional[float] = None,
        direction: str = "CW",
        step_number: Optional[int] = None,
    ) -> MeasurementEvaluation:
        """
        Evaluate *measured_angle* against *setpoint* and *tolerance*.

        Uses unrounded float values for mathematical comparison.
        """
        if measured_angle is None:
            return MeasurementEvaluation(
                measured_angle=0.0,
                status=EvaluationStatus.NOT_EVALUATED,
                reason="No valid measured angle",
                direction=direction,
                step_number=step_number,
            )

        eff_tolerance = tolerance if tolerance is not None else self._default_tolerance_deg

        if setpoint is None:
            return MeasurementEvaluation(
                measured_angle=measured_angle,
                setpoint=None,
                deviation=None,
                absolute_error=None,
                tolerance=eff_tolerance,
                status=EvaluationStatus.NOT_EVALUATED,
                reason="Setpoint not specified in workbook or settings",
                direction=direction,
                step_number=step_number,
            )

        # GMF Flap Angle Wrapping Rule (matching Excel columns J & K: =IF(G11>300,G11-360,0)):
        # When setpoint is close to zero/negative (e.g. 0° or -6°) and measured angle is > 300° (e.g. 359.96°):
        # The angle is wrapped across the 360° discontinuity so difference is exact.
        effective_angle = float(measured_angle)
        sp_float = float(setpoint)
        if sp_float <= 10.0 and effective_angle > 300.0:
            effective_angle -= 360.0
        elif sp_float >= 350.0 and effective_angle < 50.0:
            effective_angle += 360.0

        deviation = effective_angle - sp_float
        absolute_error = abs(deviation)

        if eff_tolerance is None:
            return MeasurementEvaluation(
                measured_angle=measured_angle,
                setpoint=setpoint,
                deviation=deviation,
                absolute_error=absolute_error,
                tolerance=None,
                status=EvaluationStatus.NOT_EVALUATED,
                reason="Tolerance not specified in workbook or settings",
                direction=direction,
                step_number=step_number,
            )

        # Exact numerical comparison
        is_pass = absolute_error <= eff_tolerance

        return MeasurementEvaluation(
            measured_angle=measured_angle,
            setpoint=setpoint,
            deviation=deviation,
            absolute_error=absolute_error,
            tolerance=eff_tolerance,
            status=EvaluationStatus.PASS if is_pass else EvaluationStatus.FAIL,
            reason=None,
            direction=direction,
            step_number=step_number,
        )

    @staticmethod
    def detect_columns_in_sheet(
        ws: Any,
        max_header_row: int = 10,
        max_col: int = 15,
    ) -> dict[str, str]:
        """
        Scan rows 1 through *max_header_row* for standard engineering column headers.
        Prioritizes the actual table header row (e.g. Row 9 in GMF inspection sheets)
        and prevents false matches on top sheet title banners (e.g. 'RESULT RECORD SHEET').
        """
        detected: dict[str, str] = {}
        if ws is None:
            return detected

        # Search from max_header_row down to 1 so the primary table header row takes priority
        for r in range(max_header_row, 0, -1):
            row_detected: dict[str, str] = {}
            for c in range(1, max_col + 1):
                val = ws.cell(r, c).value
                if not val or not isinstance(val, str):
                    continue
                txt = val.strip().lower()
                col_letter = get_column_letter(c)

                # Skip sheet title banners (e.g., 'RESULT RECORD SHEET', 'FOR TESTING...')
                if "sheet" in txt or "record sheet" in txt or len(txt) > 35:
                    continue

                if any(k in txt for k in ("setpoint", "nominal", "target angle", "std angle", "syncro", "elect. output", "synchro")):
                    if "setpoint" not in row_detected:
                        row_detected["setpoint"] = col_letter
                elif any(k in txt for k in ("top limit", "bottom limit", "tolerance", "tol")):
                    if "tolerance" not in row_detected:
                        row_detected["tolerance"] = col_letter
                elif any(k in txt for k in ("difference", "deviation", "error", "delta")):
                    if "deviation" not in row_detected:
                        row_detected["deviation"] = col_letter
                elif txt == "result" or txt.startswith("result (") or "actual" in txt or "measured" in txt:
                    if "result" not in row_detected:
                        row_detected["result"] = col_letter
                elif any(k in txt for k in ("remark", "pass/fail", "eval", "evaluation", "verdict")):
                    if "status" not in row_detected:
                        row_detected["status"] = col_letter

            # If this row contains multiple engineering headers, it is the primary table header row!
            if len(row_detected) >= 2:
                detected.update(row_detected)
                break
            elif row_detected:
                for k, v in row_detected.items():
                    if k not in detected:
                        detected[k] = v

        return detected

    @staticmethod
    def extract_row_reference(
        ws: Any,
        row: int,
        setpoint_col: Optional[str] = None,
        tolerance_col: Optional[str] = None,
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Read numerical setpoint and tolerance for a specific Excel row.
        Supports numerical values, text with ±/+/-, and formula limits (e.g. =D11+1.5 or =D11-1.5).
        """
        if ws is None or row < 1:
            return None, None

        setpoint_val: Optional[float] = None
        tolerance_val: Optional[float] = None

        if setpoint_col:
            raw = ws[f"{setpoint_col}{row}"].value
            if raw is not None:
                try:
                    setpoint_val = float(raw)
                except (ValueError, TypeError):
                    setpoint_val = None

        if tolerance_col:
            raw = ws[f"{tolerance_col}{row}"].value
            if raw is not None:
                try:
                    raw_str = str(raw).strip()
                    m = re.search(r'[\+\-](\d+(?:\.\d+)?)', raw_str)
                    if m:
                        tolerance_val = float(m.group(1))
                    else:
                        cleaned = raw_str.replace("±", "").replace("+/-", "").strip()
                        tolerance_val = float(cleaned)
                except (ValueError, TypeError):
                    tolerance_val = None

        # GMF Standard default tolerance if setpoint is available but cell formula wasn't evaluated
        if tolerance_val is None and setpoint_val is not None:
            tolerance_val = 1.50

        return setpoint_val, tolerance_val
