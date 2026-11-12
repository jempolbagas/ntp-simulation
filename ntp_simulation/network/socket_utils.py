"""Low-level socket helpers and local IP detection for NTP Simulation."""
from __future__ import annotations

import socket
from typing import Optional


def get_local_ip() -> str:
    """Detect LAN IP address of this machine.

    Attempts to connect a UDP socket to an external address (8.8.8.8:80) to determine
    the outbound routing interface IP address. Falls back to host resolution
    or loopback if network routing is unavailable.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return str(ip)
    except Exception:
        try:
            return str(socket.gethostbyname(socket.gethostname()))
        except Exception:
            return "127.0.0.1"


def create_udp_socket(timeout: Optional[float] = None) -> socket.socket:
    """Creates a UDP socket with standard options.

    Args:
        timeout: Optional socket timeout in seconds.

    Returns:
        socket.socket: Configured UDP socket with SO_REUSEADDR enabled.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if timeout is not None:
        sock.settimeout(float(timeout))
    return sock
