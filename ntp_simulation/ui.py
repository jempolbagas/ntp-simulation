"""Terminal UI components: POSIX KeyReader, Unicode gauges/charts, and Rich TUI dashboard."""
from __future__ import annotations

import atexit
import math
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.markup import escape
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ntp_simulation.clock import VirtualClock
from ntp_simulation.state import SharedState


class KeyReader:
    """Reads single keypresses non-blockingly while guaranteeing terminal restoration."""

    def __init__(self) -> None:
        self.is_windows: bool = os.name == "nt"
        self.old_settings: Optional[Any] = None
        self._restored: bool = False
        self._orig_handlers: Dict[int, Any] = {}

        if not self.is_windows:
            try:
                is_atty = bool(sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())
            except Exception:
                is_atty = False

            if is_atty:
                try:
                    import termios
                    import tty

                    fd = sys.stdin.fileno()
                    self.old_settings = termios.tcgetattr(fd)
                    tty.setcbreak(fd)

                    atexit.register(self.restore)

                    for sig in (signal.SIGINT, signal.SIGTERM):
                        try:
                            orig = signal.signal(sig, self._handle_signal)
                            self._orig_handlers[sig] = orig
                        except (ValueError, OSError):
                            pass
                except Exception:
                    self.old_settings = None

    def _handle_signal(self, signum: int, frame: Any) -> None:
        orig = self._orig_handlers.get(signum)
        self.restore()
        if callable(orig) and orig not in (self._handle_signal, signal.SIG_IGN, signal.SIG_DFL):
            orig(signum, frame)
        elif orig == signal.SIG_DFL:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        else:
            sys.exit(128 + signum)

    def read_key(self, timeout: float = 0.1) -> Optional[str]:
        """Reads a single keypress without blocking indefinitely."""
        if self.is_windows:
            try:
                import msvcrt

                start = time.monotonic()
                while time.monotonic() - start < timeout:
                    if msvcrt.kbhit():
                        ch = msvcrt.getch()
                        try:
                            return ch.decode("utf-8")
                        except UnicodeDecodeError:
                            return None
                    time.sleep(0.02)
                return None
            except Exception:
                time.sleep(timeout)
                return None
        else:
            try:
                is_atty = bool(sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())
            except Exception:
                is_atty = False

            if not is_atty or self.old_settings is None:
                time.sleep(timeout)
                return None

            try:
                import select

                rlist, _, _ = select.select([sys.stdin], [], [], timeout)
                if rlist:
                    return sys.stdin.read(1)
                return None
            except Exception:
                return None

    def restore(self) -> None:
        """Restores normal canonical terminal settings and signal handlers."""
        if not self._restored:
            try:
                if self.old_settings is not None and not self.is_windows:
                    try:
                        is_atty = bool(sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())
                    except Exception:
                        is_atty = False

                    if is_atty:
                        import termios

                        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass
            finally:
                self._restored = True
                for sig, orig in list(self._orig_handlers.items()):
                    try:
                        signal.signal(sig, orig)
                    except (ValueError, OSError):
                        pass
                self._orig_handlers.clear()
                try:
                    atexit.unregister(self.restore)
                except Exception:
                    pass

    def __enter__(self) -> KeyReader:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.restore()


def generate_sparkline(values: Sequence[float], width: int = 16) -> str:
    """Renders a Unicode sparkline graph showing offset convergence trend."""
    if width <= 0:
        return ""
    finite = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not finite:
        return " " * width

    bars = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    trimmed = finite[-width:]
    abs_vals = [abs(v) for v in trimmed]
    max_val = max(abs_vals) if abs_vals and max(abs_vals) > 0.0 else 1.0

    result = []
    for v in trimmed:
        ratio = min(1.0, abs(v) / max_val)
        idx = int(ratio * (len(bars) - 1))
        result.append(bars[idx])

    padding = " " * (width - len(result))
    return padding + "".join(result)


def render_offset_gauge(offset: float, width: int = 36) -> Text:
    """Renders calibrated deviation gauge meter with needle indicator and status badge."""
    raw_offset = float(offset)
    abs_val = abs(raw_offset)
    gauge = Text()

    if not math.isfinite(raw_offset) or abs_val > 1.0:
        max_bound = 30.0
        label_l, label_r = "-30s", "+30s"
    elif abs_val > 0.05:
        max_bound = 1.0
        label_l, label_r = "-1.0s", "+1.0s"
    else:
        max_bound = 0.05
        label_l, label_r = "-50ms", "+50ms"

    half_w = width // 2
    if not math.isfinite(raw_offset):
        pos = half_w if raw_offset > 0 else (-half_w if raw_offset < 0 else 0)
    else:
        clamped_offset = max(-max_bound, min(max_bound, raw_offset))
        pos = int((clamped_offset / max_bound) * half_w)

    bar_chars = []
    for i in range(-half_w, half_w + 1):
        if i == pos:
            bar_chars.append("█")
        elif i == 0:
            bar_chars.append("│")
        else:
            bar_chars.append("─")

    gauge_str = "".join(bar_chars)

    if abs_val < 0.005:
        status_color = "bold green"
        desc = "LOCKED (< 5ms)"
    elif abs_val < 0.05:
        status_color = "green"
        desc = "ACCURATE (< 50ms)"
    elif abs_val < 1.0:
        status_color = "yellow"
        desc = "MODERATE DRIFT"
    else:
        status_color = "bold red"
        desc = "HIGH SKEW"

    gauge.append(f"{label_l:>6} [")
    gauge.append(gauge_str, style=status_color)
    gauge.append(f"] {label_r:<6} ")
    gauge.append(f"[{desc}]", style=status_color)
    return gauge


