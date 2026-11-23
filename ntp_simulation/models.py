"""Pydantic data models for NTP packets, peers, node configuration, and telemetry."""
from __future__ import annotations

import secrets
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NTPRequest(BaseModel):
    """Client NTP request packet.

    Attributes:
        t1: Client departure timestamp (Unix epoch float).
        nonce: Cryptographically random hex string to correlate response and prevent replay.
        client_id: Optional identifier (host:port or hostname).
    """

    t1: float = Field(..., description="Client transmit timestamp (epoch seconds)")
    nonce: str = Field(..., min_length=8, description="Random correlation nonce")
    client_id: Optional[str] = Field(default=None, description="Optional client identifier")

    @classmethod
    def create(
        cls,
        t1: float,
        nonce: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> NTPRequest:
        """Helper factory creating an NTPRequest with an auto-generated nonce."""
        if nonce is None:
            nonce = secrets.token_hex(8)
        return cls(t1=t1, nonce=nonce, client_id=client_id)

    def to_bytes(self) -> bytes:
        """Serializes model to UTF-8 encoded JSON bytes for UDP transmission."""
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, data: Union[bytes, bytearray, str]) -> NTPRequest:
        """Deserializes and validates model from raw UDP bytes or JSON string."""
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        return cls.model_validate_json(data)


class NTPResponse(BaseModel):
    """Server NTP response packet echoing client timestamp and nonce."""

    t1: float = Field(..., description="Echoed client transmit timestamp")
    t2: float = Field(..., description="Server receive timestamp")
    t3: float = Field(..., description="Server transmit timestamp")
    nonce: str = Field(..., min_length=8, description="Echoed correlation nonce")
    server_stratum: int = Field(..., ge=1, le=16, description="Server stratum level")
    server_id: Optional[str] = Field(default=None, description="Server identifier host:port")
    is_falseticker: bool = Field(default=False, description="Fault injection status")
    skew_applied: float = Field(default=0.0, description="Skew applied in seconds")

    def to_bytes(self) -> bytes:
        """Serializes model to UTF-8 encoded JSON bytes for UDP transmission."""
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, data: Union[bytes, bytearray, str]) -> NTPResponse:
        """Deserializes and validates model from raw UDP bytes or JSON string."""
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        return cls.model_validate_json(data)


class PeerAddress(BaseModel):
    """Represents a network peer address (host:port)."""

    model_config = ConfigDict(frozen=True)

    host: str = Field(..., min_length=1, description="IP address or hostname")
    port: int = Field(..., ge=1, le=65535, description="UDP port number")

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None, **kwargs: Any) -> None:
        if host is not None:
            kwargs.setdefault("host", host)
        if port is not None:
            kwargs.setdefault("port", port)
        super().__init__(**kwargs)

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    def __str__(self) -> str:
        return self.key

    def __repr__(self) -> str:
        return f"PeerAddress(host='{self.host}', port={self.port})"

    @classmethod
    def parse(cls, value: str) -> PeerAddress:
        """Parses 'host:port' string into a PeerAddress instance."""
        if ":" not in value:
            raise ValueError(f"Invalid peer address format '{value}', expected 'HOST:PORT'")
        host, port_str = value.rsplit(":", 1)
        return cls(host=host, port=int(port_str))

    def to_tuple(self) -> Tuple[str, int]:
        return (self.host, self.port)


class PeerSample(BaseModel):
    """Raw round-trip network measurement from a peer polling cycle or algorithm input."""

    model_config = ConfigDict(populate_by_name=True)

    peer: Optional[PeerAddress] = None
    peer_id_val: Optional[str] = Field(default=None, alias="peer_id")
    t1: Optional[float] = None
    t2: Optional[float] = None
    t3: Optional[float] = None
    t4: Optional[float] = None
    theta_val: Optional[float] = Field(default=None, alias="theta")
    delay_val: Optional[float] = Field(default=None, alias="delay")
    server_stratum: Optional[int] = Field(default=1, alias="stratum")
    nonce_matched: bool = True
    status: str = "RESPONDED"

    @property
    def peer_id(self) -> str:
        if self.peer_id_val:
            return self.peer_id_val
        if self.peer is not None:
            return self.peer.key
        return "unknown"

    @property
    def delay(self) -> float:
        """Round trip network delay delta = (t4 - t1) - (t3 - t2)."""
        if self.delay_val is not None:
            return float(self.delay_val)
        if (
            self.t1 is not None
            and self.t2 is not None
            and self.t3 is not None
            and self.t4 is not None
        ):
            return float((self.t4 - self.t1) - (self.t3 - self.t2))
        return 0.0

    @property
    def theta(self) -> float:
        """Clock offset theta = ((t2 - t1) + (t3 - t4)) / 2.0."""
        if self.theta_val is not None:
            return float(self.theta_val)
        if (
            self.t1 is not None
            and self.t2 is not None
            and self.t3 is not None
            and self.t4 is not None
        ):
            return float(((self.t2 - self.t1) + (self.t3 - self.t4)) / 2.0)
        return 0.0

    @property
    def interval(self) -> Tuple[float, float]:
        """Marzullo confidence interval [theta - delay/2, theta + delay/2]."""
        eff_delay = max(0.0, self.delay)
        return (self.theta - eff_delay / 2.0, self.theta + eff_delay / 2.0)


