"""Unit tests for cross-platform host OS clock discipline (--sync-os-clock) and AUTO mode."""
from __future__ import annotations

import ctypes
import io
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.console import Console

import ntp_simulation.cli as cli_mod
from ntp_simulation.clock import VirtualClock
from ntp_simulation.models import NodeConfig, PeerAddress, PeerSample
from ntp_simulation.os_clock import (
    DEFAULT_STEP_THRESHOLD,
    LINUX_ADJ_OFFSET_SINGLESHOT,
    LINUX_ADJ_OFFSET_SS_READ,
    LINUX_CAP_SYS_TIME,
    WINDOWS_ERROR_NOT_ALL_ASSIGNED,
    LinuxOSClockDriver,
    OSClockConflictError,
    OSClockError,
    OSClockPrivilegeError,
    OSClockUnsupportedError,
    SystemClock,
    WindowsOSClockDriver,
    _SYSTEMTIME,
    _Timex,
    create_os_clock_driver,
)
from ntp_simulation.state import SharedState
from ntp_simulation.ui import build_dashboard


class TestPlatformDispatch:
    def test_linux_dispatch(self, monkeypatch):
        monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: MagicMock())
        drv = create_os_clock_driver("linux")
        assert isinstance(drv, LinuxOSClockDriver)

    def test_windows_dispatch(self, monkeypatch):
        dlls = {"kernel32": MagicMock(), "advapi32": MagicMock()}
        monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: dlls[name], raising=False)
        drv = create_os_clock_driver("win32")
        assert isinstance(drv, WindowsOSClockDriver)
        assert dlls["kernel32"].GetCurrentProcess.restype == ctypes.c_void_p
        assert dlls["advapi32"].OpenProcessToken.argtypes[0] == ctypes.c_void_p

    @pytest.mark.parametrize("plat", ["darwin", "freebsd14", "sunos5"])
    def test_unsupported_os_raises(self, plat):
        with pytest.raises(OSClockUnsupportedError):
            create_os_clock_driver(plat)


