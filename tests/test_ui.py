"""Unit tests for KeyReader terminal safety, UI widgets, and Rich TUI dashboard."""
from __future__ import annotations

import io
import os
import pty
import sys
import termios
import pytest
from rich.console import Console

from ntp_simulation.clock import VirtualClock
from ntp_simulation.models import NodeConfig, PeerEvaluation, ServerLogEntry
from ntp_simulation.state import SharedState
from ntp_simulation.ui import (
    KeyReader,
    build_dashboard,
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

    def test_dashboard_uses_upstream_sync_for_stratum_gt_1(self):
        buf = io.StringIO()
        console = Console(file=buf, width=120, height=35)
        clock = VirtualClock(initial_offset=-2.5)
        s3 = SharedState(NodeConfig(stratum=3, port=5004, upstreams=["127.0.0.1:5002"]))
        s3.record_sync(peer="127.0.0.1:5002", delay=0.004, theta=12.0, mode="STEP", before=100.0, after=112.0)

        console.print(build_dashboard(clock, s3))
        rendered = buf.getvalue()
        assert "Clock Deviation (vs Upstream)" in rendered
        assert "+0.000000 s" in rendered
        assert "OS: -2.500s" in rendered
        assert "LOCKED (< 5ms)" in rendered

    def test_dashboard_tracks_slew_and_falls_back_when_consensus_lost(self):
        buf = io.StringIO()
        console = Console(file=buf, width=120, height=35)
        clock = VirtualClock(initial_offset=-2.5)
        s3 = SharedState(NodeConfig(stratum=3, port=5004, upstreams=["127.0.0.1:5002"]))
        clock.start_slew(1.5)
        s3.record_sync(peer="127.0.0.1:5002", delay=0.004, theta=1.5, mode="SLEW", before=100.0, after=100.0)

        console.print(build_dashboard(clock, s3))
        rendered = buf.getvalue()
        assert "+1.49" in rendered or "+1.50" in rendered
        assert "LOCKED (< 5ms)" not in rendered
        assert "+30s" in rendered

        s3.update_peer_evaluations({}, chosen_peer=None)
        buf.seek(0)
        buf.truncate(0)
        console.print(build_dashboard(clock, s3))
        rendered = buf.getvalue()
        assert "Delta (Virtual vs Host)" in rendered
        assert "-2." in rendered

    def test_dashboard_shows_live_drift_after_step_sync(self):
        buf = io.StringIO()
        console = Console(file=buf, width=120, height=35)
        clock = VirtualClock(initial_offset=-2.5)
        s2 = SharedState(NodeConfig(stratum=2, port=5002, upstreams=["127.0.0.1:5001"]))
        s2.record_sync(
            peer="127.0.0.1:5001",
            delay=0.002,
            theta=0.015,
            mode="STEP",
            before=100.0,
            after=100.015,
            os_offset=clock.get_offset(),
        )

        # Simulate clock drifting +80ms between poll cycles
        clock.step(0.080)
        console.print(build_dashboard(clock, s2))
        rendered = buf.getvalue()
        assert "-0.080000 s (-80.00 ms)" in rendered
        assert "Last: +0.015s" in rendered
