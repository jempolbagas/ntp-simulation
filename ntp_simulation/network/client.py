"""Concurrent UDP client with request nonce correlation and timeout handling."""
from __future__ import annotations

import concurrent.futures
import socket
from typing import Dict, List, Optional, Union

from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.models.packet import NTPRequest, NTPResponse
from ntp_simulation.models.peer import PeerAddress, PeerSample
from ntp_simulation.network.socket_utils import create_udp_socket


def poll_peer(
    peer: Union[PeerAddress, str, tuple],
    clock: VirtualClock,
    timeout: float = 1.5,
    client_id: Optional[str] = None,
) -> PeerSample:
    """Polls a single upstream peer using an ephemeral UDP socket and correlation nonce.

    Args:
        peer: Target peer address (PeerAddress instance, 'host:port' string, or (host, port) tuple).
        clock: Local VirtualClock instance for timestamping.
        timeout: Socket receive timeout in seconds (default 1.5s).
        client_id: Optional client identifier included in the request.

    Returns:
        PeerSample: Measurement containing transit timestamps (t1..t4), delay, offset,
                    and correlation validation status.
    """
    if isinstance(peer, str):
        target_peer = PeerAddress.parse(peer)
    elif isinstance(peer, (tuple, list)):
        target_peer = PeerAddress(host=str(peer[0]), port=int(peer[1]))
    elif isinstance(peer, PeerAddress):
        target_peer = peer
    else:
        raise TypeError(f"Unsupported peer address type: {type(peer)}")

    sock = create_udp_socket(timeout=timeout)
    t1: Optional[float] = None
    try:
        # 1. Transmit request with cryptographically random correlation nonce
        t1 = clock.now()
        req = NTPRequest.create(t1=t1, client_id=client_id)
        sock.sendto(req.to_bytes(), (target_peer.host, target_peer.port))

        # 2. Receive datagram from server
        data, _ = sock.recvfrom(2048)
        t4 = clock.now()

        # 3. Deserialize and validate response schema
        resp = NTPResponse.from_bytes(data)

        # 4. Enforce correlation nonce matching
        if resp.nonce != req.nonce:
            return PeerSample(
                peer=target_peer,
                t1=t1,
                t2=resp.t2,
                t3=resp.t3,
                t4=t4,
                server_stratum=resp.server_stratum,
                nonce_matched=False,
                status="NONCE_MISMATCH",
            )

        return PeerSample(
            peer=target_peer,
            t1=t1,
            t2=resp.t2,
            t3=resp.t3,
            t4=t4,
            server_stratum=resp.server_stratum,
            nonce_matched=True,
            status="RESPONDED",
        )

    except socket.timeout:
        t_err = clock.now()
        return PeerSample(
            peer=target_peer,
            t1=t1 if t1 is not None else t_err,
            t2=0.0,
            t3=0.0,
            t4=t_err,
            nonce_matched=False,
            status="TIMEOUT",
        )
    except Exception as exc:
        t_err = clock.now()
        return PeerSample(
            peer=target_peer,
            t1=t1 if t1 is not None else t_err,
            t2=0.0,
            t3=0.0,
            t4=t_err,
            nonce_matched=False,
            status=f"ERROR: {exc}",
        )
    finally:
        sock.close()


def poll_peers_concurrently(
    upstreams: List[Union[PeerAddress, str, tuple]],
    clock: VirtualClock,
    timeout: float = 1.5,
    client_id: Optional[str] = None,
    max_workers: int = 10,
) -> Dict[str, PeerSample]:
    """Polls all upstream peers concurrently using a thread pool.

    Total round duration is bounded by max(peer latency) rather than sum(latencies),
    ensuring unresponsive or slow peers do not delay synchronization with responsive peers.

    Args:
        upstreams: List of target peer addresses.
        clock: Local VirtualClock instance.
        timeout: Per-peer socket receive timeout in seconds.
        client_id: Optional client identifier sent in requests.
        max_workers: Maximum worker threads in the thread pool.

    Returns:
        Dict[str, PeerSample]: Mapping from peer key ('host:port') to its PeerSample.
    """
    if not upstreams:
        return {}

    parsed_peers: List[PeerAddress] = []
    for p in upstreams:
        if isinstance(p, str):
            parsed_peers.append(PeerAddress.parse(p))
        elif isinstance(p, (tuple, list)):
            parsed_peers.append(PeerAddress(host=str(p[0]), port=int(p[1])))
        elif isinstance(p, PeerAddress):
            parsed_peers.append(p)
        else:
            raise TypeError(f"Unsupported peer address type in list: {type(p)}")

    results: Dict[str, PeerSample] = {}
    workers = min(max_workers, max(1, len(parsed_peers)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_peer = {
            executor.submit(poll_peer, peer, clock, timeout, client_id): peer
            for peer in parsed_peers
        }
        for future in concurrent.futures.as_completed(future_to_peer):
            peer = future_to_peer[future]
            try:
                sample = future.result()
            except Exception as exc:
                t_err = clock.now()
                sample = PeerSample(
                    peer=peer,
                    t1=t_err,
                    t2=0.0,
                    t3=0.0,
                    t4=t_err,
                    nonce_matched=False,
                    status=f"ERROR: {exc}",
                )
            results[peer.key] = sample

    return results
