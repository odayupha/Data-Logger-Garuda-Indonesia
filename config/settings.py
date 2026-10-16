"""
config/settings.py
──────────────────
Centralized configuration for the Flap Sensor Data Logger backend.
All tunable parameters live here — no magic numbers in other modules.
"""

from __future__ import annotations

# ── Excel Workbook Settings ────────────────────────────────────────────
EXCEL_RESULT_COLUMN: str = "G"          # Column that receives angle values
EXCEL_START_ROW: int = 11               # First data row (rows 1-10 = headers/metadata)

# ── NAI 8810A Ethernet Connection ─────────────────────────────────────
NAI_DEFAULT_HOST: str = "192.168.1.21"
NAI_DEFAULT_PORT: int = 23
NAI_DEFAULT_COMMAND: str = "API1 ANGLE?"
NAI_POLL_INTERVAL_SEC: float = 0.03    # ~33 Hz real-time polling

# ── Stability Detection ────────────────────────────────────────────────
# Precision tolerance: max allowable spread across all samples in the stable window.
STABILITY_TOLERANCE_DEG: float = 0.045     # degrees — allowable trimmed spread in window
STABILITY_MAX_DRIFT_DEG: float = 0.015     # degrees — max allowable drift rate to prevent creep
STABILITY_MIN_SAMPLES: int = 8             # minimum consecutive samples required (~250-300ms min)
TARGET_SAMPLE_COUNT: int = 8               # alias for consistency
STABILITY_MIN_DURATION_SEC: float = 1.0    # minimum wall-clock seconds sensor must be stationary
TARGET_STABLE_DURATION_SEC: float = 1.0    # alias for consistency
MAX_SAMPLE_GAP_SEC: float = 2.5            # max allowed gap between samples before reset
MOVEMENT_THRESHOLD_DEG: float = 0.08       # degrees — active motion detection delta
HYSTERESIS_BAND_DEG: float = 0.025         # degrees — hysteresis margin between stable and moving

# ── Auto-Save Re-arm & Debounce ─────────────────────────────────────────
# Angle must move by at least this much from the stable reference before
# the latch can re-open. 0.18° allows detecting small rigging steps (e.g. 0.28°)
# while remaining well above typical synchro electrical noise (0.02° - 0.04°).
AUTO_SAVE_REARM_THRESHOLD_DEG: float = 0.18
REARM_THRESHOLD_DEG: float = 0.18          # alias for hybrid stability
AUTO_SAVE_MIN_INTERVAL_SEC: float = 1.2    # minimum seconds between auto-saves (debounce)

# ── Excel Repository ───────────────────────────────────────────────────
EXCEL_WRITE_MAX_RETRIES: int = 3
EXCEL_WRITE_RETRY_DELAY_SEC: float = 0.8   # seconds between retries

# ── Logging ────────────────────────────────────────────────────────────
LOG_LEVEL: str = "INFO"                # DEBUG | INFO | WARNING | ERROR | CRITICAL
LOG_FORMAT: str = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
LOG_DATE_FORMAT: str = "%H:%M:%S"

# ── Simulator ──────────────────────────────────────────────────────────
# When False, allows writing during simulation and real sensor testing.
BLOCK_SIMULATOR_WRITES: bool = False
