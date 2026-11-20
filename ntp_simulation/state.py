"""Thread-safe telemetry store and UI freeze snapshot state manager."""
from __future__ import annotations

import collections
import socket
import threading
import time
from typing import Deque, Dict, Optional

from ntp_simulation.clock import VirtualClock
from ntp_simulation.models import (
    NodeConfig,
    PeerEvaluation,
    ServerLogEntry,
    SyncHistoryEntry,
    TelemetrySnapshot,
)
from ntp_simulation.network import get_local_ip


class SharedState:
    """Thread-safe data store for telemetry, background workers, and UI state.

    Protects all mutations and reads across concurrent server, client, and UI threads
    using a reentrant lock (RLock). Supports UI freeze snapshot isolation.
    """

    def __init__(self, config: NodeConfig) -> None:
        self.lock = threading.RLock()
        self.stratum = config.stratum
        self.port = config.port
        self.bind_host = config.bind_host
        self.upstreams = list(config.upstreams)
        self.sync_mode = config.sync_mode
        self.falseticker_skew = config.falseticker_skew
        self.chaos_probability = config.chaos_probability
        self.chaos_min_skew = config.chaos_min_skew
        self.chaos_max_skew = config.chaos_max_skew
        self.socket_timeout = config.socket_timeout
        self.dispersion_floor = config.dispersion_floor

        self.local_ip = get_local_ip()
        self.hostname = socket.gethostname()

        # UI Freeze / Pause State [SPACE]
        self.ui_paused: bool = False
        self.frozen_snapshot: Optional[TelemetrySnapshot] = None

        # Interactive toggles
        self.falseticker_mode: bool = False
        self.chaos_mode: bool = False

        # Packet and transaction counters
        self.tx_count: int = 0
        self.rx_count: int = 0
        self.server_req_count: int = 0
        self.sync_count: int = 0
        self.last_action_banner: str = "System Initialized. Awaiting synchronization cycles."

        # Telemetry & Multi-Peer State
        self.peer_reports: Dict[str, PeerEvaluation] = {}
        self.selected_peer: Optional[str] = None
        self.synced_os_offset: Optional[float] = None

        # Ring buffers (fixed maximum capacity)
        self.recent_server_logs: Deque[ServerLogEntry] = collections.deque(maxlen=10)
        self.sync_history: Deque[SyncHistoryEntry] = collections.deque(maxlen=10)
        self.offset_samples: Deque[float] = collections.deque(maxlen=30)

    def toggle_ui_pause(self, clock: VirtualClock) -> bool:
        """Freezes or unfreezes UI rendering while background processes run uninterrupted."""
        with self.lock:
            self.ui_paused = not self.ui_paused
            if self.ui_paused:
                self.frozen_snapshot = self._build_snapshot(clock)
                self.last_action_banner = (
                    "UI TELEMETRY FROZEN (Background sync processes continue running)"
                )
            else:
                self.frozen_snapshot = None
                self.last_action_banner = "UI Telemetry Resumed"
            return self.ui_paused

    def toggle_falseticker(self) -> bool:
        """Toggles manual falseticker fault injection (+skew seconds offset)."""
        with self.lock:
            self.falseticker_mode = not self.falseticker_mode
            status = f"ACTIVE (+{self.falseticker_skew:.1f}s)" if self.falseticker_mode else "DISABLED (Honest)"
            self.last_action_banner = f"Manual Falseticker Mode: {status}"
            return self.falseticker_mode

    def toggle_chaos(self) -> bool:
        """Toggles random timing chaos mode (35% probability of ±30-90s anomaly)."""
        with self.lock:
            self.chaos_mode = not self.chaos_mode
            status = "ENABLED (35% Sporadic Glitches)" if self.chaos_mode else "DISABLED"
            self.last_action_banner = f"Random Chaos Mode: {status}"
            return self.chaos_mode

    def toggle_sync_mode(self) -> str:
        """Toggles clock synchronization adjustment mode between STEP and SLEW."""
        with self.lock:
            self.sync_mode = "SLEW" if self.sync_mode == "STEP" else "STEP"
            self.last_action_banner = f"Sync Algorithm switched to: {self.sync_mode} Mode"
            return self.sync_mode

    def record_server_request(
        self,
        client: str,
        t2: float,
        t3: float,
        is_fake: bool = False,
        skew: float = 0.0,
    ) -> None:
        """Records an incoming client NTP request and server response timestamps."""
        with self.lock:
            self.server_req_count += 1
            self.tx_count += 1
            self.rx_count += 1
            self.recent_server_logs.appendleft(
                ServerLogEntry(
                    time=time.time(),
                    client=str(client),
                    t2=float(t2),
                    t3=float(t3),
                    is_fake=bool(is_fake),
                    skew=float(skew),
                )
            )

    def update_peer_evaluations(
        self,
        reports: Dict[str, PeerEvaluation],
        chosen_peer: Optional[str] = None,
    ) -> None:
        """Updates full peer evaluation dictionary and selected best truechimer peer."""
        with self.lock:
            self.peer_reports = dict(reports)
            self.selected_peer = chosen_peer

    def add_offset_sample(self, offset: float) -> None:
        """Appends offset sample to rolling history ring buffer."""
        with self.lock:
            self.offset_samples.append(float(offset))

    def record_sync(
        self,
        *,
        peer: str,
        delay: float,
        theta: float,
        mode: str = "STEP",
        before: float = 0.0,
        after: float = 0.0,
        os_offset: Optional[float] = None,
    ) -> None:
        """Records clock adjustment event, updating synchronization telemetry and history."""
        with self.lock:
            now = time.time()
            self.selected_peer = peer
            if os_offset is not None:
                self.synced_os_offset = float(os_offset) + (0.0 if mode == "STEP" else float(theta))
            self.sync_count += 1
            self.offset_samples.append(float(theta))

            if mode == "STEP":
                self.last_action_banner = f"[STEP APPLIED] Clock shifted {theta:+.4f}s"
            else:
                self.last_action_banner = f"[SLEW ACTIVE] Adapting clock smoothly toward {theta:+.4f}s"

            self.sync_history.appendleft(
                SyncHistoryEntry(
                    time=now,
                    peer=peer,
                    delay_ms=float(delay) * 1000.0,
                    theta_s=float(theta),
                    mode=mode,
                    before=float(before),
                    after=float(after),
                )
            )

    def get_snapshot(self, clock: Optional[VirtualClock] = None) -> Optional[TelemetrySnapshot]:
        """Returns frozen TelemetrySnapshot if UI is paused, or live snapshot if clock is provided."""
        with self.lock:
            if self.ui_paused and self.frozen_snapshot is not None:
                return self.frozen_snapshot
            if clock is not None:
                return self._build_snapshot(clock)
            return None

    def _build_snapshot(self, clock: VirtualClock) -> TelemetrySnapshot:
        """Internal helper constructing a consistent TelemetrySnapshot under lock."""
        now_os = time.time()
        now_virtual = clock.now()
        slew_rem, slew_target, slew_ratio = clock.get_slew_info()

        return TelemetrySnapshot(
            pause_time=now_os,
            now_os=now_os,
            now_virtual=now_virtual,
            offset=clock.get_offset(),
            drift_rate=clock.get_drift_rate(),
            slew_rem=slew_rem,
            slew_target=slew_target,
            slew_ratio=slew_ratio,
            peer_reports=dict(self.peer_reports),
            selected_peer=self.selected_peer,
            synced_os_offset=self.synced_os_offset,
            server_logs=list(self.recent_server_logs),
            sync_history=list(self.sync_history),
            offset_samples=list(self.offset_samples),
            tx_count=self.tx_count,
            rx_count=self.rx_count,
        )
