"""Network communication engine for NTP Simulation.

Exports UDP socket helpers, concurrent client polling, and server listener.
"""

from ntp_simulation.network.client import poll_peer, poll_peers_concurrently
from ntp_simulation.network.server import run_server
from ntp_simulation.network.socket_utils import create_udp_socket, get_local_ip

__all__ = [
    "create_udp_socket",
    "get_local_ip",
    "poll_peer",
    "poll_peers_concurrently",
    "run_server",
]
