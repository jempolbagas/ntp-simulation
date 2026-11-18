"""Unit tests for KeyReader terminal safety, UI widgets, and Rich TUI dashboard."""
from __future__ import annotations

import io
import os
import pty
import sys
import termios
import pytest
from rich.console import Console

from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.models.config import NodeConfig
from ntp_simulation.models.peer import PeerEvaluation
from ntp_simulation.models.telemetry import ServerLogEntry
from ntp_simulation.state import SharedState
from ntp_simulation.ui.dashboard import build_dashboard
from ntp_simulation.ui.key_reader import KeyReader
from ntp_simulation.ui.widgets import (
    generate_sparkline,
    render_line_chart,
    render_offset_gauge,
    render_topology_diagram,
)


class TestKeyReaderPtySafety:
    def test_pty_context_manager_restores_termios(self):
        master, slave = pty.openpty()
        try:
            orig_attrs = termios.tcgetattr(slave)
            real_stdin = sys.stdin
            sys.stdin = os.fdopen(os.dup(slave), "r")
            try:
                with KeyReader():
                    assert termios.tcgetattr(slave) != orig_attrs
            finally:
                sys.stdin.close()
                sys.stdin = real_stdin
            assert termios.tcgetattr(slave) == orig_attrs
        finally:
            os.close(master)
            os.close(slave)

    def test_non_interactive_fallback(self):
        real_stdin = sys.stdin
        sys.stdin = io.StringIO("input")
        try:
            kr = KeyReader()
            assert kr.old_settings is None
            assert kr.read_key(timeout=0.01) is None
            kr.restore()
        finally:
            sys.stdin = real_stdin


class TestWidgets:
    @pytest.mark.parametrize(
        "values",
        [[], [0.0], [-10.0, 0.0, 10.0], [float("nan"), float("inf"), 0.05]],
    )
    def test_sparkline_and_line_chart(self, values):
        spark = generate_sparkline(values, width=16)
        assert len(spark) == 16
        chart = render_line_chart(values, width=24, height=5)
        assert "┼" in chart.plain

    @pytest.mark.parametrize("offset", [0.0, 0.02, 2.5, -50.0, float("nan"), float("inf")])
    def test_offset_gauge(self, offset):
        plain = render_offset_gauge(offset, width=36).plain
        assert "[" in plain and "]" in plain

    @pytest.mark.parametrize("stratum", [1, 2, 3])
    def test_topology_diagram(self, stratum):
        assert f"STRATUM {stratum}" in render_topology_diagram(stratum).plain


class TestDashboard:
    def test_dashboard_renders_all_stratums_and_freeze_state(self):
        console = Console(file=io.StringIO(), width=100, height=30)
        clock = VirtualClock(initial_offset=1.5)

        # Stratum 1 with server log
        s1 = SharedState(NodeConfig(stratum=1, port=5001))
        s1.recent_server_logs.append(
            ServerLogEntry(time=100.0, client="127.0.0.1:5002", t2=100.1, t3=100.2, is_fake=True, skew=60.0)
        )
        console.print(build_dashboard(clock, s1))

        # Stratum 3 with peer evaluations and UI freeze
        s3 = SharedState(NodeConfig(stratum=3, port=5004, upstreams=["127.0.0.1:5002"]))
        s3.peer_reports["127.0.0.1:5002"] = PeerEvaluation(
            peer_key="127.0.0.1:5002",
            stratum=2,
            delay=0.004,
            theta=0.001,
            status="RESPONDED",
            classification="[TRUECHIMER]",
            action="SELECTED FOR SYNC",
        )
        assert s3.toggle_ui_pause(clock) is True
        clock.step(50.0)
        console.print(build_dashboard(clock, s3))
        assert s3.frozen_snapshot.offset == pytest.approx(1.5)
        assert s3.toggle_ui_pause(clock) is False
