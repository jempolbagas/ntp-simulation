"""Cross-platform host OS clock discipline (Linux & Windows) via native kernel/Win32 APIs."""
from __future__ import annotations

import atexit
import ctypes
import errno as errno_mod
import math
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple

DEFAULT_STEP_THRESHOLD: float = 0.128  # RFC 5905 step threshold in seconds

LINUX_CAP_SYS_TIME: int = 25
LINUX_ADJ_OFFSET_SINGLESHOT: int = 0x8001
LINUX_ADJ_OFFSET_SS_READ: int = 0xA001
LINUX_COMPETING_SERVICES: Tuple[str, ...] = (
    "systemd-timesyncd",
    "chronyd",
    "ntpd",
    "ntp",
    "openntpd",
)

WINDOWS_SE_SYSTEMTIME_NAME: str = "SeSystemtimePrivilege"
WINDOWS_ERROR_NOT_ALL_ASSIGNED: int = 1300
WINDOWS_MAX_SLEW_PPM: float = 500.0


class OSClockError(RuntimeError):
    """Base error for startup validation and host OS clock adjustment failures."""


class OSClockPrivilegeError(OSClockError):
    """Raised when the process lacks OS privileges to modify the system clock."""


class OSClockConflictError(OSClockError):
    """Raised when a competing OS time-synchronization daemon is active."""


class OSClockUnsupportedError(OSClockError):
    """Raised when --sync-os-clock is used on an unsupported operating system."""


def _require_finite_delta(delta: float) -> float:
    val = float(delta)
    if not math.isfinite(val):
        raise OSClockError(f"Non-finite clock adjustment delta: {delta!r}")
    return val


class _Timeval(ctypes.Structure):
    """POSIX struct timeval for Linux struct timex."""

    _fields_ = [
        ("tv_sec", ctypes.c_long),
        ("tv_usec", ctypes.c_long),
    ]


class _Timex(ctypes.Structure):
    """Linux <linux/timex.h> struct timex layout."""

    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time", _Timeval),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("_pad", ctypes.c_int * 11),
    ]


class _LUID(ctypes.Structure):
    """Win32 LUID structure."""

    _fields_ = [
        ("LowPart", ctypes.c_uint32),
        ("HighPart", ctypes.c_int32),
    ]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    """Win32 LUID_AND_ATTRIBUTES structure."""

    _fields_ = [
        ("Luid", _LUID),
        ("Attributes", ctypes.c_uint32),
    ]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    """Win32 TOKEN_PRIVILEGES structure with one privilege entry."""

    _fields_ = [
        ("PrivilegeCount", ctypes.c_uint32),
        ("Privileges", _LUID_AND_ATTRIBUTES * 1),
    ]


class _SYSTEMTIME(ctypes.Structure):
    """Win32 SYSTEMTIME structure (UTC)."""

    _fields_ = [
        ("wYear", ctypes.c_uint16),
        ("wMonth", ctypes.c_uint16),
        ("wDayOfWeek", ctypes.c_uint16),
        ("wDay", ctypes.c_uint16),
        ("wHour", ctypes.c_uint16),
        ("wMinute", ctypes.c_uint16),
        ("wSecond", ctypes.c_uint16),
        ("wMilliseconds", ctypes.c_uint16),
    ]


class OSClockDriver(ABC):
    """Abstract platform driver for validating and adjusting the host OS clock."""

    @abstractmethod
    def validate_environment(self) -> None:
        """Verifies privileges and absence of competing time daemons (fail-fast)."""

    @abstractmethod
    def step(self, delta: float) -> None:
        """Immediately steps the OS wall clock by delta seconds."""

    @abstractmethod
    def start_slew(self, delta: float) -> None:
        """Starts a gradual, rate-limited OS clock slew of delta seconds."""

    @abstractmethod
    def get_remaining_slew(self) -> float:
        """Returns remaining unslewed offset in seconds (0.0 if idle)."""

    @abstractmethod
    def tick(self) -> None:
        """Polls/maintains any active slew state."""

    @abstractmethod
    def restore(self) -> None:
        """Safely restores OS clock adjustment settings on shutdown (idempotent)."""


