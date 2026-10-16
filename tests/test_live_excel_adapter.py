"""
tests/test_live_excel_adapter.py
─────────────────────────────────
Unit tests for LiveExcelAdapter.
"""

from unittest.mock import patch, MagicMock
from infrastructure.live_excel_adapter import LiveExcelAdapter


def test_is_workbook_open_returns_false_for_empty_path():
    assert LiveExcelAdapter.is_workbook_open("") is False
    assert LiveExcelAdapter.is_workbook_open(None) is False


def test_write_cell_live_returns_false_for_empty_path():
    assert LiveExcelAdapter.write_cell_live("", "Sheet1", "G", 11, 24.5) is False
    assert LiveExcelAdapter.write_cell_live(None, "Sheet1", "G", 11, 24.5) is False


def test_clear_range_live_returns_false_for_empty_path():
    assert LiveExcelAdapter.clear_range_live("", "Sheet1", 11, 15) is False
    assert LiveExcelAdapter.clear_range_live(None, "Sheet1", 11, 15) is False


def test_is_workbook_open_success_mock():
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "OPEN_IN_EXCEL\n"

    with patch("subprocess.run", return_value=mock_res):
        assert LiveExcelAdapter.is_workbook_open("test.xlsx") is True


def test_write_cell_live_success_mock():
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "SUCCESS_WRITE_LIVE\n"

    with patch("subprocess.run", return_value=mock_res):
        assert LiveExcelAdapter.write_cell_live("test.xlsx", "Sheet1", "G", 11, 15.2) is True


def test_clear_range_live_success_mock():
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "SUCCESS_CLEAR_LIVE\n"

    with patch("subprocess.run", return_value=mock_res):
        assert LiveExcelAdapter.clear_range_live("test.xlsx", "Sheet1", 11, 15) is True
