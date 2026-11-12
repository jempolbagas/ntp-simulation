"""Telemetry data structures and frozen snapshots."""
from __future__ import annotations

from typing import Dict, List
from pydantic import BaseModel, Field
from ntp_simulation.models.peer import PeerEvaluation


class ServerLogEntry(BaseModel):
    """Inbound client request telemetry record."""

    time: float = Field(..., description="Server processing timestamp")
    client: str = Field(..., description="Client address host:port")
    t2: float = Field(..., description="Server receive timestamp")
    t3: float = Field(..., description="Server transmit timestamp")
    is_fake: bool = Field(default=False, description="Whether fault skew was applied")
    skew: float = Field(default=0.0, description="Skew applied in seconds")


class SyncHistoryEntry(BaseModel):
    """Historical record of clock synchronization adjustment."""

    time: float = Field(..., description="Sync event timestamp")
    peer: str = Field(..., description="Selected upstream peer identifier")
    delay_ms: float = Field(..., description="Round-trip delay in milliseconds")
    theta_s: float = Field(..., description="Measured clock offset in seconds")
    mode: str = Field(..., description="Adjustment mode: STEP or SLEW")
    before: float = Field(..., description="Virtual clock time before adjustment")
    after: float = Field(..., description="Virtual clock time after adjustment")


class TelemetrySnapshot(BaseModel):
    """Immutable snapshot captured when UI is paused via [SPACE]."""

    pause_time: float = Field(..., description="Timestamp when UI was paused")
    now_os: float = Field(..., description="Host OS timestamp")
    now_virtual: float = Field(..., description="Virtual clock timestamp")
    offset: float = Field(..., description="Current clock offset in seconds")
    drift_rate: float = Field(..., description="Instantaneous drift rate in s/s")
    slew_rem: float = Field(..., description="Remaining slew adjustment in seconds")
    slew_target: float = Field(..., description="Total queued slew adjustment in seconds")
    slew_ratio: float = Field(..., description="Slew completion progress ratio [0.0, 1.0]")
    peer_reports: Dict[str, PeerEvaluation] = Field(
        default_factory=dict, description="Peer evaluation reports"
    )
    server_logs: List[ServerLogEntry] = Field(
        default_factory=list, description="Recent server log entries"
    )
    sync_history: List[SyncHistoryEntry] = Field(
        default_factory=list, description="Recent synchronization records"
    )
    offset_samples: List[float] = Field(
        default_factory=list, description="Rolling history of offset measurements"
    )
    tx_count: int = Field(default=0, description="Total transmitted packets")
    rx_count: int = Field(default=0, description="Total received packets")
