"""UDP Server listening for client requests and handling fault injection."""
from __future__ import annotations

import random
import socket
import threading
from typing import TYPE_CHECKING, Optional

from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.models.packet import NTPRequest, NTPResponse
from ntp_simulation.network.socket_utils import create_udp_socket

if TYPE_CHECKING:
    from ntp_simulation.state import SharedState


def run_server(
    clock: VirtualClock,
    state: SharedState,
    stop_event: threading.Event,
    bind_host: str = "0.0.0.0",
) -> None:
    """Listens for inbound NTP requests and responds with local virtual time.

    Supports dynamic fault injection (falseticker mode injecting constant skew,
    or chaos mode injecting sporadic random timing anomalies).

    Args:
        clock: Local VirtualClock instance supplying virtual time.
        state: Shared telemetry and control state.
        stop_event: Threading event signaling graceful shutdown.
        bind_host: Interface IP address to bind UDP socket to (default '0.0.0.0').
    """
    with state.lock:
        listen_port = state.port

    sock = create_udp_socket(timeout=0.5)
    sock.bind((bind_host, listen_port))

    try:
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            t2 = clock.now()
            try:
                req = NTPRequest.from_bytes(data)
            except Exception:
                # Discard corrupted or non-NTP datagrams
                continue

            t3 = clock.now()

            # Read server runtime configuration and fault flags under lock
            with state.lock:
                manual_fault = state.falseticker_mode
                chaos_active = state.chaos_mode
                default_skew = state.falseticker_skew
                stratum = state.stratum
                local_ip = state.local_ip
                port = state.port

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

            resp = NTPResponse(
                t1=req.t1,
                t2=t2,
                t3=t3,
                nonce=req.nonce,
                server_stratum=stratum,
                server_id=f"{local_ip}:{port}",
                is_falseticker=is_corrupted,
                skew_applied=applied_skew,
            )

            try:
                sock.sendto(resp.to_bytes(), addr)
                client_id = f"{addr[0]}:{addr[1]}"
                state.record_server_request(client_id, t2, t3, is_corrupted, applied_skew)
            except OSError:
                pass
    finally:
        sock.close()