class TestLinuxOSClockDriver:
    def test_has_privilege_root(self, tmp_path):
        status_file = tmp_path / "status"
        status_file.write_text("CapEff:\t0000000000000000\n", encoding="utf-8")
        drv = LinuxOSClockDriver(
            libc=MagicMock(),
            geteuid_fn=lambda: 0,
            proc_status_path=str(status_file),
        )
        assert drv.has_privilege() is True

    def test_has_privilege_cap_sys_time(self, tmp_path):
        cap_mask = 1 << LINUX_CAP_SYS_TIME
        status_file = tmp_path / "status"
        status_file.write_text(f"Name:\tpython3\nCapEff:\t{cap_mask:016x}\n", encoding="utf-8")
        drv = LinuxOSClockDriver(
            libc=MagicMock(),
            geteuid_fn=lambda: 1000,
            proc_status_path=str(status_file),
        )
        assert drv.has_privilege() is True

    def test_has_privilege_unprivileged(self, tmp_path):
        status_file = tmp_path / "status"
        status_file.write_text("CapEff:\t0000000000000000\n", encoding="utf-8")
        drv = LinuxOSClockDriver(
            libc=MagicMock(),
            geteuid_fn=lambda: 1000,
            proc_status_path=str(status_file),
        )
        assert drv.has_privilege() is False

    def test_detect_competing_daemons(self):
        def fake_run(cmd, **kwargs):
            svc = cmd[-1]
            out = "active\n" if svc in ("chronyd", "systemd-timesyncd") else "inactive\n"
            return SimpleNamespace(returncode=0 if out == "active\n" else 3, stdout=out)

        drv = LinuxOSClockDriver(libc=MagicMock(), run_cmd_fn=fake_run)
        assert drv.detect_competing_daemons() == ["systemd-timesyncd", "chronyd"]

    def test_detect_competing_daemons_all_inactive_or_no_systemctl(self):
        drv_no_ctl = LinuxOSClockDriver(libc=MagicMock(), which_fn=lambda _: None)
        assert drv_no_ctl.detect_competing_daemons() == []

        drv_inactive = LinuxOSClockDriver(
            libc=MagicMock(),
            run_cmd_fn=lambda cmd, **kw: SimpleNamespace(returncode=3, stdout="inactive\n"),
        )
        assert drv_inactive.detect_competing_daemons() == []

    def test_validate_environment_fail_fast(self, tmp_path):
        status_file = tmp_path / "status"
        status_file.write_text("CapEff:\t0000000000000000\n", encoding="utf-8")

        unpriv = LinuxOSClockDriver(
            libc=MagicMock(),
            geteuid_fn=lambda: 1000,
            proc_status_path=str(status_file),
            run_cmd_fn=lambda *a, **kw: SimpleNamespace(returncode=3, stdout="inactive\n"),
        )
        with pytest.raises(OSClockPrivilegeError):
            unpriv.validate_environment()

        conflict = LinuxOSClockDriver(
            libc=MagicMock(),
            geteuid_fn=lambda: 0,
            proc_status_path=str(status_file),
            run_cmd_fn=lambda cmd, **kw: SimpleNamespace(
                returncode=0 if cmd[-1] == "chronyd" else 3,
                stdout="active\n" if cmd[-1] == "chronyd" else "inactive\n",
            ),
        )
        with pytest.raises(OSClockConflictError, match="chronyd"):
            conflict.validate_environment()

    def test_step_cancels_slew_and_calls_clock_settime(self):
        recorded_timex = []
        settime_calls = []

        class FakeLibc:
            def adjtimex(self, ptr):
                tx = ctypes.cast(ptr, ctypes.POINTER(_Timex)).contents
                recorded_timex.append((tx.modes, tx.offset))
                return 0

        drv = LinuxOSClockDriver(
            libc=FakeLibc(),
            clock_settime_fn=lambda clk_id, ts: settime_calls.append((clk_id, ts)),
            time_fn=lambda: 1700000000.5,
        )
        drv.step(2.25)

        assert recorded_timex == [(LINUX_ADJ_OFFSET_SINGLESHOT, 0)]
        assert len(settime_calls) == 1
        assert settime_calls[0][1] == pytest.approx(1700000002.75)

    def test_start_slew_and_get_remaining_slew(self):
        state = {"offset": 0, "modes": []}

        class FakeLibc:
            def adjtimex(self, ptr):
                tx = ctypes.cast(ptr, ctypes.POINTER(_Timex)).contents
                state["modes"].append(tx.modes)
                if tx.modes == LINUX_ADJ_OFFSET_SINGLESHOT:
                    state["offset"] = tx.offset
                elif tx.modes == LINUX_ADJ_OFFSET_SS_READ:
                    tx.offset = state["offset"]
                return 0

        drv = LinuxOSClockDriver(libc=FakeLibc())
        drv.start_slew(-0.075)
        assert state["offset"] == -75_000
        assert drv.get_remaining_slew() == pytest.approx(-0.075)
        assert state["modes"] == [LINUX_ADJ_OFFSET_SINGLESHOT, LINUX_ADJ_OFFSET_SS_READ]

    def test_errors_wrapped_in_os_clock_error(self):
        class FailingLibc:
            def adjtimex(self, ptr):
                ctypes.set_errno(1)
                return -1

        drv = LinuxOSClockDriver(libc=FailingLibc())
        with pytest.raises(OSClockError, match="adjtimex"):
            drv.start_slew(0.05)

        class OkLibc:
            def adjtimex(self, ptr):
                return 0

        def raise_eperm(clk, ts):
            raise PermissionError("Operation not permitted")

        drv2 = LinuxOSClockDriver(libc=OkLibc(), clock_settime_fn=raise_eperm, time_fn=lambda: 100.0)
        with pytest.raises(OSClockError, match="clock_settime"):
            drv2.step(1.0)

    @pytest.mark.parametrize("bad_delta", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_delta_raises(self, bad_delta):
        drv = LinuxOSClockDriver(libc=MagicMock(), clock_settime_fn=MagicMock(), time_fn=lambda: 100.0)
        with pytest.raises(OSClockError, match="Non-finite"):
            drv.step(bad_delta)
        with pytest.raises(OSClockError, match="Non-finite"):
            drv.start_slew(bad_delta)


class TestWindowsOSClockDriver:
    def _make_win_mocks(self, last_error=0, adj_ok=1, inc_value=156250):
        calls = {"closed": [], "set_system_time": [], "set_time_adj": []}

        class FakeKernel32:
            def GetCurrentProcess(self):
                return 0x1234

            def CloseHandle(self, handle):
                calls["closed"].append(handle)
                return 1

            def SetSystemTime(self, ptr):
                st = ctypes.cast(ptr, ctypes.POINTER(_SYSTEMTIME)).contents
                calls["set_system_time"].append(
                    (
                        st.wYear,
                        st.wMonth,
                        st.wDayOfWeek,
                        st.wDay,
                        st.wHour,
                        st.wMinute,
                        st.wSecond,
                        st.wMilliseconds,
                    )
                )
                return 1

            def GetSystemTimeAdjustment(self, p_adj, p_inc, p_disabled):
                ctypes.cast(p_adj, ctypes.POINTER(ctypes.c_uint32)).contents.value = inc_value
                ctypes.cast(p_inc, ctypes.POINTER(ctypes.c_uint32)).contents.value = inc_value
                ctypes.cast(p_disabled, ctypes.POINTER(ctypes.c_int)).contents.value = 1
                return 1

            def SetSystemTimeAdjustment(self, adj, disabled):
                calls["set_time_adj"].append((int(adj), bool(disabled)))
                return 1

        class FakeAdvapi32:
            def OpenProcessToken(self, proc, access, p_token):
                ctypes.cast(p_token, ctypes.POINTER(ctypes.c_void_p)).contents.value = 0xABCD
                return 1

            def LookupPrivilegeValueW(self, sys_name, name, p_luid):
                return 1

            def AdjustTokenPrivileges(self, token, disable_all, p_tp, buf_len, prev, ret_len):
                return adj_ok

        return FakeKernel32(), FakeAdvapi32(), calls

    def test_enable_systemtime_privilege_success_and_close_handle(self):
        k32, adv32, calls = self._make_win_mocks(last_error=0)
        drv = WindowsOSClockDriver(
            kernel32=k32,
            advapi32=adv32,
            get_last_error_fn=lambda: 0,
        )
        drv.enable_systemtime_privilege()
        assert len(calls["closed"]) == 1

    def test_enable_systemtime_privilege_error_not_all_assigned(self):
        k32, adv32, calls = self._make_win_mocks(last_error=WINDOWS_ERROR_NOT_ALL_ASSIGNED)
        drv = WindowsOSClockDriver(
            kernel32=k32,
            advapi32=adv32,
            get_last_error_fn=lambda: WINDOWS_ERROR_NOT_ALL_ASSIGNED,
        )
        with pytest.raises(OSClockPrivilegeError, match="SeSystemtimePrivilege"):
            drv.enable_systemtime_privilege()
        assert len(calls["closed"]) == 1

    def test_detect_competing_daemons_w32time(self):
        k32, adv32, _ = self._make_win_mocks()
        drv_running = WindowsOSClockDriver(
            kernel32=k32,
            advapi32=adv32,
            get_last_error_fn=lambda: 0,
            run_cmd_fn=lambda *a, **kw: SimpleNamespace(
                returncode=0, stdout="SERVICE_NAME: w32time\n        STATE              : 4  RUNNING\n"
            ),
        )
        with pytest.raises(OSClockConflictError, match="w32time"):
            drv_running.validate_environment()

        drv_stopped = WindowsOSClockDriver(
            kernel32=k32,
            advapi32=adv32,
            get_last_error_fn=lambda: 0,
            run_cmd_fn=lambda *a, **kw: SimpleNamespace(
                returncode=0, stdout="SERVICE_NAME: w32time\n        STATE              : 1  STOPPED\n"
            ),
        )
        drv_stopped.validate_environment()

    def test_step_populates_utc_systemtime_and_restores_slew(self):
        k32, adv32, calls = self._make_win_mocks()
        drv = WindowsOSClockDriver(
            kernel32=k32,
            advapi32=adv32,
            time_fn=lambda: 1700000000.0,
            monotonic_fn=lambda: 10.0,
            get_last_error_fn=lambda: 0,
        )
        drv.start_slew(0.05)
        drv.step(0.250)

        assert calls["set_time_adj"][-1] == (0, True)
        assert calls["set_system_time"] == [(2023, 11, 2, 14, 22, 13, 20, 250)]

    def test_start_slew_progress_and_restore(self):
        k32, adv32, calls = self._make_win_mocks(inc_value=156250)
        mono = {"t": 100.0}
        drv = WindowsOSClockDriver(
            kernel32=k32,
            advapi32=adv32,
            monotonic_fn=lambda: mono["t"],
            get_last_error_fn=lambda: 0,
        )

        drv.start_slew(0.050)
        adj, disabled = calls["set_time_adj"][-1]
        assert adj > 156250
        assert disabled is False
        assert drv.get_remaining_slew() == pytest.approx(0.050, abs=1e-4)

        mono["t"] += 50.0
        rem_mid = drv.get_remaining_slew()
        assert 0.0 < rem_mid < 0.050

        mono["t"] += 200.0
        drv.tick()
        assert drv.get_remaining_slew() == 0.0
        assert calls["set_time_adj"][-1] == (0, True)

        drv.start_slew(-0.020)
        adj_neg, disabled_neg = calls["set_time_adj"][-1]
        assert adj_neg < 156250
        assert disabled_neg is False
        assert drv.get_remaining_slew() < 0.0
        drv.restore()
        assert calls["set_time_adj"][-1] == (0, True)
        assert drv.get_remaining_slew() == 0.0

    @pytest.mark.parametrize("bad_delta", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_delta_raises(self, bad_delta):
        k32, adv32, _ = self._make_win_mocks()
        drv = WindowsOSClockDriver(
            kernel32=k32,
            advapi32=adv32,
            time_fn=lambda: 1700000000.0,
            get_last_error_fn=lambda: 0,
        )
        with pytest.raises(OSClockError, match="Non-finite"):
            drv.step(bad_delta)
        with pytest.raises(OSClockError, match="Non-finite"):
            drv.start_slew(bad_delta)

    def test_out_of_range_timestamp_raises_os_clock_error(self):
        k32, adv32, _ = self._make_win_mocks()
        drv = WindowsOSClockDriver(
            kernel32=k32,
            advapi32=adv32,
            time_fn=lambda: 1700000000.0,
            get_last_error_fn=lambda: 0,
        )
        with pytest.raises(OSClockError, match="out of range"):
            drv.step(1e20)


class TestAutoModeAndCLIIntegration:
    def test_parse_args_sync_os_clock_defaults_to_auto(self):
        args_default = cli_mod.parse_args(["--stratum", "3", "--upstream", "127.0.0.1:5001"])
        assert args_default.sync_os_clock is False
        assert args_default.sync_mode == "STEP"
        assert args_default.step_threshold == pytest.approx(DEFAULT_STEP_THRESHOLD)

        args_os = cli_mod.parse_args(
            ["--stratum", "3", "--upstream", "127.0.0.1:5001", "--sync-os-clock"]
        )
        assert args_os.sync_os_clock is True
        assert args_os.sync_mode == "AUTO"

        args_override = cli_mod.parse_args(
            [
                "--stratum",
                "3",
                "--upstream",
                "127.0.0.1:5001",
                "--sync-os-clock",
                "--sync-mode",
                "SLEW",
                "--step-threshold",
                "0.25",
            ]
        )
        assert args_override.sync_mode == "SLEW"
        assert args_override.step_threshold == pytest.approx(0.25)

    def test_node_config_auto_mode_and_os_clock_default(self):
        cfg_sim = NodeConfig(stratum=1)
        assert cfg_sim.sync_mode == "STEP"
        assert cfg_sim.sync_os_clock is False

        cfg_auto = NodeConfig(stratum=1, sync_mode="AUTO")
        assert cfg_auto.sync_mode == "AUTO"

        cfg_os = NodeConfig(stratum=1, sync_os_clock=True)
        assert cfg_os.sync_mode == "AUTO"

        cfg_os_explicit = NodeConfig(stratum=1, sync_os_clock=True, sync_mode="STEP")
        assert cfg_os_explicit.sync_mode == "STEP"

    def test_shared_state_cycles_step_slew_auto(self):
        state = SharedState(NodeConfig(stratum=1, sync_mode="STEP"))
        assert state.toggle_sync_mode() == "SLEW"
        assert state.toggle_sync_mode() == "AUTO"
        assert state.toggle_sync_mode() == "STEP"

    def test_client_worker_auto_mode_selects_step_or_slew(self, monkeypatch):
        def run_one_poll(theta_val: float, threshold: float = 0.128):
            fake_drv = MagicMock()
            fake_drv.get_remaining_slew.return_value = 0.0
            sys_clk = SystemClock(driver=fake_drv, validate=False)
            try:
                cfg = NodeConfig(
                    stratum=3,
                    port=5009,
                    upstreams=[PeerAddress(host="127.0.0.1", port=5001)],
                    sync_os_clock=True,
                    sync_mode="AUTO",
                    step_threshold=threshold,
                )
                state = SharedState(cfg)
                stop_event = threading.Event()

                sample = PeerSample(
                    peer=PeerAddress(host="127.0.0.1", port=5001),
                    theta=theta_val,
                    delay=0.004,
                    server_stratum=1,
                    status="RESPONDED",
                )
                monkeypatch.setattr(
                    cli_mod,
                    "poll_peers_concurrently",
                    lambda *a, **kw: {"127.0.0.1:5001": sample},
                )

                orig_wait = stop_event.wait
                monkeypatch.setattr(stop_event, "wait", lambda timeout=None: stop_event.set() or True)

                cli_mod.client_worker(sys_clk, state, interval=0.01, stop_event=stop_event)
                return fake_drv, state
            finally:
                sys_clk.close()

        drv_step, st_step = run_one_poll(0.250)
        drv_step.step.assert_called_once_with(pytest.approx(0.250))
        drv_step.start_slew.assert_not_called()
        assert st_step.sync_history[0].mode == "STEP"
        assert "[AUTO -> STEP]" in st_step.last_action_banner

        drv_slew, st_slew = run_one_poll(-0.050)
        drv_slew.start_slew.assert_called_once_with(pytest.approx(-0.050))
        drv_slew.step.assert_not_called()
        assert st_slew.sync_history[0].mode == "SLEW"
        assert "[AUTO -> SLEW]" in st_slew.last_action_banner

    def test_main_fail_fast_when_os_clock_validation_fails(self, monkeypatch, capsys):
        def failing_sys_clock(*args, **kwargs):
            raise OSClockPrivilegeError("No CAP_SYS_TIME")

        monkeypatch.setattr(cli_mod, "SystemClock", failing_sys_clock)
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(
                [
                    "--stratum",
                    "3",
                    "--upstream",
                    "127.0.0.1:5001",
                    "--sync-os-clock",
                    "--no-ui",
                ]
            )
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "No CAP_SYS_TIME" in err

    def test_main_ignores_simulation_flags_in_os_clock_mode(self, monkeypatch):
        fake_drv = MagicMock()
        fake_drv.get_remaining_slew.return_value = 0.0
        created = {}

        def fake_system_clock(validate=True):
            assert validate is True
            clk = SystemClock(driver=fake_drv, validate=False)
            created["clock"] = clk
            return clk

        class CapturingSharedState(SharedState):
            def __init__(self, config):
                super().__init__(config)
                created["state"] = self

        monkeypatch.setattr(cli_mod, "SystemClock", fake_system_clock)
        monkeypatch.setattr(cli_mod, "SharedState", CapturingSharedState)
        monkeypatch.setattr(cli_mod, "run_server", lambda *a, **kw: None)
        monkeypatch.setattr(cli_mod, "client_worker", lambda *a, **kw: None)

        orig_wait = threading.Event.wait

        def stop_immediately(self, timeout=None):
            self.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", stop_immediately)

        cli_mod.main(
            [
                "--stratum",
                "3",
                "--upstream",
                "127.0.0.1:5001",
                "--sync-os-clock",
                "--no-ui",
                "--initial-offset",
                "42.0",
                "--drift",
                "--falseticker",
                "--chaos",
            ]
        )

        st = created["state"]
        clk = created["clock"]
        assert st.sync_os_clock is True
        assert st.sync_mode == "AUTO"
        assert st.falseticker_mode is False
        assert st.chaos_mode is False
        assert clk.is_drift_active() is False
        assert clk.get_offset() == 0.0
        fake_drv.restore.assert_called()

    def test_key_listener_ignores_sim_hotkeys_and_cycles_mode(self):
        fake_drv = MagicMock()
        fake_drv.get_remaining_slew.return_value = 0.0
        clk = SystemClock(driver=fake_drv, validate=False)
        try:
            state = SharedState(
                NodeConfig(
                    stratum=3,
                    upstreams=["127.0.0.1:5001"],
                    sync_os_clock=True,
                )
            )
            keys = iter(["f", "c", "d", "m", "q"])
            fake_reader = MagicMock()
            fake_reader.read_key.side_effect = lambda timeout=0.1: next(keys, None)
            stop_event = threading.Event()

            cli_mod.key_listener_worker(fake_reader, state, clk, stop_event)

            assert state.falseticker_mode is False
            assert state.chaos_mode is False
            assert clk.is_drift_active() is False
            assert state.sync_mode == "STEP"
            assert stop_event.is_set()
        finally:
            clk.close()

    def test_virtual_clock_monotonic_safe_across_wall_clock_step(self, monkeypatch):
        wall = {"t": 1700000000.0}
        mono = {"t": 500.0}
        monkeypatch.setattr(time, "time", lambda: wall["t"])
        monkeypatch.setattr(time, "monotonic", lambda: mono["t"])

        vclk = VirtualClock(initial_offset=0.0, slew_rate=0.40)
        vclk.start_slew(1.0)

        mono["t"] += 1.0
        wall["t"] -= 120.0
        vclk.tick()

        assert vclk.get_offset() == pytest.approx(0.40)
        assert vclk.now() == pytest.approx(wall["t"] + 0.40)

    def test_dashboard_renders_os_clock_and_auto_badges(self):
        fake_drv = MagicMock()
        fake_drv.get_remaining_slew.return_value = 0.0
        clk = SystemClock(driver=fake_drv, validate=False)
        try:
            state = SharedState(
                NodeConfig(
                    stratum=3,
                    port=5004,
                    upstreams=["127.0.0.1:5001"],
                    sync_os_clock=True,
                )
            )
            buf = io.StringIO()
            console = Console(file=buf, width=120, height=35)
            console.print(build_dashboard(clk, state))
            rendered = buf.getvalue()
            assert "[OS CLOCK]" in rendered
            assert "[AUTO MODE]" in rendered
            assert "Live OS Clock" in rendered
            assert "Mode (Auto/Step/Slew)" in rendered
        finally:
            clk.close()

    def test_client_worker_handles_os_clock_error_without_crashing(self, monkeypatch):
        fake_drv = MagicMock()
        fake_drv.step.side_effect = OSClockError("EPERM denied")
        fake_drv.get_remaining_slew.return_value = 0.0
        sys_clk = SystemClock(driver=fake_drv, validate=False)
        try:
            cfg = NodeConfig(
                stratum=3,
                port=5009,
                upstreams=[PeerAddress(host="127.0.0.1", port=5001)],
                sync_os_clock=True,
                sync_mode="AUTO",
            )
            state = SharedState(cfg)
            stop_event = threading.Event()
            sample = PeerSample(
                peer=PeerAddress(host="127.0.0.1", port=5001),
                theta=1.0,
                delay=0.004,
                server_stratum=1,
                status="RESPONDED",
            )
            monkeypatch.setattr(
                cli_mod,
                "poll_peers_concurrently",
                lambda *a, **kw: {"127.0.0.1:5001": sample},
            )
            monkeypatch.setattr(stop_event, "wait", lambda timeout=None: stop_event.set() or True)

            cli_mod.client_worker(sys_clk, state, interval=0.01, stop_event=stop_event)

            assert "[OS CLOCK ERROR]" in state.last_action_banner
            assert "EPERM denied" in state.last_action_banner
            assert len(state.sync_history) == 0
        finally:
            sys_clk.close()

    def test_system_clock_slew_tick_progression_and_completion(self):
        fake_drv = MagicMock()
        remaining = iter([0.040, 0.010, 0.0])
        fake_drv.get_remaining_slew.side_effect = lambda: next(remaining, 0.0)

        sys_clk = SystemClock(driver=fake_drv, validate=False)
        try:
            sys_clk.start_slew(0.050)
            assert sys_clk.get_slew_info() == pytest.approx((0.050, 0.050, 0.0))

            sys_clk.tick()
            assert sys_clk.get_slew_info() == pytest.approx((0.040, 0.050, 0.2))

            sys_clk.tick()
            assert sys_clk.get_slew_info() == pytest.approx((0.010, 0.050, 0.8))

            sys_clk.tick()
            assert sys_clk.get_slew_info() == (0.0, 0.0, 1.0)
        finally:
            sys_clk.close()
