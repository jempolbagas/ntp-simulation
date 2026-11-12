"""Adversarial and stress test suite for NTP simulation network layer.

Validates:
- Socket utilities and LAN IP detection
- Cryptographic nonce correlation and rogue anti-spoofing rejection
- Replay attack rejection and malformed packet fuzzing
- Network timeout bounds and concurrent upstream polling
- High-load loopback stability (1,000+ requests) and socket descriptor leak prevention
- Dynamic fault injection (manual falseticker and random chaos)
- Telemetry state thread-safety, bounded ring buffers, and UI snapshot isolation
"""

from __future__ import annotations

import concurrent.futures
import os
import socket
import threading
import time
from typing import List, Tuple

import pytest

from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.models.config import NodeConfig
from ntp_simulation.models.packet import NTPRequest, NTPResponse
from ntp_simulation.models.peer import PeerAddress, PeerSample
from ntp_simulation.network.client import poll_peer, poll_peers_concurrently
from ntp_simulation.network.server import run_server
from ntp_simulation.network.socket_utils import create_udp_socket, get_local_ip
from ntp_simulation.state import SharedState


# ==============================================================================
# Helper utilities for ephemeral dynamic test ports
# ==============================================================================

def get_free_udp_port() -> int:
    """Finds an available UDP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ==============================================================================
# 1. Socket Utilities Tests
# ==============================================================================

class TestSocketUtils:
    def test_get_local_ip(self):
        ip = get_local_ip()
        assert isinstance(ip, str)
        parts = ip.split(".")
        assert len(parts) == 4
        for part in parts:
            assert 0 <= int(part) <= 255

    def test_create_udp_socket(self):
        sock = create_udp_socket(timeout=0.25)
        try:
            assert sock.type == socket.SOCK_DGRAM
            assert sock.gettimeout() == 0.25
            reuse = sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
            assert reuse != 0
        finally:
            sock.close()


# ==============================================================================
# 2. Nonce Correlation & Anti-Spoofing Stress Tests
# ==============================================================================

class TestNonceCorrelationAndAntiSpoofing:
    def test_rogue_server_mismatched_nonce(self):
        """Verifies that poll_peer strictly rejects responses with mismatched nonces."""
        clk = VirtualClock()
        port = get_free_udp_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", port))

        def rogue_handler():
            data, addr = sock.recvfrom(2048)
            req = NTPRequest.from_bytes(data)
            # Deliberately spoof with a mismatched nonce
            resp = NTPResponse(
                t1=req.t1,
                t2=clk.now(),
                t3=clk.now(),
                nonce="badnonce12345678",
                server_stratum=1,
            )
            sock.sendto(resp.to_bytes(), addr)

        t = threading.Thread(target=rogue_handler, daemon=True)
        t.start()

        sample = poll_peer(PeerAddress("127.0.0.1", port), clk, timeout=1.0)
        sock.close()
        t.join(timeout=1.0)

        assert sample.nonce_matched is False
        assert sample.status == "NONCE_MISMATCH"

    def test_delayed_nonce_replay_attack(self):
        """Verifies that replaying an old nonce from a previous round is rejected."""
        clk = VirtualClock()
        port = get_free_udp_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", port))

        # Stale nonce captured from an older round
        stale_nonce = "stale_nonce_1234"

        def replay_handler():
            data, addr = sock.recvfrom(2048)
            req = NTPRequest.from_bytes(data)
            # Server returns stale nonce instead of current req.nonce
            resp = NTPResponse(
                t1=req.t1,
                t2=clk.now(),
                t3=clk.now(),
                nonce=stale_nonce,
                server_stratum=1,
            )
            sock.sendto(resp.to_bytes(), addr)

        t = threading.Thread(target=replay_handler, daemon=True)
        t.start()

        sample = poll_peer(PeerAddress("127.0.0.1", port), clk, timeout=1.0)
        sock.close()
        t.join(timeout=1.0)

        assert sample.nonce_matched is False
        assert sample.status == "NONCE_MISMATCH"

    @pytest.mark.parametrize(
        "payload,expected_error_substr",
        [
            (b"", "validation error"),
            (b"\x00\xff\xfe\x01\x02\x03", "codec can't decode"),
            (b"This is not a JSON payload at all", "validation error"),
            (b'{"unrelated": "json", "number": 42}', "validation error"),
            (b'{"t1": 100.0, "t2": 101.0, "t3": 102.0}', "validation error"),  # missing nonce
            (b'{"t1": "invalid", "nonce": "abc"}', "validation error"),
        ],
    )
    def test_malformed_udp_payload_fuzzing(self, payload: bytes, expected_error_substr: str):
        """Verifies that malformed and fuzzed UDP datagrams are safely caught without crashing."""
        clk = VirtualClock()
        port = get_free_udp_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", port))

        def fuzzed_handler():
            _, addr = sock.recvfrom(2048)
            sock.sendto(payload, addr)

        t = threading.Thread(target=fuzzed_handler, daemon=True)
        t.start()

        sample = poll_peer(PeerAddress("127.0.0.1", port), clk, timeout=1.0)
        sock.close()
        t.join(timeout=1.0)

        assert sample.nonce_matched is False
        assert sample.status.startswith("ERROR:")
        assert expected_error_substr in sample.status.lower()

    def test_server_discards_garbage_datagrams(self):
        """Verifies that the UDP server listener silently discards garbage datagrams."""
        clk = VirtualClock()
        port = get_free_udp_port()
        state = SharedState(NodeConfig(stratum=1, port=port))
        stop = threading.Event()
        srv = threading.Thread(
            target=run_server,
            args=(clk, state, stop, "127.0.0.1"),
            daemon=True,
        )
        srv.start()
        time.sleep(0.05)

        # Flood server with corrupted datagrams
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(50):
            raw_sock.sendto(b"MALFORMED_GARBAGE_" + bytes([i]), ("127.0.0.1", port))
        raw_sock.close()

        # Follow up with a legitimate NTP query
        sample = poll_peer(PeerAddress("127.0.0.1", port), clk, timeout=1.0)
        stop.set()
        srv.join(timeout=1.0)

        assert sample.status == "RESPONDED"
        assert sample.nonce_matched is True


# ==============================================================================
# 3. Network Timeout & Concurrency Stress Tests
# ==============================================================================

class TestNetworkTimeoutAndConcurrency:
    def test_unreachable_peer_timeout(self):
        """Verifies that polling a non-responsive peer times out correctly."""
        clk = VirtualClock()
        # Bind a socket that never answers (blackhole)
        port = get_free_udp_port()
        blackhole = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blackhole.bind(("127.0.0.1", port))

        t0 = time.perf_counter()
        sample = poll_peer(PeerAddress("127.0.0.1", port), clk, timeout=0.3)
        duration = time.perf_counter() - t0
        blackhole.close()

        assert sample.status == "TIMEOUT"
        assert sample.nonce_matched is False
        assert 0.25 <= duration <= 0.60

    def test_concurrent_polling_time_bound_mixed_peers(self):
        """Verifies concurrent polling of 5 live and 5 dead peers is bounded by max latency (~0.5s).

        Asserts total elapsed time does NOT scale linearly to 5 x 0.5s = 2.5s.
        """
        clk = VirtualClock()
        stop = threading.Event()

        # 5 live responding servers
        live_servers: List[Tuple[threading.Thread, SharedState]] = []
        live_peers: List[PeerAddress] = []
        for _ in range(5):
            p = get_free_udp_port()
            st = SharedState(NodeConfig(stratum=1, port=p))
            t = threading.Thread(
                target=run_server,
                args=(clk, st, stop, "127.0.0.1"),
                daemon=True,
            )
            t.start()
            live_servers.append((t, st))
            live_peers.append(PeerAddress("127.0.0.1", p))

        # 5 silent dead peers (bound UDP sockets that never send responses)
        dead_sockets: List[socket.socket] = []
        dead_peers: List[PeerAddress] = []
        for _ in range(5):
            p = get_free_udp_port()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", p))
            dead_sockets.append(s)
            dead_peers.append(PeerAddress("127.0.0.1", p))

        time.sleep(0.05)

        all_peers = live_peers + dead_peers
        timeout = 0.5

        t0 = time.perf_counter()
        results = poll_peers_concurrently(
            all_peers,
            clk,
            timeout=timeout,
            max_workers=10,
        )
        elapsed = time.perf_counter() - t0

        stop.set()
        for s in dead_sockets:
            s.close()

        responded_count = sum(1 for p in live_peers if results[p.key].status == "RESPONDED")
        timeout_count = sum(1 for p in dead_peers if results[p.key].status == "TIMEOUT")

        assert responded_count == 5, f"Expected 5 responded, got {responded_count}"
        assert timeout_count == 5, f"Expected 5 timeouts, got {timeout_count}"
        # Crucial check: Elapsed time must be bounded by max latency (~0.5s), NOT 2.5s
        assert elapsed < 1.0, f"Concurrent polling took {elapsed:.3f}s, expected < 1.0s"

    def test_concurrent_polling_empty_input(self):
        clk = VirtualClock()
        res = poll_peers_concurrently([], clk)
        assert res == {}

    def test_poll_peer_address_types(self):
        """Verifies poll_peer accepts PeerAddress, 'host:port' string, and (host, port) tuple."""
        clk = VirtualClock()
        port = get_free_udp_port()
        state = SharedState(NodeConfig(stratum=1, port=port))
        stop = threading.Event()
        srv = threading.Thread(
            target=run_server,
            args=(clk, state, stop, "127.0.0.1"),
            daemon=True,
        )
        srv.start()
        time.sleep(0.05)

        try:
            # 1. PeerAddress object
            s1 = poll_peer(PeerAddress("127.0.0.1", port), clk, timeout=1.0)
            assert s1.status == "RESPONDED"

            # 2. String '127.0.0.1:port'
            s2 = poll_peer(f"127.0.0.1:{port}", clk, timeout=1.0)
            assert s2.status == "RESPONDED"

            # 3. Tuple ('127.0.0.1', port)
            s3 = poll_peer(("127.0.0.1", port), clk, timeout=1.0)
            assert s3.status == "RESPONDED"
        finally:
            stop.set()
            srv.join(timeout=1.0)


# ==============================================================================
# 4. High-Load Loopback & Socket Descriptor Leak Tests
# ==============================================================================

class TestHighLoadLoopbackAndDescriptorLeaks:
    def test_1000_rapid_loopback_exchanges(self):
        """Runs 1,000 rapid UDP exchanges across worker threads.

        Asserts 0 unhandled socket leaks or descriptor exhaustion.
        """
        clk = VirtualClock()
        port = get_free_udp_port()
        state = SharedState(NodeConfig(stratum=1, port=port))
        stop = threading.Event()
        srv = threading.Thread(
            target=run_server,
            args=(clk, state, stop, "127.0.0.1"),
            daemon=True,
        )
        srv.start()
        time.sleep(0.05)

        peer = PeerAddress("127.0.0.1", port)
        initial_fds = len(os.listdir("/proc/self/fd"))

        total_requests = 1000
        concurrency = 4
        status_counts: dict[str, int] = {}

        def send_load_req(idx: int):
            time.sleep(0.0001 * (idx % 5))
            return poll_peer(peer, clk, 1.0, f"load-{idx}")

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(send_load_req, i)
                for i in range(total_requests)
            ]
            for f in concurrent.futures.as_completed(futures):
                sample = f.result()
                status_counts[sample.status] = status_counts.get(sample.status, 0) + 1

        elapsed = time.perf_counter() - t0
        stop.set()
        srv.join(timeout=1.0)
        import gc
        gc.collect()
        time.sleep(0.05)

        final_fds = len(os.listdir("/proc/self/fd"))

        # Verify all 1,000 completed
        assert sum(status_counts.values()) == total_requests
        # High success rate under rapid load
        assert status_counts.get("RESPONDED", 0) >= 980, f"Low success rate: {status_counts}"
        # Any dropped or mismatched datagrams are handled gracefully
        for st in status_counts:
            assert st in ("RESPONDED", "NONCE_MISMATCH", "TIMEOUT")
        # Verify 0 socket descriptor leaks (allowing minor threadpool worker variation <= 10)
        fd_diff = abs(final_fds - initial_fds)
        assert fd_diff <= 10, f"Socket descriptor leak detected! FD delta: {fd_diff}"


# ==============================================================================
# 5. Fault Injection Tests
# ==============================================================================

class TestFaultInjection:
    def test_falseticker_mode_skew(self):
        """Verifies manual falseticker mode injects +60s skew."""
        clk = VirtualClock()
        port = get_free_udp_port()
        state = SharedState(NodeConfig(stratum=1, port=port))
        stop = threading.Event()
        srv = threading.Thread(
            target=run_server,
            args=(clk, state, stop, "127.0.0.1"),
            daemon=True,
        )
        srv.start()
        time.sleep(0.05)

        peer = PeerAddress("127.0.0.1", port)

        try:
            # Honest baseline
            s1 = poll_peer(peer, clk, timeout=1.0)
            assert abs(s1.theta) < 0.05

            # Activate falseticker (+60.0s skew)
            state.toggle_falseticker()
            s2 = poll_peer(peer, clk, timeout=1.0)
            assert abs(s2.theta - 60.0) < 0.05

            # Deactivate
            state.toggle_falseticker()
            s3 = poll_peer(peer, clk, timeout=1.0)
            assert abs(s3.theta) < 0.05
        finally:
            stop.set()
            srv.join(timeout=1.0)

    def test_chaos_mode_skew_distribution(self):
        """Verifies chaos mode injects random timing anomalies at ~35% frequency."""
        clk = VirtualClock()
        port = get_free_udp_port()
        state = SharedState(NodeConfig(stratum=1, port=port))
        stop = threading.Event()
        srv = threading.Thread(
            target=run_server,
            args=(clk, state, stop, "127.0.0.1"),
            daemon=True,
        )
        srv.start()
        time.sleep(0.05)

        peer = PeerAddress("127.0.0.1", port)
        state.toggle_chaos()

        total = 100
        corrupted = 0
        try:
            for _ in range(total):
                sample = poll_peer(peer, clk, timeout=1.0)
                assert sample.status == "RESPONDED"
                if abs(sample.theta) > 10.0:
                    corrupted += 1
                    # Magnitude must be between 30 and 90 seconds
                    assert 30.0 <= abs(sample.theta) <= 90.5

            ratio = corrupted / total
            # Expect roughly 35% with binomial tolerance (15% to 55%)
            assert 0.15 <= ratio <= 0.55, f"Chaos ratio unexpected: {ratio}"
        finally:
            stop.set()
            srv.join(timeout=1.0)


# ==============================================================================
# 6. SharedState and Snapshot Isolation Tests
# ==============================================================================

class TestSharedStateAndSnapshotIsolation:
    def test_snapshot_isolation_during_active_traffic(self):
        """Verifies UI pause freezes telemetry snapshot while background requests proceed."""
        clk = VirtualClock()
        port = get_free_udp_port()
        state = SharedState(NodeConfig(stratum=1, port=port))
        stop = threading.Event()
        srv = threading.Thread(
            target=run_server,
            args=(clk, state, stop, "127.0.0.1"),
            daemon=True,
        )
        srv.start()
        time.sleep(0.05)

        peer = PeerAddress("127.0.0.1", port)

        try:
            # Pause UI
            state.toggle_ui_pause(clk)
            assert state.ui_paused is True
            frozen_snap = state.get_snapshot(clk)
            assert frozen_snap is not None
            frozen_tx = frozen_snap.tx_count

            # Process 15 requests while paused
            for _ in range(15):
                sample = poll_peer(peer, clk, timeout=1.0)
                assert sample.status == "RESPONDED"

            # Check snapshot remains frozen
            current_snap = state.get_snapshot(clk)
            assert current_snap.tx_count == frozen_tx
            # Check underlying state advanced
            assert state.tx_count == frozen_tx + 15

            # Unpause UI
            state.toggle_ui_pause(clk)
            assert state.ui_paused is False
            live_snap = state.get_snapshot(clk)
            assert live_snap.tx_count == state.tx_count
        finally:
            stop.set()
            srv.join(timeout=1.0)

    def test_ring_buffers_bounded_capacity(self):
        """Verifies fixed capacity deque bounds under heavy insertions."""
        state = SharedState(NodeConfig(stratum=1, port=5000))

        for i in range(100):
            state.record_server_request(f"client-{i}", 100.0 + i, 100.1 + i)
            state.record_sync(
                peer=f"peer-{i}",
                delay_ms=2.5,
                theta_s=0.001,
                mode="STEP",
                before=10.0,
                after=10.001,
            )
            state.add_offset_sample(float(i))

        assert len(state.recent_server_logs) == 10
        assert len(state.sync_history) == 10
        assert len(state.offset_samples) == 30