def render_topology_diagram(stratum: int) -> Text:
    """Renders visual ASCII topology hierarchy diagram highlighting the current node."""
    diag = Text()
    if stratum == 1:
        diag.append("[STRATUM 1: MASTER]", style="bold black on bright_yellow")
        diag.append(" --> [Stratum 2 Relays] --> [Stratum 3 Clients]", style="dim")
    elif stratum == 2:
        diag.append("[Stratum 1 Master] --> ", style="dim")
        diag.append("[STRATUM 2: THIS RELAY]", style="bold white on dodger_blue1")
        diag.append(" --> [Stratum 3 Clients]", style="dim")
    else:
        diag.append("[Stratum 1 Master] --> [Stratum 2 Relays] --> ", style="dim")
        diag.append("[STRATUM 3: THIS CLIENT]", style="bold white on dark_green")
    return diag


def render_line_chart(
    values: Sequence[float],
    width: int = 34,
    height: int = 7,
) -> Text:
    """Renders a multi-line ASCII/Unicode terminal line chart of offset convergence history."""
    valid_vals = [
        float(v)
        for v in values
        if v is not None and math.isfinite(float(v))
    ]
    if not valid_vals:
        valid_vals = [0.0]

    limit = max(max(abs(v) for v in valid_vals), 0.005)
    if limit >= 1.0:
        top_lbl = f"{limit:+6.1f}s"
        mid_lbl = "   0.0s"
        bot_lbl = f"{-limit:+6.1f}s"
    elif limit >= 0.05:
        top_lbl = f"{limit * 1000:+6.0f}ms"
        mid_lbl = "    0ms"
        bot_lbl = f"{-limit * 1000:+6.0f}ms"
    else:
        top_lbl = f"{limit * 1000:+6.1f}ms"
        mid_lbl = "    0ms"
        bot_lbl = f"{-limit * 1000:+6.1f}ms"

    plot_vals = [None] * max(0, width - len(valid_vals)) + valid_vals[-width:]
    plot_vals = plot_vals[-width:]

    mid_row = height // 2
    col_rows: List[Optional[int]] = []
    for v in plot_vals:
        if v is None:
            col_rows.append(None)
        else:
            norm = max(-1.0, min(1.0, float(v) / limit))
            r = int(round(mid_row - norm * mid_row))
            col_rows.append(max(0, min(height - 1, r)))

    grid = [[" " for _ in range(width)] for _ in range(height)]
    styles = [["" for _ in range(width)] for _ in range(height)]

    for c in range(width):
        grid[mid_row][c] = "┄"
        styles[mid_row][c] = "dim cyan"

    prev_r: Optional[int] = None
    for c in range(width):
        curr_r = col_rows[c]
        if curr_r is not None:
            v_val = plot_vals[c]
            abs_v = abs(float(v_val)) if v_val is not None else 0.0
            if abs_v < 0.005:
                pt_style = "bold bright_green"
                char = "█" if curr_r == mid_row else "●"
            elif abs_v < 0.05:
                pt_style = "bold green"
                char = "●"
            elif abs_v < 1.0:
                pt_style = "bold yellow"
                char = "●"
            else:
                pt_style = "bold red"
                char = "▲" if v_val > 0 else "▼"

            if prev_r is not None and prev_r != curr_r:
                step = 1 if curr_r > prev_r else -1
                for r in range(prev_r + step, curr_r, step):
                    grid[r][c] = "│"
                    styles[r][c] = "cyan" if abs_v < 0.05 else ("yellow" if abs_v < 1.0 else "red")

            grid[curr_r][c] = char
            styles[curr_r][c] = pt_style
            prev_r = curr_r

    result = Text()
    for r in range(height):
        if r == 0:
            result.append(f"{top_lbl} ┤", style="cyan")
        elif r == mid_row:
            result.append(f"{mid_lbl} ┼", style="bold cyan")
        elif r == height - 1:
            result.append(f"{bot_lbl} ┤", style="cyan")
        else:
            result.append("        │", style="dim cyan")

        for c in range(width):
            ch = grid[r][c]
            st = styles[r][c]
            result.append(ch, style=st if st else None)
        result.append("\n")

    result.append("        └" + "─" * width + "\n", style="dim cyan")
    latest_val = valid_vals[-1]
    latest_str = (
        f"Now: {latest_val * 1000:+.2f}ms"
        if abs(latest_val) < 1.0
        else f"Now: {latest_val:+.4f}s"
    )
    pad_len = max(1, width - len(latest_str) - 8)
    result.append("        -history" + " " * pad_len, style="dim")
    status_color = (
        "bold bright_green"
        if abs(latest_val) < 0.005
        else ("green" if abs(latest_val) < 0.05 else ("yellow" if abs(latest_val) < 1.0 else "bold red"))
    )
    result.append(f"[{latest_str}]", style=status_color)
    return result


