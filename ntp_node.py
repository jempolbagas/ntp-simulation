import argparse
import collections
import json
import random
import socket
import sys
import threading
import time
from datetime import datetime
from typing import Deque, Optional, Tuple

try:
    from rich.align import Align
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


class VirtualClock:
    """Thread-safe virtual clock simulating local time with an adjustable offset."""

    def __init__(self, initial_offset: float = 0.0):
        self._offset = initial_offset
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return time.time() + self._offset

    def adjust(self, delta: float) -> None:
        with self._lock:
            self._offset += delta

    def get_offset(self) -> float:
        with self._lock:
            return self._offset

    @staticmethod
    def format_ts(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


class SharedState:
    """Thread-safe data store for UI rendering."""

    def __init__(self, stratum: int, port: int, upstream: Optional[Tuple[str, int]]):
        self.stratum = stratum
        self.port = port
        self.upstream = upstream
        self.lock = threading.Lock()

        # Telemetry
        self.last_sync_time: Optional[float] = None
        self.t1: Optional[float] = None
        self.t2: Optional[float] = None
        self.t3: Optional[float] = None
        self.t4: Optional[float] = None
        self.last_delay: Optional[float] = None
        self.last_theta: Optional[float] = None
        self.upstream_stratum: Optional[int] = None
        self.sync_count: int = 0

        # Server stats
        self.server_req_count: int = 0
        self.recent_server_logs: Deque[dict] = collections.deque(maxlen=5)

        # History
        self.sync_history: Deque[dict] = collections.deque(maxlen=6)

    def record_server_request(self, client_addr: str, t2: float, t3: float) -> None:
        with self.lock:
            self.server_req_count += 1
            self.recent_server_logs.append({
                "time": time.time(),
                "client": client_addr,
                "t2": t2,
                "t3": t3,
            })

    def record_sync(
        self,
        t1: float,
        t2: float,
        t3: float,
        t4: float,
        delay: float,
        theta: float,
        srv_stratum: any,
        before_ts: float,
        after_ts: float,
    ) -> None:
        with self.lock:
            self.last_sync_time = time.time()
            self.t1 = t1
            self.t2 = t2
            self.t3 = t3
            self.t4 = t4
            self.last_delay = delay
            self.last_theta = theta
            self.upstream_stratum = srv_stratum
            self.sync_count += 1

            self.sync_history.appendleft({
                "time": time.time(),
                "delay_ms": delay * 1000.0,
                "theta_s": theta,
                "theta_ms": theta * 1000.0,
                "before": before_ts,
                "after": after_ts,
            })


def run_server(clock: VirtualClock, state: SharedState, stop_event: threading.Event) -> None:
    """UDP NTP server listener handling incoming time sync requests."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", state.port))
    sock.settimeout(0.5)

    try:
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break

            t2 = clock.now()
            try:
                payload = json.loads(data.decode("utf-8"))
                t1 = float(payload["t1"])
            except (ValueError, KeyError, UnicodeDecodeError):
                continue

            t3 = clock.now()
            response = json.dumps({
                "t1": t1,
                "t2": t2,
                "t3": t3,
                "server_stratum": state.stratum,
            }).encode("utf-8")

            sock.sendto(response, addr)
            client_id = f"{addr[0]}:{addr[1]}"
            state.record_server_request(client_id, t2, t3)
    finally:
        sock.close()


def run_client(
    clock: VirtualClock,
    state: SharedState,
    interval: float,
    stop_event: threading.Event,
) -> None:
    """UDP NTP client polling upstream server and applying offset correction."""
    if not state.upstream:
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    up_host, up_port = state.upstream

    try:
        while not stop_event.is_set():
            t1 = clock.now()
            request = json.dumps({"t1": t1}).encode("utf-8")

            try:
                sock.sendto(request, (up_host, up_port))
                data, _ = sock.recvfrom(1024)
                t4 = clock.now()

                resp = json.loads(data.decode("utf-8"))
                t2 = float(resp["t2"])
                t3 = float(resp["t3"])
                srv_stratum = resp.get("server_stratum", "?")

                delay = (t4 - t1) - (t3 - t2)
                theta = ((t2 - t1) + (t3 - t4)) / 2.0

                before_ts = clock.now()
                clock.adjust(theta)
                after_ts = clock.now()

                state.record_sync(
                    t1=t1,
                    t2=t2,
                    t3=t3,
                    t4=t4,
                    delay=delay,
                    theta=theta,
                    srv_stratum=srv_stratum,
                    before_ts=before_ts,
                    after_ts=after_ts,
                )
            except (socket.timeout, OSError, ValueError, KeyError):
                pass

            stop_event.wait(interval)
    finally:
        sock.close()


def render_offset_gauge(offset: float, width: int = 42) -> Text:
    """Render a visual ASCII/Unicode meter showing offset deviation."""
    abs_val = abs(offset)
    gauge = Text()

    # Determine dynamic scale
    if abs_val > 1.0:
        max_bound = 30.0
        label_l, label_r = "-30s", "+30s"
    elif abs_val > 0.05:
        max_bound = 1.0
        label_l, label_r = "-1.0s", "+1.0s"
    else:
        max_bound = 0.05
        label_l, label_r = "-50ms", "+50ms"

    half_w = width // 2
    clamped_offset = max(-max_bound, min(max_bound, offset))
    # Normalized position: -half_w to +half_w
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

    # Color coding based on error threshold
    if abs_val < 0.005:
        status_color = "bold green"
        desc = "PERFECT (in sync < 5ms)"
    elif abs_val < 0.05:
        status_color = "green"
        desc = "ACCURATE (< 50ms)"
    elif abs_val < 1.0:
        status_color = "yellow"
        desc = "MODERATE DRIFT"
    else:
        status_color = "bold red"
        desc = "HIGH DRIFT / UNSYNCED"

    gauge.append(f"{label_l:>6} [")
    gauge.append(gauge_str, style=status_color)
    gauge.append(f"] {label_r:<6}  ")
    gauge.append(f"● {desc}", style=status_color)
    return gauge


def build_dashboard(clock: VirtualClock, state: SharedState) -> Layout:
    """Build modern Rich TUI layout for the NTP node."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="clocks", size=8),
        Layout(name="main", ratio=1),
        Layout(name="footer", size=3),
    )

    now_os = time.time()
    now_virtual = clock.now()
    current_offset = clock.get_offset()

    # 1. HEADER SECTION
    stratum_badges = {
        1: ("[bold black on bright_yellow] STRATUM 1 [/] [bold bright_yellow]ROOT MASTER REFERENCE[/]", "cyan"),
        2: ("[bold white on dodger_blue1] STRATUM 2 [/] [bold dodger_blue1]SECONDARY RELAY NODE[/]", "dodger_blue1"),
        3: ("[bold white on dark_green] STRATUM 3 [/] [bold dark_green]LEAF CLIENT NODE[/]", "green"),
    }
    badge_title, border_color = stratum_badges.get(state.stratum, ("[UNKNOWN]", "white"))

    if state.stratum == 1:
        sync_badge = "[bold green]● MASTER CLOCK SOURCE[/bold green]"
    elif abs(current_offset) < 0.005 and state.sync_count > 0:
        sync_badge = f"[bold green]● SYNCHRONIZED ({state.sync_count} syncs)[/bold green]"
    else:
        sync_badge = f"[bold yellow]○ CONVERGING (Syncing... {state.sync_count})[/bold yellow]"

    up_info = f"Upstream: [bold]{state.upstream[0]}:{state.upstream[1]}[/bold]" if state.upstream else "Upstream: [italic]None (Root Authority)[/italic]"
    srv_info = f"Server: [bold]0.0.0.0:{state.port}[/bold]" if state.stratum in (1, 2) else "Server: [italic]Disabled (Client-only)[/italic]"

    header_table = Table.grid(expand=True)
    header_table.add_column(justify="left", ratio=3)
    header_table.add_column(justify="right", ratio=2)
    header_table.add_row(
        Text.from_markup(f"{badge_title}  |  {up_info}  |  {srv_info}"),
        Text.from_markup(sync_badge),
    )
    layout["header"].update(Panel(header_table, border_style=border_color, title="[bold]Network Time Protocol Simulation[/bold]"))

    # 2. CLOCKS & DRIFT SECTION
    clocks_table = Table.grid(expand=True)
    clocks_table.add_column(justify="center", ratio=1)
    clocks_table.add_column(justify="center", ratio=1)
    clocks_table.add_column(justify="center", ratio=1)

    offset_color = "bold green" if abs(current_offset) < 0.005 else ("yellow" if abs(current_offset) < 1.0 else "bold red")
    offset_text = f"{current_offset:+.6f} s ({current_offset * 1000:+.2f} ms)"

    clocks_table.add_row(
        Panel(
            Align.center(f"[bold cyan]{VirtualClock.format_ts(now_os)}[/bold cyan]\n[dim]OS System Clock[/dim]"),
            title="[dim]Host Hardware[/dim]",
            border_style="cyan",
        ),
        Panel(
            Align.center(f"[bold bright_white]{VirtualClock.format_ts(now_virtual)}[/bold bright_white]\n[dim]Simulated Clock[/dim]"),
            title="[bold]NTP Virtual Time[/bold]",
            border_style=border_color,
        ),
        Panel(
            Align.center(f"[{offset_color}]{offset_text}[/{offset_color}]\n[dim]Δ Virtual vs Host[/dim]"),
            title="[bold]Current Offset[/bold]",
            border_style=offset_color,
        ),
    )

    gauge_text = render_offset_gauge(current_offset)
    clock_box = Group(
        clocks_table,
        Align.center(gauge_text),
    )
    layout["clocks"].update(Panel(clock_box, border_style="dim", title="Clock Telemetry"))

    # 3. MAIN BODY SECTION (Telemetry + History/Server Activity)
    if state.stratum == 1:
        # Stratum 1: Server requests table
        srv_table = Table(title="Recent Inbound Client Synchronizations", expand=True, border_style="dim")
        srv_table.add_column("Timestamp", style="cyan", no_wrap=True)
        srv_table.add_column("Client IP:Port", style="bold white")
        srv_table.add_column("T2 (Server Recv)", style="yellow")
        srv_table.add_column("T3 (Server Send)", style="green")

        with state.lock:
            for item in list(state.recent_server_logs):
                srv_table.add_row(
                    VirtualClock.format_ts(item["time"]),
                    item["client"],
                    VirtualClock.format_ts(item["t2"]),
                    VirtualClock.format_ts(item["t3"]),
                )

        status_text = Text.from_markup(
            f"\n[bold]Total Served Sync Requests:[/] [bright_yellow]{state.server_req_count}[/bright_yellow]\n"
            f"[dim]Operating as the root clock master. No upstream polling active.[/dim]"
        )
        layout["main"].update(Panel(Group(srv_table, status_text), border_style="bright_yellow", title="Master Server Engine"))

    else:
        # Stratum 2 & 3: Telemetry & Math Breakdown + Sync History
        layout["main"].split_row(
            Layout(name="math", ratio=1),
            Layout(name="history", ratio=1),
        )

        # Mathematical Formula Panel
        math_content = Text()
        with state.lock:
            t1_s = VirtualClock.format_ts(state.t1) if state.t1 else "--:--:--.---"
            t2_s = VirtualClock.format_ts(state.t2) if state.t2 else "--:--:--.---"
            t3_s = VirtualClock.format_ts(state.t3) if state.t3 else "--:--:--.---"
            t4_s = VirtualClock.format_ts(state.t4) if state.t4 else "--:--:--.---"
            delay_str = f"{state.last_delay * 1000:+.3f} ms" if state.last_delay is not None else "-- ms"
            theta_str = f"{state.last_theta:+.6f} s ({state.last_theta * 1000:+.3f} ms)" if state.last_theta is not None else "-- s"

        math_content.append("4-Timestamp Protocol:\n", style="bold underline")
        math_content.append(f"  T1 (Client Send) : {t1_s}\n", style="cyan")
        math_content.append(f"  T2 (Server Recv) : {t2_s}\n", style="yellow")
        math_content.append(f"  T3 (Server Send) : {t3_s}\n", style="green")
        math_content.append(f"  T4 (Client Recv) : {t4_s}\n\n", style="magenta")

        math_content.append("Computed Variables:\n", style="bold underline")
        math_content.append(f"  Delay  (δ) = (T4-T1) - (T3-T2) : [bold]{delay_str}[/bold]\n")
        math_content.append(f"  Offset (θ) = ((T2-T1)+(T3-T4))/2: [bold]{theta_str}[/bold]\n\n")
        math_content.append("Adjustment: Virtual Clock += θ", style="dim italic")

        layout["main"]["math"].update(Panel(math_content, border_style="cyan", title="NTP Mathematical Deduction"))

        # History Table
        hist_table = Table(title="Recent Synchronization Rounds", expand=True, border_style="dim")
        hist_table.add_column("Time", style="dim", no_wrap=True)
        hist_table.add_column("Delay", justify="right", style="cyan")
        hist_table.add_column("Applied Offset (θ)", justify="right", style="bold yellow")
        hist_table.add_column("Virtual Time Shift", justify="center", style="white")

        with state.lock:
            for h in list(state.sync_history):
                hist_table.add_row(
                    VirtualClock.format_ts(h["time"]),
                    f"{h['delay_ms']:+.2f}ms",
                    f"{h['theta_s']:+.4f}s",
                    f"{VirtualClock.format_ts(h['before'])} → {VirtualClock.format_ts(h['after'])}",
                )

        layout["main"]["history"].update(Panel(hist_table, border_style="magenta", title="Convergence History"))

    # 4. FOOTER
    layout["footer"].update(
        Panel(
            Align.center("[dim]Press [bold red]Ctrl+C[/bold red] to stop the simulation gracefully | Group Distributed Systems Project[/dim]"),
            border_style="dim",
        )
    )

    return layout


