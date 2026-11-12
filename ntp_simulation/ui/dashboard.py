"""Rich TUI Dashboard builder with frozen snapshot support."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.markup import escape
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.state import SharedState
from ntp_simulation.ui.widgets import (
    generate_sparkline,
    render_line_chart,
    render_offset_gauge,
    render_topology_diagram,
)


def build_dashboard(clock: VirtualClock, state: SharedState) -> Layout:
    """Builds the 4-pane Rich TUI engineering dashboard.

    Panes:
        header: Stratum badge, IP/hostname, fault status, drift rate, TX/RX counters.
        clocks: Real-time clock displays, offset deviation gauge, and convergence sparkline.
        main: Upstream peer Marzullo cross-validation table and sync history / server logs.
        footer: Hotkey guide for interactive manipulation during demonstrations.

    Args:
        clock: Active VirtualClock instance.
        state: SharedState holding telemetry, counters, and frozen snapshots.

    Returns:
        Layout: Rich Layout instance ready for Live terminal rendering.
    """
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="clocks", size=9),
        Layout(name="main", ratio=1),
        Layout(name="footer", size=3),
    )

    with state.lock:
        is_paused = state.ui_paused
        snap = state.frozen_snapshot
        is_falseticker = state.falseticker_mode
        chaos_active = state.chaos_mode
        fault_skew = state.falseticker_skew
        sync_mode = state.sync_mode
        last_banner = state.last_action_banner
        stratum = state.stratum
        local_ip = state.local_ip
        port = state.port
        hostname = state.hostname
        server_req_count = state.server_req_count

    # Extract metrics from snapshot if paused, otherwise query live
    if is_paused and snap is not None:
        now_os = snap.now_os
        now_virtual = snap.now_virtual
        current_offset = snap.offset
        drift_rate = snap.drift_rate
        slew_rem = snap.slew_rem
        slew_target = snap.slew_target
        slew_ratio = snap.slew_ratio
        reports = snap.peer_reports
        server_logs = snap.server_logs
        history_logs = snap.sync_history
        samples = snap.offset_samples
        tx = snap.tx_count
        rx = snap.rx_count
    else:
        now_os = time.time()
        now_virtual = clock.now()
        current_offset = clock.get_offset()
        slew_rem, slew_target, slew_ratio = clock.get_slew_info()
        drift_rate = clock.get_drift_rate()
        with state.lock:
            reports = dict(state.peer_reports)
            server_logs = list(state.recent_server_logs)
            history_logs = list(state.sync_history)
            samples = list(state.offset_samples)
            tx = state.tx_count
            rx = state.rx_count

    # 1. HEADER SECTION
    stratum_badges = {
        1: ("[bold black on bright_yellow] STRATUM 1: ROOT MASTER [/]", "bright_yellow"),
        2: ("[bold white on dodger_blue1] STRATUM 2: SECONDARY RELAY [/]", "dodger_blue1"),
        3: ("[bold white on dark_green] STRATUM 3: LEAF CLIENT [/]", "green"),
    }
    badge_title, border_color = stratum_badges.get(stratum, ("[UNKNOWN]", "white"))

    if is_paused:
        fault_badge = "[bold black on bright_yellow] [FROZEN - PRESS SPACE TO RESUME] [/bold black on bright_yellow]"
    elif is_falseticker:
        fault_badge = "[bold white on red] [FALSETICKER: +60s FAULT] [/bold white on red]"
    elif chaos_active:
        fault_badge = "[bold white on dark_magenta] [CHAOS ACTIVE: 35% GLITCHES] [/bold white on dark_magenta]"
    else:
        fault_badge = "[bold green][ONLINE SERVER][/bold green]" if stratum in (1, 2) else "[dim][CLIENT NODE][/dim]"

    if drift_rate > 0.0:
        drift_badge = f"[bold yellow][DRIFT: {drift_rate * 1000:+.1f}ms/s FAST][/bold yellow]"
    elif drift_rate < 0.0:
        drift_badge = f"[bold red][DRIFT: {drift_rate * 1000:+.1f}ms/s SLOW][/bold red]"
    else:
        drift_badge = "[dim][DRIFT: OFF][/dim]"

    mode_badge = f"[bold white on dark_cyan] [{sync_mode} MODE] [/bold white on dark_cyan]"

    header_table = Table.grid(expand=True)
    header_table.add_column(justify="left", ratio=3)
    header_table.add_column(justify="right", ratio=2)

    ip_info = f"[bold bright_white]{local_ip}[/bold bright_white]:[bold cyan]{port}[/bold cyan] ({hostname})"
    header_table.add_row(
        Text.from_markup(f"{badge_title}  |  IP: {ip_info}  |  {fault_badge}"),
        Text.from_markup(f"{mode_badge}  {drift_badge}  [dim](TX:{tx} RX:{rx})[/dim]"),
    )
    layout["header"].update(
        Panel(header_table, border_style=border_color, title="[bold]Network Time Protocol Simulation Node[/bold]")
    )

    # 2. CLOCKS SECTION
    clocks_table = Table.grid(expand=True)
    if is_falseticker:
        clocks_table.add_column(justify="center", ratio=1)
        clocks_table.add_column(justify="center", ratio=1)
        clocks_table.add_column(justify="center", ratio=1)
        clocks_table.add_column(justify="center", ratio=1)

        fake_ts = now_virtual + fault_skew
        clocks_table.add_row(
            Panel(
                Align.center(f"[bold cyan]{VirtualClock.format_ts(now_os)}[/bold cyan]\n[dim]OS Hardware Time[/dim]"),
                title="[dim]Host Hardware[/dim]",
                border_style="cyan",
            ),
            Panel(
                Align.center(f"[bold white]{VirtualClock.format_ts(now_virtual)}[/bold white]\n[dim]Internal Real Clock[/dim]"),
                title="[bold]True Virtual Clock[/bold]",
                border_style="white",
            ),
            Panel(
                Align.center(f"[bold bright_white on red] {VirtualClock.format_ts(fake_ts)} [/bold bright_white on red]\n[bold red]Emitted (+60s Fake)[/bold red]"),
                title="[bold red]Broadcasted Fake Time[/bold red]",
                border_style="bold red",
            ),
            Panel(
                Align.center(f"[bold yellow]{current_offset:+.6f}s[/bold yellow]\n[dim]Offset (True vs OS)[/dim]"),
                title="[bold]Offset[/bold]",
                border_style="yellow",
            ),
        )
    else:
        clocks_table.add_column(justify="center", ratio=1)
        clocks_table.add_column(justify="center", ratio=1)
        clocks_table.add_column(justify="center", ratio=1)

        offset_color = "bold green" if abs(current_offset) < 0.005 else ("yellow" if abs(current_offset) < 1.0 else "bold red")
        offset_text = f"{current_offset:+.6f} s ({current_offset * 1000:+.2f} ms)"

        clocks_table.add_row(
            Panel(
                Align.center(f"[bold cyan]{VirtualClock.format_ts(now_os)}[/bold cyan]\n[dim]OS Hardware Time[/dim]"),
                title="[dim]Host Hardware[/dim]",
                border_style="cyan",
            ),
            Panel(
                Align.center(f"[bold bright_white]{VirtualClock.format_ts(now_virtual)}[/bold bright_white]\n[dim]Simulated Clock[/dim]"),
                title="[bold]NTP Virtual Time[/bold]",
                border_style=border_color,
            ),
            Panel(
                Align.center(f"[{offset_color}]{offset_text}[/{offset_color}]\n[dim]Delta (Virtual vs Host)[/dim]"),
                title="[bold]Clock Deviation[/bold]",
                border_style=offset_color,
            ),
        )

    slew_display_elements = []
    if abs(slew_rem) > 0.001:
        slew_bar = ProgressBar(total=1.0, completed=slew_ratio, width=32, style="cyan", complete_style="bold cyan")
        slew_text = Text.from_markup(f"[bold cyan][SLEWING SMOOTHLY][/] Left: [bold]{slew_rem:+.3f}s[/bold] / Total: [dim]{slew_target:+.3f}s[/dim]  ")
        slew_grid = Table.grid()
        slew_grid.add_row(slew_text, slew_bar)
        slew_display_elements.append(slew_grid)
    else:
        spark = generate_sparkline(samples, width=16)
        spark_text = Text.from_markup(f"[dim]Convergence Trend:[/] [bold green]{spark}[/bold green] (Last {len(samples)} syncs)")
        slew_display_elements.append(spark_text)

    gauge_text = render_offset_gauge(current_offset)
    gauge_grid = Table.grid(expand=True)
    gauge_grid.add_column(justify="left", ratio=3)
    gauge_grid.add_column(justify="right", ratio=2)
    gauge_grid.add_row(gauge_text, slew_display_elements[0])

    clock_box = Group(clocks_table, gauge_grid)
    panel_title = "Clock Telemetry [FROZEN]" if is_paused else "Clock Telemetry & Dynamic Drift Gauge"
    layout["clocks"].update(Panel(clock_box, border_style="bright_yellow" if is_paused else "dim", title=panel_title))

    # 3. MAIN BODY SECTION
    if stratum == 1:
        srv_table = Table(title="Recent Inbound Client Synchronizations", expand=True, border_style="dim")
        srv_table.add_column("Timestamp", style="cyan", no_wrap=True)
        srv_table.add_column("Client IP:Port", style="bold white")
        srv_table.add_column("Emitted T2", style="yellow")
        srv_table.add_column("Emitted T3", style="green")
        srv_table.add_column("Integrity", justify="center")

        for item in server_logs:
            integrity = f"[bold white on red] [FAKE {item.skew:+.0f}s] [/bold white on red]" if item.is_fake else "[bold green][VALID][/bold green]"
            srv_table.add_row(
                VirtualClock.format_ts(item.time),
                item.client,
                VirtualClock.format_ts(item.t2),
                VirtualClock.format_ts(item.t3),
                integrity,
            )

        status_box = Text.from_markup(
            f"[bold]Stratum Hierarchy:[/] {render_topology_diagram(1).markup}\n"
            f"[bold]Active Event:[/] [bright_yellow]{escape(str(last_banner))}[/bright_yellow]  |  "
            f"[bold]Total Served Clients:[/] [bright_yellow]{server_req_count}[/bright_yellow]"
        )
        layout["main"].update(Panel(Group(srv_table, status_box), border_style="bright_yellow", title="Master Server Engine"))
    else:
        layout["main"].split_row(
            Layout(name="peers", ratio=3),
            Layout(name="history", ratio=2),
        )

        peer_table = Table(title="Upstream Peer Telemetry & Marzullo Filtering", expand=True, border_style="dim")
        peer_table.add_column("Upstream Peer", style="bold white", ratio=2)
        peer_table.add_column("Delay (delta)", justify="right", style="cyan", ratio=1)
        peer_table.add_column("Reported (theta)", justify="right", style="yellow", ratio=2)
        peer_table.add_column("Classification", justify="center", ratio=2)
        peer_table.add_column("Filter Action", justify="left", ratio=2)

        for p_key, p_eval in reports.items():
            if p_eval.status == "RESPONDED":
                c_style = "bold green" if "TRUECHIMER" in p_eval.classification else "bold red"
                a_style = "bold bright_green" if "SELECTED" in p_eval.action else "dim"
                peer_table.add_row(
                    p_key,
                    f"{p_eval.delay * 1000:+.2f}ms",
                    f"{p_eval.theta:+.4f}s",
                    Text(p_eval.classification, style=c_style),
                    Text(p_eval.action, style=a_style),
                )
            else:
                peer_table.add_row(
                    p_key, "--", "--", "[dim]NO RESPONSE[/dim]", Text(str(p_eval.status), style="red")
                )

        topo_and_status = Text.from_markup(
            f"[bold]Hierarchy:[/] {render_topology_diagram(stratum).markup}\n"
            f"[bold]Latest Event:[/] [bright_yellow]{escape(str(last_banner))}[/bright_yellow]"
        )
        layout["main"]["peers"].update(
            Panel(Group(peer_table, topo_and_status), border_style="dodger_blue1", title="Peer Cross-Validation")
        )

        if stratum == 2 and server_req_count > 0:
            srv_table = Table(title="Recent Inbound Client Synchronizations", expand=True, border_style="dim")
            srv_table.add_column("Client IP:Port", style="bold white")
            srv_table.add_column("Emitted T2", style="yellow")
            srv_table.add_column("Emitted T3", style="green")
            srv_table.add_column("Integrity", justify="center")

            for item in server_logs:
                integrity = f"[bold white on red] [FAKE {item.skew:+.0f}s] [/bold white on red]" if item.is_fake else "[bold green][VALID][/bold green]"
                srv_table.add_row(
                    item.client,
                    VirtualClock.format_ts(item.t2),
                    VirtualClock.format_ts(item.t3),
                    integrity,
                )
            layout["main"]["history"].update(
                Panel(srv_table, border_style="bright_yellow", title="Inbound Downstream Requests")
            )
        else:
            hist_table = Table(title="Recent Sync Rounds", expand=True, border_style="dim")
            hist_table.add_column("Time", style="dim", no_wrap=True)
            hist_table.add_column("Peer", style="white")
            hist_table.add_column("Mode", style="cyan")
            hist_table.add_column("Offset (theta)", justify="right", style="bold yellow")

            for h in list(history_logs)[:3]:
                hist_table.add_row(
                    VirtualClock.format_ts(h.time),
                    h.peer.split(":")[0],
                    h.mode,
                    f"{h.theta_s:+.4f}s",
                )

            chart_text = render_line_chart(samples, width=32, height=6)
            history_content = Group(
                Panel(chart_text, title="[bold]Offset Convergence Trajectory[/bold]", border_style="cyan"),
                hist_table,
            )
            layout["main"]["history"].update(
                Panel(history_content, border_style="magenta", title="Sync History & Trend")
            )

    # 4. FOOTER
    layout["footer"].update(
        Panel(
            Align.center(
                "[bold white][SPACE][/bold white] Freeze/Pause UI  |  "
                "[bold bright_yellow][F][/bold bright_yellow] Manual Falseticker  |  "
                "[bold magenta][C][/bold magenta] Auto Chaos  |  "
                "[bold cyan][M][/bold cyan] Slew/Step  |  "
                "[bold green][D][/bold green] Dynamic Drift  |  "
                "[bold red][Q][/bold red] Quit"
            ),
            border_style="dim",
        )
    )

    return layout
