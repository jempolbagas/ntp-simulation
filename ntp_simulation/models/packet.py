"""NTP Packet Pydantic models with cryptographic correlation nonce."""
from __future__ import annotations

import secrets
from typing import Any, Optional, Union
from pydantic import BaseModel, Field


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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if args:
            field_names = ["t1", "nonce", "client_id"]
            for i, val in enumerate(args):
                if i < len(field_names):
                    kwargs.setdefault(field_names[i], val)
        super().__init__(**kwargs)

    @classmethod
    def create(
        cls,
        t1: float,
        nonce: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> NTPRequest:
        """Helper factory creating an NTPRequest with an auto-generated nonce."""
        if nonce is None:
            nonce = secrets.token_hex(8)  # 16-character unique hex string
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
    """Server NTP response packet echoing client timestamp and nonce.

    Attributes:
        t1: Echoed client transmit timestamp.
        t2: Server arrival timestamp.
        t3: Server departure timestamp.
        nonce: Echoed correlation nonce matching the client request.
        server_stratum: Stratum level of the responding server (1-16).
        server_id: Optional server identifier (host:port).
        is_falseticker: Indicator if fault injection altered timestamps.
        skew_applied: Skew offset applied during fault injection in seconds.
    """

    t1: float = Field(..., description="Echoed client transmit timestamp")
    t2: float = Field(..., description="Server receive timestamp")
    t3: float = Field(..., description="Server transmit timestamp")
    nonce: str = Field(..., min_length=8, description="Echoed correlation nonce")
    server_stratum: int = Field(..., ge=1, le=16, description="Server stratum level")
    server_id: Optional[str] = Field(default=None, description="Server identifier host:port")
    is_falseticker: bool = Field(default=False, description="Fault injection status")
    skew_applied: float = Field(default=0.0, description="Skew applied in seconds")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if args:
            field_names = [
                "t1",
                "t2",
                "t3",
                "nonce",
                "server_stratum",
                "server_id",
                "is_falseticker",
                "skew_applied",
            ]
            for i, val in enumerate(args):
                if i < len(field_names):
                    kwargs.setdefault(field_names[i], val)
        super().__init__(**kwargs)

    def to_bytes(self) -> bytes:
        """Serializes model to UTF-8 encoded JSON bytes for UDP transmission."""
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, data: Union[bytes, bytearray, str]) -> NTPResponse:
        """Deserializes and validates model from raw UDP bytes or JSON string."""
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        return cls.model_validate_json(data)
