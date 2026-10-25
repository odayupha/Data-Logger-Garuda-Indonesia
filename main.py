"""
main.py – Flap Sensor Data Logger
──────────────────────────────────
Full-featured desktop data logger for the API 8810A Digital Protractor.
Built for GMF AeroAsia flap rigging inspections.

Visual Design:
- Theme     : Clean Light Enterprise Mode (GMF AeroAsia Identity)
- Workspace : Light Slate (#F1F5F9), Pure White Cards (#FFFFFF), Royal Blue (#2563EB)
Rev         : 3.3 (2026-10-16) – Clean Light Enterprise UI Mode
"""

# ── stdlib ───────────────────────────────────────────────────────────
import io
import os
import sys
import math
import time
import wave
import struct
import threading
import winsound
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, StringVar, IntVar, BooleanVar
from tkinter import ttk
from typing import Optional

# ── third-party ──────────────────────────────────────────────────────
import customtkinter as ctk
from PIL import Image, ImageTk
import openpyxl

# ── local ────────────────────────────────────────────────────────────
from serial_handler import EthernetReader, SerialReader
from controllers.measurement_controller import MeasurementController
from core.models import (
    AcquisitionEvent,
    ConnectionState,
    EvaluationStatus,
    MeasurementEvaluation,
    MotionState,
    MotionTriggerResult,
    SaveMode,
    SaveResult,
    SaveStatus,
    StabilityResult,
    StabilityStatus,
    TWDAMotionStatus,
)
from infrastructure.logging_config import configure_logging
from infrastructure.live_excel_adapter import LiveExcelAdapter

# Initialize centralized logging
configure_logging()


# ═════════════════════════════════════════════════════════════════════
#  CONSTANTS & COLOR PALETTE
# ═════════════════════════════════════════════════════════════════════
APP_TITLE = "GMF AeroAsia | FLAP SENSOR DATA LOGGER - API 8810A DIGITAL PROTRACTOR"
APP_SIZE = "1380x880"
APP_MIN_W = 1180
APP_MIN_H = 760

DEFAULT_START_ROW = 11
RESULT_COLUMN = "G"           # Column G = "RESULT"
DEFAULT_COMMAND = "API1 ANGLE?"
PRESET_COMMANDS = [
    "API1 ANGLE?  (CH1 Real-time Angle — Instant)",
    "API2 ANGLE?  (CH2 Real-time Angle — Instant)",
    "API1 AVG_ANGLE?  (CH1 Angle — Hardware Filtered)",
    "API2 AVG_ANGLE?  (CH2 Angle — Hardware Filtered)",
    "*IDN?  (Model Identification / Ping)",
    "*ERR?  (Check Error Status)",
]
POLL_INTERVAL_MS = 30
LOGO_FILENAME = "logo.png"

# ── Clean Light Enterprise Mode (GMF AeroAsia) ────────────────────────
CLR_BG_MAIN        = "#F1F5F9"  # Light Slate background
CLR_PANEL_BG       = "#FFFFFF"  # Crisp White card
CLR_PANEL_SEC      = "#F8FAFC"  # Secondary light container / hover
CLR_INPUT_BG       = "#FFFFFF"  # Crisp White input background
CLR_BORDER         = "#E2E8F0"  # Subtle light slate border
CLR_BORDER_CYAN    = "#BAE6FD"  # Highlight soft cyan border
CLR_ACCENT_CYAN    = "#0284C7"  # Deep telemetry cyan
CLR_ACCENT_BLUE    = "#2563EB"  # Corporate Royal Blue
CLR_SUCCESS_NEON   = "#16A34A"  # Emerald Green (PASS / Connected)
CLR_WARNING_AMBER  = "#D97706"  # Amber gold (warning / simulator)
CLR_DANGER_RED     = "#DC2626"  # Crimson red (FAIL / error)
CLR_TEXT_PRIMARY   = "#0F172A"  # Deep slate / Charcoal text
CLR_TEXT_SECONDARY = "#64748B"  # Muted slate text
CLR_TEXT_MUTED     = "#94A3B8"  # Subtle hint text
CLR_GAUGE_TRACK    = "#E2E8F0"  # Inactive gauge track arc

# Header unified colors
CLR_HDR_BG         = "#FFFFFF"  # Clean white header background
CLR_HDR_TITLE      = "#0F172A"  # Deep slate header title
CLR_HDR_SUBTITLE   = "#0284C7"  # Corporate cyan subtitle
CLR_HDR_ACCENT     = "#2563EB"  # Royal blue accent line
CLR_HDR_BORDER     = "#E2E8F0"  # Subtle light divider
CLR_HDR_ACTIVE     = "#16A34A"  # Emerald green for active badge
CLR_HDR_BADGE_BG   = "#DCFCE7"  # Soft green pill background
CLR_HDR_CLOCK_BG   = "#F8FAFC"  # Light slate clock container
CLR_TEAL_LIGHT     = "#E0F2FE"  # Soft teal container for Target Row
CLR_TEAL_BORDER    = "#BAE6FD"  # Border for Target Row container
CLR_TEAL_TEXT      = "#0284C7"  # Teal badge text


def _generate_beep_wav(freq: int = 1600, duration_ms: int = 220, volume: float = 0.85) -> bytes:
    """Generate in-memory PCM WAV bytes for a tone with smooth attack/decay."""
    try:
        sample_rate = 44100
        n_samples = int(sample_rate * (duration_ms / 1000.0))
        attack = int(sample_rate * 0.015)
        decay = int(sample_rate * 0.025)
        frames = bytearray()
        for i in range(n_samples):
            if i < attack:
                env = i / attack
            elif i > n_samples - decay:
                env = max(0.0, (n_samples - i) / decay)
            else:
                env = 1.0
            sample_val = int(volume * env * 32767.0 * math.sin(2.0 * math.pi * freq * (i / sample_rate)))
            frames.extend(struct.pack("<h", max(-32768, min(32767, sample_val))))

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(frames)
        return buf.getvalue()
    except Exception:
        return b""


_SUCCESS_BEEP_WAV = _generate_beep_wav(freq=1600, duration_ms=220, volume=0.85)
_ALERT_BEEP_WAV = _generate_beep_wav(freq=450, duration_ms=350, volume=0.85)


def play_sound_beep(freq: int = 1600, duration_ms: int = 220, is_success: bool = True):
    """
    Play a crisp audible confirmation beep in a background daemon thread.
    Uses in-memory WAV playback via Windows Multimedia (SND_MEMORY) so it is
    100% audible on all laptop/desktop speakers and headsets, with graceful fallback.
    """
    def _worker():
        played = False
        try:
            data = _SUCCESS_BEEP_WAV if is_success else _ALERT_BEEP_WAV
            if data:
                winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
                played = True
        except Exception:
            pass

        if not played:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK if is_success else winsound.MB_ICONHAND)
                played = True
            except Exception:
                pass

        if not played:
            try:
                winsound.Beep(freq, duration_ms)
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


# ═════════════════════════════════════════════════════════════════════
#  CUSTOM WIDGET: Cockpit Panel Frame
# ═════════════════════════════════════════════════════════════════
class CockpitCard(ctk.CTkFrame):
    """Container frame styled with Clean Light Enterprise aesthetic."""

    def __init__(self, master, title: str = "", badge_text: str = "", **kw):
        super().__init__(
            master,
            corner_radius=8,
            fg_color=CLR_PANEL_BG,
            border_width=1,
            border_color=CLR_BORDER,
            **kw,
        )
        if title:
            header_bar = ctk.CTkFrame(self, fg_color="transparent", height=28)
            header_bar.pack(fill="x", padx=14, pady=(10, 4))

            title_lbl = ctk.CTkLabel(
                header_bar,
                text=title,
                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                text_color=CLR_TEXT_PRIMARY,
                anchor="w",
            )
            title_lbl.pack(side="left")

            if badge_text:
                self.badge = ctk.CTkLabel(
                    header_bar,
                    text=badge_text,
                    font=ctk.CTkFont(family="Consolas", size=10, weight="bold"),
                    text_color=CLR_TEXT_SECONDARY,
                    fg_color=CLR_PANEL_SEC,
                    corner_radius=6,
                    padx=8,
                    pady=2,
                )
                self.badge.pack(side="right")
            else:
                self.badge = None

            sep = ctk.CTkFrame(self, height=1, fg_color=CLR_BORDER)
            sep.pack(fill="x", padx=14, pady=(0, 8))


# ═════════════════════════════════════════════════════════════════
#  CUSTOM WIDGET: Status Dot Indicator
# ═════════════════════════════════════════════════════════════════
class TelemetryStatusDot(ctk.CTkLabel):
    """Glowing circular indicator for hardware connection state."""

    def __init__(self, master, **kw):
        super().__init__(master, text="●", font=ctk.CTkFont(size=14), width=16, **kw)
        self.set_state("off")

    def set_state(self, state: str):
        mapping = {
            "on": CLR_SUCCESS_NEON,
            "off": CLR_DANGER_RED,
            "connecting": CLR_WARNING_AMBER,
        }
        self.configure(text_color=mapping.get(state, CLR_TEXT_MUTED))


# ═════════════════════════════════════════════════════════════════
#  CUSTOM WIDGET: Semicircular Telemetry Gauge
# ═════════════════════════════════════════════════════════════════
class TelemetryGaugeCanvas(tk.Canvas):
    """
    Circular 360-degree analog protractor telemetry gauge.
    Renders an engineering-grade 0° - 360° circular degree dial with radial tick marks,
    degree numbers, high-visibility needle, and center pivot.
    """

    def __init__(self, master, width: int = 180, height: int = 180, **kwargs):
        super().__init__(
            master,
            width=width,
            height=height,
            bg="#FFFFFF",
            highlightthickness=0,
            bd=0,
            **kwargs,
        )
        self._width = width
        self._height = height
        self._value: Optional[float] = None
        self._is_simulator = False
        self._error_msg: Optional[str] = None
        self._setpoint: Optional[float] = None
        self._deviation: Optional[float] = None
        self.bind("<Configure>", self._on_resize)
        self._draw_gauge()

    def _on_resize(self, event):
        if event.width > 20 and event.height > 20:
            self._width = event.width
            self._height = event.height
            self._draw_gauge()

    def set_value(
        self,
        value: Optional[float],
        is_simulator: bool = False,
        error_msg: Optional[str] = None,
        setpoint: Optional[float] = None,
        deviation: Optional[float] = None,
    ):
        self._value = value
        self._is_simulator = is_simulator
        self._error_msg = error_msg
        self._setpoint = setpoint
        self._deviation = deviation
        self._draw_gauge()

    def _draw_gauge(self):
        self.delete("all")
        cx = self._width / 2.0
        cy = self._height / 2.0
        r_outer = min(cx, cy) - 8.0
        if r_outer < 25.0:
            r_outer = 25.0
        r_inner = r_outer - 18.0

        # Base Dial Face Circle
        self.create_oval(
            cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer,
            fill="#FFFFFF", outline="#CBD5E1", width=1.5
        )
        if r_inner > 15:
            self.create_oval(
                cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner,
                fill="#FAFAFA", outline="#E2E8F0", width=1.0
            )

        # Draw Degree Ticks (360 degrees)
        for d in range(0, 360, 5):
            rad = math.radians(d - 90)
            if d % 30 == 0:
                tick_len = 10.0
                width = 1.5
                color = "#475569"
            elif d % 10 == 0:
                tick_len = 7.0
                width = 1.0
                color = "#64748B"
            else:
                tick_len = 4.0
                width = 0.8
                color = "#CBD5E1"

            x1 = cx + (r_outer - tick_len) * math.cos(rad)
            y1 = cy + (r_outer - tick_len) * math.sin(rad)
            x2 = cx + r_outer * math.cos(rad)
            y2 = cy + r_outer * math.sin(rad)
            self.create_line(x1, y1, x2, y2, fill=color, width=width)

            # Degree Labels every 30 degrees
            if d % 30 == 0 and r_inner > 25:
                lbl_r = r_outer - 18.0
                lx = cx + lbl_r * math.cos(rad)
                ly = cy + lbl_r * math.sin(rad)
                self.create_text(
                    lx, ly,
                    text=f"{d}°",
                    fill="#64748B",
                    font=("Segoe UI", 7, "bold"),
                )

        # Guideline cross
        self.create_line(cx - 15, cy, cx + 15, cy, fill="#E2E8F0", width=1)
        self.create_line(cx, cy - 15, cx, cy + 15, fill="#E2E8F0", width=1)

        # Draw Indicator Needle (pointing clockwise from 12 o'clock)
        angle_val = float(self._value) if self._value is not None else 45.0
        needle_rad = math.radians(angle_val - 90)
        needle_len = max(10.0, r_inner - 6.0)
        back_len = 14.0

        nx = cx + needle_len * math.cos(needle_rad)
        ny = cy + needle_len * math.sin(needle_rad)

        bx = cx - back_len * math.cos(needle_rad)
        by = cy - back_len * math.sin(needle_rad)

        perp_rad = needle_rad + math.pi / 2.0
        w = 3.5
        p1x = cx + w * math.cos(perp_rad)
        p1y = cy + w * math.sin(perp_rad)
        p2x = cx - w * math.cos(perp_rad)
        p2y = cy - w * math.sin(perp_rad)

        self.create_polygon(
            bx, by, p1x, p1y, nx, ny, p2x, p2y,
            fill="#0F172A", outline="#0F172A"
        )
        tip_x = cx + (needle_len - 14) * math.cos(needle_rad)
        tip_y = cy + (needle_len - 14) * math.sin(needle_rad)
        self.create_line(tip_x, tip_y, nx, ny, fill="#2563EB", width=2.5)

        # Center Pivot Cap
        self.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill="#0F172A", outline="#CBD5E1", width=1.5)
        self.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill="#FFFFFF")


# ═════════════════════════════════════════════════════════════════
#  MODAL DIALOG: Edit Record (Cockpit Styled)
# ═════════════════════════════════════════════════════════════════
# ═════════════════════════════════════════════════════════════════
# 3. MODAL DIALOGS: EDIT, DELETE, CLEAR RANGE, RESTART
# ═════════════════════════════════════════════════════════════════

