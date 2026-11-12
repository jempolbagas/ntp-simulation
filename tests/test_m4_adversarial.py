"""Adversarial stress harness for KeyReader terminal safety and Rich TUI dashboard rendering.

Adversarially stress-tests:
1. KeyReader under real POSIX pseudo-terminal (pty) conditions:
   - Bitwise termios preservation across normal context exits, unhandled exceptions,
     atexit handlers on process exit/crash, and asynchronous SIGINT/SIGTERM delivery.
   - Non-blocking select timeout and character reading fidelity.
2. Widgets (sparklines, gauges, topology diagrams) under extreme numerical boundary values:
   - Offsets (+-100k, +-0.0, NaN, inf, -inf).
   - Drifts (+-1.0 s/s, 0.0 s/s, NaN, inf).
   - Rolling sample buffers (empty, all-zero, NaN/inf spikes, large buffers).
3. Rich TUI Dashboard (build_dashboard):
   - Extreme offsets, drift rates, and multi-peer scaling (0 to 100 peers).
   - Long peer hostnames, socket error statuses, and IPv6 formatting.
   - Freeze snapshot isolation and unfreeze resumption cycles.
   - Terminal geometry resilience across narrow, standard, and wide dimensions.
"""

from __future__ import annotations

import io
import math
import os
import pty
import signal
import subprocess
import sys
import termios
import time
from typing import List, Tuple
import pytest
from rich.console import Console

from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.models.peer import PeerEvaluation
from ntp_simulation.models.telemetry import ServerLogEntry, SyncHistoryEntry, TelemetrySnapshot
from ntp_simulation.state import SharedState
from ntp_simulation.ui.dashboard import build_dashboard
from ntp_simulation.ui.key_reader import KeyReader
from ntp_simulation.ui.widgets import (
    generate_sparkline,
    render_line_chart,
    render_offset_gauge,
    render_topology_diagram,
)


# ==============================================================================
# 1. KEYREADER TERMINAL SAFETY UNDER PTY STRESS
# ==============================================================================


