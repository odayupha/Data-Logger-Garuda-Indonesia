"""
FLAP SENSOR DATA LOGGER — API 8810A DIGITAL PROTRACTOR
Theme: Clean Light Enterprise Mode (GMF AeroAsia)
──────────────────────────────────────────────────────────────────────
Aviation-grade telemetry data logger desktop application interface.
Built with CustomTkinter & Python 3.
"""

import sys
import os
import math
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

# ═════════════════════════════════════════════════════════════════════
# 1. COLOR PALETTE & DESIGN TOKENS (Clean Light Enterprise)
# ═════════════════════════════════════════════════════════════════════
CLR_BG_MAIN        = "#F1F5F9"  # Light Slate background
CLR_CARD_BG        = "#FFFFFF"  # Crisp White card
CLR_BORDER         = "#E2E8F0"  # Subtle light slate border
CLR_BORDER_FOCUS   = "#94A3B8"  # Slate-400 focus border

# Typography Colors
CLR_TEXT_PRIMARY   = "#0F172A"  # Deep slate / Charcoal (high readability)
CLR_TEXT_SECONDARY = "#64748B"  # Muted slate
CLR_TEXT_MUTED     = "#94A3B8"  # Subtle hint slate

# Brand & Accent Colors
CLR_BLUE_PRIMARY   = "#2563EB"  # Corporate Royal Blue (Save button)
CLR_BLUE_HOVER     = "#1D4ED8"  # Darker Royal Blue hover
CLR_GREEN_PRIMARY  = "#16A34A"  # Emerald Green (Connect button & Pass badge)
CLR_GREEN_HOVER    = "#15803D"  # Darker Emerald hover
CLR_GREEN_LIGHT    = "#DCFCE7"  # Soft green pill background
CLR_TEAL_LIGHT     = "#E0F2FE"  # Soft teal pill & Target Row container
CLR_TEAL_BORDER    = "#BAE6FD"  # Border for Target Row container
CLR_TEAL_TEXT      = "#0284C7"  # Teal badge text
CLR_WARNING        = "#D97706"  # Amber warning
CLR_DANGER         = "#DC2626"  # Crimson danger

FONT_FAMILY = "Segoe UI"


# ═════════════════════════════════════════════════════════════════════
# 2. CUSTOM WIDGET: 360° CIRCULAR PROTRACTOR DIAL CANVAS
# ═════════════════════════════════════════════════════════════════════
class CircularProtractorCanvas(tk.Canvas):
    """
    Renders an engineering-grade 360-degree circular protractor dial gauge.
    Features:
    - Precise outer degree ring (0° - 360°) with tick marks every 5° & 10°
    - Degree numerical labels at 30° increments
    - Smooth, balanced indicator needle pointing to current live angle
    - Modern instrument pivot cap
    """
    def __init__(self, master, size: int = 190, **kwargs):
        super().__init__(
            master,
            width=size,
            height=size,
            bg=CLR_CARD_BG,
            highlightthickness=0,
            bd=0,
            **kwargs
        )
        self.size = size
        self.angle_deg = 45.0  # Default initial pointer at 45°
        self.draw_dial()

    def set_angle(self, deg: float):
        self.angle_deg = float(deg) % 360.0
        self.draw_dial()

    def draw_dial(self):
        self.delete("all")
        cx = self.size / 2.0
        cy = self.size / 2.0
        r_outer = (self.size / 2.0) - 10.0
        r_inner_ring = r_outer - 18.0

        # Base Dial Face Circle
        self.create_oval(
            cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer,
            fill="#FFFFFF", outline="#CBD5E1", width=1.5
        )
        self.create_oval(
            cx - r_inner_ring, cy - r_inner_ring, cx + r_inner_ring, cy + r_inner_ring,
            fill="#FAFAFA", outline="#E2E8F0", width=1.0
        )

        # Draw Degree Ticks (360 degrees)
        for d in range(0, 360, 5):
            # 0° is North (top)
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
            if d % 30 == 0:
                lbl_r = r_outer - 18.0
                lx = cx + lbl_r * math.cos(rad)
                ly = cy + lbl_r * math.sin(rad)
                self.create_text(
                    lx, ly,
                    text=f"{d}°",
                    fill="#64748B",
                    font=(FONT_FAMILY, 7, "bold")
                )

        # Draw Cardinal Marks (N, E, S, W faint guidelines)
        self.create_line(cx - 15, cy, cx + 15, cy, fill="#E2E8F0", width=1)
        self.create_line(cx, cy - 15, cx, cy + 15, fill="#E2E8F0", width=1)

        # Draw Indicator Needle (Angle in degrees clockwise from 12 o'clock)
        needle_rad = math.radians(self.angle_deg - 90)
        needle_len = r_inner_ring - 6.0
        back_len = 14.0

        # Main Needle Tip
        nx = cx + needle_len * math.cos(needle_rad)
        ny = cy + needle_len * math.sin(needle_rad)

        # Counterbalance tail
        bx = cx - back_len * math.cos(needle_rad)
        by = cy - back_len * math.sin(needle_rad)

        # Needle width offsets
        perp_rad = needle_rad + math.pi / 2.0
        w = 3.5
        p1x = cx + w * math.cos(perp_rad)
        p1y = cy + w * math.sin(perp_rad)
        p2x = cx - w * math.cos(perp_rad)
        p2y = cy - w * math.sin(perp_rad)

        # Tapered Needle Polygon
        self.create_polygon(
            bx, by, p1x, p1y, nx, ny, p2x, p2y,
            fill="#0F172A", outline="#0F172A"
        )
        # Red Accent Tip line
        tip_x = cx + (needle_len - 14) * math.cos(needle_rad)
        tip_y = cy + (needle_len - 14) * math.sin(needle_rad)
        self.create_line(tip_x, tip_y, nx, ny, fill="#EF4444", width=2.5)

        # Center Pivot Cap
        self.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill="#0F172A", outline="#E2E8F0", width=1.5)
        self.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill="#FFFFFF")


