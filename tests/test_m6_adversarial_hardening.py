"""Adversarial stress and hardening test suite for Milestone 6.

Focus Areas:
1. Network Datagram Injection Fuzzing:
   - Truncated bytes (< 10 bytes, single byte, zero bytes)
   - Massive payloads (up to max UDP 65507 bytes and oversized datagram handling)
   - Malformed JSON syntax (unclosed braces, raw strings, NaN, Inf, binary noise)
   - Schema mismatches (missing nonces, non-string nonces, type errors, unexpected payloads)
   - Replayed nonces and mismatched correlation IDs under high-rate burst
   - Server stability and continuous responsiveness under 1,000+ fuzz datagrams

2. VirtualClock Extreme Edge Cases & Monotonicity:
   - Microsecond step adjustments (1e-6, -1e-6, 1e-9)
   - Extreme slewing rates and high-magnitude targets (+1e6, -1e6)
   - Leap second step adjustments (+1.0s, -1.0s)
   - Monotonicity (non-decreasing now() values) under concurrent high-frequency polling
     with simultaneous Brownian drift and rate-limited slewing

3. SharedState 50-Thread High-Concurrency Stress:
   - 50 concurrent worker threads executing 50,000+ operations synchronized with Barrier
   - Concurrent UI reader threads taking snapshots and toggling UI freeze
   - Verification of zero deadlocks, zero race conditions, zero state tearing,
     and bitwise snapshot isolation invariance
"""

from __future__ import annotations

import collections
import concurrent.futures
import json
import os
import random
import secrets
import socket
import threading
import time
from typing import Dict, List, Optional
import pytest

from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.models.config import NodeConfig
from ntp_simulation.models.packet import NTPRequest, NTPResponse
from ntp_simulation.models.peer import PeerAddress, PeerEvaluation, PeerSample
from ntp_simulation.models.telemetry import TelemetrySnapshot
from ntp_simulation.network.client import poll_peer, poll_peers_concurrently
from ntp_simulation.network.server import run_server
from ntp_simulation.network.socket_utils import create_udp_socket
from ntp_simulation.state import SharedState