class TestKeyReaderPtySafety:
    """Stress tests KeyReader terminal preservation and signal safety using real ptys."""

    def test_pty_context_manager_normal_exit_bitwise_identical(self):
        """Asserts termios attributes before and after normal context manager exit are 100% bitwise identical."""
        master, slave = pty.openpty()
        try:
            orig_attrs = termios.tcgetattr(slave)
            real_stdin = sys.stdin
            sys.stdin = os.fdopen(os.dup(slave), "r")
            try:
                with KeyReader() as kr:
                    cbreak_attrs = termios.tcgetattr(slave)
                    # Cbreak must disable ICANON and ECHO
                    assert cbreak_attrs != orig_attrs, "cbreak mode failed to alter terminal attributes"
            finally:
                sys.stdin.close()
                sys.stdin = real_stdin

            restored_attrs = termios.tcgetattr(slave)
            assert (
                restored_attrs == orig_attrs
            ), f"Terminal attributes not bitwise identical after normal context exit: {restored_attrs} != {orig_attrs}"
        finally:
            os.close(master)
            os.close(slave)

    def test_pty_context_manager_exception_bitwise_identical(self):
        """Asserts termios attributes are restored 100% bitwise identical when an unhandled exception occurs."""
        master, slave = pty.openpty()
        try:
            orig_attrs = termios.tcgetattr(slave)
            real_stdin = sys.stdin
            sys.stdin = os.fdopen(os.dup(slave), "r")
            try:
                with pytest.raises(RuntimeError, match="adversarial_test_crash"):
                    with KeyReader() as kr:
                        active_attrs = termios.tcgetattr(slave)
                        assert active_attrs != orig_attrs
                        raise RuntimeError("adversarial_test_crash")
            finally:
                sys.stdin.close()
                sys.stdin = real_stdin

            restored_attrs = termios.tcgetattr(slave)
            assert (
                restored_attrs == orig_attrs
            ), f"Terminal attributes corrupted after context exception: {restored_attrs} != {orig_attrs}"
        finally:
            os.close(master)
            os.close(slave)

    def test_pty_subprocess_atexit_on_clean_termination(self):
        """Tests that atexit restoration fires when process exits without explicit restore() call."""
        master, slave = pty.openpty()
        try:
            orig_attrs = termios.tcgetattr(slave)
            script = (
                "import os, sys\n"
                "sys.path.insert(0, 'ntp-simulation')\n"
                "from ntp_simulation.ui.key_reader import KeyReader\n"
                "kr = KeyReader()\n"
                "sys.exit(0)\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.wait(timeout=5)
            assert proc.returncode == 0

            restored_attrs = termios.tcgetattr(slave)
            assert (
                restored_attrs == orig_attrs
            ), f"atexit failed to restore terminal attributes on clean exit: {restored_attrs} != {orig_attrs}"
        finally:
            os.close(master)
            os.close(slave)

    def test_pty_subprocess_atexit_on_fatal_unhandled_crash(self):
        """Tests that atexit restoration fires when process crashes from an unhandled exception."""
        master, slave = pty.openpty()
        try:
            orig_attrs = termios.tcgetattr(slave)
            script = (
                "import os, sys\n"
                "sys.path.insert(0, 'ntp-simulation')\n"
                "from ntp_simulation.ui.key_reader import KeyReader\n"
                "kr = KeyReader()\n"
                "raise SystemExit('fatal_crash_exit')\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.wait(timeout=5)
            assert proc.returncode != 0

            restored_attrs = termios.tcgetattr(slave)
            assert (
                restored_attrs == orig_attrs
            ), f"atexit failed to restore terminal attributes on fatal crash: {restored_attrs} != {orig_attrs}"
        finally:
            os.close(master)
            os.close(slave)

    def test_pty_sigint_restoration_bitwise_identical(self):
        """Tests terminal restoration when process receives asynchronous SIGINT."""
        master, slave = pty.openpty()
        try:
            orig_attrs = termios.tcgetattr(slave)
            script = (
                "import os, sys, time\n"
                "sys.path.insert(0, 'ntp-simulation')\n"
                "from ntp_simulation.ui.key_reader import KeyReader\n"
                "kr = KeyReader()\n"
                "sys.stdout.write('READY\\n')\n"
                "sys.stdout.flush()\n"
                "time.sleep(10)\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ready_line = proc.stdout.readline()
            assert b"READY" in ready_line

            # Confirm terminal is in cbreak mode
            mid_attrs = termios.tcgetattr(slave)
            assert mid_attrs != orig_attrs, "Terminal was not put into cbreak mode"

            # Deliver SIGINT
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)

            restored_attrs = termios.tcgetattr(slave)
            assert (
                restored_attrs == orig_attrs
            ), f"Terminal corrupted after SIGINT: {restored_attrs} != {orig_attrs}"
        finally:
            os.close(master)
            os.close(slave)

    def test_pty_sigterm_restoration_bitwise_identical(self):
        """Tests terminal restoration when process receives asynchronous SIGTERM."""
        master, slave = pty.openpty()
        try:
            orig_attrs = termios.tcgetattr(slave)
            script = (
                "import os, sys, time\n"
                "sys.path.insert(0, 'ntp-simulation')\n"
                "from ntp_simulation.ui.key_reader import KeyReader\n"
                "kr = KeyReader()\n"
                "sys.stdout.write('READY\\n')\n"
                "sys.stdout.flush()\n"
                "time.sleep(10)\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ready_line = proc.stdout.readline()
            assert b"READY" in ready_line

            mid_attrs = termios.tcgetattr(slave)
            assert mid_attrs != orig_attrs

            # Deliver SIGTERM
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)

            restored_attrs = termios.tcgetattr(slave)
            assert (
                restored_attrs == orig_attrs
            ), f"Terminal corrupted after SIGTERM: {restored_attrs} != {orig_attrs}"
        finally:
            os.close(master)
            os.close(slave)

    def test_key_reader_non_blocking_timeout(self):
        """Verifies that read_key(timeout) returns None within the expected time window when no input is pending."""
        master, slave = pty.openpty()
        try:
            script = (
                "import os, sys, time\n"
                "sys.path.insert(0, 'ntp-simulation')\n"
                "from ntp_simulation.ui.key_reader import KeyReader\n"
                "kr = KeyReader()\n"
                "t0 = time.time()\n"
                "res = kr.read_key(timeout=0.20)\n"
                "elapsed = time.time() - t0\n"
                "assert res is None, f'Expected None, got {res}'\n"
                "assert 0.15 <= elapsed <= 0.40, f'Elapsed out of bounds: {elapsed}'\n"
                "sys.stdout.write('TIMEOUT_SUCCESS\\n')\n"
                "sys.stdout.flush()\n"
                "kr.restore()\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output = proc.stdout.readline().decode().strip()
            proc.wait(timeout=5)
            assert proc.returncode == 0
            assert output == "TIMEOUT_SUCCESS"
        finally:
            os.close(master)
            os.close(slave)

    def test_key_reader_reads_characters(self):
        """Verifies that read_key reads queued characters immediately."""
        master, slave = pty.openpty()
        try:
            script = (
                "import os, sys, time\n"
                "sys.path.insert(0, 'ntp-simulation')\n"
                "from ntp_simulation.ui.key_reader import KeyReader\n"
                "kr = KeyReader()\n"
                "sys.stdout.write('READY\\n')\n"
                "sys.stdout.flush()\n"
                "ch = kr.read_key(timeout=2.0)\n"
                "sys.stdout.write(f'CHAR:{ch}\\n')\n"
                "sys.stdout.flush()\n"
                "kr.restore()\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ready = proc.stdout.readline().decode().strip()
            assert ready == "READY"

            os.write(master, b"m")
            line = proc.stdout.readline().decode().strip()
            proc.wait(timeout=5)
            assert proc.returncode == 0
            assert line == "CHAR:m"
        finally:
            os.close(master)
            os.close(slave)

    def test_key_reader_multiple_consecutive_cycles(self):
        """Tests that entering and exiting KeyReader repeatedly does not leak attributes or handlers."""
        master, slave = pty.openpty()
        try:
            orig_attrs = termios.tcgetattr(slave)
            real_stdin = sys.stdin
            sys.stdin = os.fdopen(os.dup(slave), "r")
            try:
                for i in range(5):
                    with KeyReader() as kr:
                        assert termios.tcgetattr(slave) != orig_attrs
                    assert termios.tcgetattr(slave) == orig_attrs
            finally:
                sys.stdin.close()
                sys.stdin = real_stdin

            assert termios.tcgetattr(slave) == orig_attrs
        finally:
            os.close(master)
            os.close(slave)

    def test_key_reader_non_interactive_fallback(self):
        """Verifies safe behavior when stdin is not a tty."""
        real_stdin = sys.stdin
        sys.stdin = io.StringIO("some input")
        try:
            kr = KeyReader()
            assert kr.old_settings is None
            k = kr.read_key(timeout=0.05)
            assert k is None
            kr.restore()  # Idempotent no-op
        finally:
            sys.stdin = real_stdin


# ==============================================================================
# 2. WIDGETS ADVERSARIAL STRESS (SPARKLINE, GAUGE, TOPOLOGY)
# ==============================================================================


class TestWidgetsAdversarial:
    """Stress tests UI widgets against numerical anomalies, boundary values, and NaN/inf."""

    @pytest.mark.parametrize(
        "values",
        [
            [],
            [0.0],
            [0.0, 0.0, 0.0],
            [1e-6, 2e-6, 3e-6],
            [-100000.0, 100000.0],
            [float("nan")],
            [float("inf")],
            [float("-inf")],
            [float("nan"), 1.0, float("inf"), -1.0, 0.0],
            list(range(100)),
        ],
    )
    def test_sparkline_resilience(self, values):
        """Tests that generate_sparkline never crashes and always returns exact width."""
        width = 16
        result = generate_sparkline(values, width=width)
        assert isinstance(result, str)
        assert len(result) == width

    @pytest.mark.parametrize(
        "offset",
        [
            0.0,
            -0.0,
            0.000001,
            -0.000001,
            0.0049,
            0.049,
            0.99,
            1.01,
            30.0,
            -30.0,
            100000.0,
            -100000.0,
            float("nan"),
            float("inf"),
            float("-inf"),
        ],
    )
    def test_offset_gauge_resilience(self, offset):
        """Tests that render_offset_gauge produces valid Rich Text for all numerical extremes."""
        gauge = render_offset_gauge(offset, width=36)
        assert gauge is not None
        plain = gauge.plain
        assert "[" in plain and "]" in plain
        assert ("LOCKED" in plain or "ACCURATE" in plain or "MODERATE" in plain or "HIGH SKEW" in plain)

    @pytest.mark.parametrize("stratum", [1, 2, 3, 0, 4, -1, 99])
    def test_topology_diagram_all_stratums(self, stratum):
        """Tests that topology diagram returns styled Rich Text for valid and out-of-range stratums."""
        diagram = render_topology_diagram(stratum)
        assert diagram is not None
        assert len(diagram.plain) > 0

    @pytest.mark.parametrize(
        "values",
        [
            [],
            [0.0],
            [-0.050, 0.050],
            [-10.0, 0.0, 10.0],
            [0.0001, -0.0001, 0.0002],
            [float("nan"), float("inf"), 0.05],
            list(range(-20, 20)),
        ],
    )
    def test_line_chart_resilience(self, values):
        """Tests that render_line_chart handles extreme inputs and returns valid Rich Text."""
        chart = render_line_chart(values, width=32, height=7)
        assert chart is not None
        plain = chart.plain
        assert "┤" in plain
        assert "┼" in plain
        assert "└" in plain
        assert "history" in plain


# ==============================================================================
# 3. DASHBOARD RENDERING & STATE STRESS
# ==============================================================================


class TestDashboardRenderingAdversarial:
    """Stress tests build_dashboard across extreme clock states, telemetry extremes, and console sizes."""

    @pytest.fixture
    def console(self):
        return Console(file=io.StringIO(), width=80, height=24)

    @pytest.mark.parametrize(
        "offset",
        [
            100000.0,
            -100000.0,
            0.0,
            -0.0,
            float("nan"),
            float("inf"),
            float("-inf"),
        ],
    )
    def test_dashboard_offset_extremes(self, console, offset):
        """Asserts build_dashboard renders without error across extreme offset values."""
        clock = VirtualClock()
        clock._offset = offset
        state = SharedState(stratum=2, local_ip="127.0.0.1", port=5000, hostname="relay")
        dash = build_dashboard(clock, state)
        console.print(dash)

    @pytest.mark.parametrize(
        "drift",
        [
            1.0,
            -1.0,
            0.0,
            -0.0,
            0.025,
            -0.025,
            float("nan"),
            float("inf"),
        ],
    )
    def test_dashboard_drift_rate_extremes(self, console, drift):
        """Asserts build_dashboard handles extreme drift rates cleanly."""
        clock = VirtualClock()
        clock._drift_rate = drift
        state = SharedState(stratum=2, local_ip="127.0.0.1", port=5000, hostname="relay")
        dash = build_dashboard(clock, state)
        console.print(dash)

    def test_dashboard_peer_scaling_and_fault_statuses(self, console):
        """Stresses dashboard with 50 peers including timeouts, long keys, and error messages."""
        clock = VirtualClock()
        state = SharedState(stratum=2, local_ip="10.0.0.1", port=5000, hostname="relay-huge")

        statuses = ["RESPONDED", "TIMEOUT", "NONCE_MISMATCH", "ERROR: [Errno 111] Connection refused"]
        classifications = ["[TRUECHIMER]", "[FALSETICKER]", "[UNREACHABLE]"]
        actions = ["SELECTED FOR SYNC", "DISCARDED (Outlier)", "PENDING"]

        for i in range(50):
            p_key = f"peer-{i:02d}-{('x' * 30)}:500{i % 10}"
            status = statuses[i % len(statuses)]
            classification = classifications[i % len(classifications)]
            action = actions[i % len(actions)]

            state.peer_reports[p_key] = PeerEvaluation(
                peer_key=p_key,
                stratum=i % 3 + 1,
                delay=0.005 * (i + 1),
                theta=0.001 * (i - 25),
                status=status,
                classification=classification,
                action=action,
            )

        dash = build_dashboard(clock, state)
        # Render on standard and wide consoles
        console.print(dash)
        wide_console = Console(file=io.StringIO(), width=160, height=50)
        wide_console.print(dash)

    def test_dashboard_stratum_1_inbound_server_logs(self, console):
        """Stresses Stratum 1 Master view with 30 inbound server synchronizations."""
        clock = VirtualClock()
        state = SharedState(stratum=1, local_ip="192.168.1.1", port=5000, hostname="stratum1-master")
        state.server_req_count = 125000

        for i in range(30):
            state.recent_server_logs.append(
                ServerLogEntry(
                    time=1700000000.0 + i,
                    client=f"192.168.1.{100 + i}:49152",
                    t2=1700000000.0 + i + 0.0001,
                    t3=1700000000.0 + i + 0.0002,
                    is_fake=(i % 5 == 0),
                    skew=60.0 if (i % 5 == 0) else 0.0,
                )
            )

        dash = build_dashboard(clock, state)
        console.print(dash)

    def test_dashboard_freeze_unfreeze_and_snapshot_fidelity(self, console):
        """Verifies UI freeze isolation, snapshot rendering, and live state resumption."""
        clock = VirtualClock(initial_offset=1.5)
        state = SharedState(stratum=3, local_ip="127.0.0.1", port=5003, hostname="leaf")

        # Initial live render
        d1 = build_dashboard(clock, state)
        console.print(d1)

        # Freeze UI
        assert state.toggle_ui_pause(clock) is True
        assert state.ui_paused is True
        assert state.frozen_snapshot is not None
        frozen_offset = state.frozen_snapshot.offset
        assert frozen_offset == pytest.approx(1.5)

        # Render frozen dashboard
        d2 = build_dashboard(clock, state)
        console.print(d2)

        # Advance live clock while frozen
        clock.step(100.0)
        assert clock.get_offset() == pytest.approx(101.5)

        # Frozen snapshot must remain isolated at 1.5
        d3 = build_dashboard(clock, state)
        console.print(d3)
        assert state.frozen_snapshot.offset == pytest.approx(1.5)

        # Unfreeze UI
        assert state.toggle_ui_pause(clock) is False
        assert state.ui_paused is False
        assert state.frozen_snapshot is None

        # Resumed dashboard displays new live clock offset
        d4 = build_dashboard(clock, state)
        console.print(d4)

    def test_dashboard_active_slew_progress_bar(self, console):
        """Tests that active slew progress bar renders correctly with various progress ratios."""
        clock = VirtualClock()
        clock.start_slew(10.0)
        state = SharedState(stratum=2, local_ip="127.0.0.1", port=5000, hostname="relay")

        for ratio in [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, -0.2]:
            clock._slew_remaining = 10.0 * (1.0 - max(0.0, min(1.0, ratio)))
            dash = build_dashboard(clock, state)
            console.print(dash)

    @pytest.mark.parametrize(
        "w,h",
        [
            (20, 10),
            (40, 15),
            (60, 20),
            (80, 24),
            (120, 35),
            (200, 60),
        ],
    )
    def test_dashboard_terminal_geometry_resilience(self, w, h):
        """Tests that build_dashboard never crashes on tight or huge terminal dimensions."""
        clock = VirtualClock()
        state = SharedState(stratum=2, local_ip="127.0.0.1", port=5000, hostname="relay")
        dash = build_dashboard(clock, state)
        c = Console(file=io.StringIO(), width=w, height=h)
        c.print(dash)