def parse_upstream(value: str) -> Tuple[str, int]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("Upstream must be in HOST:PORT format (e.g. 192.168.1.10:5000)")
    host, port_str = value.rsplit(":", 1)
    try:
        port = int(port_str)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Upstream port must be an integer") from exc
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError("Upstream port must be between 1 and 65535")
    return host, port


def main() -> None:
    parser = argparse.ArgumentParser(description="NTP Stratum Simulation Node (Interactive TUI)")
    parser.add_argument("--stratum", type=int, required=True, choices=[1, 2, 3], help="Node stratum level (1, 2, or 3)")
    parser.add_argument("--port", type=int, default=5000, help="UDP port to listen on (Stratum 1 & 2, default: 5000)")
    parser.add_argument("--upstream", type=parse_upstream, help="Upstream server HOST:PORT (required for Stratum 2 & 3)")
    parser.add_argument("--interval", type=float, default=5.0, help="Sync interval in seconds (default: 5.0)")
    parser.add_argument("--no-ui", action="store_true", help="Disable Rich TUI and output simple logs")
    args = parser.parse_args()

    if args.stratum > 1 and args.upstream is None:
        parser.error(f"--upstream is required when --stratum is {args.stratum}")

    if args.stratum == 1:
        initial_offset = 0.0
    else:
        # Initial random offset between -30s and -5s or +5s and +30s
        initial_offset = random.choice([-1, 1]) * random.uniform(5.0, 30.0)

    clock = VirtualClock(initial_offset=initial_offset)
    state = SharedState(stratum=args.stratum, port=args.port, upstream=args.upstream)
    stop_event = threading.Event()
    threads: list[threading.Thread] = []

    # Start Server thread if Stratum 1 or 2
    if args.stratum in (1, 2):
        srv_thread = threading.Thread(
            target=run_server,
            args=(clock, state, stop_event),
            daemon=True,
        )
        srv_thread.start()
        threads.append(srv_thread)

    # Start Client thread if Stratum 2 or 3
    if args.stratum in (2, 3) and args.upstream is not None:
        cli_thread = threading.Thread(
            target=run_client,
            args=(clock, state, args.interval, stop_event),
            daemon=True,
        )
        cli_thread.start()
        threads.append(cli_thread)

    # UI RENDERING
    if HAS_RICH and not args.no_ui:
        console = Console()
        with Live(build_dashboard(clock, state), console=console, refresh_per_second=10, screen=True) as live:
            try:
                while not stop_event.is_set():
                    live.update(build_dashboard(clock, state))
                    time.sleep(0.1)
            except KeyboardInterrupt:
                stop_event.set()
    else:
        # Fallback console mode
        print(f"=== NTP Node Started (Stratum {args.stratum}) | Initial Offset: {initial_offset:+.3f}s ===")
        try:
            while not stop_event.is_set():
                print(
                    f"[{VirtualClock.format_ts(clock.now())}] Offset: {clock.get_offset():+.6f}s "
                    f"(Syncs: {state.sync_count}, SrvReqs: {state.server_req_count})"
                )
                time.sleep(args.interval)
        except KeyboardInterrupt:
            stop_event.set()

    print("\n[SYSTEM] Shutting down node cleanly...")
    for t in threads:
        t.join(timeout=1.0)
    sys.exit(0)


if __name__ == "__main__":
    main()