class PeerEvaluation(BaseModel):
    """Algorithmic evaluation of a peer following Marzullo filtering."""

    peer_key: str
    stratum: Optional[int] = None
    delay: float
    theta: float
    status: str
    classification: str = "[UNKNOWN]"
    action: str = "PENDING"
    t1: Optional[float] = None
    t2: Optional[float] = None
    t3: Optional[float] = None
    t4: Optional[float] = None
    interval: Optional[Tuple[float, float]] = None


class NodeConfig(BaseModel):
    """Runtime configuration for an NTP node."""

    stratum: int = Field(..., ge=1, le=3, description="Node stratum level (1, 2, or 3)")
    port: int = Field(default=5000, ge=1, le=65535, description="UDP listening port")
    bind_host: str = Field(default="0.0.0.0", description="UDP bind IP")
    upstreams: List[PeerAddress] = Field(
        default_factory=list, description="List of upstream peers"
    )
    interval: float = Field(default=4.0, gt=0.1, description="Polling interval in seconds")
    no_ui: bool = Field(default=False, description="Disable Rich TUI dashboard")
    sync_mode: str = Field(default="STEP", pattern="^(STEP|SLEW|AUTO)$")
    sync_os_clock: bool = Field(
        default=False,
        description="Discipline host OS clock (Linux/Windows)",
    )
    step_threshold: float = Field(
        default=0.128,
        gt=0.0,
        description="AUTO mode step threshold in seconds (RFC 5905)",
    )
    slew_rate: float = Field(default=0.40, gt=0.0, description="Slew adaptation rate in s/s")
    falseticker_skew: float = Field(
        default=60.0, description="Skew injected in falseticker mode in seconds"
    )
    chaos_probability: float = Field(default=0.35, ge=0.0, le=1.0)
    chaos_min_skew: float = Field(default=30.0)
    chaos_max_skew: float = Field(default=90.0)
    socket_timeout: float = Field(default=1.5, gt=0.0)
    dispersion_floor: float = Field(
        default=0.050,
        ge=0.0,
        description="Minimum dispersion/uncertainty floor in seconds (RFC 5905)",
    )

    @model_validator(mode="before")
    @classmethod
    def default_auto_mode_for_os_clock(cls, data: Any) -> Any:
        """Defaults sync_mode to AUTO when sync_os_clock is enabled and mode is omitted."""
        if isinstance(data, dict) and data.get("sync_os_clock"):
            if "sync_mode" not in data or data.get("sync_mode") is None:
                updated = dict(data)
                updated["sync_mode"] = "AUTO"
                return updated
        return data

    @field_validator("upstreams", mode="before")
    @classmethod
    def parse_upstreams(cls, v: Any) -> Any:
        """Coerces strings or tuples into PeerAddress instances."""
        if isinstance(v, list):
            parsed: List[Union[PeerAddress, Any]] = []
            for item in v:
                if isinstance(item, str):
                    parsed.append(PeerAddress.parse(item))
                elif isinstance(item, (tuple, list)) and len(item) == 2:
                    parsed.append(PeerAddress(host=str(item[0]), port=int(item[1])))
                else:
                    parsed.append(item)
            return parsed
        return v

    @model_validator(mode="after")
    def validate_upstreams_for_stratum(self) -> NodeConfig:
        """Enforces that non-master stratum nodes configure at least one upstream peer."""
        self.upstreams = list(dict.fromkeys(self.upstreams))
        if self.stratum > 1 and not self.upstreams:
            raise ValueError(
                f"Stratum {self.stratum} node requires at least one upstream peer"
            )
        return self


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
    selected_peer: Optional[str] = Field(
        default=None, description="Currently selected truechimer peer"
    )
    synced_os_offset: Optional[float] = Field(
        default=None, description="Expected VirtualClock OS offset when aligned with upstream"
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