class LinuxOSClockDriver(OSClockDriver):
    """Linux OS clock driver using clock_settime(CLOCK_REALTIME) and libc adjtimex(2)."""

    def __init__(
        self,
        libc: Optional[Any] = None,
        clock_settime_fn: Optional[Callable[[int, float], None]] = None,
        time_fn: Optional[Callable[[], float]] = None,
        geteuid_fn: Optional[Callable[[], int]] = None,
        proc_status_path: str = "/proc/self/status",
        run_cmd_fn: Optional[Callable[..., Any]] = None,
        which_fn: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        if libc is None:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self._libc = libc
        self._clock_settime_fn = clock_settime_fn or getattr(time, "clock_settime", None)
        self._time_fn = time_fn or time.time
        if geteuid_fn is None:
            import os

            geteuid_fn = getattr(os, "geteuid", lambda: -1)
        self._geteuid_fn = geteuid_fn
        self._proc_status_path = proc_status_path
        self._run_cmd_fn = run_cmd_fn
        self._which_fn = which_fn or shutil.which

    def has_privilege(self) -> bool:
        """Returns True if running as root or possessing effective CAP_SYS_TIME."""
        try:
            if self._geteuid_fn() == 0:
                return True
        except Exception:
            pass

        try:
            with open(self._proc_status_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("CapEff:"):
                        hex_mask = line.split(":", 1)[1].strip()
                        cap_eff = int(hex_mask, 16)
                        return bool(cap_eff & (1 << LINUX_CAP_SYS_TIME))
        except (OSError, ValueError):
            pass
        return False

    def detect_competing_daemons(self) -> List[str]:
        """Detects active Linux NTP/time daemons that would conflict with clock discipline."""
        run_fn = self._run_cmd_fn
        if run_fn is None:
            if not self._which_fn("systemctl"):
                return []
            run_fn = subprocess.run

        active: List[str] = []
        for svc in LINUX_COMPETING_SERVICES:
            try:
                res = run_fn(
                    ["systemctl", "is-active", svc],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
                if getattr(res, "stdout", "").strip() == "active":
                    active.append(svc)
            except (subprocess.SubprocessError, OSError):
                continue
        return active

    def validate_environment(self) -> None:
        """Fails fast if lacking CAP_SYS_TIME/root or if a competing time daemon is active."""
        if not self.has_privilege():
            raise OSClockPrivilegeError(
                "Insufficient privileges to discipline the Linux OS clock. "
                "Run with sudo/root or grant CAP_SYS_TIME: "
                "sudo setcap cap_sys_time+ep $(which python3)"
            )

        active = self.detect_competing_daemons()
        if active:
            svc_list = ", ".join(active)
            stop_cmds = " ".join(active)
            raise OSClockConflictError(
                f"Competing OS time daemon(s) active: {svc_list}. "
                f"Stop them first to avoid clock conflicts: sudo systemctl stop {stop_cmds}"
            )

    def _call_adjtimex(self, tx: _Timex) -> Tuple[int, int]:
        ctypes.set_errno(0)
        rc = int(self._libc.adjtimex(ctypes.byref(tx)))
        err = ctypes.get_errno()
        return rc, err

    def step(self, delta: float) -> None:
        """Cancels any pending single-shot slew and steps CLOCK_REALTIME by delta seconds."""
        d = _require_finite_delta(delta)
        tx = _Timex()
        tx.modes = LINUX_ADJ_OFFSET_SINGLESHOT
        tx.offset = 0
        rc, err = self._call_adjtimex(tx)
        if rc < 0 and err not in (0, errno_mod.EPERM):
            raise OSClockError(f"adjtimex(ADJ_OFFSET_SINGLESHOT=0) failed: errno={err}")

        if self._clock_settime_fn is None:
            raise OSClockError("time.clock_settime is unavailable on this Python build")

        clock_realtime = getattr(time, "CLOCK_REALTIME", 0)
        target_ts = float(self._time_fn()) + d
        try:
            self._clock_settime_fn(clock_realtime, target_ts)
        except (PermissionError, OSError) as exc:
            raise OSClockError(f"clock_settime(CLOCK_REALTIME) failed: {exc}") from exc

    def start_slew(self, delta: float) -> None:
        """Initiates a kernel single-shot adjtime slew of delta seconds."""
        d = _require_finite_delta(delta)
        offset_us = int(round(d * 1_000_000.0))
        tx = _Timex()
        tx.modes = LINUX_ADJ_OFFSET_SINGLESHOT
        tx.offset = offset_us
        rc, err = self._call_adjtimex(tx)
        if rc < 0:
            raise OSClockError(f"adjtimex(ADJ_OFFSET_SINGLESHOT) failed: errno={err}")

    def get_remaining_slew(self) -> float:
        """Queries remaining kernel single-shot adjtime offset in seconds."""
        tx = _Timex()
        tx.modes = LINUX_ADJ_OFFSET_SS_READ
        rc, _ = self._call_adjtimex(tx)
        if rc < 0:
            return 0.0
        return float(tx.offset) / 1_000_000.0

    def tick(self) -> None:
        """No-op on Linux; kernel autonomously steps down single-shot offset."""

    def restore(self) -> None:
        """No-op on Linux; single-shot adjtime preserves clean kernel PLL state."""


class WindowsOSClockDriver(OSClockDriver):
    """Windows OS clock driver using SetSystemTime and SetSystemTimeAdjustment."""

    def __init__(
        self,
        kernel32: Optional[Any] = None,
        advapi32: Optional[Any] = None,
        time_fn: Optional[Callable[[], float]] = None,
        monotonic_fn: Optional[Callable[[], float]] = None,
        run_cmd_fn: Optional[Callable[..., Any]] = None,
        get_last_error_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        if kernel32 is None or advapi32 is None:
            windll_cls = getattr(ctypes, "WinDLL", None)
            if windll_cls is None:
                raise OSClockUnsupportedError(
                    "Windows WinDLL interface is unavailable on this platform"
                )
            if kernel32 is None:
                kernel32 = windll_cls("kernel32", use_last_error=True)
                kernel32.GetCurrentProcess.argtypes = []
                kernel32.GetCurrentProcess.restype = ctypes.c_void_p
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle.restype = ctypes.c_int
                kernel32.SetSystemTime.argtypes = [ctypes.POINTER(_SYSTEMTIME)]
                kernel32.SetSystemTime.restype = ctypes.c_int
                kernel32.GetSystemTimeAdjustment.argtypes = [
                    ctypes.POINTER(ctypes.c_uint32),
                    ctypes.POINTER(ctypes.c_uint32),
                    ctypes.POINTER(ctypes.c_int),
                ]
                kernel32.GetSystemTimeAdjustment.restype = ctypes.c_int
                kernel32.SetSystemTimeAdjustment.argtypes = [ctypes.c_uint32, ctypes.c_int]
                kernel32.SetSystemTimeAdjustment.restype = ctypes.c_int
            if advapi32 is None:
                advapi32 = windll_cls("advapi32", use_last_error=True)
                advapi32.OpenProcessToken.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_void_p),
                ]
                advapi32.OpenProcessToken.restype = ctypes.c_int
                advapi32.LookupPrivilegeValueW.argtypes = [
                    ctypes.c_wchar_p,
                    ctypes.c_wchar_p,
                    ctypes.POINTER(_LUID),
                ]
                advapi32.LookupPrivilegeValueW.restype = ctypes.c_int
                advapi32.AdjustTokenPrivileges.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.POINTER(_TOKEN_PRIVILEGES),
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ]
                advapi32.AdjustTokenPrivileges.restype = ctypes.c_int

        self._kernel32 = kernel32
        self._advapi32 = advapi32
        self._time_fn = time_fn or time.time
        self._monotonic_fn = monotonic_fn or time.monotonic
        self._run_cmd_fn = run_cmd_fn or subprocess.run
        self._get_last_error_fn = get_last_error_fn or getattr(ctypes, "get_last_error", lambda: 0)

        self._slew_active: bool = False
        self._slew_sign: float = 0.0
        self._slew_rate: float = 0.0
        self._slew_end_mono: float = 0.0

    def enable_systemtime_privilege(self) -> None:
        """Enables SeSystemtimePrivilege in the current process token."""
        token = ctypes.c_void_p()
        h_proc = self._kernel32.GetCurrentProcess()
        token_access = 0x0028  # TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY
        if not self._advapi32.OpenProcessToken(h_proc, token_access, ctypes.byref(token)):
            raise OSClockPrivilegeError(
                "Failed to open process token for SeSystemtimePrivilege. "
                "Run from an elevated Administrator terminal."
            )

        try:
            luid = _LUID()
            if not self._advapi32.LookupPrivilegeValueW(
                None, WINDOWS_SE_SYSTEMTIME_NAME, ctypes.byref(luid)
            ):
                raise OSClockPrivilegeError(
                    "Failed to look up SeSystemtimePrivilege. "
                    "Run from an elevated Administrator terminal."
                )

            tp = _TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = 0x00000002  # SE_PRIVILEGE_ENABLED

            ok = self._advapi32.AdjustTokenPrivileges(
                token, False, ctypes.byref(tp), 0, None, None
            )
            last_err = int(self._get_last_error_fn())
            if not ok or last_err == WINDOWS_ERROR_NOT_ALL_ASSIGNED:
                raise OSClockPrivilegeError(
                    "SeSystemtimePrivilege not available. "
                    "Run from an elevated Administrator terminal."
                )
        finally:
            self._kernel32.CloseHandle(token)

    def detect_competing_daemons(self) -> List[str]:
        """Detects whether the Windows Time service (w32time) is running."""
        try:
            res = self._run_cmd_fn(
                ["sc", "query", "w32time"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            if getattr(res, "returncode", 1) == 0 and "RUNNING" in getattr(res, "stdout", "").upper():
                return ["w32time"]
        except (subprocess.SubprocessError, OSError):
            pass
        return []

    def validate_environment(self) -> None:
        """Fails fast if lacking SeSystemtimePrivilege or if w32time is active."""
        self.enable_systemtime_privilege()
        active = self.detect_competing_daemons()
        if active:
            raise OSClockConflictError(
                "Competing Windows Time service (w32time) is running. "
                "Stop it first with: net stop w32time"
            )

    def step(self, delta: float) -> None:
        """Restores normal tick rate and steps the UTC Windows system clock."""
        d = _require_finite_delta(delta)
        self.restore()
        target_ts = float(self._time_fn()) + d
        try:
            dt = datetime.fromtimestamp(target_ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError) as exc:
            raise OSClockError(f"Target timestamp out of range for SetSystemTime: {target_ts}") from exc

        st = _SYSTEMTIME()
        st.wYear = dt.year
        st.wMonth = dt.month
        st.wDayOfWeek = (dt.weekday() + 1) % 7  # Win32: Sunday=0 .. Saturday=6
        st.wDay = dt.day
        st.wHour = dt.hour
        st.wMinute = dt.minute
        st.wSecond = dt.second
        st.wMilliseconds = dt.microsecond // 1000

        if not self._kernel32.SetSystemTime(ctypes.byref(st)):
            err = int(self._get_last_error_fn())
            raise OSClockError(f"SetSystemTime failed: GetLastError={err}")

    def start_slew(self, delta: float) -> None:
        """Slews the Windows system clock smoothly via SetSystemTimeAdjustment."""
        d = _require_finite_delta(delta)
        if abs(d) < 1e-6:
            self.restore()
            return

        adj = ctypes.c_uint32()
        inc = ctypes.c_uint32()
        disabled = ctypes.c_int()
        if not self._kernel32.GetSystemTimeAdjustment(
            ctypes.byref(adj), ctypes.byref(inc), ctypes.byref(disabled)
        ):
            err = int(self._get_last_error_fn())
            raise OSClockError(f"GetSystemTimeAdjustment failed: GetLastError={err}")

        if inc.value == 0:
            raise OSClockError("GetSystemTimeAdjustment returned zero time increment")

        ppm = WINDOWS_MAX_SLEW_PPM * 1e-6
        delta_inc = max(1, int(round(inc.value * ppm)))
        signed_delta_inc = delta_inc if d > 0 else -delta_inc
        target_adj = max(1, inc.value + signed_delta_inc)

        actual_rate = abs(target_adj - inc.value) / float(inc.value)
        if actual_rate <= 0.0:
            raise OSClockError("Computed zero Windows slew rate")

        duration_s = abs(d) / actual_rate
        if not self._kernel32.SetSystemTimeAdjustment(target_adj, False):
            err = int(self._get_last_error_fn())
            raise OSClockError(f"SetSystemTimeAdjustment failed: GetLastError={err}")

        self._slew_active = True
        self._slew_sign = 1.0 if d > 0 else -1.0
        self._slew_rate = actual_rate
        self._slew_end_mono = float(self._monotonic_fn()) + duration_s

    def get_remaining_slew(self) -> float:
        """Returns remaining slew offset in seconds, restoring default rate when done."""
        if not self._slew_active:
            return 0.0
        rem_time = self._slew_end_mono - float(self._monotonic_fn())
        if rem_time <= 0.0:
            self.restore()
            return 0.0
        return self._slew_sign * rem_time * self._slew_rate

    def tick(self) -> None:
        """Checks monotonic slew deadline and restores Windows tick rate upon completion."""
        self.get_remaining_slew()

    def restore(self) -> None:
        """Disables manual tick adjustment so Windows resumes its default tick rate."""
        if self._slew_active:
            self._kernel32.SetSystemTimeAdjustment(0, True)
            self._slew_active = False


def create_os_clock_driver(platform_name: Optional[str] = None) -> OSClockDriver:
    """Factory creating the OSClockDriver for the host OS (Linux or Windows)."""
    plat = (platform_name or sys.platform).lower()
    if plat.startswith("linux"):
        return LinuxOSClockDriver()
    if plat in ("win32", "cygwin"):
        return WindowsOSClockDriver()
    raise OSClockUnsupportedError(
        f"Host OS '{plat}' is not supported for --sync-os-clock (supported: Linux, Windows)"
    )


class SystemClock:
    """Thread-safe host OS clock controller duck-type compatible with VirtualClock."""

    def __init__(
        self,
        driver: Optional[OSClockDriver] = None,
        validate: bool = True,
    ) -> None:
        self._driver: OSClockDriver = driver if driver is not None else create_os_clock_driver()
        self._lock: threading.Lock = threading.Lock()
        self._slew_total_target: float = 0.0
        self._slew_remaining: float = 0.0
        self._closed: bool = False

        if validate:
            self._driver.validate_environment()

        atexit.register(self.close)

    def now(self) -> float:
        """Returns current live host OS wall-clock timestamp."""
        return time.time()

    def get_offset(self) -> float:
        """Returns 0.0 because the OS clock itself is the disciplined clock."""
        return 0.0

    def step(self, delta: float) -> None:
        """Immediately steps the host OS clock by delta seconds and clears active slew."""
        with self._lock:
            self._driver.step(float(delta))
            self._slew_remaining = 0.0
            self._slew_total_target = 0.0

    def start_slew(self, delta: float) -> None:
        """Initiates a smooth rate-limited slew on the host OS clock."""
        with self._lock:
            d = float(delta)
            self._driver.start_slew(d)
            if abs(d) < 1e-6:
                self._slew_remaining = 0.0
                self._slew_total_target = 0.0
                return
            if self._slew_remaining * d <= 0.0 or abs(d) > abs(self._slew_total_target):
                self._slew_total_target = d
            self._slew_remaining = d

    def tick(self, now_real: Optional[float] = None, dt: Optional[float] = None) -> None:
        """Updates OS slew progress and clears completed slew targets."""
        with self._lock:
            self._driver.tick()
            rem = self._driver.get_remaining_slew()
            if abs(rem) < 1e-6:
                self._slew_remaining = 0.0
                self._slew_total_target = 0.0
            else:
                self._slew_remaining = rem
                if self._slew_total_target == 0.0:
                    self._slew_total_target = rem

    def get_slew_info(self) -> Tuple[float, float, float]:
        """Returns slewing progress tuple: (remaining_slew, total_target_slew, progress_ratio)."""
        with self._lock:
            if self._slew_total_target == 0.0:
                return (0.0, 0.0, 1.0)
            completed = abs(self._slew_total_target) - abs(self._slew_remaining)
            ratio = max(0.0, min(1.0, completed / max(0.0001, abs(self._slew_total_target))))
            return (self._slew_remaining, self._slew_total_target, ratio)

    def get_drift_rate(self) -> float:
        """Returns 0.0 (simulated Brownian drift is disabled in OS clock mode)."""
        return 0.0

    def is_drift_active(self) -> bool:
        """Returns False (simulated Brownian drift is disabled in OS clock mode)."""
        return False

    def close(self) -> None:
        """Restores OS clock adjustment settings cleanly on shutdown (idempotent)."""
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._driver.restore()
                finally:
                    try:
                        atexit.unregister(self.close)
                    except Exception:
                        pass

    def __repr__(self) -> str:
        with self._lock:
            return f"SystemClock(driver={type(self._driver).__name__}, slew_remaining={self._slew_remaining:+.6f}s)"
