"""
infrastructure/live_excel_adapter.py
────────────────────────────────────
Enables direct, real-time writing and saving to Microsoft Excel workbooks
that are currently open on the operator's desktop.

Uses native Windows Script Host (cscript) and COM to communicate with
the running Excel.Application instance.
Requires zero external pip packages and works reliably in all Windows environments.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Native VBScript for COM communication with running Excel instances
_VBS_SCRIPT = """
Option Explicit
Dim mode, targetPath, targetName, sheetName, cellRef, cellVal
Dim startRow, endRow, objExcel, foundWb, ws, wb, i

If WScript.Arguments.Count < 2 Then
    WScript.Echo "ERR_ARGS"
    WScript.Quit 2
End If

mode = UCase(WScript.Arguments(0))
targetPath = LCase(WScript.Arguments(1))
targetName = LCase(Mid(targetPath, InStrRev(targetPath, "\\") + 1))

Set foundWb = Nothing

' Step 1: Check running Excel.Application without spawning background processes
On Error Resume Next
Set objExcel = GetObject(, "Excel.Application")
If Not objExcel Is Nothing Then
    For Each wb In objExcel.Workbooks
        If LCase(wb.FullName) = targetPath Or LCase(wb.Name) = targetName Then
            Set foundWb = wb
            Exit For
        End If
    Next
End If
On Error GoTo 0

' For CHECK mode: if workbook not found in running Excel, quit cleanly!
' NEVER call GetObject(targetPath) on CHECK because it launches a hidden background Excel.
If mode = "CHECK" Then
    If foundWb Is Nothing Then
        WScript.Echo "ERR_NOT_OPEN"
        WScript.Quit 1
    Else
        WScript.Echo "OPEN_IN_EXCEL"
        WScript.Quit 0
    End If
End If

' For WRITE or CLEAR: if not found via objExcel, try direct moniker as fallback
If foundWb Is Nothing Then
    On Error Resume Next
    Set foundWb = GetObject(targetPath)
    On Error GoTo 0
End If

If foundWb Is Nothing Then
    WScript.Echo "ERR_NOT_OPEN"
    WScript.Quit 3
End If

' Ensure workbook window and application are ALWAYS visible (eliminates the grey screen bug)
On Error Resume Next
foundWb.Application.Visible = True
If foundWb.Windows.Count > 0 Then
    foundWb.Windows(1).Visible = True
End If
On Error GoTo 0

If mode = "WRITE" Then
    If WScript.Arguments.Count < 5 Then
        WScript.Echo "ERR_WRITE_ARGS"
        WScript.Quit 2
    End If
    sheetName = WScript.Arguments(2)
    cellRef = WScript.Arguments(3)
    cellVal = WScript.Arguments(4)

    On Error Resume Next
    Set ws = foundWb.Sheets(sheetName)
    If ws Is Nothing Then
        WScript.Echo "ERR_NO_SHEET"
        WScript.Quit 4
    End If

    Dim decSep, sVal
    decSep = Mid(CStr(1.1), 2, 1)

    If cellVal = "None" Or cellVal = "" Then
        ws.Range(cellRef).ClearContents
    Else
        ' Parse invariant float regardless of Windows regional locale (e.g. Indonesian comma decimal)
        sVal = Replace(cellVal, ",", ".")
        sVal = Replace(sVal, ".", decSep)
        If IsNumeric(sVal) Then
            ws.Range(cellRef).NumberFormat = "0.00"
            ws.Range(cellRef).Value = CDbl(sVal)
        Else
            ws.Range(cellRef).Value = cellVal
        End If
    End If

    foundWb.Save
    If Err.Number <> 0 Then
        WScript.Echo "ERR_SAVE:" & Err.Description
        WScript.Quit 5
    End If

    WScript.Echo "SUCCESS_WRITE_LIVE"
    WScript.Quit 0
End If

