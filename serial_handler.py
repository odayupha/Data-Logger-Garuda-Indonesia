"""
serial_handler.py
─────────────────
Background serial communication handler for the API 8810A Angle Position
Indicator (North Atlantic Industries).  Runs in a dedicated daemon thread
so the CustomTkinter GUI never blocks.

Key protocol details (NAI 8810A over USB / Virtual COM / FTDI):
  • Every command MUST be terminated with \r\n (CR+LF).
  • The instrument replies with an ASCII float (e.g. "2.427\r\n").
  • The input buffer must be flushed BEFORE each query to avoid
    reading stale data from a previous cycle.
  • Supported query commands (SCPI-style):
        MEASure:ANGle?     – read angle (degrees)
        FETC:ANG? CH1      – fetch angle, channel 1
        FETC:ANG? CH2      – fetch angle, channel 2
        FETCH?  /  READ?   – read active channel
  • Read timeout should be 1.0 – 2.0 s to tolerate slow responses.

Author : Senior Python Dev – GMF AeroAsia Tooling
Rev    : 2.0  (2026-10-09) – full refactor for reliable NAI 8810A comms
"""

import re
import socket
import threading
import time
import random
from typing import Callable, Optional

import serial
import serial.tools.list_ports


# Compiled regex: matches an optional sign, digits, optional
# decimal point + more digits.  Covers "+2.427", "-0.15", "274.926000".
_RE_FLOAT = re.compile(r"[-+]?\d+\.?\d*")


# ──────────────────────────────────────────────────────────────────────
#  Utility: list available COM ports
# ──────────────────────────────────────────────────────────────────────
def list_com_ports() -> list[str]:
    """Return a list of available COM port names (e.g. ['COM3', 'COM5'])."""
    ports = serial.tools.list_ports.comports()
    return [p.device for p in sorted(ports)]


