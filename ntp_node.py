import argparse
import collections
import json
import math
import os
import random
import socket
import sys
import threading
import time
from datetime import datetime
from typing import Deque, List, Optional, Tuple

try:
    from rich.align import Align
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.table import Table
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


def get_local_ip() -> str:
    """Detect LAN IP address of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


class KeyReader:
    """Cross-platform non-blocking key reader for interactive terminal hotkeys."""

    def __init__(self):
        self.is_windows = os.name == "nt"
        self.old_settings = None
        if not self.is_windows and sys.stdin.isatty():
            try:
                import termios
                import tty
                self.old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self.old_settings = None

    def read_key(self, timeout: float = 0.1) -> Optional[str]:
        if self.is_windows:
            import msvcrt
            start = time.time()
            while time.time() - start < timeout:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    try:
                        return ch.decode("utf-8")
                    except UnicodeDecodeError:
                        return None
                time.sleep(0.02)
            return None
        else:
            if not sys.stdin.isatty() or self.old_settings is None:
                time.sleep(timeout)
                return None
            import select
            rlist, _, _ = select.select([sys.stdin], [], [], timeout)
            if rlist:
                return sys.stdin.read(1)
            return None

    def restore(self) -> None:
        if self.old_settings is not None and not self.is_windows and sys.stdin.isatty():
            try:
                import termios
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass


class VirtualClock:
    """Thread-safe virtual clock with dynamic Brownian drift and slewing."""

    def __init__(self, initial_offset: float = 0.0):
        self._offset = initial_offset
        self._slew_total_target = 0.0
        self._slew_remaining = 0.0
        self._slew_rate = 0.40  # ±0.40 s/s adjustment speed during slewing

        # Dynamic Brownian drift simulation
        self._drift_active = False
        self._drift_rate = 0.0  # current instantaneous drift rate (s/s)
        self._drift_target = 0.0
        self._last_drift_shift = time.time()

        self._last_tick = time.time()
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return time.time() + self._offset

    def step(self, delta: float) -> None:
        """Instantaneous clock adjustment."""
        with self._lock:
            self._offset += delta
            self._slew_remaining = 0.0
            self._slew_total_target = 0.0

    def start_slew(self, delta: float) -> None:
        """Gradual clock adjustment (slewing) without discontinuous jumps."""
        with self._lock:
            self._slew_remaining += delta
            self._slew_total_target = self._slew_remaining

    def tick(self) -> None:
        """Called periodically by background thread to apply continuous drift and slewing."""
        with self._lock:
            now_real = time.time()
            dt = now_real - self._last_tick
            self._last_tick = now_real

            # Dynamic continuous random walk drift
            if self._drift_active:
                # Every 1-2 seconds, adjust target drift direction slightly
                if now_real - self._last_drift_shift > 1.2:
                    self._drift_target += random.uniform(-0.006, 0.006)
                    # Bound maximum drift target between -25ms/s and +25ms/s
                    self._drift_target = max(-0.025, min(0.025, self._drift_target))
                    self._last_drift_shift = now_real

                # Smoothly interpolate current drift rate toward drift target
                drift_step = (self._drift_target - self._drift_rate) * min(1.0, dt * 2.0)
                self._drift_rate += drift_step

                # Apply continuous drift to offset
                self._offset += self._drift_rate * dt

            # Apply gradual slewing
            if self._slew_remaining != 0.0:
                max_step = self._slew_rate * dt
                if abs(self._slew_remaining) <= max_step:
                    self._offset += self._slew_remaining
                    self._slew_remaining = 0.0
                    self._slew_total_target = 0.0
                else:
                    step = math.copysign(max_step, self._slew_remaining)
                    self._offset += step
                    self._slew_remaining -= step

    def get_offset(self) -> float:
        with self._lock:
            return self._offset

    def get_slew_info(self) -> Tuple[float, float, float]:
        """Returns (remaining, total_target, progress_ratio)."""
        with self._lock:
            if self._slew_total_target == 0.0:
                return (0.0, 0.0, 1.0)
            completed = abs(self._slew_total_target) - abs(self._slew_remaining)
            ratio = max(0.0, min(1.0, completed / max(0.0001, abs(self._slew_total_target))))
            return (self._slew_remaining, self._slew_total_target, ratio)

    def get_drift_rate(self) -> float:
        with self._lock:
            return self._drift_rate if self._drift_active else 0.0

    def is_drift_active(self) -> bool:
        with self._lock:
            return self._drift_active

    def toggle_drift(self) -> bool:
        """Toggles hardware dynamic drift on/off."""
        with self._lock:
            self._drift_active = not self._drift_active
            if self._drift_active:
                # Seed with an initial random drift
                self._drift_target = random.choice([-1.0, 1.0]) * random.uniform(0.005, 0.018)
                self._drift_rate = self._drift_target
                self._last_drift_shift = time.time()
            else:
                self._drift_rate = 0.0
                self._drift_target = 0.0
            return self._drift_active

    @staticmethod
    def format_ts(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


class SharedState:
    """Thread-safe data store for telemetry, background workers, and UI state."""

    def __init__(self, stratum: int, port: int, upstreams: List[Tuple[str, int]]):
        self.stratum = stratum
        self.port = port
        self.local_ip = get_local_ip()
        self.hostname = socket.gethostname()
        self.upstreams = upstreams
        self.lock = threading.Lock()

        # UI Freeze / Pause State (Spacebar)
        self.ui_paused: bool = False
        self.frozen_snapshot: Optional[dict] = None

        # Runtime interactive toggles
        self.falseticker_mode: bool = False  # Manual toggle [F]
        self.falseticker_skew: float = 60.0
        self.chaos_mode: bool = False  # Random occurrence mode [C]
        self.sync_mode: str = "STEP"  # "STEP" or "SLEW" [M]

        # Packet counters
        self.tx_count: int = 0
        self.rx_count: int = 0
        self.last_action_banner: str = "System Initialized. Awaiting synchronization cycles."

        # Telemetry & Multi-Peer State
        self.peer_reports: dict = {}
        self.last_sync_time: Optional[float] = None
        self.t1: Optional[float] = None
        self.t2: Optional[float] = None
        self.t3: Optional[float] = None
        self.t4: Optional[float] = None
        self.last_delay: Optional[float] = None
        self.last_theta: Optional[float] = None
        self.selected_peer: Optional[str] = None
        self.sync_count: int = 0
        self.prev_steady_offset: Optional[float] = None

        # Server stats
        self.server_req_count: int = 0
        self.recent_server_logs: Deque[dict] = collections.deque(maxlen=5)

        # History and Sparkline samples
        self.sync_history: Deque[dict] = collections.deque(maxlen=6)
        self.offset_samples: Deque[float] = collections.deque(maxlen=20)

    def toggle_ui_pause(self, clock: VirtualClock) -> bool:
        """Freezes or unfreezes the UI rendering without stopping background processes."""
        with self.lock:
            self.ui_paused = not self.ui_paused
            if self.ui_paused:
                now_os = time.time()
                now_virtual = clock.now()
                slew_rem, slew_target, slew_ratio = clock.get_slew_info()
                self.frozen_snapshot = {
                    "now_os": now_os,
                    "now_virtual": now_virtual,
                    "offset": clock.get_offset(),
                    "drift_rate": clock.get_drift_rate(),
                    "slew_rem": slew_rem,
                    "slew_target": slew_target,
                    "slew_ratio": slew_ratio,
                    "peer_reports": dict(self.peer_reports),
                    "server_logs": list(self.recent_server_logs),
                    "sync_history": list(self.sync_history),
                    "offset_samples": list(self.offset_samples),
                    "tx": self.tx_count,
                    "rx": self.rx_count,
                    "pause_time": now_os,
                }
                self.last_action_banner = "UI TELEMETRY FROZEN (Background sync processes continue running)"
            else:
                self.frozen_snapshot = None
                self.last_action_banner = "UI Telemetry Resumed"
            return self.ui_paused

    def toggle_falseticker(self) -> bool:
        with self.lock:
            self.falseticker_mode = not self.falseticker_mode
            status = "ACTIVE (+60s)" if self.falseticker_mode else "DISABLED (Honest)"
            self.last_action_banner = f"Manual Falseticker Mode: {status}"
            return self.falseticker_mode

    def toggle_chaos(self) -> bool:
        with self.lock:
            self.chaos_mode = not self.chaos_mode
            status = "ENABLED (35% Sporadic Glitches)" if self.chaos_mode else "DISABLED"
            self.last_action_banner = f"Random Chaos Mode: {status}"
            return self.chaos_mode

    def toggle_sync_mode(self) -> str:
        with self.lock:
            self.sync_mode = "SLEW" if self.sync_mode == "STEP" else "STEP"
            self.last_action_banner = f"Sync Algorithm switched to: {self.sync_mode} Mode"
            return self.sync_mode

    def record_server_request(self, client_addr: str, t2: float, t3: float, is_fake: bool, skew: float) -> None:
        with self.lock:
            self.server_req_count += 1
            self.tx_count += 1
            self.rx_count += 1
            self.recent_server_logs.append({
                "time": time.time(),
                "client": client_addr,
                "t2": t2,
                "t3": t3,
                "is_fake": is_fake,
                "skew": skew,
            })

    def update_peer_evaluations(self, reports: dict, chosen_peer: Optional[str]) -> None:
        with self.lock:
            self.peer_reports = reports
            self.selected_peer = chosen_peer

    def record_sync(
        self,
        t1: float,
        t2: float,
        t3: float,
        t4: float,
        delay: float,
        theta: float,
        peer_name: str,
        before_ts: float,
        after_ts: float,
        mode: str,
    ) -> None:
        with self.lock:
            self.last_sync_time = time.time()
            self.t1 = t1
            self.t2 = t2
            self.t3 = t3
            self.t4 = t4
            self.last_delay = delay
            self.last_theta = theta
            self.selected_peer = peer_name
            self.sync_count += 1
            self.prev_steady_offset = theta
            self.offset_samples.append(theta)

            if mode == "STEP":
                self.last_action_banner = f"[STEP APPLIED] Clock shifted {theta:+.4f}s"
            else:
                self.last_action_banner = f"[SLEW ACTIVE] Adapting clock smoothly toward {theta:+.4f}s"

            self.sync_history.appendleft({
                "time": time.time(),
                "peer": peer_name,
                "delay_ms": delay * 1000.0,
                "theta_s": theta,
                "mode": mode,
                "before": before_ts,
                "after": after_ts,
            })


def clock_tick_worker(clock: VirtualClock, stop_event: threading.Event) -> None:
    """Worker loop that drives gradual slewing and continuous dynamic drift simulation."""
    while not stop_event.is_set():
        clock.tick()
        time.sleep(0.04)


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

            with state.lock:
                manual_fault = state.falseticker_mode
                chaos_active = state.chaos_mode
                default_skew = state.falseticker_skew

            is_corrupted = False
            applied_skew = 0.0

            if manual_fault:
                is_corrupted = True
                applied_skew = default_skew
            elif chaos_active and (random.random() < 0.35):
                is_corrupted = True
                applied_skew = random.choice([-1.0, 1.0]) * random.uniform(30.0, 90.0)

            if is_corrupted:
                t2 += applied_skew
                t3 += applied_skew

            response = json.dumps({
                "t1": t1,
                "t2": t2,
                "t3": t3,
                "server_stratum": state.stratum,
                "is_falseticker": is_corrupted,
            }).encode("utf-8")

            sock.sendto(response, addr)
            client_id = f"{addr[0]}:{addr[1]}"
            state.record_server_request(client_id, t2, t3, is_corrupted, applied_skew)
    finally:
        sock.close()


def run_client(
    clock: VirtualClock,
    state: SharedState,
    interval: float,
    stop_event: threading.Event,
) -> None:
    """UDP NTP client with multi-peer polling and Falseticker/Outlier rejection."""
    if not state.upstreams:
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.5)

    try:
        while not stop_event.is_set():
            peer_samples = {}

            # 1. Poll all upstream peers
            for host, port in state.upstreams:
                peer_key = f"{host}:{port}"
                t1 = clock.now()
                request = json.dumps({"t1": t1}).encode("utf-8")

                try:
                    with state.lock:
                        state.tx_count += 1
                    sock.sendto(request, (host, port))
                    data, _ = sock.recvfrom(1024)
                    with state.lock:
                        state.rx_count += 1
                    t4 = clock.now()

                    resp = json.loads(data.decode("utf-8"))
                    t2 = float(resp["t2"])
                    t3 = float(resp["t3"])
                    srv_stratum = resp.get("server_stratum", "?")

                    delay = (t4 - t1) - (t3 - t2)
                    theta = ((t2 - t1) + (t3 - t4)) / 2.0

                    peer_samples[peer_key] = {
                        "stratum": srv_stratum,
                        "delay": delay,
                        "theta": theta,
                        "t1": t1,
                        "t2": t2,
                        "t3": t3,
                        "t4": t4,
                        "status": "RESPONDED",
                    }
                except socket.timeout:
                    peer_samples[peer_key] = {"status": "TIMEOUT", "delay": 0.0, "theta": 0.0, "stratum": "?"}
                except Exception as exc:
                    peer_samples[peer_key] = {"status": f"ERROR: {exc}", "delay": 0.0, "theta": 0.0, "stratum": "?"}

            # 2. Evaluate Peers (Falseticker vs Truechimer Detection)
            responded_peers = {k: v for k, v in peer_samples.items() if v["status"] == "RESPONDED"}
            chosen_peer = None

            if len(responded_peers) == 1:
                p_key = list(responded_peers.keys())[0]
                responded_peers[p_key]["classification"] = "[TRUECHIMER]"
                responded_peers[p_key]["action"] = "SELECTED"
                chosen_peer = p_key

            elif len(responded_peers) >= 2:
                items = list(responded_peers.items())
                thetas = [v["theta"] for _, v in items]

                max_diff = max(thetas) - min(thetas)
                if max_diff > 5.0:
                    with state.lock:
                        baseline = state.prev_steady_offset

                    for p_key, data in items:
                        ref = baseline if baseline is not None else (sum(thetas) / len(thetas))
                        if abs(data["theta"] - ref) > 10.0:
                            data["classification"] = "[FALSETICKER]"
                            data["action"] = "DISCARDED (Outlier)"
                        else:
                            data["classification"] = "[TRUECHIMER]"
                            data["action"] = "CANDIDATE"

                    truechimers = [k for k, v in items if v.get("classification") == "[TRUECHIMER]"]
                    if truechimers:
                        chosen_peer = min(truechimers, key=lambda k: responded_peers[k]["delay"])
                        responded_peers[chosen_peer]["action"] = "SELECTED FOR SYNC"
                    else:
                        chosen_peer = min(responded_peers.keys(), key=lambda k: responded_peers[k]["delay"])
                        responded_peers[chosen_peer]["action"] = "FALLBACK"
                else:
                    for _, data in items:
                        data["classification"] = "[TRUECHIMER]"
                        data["action"] = "CANDIDATE"
                    chosen_peer = min(responded_peers.keys(), key=lambda k: responded_peers[k]["delay"])
                    responded_peers[chosen_peer]["action"] = "SELECTED (Lowest Delay)"

            state.update_peer_evaluations(peer_samples, chosen_peer)

            # 3. Apply clock adjustment based on chosen Truechimer
            if chosen_peer and chosen_peer in responded_peers:
                p_data = responded_peers[chosen_peer]
                theta_val = p_data["theta"]
                before_ts = clock.now()

                with state.lock:
                    mode = state.sync_mode

                if mode == "STEP":
                    clock.step(theta_val)
                else:
                    clock.start_slew(theta_val)

                after_ts = clock.now()

                state.record_sync(
                    t1=p_data["t1"],
                    t2=p_data["t2"],
                    t3=p_data["t3"],
                    t4=p_data["t4"],
                    delay=p_data["delay"],
                    theta=theta_val,
                    peer_name=chosen_peer,
                    before_ts=before_ts,
                    after_ts=after_ts,
                    mode=mode,
                )

            stop_event.wait(interval)
    finally:
        sock.close()


def generate_sparkline(values: List[float], width: int = 14) -> str:
    """Generate clean Unicode sparkline graph showing offset convergence."""
    if not values:
        return " " * width
    bars = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    trimmed = list(values)[-width:]
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
    """Render a clean ASCII/Unicode meter showing offset deviation."""
    abs_val = abs(offset)
    gauge = Text()

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
    """Renders visual ASCII topology hierarchy diagram highlighting current node."""
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


def build_dashboard(clock: VirtualClock, state: SharedState) -> Layout:
    """Build modern Rich TUI layout with minimal emojis and support for UI pause."""
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

    # Use frozen snapshot values if UI is paused (spacebar)
    if is_paused and snap is not None:
        now_os = snap["now_os"]
        now_virtual = snap["now_virtual"]
        current_offset = snap["offset"]
        drift_rate = snap["drift_rate"]
        slew_rem = snap["slew_rem"]
        slew_target = snap["slew_target"]
        slew_ratio = snap["slew_ratio"]
        reports = snap["peer_reports"]
        server_logs = snap["server_logs"]
        history_logs = snap["sync_history"]
        samples = snap["offset_samples"]
        tx = snap["tx"]
        rx = snap["rx"]
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
        1: ("[bold black on bright_yellow] STRATUM 1: ROOT MASTER [/]", "cyan"),
        2: ("[bold white on dodger_blue1] STRATUM 2: SECONDARY RELAY [/]", "dodger_blue1"),
        3: ("[bold white on dark_green] STRATUM 3: LEAF CLIENT [/]", "green"),
    }
    badge_title, border_color = stratum_badges.get(state.stratum, ("[UNKNOWN]", "white"))

    # Status / Fault indicator
    if is_paused:
        fault_badge = "[bold black on bright_yellow] [PAUSED - PRESS SPACE TO RESUME] [/bold black on bright_yellow]"
    elif is_falseticker:
        fault_badge = "[bold white on red] [FALSETICKER: +60s FAULT] [/bold white on red]"
    elif chaos_active:
        fault_badge = "[bold white on dark_magenta] [CHAOS ACTIVE: 35% GLITCHES] [/bold white on dark_magenta]"
    else:
        fault_badge = "[bold green][ONLINE SERVER][/bold green]" if state.stratum in (1, 2) else "[dim][CLIENT NODE][/dim]"

    # Dynamic drift indicator
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

    ip_info = f"[bold bright_white]{state.local_ip}[/bold bright_white]:[bold cyan]{state.port}[/bold cyan] ({state.hostname})"
    header_table.add_row(
        Text.from_markup(f"{badge_title}  |  IP: {ip_info}  |  {fault_badge}"),
        Text.from_markup(f"{mode_badge}  {drift_badge}  [dim](TX:{tx} RX:{rx})[/dim]"),
    )
    layout["header"].update(Panel(header_table, border_style=border_color, title="[bold]Network Time Protocol Simulation Node[/bold]"))

    # 2. CLOCKS & DRIFT SECTION
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

    # Visual Slew progress bar or Sparkline
    slew_display_elements = []
    if abs(slew_rem) > 0.001:
        slew_bar = ProgressBar(total=1.0, completed=slew_ratio, width=32, style="cyan", complete_style="bold cyan")
        slew_text = Text.from_markup(
            f"[bold cyan][SLEWING SMOOTHLY][/] Left: [bold]{slew_rem:+.3f}s[/bold] / Total: [dim]{slew_target:+.3f}s[/dim]  "
        )
        slew_display_elements.append(Table.grid().add_row(slew_text, slew_bar))
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
    if state.stratum == 1:
        srv_table = Table(title="Recent Inbound Client Synchronizations", expand=True, border_style="dim")
        srv_table.add_column("Timestamp", style="cyan", no_wrap=True)
        srv_table.add_column("Client IP:Port", style="bold white")
        srv_table.add_column("Emitted T2", style="yellow")
        srv_table.add_column("Emitted T3", style="green")
        srv_table.add_column("Integrity", justify="center")

        for item in server_logs:
            t2_str = VirtualClock.format_ts(item["t2"])
            t3_str = VirtualClock.format_ts(item["t3"])
            if item.get("is_fake"):
                integrity = f"[bold white on red] [FAKE {item.get('skew', 60):+.0f}s] [/bold white on red]"
            else:
                integrity = "[bold green][VALID][/bold green]"

            srv_table.add_row(
                VirtualClock.format_ts(item["time"]),
                item["client"],
                t2_str,
                t3_str,
                integrity,
            )

        status_box = Text.from_markup(
            f"[bold]Stratum Hierarchy:[/] {render_topology_diagram(1).markup}\n"
            f"[bold]Active Event:[/] [bright_yellow]{last_banner}[/bright_yellow]  |  "
            f"[bold]Total Served Clients:[/] [bright_yellow]{state.server_req_count}[/bright_yellow]"
        )
        layout["main"].update(Panel(Group(srv_table, status_box), border_style="bright_yellow", title="Master Server Engine"))

    else:
        layout["main"].split_row(
            Layout(name="peers", ratio=3),
            Layout(name="history", ratio=2),
        )

        peer_table = Table(title="Upstream Peer Telemetry & Fault Detection", expand=True, border_style="dim")
        peer_table.add_column("Upstream Peer", style="bold white", ratio=2)
        peer_table.add_column("Delay (delta)", justify="right", style="cyan", ratio=1)
        peer_table.add_column("Reported (theta)", justify="right", style="yellow", ratio=2)
        peer_table.add_column("Classification", justify="center", ratio=2)
        peer_table.add_column("Filter Action", justify="left", ratio=2)

        for p_key, p_data in reports.items():
            if p_data.get("status") == "RESPONDED":
                c_style = "bold green" if "TRUECHIMER" in p_data.get("classification", "") else "bold red"
                a_style = "bold bright_green" if "SELECTED" in p_data.get("action", "") else "dim"
                peer_table.add_row(
                    p_key,
                    f"{p_data['delay'] * 1000:+.2f}ms",
                    f"{p_data['theta']:+.4f}s",
                    Text(p_data.get("classification", "?"), style=c_style),
                    Text(p_data.get("action", "?"), style=a_style),
                )
            else:
                peer_table.add_row(p_key, "--", "--", "[dim]NO RESPONSE[/dim]", f"[red]{p_data.get('status')}[/red]")

        topo_and_status = Text.from_markup(
            f"[bold]Hierarchy:[/] {render_topology_diagram(state.stratum).markup}\n"
            f"[bold]Latest Event:[/] [bright_yellow]{last_banner}[/bright_yellow]"
        )
        layout["main"]["peers"].update(Panel(Group(peer_table, topo_and_status), border_style="dodger_blue1", title="Peer Cross-Validation"))

        if state.stratum == 2 and state.server_req_count > 0:
            srv_table = Table(title="Recent Inbound Client Synchronizations", expand=True, border_style="dim")
            srv_table.add_column("Client IP:Port", style="bold white")
            srv_table.add_column("Emitted T2", style="yellow")
            srv_table.add_column("Emitted T3", style="green")
            srv_table.add_column("Integrity", justify="center")

            for item in server_logs:
                integrity = f"[bold white on red] [FAKE {item.get('skew', 60):+.0f}s] [/bold white on red]" if item.get("is_fake") else "[bold green][VALID][/bold green]"
                srv_table.add_row(
                    item["client"],
                    VirtualClock.format_ts(item["t2"]),
                    VirtualClock.format_ts(item["t3"]),
                    integrity,
                )
            layout["main"]["history"].update(Panel(srv_table, border_style="bright_yellow", title="Inbound Downstream Requests"))
        else:
            hist_table = Table(title="Convergence Rounds", expand=True, border_style="dim")
            hist_table.add_column("Time", style="dim", no_wrap=True)
            hist_table.add_column("Peer", style="white")
            hist_table.add_column("Mode", style="cyan")
            hist_table.add_column("Offset (theta)", justify="right", style="bold yellow")

            for h in history_logs:
                hist_table.add_row(
                    VirtualClock.format_ts(h["time"]),
                    h["peer"].split(":")[0],
                    h["mode"],
                    f"{h['theta_s']:+.4f}s",
                )

            layout["main"]["history"].update(Panel(hist_table, border_style="magenta", title="Sync History"))

    # 4. FOOTER WITH INTERACTIVE HOTKEYS
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


def key_listener_worker(state: SharedState, clock: VirtualClock, stop_event: threading.Event) -> None:
    """Listens for single keypresses to trigger runtime toggles without pressing Enter."""
    reader = KeyReader()
    try:
        while not stop_event.is_set():
            ch = reader.read_key(timeout=0.1)
            if not ch:
                continue
            if ch == " ":
                state.toggle_ui_pause(clock)
            else:
                key = ch.lower()
                if key == "f":
                    state.toggle_falseticker()
                elif key == "c":
                    state.toggle_chaos()
                elif key == "m":
                    state.toggle_sync_mode()
                elif key == "d":
                    active = clock.toggle_drift()
                    with state.lock:
                        state.last_action_banner = "Dynamic Brownian Drift Enabled" if active else "Drift Disabled"
                elif key == "q":
                    stop_event.set()
                    break
    finally:
        reader.restore()


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
    parser = argparse.ArgumentParser(description="NTP Stratum Simulation Node (Clean Engineering TUI)")
    parser.add_argument("--stratum", type=int, required=True, choices=[1, 2, 3], help="Node stratum level (1, 2, or 3)")
    parser.add_argument("--port", type=int, default=5000, help="UDP port to listen on (Stratum 1 & 2, default: 5000)")
    parser.add_argument(
        "--upstream",
        type=parse_upstream,
        nargs="+",
        help="One or more upstream server HOST:PORT (e.g. 192.168.1.11:5000 192.168.1.12:5000)",
    )
    parser.add_argument("--interval", type=float, default=4.0, help="Sync interval in seconds (default: 4.0)")
    parser.add_argument("--no-ui", action="store_true", help="Disable Rich TUI and output simple logs")
    args = parser.parse_args()

    if args.stratum > 1 and not args.upstream:
        parser.error(f"--upstream is required when --stratum is {args.stratum}")

    upstreams = args.upstream if args.upstream else []

    if args.stratum == 1:
        initial_offset = 0.0
    else:
        # Initial random offset between -30s and +30s
        initial_offset = random.choice([-1, 1]) * random.uniform(5.0, 30.0)

    clock = VirtualClock(initial_offset=initial_offset)
    state = SharedState(stratum=args.stratum, port=args.port, upstreams=upstreams)
    stop_event = threading.Event()
    threads: list[threading.Thread] = []

    # Start Clock Tick worker for continuous dynamic drift and slewing
    ticker_thread = threading.Thread(target=clock_tick_worker, args=(clock, stop_event), daemon=True)
    ticker_thread.start()
    threads.append(ticker_thread)

    # Start Keyboard listener thread
    key_thread = threading.Thread(target=key_listener_worker, args=(state, clock, stop_event), daemon=True)
    key_thread.start()
    threads.append(key_thread)

    # Start Server thread if Stratum 1 or 2
    if args.stratum in (1, 2):
        srv_thread = threading.Thread(target=run_server, args=(clock, state, stop_event), daemon=True)
        srv_thread.start()
        threads.append(srv_thread)

    # Start Client thread if Stratum 2 or 3
    if args.stratum in (2, 3) and state.upstreams:
        cli_thread = threading.Thread(target=run_client, args=(clock, state, args.interval, stop_event), daemon=True)
        cli_thread.start()
        threads.append(cli_thread)

    # UI RENDERING LOOP
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
        print(f"=== NTP Node Started (Stratum {args.stratum}) | Node IP: {state.local_ip}:{state.port} ===")
        print("Hotkeys: [SPACE] Freeze UI | [F] Manual Falseticker | [C] Auto Chaos | [M] Slew/Step | [D] Drift | [Q] Quit")
        try:
            while not stop_event.is_set():
                drift_str = f"Drift: {clock.get_drift_rate() * 1000:+.1f}ms/s" if clock.is_drift_active() else "Drift: OFF"
                fake_info = f" (Broadcasted Fake: {VirtualClock.format_ts(clock.now() + state.falseticker_skew)})" if state.falseticker_mode else ""
                paused_str = " [UI PAUSED]" if state.ui_paused else ""
                print(
                    f"[{VirtualClock.format_ts(clock.now())}] Offset: {clock.get_offset():+.6f}s "
                    f"| Mode: {state.sync_mode} | {drift_str} | Falseticker: {state.falseticker_mode}{fake_info}{paused_str}"
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