If mode = "CLEAR" Then
    If WScript.Arguments.Count < 5 Then
        WScript.Echo "ERR_CLEAR_ARGS"
        WScript.Quit 2
    End If
    sheetName = WScript.Arguments(2)
    startRow = CInt(WScript.Arguments(3))
    endRow = CInt(WScript.Arguments(4))

    On Error Resume Next
    Set ws = foundWb.Sheets(sheetName)
    If ws Is Nothing Then
        WScript.Echo "ERR_NO_SHEET"
        WScript.Quit 4
    End If

    ws.Range("G" & startRow & ":G" & endRow).ClearContents
    foundWb.Save
    If Err.Number <> 0 Then
        WScript.Echo "ERR_SAVE:" & Err.Description
        WScript.Quit 5
    End If

    WScript.Echo "SUCCESS_CLEAR_LIVE"
    WScript.Quit 0
End If

WScript.Echo "ERR_UNKNOWN_MODE"
WScript.Quit 9
"""



class LiveExcelAdapter:
    """
    Facilitates real-time writes into open Microsoft Excel workbooks.
    """

    _script_path: Optional[str] = None

    @classmethod
    def _ensure_script(cls) -> str:
        """Create or update temporary VBScript file for cscript execution."""
        script_file = os.path.join(tempfile.gettempdir(), "gmf_live_excel_adapter.vbs")
        with open(script_file, "w", encoding="utf-8") as f:
            f.write(_VBS_SCRIPT)
        cls._script_path = script_file
        return script_file

    @classmethod
    def is_workbook_open(cls, filepath: str) -> bool:
        """
        Check if the workbook at *filepath* is currently open in Microsoft Excel.
        """
        if not filepath:
            return False
        try:
            vbs = cls._ensure_script()
            res = subprocess.run(
                ["cscript", "//nologo", vbs, "CHECK", os.path.abspath(filepath)],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            return res.returncode == 0 and "OPEN_IN_EXCEL" in res.stdout
        except Exception as exc:
            logger.debug("LiveExcelAdapter.is_workbook_open check error: %s", exc)
            return False

    @classmethod
    def write_cell_live(
        cls,
        filepath: str,
        sheet_name: str,
        column: str,
        row: int,
        value: Any,
    ) -> bool:
        """
        Directly update a cell in an already-open Microsoft Excel workbook and save it.

        Parameters
        ----------
        filepath : str
            Full path to the .xlsx workbook.
        sheet_name : str
            Name of the worksheet.
        column : str
            Column letter (e.g. 'G').
        row : int
            Row number (e.g. 11).
        value : Any
            Value to write.

        Returns
        -------
        bool
            True if successfully updated in the open Excel instance; False otherwise.
        """
        if not filepath:
            return False
        try:
            vbs = cls._ensure_script()
            cell_ref = f"{column}{row}"
            val_str = str(value)
            res = subprocess.run(
                [
                    "cscript",
                    "//nologo",
                    vbs,
                    "WRITE",
                    os.path.abspath(filepath),
                    sheet_name,
                    cell_ref,
                    val_str,
                ],
                capture_output=True,
                text=True,
                timeout=2.5,
            )
            if res.returncode == 0 and "SUCCESS_WRITE_LIVE" in res.stdout:
                logger.info(
                    "LiveExcelAdapter: cell %s updated live in open Excel (%s)",
                    cell_ref,
                    Path(filepath).name,
                )
                return True
            logger.debug(
                "LiveExcelAdapter write_cell_live code=%d output=%s",
                res.returncode,
                res.stdout.strip(),
            )
            return False
        except Exception as exc:
            logger.warning("LiveExcelAdapter write_cell_live exception: %s", exc)
            return False

    @classmethod
    def clear_range_live(
        cls,
        filepath: str,
        sheet_name: str,
        start_row: int,
        end_row: int,
    ) -> bool:
        """Clear column G from *start_row* to *end_row* in the open Excel workbook."""
        if not filepath or start_row > end_row:
            return False
        try:
            vbs = cls._ensure_script()
            res = subprocess.run(
                [
                    "cscript",
                    "//nologo",
                    vbs,
                    "CLEAR",
                    os.path.abspath(filepath),
                    sheet_name,
                    str(start_row),
                    str(end_row),
                ],
                capture_output=True,
                text=True,
                timeout=2.5,
            )
            return res.returncode == 0 and "SUCCESS_CLEAR_LIVE" in res.stdout
        except Exception as exc:
            logger.warning("LiveExcelAdapter clear_range_live exception: %s", exc)
            return False