class EditRecordDialog(ctk.CTkToplevel):
    """
    Industrial modal dialog for editing an existing recorded measurement.
    Displays engineering metadata (Test Point, Turns, Setpoint) with direct validation.
    """

    def __init__(self, master, excel_row: int, current_value, row_info: Optional[dict] = None):
        super().__init__(master)
        self.title(f"EDIT RECORD — ROW {excel_row}")
        self.resizable(False, False)
        self.configure(fg_color=CLR_PANEL_BG)

        self.result: Optional[float] = None
        self._excel_row = excel_row
        self._original_value = current_value
        self._row_info = row_info or {}

        self._build_ui()

        dialog_w, dialog_h = 460, 410
        self.update_idletasks()
        parent_x = master.winfo_rootx()
        parent_y = master.winfo_rooty()
        parent_w = master.winfo_width()
        parent_h = master.winfo_height()
        x = parent_x + (parent_w - dialog_w) // 2
        y = parent_y + (parent_h - dialog_h) // 2
        self.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")

        self.transient(master)
        self.grab_set()
        self.focus_set()

        self._entry_value.focus_set()
        self._entry_value.select_range(0, "end")

        self.bind("<Return>", lambda e: self._on_save())
        self.bind("<KP_Enter>", lambda e: self._on_save())
        self.bind("<Escape>", lambda e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _build_ui(self):
        # ── Header ───────────────────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color=CLR_PANEL_SEC, corner_radius=0, height=48)
        header.pack(fill="x")
        header.pack_propagate(False)

        ctk.CTkLabel(
            header,
            text=f"✎  KOREKSI / EDIT PENGUKURAN — BARIS {self._excel_row}",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=CLR_WARNING_AMBER,
        ).pack(side="left", padx=16)

        cyan_strip = ctk.CTkFrame(self, fg_color=CLR_ACCENT_CYAN, height=2, corner_radius=0)
        cyan_strip.pack(fill="x")

        # ── Body ─────────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=12)

        # Context Info Card (Test Point, Turns, Setpoint)
        tp = self._row_info.get("test_point", "—")
        turns = self._row_info.get("input_turns", "—")
        sp = self._row_info.get("setpoint", "—")
        sp_txt = f"{float(sp):.2f}°" if isinstance(sp, (int, float)) else str(sp)

        info_box = ctk.CTkFrame(body, fg_color=CLR_PANEL_SEC, border_color=CLR_BORDER, border_width=1, corner_radius=6)
        info_box.pack(fill="x", pady=(0, 10), ipady=4)
        info_txt = f"Test Point: {tp}  |  Turns: {turns}  |  Setpoint: {sp_txt}"
        ctk.CTkLabel(
            info_box,
            text=info_txt,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=CLR_TEXT_SECONDARY,
        ).pack(fill="x", padx=8, pady=4)

        # Original Value
        ctk.CTkLabel(
            body,
            text="Nilai Tercatat Sebelumnya di Excel:",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=CLR_TEXT_SECONDARY,
            anchor="w",
        ).pack(fill="x")

        if self._original_value is not None:
            try:
                orig_str = f"{float(self._original_value):.2f}°"
            except (ValueError, TypeError):
                orig_str = str(self._original_value)
        else:
            orig_str = "—"
        orig_box = ctk.CTkFrame(body, fg_color=CLR_INPUT_BG, border_color=CLR_BORDER, border_width=1, corner_radius=6, height=34)
        orig_box.pack(fill="x", pady=(4, 10))
        orig_box.pack_propagate(False)
        ctk.CTkLabel(
            orig_box,
            text=orig_str,
            font=ctk.CTkFont(family="Consolas", size=13, weight="bold"),
            text_color=CLR_TEXT_MUTED,
            anchor="w",
        ).pack(side="left", padx=12)

        # New Value Entry
        ctk.CTkLabel(
            body,
            text="Nilai Baru yang Dikoreksi (°):",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
            anchor="w",
        ).pack(fill="x")

        self._entry_value = ctk.CTkEntry(
            body,
            font=ctk.CTkFont(family="Consolas", size=15, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            height=38,
        )
        self._entry_value.pack(fill="x", pady=(4, 6))
        if self._original_value is not None:
            try:
                self._entry_value.insert(0, f"{float(self._original_value):.2f}")
            except (ValueError, TypeError):
                self._entry_value.insert(0, str(self._original_value))

        # Preset Nudge Buttons
        nudge_bar = ctk.CTkFrame(body, fg_color="transparent")
        nudge_bar.pack(fill="x", pady=(0, 6))
        for delta in (-1.0, -0.1, 0.1, 1.0):
            lbl = f"{delta:+.1f}°"
            ctk.CTkButton(
                nudge_bar,
                text=lbl,
                width=55,
                height=24,
                font=ctk.CTkFont(family="Consolas", size=10),
                fg_color="#FFFFFF",
                border_color=CLR_BORDER,
                border_width=1,
                hover_color="#F1F5F9",
                text_color=CLR_TEXT_PRIMARY,
                command=lambda d=delta: self._nudge(d),
            ).pack(side="left", padx=2)

        # Error label
        self._lbl_err = ctk.CTkLabel(
            body, text="", font=ctk.CTkFont(family="Segoe UI", size=10), text_color=CLR_DANGER_RED, anchor="w"
        )
        self._lbl_err.pack(fill="x")

        # ── Buttons ──────────────────────────────────────────────────
        btn_bar = ctk.CTkFrame(self, fg_color=CLR_PANEL_SEC, height=52)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        ctk.CTkButton(
            btn_bar,
            text="Batal",
            width=85,
            height=32,
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            border_width=1,
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            command=self._on_cancel,
        ).pack(side="right", padx=(6, 16), pady=10)

        ctk.CTkButton(
            btn_bar,
            text="✓ Simpan Koreksi",
            width=135,
            height=32,
            fg_color=CLR_ACCENT_BLUE,
            hover_color="#1D4ED8",
            text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=self._on_save,
        ).pack(side="right", padx=6, pady=10)

    def _nudge(self, delta: float):
        try:
            curr = float(self._entry_value.get().strip().replace("°", "").replace(",", "."))
            new_val = round(curr + delta, 2)
            self._entry_value.delete(0, "end")
            self._entry_value.insert(0, f"{new_val:.2f}")
        except ValueError:
            pass

    def _on_save(self):
        raw = self._entry_value.get().strip().replace("°", "").replace(",", ".")
        if not raw:
            self._lbl_err.configure(text="Nilai tidak boleh kosong.")
            return
        try:
            val = round(float(raw), 2)
        except ValueError:
            self._lbl_err.configure(text="Format angka tidak valid (contoh: 12.34 atau 12,34).")
            return

        self.result = val
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()


class ClearOrDeleteRowDialog(ctk.CTkToplevel):
    """
    Industrial dialog to choose between:
      1. Clear Measurement Data (blanks Column G, keeps row & step mapping).
      2. Delete Physical Worksheet Row (physically removes row in Excel).
    """

    def __init__(self, master, excel_row: int, current_value, row_info: Optional[dict] = None):
        super().__init__(master)
        self.title(f"HAPUS / KOSONGKAN DATA — BARIS {excel_row}")
        self.resizable(False, False)
        self.configure(fg_color=CLR_PANEL_BG)

        self.choice: Optional[str] = None  # "CLEAR_DATA", "DELETE_ROW", None
        self._excel_row = excel_row
        self._current_value = current_value
        self._row_info = row_info or {}

        self._build_ui()

        dialog_w, dialog_h = 490, 370
        self.update_idletasks()
        parent_x = master.winfo_rootx()
        parent_y = master.winfo_rooty()
        parent_w = master.winfo_width()
        parent_h = master.winfo_height()
        x = parent_x + (parent_w - dialog_w) // 2
        y = parent_y + (parent_h - dialog_h) // 2
        self.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")

        self.transient(master)
        self.grab_set()
        self.focus_set()

        self.bind("<Escape>", lambda e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color=CLR_PANEL_SEC, corner_radius=0, height=48)
        header.pack(fill="x")
        header.pack_propagate(False)

        ctk.CTkLabel(
            header,
            text=f"⌫  PILIHAN PENGELOLAAN BARIS {self._excel_row}",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=CLR_DANGER_RED,
        ).pack(side="left", padx=16)

        ctk.CTkFrame(self, fg_color=CLR_BORDER, height=1, corner_radius=0).pack(fill="x")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=14)

        tp = self._row_info.get("test_point", "—")
        val_txt = f"{float(self._current_value):.2f}°" if isinstance(self._current_value, (int, float)) else str(self._current_value or "—")

        ctk.CTkLabel(
            body,
            text=f"Baris terpilih: Baris {self._excel_row} (Test Point: {tp}) | Nilai saat ini: {val_txt}",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
            anchor="w",
        ).pack(fill="x", pady=(0, 10))

        # Option 1 Card: Clear Measurement Data (Recommended)
        opt1_frame = ctk.CTkFrame(body, fg_color="#F0FDF4", border_color="#86EFAC", border_width=1, corner_radius=6)
        opt1_frame.pack(fill="x", pady=(0, 10), padx=2, ipady=6)

        ctk.CTkLabel(
            opt1_frame,
            text="OPSI 1: KOSONGKAN DATA PENGUKURAN (DIREKOMENDASIKAN)",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#15803D",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(4, 2))

        ctk.CTkLabel(
            opt1_frame,
            text="• Menghapus hasil ukur pada Kolom G saja.\n• Nomor baris, step pengujian, dan formula Excel lainnya tetap utuh.\n• Baris siap diisi ulang pada pengujian berikutnya.",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color="#166534",
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=12, pady=(0, 6))

        ctk.CTkButton(
            opt1_frame,
            text="⌫ Kosongkan Nilai Baris Ini",
            height=30,
            fg_color="#16A34A",
            hover_color="#15803D",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=self._on_clear_data,
        ).pack(anchor="e", padx=12, pady=(0, 4))

        # Option 2 Card: Delete Physical Row
        opt2_frame = ctk.CTkFrame(body, fg_color="#FEF2F2", border_color="#FECACA", border_width=1, corner_radius=6)
        opt2_frame.pack(fill="x", pady=(0, 4), padx=2, ipady=4)

        ctk.CTkLabel(
            opt2_frame,
            text="OPSI 2: HAPUS BARIS FISIK EXCEL (DELETE ROW)",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color="#B91C1C",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(4, 2))

        ctk.CTkLabel(
            opt2_frame,
            text="• Menghapus baris secara fisik dari worksheet (baris bawahnya bergeser naik).\n• Gunakan hanya jika Anda ingin menghapus baris fisik secara permanen.",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color="#991B1B",
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=12, pady=(0, 6))

        ctk.CTkButton(
            opt2_frame,
            text="🗑 Hapus Baris Fisik",
            height=28,
            fg_color="#DC2626",
            hover_color="#B91C1C",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._on_delete_row,
        ).pack(anchor="e", padx=12, pady=(0, 4))

        # ── Buttons ──────────────────────────────────────────────────
        btn_bar = ctk.CTkFrame(self, fg_color=CLR_PANEL_SEC, height=48)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        ctk.CTkButton(
            btn_bar,
            text="Batal",
            width=90,
            height=30,
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            border_width=1,
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            command=self._on_cancel,
        ).pack(side="right", padx=16, pady=8)

    def _on_clear_data(self):
        self.choice = "CLEAR_DATA"
        self.grab_release()
        self.destroy()

    def _on_delete_row(self):
        self.choice = "DELETE_ROW"
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.choice = None
        self.grab_release()
        self.destroy()


class ClearRowRangeDialog(ctk.CTkToplevel):
    """
    Dialog to clear measurement results across an explicit row range [start_row, end_row].
    Guarantees rows outside range (e.g. Rows 11 to start_row - 1) are untouched.
    """

    def __init__(self, master, default_start: int = 81, default_end: int = 114, max_sheet_row: int = 374):
        super().__init__(master)
        self.title("KOSONGKAN RENTANG BARIS (CLEAR RANGE)")
        self.resizable(False, False)
        self.configure(fg_color=CLR_PANEL_BG)

        self.result: Optional[tuple[int, int]] = None
        self._default_start = max(default_start, DEFAULT_START_ROW)
        self._default_end = max(default_end, self._default_start)
        self._max_sheet_row = max(max_sheet_row, self._default_end)

        self._build_ui()

        dialog_w, dialog_h = 480, 360
        self.update_idletasks()
        parent_x = master.winfo_rootx()
        parent_y = master.winfo_rooty()
        parent_w = master.winfo_width()
        parent_h = master.winfo_height()
        x = parent_x + (parent_w - dialog_w) // 2
        y = parent_y + (parent_h - dialog_h) // 2
        self.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")

        self.transient(master)
        self.grab_set()
        self.focus_set()

        self.bind("<Escape>", lambda e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color=CLR_PANEL_SEC, corner_radius=0, height=48)
        header.pack(fill="x")
        header.pack_propagate(False)

        ctk.CTkLabel(
            header,
            text="✂  KOSONGKAN RENTANG BARIS DATA (CLEAR ROW RANGE)",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
        ).pack(side="left", padx=16)

        ctk.CTkFrame(self, fg_color=CLR_BORDER, height=1, corner_radius=0).pack(fill="x")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=16)

        ctk.CTkLabel(
            body,
            text="Tentukan rentang baris Excel yang ingin dikosongkan hasil pengukurannya:",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=CLR_TEXT_SECONDARY,
            anchor="w",
        ).pack(fill="x", pady=(0, 10))

        # Inputs Grid
        grid_f = ctk.CTkFrame(body, fg_color="transparent")
        grid_f.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(grid_f, text="Baris Awal (Start):", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold")).grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        self._ent_start = ctk.CTkEntry(grid_f, width=90, font=ctk.CTkFont(family="Consolas", size=13, weight="bold"), justify="center")
        self._ent_start.insert(0, str(self._default_start))
        self._ent_start.grid(row=0, column=1, sticky="w", pady=4)

        ctk.CTkLabel(grid_f, text="Baris Akhir (End):", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold")).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        self._ent_end = ctk.CTkEntry(grid_f, width=90, font=ctk.CTkFont(family="Consolas", size=13, weight="bold"), justify="center")
        self._ent_end.insert(0, str(self._default_end))
        self._ent_end.grid(row=1, column=1, sticky="w", pady=4)

        self._ent_start.bind("<KeyRelease>", lambda e: self._update_preview())
        self._ent_end.bind("<KeyRelease>", lambda e: self._update_preview())

        # Live Dynamic Preview Card
        self._preview_card = ctk.CTkFrame(body, fg_color="#F8FAFC", border_color=CLR_BORDER, border_width=1, corner_radius=6)
        self._preview_card.pack(fill="x", pady=(0, 8), ipady=6)

        self._lbl_preview = ctk.CTkLabel(
            self._preview_card,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=CLR_TEXT_PRIMARY,
            justify="left",
            anchor="w",
        )
        self._lbl_preview.pack(fill="x", padx=12, pady=4)

        self._lbl_range_err = ctk.CTkLabel(body, text="", font=ctk.CTkFont(family="Segoe UI", size=10), text_color=CLR_DANGER_RED, anchor="w")
        self._lbl_range_err.pack(fill="x")

        self._update_preview()

        # ── Buttons ──────────────────────────────────────────────────
        btn_bar = ctk.CTkFrame(self, fg_color=CLR_PANEL_SEC, height=52)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        ctk.CTkButton(
            btn_bar,
            text="Batal",
            width=85,
            height=32,
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            border_width=1,
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            command=self._on_cancel,
        ).pack(side="right", padx=(6, 16), pady=10)

        ctk.CTkButton(
            btn_bar,
            text="✂ Kosongkan Rentang Ini",
            width=170,
            height=32,
            fg_color=CLR_DANGER_RED,
            hover_color="#B91C1C",
            text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=self._on_confirm,
        ).pack(side="right", padx=6, pady=10)

    def _update_preview(self):
        try:
            s = int(self._ent_start.get().strip())
            e = int(self._ent_end.get().strip())
            if s < DEFAULT_START_ROW:
                self._lbl_range_err.configure(text=f"Baris awal harus ≥ {DEFAULT_START_ROW} (melindungi header).")
                self._lbl_preview.configure(text="Input baris tidak valid.")
                return
            if s > e:
                self._lbl_range_err.configure(text="Baris awal tidak boleh lebih besar dari baris akhir.")
                self._lbl_preview.configure(text="Input baris tidak valid.")
                return

            self._lbl_range_err.configure(text="")
            count = e - s + 1
            safe_range = f"Baris 11 s/d {s-1}" if s > DEFAULT_START_ROW else "Tidak ada data sebelumnya"
            preview_txt = (
                f"✦ Dampak Operasi:\n"
                f"• Akan mengosongkan data Kolom G dari Baris {s} sampai Baris {e} (total {count} baris).\n"
                f"• {safe_range} TETAP UTUH dan tidak akan disentuh atau diubah."
            )
            self._lbl_preview.configure(text=preview_txt)
        except ValueError:
            self._lbl_range_err.configure(text="Nomor baris harus berupa bilangan bulat positif.")

    def _on_confirm(self):
        try:
            s = int(self._ent_start.get().strip())
            e = int(self._ent_end.get().strip())
            if s < DEFAULT_START_ROW or s > e:
                return
            self.result = (s, e)
            self.grab_release()
            self.destroy()
        except ValueError:
            pass

    def _on_cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()


class RestartMeasurementDialog(ctk.CTkToplevel):
    """
    Dialog to restart/resume measurement from a specific row with 3 explicit modes:
      - Mode 1: Restart Single Row
      - Mode 2: Restart From Row to End
      - Mode 3: Resume From Selected Row
    """

    def __init__(self, master, default_start_row: int = 81, max_populated_row: int = 114):
        super().__init__(master)
        self.title(f"RESTART PENGUKURAN DARI BARIS {default_start_row}")
        self.resizable(False, False)
        self.configure(fg_color=CLR_PANEL_BG)

        self.result: Optional[dict[str, Any]] = None
        self._default_start_row = max(default_start_row, DEFAULT_START_ROW)
        self._max_populated_row = max(max_populated_row, self._default_start_row)

        self._mode_var = ctk.StringVar(value="ROW_TO_END")

        self._build_ui()

        dialog_w, dialog_h = 520, 480
        self.update_idletasks()
        parent_x = master.winfo_rootx()
        parent_y = master.winfo_rooty()
        parent_w = master.winfo_width()
        parent_h = master.winfo_height()
        x = parent_x + (parent_w - dialog_w) // 2
        y = parent_y + (parent_h - dialog_h) // 2
        self.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")

        self.transient(master)
        self.grab_set()
        self.focus_set()

        self.bind("<Escape>", lambda e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color=CLR_PANEL_SEC, corner_radius=0, height=48)
        header.pack(fill="x")
        header.pack_propagate(False)

        ctk.CTkLabel(
            header,
            text=f"⟲  RESTART PENGUKURAN DARI BARIS TERTENTU",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=CLR_ACCENT_BLUE,
        ).pack(side="left", padx=16)

        ctk.CTkFrame(self, fg_color=CLR_BORDER, height=1, corner_radius=0).pack(fill="x")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=14)

        # Target Row Input Row
        top_bar = ctk.CTkFrame(body, fg_color="transparent")
        top_bar.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            top_bar,
            text="Mulai Ulang Dari Baris (Target Row):",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
        ).pack(side="left", padx=(0, 10))

        self._ent_start = ctk.CTkEntry(
            top_bar,
            width=90,
            font=ctk.CTkFont(family="Consolas", size=13, weight="bold"),
            justify="center",
        )
        self._ent_start.insert(0, str(self._default_start_row))
        self._ent_start.pack(side="left")
        self._ent_start.bind("<KeyRelease>", lambda e: self._update_explanation())

        # Mode Selection Options
        ctk.CTkLabel(
            body,
            text="Pilih Metode Restart / Pengulangan:",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        # Radio button 1: ROW_TO_END
        r1 = ctk.CTkRadioButton(
            body,
            text="Mode 2 — Restart From Row to End (Rekomendasi Uji Flap)",
            variable=self._mode_var,
            value="ROW_TO_END",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
            command=self._update_explanation,
        )
        r1.pack(anchor="w", pady=3)

        # Radio button 2: SINGLE_ROW
        r2 = ctk.CTkRadioButton(
            body,
            text="Mode 1 — Restart Single Row (Hanya Ulangi Baris Ini)",
            variable=self._mode_var,
            value="SINGLE_ROW",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=CLR_TEXT_PRIMARY,
            command=self._update_explanation,
        )
        r2.pack(anchor="w", pady=3)

        # Radio button 3: RESUME_FROM_ROW
        r3 = ctk.CTkRadioButton(
            body,
            text="Mode 3 — Resume From Selected Row (Lanjutkan Tanpa Menghapus)",
            variable=self._mode_var,
            value="RESUME_FROM_ROW",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=CLR_TEXT_PRIMARY,
            command=self._update_explanation,
        )
        r3.pack(anchor="w", pady=3)

        # Explanation Box
        self._expl_card = ctk.CTkFrame(body, fg_color="#F8FAFC", border_color=CLR_BORDER, border_width=1, corner_radius=6)
        self._expl_card.pack(fill="both", expand=True, pady=(10, 8), padx=2)

        self._lbl_expl = ctk.CTkLabel(
            self._expl_card,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=CLR_TEXT_PRIMARY,
            justify="left",
            anchor="nw",
        )
        self._lbl_expl.pack(fill="both", expand=True, padx=12, pady=10)

        self._lbl_err = ctk.CTkLabel(body, text="", font=ctk.CTkFont(family="Segoe UI", size=10), text_color=CLR_DANGER_RED, anchor="w")
        self._lbl_err.pack(fill="x")

        self._update_explanation()

        # ── Buttons ──────────────────────────────────────────────────
        btn_bar = ctk.CTkFrame(self, fg_color=CLR_PANEL_SEC, height=52)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        ctk.CTkButton(
            btn_bar,
            text="Batal",
            width=85,
            height=32,
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            border_width=1,
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            command=self._on_cancel,
        ).pack(side="right", padx=(6, 16), pady=10)

        ctk.CTkButton(
            btn_bar,
            text="⟲ Konfirmasi Restart",
            width=150,
            height=32,
            fg_color=CLR_ACCENT_BLUE,
            hover_color="#1D4ED8",
            text_color="#FFFFFF",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=self._on_confirm,
        ).pack(side="right", padx=6, pady=10)

    def _update_explanation(self):
        try:
            s = int(self._ent_start.get().strip())
            if s < DEFAULT_START_ROW:
                self._lbl_err.configure(text=f"Baris awal harus ≥ {DEFAULT_START_ROW}.")
                self._lbl_expl.configure(text="Input baris tidak valid.")
                return

            self._lbl_err.configure(text="")
            mode = self._mode_var.get()
            safe_txt = f"Baris 11 s/d {s-1} DIJAMIN TETAP UTUH dan tidak akan disentuh." if s > DEFAULT_START_ROW else "Mulai dari awal sheet."

            if mode == "ROW_TO_END":
                txt = (
                    f"✦ PENJELASAN MODE 2 (RESTART FROM ROW TO END):\n\n"
                    f"1. Seluruh data hasil pengukuran dari Baris {s} hingga akhir ({self._max_populated_row}) akan dikosongkan terlebih dahulu.\n"
                    f"2. {safe_txt}\n"
                    f"3. Target row aktif diatur tepat ke Baris {s}.\n"
                    f"4. Pengukuran baru dapat langsung dimulai dari Baris {s}."
                )
            elif mode == "SINGLE_ROW":
                txt = (
                    f"✦ PENJELASAN MODE 1 (RESTART SINGLE ROW):\n\n"
                    f"1. Hanya baris {s} yang akan ditargetkan untuk diukur ulang.\n"
                    f"2. Baris lainnya (baik sebelum {s} maupun sesudah {s}) TIDAK AKAN DIHAPUS.\n"
                    f"3. Jika baris {s} sudah ada data, pengukuran baru akan menimpa baris {s}."
                )
            else:  # RESUME_FROM_ROW
                txt = (
                    f"✦ PENJELASAN MODE 3 (RESUME FROM SELECTED ROW):\n\n"
                    f"1. Melanjutkan pengukuran mulai Baris {s} tanpa menghapus data secara otomatis.\n"
                    f"2. Baris yang sudah terisi akan diperingatkan sebelum ditimpa."
                )

            self._lbl_expl.configure(text=txt)
        except ValueError:
            self._lbl_err.configure(text="Nomor baris harus angka bulat.")

    def _on_confirm(self):
        try:
            s = int(self._ent_start.get().strip())
            if s < DEFAULT_START_ROW:
                return
            mode = self._mode_var.get()
            self.result = {
                "start_row": s,
                "mode": mode,
                "end_row": self._max_populated_row,
            }
            self.grab_release()
            self.destroy()
        except ValueError:
            pass

    def _on_cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()



# ═════════════════════════════════════════════════════════════════
#  MAIN APPLICATION CLASS
# ═════════════════════════════════════════════════════════════════
class FlapSensorApp(ctk.CTk):
    """
    Flap Sensor Data Logger application window.
    Implements White Corporate Header & Cyber-Dark Cockpit Dashboard.
    """

    def __init__(self):
        super().__init__()

        # Set official GMF AeroAsia application icon on titlebar & taskbar
        self._apply_app_icon()

        # Appearance configuration (Clean Light Enterprise Mode)
        ctk.set_appearance_mode("Light")
        ctk.set_default_color_theme("blue")

        self.title(APP_TITLE)
        self.geometry(APP_SIZE)
        self.minsize(APP_MIN_W, APP_MIN_H)
        self.configure(fg_color=CLR_BG_MAIN)

        # ── State Variables ──────────────────────────────────────────
        self._reader: Optional[EthernetReader] = None
        self._connection_state: ConnectionState = ConnectionState.DISCONNECTED
        self._live_value: Optional[float] = None
        self._last_update_str: Optional[str] = None
        self._workbook: Optional[openpyxl.Workbook] = None
        self._excel_path: Optional[str] = None
        self._active_sheet_name: Optional[str] = None
        self._current_row: int = DEFAULT_START_ROW
        self._save_count: int = 0
        self._save_in_progress: bool = False

        # History and decoupled row tracking
        self._history_data: list[dict] = []
        self._record_stack: list[dict] = []
        self._edited_rows: set[int] = set()
        self._last_saved_row: Optional[int] = None
        self._selected_history_row: Optional[int] = None
        self._selected_excel_row: Optional[int] = None
        self._btn_save = None
        self._btn_undo = None
        self._btn_export = None
        self._btn_pause = None
        self._lbl_action_banner = None

        # ── Modular Backend Controller ────────────────────────────────
        self._ctrl = MeasurementController(
            on_auto_save=self._on_auto_save_result,
            on_stability=self._on_stability_update,
            on_error=self._log,
        )

        # ── Tk Variables ─────────────────────────────────────────────
        self._var_ip = StringVar(value="192.168.1.21")
        self._var_port = StringVar(value="23")
        self._var_cmd = StringVar(value=DEFAULT_COMMAND)
        self._var_sim = BooleanVar(value=False)
        self._var_auto_save = BooleanVar(value=True)
        self._var_save_mode = StringVar(value="AUTO-SAVE ON")
        self._var_target_setpoint = StringVar(value="")
        self._var_target_tolerance = StringVar(value="")
        self._var_excel_path = StringVar(value="No file loaded")
        self._var_sheet = StringVar(value="—")
        self._var_target_row = StringVar(value=str(DEFAULT_START_ROW))
        self._var_direction = StringVar(value="CW")
        self._var_auto_scroll = BooleanVar(value=True)

        # Performance & telemetry coalescing caches
        self._display_update_pending: bool = False
        self._latest_telemetry_value: Optional[float] = None
        self._last_displayed_value: Optional[float] = None  # debounce display
        self._cached_row: Optional[int] = None
        self._cached_target_sp: Optional[float] = None
        self._cached_target_tol: float = 1.50
        self._tree_context_menu = None

        # Throttle: Hardware log & error callbacks ke max 1x/detik
        self._last_hw_log_time: float = 0.0
        self._last_hw_err_time: float = 0.0
        self._hw_log_throttle_sec: float = 1.0

        self._tree_live = None
        self._lbl_sync_time = None
        self._btn_manual_save = None
        self._lbl_mode_badge = None
        self._seg_mode = None
        self._seg_direction = None
        self._lbl_direction_pill = None
        self._lbl_verdict_badge = None
        self._badge_verdict_frame = None
        self._lbl_setpoint_info = None

        # Configure TTK Styles (Treeview)
        self._configure_treeview_style()

        # Build UI layout
        self._build_top_bar()
        self._build_main_workspace()
        self._build_footer_bar()

        # Start digital clock update loop
        self._update_clock()

        # Application startup log
        self._log("✦ System initialized. White Corporate Header active.")
        self._log(f"✦ Target instrument: NAI API 8810A via TCP Port 23.")
        self._log(f"✦ Base Excel row: {DEFAULT_START_ROW}. Target Row fully customizable.")

        # Window shutdown protocol
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ═════════════════════════════════════════════════════════════════
    #  APPLICATION WINDOW & TASKBAR ICON
    # ═════════════════════════════════════════════════════════════════
    def _find_icon(self) -> Optional[str]:
        """Search for the official GMF AeroAsia .ico file."""
        candidates = [
            Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "logo.ico",
            Path(__file__).parent / "logo.ico",
            Path(__file__).parent / "assets" / "gmf_app_icon.ico",
            Path(sys.executable).parent / "logo.ico",
            Path.cwd() / "logo.ico",
            Path.cwd() / "assets" / "gmf_app_icon.ico",
        ]
        for p in candidates:
            if p.exists():
                return str(p)
        return None

    def _apply_app_icon(self):
        """Set the official GMF AeroAsia icon on Windows titlebar and taskbar."""
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "gmf.aeroasia.flapsensordatalogger.8810a"
                )
            except Exception:
                pass

        icon_path = self._find_icon()
        if icon_path:
            try:
                self.iconbitmap(icon_path)
            except Exception:
                try:
                    img = Image.open(icon_path)
                    photo = ImageTk.PhotoImage(img)
                    self.iconphoto(True, photo)
                except Exception:
                    pass

    # ═════════════════════════════════════════════════════════════════
    #  STYLE CONFIGURATION
    # ═════════════════════════════════════════════════════════════════
    def _configure_treeview_style(self):
        """Configure ttk.Treeview with the Clean Light Enterprise theme."""
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "Cockpit.Treeview",
            background="#FFFFFF",
            foreground=CLR_TEXT_PRIMARY,
            fieldbackground="#FFFFFF",
            borderwidth=0,
            font=("Segoe UI", 10),
            rowheight=28,
        )
        style.configure(
            "Cockpit.Treeview.Heading",
            background="#F8FAFC",
            foreground="#475569",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            padding=(6, 6),
        )
        style.map(
            "Cockpit.Treeview",
            background=[("selected", "#E0F2FE")],
            foreground=[("selected", "#0F172A")],
        )
        style.map(
            "Cockpit.Treeview.Heading",
            background=[("active", "#F1F5F9")],
            foreground=[("active", CLR_ACCENT_BLUE)],
        )

    # ═════════════════════════════════════════════════════════════════
    #  TOP BAR — WHITE CORPORATE HEADER (GMF AEROASIA)
    # ═════════════════════════════════════════════════════════════════
    def _build_top_bar(self):
        """
        Build Clean Light Enterprise header with official GMF AeroAsia logo text,
        application title, digital clock, and pill status badges.
        """
        header_frame = ctk.CTkFrame(self, fg_color=CLR_HDR_BG, corner_radius=0, height=58, border_color=CLR_BORDER, border_width=1)
        header_frame.pack(fill="x", side="top")
        header_frame.pack_propagate(False)

        # ── Left: Official Brand & Title ─────────────────────────────
        left_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        left_box.pack(side="left", padx=20, fill="y")

        brand_lbl = ctk.CTkLabel(
            left_box,
            text="GMF AeroAsia",
            font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
            text_color="#034EA2",  # Official Garuda Blue
        )
        brand_lbl.pack(side="left", pady=14)

        divider = ctk.CTkLabel(
            left_box,
            text="|",
            font=ctk.CTkFont(family="Segoe UI", size=16),
            text_color=CLR_BORDER_FOCUS if 'CLR_BORDER_FOCUS' in globals() else "#CBD5E1",
        )
        divider.pack(side="left", padx=12, pady=14)

        title_lbl = ctk.CTkLabel(
            left_box,
            text="FLAP SENSOR DATA LOGGER - API 8810A DIGITAL PROTRACTOR",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
        )
        title_lbl.pack(side="left", pady=14)

        # ── Right: Status Badges & Digital Clock ─────────────────────
        right_box = ctk.CTkFrame(header_frame, fg_color="transparent")
        right_box.pack(side="right", padx=20, fill="y")

        # 1. Connected Pill Badge
        self._lbl_hdr_instrument = ctk.CTkLabel(
            right_box,
            text="● CONNECTED",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_SUCCESS_NEON,
            fg_color=CLR_GREEN_LIGHT if 'CLR_GREEN_LIGHT' in globals() else "#DCFCE7",
            corner_radius=20,
            padx=14,
            pady=4,
        )
        self._lbl_hdr_instrument.pack(side="left", padx=(0, 10), pady=14)

        # 2. Workbook Loaded Pill Badge
        self._lbl_hdr_workbook = ctk.CTkLabel(
            right_box,
            text="● WORKBOOK LOADED",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_TEAL_TEXT,
            fg_color=CLR_TEAL_LIGHT,
            corner_radius=20,
            padx=14,
            pady=4,
        )
        self._lbl_hdr_workbook.pack(side="left", padx=(0, 14), pady=14)

        # Compatibility hidden badge
        self._badge_sys_active = ctk.CTkLabel(right_box, text="")

        # 3. Digital Clock
        self._lbl_clock = ctk.CTkLabel(
            right_box,
            text="08:08:38",
            font=ctk.CTkFont(family="Consolas", size=14, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
        )
        self._lbl_clock.pack(side="left", pady=14)

    def _build_text_badge(self, parent):
        pass

    def _update_clock(self):
        """Update the digital clock display every second."""
        current_time = time.strftime("%H:%M:%S")
        if hasattr(self, "_lbl_clock") and self._lbl_clock:
            self._lbl_clock.configure(text=current_time)
        self.after(1000, self._update_clock)

    def _update_header_status(self):
        """Synchronize top bar indicators with actual runtime telemetry state."""
        if not hasattr(self, "_lbl_hdr_instrument") or self._lbl_hdr_instrument is None:
            return

        is_connected = (self._reader is not None and self._reader.is_connected)
        is_sim = self._var_sim.get()
        conn_st = getattr(self, "_connection_state", ConnectionState.DISCONNECTED)

        if is_connected:
            txt = "● CONNECTED (SIM)" if is_sim else "● CONNECTED"
            self._lbl_hdr_instrument.configure(
                text=txt,
                text_color=CLR_SUCCESS_NEON,
                fg_color="#DCFCE7",
            )
        elif conn_st == ConnectionState.CONNECTING:
            self._lbl_hdr_instrument.configure(
                text="● CONNECTING...",
                text_color=CLR_WARNING_AMBER,
                fg_color="#FEF3C7",
            )
        elif conn_st == ConnectionState.DISCONNECTING:
            self._lbl_hdr_instrument.configure(
                text="● DISCONNECTING...",
                text_color=CLR_WARNING_AMBER,
                fg_color="#FEF3C7",
            )
        elif conn_st == ConnectionState.CONNECTION_ERROR:
            self._lbl_hdr_instrument.configure(
                text="⚠ CONN ERROR",
                text_color=CLR_DANGER_RED,
                fg_color="#FEE2E2",
            )
        else:
            self._lbl_hdr_instrument.configure(
                text="○ DISCONNECTED",
                text_color=CLR_DANGER_RED,
                fg_color="#FEE2E2",
            )

        if self._workbook is not None and self._ctrl.is_workbook_loaded:
            self._lbl_hdr_workbook.configure(
                text="● WORKBOOK LOADED",
                text_color=CLR_TEAL_TEXT,
                fg_color=CLR_TEAL_LIGHT,
            )
        else:
            self._lbl_hdr_workbook.configure(
                text="○ NO WORKBOOK",
                text_color=CLR_TEXT_MUTED,
                fg_color=CLR_PANEL_SEC,
            )

        if hasattr(self, "_badge_sys_active") and self._badge_sys_active:
            if is_connected:
                self._badge_sys_active.configure(text="● SYSTEM ACTIVE", text_color=CLR_SUCCESS_NEON)
            else:
                self._badge_sys_active.configure(text="○ SYSTEM STANDBY", text_color=CLR_TEXT_MUTED)

    def _build_main_workspace(self):
        workspace = ctk.CTkFrame(self, fg_color="transparent")
        workspace.pack(fill="both", expand=True, padx=16, pady=(12, 6))

        # ── LEFT COLUMN: Connection Hub & Excel Setup (~265px) ───────
        left_col = ctk.CTkFrame(workspace, width=265, fg_color="transparent")
        left_col.pack(side="left", fill="y", padx=(0, 12))
        left_col.pack_propagate(False)

        self._build_connection_hub(left_col)
        self._build_excel_hub(left_col)

        # ── RIGHT AREA: Cockpit & Excel Live Monitoring ──────────────
        right_area = ctk.CTkFrame(workspace, fg_color="transparent")
        right_area.pack(side="left", fill="both", expand=True)

        # Top row: 2 cards side-by-side
        top_row = ctk.CTkFrame(right_area, fg_color="transparent")
        top_row.pack(fill="x", pady=(0, 12))

        # Card A: Live Angle Reading (left side of top row)
        self._build_live_angle_panel(top_row)

        # Card B: Cockpit Controls & Manual Save (right side of top row)
        self._build_cockpit_control_bar(top_row)

        # Middle row: Excel Live Monitoring & History (fills remaining vertical space)
        self._build_history_panel(right_area)

        # Initialize hidden measurement summary labels for test compatibility
        self._build_measurement_summary_card(None)

    # ═════════════════════════════════════════════════════════════════
    #  PANEL A: CONNECTION HUB
    # ═════════════════════════════════════════════════════════════════
    def _build_connection_hub(self, parent):
        card = CockpitCard(parent, title="CONNECTION HUB")
        card.pack(fill="x", pady=(0, 12))

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 14))

        # ── Row 1: IP Address & Port side-by-side ────────────────────
        ip_port_box = ctk.CTkFrame(body, fg_color="transparent")
        ip_port_box.pack(fill="x", pady=(0, 10))

        # IP Entry
        ip_col = ctk.CTkFrame(ip_port_box, fg_color="transparent")
        ip_col.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkLabel(
            ip_col, text="IP Address", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=CLR_TEXT_SECONDARY, anchor="w"
        ).pack(fill="x", pady=(0, 2))
        self._entry_ip = ctk.CTkEntry(
            ip_col,
            textvariable=self._var_ip,
            height=32,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            text_color=CLR_TEXT_PRIMARY,
        )
        self._entry_ip.pack(fill="x")

        # Port Entry
        port_col = ctk.CTkFrame(ip_port_box, fg_color="transparent", width=75)
        port_col.pack(side="right")
        ctk.CTkLabel(
            port_col, text="Port", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=CLR_TEXT_SECONDARY, anchor="w"
        ).pack(fill="x", pady=(0, 2))
        self._entry_port = ctk.CTkEntry(
            port_col,
            textvariable=self._var_port,
            height=32,
            width=75,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            text_color=CLR_TEXT_PRIMARY,
        )
        self._entry_port.pack(fill="x")

        # ── Row 2: Preset Command ────────────────────────────────────
        ctk.CTkLabel(
            body, text="Preset Command", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=CLR_TEXT_SECONDARY, anchor="w"
        ).pack(fill="x", pady=(0, 2))
        self._opt_cmd = ctk.CTkOptionMenu(
            body,
            values=["API1 ANGLE?", "API2 ANGLE?", "API1 AVG_ANGLE?", "*IDN?", "*ERR?"],
            command=self._on_preset_selected,
            height=32,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#FFFFFF",
            button_color="#E2E8F0",
            button_hover_color="#CBD5E1",
            text_color=CLR_TEXT_PRIMARY,
            dropdown_text_color=CLR_TEXT_PRIMARY,
            dropdown_fg_color="#FFFFFF",
            corner_radius=6,
        )
        self._opt_cmd.set("API1 ANGLE?")
        self._opt_cmd.pack(fill="x", pady=(0, 10))

        # ── Row 3: Simulator Mode Toggle Row ─────────────────────────
        sim_box = ctk.CTkFrame(body, fg_color="transparent")
        sim_box.pack(fill="x", pady=(0, 14))

        ctk.CTkLabel(
            sim_box, text="Simulator Mode", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=CLR_TEXT_PRIMARY
        ).pack(side="left")

        self._chk_sim = ctk.CTkSwitch(
            sim_box,
            text="",
            variable=self._var_sim,
            width=42,
            progress_color=CLR_ACCENT_BLUE,
            command=self._on_sim_toggled,
        )
        self._chk_sim.pack(side="right")

        # ── Row 4: Action Buttons (CONNECT / DISCONNECT side by side) ──
        btn_action_box = ctk.CTkFrame(body, fg_color="transparent")
        btn_action_box.pack(fill="x", pady=(0, 8))

        self._btn_connect = ctk.CTkButton(
            btn_action_box,
            text="CONNECT",
            height=36,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            fg_color=CLR_SUCCESS_NEON,
            hover_color="#15803D",
            text_color="#FFFFFF",
            corner_radius=6,
            command=self._connect_hardware,
        )
        self._btn_connect.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self._btn_disconnect = ctk.CTkButton(
            btn_action_box,
            text="■ DISCONNECT",
            height=36,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            fg_color=CLR_PANEL_SEC,
            hover_color="#CBD5E1",
            text_color=CLR_TEXT_MUTED,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            state="disabled",
            command=self._disconnect_hardware,
        )
        self._btn_disconnect.pack(side="right", fill="x", expand=True, padx=(4, 0))

        # ── Row 5: Real-time Connection Status Indicator ──────────────
        conn_status_box = ctk.CTkFrame(body, fg_color=CLR_PANEL_SEC, border_color=CLR_BORDER, border_width=1, corner_radius=6)
        conn_status_box.pack(fill="x", ipady=4, pady=(0, 4))

        self._conn_dot = TelemetryStatusDot(conn_status_box)
        self._conn_dot.pack(side="left", padx=(8, 4))

        self._lbl_conn_status = ctk.CTkLabel(
            conn_status_box,
            text="DISCONNECTED",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_DANGER_RED,
            anchor="w",
        )
        self._lbl_conn_status.pack(side="left", fill="x", expand=True)

        # Hidden compatibility references for direct query testing
        dummy_conn = ctk.CTkFrame(body, fg_color="transparent")
        self._entry_cmd = ctk.CTkEntry(dummy_conn, textvariable=self._var_cmd)
        self._btn_test = ctk.CTkButton(dummy_conn, text="✦ TEST", command=self._on_test_query)

    def _on_sim_toggled(self):
        is_sim = self._var_sim.get()
        if is_sim:
            self._log("✦ Simulator Mode enabled. Physical hardware not required.")
        else:
            self._log("✦ Simulator Mode disabled. Production Ethernet mode active.")
        self._update_header_status()
        self._update_measurement_summary()

    # ═════════════════════════════════════════════════════════════════
    #  PANEL B: WORKBOOK SETUP (LEFT COLUMN)
    # ═════════════════════════════════════════════════════════════════
    def _build_excel_hub(self, parent):
        card = CockpitCard(parent, title="WORKBOOK SETUP")
        card.pack(fill="x")

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=16, pady=(0, 16))

        # Master Workbook File Label
        ctk.CTkLabel(
            body, text="Master Workbook", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=CLR_TEXT_SECONDARY, anchor="w"
        ).pack(fill="x", pady=(0, 2))

        # Path Entry & Browse Button Row
        wb_box = ctk.CTkFrame(body, fg_color="transparent")
        wb_box.pack(fill="x", pady=(0, 10))

        self._lbl_excel_file = ctk.CTkLabel(
            wb_box,
            textvariable=self._var_excel_path,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=CLR_TEXT_PRIMARY,
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            height=32,
            anchor="w",
            padx=10,
        )
        self._lbl_excel_file.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self._btn_browse = ctk.CTkButton(
            wb_box,
            text="Browse",
            width=65,
            height=32,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            border_width=1,
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            corner_radius=6,
            command=self._browse_excel,
        )
        self._btn_browse.pack(side="right")

        # Active Worksheet Label & OptionMenu
        ctk.CTkLabel(
            body, text="Active Worksheet", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=CLR_TEXT_SECONDARY, anchor="w"
        ).pack(fill="x", pady=(0, 2))

        default_gmf_sheets = [
            "ACCURACY STALL",
            "CAM OUTPUT STALL",
            "ACCURACY AUTOPILOT",
            "CAM OUTPUT AUTOPILOT",
            "ACCURACY FLAP",
            "CAM OUTPUT FLAP",
        ]
        self._opt_sheet = ctk.CTkOptionMenu(
            body,
            values=default_gmf_sheets,
            command=self._on_sheet_changed,
            height=32,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="#FFFFFF",
            button_color="#E2E8F0",
            button_hover_color="#CBD5E1",
            text_color=CLR_TEXT_PRIMARY,
            dropdown_text_color=CLR_TEXT_PRIMARY,
            dropdown_fg_color="#FFFFFF",
            corner_radius=6,
        )
        self._opt_sheet.set("ACCURACY STALL")
        self._opt_sheet.pack(fill="x")

        # Hidden compatibility references
        dummy = ctk.CTkFrame(body, fg_color="transparent")
        self._lbl_wb_status = ctk.CTkLabel(dummy, text="NO WORKBOOK")
        self._btn_scan_empty = ctk.CTkButton(dummy, text="SCAN KOSONG", command=self._scan_and_jump_next_empty)
        self._btn_reset_row = ctk.CTkButton(dummy, text="RESET ROW", command=self._restart_sampling_dialog)
        self._badge_saved = ctk.CTkLabel(dummy, text="[ 0 SAVED ]")
        self._badge_row = ctk.CTkLabel(dummy, text="NEXT ROW [ 11 ]")

    # ═════════════════════════════════════════════════════════════════
    #  PANEL: LIVE ANGLE READING (TOP ROW LEFT)
    # ═════════════════════════════════════════════════════════════════
    def _build_live_angle_panel(self, parent):
        self._card_gauge = CockpitCard(parent, title="LIVE ANGLE READING — NAI API 8810A")
        self._card_gauge.pack(side="left", fill="both", expand=True, padx=(0, 6))

        body = ctk.CTkFrame(self._card_gauge, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(0, 14))

        # Left Column: Readout, prominent verdict badge, and status indicators
        readout_col = ctk.CTkFrame(body, fg_color="transparent")
        readout_col.pack(side="left", fill="both", expand=True, padx=(10, 10))

        # Horizontal Row for Angle Readout + Prominent PASS/FAIL Badge
        readout_header_row = ctk.CTkFrame(readout_col, fg_color="transparent")
        readout_header_row.pack(anchor="w", pady=(10, 4))

        self._lbl_angle_readout = ctk.CTkLabel(
            readout_header_row,
            text=f"{self._live_value:.2f}°" if self._live_value is not None else "17.50°",
            font=ctk.CTkFont(family="Segoe UI", size=46, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
            anchor="w",
        )
        self._lbl_angle_readout.pack(side="left", padx=(0, 14))

        # Prominent Verdict Badge (PASS / FAIL / N/A)
        self._badge_verdict_frame = ctk.CTkFrame(
            readout_header_row,
            fg_color="#F1F5F9",
            border_color="#CBD5E1",
            border_width=2,
            corner_radius=8,
            height=38,
        )
        self._badge_verdict_frame.pack(side="left", pady=4)

        self._lbl_verdict_badge = ctk.CTkLabel(
            self._badge_verdict_frame,
            text="— N/A",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color="#64748B",
            padx=16,
            pady=4,
        )
        self._lbl_verdict_badge.pack()

        # Row of Status Badges: SENSOR STABILITY + ACTIVE DIRECTION
        badges_row = ctk.CTkFrame(readout_col, fg_color="transparent")
        badges_row.pack(anchor="w", pady=(2, 4))

        self._badge_sensor_frame = ctk.CTkFrame(
            badges_row,
            fg_color="#F1F5F9",
            border_color="#CBD5E1",
            border_width=1,
            corner_radius=6,
            height=26,
        )
        self._badge_sensor_frame.pack(side="left", padx=(0, 8))

        self._lbl_stability_badge = ctk.CTkLabel(
            self._badge_sensor_frame,
            text="WAITING FOR MOTION",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_TEXT_MUTED,
            padx=10,
            pady=2,
        )
        self._lbl_stability_badge.pack()


        self._badge_direction_frame = ctk.CTkFrame(
            badges_row,
            fg_color="#E0F2FE",
            border_color="#BAE6FD",
            border_width=1,
            corner_radius=6,
            height=26,
        )
        self._badge_direction_frame.pack(side="left")

        self._lbl_direction_pill = ctk.CTkLabel(
            self._badge_direction_frame,
            text="↻ CW ACTIVE",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_ACCENT_BLUE,
            padx=10,
            pady=2,
        )
        self._lbl_direction_pill.pack()

        # Engineering Setpoint Details (Target, Deviation, Tolerance)
        self._lbl_setpoint_info = ctk.CTkLabel(
            readout_col,
            text="TARGET: —  |  DEV: —  |  TOL: ±1.50°",
            font=ctk.CTkFont(family="Consolas", size=10, weight="bold"),
            text_color=CLR_TEXT_SECONDARY,
        )
        self._lbl_setpoint_info.pack(anchor="w", pady=(2, 0))

        self._lbl_last_update = ctk.CTkLabel(
            readout_col,
            text="✦ LIVE TELEMETRY STREAM ACTIVE",
            font=ctk.CTkFont(family="Consolas", size=8),
            text_color=CLR_TEXT_MUTED,
        )
        self._lbl_last_update.pack(anchor="w", pady=(2, 0))

        # Right Column: Circular 360-degree Protractor Dial Gauge
        self._gauge_canvas = TelemetryGaugeCanvas(body, width=180, height=180)
        self._gauge_canvas.pack(side="right", padx=(10, 10), pady=(0, 4))

    # ═════════════════════════════════════════════════════════════════
    #  PANEL: MEASUREMENT SUMMARY (OFF-SCREEN STATE HOLDER)
    # ═════════════════════════════════════════════════════════════════
    def _build_measurement_summary_card(self, parent=None):
        """State holder for measurement evaluation summary metrics (removed from UI per user request)."""
        dummy_parent = ctk.CTkFrame(self, fg_color="transparent")
        cell_configs = [
            ("WORKSHEET", "_lbl_sum_sheet", "—", CLR_TEXT_PRIMARY, 10),
            ("TEST POINT", "_lbl_sum_tp", "—", CLR_ACCENT_CYAN, 11),
            ("EXPECTED (D)", "_lbl_sum_expected", "—", CLR_TEXT_PRIMARY, 11),
            ("MEASURED (G)", "_lbl_sum_measured", "—", CLR_ACCENT_CYAN, 12),
            ("DIFF (I)", "_lbl_sum_deviation", "—", CLR_TEXT_PRIMARY, 11),
            ("TOL (E/F)", "_lbl_sum_tolerance", "±1.50°", CLR_TEXT_SECONDARY, 11),
            ("RESULT", "_lbl_sum_eval", "NOT EVAL", CLR_TEXT_MUTED, 11),
            ("SAVE STATUS", "_lbl_sum_save_status", "READY", CLR_TEXT_MUTED, 11),
        ]
        for title, attr_name, default_val, val_color, font_sz in cell_configs:
            lbl = ctk.CTkLabel(dummy_parent, text=default_val)
            setattr(self, attr_name, lbl)

    def _update_measurement_summary(
        self,
        measured: Optional[float] = None,
        evaluation: Optional[MeasurementEvaluation] = None,
        save_status: Optional[str] = None,
        selected_record: Optional[dict] = None,
    ):
        """Update the 8 telemetry cells in the Measurement Summary Card."""
        if not hasattr(self, "_lbl_sum_sheet") or self._lbl_sum_sheet is None:
            return

        sheet_text = self._active_sheet_name or "—"
        self._lbl_sum_sheet.configure(text=sheet_text)

        if selected_record:
            tp = selected_record.get("test_point") or f"Row {selected_record.get('excel_row')}"
            sp = selected_record.get("setpoint")
            val = selected_record.get("measured_angle")
            dev = selected_record.get("deviation")
            tol = selected_record.get("tolerance")
            status = selected_record.get("status")
            s_status = selected_record.get("save_status", "SAVED")

            self._lbl_sum_tp.configure(text=str(tp))
            self._lbl_sum_expected.configure(
                text=f"{sp:.2f}°" if sp is not None and isinstance(sp, (int, float)) else (str(sp) if sp is not None else "—")
            )
            self._lbl_sum_measured.configure(
                text=f"{val:.2f}°" if val is not None and isinstance(val, (int, float)) else "—"
            )
            self._lbl_sum_deviation.configure(
                text=f"{dev:+.2f}°" if dev is not None and isinstance(dev, (int, float)) else "—"
            )
            self._lbl_sum_tolerance.configure(
                text=f"±{tol:.2f}°" if tol is not None and isinstance(tol, (int, float)) else "±1.50°"
            )

            if status == EvaluationStatus.PASS or str(status) == "EvaluationStatus.PASS" or "PASS" in str(status):
                self._lbl_sum_eval.configure(text="PASS ✓", text_color=CLR_SUCCESS_NEON)
            elif status == EvaluationStatus.FAIL or str(status) == "EvaluationStatus.FAIL" or "FAIL" in str(status):
                self._lbl_sum_eval.configure(text="FAIL ✗", text_color=CLR_DANGER_RED)
            else:
                self._lbl_sum_eval.configure(text="NOT EVAL", text_color=CLR_TEXT_MUTED)

            if s_status == "SAVED":
                self._lbl_sum_save_status.configure(text="SAVED", text_color=CLR_SUCCESS_NEON)
            elif s_status == "EDITED":
                self._lbl_sum_save_status.configure(text="EDITED", text_color=CLR_WARNING_AMBER)
            elif s_status == "SAVE FAILED":
                self._lbl_sum_save_status.configure(text="SAVE FAILED", text_color=CLR_DANGER_RED)
            elif s_status == "PENDING":
                self._lbl_sum_save_status.configure(text="PENDING", text_color=CLR_WARNING_AMBER)
            else:
                self._lbl_sum_save_status.configure(text=str(s_status), text_color=CLR_TEXT_SECONDARY)
            return

        target_row = self._current_row
        target_tp = f"Row {target_row}"
        target_sp = None
        target_tol = 1.50

        if self._workbook and self._active_sheet_name:
            try:
                ws = self._workbook[self._active_sheet_name]
                tp_val = ws.cell(row=target_row, column=1).value
                if tp_val is not None:
                    target_tp = str(tp_val)
                sp_val, tol_val = self._ctrl._evaluation.extract_row_reference(
                    ws, target_row, setpoint_col="D", tolerance_col="E"
                )
                if sp_val is not None:
                    target_sp = sp_val
                if tol_val is not None:
                    target_tol = tol_val or 1.50
            except Exception:
                pass

        try:
            sp_ovr = self._var_target_setpoint.get().strip()
            if sp_ovr:
                target_sp = float(sp_ovr)
        except ValueError:
            pass
        try:
            tol_ovr = self._var_target_tolerance.get().strip()
            if tol_ovr:
                target_tol = float(tol_ovr)
        except ValueError:
            pass

        self._lbl_sum_tp.configure(text=str(target_tp))
        self._lbl_sum_expected.configure(text=f"{target_sp:.2f}°" if target_sp is not None else "—")
        self._lbl_sum_tolerance.configure(text=f"±{target_tol:.2f}°" if target_tol is not None else "±1.50°")

        meas_val = measured if measured is not None else self._live_value
        if meas_val is not None:
            self._lbl_sum_measured.configure(text=f"{meas_val:.2f}°")
            if target_sp is not None:
                eval_res = evaluation or self._ctrl.evaluate_measurement(meas_val, target_sp, target_tol)
                dev = eval_res.deviation
                self._lbl_sum_deviation.configure(text=f"{dev:+.2f}°" if dev is not None else "—")
                if eval_res.status == EvaluationStatus.PASS:
                    self._lbl_sum_eval.configure(text="PASS ✓", text_color=CLR_SUCCESS_NEON)
                elif eval_res.status == EvaluationStatus.FAIL:
                    self._lbl_sum_eval.configure(text="FAIL ✗", text_color=CLR_DANGER_RED)
                else:
                    self._lbl_sum_eval.configure(text="NOT EVAL", text_color=CLR_TEXT_MUTED)
            else:
                self._lbl_sum_deviation.configure(text="—")
                self._lbl_sum_eval.configure(text="NOT EVAL", text_color=CLR_TEXT_MUTED)
        else:
            self._lbl_sum_measured.configure(text="—")
            self._lbl_sum_deviation.configure(text="—")
            self._lbl_sum_eval.configure(text="NOT EVAL", text_color=CLR_TEXT_MUTED)

        if save_status:
            if save_status == "SAVED":
                self._lbl_sum_save_status.configure(text="SAVED", text_color=CLR_SUCCESS_NEON)
            elif save_status == "SAVE FAILED":
                self._lbl_sum_save_status.configure(text="SAVE FAILED", text_color=CLR_DANGER_RED)
            elif save_status == "PENDING":
                self._lbl_sum_save_status.configure(text="PENDING", text_color=CLR_WARNING_AMBER)
            else:
                self._lbl_sum_save_status.configure(text=save_status, text_color=CLR_TEXT_SECONDARY)
        else:
            self._lbl_sum_save_status.configure(text="READY", text_color=CLR_ACCENT_CYAN)

    # ═════════════════════════════════════════════════════════════════
    #  PANEL: COCKPIT CONTROLS & MANUAL SAVE (TOP ROW RIGHT)
    # ═════════════════════════════════════════════════════════════════
    def _build_cockpit_control_bar(self, parent):
        card = CockpitCard(parent, title="COCKPIT CONTROLS & MANUAL SAVE")
        card.pack(side="right", fill="both", expand=True, padx=(6, 0))

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(0, 14))

        # 1. Light-Teal Highlighted Container for Target Row
        target_container = ctk.CTkFrame(
            body,
            fg_color=CLR_TEAL_LIGHT,
            border_color=CLR_TEAL_BORDER,
            border_width=1,
            corner_radius=8,
            height=44,
        )
        target_container.pack(fill="x", pady=(4, 10))
        target_container.pack_propagate(False)

        lbl_tr = ctk.CTkLabel(
            target_container,
            text="Target Row",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
        )
        lbl_tr.pack(side="left", padx=(14, 10))

        # Stepper Controls
        stepper_box = ctk.CTkFrame(target_container, fg_color="transparent")
        stepper_box.pack(side="right", padx=10)

        ctk.CTkButton(
            stepper_box,
            text="—",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            width=28,
            height=28,
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color="#CBD5E1",
            border_width=1,
            corner_radius=4,
            command=lambda: self._nudge_target_row(-1),
        ).pack(side="left", padx=2)

        self._entry_row = ctk.CTkEntry(
            stepper_box,
            textvariable=self._var_target_row,
            font=ctk.CTkFont(family="Consolas", size=13, weight="bold"),
            width=46,
            height=28,
            corner_radius=4,
            border_color="#CBD5E1",
            fg_color="#FFFFFF",
            text_color=CLR_TEXT_PRIMARY,
            justify="center",
        )
        self._entry_row.pack(side="left", padx=2)
        self._entry_row.bind("<Return>", lambda e: self._apply_target_row())
        self._entry_row.bind("<KP_Enter>", lambda e: self._apply_target_row())

        ctk.CTkButton(
            stepper_box,
            text="+",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            width=28,
            height=28,
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color="#CBD5E1",
            border_width=1,
            corner_radius=4,
            command=lambda: self._nudge_target_row(1),
        ).pack(side="left", padx=2)

        self._btn_edit = ctk.CTkButton(
            stepper_box,
            text="✎",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            width=28,
            height=28,
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_SECONDARY,
            border_color="#CBD5E1",
            border_width=1,
            corner_radius=4,
            command=self._edit_selected_row,
        )
        self._btn_edit.pack(side="left", padx=(4, 0))

        # 2. Test Direction Control (CW / CCW)
        dir_box = ctk.CTkFrame(body, fg_color="transparent")
        dir_box.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(
            dir_box, text="Test Direction", font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"), text_color=CLR_TEXT_PRIMARY
        ).pack(side="left")

        self._seg_direction = ctk.CTkSegmentedButton(
            dir_box,
            values=["↻ CW", "↺ CCW"],
            command=self._on_direction_selected,
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            selected_color=CLR_ACCENT_BLUE,
            selected_hover_color="#1D4ED8",
            unselected_color="#FFFFFF",
            unselected_hover_color="#F1F5F9",
            corner_radius=6,
            height=28,
        )
        self._seg_direction.set("↻ CW")
        self._seg_direction.pack(side="right")

        # 3. Auto-Save Row with Switch
        as_box = ctk.CTkFrame(body, fg_color="transparent")
        as_box.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(
            as_box, text="Auto-Save", font=ctk.CTkFont(family="Segoe UI", size=12), text_color=CLR_TEXT_PRIMARY
        ).pack(side="left")

        self._switch_auto_save = ctk.CTkSwitch(
            as_box,
            text="",
            variable=self._var_auto_save,
            width=42,
            progress_color=CLR_ACCENT_BLUE,
            command=self._on_auto_save_toggled,
        )
        self._switch_auto_save.pack(side="right")

        # 3. Full-width Corporate Blue Button: SAVE DATA (ENTER)
        self._btn_manual_save = ctk.CTkButton(
            body,
            text="SAVE DATA (ENTER)",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=CLR_ACCENT_BLUE,
            hover_color="#1D4ED8",
            corner_radius=6,
            height=36,
            command=self._do_manual_save,
        )
        self._btn_manual_save.pack(fill="x", pady=(0, 8))

        # 4. Secondary Buttons Row (3 outline buttons)
        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x")

        self._btn_pause = ctk.CTkButton(
            btn_row,
            text="PAUSE (SPACE)",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            height=30,
            command=self._toggle_auto_save_pause,
        )
        self._btn_pause.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self._btn_undo_action = ctk.CTkButton(
            btn_row,
            text="UNDO LAST",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            height=30,
            command=self._undo_last_record,
        )
        self._btn_undo_action.pack(side="left", fill="x", expand=True, padx=4)

        self._btn_restart_bar = ctk.CTkButton(
            btn_row,
            text=f"RESET ROW {self._current_row}",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            height=30,
            command=self._restart_sampling_dialog,
        )
        self._btn_restart_bar.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Hidden state holder widgets for test compatibility
        dummy = ctk.CTkFrame(body, fg_color="transparent")
        self._seg_mode = ctk.CTkSegmentedButton(dummy, values=["AUTO-SAVE ON", "MANUAL MODE"], command=self._on_save_mode_changed)
        self._seg_mode.set("AUTO-SAVE ON")
        self._lbl_mode_badge = ctk.CTkLabel(dummy, text="● AUTO-SAVE ENABLED")
        self._lbl_action_banner = ctk.CTkLabel(dummy, text="✦ AUTO-SAVE AKTIF")
        self._lbl_cockpit_target_row = ctk.CTkLabel(dummy, text=f"🎯 TARGET ROW [ {self._current_row} ]")

        # Bind keyboard shortcuts (sekali saja — hindari duplikasi)
        self.bind("<space>", self._on_space_pressed)
        self.bind("<Return>", self._on_enter_pressed)
        self.bind("<KP_Enter>", self._on_enter_pressed)

    # ═════════════════════════════════════════════════════════════════
    #  PANEL: OPERATIONS & UTILITIES (RIGHT COLUMN TOP)
    # ═════════════════════════════════════════════════════════════════
    def _build_operations_utilities_card(self, parent):
        card = CockpitCard(parent, title="WORKBOOK OPERATIONS")
        card.pack(fill="x", pady=(0, 6))

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=10, pady=(2, 8))

        r1 = ctk.CTkFrame(body, fg_color="transparent")
        r1.pack(fill="x", pady=(0, 4))

        self._btn_export_action = ctk.CTkButton(
            r1,
            text="📥 EXPORT...",
            height=34,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            fg_color="#F8FAFC",
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            hover_color="#E2E8F0",
            text_color=CLR_TEXT_PRIMARY,
            command=self._export_saveas,
        )
        self._btn_export_action.pack(side="left", fill="x", expand=True, padx=(0, 2))

        self._btn_refresh_action = ctk.CTkButton(
            r1,
            text="🔄 REFRESH VIEW",
            height=34,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            fg_color="#F8FAFC",
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            hover_color="#E2E8F0",
            text_color=CLR_ACCENT_BLUE,
            command=self._refresh_excel_live_view,
        )
        self._btn_refresh_action.pack(side="right", fill="x", expand=True, padx=(2, 0))

    def _build_save_mode_card(self, parent):
        """Backward-compatibility stub."""
        pass

    def _build_action_buttons_card(self, parent):
        """Backward-compatibility stub."""
        pass

    # ═════════════════════════════════════════════════════════════════
    #  PANEL: EXCEL LIVE VIEW & MEASUREMENT HISTORY (RIGHT COLUMN MIDDLE)
    # ═════════════════════════════════════════════════════════════════
    def _build_history_panel(self, parent):
        card = CockpitCard(parent, title="EXCEL LIVE MONITORING & HISTORY")
        card.pack(fill="both", expand=True)

        # Header controls at top-right
        t_header_box = ctk.CTkFrame(card, fg_color="transparent")
        t_header_box.place(relx=1.0, rely=0.012, anchor="ne", x=-16, y=8)

        # Auto-Scroll Toggle Checkbox
        self._chk_auto_scroll = ctk.CTkCheckBox(
            t_header_box,
            text="Auto-Scroll",
            variable=self._var_auto_scroll,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=CLR_TEXT_SECONDARY,
            width=20,
            checkbox_width=16,
            checkbox_height=16,
        )
        self._chk_auto_scroll.pack(side="left", padx=(0, 12))

        lbl_sp = ctk.CTkLabel(
            t_header_box,
            text="Setpoint (°)",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=CLR_TEXT_SECONDARY,
        )
        lbl_sp.pack(side="left", padx=(0, 4))

        self._entry_target_setpoint = ctk.CTkEntry(
            t_header_box,
            textvariable=self._var_target_setpoint,
            width=50,
            height=26,
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            text_color=CLR_TEXT_PRIMARY,
            justify="center",
            placeholder_text="0",
        )
        self._entry_target_setpoint.pack(side="left", padx=(0, 10))
        self._entry_target_setpoint.bind("<Return>", lambda e: self._refresh_excel_live_view())

        lbl_tol = ctk.CTkLabel(
            t_header_box,
            text="Tolerance (±°)",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=CLR_TEXT_SECONDARY,
        )
        lbl_tol.pack(side="left", padx=(0, 4))

        self._entry_target_tolerance = ctk.CTkEntry(
            t_header_box,
            textvariable=self._var_target_tolerance,
            width=50,
            height=26,
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            text_color=CLR_TEXT_PRIMARY,
            justify="center",
            placeholder_text="0",
        )
        self._entry_target_tolerance.pack(side="left", padx=(0, 10))
        self._entry_target_tolerance.bind("<Return>", lambda e: self._refresh_excel_live_view())

        self._btn_refresh_live = ctk.CTkButton(
            t_header_box,
            text="⟳",
            font=ctk.CTkFont(family="Segoe UI", size=14),
            width=30,
            height=26,
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=4,
            command=self._refresh_excel_live_view,
        )
        self._btn_refresh_live.pack(side="left")

        # ── Row Management Quick Toolbar ──────────────────────────────
        row_bar = ctk.CTkFrame(card, fg_color=CLR_PANEL_SEC, height=36, corner_radius=6, border_color=CLR_BORDER, border_width=1)
        row_bar.pack(fill="x", padx=16, pady=(8, 0))
        row_bar.pack_propagate(False)

        ctk.CTkLabel(
            row_bar,
            text="ROW ACTIONS:",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=CLR_TEXT_SECONDARY,
        ).pack(side="left", padx=(10, 8), pady=4)

        self._btn_edit_row_toolbar = ctk.CTkButton(
            row_bar,
            text="✎ Edit Row",
            width=80,
            height=26,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            border_width=1,
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            command=self._edit_selected_row,
        )
        self._btn_edit_row_toolbar.pack(side="left", padx=3, pady=4)

        self._btn_clear_row_toolbar = ctk.CTkButton(
            row_bar,
            text="⌫ Clear Row",
            width=85,
            height=26,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            border_width=1,
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            command=self._clear_selected_row,
        )
        self._btn_clear_row_toolbar.pack(side="left", padx=3, pady=4)

        self._btn_clear_range_toolbar = ctk.CTkButton(
            row_bar,
            text="✂ Clear Range (81-114)",
            width=140,
            height=26,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            fg_color="#FFFFFF",
            border_color=CLR_BORDER,
            border_width=1,
            hover_color="#FEE2E2",
            text_color=CLR_DANGER_RED,
            command=self._clear_row_range_action,
        )
        self._btn_clear_range_toolbar.pack(side="left", padx=3, pady=4)

        self._btn_restart_row_toolbar = ctk.CTkButton(
            row_bar,
            text="⟲ Restart From Row...",
            width=145,
            height=26,
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            fg_color=CLR_ACCENT_BLUE,
            hover_color="#1D4ED8",
            text_color="#FFFFFF",
            command=lambda: self._restart_measurement_action(),
        )
        self._btn_restart_row_toolbar.pack(side="left", padx=3, pady=4)

        ctk.CTkLabel(
            row_bar,
            text="💡 Tip: Klik kanan baris pada tabel untuk menu cepat",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=CLR_TEXT_MUTED,
        ).pack(side="right", padx=12, pady=4)

        # 13-Column Live Monitoring Table
        table_container = ctk.CTkFrame(card, fg_color="transparent")
        table_container.pack(fill="both", expand=True, padx=16, pady=(10, 14))

        live_cols = (
            "no", "row", "tp", "turns", "shaft", "setpoint", "top_limit", "bottom_limit", "measured", "result", "deviation", "real_val", "status"
        )
        self._tree_live = ttk.Treeview(
            table_container,
            columns=live_cols,
            show="headings",
            style="CleanLight.Treeview",
            selectmode="browse",
        )

        headings_map = {
            "no": ("#", 35),
            "row": ("ROW", 45),
            "tp": ("TEST POINT", 85),
            "turns": ("INPUT TURN", 80),
            "shaft": ("SHAFT ANGLE", 90),
            "setpoint": ("SYNCRO ELECT. OUTPUT", 140),
            "top_limit": ("TOP LIMIT", 80),
            "bottom_limit": ("BOTTOM LIMIT", 90),
            "measured": ("RESULT", 80),
            "result": ("REMARK", 75),
            "deviation": ("DIFFERENCE", 85),
            "real_val": ("REAL VALUE", 85),
            "status": ("STATUS", 95),
        }
        for col_id, (col_txt, col_w) in headings_map.items():
            self._tree_live.heading(col_id, text=col_txt, anchor="center")
            self._tree_live.column(col_id, width=col_w, minwidth=col_w - 5, anchor="center")

        self._tree_live.tag_configure("pass", background="#DCFCE7", foreground="#15803D")
        self._tree_live.tag_configure("fail", background="#FEE2E2", foreground="#B91C1C")
        self._tree_live.tag_configure("not_evaluated", foreground=CLR_TEXT_MUTED)
        self._tree_live.tag_configure("evenrow", background="#FFFFFF")
        self._tree_live.tag_configure("oddrow", background="#F8FAFC")
        self._tree_live.tag_configure("edited", background="#FEF3C7", foreground=CLR_WARNING_AMBER)
        self._tree_live.tag_configure("latest_saved", background="#BBF7D0", foreground="#14532D")
        self._tree_live.tag_configure("active_row", background="#E0F2FE", foreground=CLR_ACCENT_BLUE)

        scroll_y_live = ttk.Scrollbar(table_container, orient="vertical", command=self._tree_live.yview)
        scroll_x_live = ttk.Scrollbar(table_container, orient="horizontal", command=self._tree_live.xview)
        self._tree_live.configure(yscrollcommand=scroll_y_live.set, xscrollcommand=scroll_x_live.set)

        self._tree_live.grid(row=0, column=0, sticky="nsew")
        scroll_y_live.grid(row=0, column=1, sticky="ns")
        scroll_x_live.grid(row=1, column=0, sticky="ew")
        table_container.grid_rowconfigure(0, weight=1)
        table_container.grid_columnconfigure(0, weight=1)

        self._tree_live.bind("<<TreeviewSelect>>", lambda e: self._on_live_tree_select())
        self._tree_live.bind("<Double-1>", lambda e: self._edit_selected_row())

        # Right-Click Context Menu
        self._tree_context_menu = tk.Menu(
            self,
            tearoff=0,
            font=("Segoe UI", 9),
            bg="#FFFFFF",
            fg=CLR_TEXT_PRIMARY,
            activebackground="#E0F2FE",
            activeforeground=CLR_ACCENT_BLUE,
        )
        self._tree_context_menu.add_command(label="✎  Edit Nilai Baris Ini...", command=self._edit_selected_row)
        self._tree_context_menu.add_command(label="⌫  Kosongkan / Hapus Baris Ini...", command=self._clear_selected_row)
        self._tree_context_menu.add_command(label="✂  Kosongkan Rentang Baris (Clear Range)...", command=self._clear_row_range_action)
        self._tree_context_menu.add_separator()
        self._tree_context_menu.add_command(label="⟲  Restart Pengukuran Mulai Baris Ini...", command=lambda: self._restart_measurement_action())
        self._tree_context_menu.add_separator()
        self._tree_context_menu.add_command(label="🔄  Refresh Live Table", command=self._refresh_excel_live_view)

        self._tree_live.bind("<Button-3>", self._show_tree_context_menu)

        # Compatibility tree & badges
        dummy_tree_frame = ctk.CTkFrame(card, fg_color="transparent")
        columns = ("no", "row", "result", "recorded")
        self._tree = ttk.Treeview(
            dummy_tree_frame,
            columns=columns,
            show="headings",
            style="Cockpit.Treeview",
            selectmode="browse",
        )
        for c in columns:
            self._tree.heading(c, text=c)

        self._history_badge = ctk.CTkLabel(t_header_box, text="0 RECORDS")
        self._lbl_sync_time = ctk.CTkLabel(t_header_box, text="")

    def _build_event_log_panel(self, parent):
        pass

    # ═════════════════════════════════════════════════════════════════
    #  FOOTER STATUS BAR (EVENT LOG FULL-WIDTH BOTTOM)
    # ═════════════════════════════════════════════════════════════════
    def _build_footer_bar(self):
        self._footer_card = CockpitCard(self)
        self._footer_card.pack(fill="x", side="bottom", padx=16, pady=(0, 10))

        # Log Header
        log_header = ctk.CTkFrame(self._footer_card, fg_color="transparent", height=28)
        log_header.pack(fill="x", padx=16, pady=(8, 4))

        lbl_log = ctk.CTkLabel(
            log_header,
            text="EVENT LOG",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=CLR_TEXT_SECONDARY,
        )
        lbl_log.pack(side="left")

        self._btn_collapse_log = ctk.CTkButton(
            log_header,
            text="^",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            width=26,
            height=22,
            fg_color="transparent",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_SECONDARY,
            command=self._toggle_collapse_log,
        )
        self._btn_collapse_log.pack(side="right")

        # Container for Text + Scrollbar
        self._log_body = ctk.CTkFrame(self._footer_card, fg_color="transparent", height=75)
        self._log_body.pack(fill="x", padx=16, pady=(0, 8))
        self._log_body.pack_propagate(False)

        log_container = tk.Frame(self._log_body, bg=CLR_INPUT_BG, highlightbackground=CLR_BORDER, highlightthickness=1)
        log_container.pack(fill="both", expand=True)

        self._log_text = tk.Text(
            log_container,
            bg=CLR_INPUT_BG,
            fg=CLR_TEXT_PRIMARY,
            insertbackground=CLR_ACCENT_CYAN,
            font=("Consolas", 9),
            wrap="word",
            bd=0,
            padx=8,
            pady=4,
            state="disabled",
        )
        scrollbar = ttk.Scrollbar(log_container, orient="vertical", command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=scrollbar.set)
        self._log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._log_text.tag_config("tag_ts", foreground=CLR_TEXT_MUTED)
        self._log_text.tag_config("tag_success", foreground=CLR_SUCCESS_NEON)
        self._log_text.tag_config("tag_info", foreground=CLR_ACCENT_CYAN)
        self._log_text.tag_config("tag_warn", foreground=CLR_WARNING_AMBER)
        self._log_text.tag_config("tag_error", foreground=CLR_DANGER_RED)
        self._log_text.tag_config("tag_norm", foreground=CLR_TEXT_PRIMARY)

        # Offscreen / status label for compatibility
        self._lbl_footer_status = ctk.CTkLabel(log_header, text="Ready")

    def _toggle_collapse_log(self):
        if not hasattr(self, "_is_log_expanded"):
            self._is_log_expanded = True
        self._is_log_expanded = not self._is_log_expanded
        if self._is_log_expanded:
            self._log_body.pack(fill="x", padx=16, pady=(0, 8))
            self._btn_collapse_log.configure(text="^")
        else:
            self._log_body.pack_forget()
            self._btn_collapse_log.configure(text="v")

    # ═════════════════════════════════════════════════════════════════
    #  TARGET ROW MANAGEMENT & SAMPLING RESET
    # ═════════════════════════════════════════════════════════════════
    def _set_active_row(self, row: int, log_reason: str = ""):
        """Set target Excel recording row and synchronize UI entry and badges."""
        if row < 1:
            row = 1
        self._current_row = row
        self._cached_row = None
        self._cached_target_sp = None
        if hasattr(self, "_ctrl") and self._ctrl.is_workbook_loaded:
            self._ctrl.current_row = row
        self._var_target_row.set(str(row))
        if hasattr(self, "_badge_row"):
            self._badge_row.configure(text=f"NEXT ROW [ {self._current_row} ]")
        if hasattr(self, "_lbl_cockpit_target_row") and self._lbl_cockpit_target_row:
            self._lbl_cockpit_target_row.configure(text=f"🎯 TARGET ROW [ {self._current_row} ]")
        if hasattr(self, "_btn_restart_bar") and self._btn_restart_bar:
            self._btn_restart_bar.configure(text=f"⟲ RESET ROW {self._current_row}")
        if hasattr(self, "_btn_reset_row") and self._btn_reset_row:
            self._btn_reset_row.configure(text=f"⟲ RESET ROW {self._current_row}")
        if hasattr(self, "_update_measurement_summary"):
            self._update_measurement_summary()
        if log_reason:
            self._log(f"⟲ Target row diubah ke baris {self._current_row} ({log_reason})")

    def _apply_target_row(self, event=None):
        """Apply row number typed into the target row entry box."""
        raw = self._var_target_row.get().strip()
        try:
            val = int(raw)
            if val < 1:
                val = 1
            self._set_active_row(val, "Manual Input")
            if hasattr(self, "_ctrl") and self._ctrl.is_workbook_loaded and self._var_auto_save.get():
                self._ctrl.reset_auto_save()
        except ValueError:
            self._var_target_row.set(str(self._current_row))
            messagebox.showwarning(
                "Input Salah",
                "Nomor baris harus berupa angka bulat positif (misalnya: 11)."
            )

    def _nudge_target_row(self, delta: int):
        """Step target row up or down by 1."""
        new_row = max(1, self._current_row + delta)
        self._set_active_row(new_row, f"Nudge {delta:+d}")

    def _reset_row_to_default(self):
        """Reset target row pointer back to base row (Row 11)."""
        self._set_active_row(DEFAULT_START_ROW, "Reset ke baris 11")
        if hasattr(self, "_ctrl") and self._ctrl.is_workbook_loaded and self._var_auto_save.get():
            self._ctrl.reset_auto_save()
        self._lbl_footer_status.configure(text=f"Target Row reset to {DEFAULT_START_ROW}")
        self._log(f"✓ Target row di-reset ke baris {DEFAULT_START_ROW}. Sampling siap dimulai ulang.")
        threading.Thread(target=lambda: winsound.Beep(900, 120), daemon=True).start()

    def _restart_sampling_dialog(self):
        """Invoke industrial Restart Measurement Dialog with 3 explicit modes."""
        self._restart_measurement_action(start_row=self._current_row)

    def _scan_and_jump_next_empty(self):
        """Scan active worksheet and jump target pointer to first empty cell in column G."""
        next_empty = self._ctrl.scan_next_empty_row()
        self._set_active_row(next_empty, "Auto-scan baris kosong")
        self._log(f"⇥ Auto-scan: baris kosong berikutnya adalah baris {next_empty}")

    def _clear_sampling_data(self):
        """Clear all measurement values in column G from row 11+ to restart sampling clean."""
        if not self._ctrl.is_workbook_loaded or not self._active_sheet_name:
            messagebox.showinfo("Info", "Load file Excel master terlebih dahulu.")
            return

        sheet_name = self._active_sheet_name
        confirm = messagebox.askyesno(
            "Konfirmasi Bersihkan Sampling",
            f"Kosongkan seluruh data hasil pengukuran pada kolom {RESULT_COLUMN} "
            f"mulai baris {DEFAULT_START_ROW} di sheet '{sheet_name}'?\n\n"
            f"Gunakan fitur ini untuk memulai ulang sampling baru tanpa data lama menumpuk.",
        )
        if not confirm:
            return

        try:
            cleared_count = self._ctrl.clear_sampling_data()
        except PermissionError:
            messagebox.showerror(
                "File Terkunci",
                "File Excel sedang dibuka program lain.\nTutup file terlebih dahulu, lalu coba lagi."
            )
            self._log("✗ Gagal bersihkan data: File Excel sedang dibuka program lain.")
            return
        except Exception as exc:
            messagebox.showerror("Error", f"Gagal menyimpan workbook:\n{exc}")
            return

        self._history_data.clear()
        self._record_stack.clear()
        self._edited_rows.clear()
        self._save_count = 0
        self._set_active_row(DEFAULT_START_ROW, "Data dibersihkan")
        self._badge_saved.configure(text="[ 0 SAVED ]")
        self._refresh_history_table()
        self._refresh_excel_live_view()
        self._update_button_states()

        self._log(f"✓ Berhasil mengosongkan {cleared_count} baris data sampling pada sheet '{sheet_name}'. Target row di-reset ke {DEFAULT_START_ROW}.")
        self._lbl_footer_status.configure(text=f"Cleared {cleared_count} rows — Target Row reset to {DEFAULT_START_ROW}")
        threading.Thread(target=lambda: winsound.Beep(850, 150), daemon=True).start()

    # ═════════════════════════════════════════════════════════════════
    #  HARDWARE COMMUNICATION & TEST
    # ═════════════════════════════════════════════════════════════════
    def _on_preset_selected(self, choice: str):
        cmd = choice.split("(")[0].strip()
        self._var_cmd.set(cmd)

    def _set_connection_state(self, state: ConnectionState, detail_msg: str = ""):
        """Centralized, thread-safe UI connection state manager."""
        self._connection_state = state

        if state == ConnectionState.DISCONNECTED:
            if hasattr(self, "_btn_connect") and self._btn_connect:
                self._btn_connect.configure(state="normal", fg_color=CLR_SUCCESS_NEON)
            if hasattr(self, "_btn_disconnect") and self._btn_disconnect:
                self._btn_disconnect.configure(
                    state="disabled", fg_color=CLR_PANEL_SEC, text_color=CLR_TEXT_MUTED, border_color=CLR_BORDER
                )
            if hasattr(self, "_conn_dot") and self._conn_dot:
                self._conn_dot.set_state("off")
            if hasattr(self, "_lbl_conn_status") and self._lbl_conn_status:
                self._lbl_conn_status.configure(text="DISCONNECTED", text_color=CLR_DANGER_RED)
            if hasattr(self, "_lbl_footer_status") and self._lbl_footer_status:
                self._lbl_footer_status.configure(text="Disconnected")
            self._live_value = None
            if hasattr(self, "_lbl_angle_readout") and self._lbl_angle_readout:
                self._lbl_angle_readout.configure(text="--.--°")
            if hasattr(self, "_gauge_canvas") and self._gauge_canvas:
                self._gauge_canvas.set_value(None)

        elif state == ConnectionState.CONNECTING:
            if hasattr(self, "_btn_connect") and self._btn_connect:
                self._btn_connect.configure(state="disabled")
            if hasattr(self, "_btn_disconnect") and self._btn_disconnect:
                self._btn_disconnect.configure(state="disabled")
            if hasattr(self, "_conn_dot") and self._conn_dot:
                self._conn_dot.set_state("connecting")
            if hasattr(self, "_lbl_conn_status") and self._lbl_conn_status:
                self._lbl_conn_status.configure(text="CONNECTING...", text_color=CLR_WARNING_AMBER)
            if hasattr(self, "_lbl_footer_status") and self._lbl_footer_status:
                self._lbl_footer_status.configure(text="Connecting to instrument...")

        elif state == ConnectionState.CONNECTED:
            if hasattr(self, "_btn_connect") and self._btn_connect:
                self._btn_connect.configure(state="disabled")
            if hasattr(self, "_btn_disconnect") and self._btn_disconnect:
                self._btn_disconnect.configure(
                    state="normal",
                    fg_color="#FEE2E2",
                    hover_color="#FECACA",
                    text_color=CLR_DANGER_RED,
                    border_color="#FCA5A5",
                )
            if hasattr(self, "_conn_dot") and self._conn_dot:
                self._conn_dot.set_state("on")
            mode_txt = f"ONLINE / {detail_msg}" if detail_msg else "ONLINE"
            if hasattr(self, "_lbl_conn_status") and self._lbl_conn_status:
                self._lbl_conn_status.configure(text=mode_txt, text_color=CLR_SUCCESS_NEON)
            if hasattr(self, "_lbl_footer_status") and self._lbl_footer_status:
                self._lbl_footer_status.configure(text=f"Connected — {detail_msg}")

        elif state == ConnectionState.DISCONNECTING:
            if hasattr(self, "_btn_connect") and self._btn_connect:
                self._btn_connect.configure(state="disabled")
            if hasattr(self, "_btn_disconnect") and self._btn_disconnect:
                self._btn_disconnect.configure(state="disabled")
            if hasattr(self, "_conn_dot") and self._conn_dot:
                self._conn_dot.set_state("connecting")
            if hasattr(self, "_lbl_conn_status") and self._lbl_conn_status:
                self._lbl_conn_status.configure(text="DISCONNECTING...", text_color=CLR_WARNING_AMBER)
            if hasattr(self, "_lbl_footer_status") and self._lbl_footer_status:
                self._lbl_footer_status.configure(text="Disconnecting instrument...")

        elif state == ConnectionState.CONNECTION_ERROR:
            if hasattr(self, "_btn_connect") and self._btn_connect:
                self._btn_connect.configure(state="normal", fg_color=CLR_SUCCESS_NEON)
            if hasattr(self, "_btn_disconnect") and self._btn_disconnect:
                self._btn_disconnect.configure(
                    state="disabled", fg_color=CLR_PANEL_SEC, text_color=CLR_TEXT_MUTED, border_color=CLR_BORDER
                )
            if hasattr(self, "_conn_dot") and self._conn_dot:
                self._conn_dot.set_state("off")
            err_txt = f"ERROR: {detail_msg}" if detail_msg else "CONNECTION ERROR"
            if hasattr(self, "_lbl_conn_status") and self._lbl_conn_status:
                self._lbl_conn_status.configure(text=err_txt[:26], text_color=CLR_DANGER_RED)
            if hasattr(self, "_lbl_footer_status") and self._lbl_footer_status:
                self._lbl_footer_status.configure(text=f"Connection Error: {detail_msg}")
            self._live_value = None
            if hasattr(self, "_lbl_angle_readout") and self._lbl_angle_readout:
                self._lbl_angle_readout.configure(text="--.--°")
            if hasattr(self, "_gauge_canvas") and self._gauge_canvas:
                self._gauge_canvas.set_value(None)

        self._update_header_status()
        self._update_measurement_summary()

    def _connect_hardware(self):
        """Initiate asynchronous Ethernet connection to NAI 8810A."""
        if getattr(self, "_connection_state", None) in (
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.DISCONNECTING,
        ):
            return

        ip = self._var_ip.get().strip()
        try:
            port = int(self._var_port.get().strip())
        except ValueError:
            port = 23

        sim = self._var_sim.get()
        cmd = self._var_cmd.get().strip()

        if not sim and not ip:
            messagebox.showwarning("Missing IP", "Masukkan IP Address NAI 8810A terlebih dahulu.")
            return

        self._set_connection_state(ConnectionState.CONNECTING)
        target_desc = "SIMULATOR" if sim else f"ETHERNET {ip}:{port}"
        self._log(f"Menghubungkan ke {target_desc}...")

        def connect_worker():
            reader = EthernetReader(
                host=ip,
                port=port,
                command=cmd,
                interval=POLL_INTERVAL_MS / 1000.0,
                on_data=self._on_hardware_data,
                on_error=self._on_hardware_error,
                on_log=self._on_hardware_log,
                simulator=sim,
            )
            success = reader.connect()
            if success:
                self.after(0, lambda: self._on_connect_success(reader, sim, ip, port))
            else:
                self.after(0, lambda: self._on_connect_failed("Gagal terhubung ke instrumen."))

        threading.Thread(target=connect_worker, daemon=True).start()

    def _on_connect_success(self, reader: EthernetReader, sim: bool, ip: str, port: int):
        if getattr(self, "_connection_state", None) != ConnectionState.CONNECTING:
            reader.disconnect()
            return

        self._reader = reader
        mode_str = "SIMULATOR" if sim else f"{ip}:{port}"
        self._set_connection_state(ConnectionState.CONNECTED, mode_str)
        self._log(f"Connected: {mode_str}")

        if hasattr(self, "_ctrl") and self._ctrl.is_workbook_loaded and self._var_auto_save.get():
            self._ctrl.enable_auto_save()
            self._log("✦ Auto-Save AKTIF: Sensor stabil / Motion controller akan otomatis dicatat ke Excel.")

    def _on_connect_failed(self, err_msg: str):
        self._reader = None
        self._set_connection_state(ConnectionState.CONNECTION_ERROR, err_msg)
        self._log(f"✗ Gagal membuka koneksi: {err_msg}")
        messagebox.showerror("Koneksi Gagal", f"Tidak dapat terhubung ke instrumen:\n{err_msg}")

    def _disconnect_hardware(self):
        """Terminate connection asynchronously and clean up socket/thread."""
        if getattr(self, "_connection_state", None) in (
            ConnectionState.DISCONNECTED,
            ConnectionState.DISCONNECTING,
        ):
            return

        self._set_connection_state(ConnectionState.DISCONNECTING)
        self._log("Memutuskan koneksi instrumen...")

        def disconnect_worker():
            if hasattr(self, "_ctrl"):
                self._ctrl.reset_auto_save()
            old_reader = self._reader
            self._reader = None
            if old_reader:
                try:
                    old_reader.disconnect()
                except Exception:
                    pass
            self.after(0, self._on_disconnect_complete)

        threading.Thread(target=disconnect_worker, daemon=True).start()

    def _on_disconnect_complete(self):
        self._set_connection_state(ConnectionState.DISCONNECTED)
        self._log("Instrument disconnected.")

    def _on_hardware_data(self, value: float):
        """
        Thread-safe telemetry callback with event coalescing.
        Directly feeds the stability engine (pure numerical calculations, non-blocking)
        and coalesces high-frequency GUI updates onto Tkinter idle cycles to prevent UI queue flooding.
        """
        self._ctrl.feed_angle(value, self._var_sim.get())
        self._latest_telemetry_value = value
        if not self._display_update_pending:
            self._display_update_pending = True
            self.after_idle(self._flush_live_display)

    def _flush_live_display(self):
        """Flushes the coalesced latest telemetry value to the GUI widgets.
        Skips full render if value has not changed significantly (debounce 0.01°).
        """
        self._display_update_pending = False
        val = self._latest_telemetry_value
        if val is None:
            return
        # Debounce: skip jika perubahan < 0.01° untuk mengurangi beban render
        prev = self._last_displayed_value
        if prev is not None and abs(val - prev) < 0.01:
            return
        self._last_displayed_value = val
        self._update_live_display(val)

    def _on_hardware_error(self, msg: str):
        """Throttled error callback — max 1 error log per detik agar tidak spam GUI."""
        now = time.monotonic()
        if now - self._last_hw_err_time >= self._hw_log_throttle_sec:
            self._last_hw_err_time = now
            self.after(0, self._log, f"⚠ Instrument Error: {msg}")

        # Detect disconnection if reader reports error and is no longer connected
        if (
            self._reader is not None
            and not self._reader.is_connected
            and getattr(self, "_connection_state", None) == ConnectionState.CONNECTED
        ):
            self.after(0, lambda: self._set_connection_state(ConnectionState.CONNECTION_ERROR, "Terputus"))

    def _on_hardware_log(self, msg: str):
        """Throttled log callback — max 1 hardware log per detik agar tidak flood GUI queue.
        Tanpa throttle ini, polling 30ms menghasilkan 33+ after() calls/detik ke Text widget.
        """
        now = time.monotonic()
        if now - self._last_hw_log_time >= self._hw_log_throttle_sec:
            self._last_hw_log_time = now
            self.after(0, self._log, f"✦ {msg}")

    def _update_live_display(self, value: float):
        """Refresh gauge and last update timestamp with zero delay."""
        self._live_value = value
        now_str = time.strftime("%H:%M:%S")
        if now_str != self._last_update_str:
            self._last_update_str = now_str
            self._lbl_last_update.configure(text=f"✦ REAL-TIME: {now_str}")

        if hasattr(self, "_lbl_angle_readout") and self._lbl_angle_readout:
            self._lbl_angle_readout.configure(text=f"{value:.2f}°" if value is not None else "--.--°")

        # Compute active setpoint, tolerance, and evaluation
        target_sp = None
        target_tol = 1.50
        dev = None

        # Check manual overrides first
        try:
            sp_raw = self._var_target_setpoint.get().strip()
            if sp_raw:
                target_sp = float(sp_raw)
        except Exception:
            pass

        try:
            tol_raw = self._var_target_tolerance.get().strip()
            if tol_raw:
                target_tol = float(tol_raw)
        except Exception:
            pass

        # If not manually overridden, use cached setpoint and tolerance for current_row
        if target_sp is None:
            if self._cached_row != self._current_row or self._cached_target_sp is None:
                step_info = self._ctrl.get_step_info_for_row(self._current_row)
                self._cached_row = self._current_row
                self._cached_target_sp = step_info.get("setpoint")
                t_tol = step_info.get("tolerance")
                self._cached_target_tol = float(t_tol) if t_tol is not None else 1.50
            target_sp = self._cached_target_sp
            target_tol = self._cached_target_tol

        curr_dir = self._var_direction.get() if hasattr(self, "_var_direction") else "CW"
        eval_res = self._ctrl.evaluate_measurement(
            value,
            target_sp,
            target_tol,
            direction=curr_dir,
            step_number=self._current_row,
        )

        if eval_res.deviation is not None:
            dev = eval_res.deviation

        # Update Prominent PASS / FAIL badge
        if hasattr(self, "_lbl_verdict_badge") and self._lbl_verdict_badge and hasattr(self, "_badge_verdict_frame") and self._badge_verdict_frame:
            if value is None or eval_res.status == EvaluationStatus.NOT_EVALUATED:
                self._badge_verdict_frame.configure(fg_color="#F1F5F9", border_color="#CBD5E1")
                self._lbl_verdict_badge.configure(text="— N/A", text_color="#64748B")
            elif eval_res.status == EvaluationStatus.PASS:
                self._badge_verdict_frame.configure(fg_color="#DCFCE7", border_color="#22C55E")
                self._lbl_verdict_badge.configure(text="✓ PASS", text_color="#15803D")
            elif eval_res.status == EvaluationStatus.FAIL:
                self._badge_verdict_frame.configure(fg_color="#FEE2E2", border_color="#EF4444")
                self._lbl_verdict_badge.configure(text="✗ FAIL", text_color="#B91C1C")

        # Update Setpoint info text
        if hasattr(self, "_lbl_setpoint_info") and self._lbl_setpoint_info:
            sp_txt = f"{target_sp:.2f}°" if target_sp is not None else "—"
            dev_txt = f"{dev:+.2f}°" if dev is not None else "—"
            tol_txt = f"±{target_tol:.2f}°" if target_tol is not None else "±1.50°"
            self._lbl_setpoint_info.configure(text=f"TARGET: {sp_txt}  |  DEV: {dev_txt}  |  TOL: {tol_txt}")

        self._gauge_canvas.set_value(
            value,
            is_simulator=self._var_sim.get(),
            setpoint=target_sp,
            deviation=dev,
        )
        self._update_measurement_summary(measured=value)
        self._update_header_status()

    def _on_direction_selected(self, val: str):
        """Operator toggle or sheet auto-suggestion for CW vs CCW test direction."""
        mode = "CCW" if "CCW" in val else "CW"
        self._var_direction.set(mode)
        self._ctrl.set_direction(mode)
        self._log(f"➔ [DIRECTION] Arah pengujian aktif: {mode}")
        if hasattr(self, "_lbl_direction_pill") and self._lbl_direction_pill:
            self._lbl_direction_pill.configure(text=f"{'↻' if mode == 'CW' else '↺'} {mode} ACTIVE")
        if hasattr(self, "_seg_direction") and self._seg_direction:
            self._seg_direction.set("↻ CW" if mode == "CW" else "↺ CCW")
        self._update_live_display(self._live_value if self._live_value is not None else 0.0)

    # ── AUTO-SAVE & STABILITY HANDLERS ───────────────────────────────
    def _on_space_pressed(self, event=None):
        """Keyboard Spacebar trigger to toggle pause/start auto-save."""
        if event and hasattr(event, "widget") and event.widget:
            w_class = event.widget.winfo_class()
            if w_class in ("Entry", "Text", "TEntry"):
                return
        self._toggle_auto_save_pause()

    def _toggle_auto_save_pause(self):
        """Toggle pause state of the auto-save engine."""
        if not self._ctrl.is_workbook_loaded:
            messagebox.showwarning(
                "No Excel Loaded",
                "Silakan load file Excel master terlebih dahulu sebelum mengaktifkan Auto-Save."
            )
            return

        is_paused = self._ctrl.toggle_auto_save_pause()
        play_sound_beep(1000, 100)

        if is_paused:
            if hasattr(self, "_btn_pause") and self._btn_pause:
                self._btn_pause.configure(
                    text="▶ START (SPACE)",
                    fg_color=CLR_SUCCESS_NEON,
                    hover_color="#2BD474",
                    text_color=CLR_BG_MAIN,
                    border_width=0,
                )
            if hasattr(self, "_lbl_action_banner") and self._lbl_action_banner:
                self._lbl_action_banner.configure(
                    text="⏸ AUTO-SAVE DI-PAUSE — TEKAN SPACE ATAU TOMBOL START UNTUK MELANJUTKAN",
                    text_color=CLR_WARNING_AMBER,
                )
            if hasattr(self, "_lbl_stability_badge") and self._lbl_stability_badge:
                self._lbl_stability_badge.configure(text="PAUSED", text_color=CLR_WARNING_AMBER)
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#FEF3C7", border_color="#FDE68A")
            self._lbl_footer_status.configure(text="Auto-Save Paused (Press Space to Resume)")
            self._log("⏸ [AUTO-SAVE] Di-PAUSE oleh operator. (Tekan SPACE untuk mulai kembali)")
        else:
            if hasattr(self, "_btn_pause") and self._btn_pause:
                self._btn_pause.configure(
                    text="PAUSE (SPACE)",
                    fg_color="#FFFFFF",
                    hover_color="#F1F5F9",
                    text_color=CLR_TEXT_PRIMARY,
                    border_color=CLR_BORDER,
                    border_width=1,
                )
            next_row = self._ctrl.current_row
            last_r = getattr(self, "_last_saved_row", None)
            last_txt = f" (TERAKHIR: ROW {last_r})" if last_r else ""
            if hasattr(self, "_lbl_action_banner") and self._lbl_action_banner:
                self._lbl_action_banner.configure(
                    text=f"✦ SISTEM AUTO-SAVE AKTIF ➔ NEXT TARGET: ROW {next_row}{last_txt}",
                    text_color=CLR_ACCENT_CYAN,
                )
            if hasattr(self, "_lbl_stability_badge") and self._lbl_stability_badge:
                self._lbl_stability_badge.configure(text="SENSOR STABLE", text_color=CLR_SUCCESS_NEON)
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#DCFCE7", border_color="#86EFAC")
            self._lbl_footer_status.configure(text=f"Auto-Save Active → Row {next_row}")
            self._log("▶ [AUTO-SAVE] AKTIF kembali. Sensor stabil akan otomatis dicatat.")

    def _on_save_mode_changed(self, choice: str):
        """Handle save mode toggle between AUTO-SAVE ON and MANUAL MODE."""
        is_auto = ("AUTO" in choice.upper() and "OFF" not in choice.upper())
        self._var_save_mode.set(choice)
        self._var_auto_save.set(is_auto)

        if is_auto:
            self._ctrl.set_save_mode(SaveMode.AUTO)
            if hasattr(self, "_lbl_mode_badge") and self._lbl_mode_badge:
                self._lbl_mode_badge.configure(
                    text="● AUTO-SAVE ENABLED",
                    text_color=CLR_SUCCESS_NEON,
                )
            if hasattr(self, "_btn_manual_save") and self._btn_manual_save:
                self._btn_manual_save.configure(
                    text="💾 SAVE DATA (ENTER)",
                    fg_color=CLR_PANEL_SEC,
                    border_color=CLR_BORDER,
                    text_color=CLR_TEXT_PRIMARY,
                )
            if hasattr(self, "_lbl_action_banner") and self._lbl_action_banner:
                self._lbl_action_banner.configure(
                    text="✦ SISTEM AUTO-SAVE AKTIF — DATA TERCATAT OTOMATIS SAAT SENSOR STABIL (BEEP CONFIRMED)",
                    text_color=CLR_ACCENT_CYAN,
                )
            self._log("⚙ [MODE] Mode penyimpanan beralih ke: [AUTO-SAVE ON]")
            self._lbl_footer_status.configure(text="Auto-Save Mode Active")
        else:
            self._ctrl.set_save_mode(SaveMode.MANUAL)
            if hasattr(self, "_lbl_mode_badge") and self._lbl_mode_badge:
                self._lbl_mode_badge.configure(
                    text="○ MANUAL SAVE MODE",
                    text_color=CLR_TEXT_SECONDARY,
                )
            if hasattr(self, "_btn_manual_save") and self._btn_manual_save:
                self._btn_manual_save.configure(
                    text="💾 SAVE DATA (TEKAN ENTER)",
                    fg_color=CLR_ACCENT_BLUE,
                    border_color=CLR_BORDER_CYAN,
                    text_color="#FFFFFF",
                )
            if hasattr(self, "_lbl_action_banner") and self._lbl_action_banner:
                self._lbl_action_banner.configure(
                    text="⌨ MODE MANUAL SAVE — TEKAN ENTER ATAU KLIK 'SAVE DATA' UNTUK MENCATAT",
                    text_color=CLR_WARNING_AMBER,
                )
            self._log("⚙ [MODE] Mode penyimpanan beralih ke: [MANUAL SAVE MODE]")
            self._lbl_footer_status.configure(text="Manual Save Mode Active (Press Enter to Save)")

        if hasattr(self, "_seg_mode") and self._seg_mode.get() != choice:
            self._seg_mode.set(choice)
        if hasattr(self, "_switch_auto_save"):
            self._switch_auto_save.select() if is_auto else self._switch_auto_save.deselect()

    def _on_auto_save_toggled(self):
        """Synchronize with segmented button when switch toggle is used."""
        if self._var_auto_save.get():
            self._on_save_mode_changed("AUTO-SAVE ON")
        else:
            self._on_save_mode_changed("AUTO-SAVE OFF / MANUAL MODE")

    def _on_auto_save_result(self, result: SaveResult):
        """Dispatched from MeasurementController when auto-save succeeds or fails."""
        self.after(0, self._apply_auto_save_result, result)

    def _apply_auto_save_result(self, result: SaveResult):
        if result.status == SaveStatus.SAVED:
            target_row = result.excel_row
            val = result.angle if result.angle is not None else (result.record.angle if result.record else (self._live_value or 0.0))
            ts = result.timestamp_str
            self._last_saved_row = target_row

            # Update history table data
            existing = next((h for h in self._history_data if h["excel_row"] == target_row), None)
            if existing:
                existing["value"] = val
                existing["timestamp"] = ts
            else:
                self._history_data.append({"excel_row": target_row, "value": val, "timestamp": ts})
                self._history_data.sort(key=lambda x: x["excel_row"])

            self._record_stack.append({"excel_row": target_row, "value": val, "timestamp": ts})
            self._save_count = len(self._history_data)
            self._badge_saved.configure(text=f"[ {self._save_count} SAVED ]")

            next_row = self._ctrl.current_row
            is_live_excel = LiveExcelAdapter.is_workbook_open(self._excel_path) if self._excel_path else False
            if is_live_excel:
                self._log(f"✓ [AUTO-SAVE LIVE] {val:.2f}° tersimpan langsung ke Microsoft Excel yang terbuka (Row {target_row}, Kolom {RESULT_COLUMN})")
                self._log(f"➔ [TARGET] Pointer otomatis lanjut ke Baris {next_row} (siap posisi berikutnya)")
            else:
                self._log(f"✓ [AUTO-SAVE] {result.message} ➔ Next: Baris {next_row}")

            self._lbl_footer_status.configure(text=f"Auto-Saved: Row {target_row} ({val:.2f}°) ➔ Next: Row {next_row}")
            if hasattr(self, "_lbl_action_banner") and self._lbl_action_banner:
                self._lbl_action_banner.configure(
                    text=f"✦ AUTO-SAVE AKTIF ➔ NEXT TARGET: ROW {next_row} (TERAKHIR: ROW {target_row} = {val:.2f}°)"
                )

            # Crisp audio beep confirmation through speakers
            play_sound_beep(1600, 220, is_success=True)

            # Advance row in GUI entry
            self._set_active_row(next_row)
            self._refresh_history_table()
            self._update_tree_live_single_row(target_row, val, is_edited=False)
            self._update_button_states()
        elif result.status == SaveStatus.FAILED:
            play_sound_beep(450, 350, is_success=False)
            self._log(f"✗ [AUTO-SAVE GAGAL] {result.error}")
            self._lbl_footer_status.configure(text=f"Auto-Save Gagal: {result.error}")
            if hasattr(self, "_lbl_stability_badge") and self._lbl_stability_badge:
                self._lbl_stability_badge.configure(text="SAVE FAILED", text_color=CLR_DANGER_RED)
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#FEE2E2", border_color="#FCA5A5")

    def _on_stability_update(self, result: StabilityResult | MotionTriggerResult):
        """Dispatched from MeasurementController on stability/motion changes."""
        self.after(0, self._apply_stability_update, result)

    def _apply_stability_update(self, result: StabilityResult | MotionTriggerResult):
        if not hasattr(self, "_lbl_stability_badge"):
            return
        if getattr(self._ctrl, "is_auto_save_paused", False):
            self._lbl_stability_badge.configure(text="PAUSED", text_color=CLR_WARNING_AMBER)
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#FEF3C7", border_color="#FDE68A")
            return

        is_auto_mode = (getattr(self._ctrl, "save_mode", SaveMode.AUTO) == SaveMode.AUTO)
        prev = getattr(self, "_last_stability_status", None)

        # Check for MotionState first, then fallback to StabilityStatus
        m_state = getattr(result, "motion_state", None)
        curr = m_state if m_state is not None else result.status
        self._last_stability_status = curr

        if curr in (MotionState.WAITING_FOR_MOTION, StabilityStatus.WAITING_FOR_DATA):
            self._lbl_stability_badge.configure(
                text="WAITING FOR MOTION",
                text_color=CLR_TEXT_MUTED,
            )
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#F1F5F9", border_color="#CBD5E1")
            if prev != curr:
                self._log("● [AUTO-SAVE] Menunggu pergerakan shaft NAI API 8810A (Auto-Save terkunci)...")
                self._lbl_footer_status.configure(text="Waiting for motion (estimated from API 8810A stream)")

        elif curr == MotionState.MOVING or curr == StabilityStatus.MOVING:
            self._lbl_stability_badge.configure(
                text="MOVING — AUTO-SAVE LOCKED",
                text_color=CLR_ACCENT_BLUE,
            )
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#E0F2FE", border_color="#BAE6FD")
            if prev != curr:
                tag = "[AUTO-SAVE]" if is_auto_mode else "[MANUAL MODE]"
                self._log(f"⇄ {tag} Pergerakan shaft terdeteksi dari sudut API 8810A (Auto-Save terkunci).")
                self._lbl_footer_status.configure(text="Moving — Auto-Save locked (estimated from API 8810A stream)")

        elif curr in (MotionState.STOP_CANDIDATE, StabilityStatus.SETTLING):
            self._lbl_stability_badge.configure(
                text="STOP CANDIDATE",
                text_color=CLR_WARNING_AMBER,
            )
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#FEF3C7", border_color="#FDE68A")
            if prev != curr:
                live_str = f"~{self._live_value:.2f}°" if self._live_value is not None else ""
                tag = "[AUTO-SAVE]" if is_auto_mode else "[MOTION]"
                self._log(f"◌ {tag} Pergerakan terhenti pada {live_str}. Konfirmasi stop berlangsung...")
                self._lbl_footer_status.configure(text="Stop candidate — confirming mechanical stop (estimated from API 8810A)")

        elif curr == MotionState.CAPTURE_PENDING:
            self._lbl_stability_badge.configure(
                text="CAPTURING DATA",
                text_color=CLR_SUCCESS_NEON,
            )
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#DCFCE7", border_color="#86EFAC")
            if prev != curr:
                ang_val = getattr(result, "captured_angle", self._live_value)
                ang_str = f"{ang_val:.2f}°" if ang_val is not None else ""
                self._log(f"● [CAPTURE] Inferred stop terkonfirmasi pada {ang_str}. Menyimpan ke Excel...")
                self._lbl_footer_status.configure(text=f"Capturing data at {ang_str} (inferred stop from API 8810A)")

        elif curr == MotionState.WAITING_FOR_NEXT_MOTION:
            row_num = self._last_saved_row or (self._current_row - 1 if self._current_row > 11 else 11)
            self._lbl_stability_badge.configure(
                text=f"SAVED — ROW {row_num}",
                text_color=CLR_SUCCESS_NEON,
            )
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#DCFCE7", border_color="#86EFAC")
            if prev != curr:
                self._log(f"✓ [AUTO-SAVE] Row {row_num} tersimpan. Menunggu pergerakan berikutnya...")
                self._lbl_footer_status.configure(text=f"Saved Row {row_num}. Waiting for next motion cycle.")

        elif curr in (MotionState.DATA_INVALID, StabilityStatus.UNSTABLE):
            self._lbl_stability_badge.configure(
                text="SENSOR DISCONNECTED / DATA INVALID",
                text_color=CLR_DANGER_RED,
            )
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#FEE2E2", border_color="#FCA5A5")
            if prev != curr:
                diag = getattr(result, "reset_reason", None) or "Pembacaan tidak valid atau timeout"
                self._log(f"⚠ [SENSOR] {diag}")
                self._lbl_footer_status.configure(text=f"Sensor invalid / disconnected: {diag}")

        elif curr == StabilityStatus.STABLE:
            badge_text = "SENSOR STABLE"
            self._lbl_stability_badge.configure(
                text=badge_text,
                text_color=CLR_SUCCESS_NEON,
            )
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#DCFCE7", border_color="#86EFAC")
            if prev != StabilityStatus.STABLE:
                angle_str = f"{result.stable_angle:.2f}°" if result.stable_angle is not None else ""
                if is_auto_mode:
                    self._log(f"● [AUTO-SAVE] Sensor STABIL terkonfirmasi pada {angle_str}. Menyimpan otomatis...")
                else:
                    self._log(f"● [MANUAL MODE] Sensor STABIL pada {angle_str}. Tekan ENTER atau klik SAVE DATA untuk mencatat.")
        else:
            self._lbl_stability_badge.configure(
                text="READY",
                text_color=CLR_TEXT_MUTED,
            )
            if hasattr(self, "_badge_sensor_frame") and self._badge_sensor_frame:
                self._badge_sensor_frame.configure(fg_color="#F1F5F9", border_color="#E2E8F0")


    def _on_test_query(self):
        """Safely test sending active command over socket or simulator."""
        cmd = self._var_cmd.get().strip()
        sim = self._var_sim.get()
        ip = self._var_ip.get().strip()
        try:
            port = int(self._var_port.get().strip())
        except ValueError:
            port = 23

        self._log(f"✦ Testing query: '{cmd}' ...")

        def worker():
            import socket
            raw = ""
            if sim:
                time.sleep(0.2)
                raw = f"{SerialReader._simulate(25.40):.3f}"
            else:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(2.5)
                    s.connect((ip, port))
                    # Handshake
                    s.sendall(b"NAII\r\n")
                    time.sleep(0.1)
                    s.recv(256)
                    # Query
                    s.sendall(f"{cmd}\r\n".encode("ascii"))
                    time.sleep(0.15)
                    raw = s.recv(512).decode("ascii", errors="ignore").strip()
                    s.close()
                except Exception as exc:
                    raw = f"[Socket Error: {exc}]"

            val = SerialReader._parse_angle(raw)
            if val is not None:
                msg = f"📥 Test Reply: '{raw}' ➔ Angle: {val:.2f}°"
            else:
                msg = f"📥 Test Reply: '{raw}'"
            self.after(0, self._log, msg)

        threading.Thread(target=worker, daemon=True).start()

    # ═════════════════════════════════════════════════════════════════
    #  EXCEL WORKBOOK SYNCHRONIZATION
    # ═════════════════════════════════════════════════════════════════
    def _browse_excel(self):
        path = filedialog.askopenfilename(
            title="Pilih File Master Excel",
            filetypes=[("Excel Files", "*.xlsx;*.xlsm"), ("All Files", "*.*")],
        )
        if not path:
            return

        try:
            wb = openpyxl.load_workbook(path)
        except Exception as exc:
            messagebox.showerror("Error Membaca Excel", f"Gagal membuka file:\n{exc}")
            self._log(f"✗ Gagal membuka {os.path.basename(path)}: {exc}")
            return

        # Auto-heal: Ensure all workbook views are visible (fixes grey screen)
        try:
            if hasattr(wb, "views"):
                for v in wb.views:
                    if getattr(v, "visibility", None) == "hidden":
                        v.visibility = "visible"
        except Exception:
            pass

        self._workbook = wb
        self._excel_path = path
        self._ctrl.inject_workbook(wb, path)
        filename = os.path.basename(path)
        self._var_excel_path.set(filename if len(filename) < 28 else filename[:25] + "...")

        sheets = wb.sheetnames
        self._opt_sheet.configure(values=sheets)
        active_sheet = wb.active.title if wb.active else sheets[0]
        self._opt_sheet.set(active_sheet)
        self._active_sheet_name = active_sheet
        self._ctrl.select_sheet(active_sheet)

        self._record_stack.clear()
        self._edited_rows.clear()
        self._load_history_from_excel()

        # Engage auto-save if enabled
        if self._var_auto_save.get():
            self._ctrl.enable_auto_save()
            self._log("✦ Auto-Save AKTIF: Sensor stabil akan otomatis dicatat ke Excel.")

        if hasattr(self, "_lbl_wb_status") and self._lbl_wb_status:
            self._lbl_wb_status.configure(text="CONNECTED", text_color=CLR_SUCCESS_NEON)
        self._update_header_status()
        self._update_measurement_summary()
        self._log(f"✓ Loaded workbook: {filename} (Sheet: {active_sheet})")

    def _on_sheet_changed(self, sheet_name: str):
        if not self._workbook or sheet_name not in self._workbook.sheetnames:
            return
        self._active_sheet_name = sheet_name
        self._ctrl.select_sheet(sheet_name)
        self._record_stack.clear()
        self._edited_rows.clear()
        self._load_history_from_excel()
        if self._var_auto_save.get():
            self._ctrl.enable_auto_save()

        # Suggest direction based on sheet and current row convention
        if "CAM OUTPUT" in str(sheet_name).upper():
            suggested_dir = "CCW" if self._current_row > 192 else "CW"
        elif "ACCURACY" in str(sheet_name).upper():
            suggested_dir = "CCW" if self._current_row >= 24 else "CW"
        else:
            suggested_dir = "CW"
        self._on_direction_selected(suggested_dir)

        self._update_header_status()
        self._update_measurement_summary()
        self._log(f"✓ Switched to sheet: {sheet_name}")

    def _scan_next_row(self) -> int:
        """Scan column G from row 11 to identify the first empty cell."""
        if not self._workbook or not self._active_sheet_name:
            return DEFAULT_START_ROW
        ws = self._workbook[self._active_sheet_name]
        row = DEFAULT_START_ROW
        while ws[f"{RESULT_COLUMN}{row}"].value is not None and str(ws[f"{RESULT_COLUMN}{row}"].value).strip() != "":
            row += 1
        return row

    def _load_history_from_excel(self):
        """Read all existing measurements from column G into the history table."""
        if not self._workbook or not self._active_sheet_name:
            return

        ws = self._workbook[self._active_sheet_name]
        self._history_data.clear()

        # Scan all rows in worksheet starting from DEFAULT_START_ROW
        max_scan = max(ws.max_row, DEFAULT_START_ROW + 30)
        last_filled_row = None
        now_str = time.strftime("%H:%M:%S")

        for r in range(DEFAULT_START_ROW, max_scan + 1):
            cell_val = ws[f"{RESULT_COLUMN}{r}"].value
            if cell_val is not None and str(cell_val).strip() != "":
                try:
                    parsed_val = round(float(cell_val), 2)
                except (ValueError, TypeError):
                    parsed_val = cell_val

                self._history_data.append({
                    "excel_row": r,
                    "value": parsed_val,
                    "timestamp": now_str,
                })
                last_filled_row = r

        # Auto-advance target row:
        # If row 11 was filled, jump to row 12 (or last_filled_row + 1).
        # If no data is filled yet, default to base row 11.
        next_target = (last_filled_row + 1) if last_filled_row is not None else DEFAULT_START_ROW
        self._set_active_row(next_target, f"Auto-detected next empty row ({next_target})")
        self._save_count = len(self._history_data)

        self._badge_saved.configure(text=f"[ {self._save_count} SAVED ]")
        self._refresh_history_table()
        self._refresh_excel_live_view()
        self._update_button_states()
        self._log(f"✦ History loaded: {len(self._history_data)} records. Target row siap di baris {self._current_row}.")

    def _refresh_history_table(self):
        """Repopulate the measurement history table."""
        for item in self._tree.get_children():
            self._tree.delete(item)

        last_saved = getattr(self, "_last_saved_row", None)
        for i, rec in enumerate(self._history_data):
            if rec["excel_row"] == last_saved:
                tag = "latest_saved"
            elif rec["excel_row"] in self._edited_rows:
                tag = "edited"
            elif i % 2 == 0:
                tag = "evenrow"
            else:
                tag = "oddrow"

            if rec['value'] is not None:
                try:
                    val_str = f"{float(rec['value']):.2f}"
                except (ValueError, TypeError):
                    val_str = str(rec['value'])
            else:
                val_str = "—"
            values = (i + 1, rec["excel_row"], val_str, rec["timestamp"])
            self._tree.insert("", "end", iid=str(i), values=values, tags=(tag,))

        if self._history_data:
            self._tree.see(str(len(self._history_data) - 1))

        if self._history_badge:
            if hasattr(self, "_last_saved_row") and self._last_saved_row:
                self._history_badge.configure(
                    text=f"ROW {self._last_saved_row} SAVED ➔ NEXT: ROW {self._current_row}"
                )
            else:
                self._history_badge.configure(text=f"{len(self._history_data)} RECORDS")

    def _update_button_states(self):
        # Update decoupled selection states without altering next_append_row
        rec = self._get_selected_record()
        has_sel = (rec is not None)
        if rec:
            self._selected_excel_row = rec.get("excel_row")
            if hasattr(self, "_ctrl"):
                self._ctrl.select_row(rec.get("excel_row"), self._selected_excel_row)
        else:
            self._selected_history_row = None
            self._selected_excel_row = None
            if hasattr(self, "_ctrl"):
                self._ctrl.clear_selection()

        # Edit is active only if a valid record is selected
        if hasattr(self, "_btn_edit") and self._btn_edit:
            self._btn_edit.configure(state="normal" if has_sel else "disabled")

    def _get_selected_record(self) -> Optional[dict]:
        """Return the dictionary corresponding to the currently selected table row."""
        if hasattr(self, "_tree_live") and self._tree_live:
            sel = self._tree_live.selection()
            if sel:
                item = self._tree_live.item(sel[0])
                vals = item.get("values", [])
                if len(vals) >= 9:
                    try:
                        row_str = str(vals[1]).replace("Row ", "").strip()
                        row_num = int(row_str)
                        for h in self._history_data:
                            if h.get("excel_row") == row_num:
                                return h
                        val_str = str(vals[8]).replace("°", "").strip()
                        val = float(val_str) if val_str != "—" else 0.0
                        return {
                            "excel_row": row_num,
                            "value": val,
                            "timestamp": str(vals[2]),
                        }
                    except Exception:
                        pass

        selected = self._tree.selection() if hasattr(self, "_tree") and self._tree else ()
        if not selected:
            return None
        try:
            idx = int(selected[0])
            if 0 <= idx < len(self._history_data):
                return self._history_data[idx]
        except (ValueError, IndexError):
            pass
        return None

    def _on_live_tree_select(self):
        """Handle row selection inside Excel Live View."""
        self._update_button_states()
        if not hasattr(self, "_tree_live") or not self._tree_live:
            return
        sel = self._tree_live.selection()
        if sel:
            item = self._tree_live.item(sel[0])
            vals = item.get("values", [])
            if len(vals) >= 13:
                try:
                    row_num = int(str(vals[1]).replace("Row ", "").strip())
                    meas_s = str(vals[8]).replace("°", "").strip()
                    meas_val = float(meas_s) if meas_s != "—" else None
                    sp_s = str(vals[5]).replace("°", "").strip()
                    sp_val = float(sp_s) if sp_s != "—" else None
                    dev_s = str(vals[10]).replace("°", "").replace("+", "").strip()
                    dev_val = float(dev_s) if dev_s != "—" else None
                    top_s = str(vals[6]).replace("°", "").strip()
                    bot_s = str(vals[7]).replace("°", "").strip()
                    tol_val = 1.50
                    if sp_val is not None:
                        try:
                            if top_s != "—":
                                tol_val = abs(float(top_s) - sp_val)
                            elif bot_s != "—":
                                tol_val = abs(sp_val - float(bot_s))
                        except Exception:
                            tol_val = 1.50

                    rec_dict = {
                        "excel_row": row_num,
                        "test_point": str(vals[2]),
                        "setpoint": sp_val,
                        "measured_angle": meas_val,
                        "deviation": dev_val,
                        "tolerance": tol_val,
                        "status": str(vals[9]),
                        "save_status": str(vals[12]),
                    }
                    self._update_measurement_summary(selected_record=rec_dict)
                except Exception:
                    pass
        else:
            self._update_measurement_summary()

    def _show_tree_context_menu(self, event):
        """Displays right-click context menu on the clicked table row."""
        item_id = self._tree_live.identify_row(event.y)
        if item_id:
            self._tree_live.selection_set(item_id)
            self._on_live_tree_select()
        try:
            self._tree_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._tree_context_menu.grab_release()

    def _update_tree_live_single_row(self, row: int, measured_val: Optional[float], is_edited: bool = False):
        """
        In-place O(1) update for a single row in ttk.Treeview without destroying and recreating the table.
        Significantly optimizes responsiveness and eliminates GUI stuttering during long data logging sessions.
        """
        if not hasattr(self, "_tree_live") or not self._tree_live:
            return

        iid = f"row_{row}"
        if not self._tree_live.exists(iid):
            self._refresh_excel_live_view()
            return

        curr_vals = list(self._tree_live.item(iid, "values"))
        if len(curr_vals) < 13:
            return

        step_info = self._ctrl.get_step_info_for_row(row)
        sp = step_info.get("setpoint")
        tol = step_info.get("tolerance", 1.50)
        curr_dir = self._var_direction.get() if hasattr(self, "_var_direction") else "CW"

        if measured_val is not None:
            eval_res = self._ctrl.evaluate_measurement(
                measured_val,
                sp,
                tol,
                direction=curr_dir,
                step_number=row,
            )
            meas_str = f"{measured_val:.2f}°"
            if eval_res.status == EvaluationStatus.PASS:
                res_str = "PASS ✓"
                status_tag = "pass"
            elif eval_res.status == EvaluationStatus.FAIL:
                res_str = "FAIL ✗"
                status_tag = "fail"
            else:
                res_str = "NOT EVAL"
                status_tag = "not_evaluated"

            dev_str = f"{eval_res.deviation:+.2f}°" if eval_res.deviation is not None else "—"
        else:
            meas_str = "—"
            res_str = "NOT EVAL"
            dev_str = "—"
            status_tag = "not_evaluated"

        save_status = "EDITED" if is_edited else ("SAVED" if measured_val is not None else "--")
        curr_vals[8] = meas_str
        curr_vals[9] = res_str
        curr_vals[10] = dev_str
        curr_vals[12] = save_status

        tags = ["latest_saved" if not is_edited else "edited", status_tag]
        self._tree_live.item(iid, values=tuple(curr_vals), tags=tuple(tags))

        if getattr(self, "_var_auto_scroll", None) and self._var_auto_scroll.get():
            self._tree_live.see(iid)
            self._tree_live.selection_set(iid)

    def _refresh_excel_live_view(self):
        """Re-read actual committed Excel worksheet rows and refresh the Live View table."""
        if not hasattr(self, "_tree_live") or self._tree_live is None:
            return

        for item in self._tree_live.get_children():
            self._tree_live.delete(item)

        if not self._ctrl.is_workbook_loaded:
            if hasattr(self, "_lbl_sync_time") and self._lbl_sync_time:
                self._lbl_sync_time.configure(text="NO WORKBOOK LOADED")
            return

        sp_override = None
        tol_override = None
        try:
            sp_str = self._var_target_setpoint.get().strip()
            if sp_str:
                sp_override = float(sp_str)
        except ValueError:
            pass
        try:
            tol_str = self._var_target_tolerance.get().strip()
            if tol_str:
                tol_override = float(tol_str)
        except ValueError:
            pass

        try:
            records = self._ctrl.get_live_worksheet_records(
                override_setpoint=sp_override,
                override_tolerance=tol_override,
            )
        except Exception as exc:
            self._log(f"⚠ Live View sync error: {exc}")
            if hasattr(self, "_lbl_sync_time") and self._lbl_sync_time:
                self._lbl_sync_time.configure(text="SYNC ERROR")
            return

        last_saved = getattr(self, "_last_saved_row", None)
        for i, rec in enumerate(records):
            r = rec["excel_row"]
            val = rec["measured_angle"]
            sp = rec["setpoint"]
            dev = rec["deviation"]
            tol = rec["tolerance"]
            status_enum = rec["status"]

            no_str = str(rec["no"])
            row_str = f"Row {r}"
            tp = rec.get("test_point")
            tp_str = str(tp) if tp is not None else "—"

            sp_str = f"{sp:.2f}°" if sp is not None and isinstance(sp, (int, float)) else (str(sp) if sp is not None else "—")
            meas_str = f"{val:.2f}°" if val is not None and isinstance(val, (int, float)) else (str(val) if val is not None else "—")
            dev_str = f"{dev:+.2f}°" if dev is not None and isinstance(dev, (int, float)) else (str(dev) if dev is not None else "—")
            tol_str = f"±{tol:.2f}°" if tol is not None and isinstance(tol, (int, float)) else (str(tol) if tol is not None else "—")

            if status_enum == EvaluationStatus.PASS:
                res_str = "PASS ✓"
                status_tag = "pass"
            elif status_enum == EvaluationStatus.FAIL:
                res_str = "FAIL ✗"
                status_tag = "fail"
            else:
                res_str = "NOT EVAL"
                status_tag = "not_evaluated"

            turns = rec.get("input_turns")
            turns_str = str(turns) if turns is not None else "—"

            shaft = rec.get("shaft_angle")
            shaft_str = f"{shaft:.2f}°" if isinstance(shaft, (int, float)) else (str(shaft) if shaft is not None else "—")

            top_lim = rec.get("top_limit")
            top_lim_str = f"{top_lim:.2f}" if isinstance(top_lim, (int, float)) else (str(top_lim) if top_lim is not None else "—")

            bot_lim = rec.get("bottom_limit")
            bot_lim_str = f"{bot_lim:.2f}" if isinstance(bot_lim, (int, float)) else (str(bot_lim) if bot_lim is not None else "—")

            real_v = rec.get("real_value")
            real_v_str = f"{real_v:.2f}" if isinstance(real_v, (int, float)) else (str(real_v) if real_v is not None else "—")

            save_status_str = rec.get("save_status", "--")
            if r in self._edited_rows:
                save_status_str = "EDITED"

            bg_tag = "evenrow" if i % 2 == 0 else "oddrow"
            if r == last_saved and val is not None:
                tag_list = ("latest_saved", status_tag)
            elif r in self._edited_rows:
                tag_list = ("edited",)
            elif r == self._current_row:
                tag_list = ("active_row", status_tag)
            elif status_tag in ("pass", "fail"):
                tag_list = (status_tag,)
            else:
                tag_list = (status_tag, bg_tag)

            self._tree_live.insert(
                "",
                "end",
                iid=f"row_{r}",
                values=(
                    no_str,
                    row_str,
                    tp_str,
                    turns_str,
                    shaft_str,
                    sp_str,
                    top_lim_str,
                    bot_lim_str,
                    meas_str,
                    res_str,
                    dev_str,
                    real_v_str,
                    save_status_str,
                ),
                tags=tag_list,
            )

        now_sync = time.strftime("%H:%M:%S")
        if hasattr(self, "_lbl_sync_time") and self._lbl_sync_time:
            self._lbl_sync_time.configure(text=f"LAST SYNC: {now_sync} ({len(records)} ROWS)")

        # Auto-scroll so active or latest saved row is centered in view if auto-scroll is enabled
        if getattr(self, "_var_auto_scroll", None) and self._var_auto_scroll.get():
            target_focus_row = last_saved if last_saved is not None else self._current_row
            focus_iid = f"row_{target_focus_row}"
            if self._tree_live.exists(focus_iid):
                self._tree_live.see(focus_iid)

    # ═════════════════════════════════════════════════════════════════
    #  ACTION: ADD RECORD (ENTER / SAVE DATA)
    # ═════════════════════════════════════════════════════════════════
    def _on_enter_pressed(self, event=None):
        """Keyboard Enter shortcut for Manual Save."""
        if event and hasattr(event, "widget") and event.widget:
            w_class = event.widget.winfo_class()
            if w_class in ("Entry", "Text", "TEntry", "TCombobox"):
                return

        if self._save_in_progress:
            return
        self._save_in_progress = True
        try:
            self._do_manual_save()
        finally:
            self.after(350, self._reset_save_flag)

    def _reset_save_flag(self):
        self._save_in_progress = False

    def _do_manual_save(self):
        """Execute manual save for current live measurement to Excel."""
        if self._live_value is None:
            self._log("⚠ Manual Save ditolak: Tidak ada pembacaan sensor valid.")
            self._lbl_footer_status.configure(text="Manual Save Ditolak — Tidak ada pembacaan sensor valid")
            play_sound_beep(450, 300, is_success=False)
            messagebox.showwarning(
                "No Sensor Data",
                "Tidak ada pembacaan sensor valid.\nPastikan perangkat NAI 8810A terhubung atau simulator aktif."
            )
            return

        if not self._ctrl.is_workbook_loaded or not self._active_sheet_name:
            self._log("⚠ Manual Save ditolak: Belum ada file Excel master yang dimuat.")
            messagebox.showwarning("No Excel Loaded", "Silakan load file Excel master terlebih dahulu.")
            return

        # Check behavior in AUTO-SAVE ON mode
        if self._ctrl.save_mode == SaveMode.AUTO:
            if getattr(self._ctrl, "is_position_latched", False):
                self._log("ℹ [AUTO-SAVE ON] Posisi sensor saat ini sudah tersimpan secara otomatis.")
                self._lbl_footer_status.configure(text="Posisi saat ini sudah tersimpan otomatis.")
                return
            last_st = getattr(self, "_last_stability_status", None)
            if last_st in (StabilityStatus.MOVING, StabilityStatus.SETTLING, StabilityStatus.UNSTABLE):
                self._log("ℹ [AUTO-SAVE ON] Sensor belum stabil. Tunggu posisi stabil untuk auto-save, atau alihkan ke Mode Manual.")
                self._lbl_footer_status.configure(text="Sensor belum stabil (Mode Auto-Save Aktif)")
                return

        val = self._live_value
        target_row = self._current_row
        cell_ref = f"{RESULT_COLUMN}{target_row}"

        sp_override = None
        tol_override = None
        try:
            sp_val = self._var_target_setpoint.get().strip()
            if sp_val:
                sp_override = float(sp_val)
        except ValueError:
            pass
        try:
            tol_val = self._var_target_tolerance.get().strip()
            if tol_val:
                tol_override = float(tol_val)
        except ValueError:
            pass

        curr_dir = self._var_direction.get() if hasattr(self, "_var_direction") else "CW"
        res = self._ctrl.save_manual(
            angle=val,
            target_row=target_row,
            is_simulator=self._var_sim.get(),
            direction=curr_dir,
            setpoint=sp_override,
            tolerance=tol_override,
        )

        if res.status == SaveStatus.FAILED:
            err_msg = res.error or res.message or "Unknown error"
            if "debounce" in err_msg.lower() or "rapid" in err_msg.lower():
                self._log(f"ℹ Manual Save: {err_msg}")
                return
            play_sound_beep(450, 350, is_success=False)
            if "locked" in err_msg.lower() or "permission" in err_msg.lower():
                messagebox.showerror(
                    "File Locked",
                    "File Excel sedang dibuka program lain.\nTutup file terlebih dahulu, lalu coba lagi."
                )
            else:
                messagebox.showerror("Save Error", f"Gagal menyimpan ke Excel:\n{err_msg}")
            self._log(f"✗ [SAVE ERROR] Row {target_row}: {err_msg}")
            self._lbl_footer_status.configure(text=f"Save Error (Row {target_row}): {err_msg}")
            return

        ts = res.timestamp_str
        self._last_saved_row = target_row
        self._last_saved_val = val

        # Success - update in-memory history data
        existing_rec = next((h for h in self._history_data if h["excel_row"] == target_row), None)
        if existing_rec:
            existing_rec["value"] = val
            existing_rec["timestamp"] = ts
            existing_rec["direction"] = curr_dir
        else:
            record = {"excel_row": target_row, "value": val, "timestamp": ts, "direction": curr_dir}
            self._history_data.append(record)
            self._history_data.sort(key=lambda x: x["excel_row"])

        self._record_stack.append({"excel_row": target_row, "value": val, "timestamp": ts, "direction": curr_dir})
        self._save_count = len(self._history_data)
        self._badge_saved.configure(text=f"[ {self._save_count} SAVED ]")

        next_row = self._ctrl.current_row
        eval_txt = ""
        if res.evaluation:
            eval_txt = f" | {res.evaluation.status.value}"
            if res.evaluation.deviation is not None:
                eval_txt += f" (Dev: {res.evaluation.deviation:+.2f}°)"

        self._log(f"✓ [MANUAL SAVE] {val:.2f}° tersimpan ke Row {target_row} ({cell_ref}){eval_txt} ➔ Next: Row {next_row}")
        self._lbl_footer_status.configure(text=f"Manual Saved: Row {target_row} ({val:.2f}°){eval_txt} ➔ Next: Row {next_row}")
        if hasattr(self, "_lbl_action_banner") and self._lbl_action_banner:
            self._lbl_action_banner.configure(
                text=f"⌨ MANUAL SAVE BERHASIL ➔ ROW {target_row} ({val:.2f}°){eval_txt} | NEXT: ROW {next_row}"
            )

        play_sound_beep(1600, 220, is_success=True)

        self._set_active_row(next_row)
        self._refresh_history_table()
        self._update_tree_live_single_row(target_row, val, is_edited=False)
        self._update_button_states()

    def _do_save_record(self):
        """Legacy / shortcut alias pointing to _do_manual_save."""
        self._do_manual_save()

    # ═════════════════════════════════════════════════════════════════
    #  ACTION: UNDO LAST RECORD
    # ═════════════════════════════════════════════════════════════════
    def _undo_last_record(self):
        if not self._record_stack:
            messagebox.showinfo("Undo Info", "Tidak ada record di sesi ini yang dapat dibatalkan.")
            return

        if not self._ctrl.is_workbook_loaded or not self._active_sheet_name:
            return

        last_record = self._record_stack[-1]
        target_row = last_record["excel_row"]
        cell_val = last_record.get("value", "—")

        confirm = messagebox.askyesno(
            "Konfirmasi Undo",
            f"Batalkan pencatatan terakhir pada Excel baris {target_row}?\n"
            f"Nilai: {cell_val}°\n\nData akan dihapus dari workbook.",
        )
        if not confirm:
            return

        undone = self._ctrl.undo_last()
        if not undone:
            messagebox.showerror("Error", "Gagal membatalkan record pada Excel.")
            self._log(f"✗ Undo gagal (Row {target_row})")
            return

        self._record_stack.pop()
        self._edited_rows.discard(target_row)
        self._history_data = [h for h in self._history_data if h["excel_row"] != target_row]

        # Reset active row to the undone row
        self._set_active_row(target_row, f"Undo baris {target_row}")
        self._save_count = len(self._history_data)
        self._badge_saved.configure(text=f"[ {self._save_count} SAVED ]")

        self._refresh_history_table()
        self._refresh_excel_live_view()
        self._update_button_states()

        self._log(f"↩ Undo: record baris {target_row} berhasil dibatalkan.")
        self._lbl_footer_status.configure(text=f"Undo — row {target_row} cleared")
        threading.Thread(target=lambda: winsound.Beep(800, 150), daemon=True).start()

    # ═════════════════════════════════════════════════════════════════
    #  ACTION: EDIT SELECTED ROW
    # ═════════════════════════════════════════════════════════════════
    def _edit_selected_row(self):
        """Action to safely edit a single measurement row with validation, audit trail, and UI sync."""
        record = self._get_selected_record()
        if not record:
            messagebox.showinfo("Edit Row", "Pilih baris pada tabel terlebih dahulu yang ingin diedit.")
            return

        if not self._ctrl.is_workbook_loaded or not self._active_sheet_name:
            messagebox.showwarning("No Excel Loaded", "Silakan load file Excel master terlebih dahulu.")
            return

        target_row = record["excel_row"]
        curr_val = record.get("value")
        step_info = self._ctrl.get_step_info_for_row(target_row)

        if self._var_auto_save.get():
            self._ctrl.reset_auto_save()

        dialog = EditRecordDialog(self, target_row, curr_val, row_info=step_info)
        self.wait_window(dialog)

        if dialog.result is None:
            self._log(f"✎ Edit row {target_row}: dibatalkan.")
            return

        new_val = dialog.result
        updated_rec = self._ctrl.edit_record(target_row, new_val)
        if not updated_rec:
            messagebox.showerror("Error", f"Gagal menyimpan koreksi ke Excel.")
            self._log(f"✗ Edit row {target_row} gagal disimpan.")
            return

        # Update in-memory record
        self._edited_rows.add(target_row)
        for h in self._history_data:
            if h["excel_row"] == target_row:
                h["value"] = new_val
                break

        for r in self._record_stack:
            if r["excel_row"] == target_row:
                r["value"] = new_val
                break

        # Fast in-place UI update
        self._update_tree_live_single_row(target_row, new_val, is_edited=True)
        self._refresh_history_table()
        self._update_button_states()

        play_sound_beep(1600, 220, is_success=True)
        self._log(f"✎ [EDIT ROW] Baris {target_row} berhasil dikoreksi menjadi {new_val:.2f}° (Audit logged).")
        self._lbl_footer_status.configure(text=f"Edited row {target_row} → {new_val:.2f}°")

    # ═════════════════════════════════════════════════════════════════
    #  ACTION: CLEAR OR DELETE ROW
    # ═════════════════════════════════════════════════════════════════
    def _clear_selected_row(self):
        """Action to clear measurement data or physically delete the selected row."""
        record = self._get_selected_record()
        if not record:
            messagebox.showinfo("Clear / Delete Row", "Pilih baris pada tabel terlebih dahulu.")
            return

        if not self._ctrl.is_workbook_loaded or not self._active_sheet_name:
            messagebox.showwarning("No Excel Loaded", "Silakan load file Excel master terlebih dahulu.")
            return

        target_row = record["excel_row"]
        curr_val = record.get("value")
        step_info = self._ctrl.get_step_info_for_row(target_row)

        if self._var_auto_save.get():
            self._ctrl.reset_auto_save()

        dialog = ClearOrDeleteRowDialog(self, target_row, curr_val, row_info=step_info)
        self.wait_window(dialog)

        if not dialog.choice:
            self._log(f"⌫ Pengelolaan baris {target_row} dibatalkan.")
            return

        if dialog.choice == "CLEAR_DATA":
            success = self._ctrl.clear_single_row(target_row)
            if not success:
                messagebox.showerror("Error", f"Gagal mengosongkan data baris {target_row}.")
                return

            self._edited_rows.discard(target_row)
            self._history_data = [h for h in self._history_data if h["excel_row"] != target_row]
            self._record_stack = [r for r in self._record_stack if r["excel_row"] != target_row]
            self._save_count = len(self._history_data)
            self._badge_saved.configure(text=f"[ {self._save_count} SAVED ]")

            self._update_tree_live_single_row(target_row, None, is_edited=False)
            self._refresh_history_table()
            self._update_button_states()

            play_sound_beep(900, 150, is_success=True)
            self._log(f"⌫ [CLEAR ROW] Data hasil ukur baris {target_row} telah dikosongkan (nomor baris & step tetap utuh).")
            self._lbl_footer_status.configure(text=f"Cleared measurement on row {target_row}")

        elif dialog.choice == "DELETE_ROW":
            confirm = messagebox.askyesno(
                "Konfirmasi Hapus Baris Fisik",
                f"PERINGATAN: Menghapus baris fisik {target_row} akan menghapus seluruh data pada baris tersebut di worksheet Excel.\n"
                f"Baris di bawahnya akan bergeser naik.\n\nApakah Anda yakin ingin melanjutkan?",
                icon="warning"
            )
            if not confirm:
                return

            success = self._ctrl.delete_physical_row(target_row)
            if not success:
                messagebox.showerror("Error", f"Gagal menghapus baris fisik {target_row}.")
                return

            self._edited_rows.discard(target_row)
            self._load_history_from_excel()
            play_sound_beep(800, 200, is_success=True)
            self._log(f"🗑 [DELETE ROW] Baris fisik {target_row} telah dihapus dari worksheet.")
            self._lbl_footer_status.configure(text=f"Deleted physical row {target_row}")

    # ═════════════════════════════════════════════════════════════════
    #  ACTION: CLEAR ROW RANGE
    # ═════════════════════════════════════════════════════════════════
    def _clear_row_range_action(self):
        """Action to clear Column G measurement data across an explicit row range."""
        if not self._ctrl.is_workbook_loaded or not self._active_sheet_name:
            messagebox.showwarning("No Excel Loaded", "Silakan load file Excel master terlebih dahulu.")
            return

        def_start = self._selected_excel_row if (self._selected_excel_row and self._selected_excel_row >= DEFAULT_START_ROW) else 81
        def_end = max(114, def_start)

        if self._var_auto_save.get():
            self._ctrl.reset_auto_save()

        dialog = ClearRowRangeDialog(self, default_start=def_start, default_end=def_end, max_sheet_row=374)
        self.wait_window(dialog)

        if dialog.result is None:
            self._log("✂ Kosongkan rentang baris dibatalkan.")
            return

        s, e = dialog.result
        confirm = messagebox.askyesno(
            "Konfirmasi Hapus Rentang",
            f"Kosongkan hasil pengukuran pada Kolom G dari Baris {s} sampai Baris {e}?\n\n"
            f"• Total: {e - s + 1} baris akan dikosongkan.\n"
            f"• Baris 11 s/d {s-1} DIJAMIN TETAP UTUH dan tidak akan berubah.\n"
            f"• Nomor baris & step pengujian tetap konsisten.",
            icon="warning"
        )
        if not confirm:
            return

        cleared_count = self._ctrl.clear_row_range(s, e)
        # Update in-memory history
        self._history_data = [h for h in self._history_data if not (s <= h["excel_row"] <= e)]
        self._record_stack = [r for r in self._record_stack if not (s <= r["excel_row"] <= e)]
        for r_num in range(s, e + 1):
            self._edited_rows.discard(r_num)

        self._save_count = len(self._history_data)
        self._badge_saved.configure(text=f"[ {self._save_count} SAVED ]")

        self._refresh_history_table()
        self._refresh_excel_live_view()
        self._update_button_states()

        play_sound_beep(1000, 200, is_success=True)
        self._log(f"✂ [CLEAR RANGE] Berhasil mengosongkan {cleared_count} baris data (Baris {s} s/d {e}). Baris 11 s/d {s-1} tetap utuh.")
        self._lbl_footer_status.configure(text=f"Cleared Range Rows {s} - {e} ({cleared_count} rows)")

    # ═════════════════════════════════════════════════════════════════
    #  ACTION: RESTART MEASUREMENT FROM SELECTED ROW
    # ═════════════════════════════════════════════════════════════════
    def _restart_measurement_action(self, start_row: Optional[int] = None):
        """
        Primary feature: Restart/Resume measurement from any selected row with 3 explicit modes:
          - Mode 1: Restart Single Row (retake target row only)
          - Mode 2: Restart From Row to End (clear from target row to end, preserve rows 11 to target_row - 1)
          - Mode 3: Resume From Selected Row (point pointer to row without clearing)
        """
        if not self._ctrl.is_workbook_loaded or not self._active_sheet_name:
            messagebox.showwarning("No Excel Loaded", "Silakan load file Excel master terlebih dahulu.")
            return

        if start_row is None:
            start_row = self._selected_excel_row or self._current_row or 81

        start_row = max(start_row, DEFAULT_START_ROW)
        max_pop = max(114, self._ctrl.scan_next_empty_row() - 1, start_row)

        if self._var_auto_save.get():
            self._ctrl.reset_auto_save()

        dialog = RestartMeasurementDialog(self, default_start_row=start_row, max_populated_row=max_pop)
        self.wait_window(dialog)

        if dialog.result is None:
            self._log(f"⟲ Restart dari baris {start_row} dibatalkan.")
            return

        res = dialog.result
        s = res["start_row"]
        mode = res["mode"]
        e = res.get("end_row", max_pop)

        success = self._ctrl.restart_measurement(s, mode=mode, end_row=e)
        if not success:
            messagebox.showerror("Error", f"Gagal melakukan restart pengukuran pada baris {s}.")
            return

        # Update in-memory history according to mode
        if mode == "ROW_TO_END":
            self._history_data = [h for h in self._history_data if h["excel_row"] < s]
            self._record_stack = [r for r in self._record_stack if r["excel_row"] < s]
            for r_num in range(s, e + 1):
                self._edited_rows.discard(r_num)
        elif mode == "SINGLE_ROW":
            self._history_data = [h for h in self._history_data if h["excel_row"] != s]
            self._record_stack = [r for r in self._record_stack if r["excel_row"] != s]
            self._edited_rows.discard(s)

        self._save_count = len(self._history_data)
        self._badge_saved.configure(text=f"[ {self._save_count} SAVED ]")

        # Set active row pointer to start_row
        self._set_active_row(s, f"Restart Mode: {mode}")

        self._refresh_history_table()
        self._refresh_excel_live_view()
        self._update_button_states()

        play_sound_beep(1600, 250, is_success=True)
        self._log(f"⟲ [RESTART MEASUREMENT] Berhasil! Mode: {mode}. Pointer siap di Baris {s}. Baris 11 s/d {s-1} tetap aman.")
        self._lbl_footer_status.configure(text=f"Restarted at Row {s} (Mode: {mode})")

    # ═════════════════════════════════════════════════════════════════
    #  ACTION: EXPORT WORKBOOK
    # ═════════════════════════════════════════════════════════════════
    def _export_saveas(self):
        if not self._ctrl.is_workbook_loaded:
            messagebox.showinfo("Export", "Load file Excel terlebih dahulu.")
            return

        dest = filedialog.asksaveasfilename(
            title="Export Workbook",
            defaultextension=".xlsx",
            filetypes=[("Excel Files", "*.xlsx")],
            initialfile="RESULT_export.xlsx",
        )
        if not dest:
            return

        try:
            self._ctrl.export_copy(dest)
            self._log(f"▤ Exported copy → {os.path.basename(dest)}")
            messagebox.showinfo("Export Berhasil", f"File berhasil disimpan ke:\n{dest}")
        except Exception as exc:
            messagebox.showerror("Export Error", str(exc))

    # ═════════════════════════════════════════════════════════════════
    #  LOGGING & UTILITIES
    # ═════════════════════════════════════════════════════════════════
    def _log(self, message: str):
        """Append a colored timestamped log entry to the cockpit event terminal."""
        ts = time.strftime("%H:%M:%S")
        self._log_text.configure(state="normal")

        # Timestamp tag
        self._log_text.insert("end", f"[{ts}]  ", "tag_ts")

        # Color mapping based on symbol/prefix
        if "✓" in message or "Saved" in message:
            tag = "tag_success"
        elif "✗" in message or "Error" in message or "Locked" in message:
            tag = "tag_error"
        elif "⚠" in message or "✎" in message or "↩" in message or "⟲" in message:
            tag = "tag_warn"
        elif "✦" in message or "Connected" in message or "📥" in message or "⇥" in message:
            tag = "tag_info"
        else:
            tag = "tag_norm"

        self._log_text.insert("end", f"{message}\n", tag)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _find_logo(self) -> Optional[str]:
        candidates = [
            Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / LOGO_FILENAME,
            Path(__file__).parent / LOGO_FILENAME,
            Path(sys.executable).parent / LOGO_FILENAME,
            Path.cwd() / LOGO_FILENAME,
        ]
        for p in candidates:
            if p.exists():
                return str(p)
        return None

    def _on_close(self):
        """Clean shutdown protocol."""
        if hasattr(self, "_ctrl"):
            self._ctrl.disable_auto_save()
        if self._reader:
            try:
                self._reader.disconnect()
            except Exception:
                pass
            self._reader = None
        self.destroy()


# ═════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = FlapSensorApp()
    app.mainloop()
