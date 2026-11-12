"""Thread-safe telemetry store and UI freeze snapshot state manager."""
from __future__ import annotations

import collections
import socket
import threading
import time
from typing import Any, Deque, Dict, List, Optional, Tuple, Union

from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.models.config import NodeConfig
from ntp_simulation.models.peer import PeerAddress, PeerEvaluation
from ntp_simulation.models.telemetry import (
    ServerLogEntry,
    SyncHistoryEntry,
    TelemetrySnapshot,
)
from ntp_simulation.network.socket_utils import get_local_ip


class SharedState:
    """Thread-safe data store for telemetry, background workers, and UI state.

    Protects all mutations and reads across concurrent server, client, and UI threads
    using a reentrant lock (RLock). Supports UI freeze snapshot isolation.
    """

    def __init__(
        self,
        config_or_stratum: Optional[Union[NodeConfig, int]] = None,
        port: Optional[int] = None,
        upstreams: Optional[List[Any]] = None,
        *,
        config: Optional[NodeConfig] = None,
        stratum: Optional[int] = None,
        bind_host: str = "0.0.0.0",
        sync_mode: str = "STEP",
        falseticker_skew: float = 60.0,
        **kwargs: Any,
    ) -> None:
        """Initializes SharedState from a NodeConfig instance or positional/keyword arguments.

        Supports both modern NodeConfig injection and legacy (stratum, port, upstreams) parameters.
        """
        self.lock = threading.RLock()

        resolved_config: Optional[NodeConfig] = None
        if isinstance(config_or_stratum, NodeConfig):
            resolved_config = config_or_stratum
        elif config is not None:
            resolved_config = config

        if resolved_config is not None:
            self.stratum = resolved_config.stratum
            self.port = resolved_config.port
            self.bind_host = resolved_config.bind_host
            self.upstreams = list(resolved_config.upstreams)
            self.sync_mode = resolved_config.sync_mode
            self.falseticker_skew = resolved_config.falseticker_skew
            self.dispersion_floor = resolved_config.dispersion_floor
        else:
            if isinstance(config_or_stratum, int):
                self.stratum = config_or_stratum
            elif stratum is not None:
                self.stratum = stratum
            else:
                self.stratum = 1

            self.port = port if port is not None else 5000
            self.bind_host = bind_host
            self.sync_mode = sync_mode
            self.falseticker_skew = falseticker_skew
            self.dispersion_floor = kwargs.get("dispersion_floor", 0.050)

            parsed_upstreams: List[PeerAddress] = []
            raw_upstreams = upstreams if upstreams is not None else []
            for item in raw_upstreams:
                if isinstance(item, PeerAddress):
                    parsed_upstreams.append(item)
                elif isinstance(item, str):
                    parsed_upstreams.append(PeerAddress.parse(item))
                elif isinstance(item, (tuple, list)) and len(item) == 2:
                    parsed_upstreams.append(PeerAddress(host=str(item[0]), port=int(item[1])))
            self.upstreams = parsed_upstreams

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
        self.last_sync_time: Optional[float] = None
        self.last_delay: Optional[float] = None
        self.last_theta: Optional[float] = None
        self.prev_steady_offset: Optional[float] = None

        # Timestamps for transit measurements
        self.t1: Optional[float] = None
        self.t2: Optional[float] = None
        self.t3: Optional[float] = None
        self.t4: Optional[float] = None

        # Ring buffers (fixed maximum capacity)
        self.recent_server_logs: Deque[ServerLogEntry] = collections.deque(maxlen=10)
        self.sync_history: Deque[SyncHistoryEntry] = collections.deque(maxlen=10)
        self.offset_samples: Deque[float] = collections.deque(maxlen=30)

    def toggle_ui_pause(self, clock: VirtualClock) -> bool:
        """Freezes or unfreezes UI rendering while background processes run uninterrupted.

        Captures an immutable TelemetrySnapshot when pausing. Clears the snapshot
        when resuming live rendering.
        """
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
            entry = ServerLogEntry(
                time=time.time(),
                client=str(client),
                t2=float(t2),
                t3=float(t3),
                is_fake=bool(is_fake),
                skew=float(skew),
            )
            self.recent_server_logs.appendleft(entry)

    def update_peer_evaluations(
        self,
        reports: Dict[str, Union[PeerEvaluation, Dict[str, Any]]],
        chosen_peer: Optional[str] = None,
    ) -> None:
        """Updates full peer evaluation dictionary and selected best truechimer peer."""
        with self.lock:
            validated_reports: Dict[str, PeerEvaluation] = {}
            for k, v in reports.items():
                if isinstance(v, PeerEvaluation):
                    validated_reports[k] = v
                elif isinstance(v, dict):
                    validated_reports[k] = PeerEvaluation(**v)
                else:
                    raise TypeError(f"Invalid evaluation item type: {type(v)}")
            self.peer_reports = validated_reports
            self.selected_peer = chosen_peer

    def update_peer_report(
        self,
        peer_key: str,
        eval_data: Union[PeerEvaluation, Dict[str, Any]],
    ) -> None:
        """Updates or adds evaluation for a single peer key."""
        with self.lock:
            if isinstance(eval_data, PeerEvaluation):
                self.peer_reports[peer_key] = eval_data
            elif isinstance(eval_data, dict):
                self.peer_reports[peer_key] = PeerEvaluation(**eval_data)
            else:
                raise TypeError(f"Invalid eval_data type: {type(eval_data)}")

    def add_offset_sample(self, offset: float) -> None:
        """Appends offset sample to rolling history ring buffer."""
        with self.lock:
            self.offset_samples.append(float(offset))

    def record_sync(
        self,
        *args: Any,
        peer: Optional[str] = None,
        peer_name: Optional[str] = None,
        delay_ms: Optional[float] = None,
        delay: Optional[float] = None,
        theta_s: Optional[float] = None,
        theta: Optional[float] = None,
        mode: str = "STEP",
        before: Optional[float] = None,
        before_ts: Optional[float] = None,
        after: Optional[float] = None,
        after_ts: Optional[float] = None,
        t1: Optional[float] = None,
        t2: Optional[float] = None,
        t3: Optional[float] = None,
        t4: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """Records clock adjustment event, updating synchronization telemetry and history.

        Accepts both positional and keyword argument variations:
        - (peer, delay_ms, theta_s, mode, before, after)
        - (peer_name, delay, theta, mode, before_ts, after_ts)
        - (t1, t2, t3, t4, delay, theta, peer_name, before_ts, after_ts, mode)
        """
        with self.lock:
            # Handle positional parameter mappings
            if len(args) == 10:
                t1, t2, t3, t4, delay, theta, peer_name, before_ts, after_ts, mode = args
            elif len(args) == 6:
                p_arg, d_arg, th_arg, m_arg, b_arg, a_arg = args
                peer = str(p_arg)
                mode = str(m_arg)
                # Check whether d_arg is in ms or seconds
                if delay_ms is None and delay is None:
                    if float(d_arg) >= 1.0 or "delay_ms" in kwargs:
                        delay_ms = float(d_arg)
                    else:
                        delay = float(d_arg)
                theta_s = float(th_arg)
                before = float(b_arg)
                after = float(a_arg)

            resolved_peer = peer_name or peer or self.selected_peer or "unknown"
            resolved_mode = mode or self.sync_mode

            resolved_theta = (
                float(theta_s)
                if theta_s is not None
                else (float(theta) if theta is not None else 0.0)
            )

            if delay_ms is not None:
                final_delay_ms = float(delay_ms)
                final_delay_sec = final_delay_ms / 1000.0
            elif delay is not None:
                final_delay_sec = float(delay)
                final_delay_ms = final_delay_sec * 1000.0
            else:
                final_delay_sec = 0.0
                final_delay_ms = 0.0

            resolved_before = (
                float(before)
                if before is not None
                else (float(before_ts) if before_ts is not None else 0.0)
            )
            resolved_after = (
                float(after)
                if after is not None
                else (float(after_ts) if after_ts is not None else 0.0)
            )

            now = time.time()
            self.last_sync_time = now
            self.last_delay = final_delay_sec
            self.last_theta = resolved_theta
            self.selected_peer = resolved_peer
            self.sync_count += 1
            self.prev_steady_offset = resolved_theta
            self.offset_samples.append(resolved_theta)

            if t1 is not None:
                self.t1 = float(t1)
            if t2 is not None:
                self.t2 = float(t2)
            if t3 is not None:
                self.t3 = float(t3)
            if t4 is not None:
                self.t4 = float(t4)

            if resolved_mode == "STEP":
                self.last_action_banner = f"[STEP APPLIED] Clock shifted {resolved_theta:+.4f}s"
            else:
                self.last_action_banner = (
                    f"[SLEW ACTIVE] Adapting clock smoothly toward {resolved_theta:+.4f}s"
                )

            self.sync_history.appendleft(
                SyncHistoryEntry(
                    time=now,
                    peer=resolved_peer,
                    delay_ms=final_delay_ms,
                    theta_s=resolved_theta,
                    mode=resolved_mode,
                    before=resolved_before,
                    after=resolved_after,
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

        validated_reports: Dict[str, PeerEvaluation] = {}
        for k, v in self.peer_reports.items():
            if isinstance(v, PeerEvaluation):
                validated_reports[k] = v
            elif isinstance(v, dict):
                validated_reports[k] = PeerEvaluation(**v)

        return TelemetrySnapshot(
            pause_time=now_os,
            now_os=now_os,
            now_virtual=now_virtual,
            offset=clock.get_offset(),
            drift_rate=clock.get_drift_rate(),
            slew_rem=slew_rem,
            slew_target=slew_target,
            slew_ratio=slew_ratio,
            peer_reports=validated_reports,
            server_logs=list(self.recent_server_logs),
            sync_history=list(self.sync_history),
            offset_samples=list(self.offset_samples),
            tx_count=self.tx_count,
            rx_count=self.rx_count,
        )

    def reset_counters(self) -> None:
        """Resets telemetry packet counters and action banner."""
        with self.lock:
            self.tx_count = 0
            self.rx_count = 0
            self.server_req_count = 0
            self.sync_count = 0
            self.last_action_banner = "Counters reset to zero."
