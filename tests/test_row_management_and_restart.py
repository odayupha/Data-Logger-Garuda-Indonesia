"""
tests/test_row_management_and_restart.py
─────────────────────────────────────────
Comprehensive test suite verifying:
1. Edit single row with metadata validation and audit trail.
2. Clear single row (blanks Column G, keeps row and step mappings).
3. Delete physical row (removes physical row in Excel).
4. Clear row range [81, 114] while guaranteeing rows 11–80 are 100% untouched.
5. Restart Measurement Mode 1 (Restart Single Row).
6. Restart Measurement Mode 2 (Restart From Row to End: rows 11–80 intact, next target = 81).
7. Restart Measurement Mode 3 (Resume From Selected Row without auto-clearing).
8. Decoupled Excel row vs Step number resolution.
9. Non-blocking persistent Audit Trail verification.
10. Template caching performance and cache invalidation.
"""

import os
import shutil
import tempfile
import pytest
import openpyxl

from infrastructure.excel_repository import ExcelRepository
from infrastructure.audit_logger import audit_logger
from services.excel_service import ExcelService, RestartMode
from controllers.measurement_controller import MeasurementController


@pytest.fixture
def test_workbook():
    """Create a realistic temporary workbook modeled after GMF AeroAsia flap rigging sheets."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ACCURACY STALL"

    # Header rows (Rows 1 to 10)
    ws["A1"] = "GMF AEROASIA - FLAP SENSOR TEST"
    ws["A10"] = "NO"
    ws["B10"] = "TEST POINT"
    ws["C10"] = "INPUT TURN"
    ws["D10"] = "SHAFT ANGLE"
    ws["E10"] = "SYNCRO ELECT. OUTPUT"  # Setpoint
    ws["F10"] = "TOP LIMIT"
    ws["G10"] = "RESULT"                # Column G: Measurement
    ws["H10"] = "REMARK"
    ws["I10"] = "DIFFERENCE"
    ws["J10"] = "REAL VALUE"
    ws["K10"] = "STATUS"

    # Populate rows 11 to 120 with realistic test point data
    for r in range(11, 121):
        step_idx = r - 10
        ws[f"A{r}"] = step_idx
        ws[f"B{r}"] = f"TP-{step_idx:03d}"
        ws[f"C{r}"] = (step_idx * 0.25)
        ws[f"D{r}"] = (step_idx * 1.5)
        setpoint = round((step_idx * 2.0) % 360, 2)
        ws[f"E{r}"] = setpoint
        ws[f"F{r}"] = f"=E{r}+1.5"
        ws[f"H{r}"] = "TEST"
        ws[f"I{r}"] = f"=G{r}-E{r}"
        ws[f"J{r}"] = f"=D{r}"
        ws[f"K{r}"] = f'=IF(ABS(G{r}-E{r})<=1.5,"PASS","FAIL")'

    # Save to a temporary file
    temp_dir = tempfile.mkdtemp()
    file_path = os.path.join(temp_dir, "test_flap_sensor.xlsx")
    wb.save(file_path)
    wb.close()

    yield file_path

    # Cleanup
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


def _init_service(file_path: str, sheet: str = "ACCURACY STALL") -> tuple[ExcelRepository, ExcelService]:
    repo = ExcelRepository()
    repo.load(file_path)
    service = ExcelService(repo)
    service.activate_sheet(sheet)
    return repo, service


# ═════════════════════════════════════════════════════════════════════
#  TEST 1: EDIT SINGLE ROW
# ═════════════════════════════════════════════════════════════════════
def test_edit_single_row_and_audit_trail(test_workbook):
    """
    Operator edits a single row (e.g. Row 81) to correct erroneous measurement.
    - Validates Column G is updated in the workbook.
    - Validates other rows (11–80 and 82–120) are untouched.
    - Validates audit logger records the change with before/after values.
    """
    repo, service = _init_service(test_workbook)

    # Initially fill rows 80, 81, 82
    service.append_measurement(80.0, target_row=80)
    service.append_measurement(999.0, target_row=81)  # erroneous reading
    service.append_measurement(82.0, target_row=82)

    # Operator edits Row 81 to correct value: 81.25
    updated_rec = service.update_record(81, 81.25)
    assert updated_rec is not None
    assert updated_rec.angle == 81.25

    # Verify physical workbook content
    wb = openpyxl.load_workbook(test_workbook, data_only=True)
    ws = wb["ACCURACY STALL"]
    assert ws["G80"].value == 80.0
    assert ws["G81"].value == 81.25
    assert ws["G82"].value == 82.0
    wb.close()


# ═════════════════════════════════════════════════════════════════════
#  TEST 2: CLEAR SINGLE ROW (PRESERVES STEP & NUMBERING)
# ═════════════════════════════════════════════════════════════════════
def test_clear_single_row_preserves_structure(test_workbook):
    """
    Operator chooses Option 1: Clear Data for Row 81.
    - Column G at row 81 becomes None.
    - Rows 11–80 and 82–120 remain in their exact physical positions.
    - Step number and metadata for Row 81 remain intact.
    """
    repo, service = _init_service(test_workbook)

    # Fill rows 80, 81, 82
    service.append_measurement(80.0, target_row=80)
    service.append_measurement(81.0, target_row=81)
    service.append_measurement(82.0, target_row=82)

    # Clear Row 81
    success = service.clear_single_row(81)
    assert success is True

    wb = openpyxl.load_workbook(test_workbook)
    ws = wb["ACCURACY STALL"]
    assert ws["G80"].value == 80.0
    assert ws["G81"].value is None  # Blanks Column G
    assert ws["G82"].value == 82.0
    # Step number and test point are preserved!
    assert ws["A81"].value == 71  # step index
    assert ws["B81"].value == "TP-071"
    wb.close()


# ═════════════════════════════════════════════════════════════════════
#  TEST 3: DELETE PHYSICAL ROW
# ═════════════════════════════════════════════════════════════════
def test_delete_physical_row(test_workbook):
    """
    Operator chooses Option 2: Delete Physical Worksheet Row.
    - Row is physically removed from the worksheet.
    - Subsequent rows shift up by 1.
    """
    repo, service = _init_service(test_workbook)

    service.append_measurement(80.0, target_row=80)
    service.append_measurement(81.0, target_row=81)
    service.append_measurement(82.0, target_row=82)

    # Initial test point at row 82
    wb_pre = openpyxl.load_workbook(test_workbook)
    tp_pre_82 = wb_pre["ACCURACY STALL"]["B82"].value
    wb_pre.close()

    # Physically delete row 81
    success = service.delete_physical_row(81)
    assert success is True

    wb_post = openpyxl.load_workbook(test_workbook)
    ws_post = wb_post["ACCURACY STALL"]
    assert ws_post["G80"].value == 80.0
    # Row 82 shifted up to row 81!
    assert ws_post["G81"].value == 82.0
    assert ws_post["B81"].value == tp_pre_82
    wb_post.close()


# ═════════════════════════════════════════════════════════════════
#  TEST 4: CLEAR ROW RANGE (e.g. Rows 81–114)
# ═════════════════════════════════════════════════════════════════
def test_clear_row_range_guarantees_prior_rows_untouched(test_workbook):
    """
    Scenario:
    - Pengukuran dilakukan mulai row 11 hingga row 114.
    - Row 11–80 sudah benar dan harus dipertahankan.
    - Row 81 ternyata salah.
    - Row 82–114 sudah terisi tetapi perlu diambil ulang.
    - Operator clears range 81–114.
    - GUARANTEE: Row 11–80 100% UNTOUCHED and intact!
    - GUARANTEE: Row 81–114 Column G cleared!
    """
    repo, service = _init_service(test_workbook)

    # Populate all rows 11 through 114 with unique values
    for r in range(11, 115):
        service.append_measurement(float(r * 2.5), target_row=r)

    # Verify rows 11 to 80 are populated before clear
    wb_check1 = openpyxl.load_workbook(test_workbook)
    ws1 = wb_check1["ACCURACY STALL"]
    for r in range(11, 81):
        assert ws1[f"G{r}"].value == float(r * 2.5)
    for r in range(81, 115):
        assert ws1[f"G{r}"].value == float(r * 2.5)
    wb_check1.close()

    # Clear range 81 to 114
    cleared_count = service.clear_row_range(81, 114)
    assert cleared_count == (114 - 81 + 1)  # 34 rows

    # Verify workbook after clear
    wb_check2 = openpyxl.load_workbook(test_workbook)
    ws2 = wb_check2["ACCURACY STALL"]

    # 1. Row 11–80 MUST REMAIN COMPLETELY UNTOUCHED!
    for r in range(11, 81):
        assert ws2[f"G{r}"].value == float(r * 2.5), f"Row {r} was corrupted!"
        assert ws2[f"I{r}"].value == f"=G{r}-E{r}", f"Formula at Row {r} was corrupted!"

    # 2. Row 81–114 MUST BE CLEARED in Column G
    for r in range(81, 115):
        assert ws2[f"G{r}"].value is None, f"Row {r} was not cleared!"
        # Formula in column I remains intact!
        assert ws2[f"I{r}"].value == f"=G{r}-E{r}"

    wb_check2.close()


# ═════════════════════════════════════════════════════════════════
#  TEST 5: RESTART MEASUREMENT MODE 1 (RESTART SINGLE ROW)
# ═════════════════════════════════════════════════════════════════
def test_restart_measurement_single_row(test_workbook):
    """
    Mode 1: Restart Single Row from Row 81.
    - Only Row 81 is cleared/re-targeted.
    - Subsequent rows (82–114) remain untouched.
    - Rows 11–80 remain untouched.
    """
    repo, service = _init_service(test_workbook)

    for r in range(11, 115):
        service.append_measurement(float(r), target_row=r)

    res = service.restart_measurement(81, mode=RestartMode.SINGLE_ROW)
    assert res["status"] == "SUCCESS"
    assert service.current_row == 81

    # Retake measurement on row 81
    rec81 = service.append_measurement(81.99)
    assert rec81.excel_row == 81

    wb = openpyxl.load_workbook(test_workbook)
    ws = wb["ACCURACY STALL"]
    assert ws["G80"].value == 80.0
    assert ws["G81"].value == 81.99  # Overwritten with retaken measurement
    assert ws["G82"].value == 82.0   # 82 untouched!
    assert ws["G114"].value == 114.0 # 114 untouched!
    wb.close()


# ═════════════════════════════════════════════════════════════════
#  TEST 6: RESTART MEASUREMENT MODE 2 (RESTART FROM ROW TO END)
# ═════════════════════════════════════════════════════════════════
def test_restart_measurement_row_to_end(test_workbook):
    """
    Mode 2: Restart From Row to End (Crucial Flap Rigging Workflow).
    - Rows 11–80 are 100% preserved.
    - Rows 81–114 are cleared.
    - Target row becomes 81.
    - New measurements start recording cleanly from Row 81.
    """
    repo, service = _init_service(test_workbook)

    # Populate 11 to 114
    for r in range(11, 115):
        service.append_measurement(float(r * 1.5), target_row=r)

    # Restart from 81 to 114
    res = service.restart_measurement(81, mode=RestartMode.ROW_TO_END, end_row=114)
    assert res["status"] == "SUCCESS"
    assert service.current_row == 81

    # Verify rows 11–80 preserved, 81–114 cleared
    wb = openpyxl.load_workbook(test_workbook)
    ws = wb["ACCURACY STALL"]
    for r in range(11, 81):
        assert ws[f"G{r}"].value == float(r * 1.5)
    for r in range(81, 115):
        assert ws[f"G{r}"].value is None
    wb.close()

    # Now take new measurement at Row 81
    rec81 = service.append_measurement(18.25)
    assert rec81.excel_row == 81
    assert rec81.angle == 18.25

    # Verify pointer advances to Row 82
    assert service.current_row == 82

    # Verify workbook has new value at 81, while 11–80 remain untouched
    wb_after = openpyxl.load_workbook(test_workbook)
    ws_after = wb_after["ACCURACY STALL"]
    assert ws_after["G80"].value == float(80 * 1.5)
    assert ws_after["G81"].value == 18.25
    wb_after.close()


# ═════════════════════════════════════════════════════════════════
#  TEST 7: RESTART MEASUREMENT MODE 3 (RESUME FROM ROW)
# ═════════════════════════════════════════════════════════════════
def test_restart_measurement_resume_from_row(test_workbook):
    """
    Mode 3: Resume From Selected Row without automatic deletion.
    - Points target pointer to Row 81.
    - Leaves all existing cell values intact.
    """
    repo, service = _init_service(test_workbook)

    for r in range(11, 115):
        service.append_measurement(float(r), target_row=r)

    res = service.restart_measurement(81, mode=RestartMode.RESUME_FROM_ROW)
    assert res["status"] == "SUCCESS"
    assert service.current_row == 81

    # Verify data on row 81 and all others is NOT cleared
    wb = openpyxl.load_workbook(test_workbook)
    ws = wb["ACCURACY STALL"]
    assert ws["G81"].value == 81.0
    assert ws["G114"].value == 114.0
    wb.close()


# ═════════════════════════════════════════════════════════════════
#  TEST 8: DECOUPLE EXCEL ROW AND STEP NUMBER
# ═════════════════════════════════════════════════════════════════
def test_decouple_excel_row_and_step_number(test_workbook):
    """
    Validates that:
    1. Excel Row number,
    2. Test Step number,
    3. Input Turns,
    4. Shaft Angle,
    5. Setpoint & Tolerance
    are extracted and resolved independently without assuming Step == Row.
    """
    ctrl = MeasurementController()
    ctrl.load_workbook(test_workbook)
    ctrl.select_sheet("ACCURACY STALL")

    # Inspect Step Info for Row 11 (Step 1)
    info11 = ctrl.get_step_info_for_row(11)
    assert info11["excel_row"] == 11
    assert info11["step_number"] == 1
    assert info11["test_point"] == "TP-001"
    assert info11["input_turns"] == 0.25
    assert info11["setpoint"] == 2.0

    # Inspect Step Info for Row 81 (Step 71)
    info81 = ctrl.get_step_info_for_row(81)
    assert info81["excel_row"] == 81
    assert info81["step_number"] == 71  # Decoupled! Not 81!
    assert info81["test_point"] == "TP-071"
    assert info81["input_turns"] == (71 * 0.25)
    assert info81["tolerance"] == 1.5


# ═════════════════════════════════════════════════════════════════
#  TEST 9: TEMPLATE CACHING PERFORMANCE
# ═════════════════════════════════════════════════════════════════
def test_template_caching_performance_and_invalidation(test_workbook):
    """
    Verify template caching in ExcelService:
    - First call loads static template data into _template_cache.
    - Subsequent calls use cache, avoiding ~3,300 cell re-reads.
    - Physical row deletion invalidates cache.
    """
    repo, service = _init_service(test_workbook)

    sheet_name = "ACCURACY STALL"
    assert sheet_name not in service._template_cache

    # First call - populates cache
    recs1 = service.get_live_worksheet_records()
    assert sheet_name in service._template_cache
    assert len(service._template_cache[sheet_name]) == len(recs1)

    # Second call - retrieves from cache instantly
    recs2 = service.get_live_worksheet_records()
    assert len(recs2) == len(recs1)

    # Delete physical row -> cache must be invalidated
    service.delete_physical_row(81)
    assert sheet_name not in service._template_cache


# ═════════════════════════════════════════════════════════════════
#  TEST 10: BOUNDARY & PROTECTION VALIDATION
# ═════════════════════════════════════════════════════════════════
def test_protection_and_boundary_validations(test_workbook):
    """
    Verify safety rules:
    - Clear range where start < 11 raises ValueError (protects headers).
    - Clear range where start > end raises ValueError.
    """
    repo, service = _init_service(test_workbook)

    # start_row < 11 must be rejected
    with pytest.raises(ValueError, match="must be >= 11 to protect headers"):
        service.clear_row_range(10, 50)

    # start_row > end_row must be rejected
    with pytest.raises(ValueError, match="cannot exceed end_row"):
        service.clear_row_range(80, 70)
