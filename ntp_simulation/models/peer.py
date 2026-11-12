"""Peer representations and evaluation status schemas."""
from __future__ import annotations

from typing import Any, Optional, Tuple, Union
from pydantic import BaseModel, ConfigDict, Field


class PeerAddress(BaseModel):
    """Represents a network peer address (host:port)."""

    model_config = ConfigDict(frozen=True)

    host: str = Field(..., min_length=1, description="IP address or hostname")
    port: int = Field(..., ge=1, le=65535, description="UDP port number")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if args:
            if len(args) == 1 and isinstance(args[0], str) and ":" in args[0]:
                h, p = args[0].rsplit(":", 1)
                kwargs.setdefault("host", h)
                kwargs.setdefault("port", int(p))
            else:
                field_names = ["host", "port"]
                for i, val in enumerate(args):
                    if i < len(field_names):
                        kwargs.setdefault(field_names[i], val)
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
    status: str = "RESPONDED"  # RESPONDED, TIMEOUT, NONCE_MISMATCH, ERROR: ...

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if args:
            first = args[0]
            if isinstance(first, str):
                if ":" in first:
                    try:
                        kwargs.setdefault("peer", PeerAddress.parse(first))
                    except Exception:
                        kwargs.setdefault("peer_id", first)
                else:
                    kwargs.setdefault("peer_id", first)
            elif isinstance(first, PeerAddress):
                kwargs.setdefault("peer", first)

            if len(args) == 3:
                # (peer, theta, delay)
                kwargs.setdefault("theta", args[1])
                kwargs.setdefault("delay", args[2])
            elif len(args) >= 4:
                # (peer, t1, t2, t3, t4, ...)
                field_names = [
                    "peer",
                    "t1",
                    "t2",
                    "t3",
                    "t4",
                    "server_stratum",
                    "nonce_matched",
                    "status",
                ]
                for i, val in enumerate(args[1:], start=1):
                    if i < len(field_names):
                        kwargs.setdefault(field_names[i], val)
        super().__init__(**kwargs)

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
    classification: str = "[UNKNOWN]"  # [TRUECHIMER], [FALSETICKER], [UNREACHABLE]
    action: str = "PENDING"  # SELECTED FOR SYNC, CANDIDATE, DISCARDED (Outlier), FALLBACK
    t1: Optional[float] = None
    t2: Optional[float] = None
    t3: Optional[float] = None
    t4: Optional[float] = None
    interval: Optional[Tuple[float, float]] = None