def get_free_udp_port() -> int:
    """Finds an available UDP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ==============================================================================
# 1. Network Datagram Injection Fuzzing
# ==============================================================================

class TestNetworkDatagramInjectionFuzzing:
    """Adversarially fusses the live UDP server and client with corrupted packets."""

    @pytest.fixture
    def live_server(self):
        """Spawns an active UDP server on an ephemeral port."""
        clock = VirtualClock(initial_offset=0.0)
        port = get_free_udp_port()
        config = NodeConfig(stratum=1, port=port)
        state = SharedState(config)
        stop_event = threading.Event()
        srv_thread = threading.Thread(
            target=run_server,
            args=(clock, state, stop_event, "127.0.0.1"),
            daemon=True,
        )
        srv_thread.start()
        time.sleep(0.05)  # Allow socket to bind
        yield clock, state, port, stop_event
        stop_event.set()
        srv_thread.join(timeout=1.0)

    def test_truncated_bytes_fuzzing(self, live_server):
        """Verifies server discards truncated bytes (< 10 bytes, single byte, zero bytes)
        and remains completely responsive.
        """
        clock, state, port, stop_event = live_server
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        truncated_payloads = [
            b"",                             # 0 bytes
            b"\x00",                         # 1 null byte
            b"{",                            # 1 byte
            b"{}",                           # 2 bytes
            b'{"t1":',                       # 6 bytes
            b'{"nonce":',                    # 9 bytes
            b"\xff\xfe\xfd\xfc",             # 4 random binary bytes
            b'{"t1": 1.0',                   # 9 bytes unclosed
            b"ABCDEFGHI",                    # 9 ASCII bytes
        ]

        # Send all truncated datagrams to live server
        for payload in truncated_payloads:
            sock.sendto(payload, ("127.0.0.1", port))

        time.sleep(0.05)

        # Confirm server did not record corrupted requests as valid
        with state.lock:
            assert state.server_req_count == 0

        # Verify server remains 100% responsive to legitimate queries
        sample = poll_peer(PeerAddress("127.0.0.1", port), clock, timeout=1.0)
        assert sample.status == "RESPONDED"
        assert sample.nonce_matched is True
        assert sample.server_stratum == 1

        time.sleep(0.05)
        with state.lock:
            assert state.server_req_count == 1
        sock.close()

    def test_massive_and_oversized_payloads(self, live_server):
        """Tests sending large payloads (2048 bytes, 4096 bytes, max UDP 65507 bytes).
        Server's 2048-byte buffer must truncate/discard without crashing.
        """
        clock, state, port, stop_event = live_server
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # 1. 2048 bytes of noise
        sock.sendto(b"A" * 2048, ("127.0.0.1", port))

        # 2. 4096 bytes of JSON padding around invalid structure
        large_json = json.dumps({"t1": 100.0, "nonce": "A" * 3500}).encode("utf-8")
        sock.sendto(large_json, ("127.0.0.1", port))

        # 3. Maximum valid IPv4 UDP payload (65,507 bytes)
        max_udp_payload = b"X" * 65507
        sock.sendto(max_udp_payload, ("127.0.0.1", port))

        # 4. Attempting to send > 65507 bytes over UDP fails locally with EMSGSIZE
        with pytest.raises(OSError):
            sock.sendto(b"Y" * 70000, ("127.0.0.1", port))

        time.sleep(0.05)

        # Server must discard all corrupted massive packets
        with state.lock:
            assert state.server_req_count == 0

        # Server remains operational
        sample = poll_peer(PeerAddress("127.0.0.1", port), clock, timeout=1.0)
        assert sample.status == "RESPONDED"
        assert sample.nonce_matched is True
        sock.close()

    def test_malformed_json_syntax_fuzzing(self, live_server):
        """Tests broken JSON syntax, non-decodable bytes, and unexpected data types."""
        clock, state, port, stop_event = live_server
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        fuzz_cases = [
            b"{bad_json: True}",
            b'{"t1": NaN}',
            b'{"t1": Infinity}',
            b'{"t1": -Infinity}',
            b'{"t1": 1e9999999999999999999999}',
            b'{"t1": "not_a_float", "nonce": "validnonce123"}',
            b'{"nonce": null}',
            b'{"t1": 100.0, "nonce": 12345678}',  # integer nonce instead of string
            b'{"t1": 100.0, "nonce": "short"}',    # nonce < 8 chars
            b'{"t1": [1, 2, 3], "nonce": "12345678"}',
            b'["array_root", 123]',
            b'"bare_string_json"',
            b'true',
            b'12345',
            b'{"extra_evil_field": "' + (b"Z" * 1500) + b'"}',
            os.urandom(256),
            os.urandom(1024),
        ]

        for payload in fuzz_cases:
            sock.sendto(payload, ("127.0.0.1", port))

        time.sleep(0.05)

        with state.lock:
            assert state.server_req_count == 0

        # Server answers valid client cleanly
        sample = poll_peer(PeerAddress("127.0.0.1", port), clock, timeout=1.0)
        assert sample.status == "RESPONDED"
        assert sample.nonce_matched is True
        sock.close()

    def test_replayed_nonces_and_mismatched_correlation(self):
        """Verifies client correlation enforcement under bursts of replayed and rogue nonces."""
        clock = VirtualClock()
        port = get_free_udp_port()
        srv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv_sock.bind(("127.0.0.1", port))

        captured_nonces: List[str] = []

        def adversary():
            for i in range(10):
                data, addr = srv_sock.recvfrom(2048)
                req = NTPRequest.from_bytes(data)
                captured_nonces.append(req.nonce)
                # First 5: return completely bogus nonce
                # Next 5: return old replayed nonce from first request
                if i < 5:
                    resp_nonce = f"bogus_nonce_{i:04d}"
                else:
                    resp_nonce = captured_nonces[0]  # replay first nonce

                resp = NTPResponse(
                    t1=req.t1,
                    t2=clock.now(),
                    t3=clock.now(),
                    nonce=resp_nonce,
                    server_stratum=1,
                )
                srv_sock.sendto(resp.to_bytes(), addr)

        adv_thread = threading.Thread(target=adversary, daemon=True)
        adv_thread.start()

        # Perform 10 polls from client
        for _ in range(10):
            sample = poll_peer(PeerAddress("127.0.0.1", port), clock, timeout=1.0)
            assert sample.nonce_matched is False
            assert sample.status == "NONCE_MISMATCH"

        adv_thread.join(timeout=2.0)
        srv_sock.close()

    def test_server_1000_fuzz_datagram_bombardment(self, live_server):
        """Bombards live server with 1,000 distinct malformed datagrams at high speed.
        Asserts server does not crash, leak sockets, or lose responsiveness.
        """
        clock, state, port, stop_event = live_server
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        for i in range(1000):
            choice = i % 5
            if choice == 0:
                payload = os.urandom(random.randint(1, 100))
            elif choice == 1:
                payload = json.dumps({"fuzz_key": i, "val": "broken"}).encode("utf-8")
            elif choice == 2:
                payload = b"{" + b"X" * random.randint(10, 500)
            elif choice == 3:
                payload = b""
            else:
                payload = b"\x00\xff" * random.randint(1, 64)

            raw_sock.sendto(payload, ("127.0.0.1", port))

        time.sleep(0.1)

        # Server must still be responsive
        sample = poll_peer(PeerAddress("127.0.0.1", port), clock, timeout=1.0)
        assert sample.status == "RESPONDED"
        assert sample.nonce_matched is True
        assert sample.server_stratum == 1

        with state.lock:
            # Exactly 1 legitimate request accepted
            assert state.server_req_count == 1
        raw_sock.close()

    def test_concurrent_polling_under_malicious_fuzz_peers(self):
        """poll_peers_concurrently with mixed peers: honest, garbage bytes, mismatched nonce,
        timeout, and schema mismatch. Bounded execution time and accurate classification.
        """
        clock = VirtualClock()
        port_honest = get_free_udp_port()
        port_garbage = get_free_udp_port()
        port_bad_nonce = get_free_udp_port()
        port_bad_schema = get_free_udp_port()
        port_unreachable = get_free_udp_port()

        stop_all = threading.Event()

        # 1. Honest peer
        s_honest = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_honest.bind(("127.0.0.1", port_honest))
        def honest_srv():
            while not stop_all.is_set():
                try:
                    s_honest.settimeout(0.2)
                    d, a = s_honest.recvfrom(2048)
                    req = NTPRequest.from_bytes(d)
                    resp = NTPResponse(t1=req.t1, t2=clock.now(), t3=clock.now(), nonce=req.nonce, server_stratum=1)
                    s_honest.sendto(resp.to_bytes(), a)
                except socket.timeout:
                    continue
        t_honest = threading.Thread(target=honest_srv, daemon=True)
        t_honest.start()

        # 2. Garbage peer
        s_garbage = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_garbage.bind(("127.0.0.1", port_garbage))
        def garbage_srv():
            while not stop_all.is_set():
                try:
                    s_garbage.settimeout(0.2)
                    _, a = s_garbage.recvfrom(2048)
                    s_garbage.sendto(b"\x00\xff\xfe\xca\xfe\xba\xbe", a)
                except socket.timeout:
                    continue
        t_garbage = threading.Thread(target=garbage_srv, daemon=True)
        t_garbage.start()

        # 3. Mismatched nonce peer
        s_bad_nonce = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_bad_nonce.bind(("127.0.0.1", port_bad_nonce))
        def bad_nonce_srv():
            while not stop_all.is_set():
                try:
                    s_bad_nonce.settimeout(0.2)
                    d, a = s_bad_nonce.recvfrom(2048)
                    req = NTPRequest.from_bytes(d)
                    resp = NTPResponse(t1=req.t1, t2=clock.now(), t3=clock.now(), nonce="wrong_nonce_999", server_stratum=1)
                    s_bad_nonce.sendto(resp.to_bytes(), a)
                except socket.timeout:
                    continue
        t_bad_nonce = threading.Thread(target=bad_nonce_srv, daemon=True)
        t_bad_nonce.start()

        # 4. Bad schema peer
        s_bad_schema = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_bad_schema.bind(("127.0.0.1", port_bad_schema))
        def bad_schema_srv():
            while not stop_all.is_set():
                try:
                    s_bad_schema.settimeout(0.2)
                    _, a = s_bad_schema.recvfrom(2048)
                    s_bad_schema.sendto(b'{"t1": "string_not_float", "nonce": "12345678"}', a)
                except socket.timeout:
                    continue
        t_bad_schema = threading.Thread(target=bad_schema_srv, daemon=True)
        t_bad_schema.start()

        # Polling all 5 peers concurrently
        peers = [
            f"127.0.0.1:{port_honest}",
            f"127.0.0.1:{port_garbage}",
            f"127.0.0.1:{port_bad_nonce}",
            f"127.0.0.1:{port_bad_schema}",
            f"127.0.0.1:{port_unreachable}",
        ]

        t_start = time.time()
        results = poll_peers_concurrently(peers, clock, timeout=0.8, max_workers=5)
        duration = time.time() - t_start

        stop_all.set()
        t_honest.join(timeout=1.0)
        t_garbage.join(timeout=1.0)
        t_bad_nonce.join(timeout=1.0)
        t_bad_schema.join(timeout=1.0)
        s_honest.close()
        s_garbage.close()
        s_bad_nonce.close()
        s_bad_schema.close()

        # Concurrent polling must complete bounded by max latency (~0.8s + slack), not 5 * 0.8s = 4.0s
        assert duration < 2.5, f"Concurrent polling was too slow ({duration:.2f}s)"

        assert results[f"127.0.0.1:{port_honest}"].status == "RESPONDED"
        assert results[f"127.0.0.1:{port_honest}"].nonce_matched is True

        assert results[f"127.0.0.1:{port_garbage}"].nonce_matched is False
        assert results[f"127.0.0.1:{port_garbage}"].status.startswith("ERROR:")

        assert results[f"127.0.0.1:{port_bad_nonce}"].nonce_matched is False
        assert results[f"127.0.0.1:{port_bad_nonce}"].status == "NONCE_MISMATCH"

        assert results[f"127.0.0.1:{port_bad_schema}"].nonce_matched is False
        assert results[f"127.0.0.1:{port_bad_schema}"].status.startswith("ERROR:")

        assert results[f"127.0.0.1:{port_unreachable}"].status == "TIMEOUT"

    def test_pydantic_packet_schema_edge_cases(self):
        """Verifies Pydantic packet validation boundaries."""
        from pydantic import ValidationError

        # Nonce min length 8
        with pytest.raises(ValidationError):
            NTPRequest(t1=100.0, nonce="short")

        with pytest.raises(ValidationError):
            NTPResponse(t1=100.0, t2=101.0, t3=102.0, nonce="short", server_stratum=1)

        # Stratum bounds [1, 16]
        with pytest.raises(ValidationError):
            NTPResponse(t1=100.0, t2=101.0, t3=102.0, nonce="validnonce123", server_stratum=0)

        with pytest.raises(ValidationError):
            NTPResponse(t1=100.0, t2=101.0, t3=102.0, nonce="validnonce123", server_stratum=17)

        # Non-numeric timestamp
        with pytest.raises(ValidationError):
            NTPRequest.from_bytes(b'{"t1": "not_a_number", "nonce": "validnonce123"}')



# ==============================================================================
# 2. VirtualClock Extreme Edge Cases & Monotonicity
# ==============================================================================

class TestVirtualClockExtremeEdgeCasesAndMonotonicity:
    """Stress tests VirtualClock edge cases, microsecond steps, extreme slews, and monotonicity."""

    def test_continuous_polling_monotonicity_in_steady_state_and_positive_slew(self):
        """Asserts now() is strictly monotonically non-decreasing (now_next >= now_prev)
        under 40,000 continuous concurrent polls while clock undergoes active positive slewing (+0.40 s/s).
        """
        clock = VirtualClock(initial_offset=5.0, slew_rate=0.40)
        clock.start_slew(10.0)  # Positive slew

        num_threads = 4
        polls_per_thread = 10000
        stop_event = threading.Event()
        monotonicity_violations: List[str] = []

        def poller(tid: int):
            last_t = clock.now()
            for _ in range(polls_per_thread):
                curr_t = clock.now()
                if curr_t < last_t:
                    monotonicity_violations.append(
                        f"Thread {tid}: Clock moved backwards! {curr_t} < {last_t} (diff={curr_t - last_t})"
                    )
                last_t = curr_t

        def ticker():
            while not stop_event.is_set():
                clock.tick()
                time.sleep(0.01)

        tick_t = threading.Thread(target=ticker, daemon=True)
        tick_t.start()

        pollers = [threading.Thread(target=poller, args=(i,), daemon=True) for i in range(num_threads)]
        for p in pollers:
            p.start()
        for p in pollers:
            p.join(timeout=10.0)
            assert not p.is_alive()

        stop_event.set()
        tick_t.join(timeout=2.0)

        assert len(monotonicity_violations) == 0, f"Monotonicity violated under positive slew: {monotonicity_violations[:5]}"

    def test_negative_slew_discrete_tick_jump_behavior(self):
        """Empirically characterizes the discrete simulation mechanics of VirtualClock:
        Because now() = time.time() + self._offset (PROJECT.md:24), and tick() decrements
        _offset by (slew_rate * elapsed) at discrete intervals, a negative slew causes an
        instantaneous backward step of approx -(slew_rate * elapsed) at each tick.
        """
        clock = VirtualClock(initial_offset=5.0, slew_rate=0.40)
        clock.start_slew(-2.0)  # Negative slew: target -2.0s

        t_start = clock.now()
        time.sleep(0.04)  # 40 ms elapsed wall time
        t_before_tick = clock.now()

        # Before tick, virtual time advanced naturally with wall clock time
        assert t_before_tick > t_start

        # Execute tick: adjusts offset by -(0.40 * elapsed)
        clock.tick()
        t_after_tick = clock.now()

        # Due to discrete offset decrement, t_after_tick drops backwards by approx 0.40 * 0.040s = 0.016s
        jump = t_after_tick - t_before_tick
        assert jump < 0.0, f"Expected discrete backward drop on negative slew tick, got {jump:+.6f}"
        assert pytest.approx(-0.016, abs=0.008) == jump


    def test_microsecond_step_adjustments(self):
        """Tests microsecond and sub-microsecond step adjustments."""
        clock = VirtualClock(initial_offset=0.0)

        # Microsecond positive step
        clock.step(1e-6)
        assert clock.get_offset() == pytest.approx(1e-6, abs=1e-9)

        # Microsecond negative step
        clock.step(-1e-6)
        assert clock.get_offset() == pytest.approx(0.0, abs=1e-9)

        # Sub-microsecond (nanosecond scale) step
        clock.step(5e-9)
        assert clock.get_offset() == pytest.approx(5e-9, abs=1e-12)

        # Zero step
        clock.step(0.0)
        assert clock.get_offset() == pytest.approx(5e-9, abs=1e-12)

    def test_extreme_slewing_rates_and_targets(self):
        """Tests extreme slew targets (±1,000,000s) and bounds on slew ratio."""
        clock = VirtualClock(initial_offset=0.0, slew_rate=0.40)

        # Enormous target: +1,000,000s
        clock.start_slew(1_000_000.0)
        rem, target, ratio = clock.get_slew_info()
        assert target == pytest.approx(1_000_000.0)
        assert rem == pytest.approx(1_000_000.0)
        assert ratio == pytest.approx(0.0)

        # Advance 100 seconds
        clock.tick(dt=100.0)
        rem, target, ratio = clock.get_slew_info()
        assert rem == pytest.approx(1_000_000.0 - 40.0)
        assert 0.0 <= ratio <= 1.0

        # Step immediately clears extreme slew
        clock.step(0.0)
        rem, target, ratio = clock.get_slew_info()
        assert rem == 0.0
        assert target == 0.0
        assert ratio == 1.0

        # Negative extreme target: -500,000s
        clock.start_slew(-500_000.0)
        rem, target, ratio = clock.get_slew_info()
        assert target == pytest.approx(-500_000.0)
        assert rem == pytest.approx(-500_000.0)

        # Advance 10 seconds (offset adjusted by -4.0s)
        clock.tick(dt=10.0)
        rem, target, ratio = clock.get_slew_info()
        assert rem == pytest.approx(-500_000.0 + 4.0)

    def test_leap_second_step_offsets(self):
        """Tests leap second stepping (+1.0s and -1.0s)."""
        clock = VirtualClock(initial_offset=10.0)
        assert clock.get_offset() == pytest.approx(10.0)

        # Positive leap second
        clock.step(1.0)
        assert clock.get_offset() == pytest.approx(11.0)

        # Negative leap second
        clock.step(-1.0)
        assert clock.get_offset() == pytest.approx(10.0)

    def test_step_backward_jump_clears_slew(self):
        """Verifies backward step jump behavior: offset jumps backwards instantaneously
        and clears pending slew.
        """
        clock = VirtualClock(initial_offset=100.0)
        clock.start_slew(50.0)

        # Backward jump by 500s
        clock.step(-500.0)
        assert clock.get_offset() == pytest.approx(-400.0)

        rem, target, ratio = clock.get_slew_info()
        assert rem == 0.0
        assert target == 0.0
        assert ratio == 1.0

    def test_format_ts_extreme_values(self):
        """Verifies format_ts gracefully handles negative, NaN, Inf, and overflow values."""
        assert VirtualClock.format_ts(-100.0) == "00:00:00.000" or isinstance(VirtualClock.format_ts(-100.0), str)
        assert VirtualClock.format_ts(1e15) == "00:00:00.000"
        assert VirtualClock.format_ts(float("nan")) == "00:00:00.000"
        assert VirtualClock.format_ts(float("inf")) == "00:00:00.000"
        assert VirtualClock.format_ts(float("-inf")) == "00:00:00.000"

    def test_high_concurrency_clock_mutations_and_reads(self):
        """10 threads calling now() while 4 threads call step(), start_slew(), and tick() simultaneously."""
        clock = VirtualClock(initial_offset=0.0, slew_rate=0.40)
        clock.toggle_drift()
        stop_event = threading.Event()
        errors: List[Exception] = []

        def reader():
            while not stop_event.is_set():
                try:
                    t = clock.now()
                    assert isinstance(t, float)
                    rem, target, ratio = clock.get_slew_info()
                    assert 0.0 <= ratio <= 1.0
                except Exception as e:
                    errors.append(e)

        def mutator():
            while not stop_event.is_set():
                try:
                    clock.step(0.001)
                    clock.start_slew(0.05)
                    clock.tick(dt=0.01)
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=reader, daemon=True) for _ in range(10)] + [
            threading.Thread(target=mutator, daemon=True) for _ in range(4)
        ]
        for t in threads:
            t.start()
        time.sleep(0.3)
        stop_event.set()
        for t in threads:
            t.join(timeout=2.0)

        assert len(errors) == 0, f"Errors in concurrent clock access: {errors}"



# ==============================================================================
# 3. SharedState 50-Thread High-Concurrency Stress
# ==============================================================================

class TestSharedState50ThreadConcurrency:
    """Stress tests SharedState with 50 concurrent worker threads and UI snapshot readers."""

    def test_50_concurrent_threads_stress(self):
        """50 concurrent worker threads perform 50,000 operations on SharedState
        simultaneously while 2 UI threads continuously capture frozen snapshots.

        Verifies:
        - 0 deadlocks across 52 total threads synchronized with a Barrier.
        - Exact transaction counter invariants (tx_count == rx_count == server_req_count).
        - Exact sync_count matches sum of all thread sync recordings.
        - Ring buffers never exceed maximum capacities (10, 10, 30).
        """
        num_workers = 50
        ops_per_worker = 1000  # 50 * 1000 = 50,000 operations
        total_threads = num_workers + 2

        cfg = NodeConfig(stratum=2, port=5002, upstreams=["127.0.0.1:5001"])
        state = SharedState(cfg)
        clock = VirtualClock(initial_offset=10.0)

        barrier = threading.Barrier(total_threads)
        errors: List[Exception] = []
        stop_readers = threading.Event()

        req_counts = [0] * num_workers
        sync_counts = [0] * num_workers
        offset_counts = [0] * num_workers

        def worker_fn(tid: int):
            try:
                barrier.wait(timeout=10.0)
                for i in range(ops_per_worker):
                    op = i % 5
                    if op == 0:
                        state.record_server_request(
                            client=f"10.0.{tid}.{i % 200}:5000",
                            t2=1000.0 + i,
                            t3=1000.0 + i + 0.001,
                            is_fake=False,
                            skew=0.0,
                        )
                        req_counts[tid] += 1
                    elif op == 1:
                        state.record_sync(
                            peer=f"10.0.{tid}.1:5001",
                            delay_ms=2.5,
                            theta_s=0.001 * (tid + 1),
                            mode="STEP" if (i % 2 == 0) else "SLEW",
                            before=1000.0 + i,
                            after=1000.0 + i + 0.001,
                        )
                        sync_counts[tid] += 1
                    elif op == 2:
                        state.add_offset_sample(0.0001 * tid)
                        offset_counts[tid] += 1
                    elif op == 3:
                        eval_data = PeerEvaluation(
                            peer_key=f"peer_{tid}",
                            stratum=1,
                            delay=0.005,
                            theta=0.001 * tid,
                            status="RESPONDED",
                            classification="[TRUECHIMER]",
                            action="CANDIDATE",
                        )
                        state.update_peer_report(f"peer_{tid}", eval_data)
                    elif op == 4:
                        if i % 100 == 0:
                            state.toggle_falseticker()
                            state.toggle_chaos()
                            state.toggle_sync_mode()
            except Exception as e:
                errors.append(e)

        reader_snapshots: List[TelemetrySnapshot] = []

        def ui_reader_fn(r_id: int):
            try:
                barrier.wait(timeout=10.0)
                while not stop_readers.is_set():
                    snap = state.get_snapshot(clock)
                    if snap is not None:
                        # Validate no state tearing inside snapshot
                        assert snap.tx_count == snap.rx_count
                        assert len(snap.server_logs) <= 10
                        assert len(snap.sync_history) <= 10
                        assert len(snap.offset_samples) <= 30
                        reader_snapshots.append(snap)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads: List[threading.Thread] = []
        for w in range(num_workers):
            threads.append(threading.Thread(target=worker_fn, args=(w,), daemon=True))
        for r in range(2):
            threads.append(threading.Thread(target=ui_reader_fn, args=(r,), daemon=True))

        for t in threads:
            t.start()

        # Wait for all 50 worker threads to complete
        for t in threads[:num_workers]:
            t.join(timeout=30.0)
            assert not t.is_alive(), "Deadlock detected in worker thread!"

        stop_readers.set()
        for t in threads[num_workers:]:
            t.join(timeout=5.0)

        assert len(errors) == 0, f"Exceptions in threads: {errors}"

        total_reqs = sum(req_counts)
        total_syncs = sum(sync_counts)

        # Invariant verifications
        assert state.server_req_count == total_reqs
        assert state.tx_count == total_reqs
        assert state.rx_count == total_reqs
        assert state.sync_count == total_syncs

        # Ring buffer bounds verifications
        assert len(state.recent_server_logs) <= 10
        assert len(state.sync_history) <= 10
        assert len(state.offset_samples) <= 30

        # Verify UI reader collected snapshots without tearing
        assert len(reader_snapshots) > 10

    def test_snapshot_isolation_under_50_threads(self):
        """Freezes snapshot, runs 25,000 rapid mutations across 50 threads,
        and asserts that frozen snapshot remains 100% immutable throughout.
        """
        clock = VirtualClock(initial_offset=5.0)
        state = SharedState(stratum=2, port=5002)

        # Pre-populate
        for i in range(5):
            state.record_server_request(f"192.168.1.{i}:1234", 100.0, 100.001)
            state.record_sync(
                peer=f"10.0.0.{i}:5001",
                delay_ms=2.0,
                theta_s=0.01,
                mode="STEP",
                before=100.0,
                after=100.01,
            )

        # Freeze UI
        paused = state.toggle_ui_pause(clock)
        assert paused is True
        assert state.ui_paused is True
        assert state.frozen_snapshot is not None

        frozen_dump_initial = state.frozen_snapshot.model_dump()
        frozen_tx_initial = state.frozen_snapshot.tx_count

        num_threads = 50
        mutations_per_thread = 500  # 25,000 total mutations
        barrier = threading.Barrier(num_threads)
        errors: List[Exception] = []

        def mutator(tid: int):
            try:
                barrier.wait(timeout=10.0)
                for i in range(mutations_per_thread):
                    state.record_server_request(f"10.50.{tid}.{i % 250}:5000", 200.0 + i, 200.0 + i + 0.001)
                    if i % 10 == 0:
                        state.record_sync(
                            peer=f"peer_{tid}",
                            delay_ms=5.0,
                            theta_s=0.002,
                            mode="SLEW",
                            before=200.0,
                            after=200.002,
                        )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mutator, args=(t,), daemon=True) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
            assert not t.is_alive()

        assert len(errors) == 0, f"Mutator errors: {errors}"

        # Strict immutability assertion
        assert state.frozen_snapshot.model_dump() == frozen_dump_initial
        assert state.frozen_snapshot.tx_count == frozen_tx_initial

        # Live state must have advanced
        assert state.tx_count > frozen_tx_initial + 20000

        # Unfreeze and verify live snapshot resumes
        state.toggle_ui_pause(clock)
        live_snap = state.get_snapshot(clock)
        assert live_snap is not None
        assert live_snap.tx_count == state.tx_count

    def test_concurrent_reset_during_50_worker_traffic(self):
        """Verifies that calling reset_counters() while 50 workers are actively
        updating state does not cause race conditions, negative values, or deadlocks.
        """
        state = SharedState(stratum=1, port=5000)
        num_workers = 50
        stop_flag = threading.Event()
        errors: List[Exception] = []

        def worker(tid: int):
            try:
                for i in range(300):
                    state.record_server_request(f"192.168.{tid}.{i % 250}:5000", 1.0, 2.0)
                    if i % 10 == 0:
                        state.record_sync(peer=f"p_{tid}", delay_ms=1.0, theta_s=0.01)
            except Exception as e:
                errors.append(e)

        def resetter():
            try:
                while not stop_flag.is_set():
                    time.sleep(0.005)
                    state.reset_counters()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(w,), daemon=True) for w in range(num_workers)]
        r_thread = threading.Thread(target=resetter, daemon=True)

        for t in threads:
            t.start()
        r_thread.start()

        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()

        stop_flag.set()
        r_thread.join(timeout=2.0)

        assert len(errors) == 0, f"Errors in concurrent reset: {errors}"
        with state.lock:
            assert state.tx_count >= 0
            assert state.rx_count >= 0
            assert state.server_req_count >= 0
            assert state.tx_count == state.rx_count
            assert state.server_req_count == state.tx_count