# ═════════════════════════════════════════════════════════════════════
# 3. HELPER CARD CONTAINER WIDGET
# ═════════════════════════════════════════════════════════════════════
class EnterpriseCard(ctk.CTkFrame):
    """Clean white card with subtle 1px border and 8px corner radius."""
    def __init__(self, master, title: str = "", **kwargs):
        super().__init__(
            master,
            fg_color=CLR_CARD_BG,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=8,
            **kwargs
        )
        if title:
            header_box = ctk.CTkFrame(self, fg_color="transparent", height=32)
            header_box.pack(fill="x", padx=16, pady=(12, 6))

            title_label = ctk.CTkLabel(
                header_box,
                text=title,
                font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
                text_color=CLR_TEXT_PRIMARY,
                anchor="w"
            )
            title_label.pack(side="left")


# ═════════════════════════════════════════════════════════════════════
# 4. MAIN APPLICATION WINDOW
# ═════════════════════════════════════════════════════════════════════
class FlapDataLoggerCleanLightApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Window Metadata
        self.title("GMF AeroAsia | FLAP SENSOR DATA LOGGER — API 8810A DIGITAL PROTRACTOR")
        self.geometry("1380x880")
        self.minsize(1180, 780)

        # Appearance Settings
        ctk.set_appearance_mode("light")
        self.configure(fg_color=CLR_BG_MAIN)

        # State Variables
        self.is_connected = True
        self.is_workbook_loaded = True
        self.is_paused = False
        self.auto_save_active = tk.BooleanVar(value=True)
        self.sim_mode_active = tk.BooleanVar(value=False)
        self.target_row_var = tk.IntVar(value=11)
        self.is_target_row_locked = True
        self.live_angle_val = 17.50
        self.is_log_expanded = True

        # Build UI Sections
        self._build_header_bar()
        self._build_main_body()
        self._build_footer_bar()

        # Keyboard Shortcuts
        self.bind("<Return>", lambda e: self.on_save_data())
        self.bind("<space>", lambda e: self.on_toggle_pause())

        # Start Realtime Clock & Telemetry Simulation Timer
        self._update_clock()
        self._schedule_telemetry_stream()

    # ─────────────────────────────────────────────────────────────────
    # HEADER BAR
    # ─────────────────────────────────────────────────────────────────
    def _build_header_bar(self):
        header = ctk.CTkFrame(self, fg_color=CLR_CARD_BG, height=58, corner_radius=0, border_color=CLR_BORDER, border_width=1)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        inner = ctk.CTkFrame(header, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=20)

        # Left: Branding & App Title
        left_box = ctk.CTkFrame(inner, fg_color="transparent")
        left_box.pack(side="left", fill="y")

        brand_lbl = ctk.CTkLabel(
            left_box,
            text="GMF AeroAsia",
            font=ctk.CTkFont(family=FONT_FAMILY, size=17, weight="bold"),
            text_color="#034EA2"  # Official Garuda Blue
        )
        brand_lbl.pack(side="left", pady=14)

        divider = ctk.CTkLabel(
            left_box,
            text="|",
            font=ctk.CTkFont(family=FONT_FAMILY, size=16),
            text_color=CLR_BORDER_FOCUS
        )
        divider.pack(side="left", padx=12, pady=14)

        title_lbl = ctk.CTkLabel(
            left_box,
            text="FLAP SENSOR DATA LOGGER - API 8810A DIGITAL PROTRACTOR",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            text_color=CLR_TEXT_PRIMARY
        )
        title_lbl.pack(side="left", pady=14)

        # Right: Status Badges & Digital Clock
        right_box = ctk.CTkFrame(inner, fg_color="transparent")
        right_box.pack(side="right", fill="y")

        # 1. Connected Pill Badge
        self.badge_conn = ctk.CTkFrame(
            right_box,
            fg_color=CLR_GREEN_LIGHT,
            border_color="#86EFAC",
            border_width=1,
            corner_radius=20,
            height=28
        )
        self.badge_conn.pack(side="left", padx=(0, 8), pady=14)
        conn_text = ctk.CTkLabel(
            self.badge_conn,
            text="● CONNECTED",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=CLR_GREEN_PRIMARY,
            padx=12
        )
        conn_text.pack(pady=3)

        # 2. Workbook Loaded Pill Badge
        self.badge_wb = ctk.CTkFrame(
            right_box,
            fg_color=CLR_TEAL_LIGHT,
            border_color=CLR_TEAL_BORDER,
            border_width=1,
            corner_radius=20,
            height=28
        )
        self.badge_wb.pack(side="left", padx=(0, 14), pady=14)
        wb_text = ctk.CTkLabel(
            self.badge_wb,
            text="● WORKBOOK LOADED",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=CLR_TEAL_TEXT,
            padx=12
        )
        wb_text.pack(pady=3)

        # 3. Digital Clock
        self.clock_lbl = ctk.CTkLabel(
            right_box,
            text="08:08:38",
            font=ctk.CTkFont(family="Consolas", size=14, weight="bold"),
            text_color=CLR_TEXT_PRIMARY
        )
        self.clock_lbl.pack(side="left", pady=14)

    # ─────────────────────────────────────────────────────────────────
    # MAIN BODY (Sidebar + Central Grid)
    # ─────────────────────────────────────────────────────────────────
    def _build_main_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(12, 6))

        # ──────── Sidebar (Left Column, Width ~280px) ────────
        sidebar = ctk.CTkFrame(body, fg_color="transparent", width=280)
        sidebar.pack(side="left", fill="y", padx=(0, 12))
        sidebar.pack_propagate(False)

        # Card 1: CONNECTION HUB
        card_conn = EnterpriseCard(sidebar, title="CONNECTION HUB")
        card_conn.pack(fill="x", pady=(0, 12))

        f_conn = ctk.CTkFrame(card_conn, fg_color="transparent")
        f_conn.pack(fill="x", padx=16, pady=(0, 14))

        # IP & Port
        ip_port_box = ctk.CTkFrame(f_conn, fg_color="transparent")
        ip_port_box.pack(fill="x", pady=(0, 10))

        lbl_ip = ctk.CTkLabel(ip_port_box, text="IP Address", font=ctk.CTkFont(family=FONT_FAMILY, size=11), text_color=CLR_TEXT_SECONDARY)
        lbl_ip.grid(row=0, column=0, sticky="w")
        self.entry_ip = ctk.CTkEntry(ip_port_box, font=ctk.CTkFont(family=FONT_FAMILY, size=12), width=155, height=32, corner_radius=6, border_color=CLR_BORDER, fg_color="#FFFFFF", text_color=CLR_TEXT_PRIMARY)
        self.entry_ip.insert(0, "192.168.1.21")
        self.entry_ip.grid(row=1, column=0, sticky="w", padx=(0, 8))

        lbl_port = ctk.CTkLabel(ip_port_box, text="Port", font=ctk.CTkFont(family=FONT_FAMILY, size=11), text_color=CLR_TEXT_SECONDARY)
        lbl_port.grid(row=0, column=1, sticky="w")
        self.entry_port = ctk.CTkEntry(ip_port_box, font=ctk.CTkFont(family=FONT_FAMILY, size=12), width=75, height=32, corner_radius=6, border_color=CLR_BORDER, fg_color="#FFFFFF", text_color=CLR_TEXT_PRIMARY)
        self.entry_port.insert(0, "23")
        self.entry_port.grid(row=1, column=1, sticky="w")

        # Preset Command
        lbl_cmd = ctk.CTkLabel(f_conn, text="Preset Command", font=ctk.CTkFont(family=FONT_FAMILY, size=11), text_color=CLR_TEXT_SECONDARY)
        lbl_cmd.pack(anchor="w", pady=(0, 2))
        self.cmd_dropdown = ctk.CTkOptionMenu(
            f_conn,
            values=["API1 ANGLE?", "API2 ANGLE?", "API1 AVG_ANGLE?", "*IDN?", "*ERR?"],
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#FFFFFF",
            button_color="#E2E8F0",
            button_hover_color="#CBD5E1",
            text_color=CLR_TEXT_PRIMARY,
            dropdown_text_color=CLR_TEXT_PRIMARY,
            dropdown_fg_color="#FFFFFF",
            corner_radius=6,
            height=32
        )
        self.cmd_dropdown.pack(fill="x", pady=(0, 10))

        # Simulator Mode Toggle
        sim_box = ctk.CTkFrame(f_conn, fg_color="transparent")
        sim_box.pack(fill="x", pady=(0, 14))
        lbl_sim = ctk.CTkLabel(sim_box, text="Simulator Mode", font=ctk.CTkFont(family=FONT_FAMILY, size=12), text_color=CLR_TEXT_PRIMARY)
        lbl_sim.pack(side="left")
        self.switch_sim = ctk.CTkSwitch(
            sim_box,
            text="",
            variable=self.sim_mode_active,
            width=42,
            progress_color=CLR_BLUE_PRIMARY
        )
        self.switch_sim.pack(side="right")

        # Connect Button (Solid Green)
        self.btn_connect = ctk.CTkButton(
            f_conn,
            text="CONNECT",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            fg_color=CLR_GREEN_PRIMARY,
            hover_color=CLR_GREEN_HOVER,
            corner_radius=6,
            height=36,
            command=self.on_toggle_connect
        )
        self.btn_connect.pack(fill="x")

        # Card 2: WORKBOOK SETUP
        card_wb = EnterpriseCard(sidebar, title="WORKBOOK SETUP")
        card_wb.pack(fill="x")

        f_wb = ctk.CTkFrame(card_wb, fg_color="transparent")
        f_wb.pack(fill="x", padx=16, pady=(0, 16))

        # Master Workbook File
        lbl_mwb = ctk.CTkLabel(f_wb, text="Master Workbook", font=ctk.CTkFont(family=FONT_FAMILY, size=11), text_color=CLR_TEXT_SECONDARY)
        lbl_mwb.pack(anchor="w", pady=(0, 2))

        mwb_box = ctk.CTkFrame(f_wb, fg_color="transparent")
        mwb_box.pack(fill="x", pady=(0, 10))
        self.entry_mwb = ctk.CTkEntry(
            mwb_box,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            height=32,
            corner_radius=6,
            border_color=CLR_BORDER,
            fg_color="#FFFFFF",
            text_color=CLR_TEXT_PRIMARY
        )
        self.entry_mwb.insert(0, "MASTER_rev3.xlsx")
        self.entry_mwb.pack(side="left", fill="x", expand=True, padx=(0, 6))

        btn_browse = ctk.CTkButton(
            mwb_box,
            text="Browse",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            width=65,
            height=32,
            fg_color="#F8FAFC",
            hover_color="#E2E8F0",
            text_color=CLR_TEXT_PRIMARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            command=self.on_browse_workbook
        )
        btn_browse.pack(side="right")

        # Active Worksheet
        lbl_sheet = ctk.CTkLabel(f_wb, text="Active Worksheet", font=ctk.CTkFont(family=FONT_FAMILY, size=11), text_color=CLR_TEXT_SECONDARY)
        lbl_sheet.pack(anchor="w", pady=(0, 2))
        self.sheet_dropdown = ctk.CTkOptionMenu(
            f_wb,
            values=["ACCURACY STALL", "ACCURACY AIRSPEED", "DYNAMIC CHECK", "SUMMARY"],
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#FFFFFF",
            button_color="#E2E8F0",
            button_hover_color="#CBD5E1",
            text_color=CLR_TEXT_PRIMARY,
            dropdown_text_color=CLR_TEXT_PRIMARY,
            dropdown_fg_color="#FFFFFF",
            corner_radius=6,
            height=32
        )
        self.sheet_dropdown.pack(fill="x")

        # ──────── Main Area (Right of Sidebar) ────────
        main_content = ctk.CTkFrame(body, fg_color="transparent")
        main_content.pack(side="right", fill="both", expand=True)

        # Top Area: Two Cards Side-by-Side
        top_panels = ctk.CTkFrame(main_content, fg_color="transparent")
        top_panels.pack(fill="x", pady=(0, 12))

        # Panel Left: LIVE ANGLE READING — NAI API 8810A
        card_reading = EnterpriseCard(top_panels, title="LIVE ANGLE READING — NAI API 8810A")
        card_reading.pack(side="left", fill="both", expand=True, padx=(0, 8))

        reading_body = ctk.CTkFrame(card_reading, fg_color="transparent")
        reading_body.pack(fill="both", expand=True, padx=20, pady=(0, 14))

        # Digital Readout (Left Column)
        readout_col = ctk.CTkFrame(reading_body, fg_color="transparent")
        readout_col.pack(side="left", fill="both", expand=True, padx=(10, 10))

        self.lbl_angle_readout = ctk.CTkLabel(
            readout_col,
            text=f"{self.live_angle_val:.2f}°",
            font=ctk.CTkFont(family=FONT_FAMILY, size=52, weight="bold"),
            text_color=CLR_TEXT_PRIMARY,
            anchor="w"
        )
        self.lbl_angle_readout.pack(anchor="w", pady=(18, 6))

        # Sensor Stable Badge
        self.badge_sensor_stable = ctk.CTkFrame(
            readout_col,
            fg_color=CLR_GREEN_LIGHT,
            border_color="#86EFAC",
            border_width=1,
            corner_radius=6,
            height=26
        )
        self.badge_sensor_stable.pack(anchor="w")
        lbl_stable = ctk.CTkLabel(
            self.badge_sensor_stable,
            text="SENSOR STABLE",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=CLR_GREEN_PRIMARY,
            padx=10,
            pady=2
        )
        lbl_stable.pack()

        # Circular Protractor Dial (Right Column)
        self.protractor = CircularProtractorCanvas(reading_body, size=180)
        self.protractor.pack(side="right", padx=(10, 10), pady=(0, 6))

        # Panel Right: COCKPIT CONTROLS & MANUAL SAVE
        card_cockpit = EnterpriseCard(top_panels, title="COCKPIT CONTROLS & MANUAL SAVE")
        card_cockpit.pack(side="right", fill="both", expand=True, padx=(8, 0))

        cockpit_body = ctk.CTkFrame(card_cockpit, fg_color="transparent")
        cockpit_body.pack(fill="both", expand=True, padx=18, pady=(0, 14))

        # Light-Teal Highlighted Container for Target Row
        target_container = ctk.CTkFrame(
            cockpit_body,
            fg_color=CLR_TEAL_LIGHT,
            border_color=CLR_TEAL_BORDER,
            border_width=1,
            corner_radius=8,
            height=44
        )
        target_container.pack(fill="x", pady=(4, 10))
        target_container.pack_propagate(False)

        lbl_tr = ctk.CTkLabel(
            target_container,
            text="Target Row",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            text_color=CLR_TEXT_PRIMARY
        )
        lbl_tr.pack(side="left", padx=(14, 10))

        # Stepper Controls
        stepper_box = ctk.CTkFrame(target_container, fg_color="transparent")
        stepper_box.pack(side="right", padx=10)

        btn_minus = ctk.CTkButton(
            stepper_box,
            text="—",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            width=28,
            height=28,
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color="#CBD5E1",
            border_width=1,
            corner_radius=4,
            command=self.on_dec_row
        )
        btn_minus.pack(side="left", padx=2)

        self.entry_row = ctk.CTkEntry(
            stepper_box,
            textvariable=self.target_row_var,
            font=ctk.CTkFont(family="Consolas", size=13, weight="bold"),
            width=46,
            height=28,
            corner_radius=4,
            border_color="#CBD5E1",
            fg_color="#FFFFFF",
            text_color=CLR_TEXT_PRIMARY,
            justify="center"
        )
        self.entry_row.pack(side="left", padx=2)

        btn_plus = ctk.CTkButton(
            stepper_box,
            text="+",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            width=28,
            height=28,
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color="#CBD5E1",
            border_width=1,
            corner_radius=4,
            command=self.on_inc_row
        )
        btn_plus.pack(side="left", padx=2)

        self.btn_edit_row = ctk.CTkButton(
            stepper_box,
            text="✎",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            width=28,
            height=28,
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_SECONDARY,
            border_color="#CBD5E1",
            border_width=1,
            corner_radius=4,
            command=self.on_toggle_edit_row
        )
        self.btn_edit_row.pack(side="left", padx=(4, 0))

        # Auto-Save Row with Switch
        as_box = ctk.CTkFrame(cockpit_body, fg_color="transparent")
        as_box.pack(fill="x", pady=(0, 10))
        lbl_as = ctk.CTkLabel(as_box, text="Auto-Save", font=ctk.CTkFont(family=FONT_FAMILY, size=12), text_color=CLR_TEXT_PRIMARY)
        lbl_as.pack(side="left")

        self.switch_as = ctk.CTkSwitch(
            as_box,
            text="ON",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            variable=self.auto_save_active,
            progress_color=CLR_BLUE_PRIMARY,
            command=self._on_autosave_toggle
        )
        self.switch_as.pack(side="right")

        # Primary Blue Button: SAVE DATA (ENTER)
        self.btn_save = ctk.CTkButton(
            cockpit_body,
            text="SAVE DATA (ENTER)",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            fg_color=CLR_BLUE_PRIMARY,
            hover_color=CLR_BLUE_HOVER,
            corner_radius=6,
            height=36,
            command=self.on_save_data
        )
        self.btn_save.pack(fill="x", pady=(0, 8))

        # Secondary Button Row
        btn_row = ctk.CTkFrame(cockpit_body, fg_color="transparent")
        btn_row.pack(fill="x")

        self.btn_pause = ctk.CTkButton(
            btn_row,
            text="PAUSE (SPACE)",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            height=30,
            command=self.on_toggle_pause
        )
        self.btn_pause.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.btn_undo = ctk.CTkButton(
            btn_row,
            text="UNDO LAST",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            height=30,
            command=self.on_undo_last
        )
        self.btn_undo.pack(side="left", fill="x", expand=True, padx=4)

        self.btn_reset_row = ctk.CTkButton(
            btn_row,
            text=f"RESET ROW {self.target_row_var.get()}",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6,
            height=30,
            command=self.on_reset_row
        )
        self.btn_reset_row.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Middle-Right Wide Card: EXCEL LIVE MONITORING & HISTORY
        card_table = EnterpriseCard(main_content, title="EXCEL LIVE MONITORING & HISTORY")
        card_table.pack(fill="both", expand=True)

        # Filter controls at top-right of table card
        # Place header controls inside custom header box
        t_header_box = ctk.CTkFrame(card_table, fg_color="transparent")
        t_header_box.place(relx=1.0, rely=0.015, anchor="ne", x=-16, y=10)

        lbl_sp = ctk.CTkLabel(t_header_box, text="Setpoint (°)", font=ctk.CTkFont(family=FONT_FAMILY, size=11), text_color=CLR_TEXT_SECONDARY)
        lbl_sp.pack(side="left", padx=(0, 4))
        self.spin_sp = ctk.CTkOptionMenu(
            t_header_box,
            values=["0", "11", "17.5", "45", "90"],
            width=65,
            height=26,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            button_color="#E2E8F0",
            button_hover_color="#CBD5E1",
            text_color=CLR_TEXT_PRIMARY,
            dropdown_text_color=CLR_TEXT_PRIMARY,
            dropdown_fg_color="#FFFFFF"
        )
        self.spin_sp.set("11")
        self.spin_sp.pack(side="left", padx=(0, 10))

        lbl_tol = ctk.CTkLabel(t_header_box, text="Tolerance (±°)", font=ctk.CTkFont(family=FONT_FAMILY, size=11), text_color=CLR_TEXT_SECONDARY)
        lbl_tol.pack(side="left", padx=(0, 4))
        self.spin_tol = ctk.CTkOptionMenu(
            t_header_box,
            values=["0.5", "1.0", "2.0", "5.0"],
            width=65,
            height=26,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            button_color="#E2E8F0",
            button_hover_color="#CBD5E1",
            text_color=CLR_TEXT_PRIMARY,
            dropdown_text_color=CLR_TEXT_PRIMARY,
            dropdown_fg_color="#FFFFFF"
        )
        self.spin_tol.set("1.0")
        self.spin_tol.pack(side="left", padx=(0, 10))

        btn_refresh = ctk.CTkButton(
            t_header_box,
            text="⟳",
            font=ctk.CTkFont(family=FONT_FAMILY, size=14),
            width=30,
            height=26,
            fg_color="#FFFFFF",
            hover_color="#F1F5F9",
            text_color=CLR_TEXT_PRIMARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=4,
            command=self.on_refresh_table
        )
        btn_refresh.pack(side="left")

        # 13-Column DataGrid / Treeview Table
        table_container = ctk.CTkFrame(card_table, fg_color="transparent")
        table_container.pack(fill="both", expand=True, padx=16, pady=(16, 14))

        self._init_data_table(table_container)

    def _init_data_table(self, parent):
        # Apply Clean Light Enterprise theme to ttk.Treeview
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "CleanLight.Treeview",
            background="#FFFFFF",
            foreground=CLR_TEXT_PRIMARY,
            fieldbackground="#FFFFFF",
            rowheight=30,
            font=(FONT_FAMILY, 9),
            borderwidth=0
        )
        style.configure(
            "CleanLight.Treeview.Heading",
            background="#F8FAFC",
            foreground="#475569",
            font=(FONT_FAMILY, 9, "bold"),
            relief="flat",
            padding=(6, 6)
        )
        style.map(
            "CleanLight.Treeview",
            background=[("selected", "#E0F2FE")],
            foreground=[("selected", "#0F172A")]
        )

        columns = (
            "idx", "row", "test_point", "input_turn", "shaft_angle",
            "syncro_out", "top_limit", "bottom_limit", "result",
            "remark", "difference", "real_value", "status"
        )
        headers = (
            "#", "ROW", "TEST POINT", "INPUT TURN", "SHAFT ANGLE",
            "SYNCRO ELECT. OUTPUT", "TOP LIMIT", "BOTTOM LIMIT", "RESULT",
            "REMARK", "DIFFERENCE", "REAL VALUE", "STATUS"
        )
        widths = (35, 45, 80, 80, 90, 140, 80, 90, 75, 75, 85, 85, 95)

        self.tree = ttk.Treeview(
            parent,
            columns=columns,
            show="headings",
            style="CleanLight.Treeview",
            selectmode="browse"
        )

        for col, h_text, w in zip(columns, headers, widths):
            self.tree.heading(col, text=h_text)
            self.tree.column(col, width=w, anchor="center")

        # Custom tag for PASS rows
        self.tree.tag_configure("pass_tag", foreground=CLR_GREEN_PRIMARY)
        self.tree.tag_configure("even_row", background="#FFFFFF")
        self.tree.tag_configure("odd_row", background="#F8FAFC")

        # Scrollbars
        scroll_y = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll_y.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scroll_y.pack(side="right", fill="y")

        # Populate Initial Mock Data matching Prompt
        mock_rows = [
            ("1", "11", "01", "0", "17.50°", "12.33", "45°", "45°", "PASS", "-", "-0°", "17.50", "COMPLETED"),
            ("2", "11", "02", "0", "17.50°", "12.07", "45°", "45°", "PASS", "-", "-0°", "17.50", "COMPLETED"),
        ]
        for i, row in enumerate(mock_rows):
            tag = "even_row" if i % 2 == 0 else "odd_row"
            self.tree.insert("", "end", values=row, tags=(tag, "pass_tag"))

    # ─────────────────────────────────────────────────────────────────
    # FOOTER BAR (EVENT LOG)
    # ─────────────────────────────────────────────────────────────────
    def _build_footer_bar(self):
        self.footer_card = EnterpriseCard(self)
        self.footer_card.pack(fill="x", side="bottom", padx=16, pady=(0, 12))

        # Log Header
        log_header = ctk.CTkFrame(self.footer_card, fg_color="transparent", height=28)
        log_header.pack(fill="x", padx=16, pady=(8, 4))

        lbl_log = ctk.CTkLabel(
            log_header,
            text="EVENT LOG",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=CLR_TEXT_SECONDARY
        )
        lbl_log.pack(side="left")

        self.btn_collapse_log = ctk.CTkButton(
            log_header,
            text="^",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            width=26,
            height=22,
            fg_color="#F8FAFC",
            hover_color="#E2E8F0",
            text_color=CLR_TEXT_SECONDARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=4,
            command=self.on_toggle_log_collapse
        )
        self.btn_collapse_log.pack(side="right")

        # Collapsible Log Textbox
        self.log_container = ctk.CTkFrame(self.footer_card, fg_color="transparent")
        self.log_container.pack(fill="x", padx=16, pady=(0, 10))

        self.log_textbox = ctk.CTkTextbox(
            self.log_container,
            height=70,
            font=ctk.CTkFont(family="Consolas", size=10),
            fg_color="#FFFFFF",
            text_color=CLR_TEXT_PRIMARY,
            border_color=CLR_BORDER,
            border_width=1,
            corner_radius=6
        )
        self.log_textbox.pack(fill="x")

        # Initial Timestamped Logs
        t0 = datetime.now().strftime("%H:%M:%S")
        self._append_log(f"[{t0}] System initialized: FLAP SENSOR DATA LOGGER (GMF AeroAsia).")
        self._append_log(f"[{t0}] Connection Hub configured for API 8810A at 192.168.1.21:23.")
        self._append_log(f"[{t0}] Master Workbook loaded: 'MASTER_rev3.xlsx' (Worksheet: ACCURACY STALL).")
        self._append_log(f"[{t0}] Digital Protractor sensor telemetry stream active at 17.50° [SENSOR STABLE].")

    def _append_log(self, text: str):
        self.log_textbox.insert("end", text + "\n")
        self.log_textbox.see("end")

    # ─────────────────────────────────────────────────────────────────
    # EVENT HANDLERS & LOGIC
    # ─────────────────────────────────────────────────────────────────
    def _update_clock(self):
        curr_time = datetime.now().strftime("%H:%M:%S")
        self.clock_lbl.configure(text=curr_time)
        self.after(1000, self._update_clock)

    def _schedule_telemetry_stream(self):
        """Simulate micro-fluctuations in stable sensor telemetry."""
        if not self.is_paused:
            # Fluctuate slightly around 17.50°
            self.live_angle_val = 17.50 + (math.sin(time.time() * 2.0) * 0.04)
            self.lbl_angle_readout.configure(text=f"{self.live_angle_val:.2f}°")
            # Update protractor needle smoothly
            self.protractor.set_angle(self.live_angle_val)
        self.after(100, self._schedule_telemetry_stream)

    def on_toggle_connect(self):
        self.is_connected = not self.is_connected
        t = datetime.now().strftime("%H:%M:%S")
        if self.is_connected:
            self.btn_connect.configure(text="DISCONNECT", fg_color=CLR_DANGER, hover_color="#B91C1C")
            self.badge_conn.configure(fg_color=CLR_GREEN_LIGHT, border_color="#86EFAC")
            self.badge_conn.winfo_children()[0].configure(text="● CONNECTED", text_color=CLR_GREEN_PRIMARY)
            self._append_log(f"[{t}] Hardware stream connected to API 8810A.")
        else:
            self.btn_connect.configure(text="CONNECT", fg_color=CLR_GREEN_PRIMARY, hover_color=CLR_GREEN_HOVER)
            self.badge_conn.configure(fg_color="#FEE2E2", border_color="#FCA5A5")
            self.badge_conn.winfo_children()[0].configure(text="● DISCONNECTED", text_color=CLR_DANGER)
            self._append_log(f"[{t}] Hardware stream disconnected.")

    def on_browse_workbook(self):
        filename = filedialog.askopenfilename(
            title="Select Master Workbook",
            filetypes=[("Excel Files", "*.xlsx *.xlsm"), ("All Files", "*.*")]
        )
        if filename:
            self.entry_mwb.delete(0, "end")
            self.entry_mwb.insert(0, os.path.basename(filename))
            t = datetime.now().strftime("%H:%M:%S")
            self._append_log(f"[{t}] Selected workbook: {filename}")

    def on_inc_row(self):
        val = self.target_row_var.get() + 1
        self.target_row_var.set(val)
        self.btn_reset_row.configure(text=f"RESET ROW {val}")

    def on_dec_row(self):
        val = max(1, self.target_row_var.get() - 1)
        self.target_row_var.set(val)
        self.btn_reset_row.configure(text=f"RESET ROW {val}")

    def on_toggle_edit_row(self):
        self.is_target_row_locked = not self.is_target_row_locked
        if self.is_target_row_locked:
            self.btn_edit_row.configure(text_color=CLR_TEXT_SECONDARY, fg_color="#FFFFFF")
            self.entry_row.configure(state="normal")
            val = self.target_row_var.get()
            self.btn_reset_row.configure(text=f"RESET ROW {val}")
            t = datetime.now().strftime("%H:%M:%S")
            self._append_log(f"[{t}] Target row locked to {val}.")
        else:
            self.btn_edit_row.configure(text_color=CLR_BLUE_PRIMARY, fg_color=CLR_TEAL_LIGHT)
            self.entry_row.focus_set()

    def _on_autosave_toggle(self):
        state_str = "ON" if self.auto_save_active.get() else "OFF"
        self.switch_as.configure(text=state_str)
        t = datetime.now().strftime("%H:%M:%S")
        self._append_log(f"[{t}] Auto-Save switched {state_str}.")

    def on_save_data(self):
        row = self.target_row_var.get()
        curr_items = len(self.tree.get_children()) + 1
        test_pt = f"{curr_items:02d}"
        angle_str = f"{self.live_angle_val:.2f}°"
        t = datetime.now().strftime("%H:%M:%S")

        new_record = (
            str(curr_items), str(row), test_pt, "0", angle_str,
            "12.15", "45°", "45°", "PASS", "-", "-0°", f"{self.live_angle_val:.2f}", "COMPLETED"
        )
        tag = "even_row" if curr_items % 2 != 0 else "odd_row"
        self.tree.insert("", "end", values=new_record, tags=(tag, "pass_tag"))
        self._append_log(f"[{t}] Data recorded successfully for Row {row} (Point {test_pt}: {angle_str} -> PASS).")

        # Auto increment row if required
        self.on_inc_row()

    def on_toggle_pause(self):
        self.is_paused = not self.is_paused
        t = datetime.now().strftime("%H:%M:%S")
        if self.is_paused:
            self.btn_pause.configure(text="RESUME (SPACE)", fg_color="#FEF3C7", text_color=CLR_WARNING)
            self._append_log(f"[{t}] Telemetry acquisition paused.")
        else:
            self.btn_pause.configure(text="PAUSE (SPACE)", fg_color="#FFFFFF", text_color=CLR_TEXT_PRIMARY)
            self._append_log(f"[{t}] Telemetry acquisition resumed.")

    def on_undo_last(self):
        items = self.tree.get_children()
        if items:
            last_item = items[-1]
            vals = self.tree.item(last_item)["values"]
            self.tree.delete(last_item)
            t = datetime.now().strftime("%H:%M:%S")
            self._append_log(f"[{t}] Reverted last logged measurement (Point {vals[2]}).")
            self.on_dec_row()

    def on_reset_row(self):
        t = datetime.now().strftime("%H:%M:%S")
        self._append_log(f"[{t}] Row sampling counter reset for Row {self.target_row_var.get()}.")

    def on_refresh_table(self):
        t = datetime.now().strftime("%H:%M:%S")
        self._append_log(f"[{t}] Workbook data table refreshed.")

    def on_toggle_log_collapse(self):
        self.is_log_expanded = not self.is_log_expanded
        if self.is_log_expanded:
            self.log_container.pack(fill="x", padx=16, pady=(0, 10))
            self.btn_collapse_log.configure(text="^")
        else:
            self.log_container.pack_forget()
            self.btn_collapse_log.configure(text="v")


# ═════════════════════════════════════════════════════════════════════
# APPLICATION ENTRY POINT
# ═════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = FlapDataLoggerCleanLightApp()
    app.mainloop()