# ──────────────────────────────────────────────────────────────────────
#  SerialReader – threaded reader for the API 8810A
# ──────────────────────────────────────────────────────────────────────
class SerialReader:
    """
    Manages a serial connection to the API 8810A.

    Parameters
    ----------
    port : str          – COM port name (e.g. "COM3").
    baudrate : int      – Baud rate (typically 9600).
    command : str       – Primary query command (e.g. "API1AVG_ANGLE?").
    interval : float    – Seconds between queries (default 0.5).
    on_data : callable  – Callback ``on_data(value: float)`` invoked
                          from the worker thread; the GUI should schedule
                          the UI update via ``root.after()``.
    on_error : callable – Callback ``on_error(msg: str)`` for warnings.
    on_log : callable   – Callback ``on_log(msg: str)`` for verbose logs.
    simulator : bool    – If True, generate fake angle readings.
    """

    def __init__(
        self,
        port: str,
        baudrate: int,
        command: str = "API1 ANGLE?",
        interval: float = 0.03,
        on_data: Optional[Callable[[float], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        simulator: bool = False,
    ):
        self.port = port
        self.baudrate = baudrate
        self.command = self._sanitise_command(command)
        self.interval = interval
        self.on_data = on_data
        self.on_error = on_error
        self.on_log = on_log
        self.simulator = simulator

        self._serial: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()

    # ── connect ──────────────────────────────────────────────────────
    def connect(self) -> bool:
        """Open the serial port (or start the simulator) and begin polling. Returns True on success."""
        if self._running.is_set():
            return True  # already running

        if not self.simulator:
            try:
                self._serial = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.6,           # responsive read timeout 0.6 s
                    write_timeout=1.0,     # write timeout 1.0 s
                    xonxoff=False,
                    rtscts=False,
                    dsrdtr=False,
                )
                
                # Activate DTR & RTS for FTDI / USB-serial interfaces
                self._serial.dtr = True
                self._serial.rts = True
                
                time.sleep(0.15)

                # Drain any initial garbage
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()

                self._log_info(
                    f"Port {self.port} opened: {self.baudrate} 8N1, timeout=1.5 s"
                )
            except serial.SerialException as exc:
                if self.on_error:
                    self.on_error(f"Serial open error: {exc}")
                return False

        self._running.set()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        return True

    # ── disconnect ───────────────────────────────────────────────────
    def disconnect(self) -> None:
        """Stop polling and close the serial port."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

        with self._lock:
            if self._serial is not None and self._serial.is_open:
                try:
                    self._serial.reset_input_buffer()
                    self._serial.reset_output_buffer()
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None

    # ══════════════════════════════════════════════════════════════════
    #  INTERNAL POLLING LOOP
    # ══════════════════════════════════════════════════════════════════
    def _poll_loop(self) -> None:
        """Continuously send the query and parse responses."""
        sim_angle = random.uniform(0.0, 360.0)

        while self._running.is_set():
            try:
                if self.simulator:
                    value = self._simulate(sim_angle)
                    sim_angle = value
                else:
                    value = self._query_device()

                if value is not None:
                    if self.on_data:
                        self.on_data(value)
                else:
                    self._log_info("No data / timeout (waiting for 8810A reply)")

            except serial.SerialException as exc:
                if self.on_error:
                    self.on_error(f"Serial I/O error: {exc}")

            except Exception as exc:
                if self.on_error:
                    self.on_error(f"Unexpected: {exc}")

            # Minimal sleep for real-time responsiveness without pegging CPU
            if self.interval > 0:
                waited = 0.0
                step = min(0.01, self.interval)
                while waited < self.interval and self._running.is_set():
                    time.sleep(step)
                    waited += step
            else:
                time.sleep(0.002)

    # ══════════════════════════════════════════════════════════════════
    #  READ HELPERS
    # ══════════════════════════════════════════════════════════════════
    def _read_response(self, timeout: float = 0.6) -> bytes:
        """
        Read until newline (\\n) or carriage return (\\r) without artificial delays.
        """
        if self._serial is None or not self._serial.is_open:
            return b""
        
        start = time.time()
        buf = bytearray()
        while time.time() - start < timeout:
            in_w = self._serial.in_waiting
            chunk = self._serial.read(in_w if in_w > 0 else 1)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in buf or b"\r" in buf:
                break
        return bytes(buf)

    # ══════════════════════════════════════════════════════════════════
    #  QUERY THE REAL DEVICE
    # ══════════════════════════════════════════════════════════════════
    def _query_device(self) -> Optional[float]:
        """Send the active command to the API 8810A and parse the reply."""
        with self._lock:
            if self._serial is None or not self._serial.is_open:
                return None

            try:
                self._serial.reset_input_buffer()
                cmd_clean = self._sanitise_command(self.command)
                wire_bytes = f"{cmd_clean}\r\n".encode("ascii", errors="ignore")
                self._serial.write(wire_bytes)
                raw_bytes = self._read_response(timeout=0.6)
            except serial.SerialException:
                raise
            except Exception as exc:
                self._log_info(f"Write/Read error: {exc}")
                return None

        if not raw_bytes:
            self._log_info(f"Command '{cmd_clean}': No reply (timeout)")
            return None

        raw_str = raw_bytes.decode("ascii", errors="ignore").strip()

        if not raw_str:
            self._log_info(f"Command '{cmd_clean}': Blank response")
            return None

        # Parse the numeric value
        value = self._parse_angle(raw_str)

        if value is None:
            self._log_info(f"Raw reply: '{raw_str}' (Non-numeric response)")

        return value

    def query_raw(self, cmd: str) -> str:
        """
        Send a one-shot query to the 8810A and return the exact raw response string.
        Thread-safe; can be called for diagnostics.
        """
        with self._lock:
            if self._serial is None or not self._serial.is_open:
                return "[Port not connected]"
            try:
                self._serial.reset_input_buffer()
                cmd_clean = self._sanitise_command(cmd)
                wire_bytes = f"{cmd_clean}\r\n".encode("ascii", errors="ignore")
                self._serial.write(wire_bytes)
                raw_bytes = self._read_response(timeout=1.8)
                if not raw_bytes:
                    return "[No reply / Timeout]"
                return raw_bytes.decode("ascii", errors="ignore").strip()
            except Exception as exc:
                return f"[Error: {exc}]"

    # ══════════════════════════════════════════════════════════════════
    #  PARSING
    # ══════════════════════════════════════════════════════════════════
    @staticmethod
    def _parse_angle(text: str) -> Optional[float]:
        """
        Safely extract a float from the device response.

        The API 8810A typically replies with a plain ASCII float:
            "2.427"   "274.926000"   "-0.05"

        Returns the value as full-precision float without premature rounding, or None.
        """
        if not text:
            return None

        # Filter out text errors, banners, and acknowledgments
        upper = text.upper()
        if any(err_kw in upper for err_kw in ("ERROR", "NORTH", "ATLANTIC", "NAI", "OK")):
            return None

        text_clean = text.strip().replace("°", "").replace("DEG", "").strip()

        # First try: direct float conversion (fastest path, retains native precision)
        try:
            return float(text_clean)
        except (ValueError, TypeError):
            pass

        # Second try: regex extraction (only if isolated or cleanly delimited)
        match = _RE_FLOAT.search(text_clean)
        if match:
            try:
                return float(match.group())
            except (ValueError, TypeError):
                pass

        return None

    # ══════════════════════════════════════════════════════════════════
    #  SIMULATOR
    # ══════════════════════════════════════════════════════════════════
    @staticmethod
    def _simulate(current: float) -> float:
        """Generate a smoothly drifting simulated angle value for real-time telemetry."""
        drift = random.gauss(0, 0.04)
        noisy = (current + drift) % 360.0
        return round(noisy, 2)

    # ══════════════════════════════════════════════════════════════════
    #  HELPERS
    # ══════════════════════════════════════════════════════════════════
    @staticmethod
    def _sanitise_command(cmd: str) -> str:
        """
        Clean up a command string from UI input.

        - Strips leading/trailing whitespace
        - Auto-inserts missing space for NAI channel queries (e.g. API1AVG_ANGLE? -> API1 AVG_ANGLE?)
        - Removes literal '\\r' / '\\n' escape sequences that may have
          been typed into the textbox (the terminator is added at send time)
        """
        if not cmd:
            return ""
        cleaned = cmd.strip()
        # Remove any literal backslash-r / backslash-n typed by the user
        cleaned = cleaned.replace("\\r", "").replace("\\n", "")
        # Also remove actual CR/LF if someone pasted them
        cleaned = cleaned.replace("\r", "").replace("\n", "")

        # Auto-correct missing space in NAI channel commands (prevents NAI 'Space Error')
        match_nai = re.match(r"^(API\d+)([A-Za-z_].*)$", cleaned, re.IGNORECASE)
        if match_nai:
            cleaned = f"{match_nai.group(1).upper()} {match_nai.group(2).upper()}"

        return cleaned

    def _log_info(self, msg: str) -> None:
        """Send a verbose log message via the on_log callback."""
        if self.on_log:
            self.on_log(msg)

    # ── properties ───────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return self._running.is_set()


# ──────────────────────────────────────────────────────────────────────
#  EthernetReader – TCP/IP socket reader for the API 8810A
# ──────────────────────────────────────────────────────────────────────
class EthernetReader:
    """
    Manages a TCP/IP socket connection to the API 8810A over Ethernet (LAN).

    Protocol details:
      • Connects to target IP and port (default 23 / Telnet).
      • Performs NAI authentication handshake: sends "NAII\\r\\n".
      • Continuously queries angle commands (e.g. "API1 AVG_ANGLE?\\r\\n").
      • Receives ASCII angle string (e.g. "2.427\\r\\n").
    """

    def __init__(
        self,
        host: str = "192.168.1.21",
        port: int = 23,
        command: str = "API1 ANGLE?",
        interval: float = 0.03,
        on_data: Optional[Callable[[float], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        simulator: bool = False,
    ):
        self.host = host.strip()
        self.port = int(port)
        self.command = SerialReader._sanitise_command(command)
        self.interval = interval
        self.on_data = on_data
        self.on_error = on_error
        self.on_log = on_log
        self.simulator = simulator

        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()

    def connect(self) -> bool:
        """Open TCP/IP socket connection and begin polling. Returns True on success."""
        if self._running.is_set():
            return True

        if not self.simulator:
            try:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._socket.settimeout(3.0)
                self._log_info(f"Connecting to NAI 8810A at {self.host}:{self.port}...")
                self._socket.connect((self.host, self.port))

                # Step 1: Send NAI Ethernet login handshake
                self._socket.sendall(b"NAII\r\n")
                time.sleep(0.15)

                # Read optional login banner without blocking long
                try:
                    self._socket.settimeout(0.5)
                    banner = self._socket.recv(1024)
                    if banner:
                        banner_str = banner.decode("ascii", errors="ignore").strip()
                        self._log_info(f"NAI 8810A handshake acknowledged: '{banner_str}'")
                except socket.timeout:
                    pass
                finally:
                    self._socket.settimeout(2.0)

                self._log_info(f"Ethernet connection established: {self.host}:{self.port}")
            except Exception as exc:
                if self._socket:
                    try:
                        self._socket.close()
                    except Exception:
                        pass
                    self._socket = None
                if self.on_error:
                    self.on_error(f"Ethernet connection error ({self.host}:{self.port}): {exc}")
                return False

        self._running.set()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        return True

    def disconnect(self) -> None:
        """Stop polling and close the TCP socket."""
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None

    def _poll_loop(self) -> None:
        """Continuously send the query and parse responses."""
        sim_angle = random.uniform(0.0, 360.0)

        while self._running.is_set():
            try:
                if self.simulator:
                    value = SerialReader._simulate(sim_angle)
                    sim_angle = value
                else:
                    value = self._query_device()

                if value is not None:
                    if self.on_data:
                        self.on_data(value)
                else:
                    self._log_info("No data / timeout (waiting for 8810A reply)")

            except Exception as exc:
                if self.on_error:
                    self.on_error(f"Ethernet I/O error: {exc}")

            # Minimal sleep for real-time responsiveness without pegging CPU
            if self.interval > 0:
                waited = 0.0
                step = min(0.01, self.interval)
                while waited < self.interval and self._running.is_set():
                    time.sleep(step)
                    waited += step
            else:
                time.sleep(0.002)

    def _read_response(self, timeout: float = 0.8) -> bytes:
        """Read until newline or carriage return from TCP socket with zero artificial delays."""
        if self._socket is None:
            return b""
        self._socket.settimeout(timeout)
        start = time.time()
        buf = bytearray()
        while time.time() - start < timeout:
            try:
                chunk = self._socket.recv(128)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in buf or b"\r" in buf:
                    break
            except socket.timeout:
                break
            except Exception:
                break
        return bytes(buf)

    def _query_device(self) -> Optional[float]:
        """Send query to the API 8810A via TCP socket and parse reply."""
        with self._lock:
            if self._socket is None:
                return None
            try:
                cmd_clean = SerialReader._sanitise_command(self.command)
                wire_bytes = f"{cmd_clean}\r\n".encode("ascii", errors="ignore")
                self._socket.sendall(wire_bytes)
                raw_bytes = self._read_response(timeout=0.8)
            except Exception as exc:
                self._log_info(f"Socket write/read error: {exc}")
                return None

        if not raw_bytes:
            self._log_info(f"Command '{cmd_clean}': No reply (timeout)")
            return None

        raw_str = raw_bytes.decode("ascii", errors="ignore").strip()
        if not raw_str:
            return None

        return SerialReader._parse_angle(raw_str)

    def query_raw(self, cmd: str) -> str:
        """Send a one-shot query to the 8810A over the active socket."""
        with self._lock:
            if self._socket is None:
                return "[Socket not connected]"
            try:
                cmd_clean = SerialReader._sanitise_command(cmd)
                wire_bytes = f"{cmd_clean}\r\n".encode("ascii", errors="ignore")
                self._socket.sendall(wire_bytes)
                raw_bytes = self._read_response(timeout=2.0)
                if not raw_bytes:
                    return "[No reply / Timeout]"
                return raw_bytes.decode("ascii", errors="ignore").strip()
            except Exception as exc:
                return f"[Socket Error: {exc}]"

    @classmethod
    def test_single_query(cls, host: str, port: int, cmd: str, timeout: float = 2.5) -> str:
        """Standalone one-shot query test via TCP socket without continuous connection."""
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host.strip(), int(port)))

            # Step 1: NAI login handshake
            s.sendall(b"NAII\r\n")
            time.sleep(0.12)
            try:
                s.settimeout(0.4)
                s.recv(1024)
            except socket.timeout:
                pass

            # Step 2: Query command
            s.settimeout(timeout)
            cmd_clean = SerialReader._sanitise_command(cmd)
            wire_bytes = f"{cmd_clean}\r\n".encode("ascii", errors="ignore")
            s.sendall(wire_bytes)

            buf = bytearray()
            start = time.time()
            while time.time() - start < timeout:
                try:
                    chunk = s.recv(128)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if b"\n" in buf or b"\r" in buf:
                        break
                except socket.timeout:
                    break
                except Exception:
                    break
            return buf.decode("ascii", errors="ignore").strip() if buf else "[No reply / Timeout]"
        except Exception as exc:
            return f"[Socket Error: {exc}]"
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass

    @property
    def is_connected(self) -> bool:
        return self._running.is_set()

    def _log_info(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

