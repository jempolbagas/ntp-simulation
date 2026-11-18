"""UDP network utilities, concurrent client polling, and server listener with fault injection."""
from __future__ import annotations

import concurrent.futures
import functools
import random
import socket
import threading
from typing import TYPE_CHECKING, Dict, List, Optional, Union

from ntp_simulation.clock import VirtualClock
from ntp_simulation.models import NTPRequest, NTPResponse, PeerAddress, PeerSample

if TYPE_CHECKING:
    from ntp_simulation.state import SharedState


@functools.lru_cache(maxsize=1)
def get_local_ip() -> str:
    """Detect LAN IP address of this machine."""
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
    """Creates a UDP socket with SO_REUSEADDR enabled."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if timeout is not None:
        sock.settimeout(float(timeout))
    return sock


def _coerce_peer(peer: Union[PeerAddress, str, tuple]) -> PeerAddress:
    if isinstance(peer, PeerAddress):
        return peer
    if isinstance(peer, str):
        return PeerAddress.parse(peer)
    if isinstance(peer, (tuple, list)):
        return PeerAddress(host=str(peer[0]), port=int(peer[1]))
    raise TypeError(f"Unsupported peer address type: {type(peer)}")


def poll_peer(
    peer: Union[PeerAddress, str, tuple],
    clock: VirtualClock,
    timeout: float = 1.5,
    client_id: Optional[str] = None,
) -> PeerSample:
    """Polls a single upstream peer using an ephemeral UDP socket and correlation nonce."""
    target_peer = _coerce_peer(peer)

    sock = create_udp_socket(timeout=timeout)
    t1: Optional[float] = None
    try:
        t1 = clock.now()
        req = NTPRequest.create(t1=t1, client_id=client_id)
        sock.sendto(req.to_bytes(), (target_peer.host, target_peer.port))

        data, _ = sock.recvfrom(2048)
        t4 = clock.now()

        resp = NTPResponse.from_bytes(data)

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
    """Polls all upstream peers concurrently using a thread pool."""
    if not upstreams:
        return {}

    parsed_peers = list(dict.fromkeys(_coerce_peer(p) for p in upstreams))
    if len(parsed_peers) == 1:
        peer = parsed_peers[0]
        return {peer.key: poll_peer(peer, clock, timeout, client_id)}

    results: Dict[str, PeerSample] = {}
    workers = min(max_workers, len(parsed_peers))

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


def run_server(
    clock: VirtualClock,
    state: SharedState,
    stop_event: threading.Event,
    bind_host: str = "0.0.0.0",
) -> None:
    """Listens for inbound NTP requests and responds with local virtual time."""
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
                continue

            t3 = clock.now()

            with state.lock:
                manual_fault = state.falseticker_mode
                chaos_active = state.chaos_mode
                default_skew = state.falseticker_skew
                chaos_prob = state.chaos_probability
                chaos_min = state.chaos_min_skew
                chaos_max = state.chaos_max_skew
                stratum = state.stratum
                local_ip = state.local_ip
                port = state.port

            is_corrupted = False
            applied_skew = 0.0

            if manual_fault:
                is_corrupted = True
                applied_skew = default_skew
            elif chaos_active and (random.random() < chaos_prob):
                is_corrupted = True
                applied_skew = random.choice([-1.0, 1.0]) * random.uniform(chaos_min, chaos_max)

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
