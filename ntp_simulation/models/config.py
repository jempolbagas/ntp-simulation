"""Node configuration model."""
from __future__ import annotations

from typing import Any, List, Union
from pydantic import BaseModel, Field, field_validator, model_validator
from ntp_simulation.models.peer import PeerAddress


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
    sync_mode: str = Field(default="STEP", pattern="^(STEP|SLEW)$")
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

    @field_validator("upstreams", mode="before")
    @classmethod
    def parse_upstreams(cls, v: Any) -> Any:
        """Coerces strings or tuples into PeerAddress instances."""
        if isinstance(v, list):
            parsed: List[Union[PeerAddress, Any]] = []
            for item in v:
                if isinstance(item, str):
                    parsed.append(PeerAddress.parse(item))
                elif isinstance(item, tuple) and len(item) == 2:
                    parsed.append(PeerAddress(host=str(item[0]), port=int(item[1])))
                else:
                    parsed.append(item)
            return parsed
        return v

    @model_validator(mode="after")
    def validate_upstreams_for_stratum(self) -> NodeConfig:
        """Enforces that non-master stratum nodes configure at least one upstream peer."""
        if self.stratum > 1 and not self.upstreams:
            raise ValueError(
                f"Stratum {self.stratum} node requires at least one upstream peer"
            )
        return self