def build_dashboard(clock: VirtualClock, state: SharedState) -> Layout:
    """Builds the 4-pane Rich TUI engineering dashboard."""
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
        sync_os_clock = state.sync_os_clock
        last_banner = state.last_action_banner
        stratum = state.stratum
        local_ip = state.local_ip
        port = state.port
        hostname = state.hostname
        server_req_count = state.server_req_count

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
        selected_peer = snap.selected_peer
        synced_os_offset = snap.synced_os_offset
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
            selected_peer = state.selected_peer
            synced_os_offset = state.synced_os_offset

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

    os_badge = "[bold white on dark_red] [OS CLOCK] [/bold white on dark_red]  " if sync_os_clock else ""
    mode_badge = f"[bold white on dark_cyan] [{sync_mode} MODE] [/bold white on dark_cyan]"

    header_table = Table.grid(expand=True)
    header_table.add_column(justify="left", ratio=3)
    header_table.add_column(justify="right", ratio=2)

    ip_info = f"[bold bright_white]{local_ip}[/bold bright_white]:[bold cyan]{port}[/bold cyan] ({hostname})"
    header_table.add_row(
        Text.from_markup(f"{badge_title}  |  IP: {ip_info}  |  {fault_badge}"),
        Text.from_markup(f"{os_badge}{mode_badge}  {drift_badge}  [dim](TX:{tx} RX:{rx})[/dim]"),
    )
    layout["header"].update(
        Panel(header_table, border_style=border_color, title="[bold]Network Time Protocol Simulation Node[/bold]")
    )

    has_upstream_sync = stratum > 1 and selected_peer is not None and len(history_logs) > 0
    if has_upstream_sync:
        if synced_os_offset is not None:
            display_offset = synced_os_offset - current_offset
        else:
            display_offset = slew_rem if abs(slew_rem) > 0.001 else 0.0
    else:
        display_offset = current_offset

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

        offset_color = "bold green" if abs(display_offset) < 0.005 else ("yellow" if abs(display_offset) < 1.0 else "bold red")
        offset_text = f"{display_offset:+.6f} s ({display_offset * 1000:+.2f} ms)"
        if has_upstream_sync:
            dev_title = "[bold]Clock Deviation (vs Upstream)[/bold]"
            dev_subtitle = f"[dim]Last: {history_logs[0].theta_s:+.3f}s | OS: {current_offset:+.3f}s[/dim]"
        else:
            dev_title = "[bold]Clock Deviation[/bold]"
            dev_subtitle = "[dim]Delta (Virtual vs Host)[/dim]"

        clock_panel_subtitle = "[dim]Live OS Clock[/dim]" if sync_os_clock else "[dim]Simulated Clock[/dim]"
        clock_panel_title = "[bold]Disciplined OS Clock[/bold]" if sync_os_clock else "[bold]NTP Virtual Time[/bold]"
        clocks_table.add_row(
            Panel(
                Align.center(f"[bold cyan]{VirtualClock.format_ts(now_os)}[/bold cyan]\n[dim]OS Hardware Time[/dim]"),
                title="[dim]Host Hardware[/dim]",
                border_style="cyan",
            ),
            Panel(
                Align.center(f"[bold bright_white]{VirtualClock.format_ts(now_virtual)}[/bold bright_white]\n{clock_panel_subtitle}"),
                title=clock_panel_title,
                border_style=border_color,
            ),
            Panel(
                Align.center(f"[{offset_color}]{offset_text}[/{offset_color}]\n{dev_subtitle}"),
                title=dev_title,
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

    gauge_text = render_offset_gauge(current_offset if is_falseticker else display_offset)
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
                "[bold cyan][M][/bold cyan] Mode (Auto/Step/Slew)  |  "
                "[bold green][D][/bold green] Dynamic Drift  |  "
                "[bold red][Q][/bold red] Quit"
            ),
            border_style="dim",
        )
    )

    return layout
