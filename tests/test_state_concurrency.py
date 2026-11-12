"""Adversarial concurrency, snapshot isolation, and ring buffer stress tests for SharedState.

Challenges:
1. 20-thread concurrency stress test (100k+ operations) with 0 deadlocks, 0 race conditions,
   and 0 state tearing.
2. Snapshot isolation invariance during rapid background mutation (10,000+ operations).
3. Ring buffer overflow limits under 10,000+ successive insertions.
4. Concurrent toggle and reset stress.
"""

from __future__ import annotations

import collections
import concurrent.futures
import threading
import time
from typing import Dict, List
import pytest

from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.models.config import NodeConfig
from ntp_simulation.models.peer import PeerAddress, PeerEvaluation
from ntp_simulation.models.telemetry import TelemetrySnapshot
from ntp_simulation.state import SharedState


class TestSharedStateConcurrencyStress:
    """Adversarial stress testing of SharedState multi-threaded concurrency."""

    def test_20_threads_100k_operations_concurrency_and_invariants(self):
        """20 concurrent threads perform 100,000+ operations on SharedState.

        Verifies:
        - 0 deadlocks across 20 simultaneous threads synchronized via Barrier.
        - Exact transaction counter invariants (tx_count == rx_count == server_req_count).
        - Exact synchronization counter invariants (sync_count).
        - No race conditions or state tearing.
        """
        num_threads = 20
        ops_per_thread = 5500  # 20 * 5500 = 110,000 operations total (> 100k)
        
        cfg = NodeConfig(stratum=2, port=5002, upstreams=["127.0.0.1:5001"])
        state = SharedState(cfg)
        clock = VirtualClock(initial_offset=10.0)

        # Thread counters to verify exact atomicity and 0 lost updates
        req_counts = [0] * num_threads
        sync_counts = [0] * num_threads
        offset_counts = [0] * num_threads
        peer_update_counts = [0] * num_threads
        read_counts = [0] * num_threads
        
        barrier = threading.Barrier(num_threads)
        errors: List[Exception] = []

        def worker(thread_idx: int):
            try:
                # Synchronize start of all 20 threads to maximize lock contention
                barrier.wait(timeout=10.0)
                
                for i in range(ops_per_thread):
                    op_type = i % 6
                    
                    if op_type == 0:
                        # Server request recording
                        state.record_server_request(
                            client=f"192.168.1.{thread_idx}:{1000 + i}",
                            t2=1000.0 + i,
                            t3=1000.0 + i + 0.001,
                            is_fake=False,
                            skew=0.0,
                        )
                        req_counts[thread_idx] += 1

                    elif op_type == 1:
                        # Sync event recording
                        state.record_sync(
                            peer=f"10.0.0.{thread_idx}:5001",
                            delay_ms=2.5,
                            theta_s=0.005 + (i * 0.00001),
                            mode="STEP" if (i % 2 == 0) else "SLEW",
                            before=1000.0 + i,
                            after=1000.0 + i + 0.005,
                        )
                        sync_counts[thread_idx] += 1

                    elif op_type == 2:
                        # Add offset sample
                        state.add_offset_sample(0.001 * (thread_idx + 1))
                        offset_counts[thread_idx] += 1

                    elif op_type == 3:
                        # Update single peer report
                        eval_data = PeerEvaluation(
                            peer_key=f"peer_{thread_idx}",
                            stratum=1,
                            delay=0.003,
                            theta=0.001 * thread_idx,
                            status="RESPONDED",
                            classification="[TRUECHIMER]",
                            action="CANDIDATE",
                        )
                        state.update_peer_report(f"peer_{thread_idx}", eval_data)
                        peer_update_counts[thread_idx] += 1

                    elif op_type == 4:
                        # Snapshot query under concurrent mutation
                        snap = state.get_snapshot(clock)
                        assert snap is not None
                        assert snap.tx_count >= 0
                        assert snap.rx_count >= 0
                        read_counts[thread_idx] += 1

                    elif op_type == 5:
                        # State toggles and raw property reads
                        if i % 100 == 0:
                            state.toggle_falseticker()
                            state.toggle_chaos()
                            state.toggle_sync_mode()
                        
                        # Read properties
                        _ = state.tx_count
                        _ = state.rx_count
                        _ = state.sync_count
                        _ = state.server_req_count
                        _ = state.selected_peer
                        _ = state.sync_mode
                        read_counts[thread_idx] += 1

            except Exception as exc:
                errors.append(exc)

        # Launch 20 threads
        threads = [threading.Thread(target=worker, args=(t,), daemon=True) for t in range(num_threads)]
        for t in threads:
            t.start()
        
        # Wait for all threads with a generous timeout to detect deadlocks
        for t in threads:
            t.join(timeout=30.0)
            assert not t.is_alive(), "Deadlock detected: thread failed to finish within 30s!"

        # Assert zero exceptions occurred across all 20 threads
        assert len(errors) == 0, f"Exceptions in worker threads: {errors}"

        total_reqs = sum(req_counts)
        total_syncs = sum(sync_counts)
        total_ops = (
            total_reqs
            + total_syncs
            + sum(offset_counts)
            + sum(peer_update_counts)
            + sum(read_counts)
        )

        assert total_ops >= 100_000, f"Total ops {total_ops} was under 100k"
        
        # Invariant checks:
        # Every record_server_request increments server_req_count, tx_count, and rx_count
        assert state.server_req_count == total_reqs, f"Lost updates in server_req_count: {state.server_req_count} != {total_reqs}"
        assert state.tx_count == total_reqs, f"Lost updates in tx_count: {state.tx_count} != {total_reqs}"
        assert state.rx_count == total_reqs, f"Lost updates in rx_count: {state.rx_count} != {total_reqs}"
        assert state.sync_count == total_syncs, f"Lost updates in sync_count: {state.sync_count} != {total_syncs}"

        # Ring buffer capacity guarantees after 100k+ ops
        assert len(state.recent_server_logs) <= 10
        assert len(state.sync_history) <= 10
        assert len(state.offset_samples) <= 30


class TestSnapshotIsolationInvariance:
    """Adversarial stress testing of UI Freeze Snapshot Isolation."""

    def test_snapshot_isolation_invariance_during_10k_rapid_mutations(self):
        """Freezes snapshot via toggle_ui_pause, runs 10,000 rapid background mutations,
        and asserts that frozen_snapshot remains 100% bitwise/structurally identical.
        """
        clock = VirtualClock(initial_offset=5.0)
        state = SharedState(stratum=2, port=5002)

        # 1. Populate initial state with baseline entries
        for i in range(5):
            state.record_server_request(f"192.168.1.{i}:1234", 100.0 + i, 100.0 + i + 0.001)
            state.record_sync(
                peer=f"10.0.0.{i}:5001",
                delay_ms=2.0 + i,
                theta_s=0.01 * (i + 1),
                mode="STEP",
                before=100.0,
                after=100.01,
            )
            state.add_offset_sample(0.005 * i)
            state.update_peer_report(
                f"peer_{i}",
                PeerEvaluation(
                    peer_key=f"peer_{i}",
                    stratum=1,
                    delay=0.002,
                    theta=0.005,
                    status="RESPONDED",
                    classification="[TRUECHIMER]",
                    action="CANDIDATE",
                ),
            )

        # 2. Freeze UI
        paused = state.toggle_ui_pause(clock)
        assert paused is True
        assert state.ui_paused is True
        assert state.frozen_snapshot is not None

        # Capture frozen snapshot reference and serial dump
        frozen_ref = state.frozen_snapshot
        frozen_dump_initial = frozen_ref.model_dump()
        frozen_tx_initial = frozen_ref.tx_count
        frozen_rx_initial = frozen_ref.rx_count
        frozen_logs_initial = list(frozen_ref.server_logs)
        frozen_history_initial = list(frozen_ref.sync_history)
        frozen_samples_initial = list(frozen_ref.offset_samples)
        frozen_peers_initial = dict(frozen_ref.peer_reports)

        # 3. Spawn 10 concurrent worker threads performing 10,000 rapid mutations
        num_mutators = 10
        mutations_per_thread = 1000  # Total 10,000 mutations
        stop_readers = threading.Event()
        reader_violations: List[str] = []

        def reader_worker():
            while not stop_readers.is_set():
                snap = state.get_snapshot(clock)
                if snap is not frozen_ref:
                    reader_violations.append("Snapshot object reference changed while frozen!")
                if snap.model_dump() != frozen_dump_initial:
                    reader_violations.append("Snapshot content mutated while frozen!")
                time.sleep(0.0005)

        def mutator_worker(t_id: int):
            for i in range(mutations_per_thread):
                sub = i % 4
                if sub == 0:
                    state.record_server_request(
                        f"10.99.{t_id}.{i % 250}:5000",
                        2000.0 + i,
                        2000.0 + i + 0.001,
                        is_fake=(i % 5 == 0),
                        skew=1.5,
                    )
                elif sub == 1:
                    state.record_sync(
                        peer=f"peer_mutator_{t_id}",
                        delay_ms=10.0,
                        theta_s=-0.05,
                        mode="SLEW",
                        before=500.0,
                        after=499.95,
                    )
                elif sub == 2:
                    state.add_offset_sample(0.123 + t_id)
                elif sub == 3:
                    state.update_peer_report(
                        f"peer_mutator_{t_id}",
                        PeerEvaluation(
                            peer_key=f"peer_mutator_{t_id}",
                            stratum=3,
                            delay=0.05,
                            theta=-0.02,
                            status="RESPONDED",
                            classification="[FALSETICKER]",
                            action="DISCARDED",
                        ),
                    )
                
                # Advance clock occasionally
                if i % 100 == 0:
                    clock.step(0.1)

        readers = [threading.Thread(target=reader_worker, daemon=True) for _ in range(2)]
        for r in readers:
            r.start()

        mutators = [threading.Thread(target=mutator_worker, args=(m,), daemon=True) for m in range(num_mutators)]
        for m in mutators:
            m.start()

        for m in mutators:
            m.join(timeout=20.0)
            assert not m.is_alive(), "Mutator thread timed out!"

        stop_readers.set()
        for r in readers:
            r.join(timeout=5.0)

        assert len(reader_violations) == 0, f"Reader detected snapshot mutations: {reader_violations[:5]}"

        # 4. Strict assertion that frozen_snapshot remains completely unaltered
        frozen_dump_final = state.frozen_snapshot.model_dump()
        assert frozen_dump_final == frozen_dump_initial, "frozen_snapshot content altered after 10k mutations!"
        assert state.frozen_snapshot.tx_count == frozen_tx_initial
        assert state.frozen_snapshot.rx_count == frozen_rx_initial
        assert state.frozen_snapshot.server_logs == frozen_logs_initial
        assert state.frozen_snapshot.sync_history == frozen_history_initial
        assert state.frozen_snapshot.offset_samples == frozen_samples_initial
        assert state.frozen_snapshot.peer_reports == frozen_peers_initial

        # 5. Assert live state DID mutate in background
        expected_new_server_reqs = num_mutators * (mutations_per_thread // 4)
        assert state.tx_count > frozen_tx_initial + expected_new_server_reqs - 100
        assert state.sync_count > 5

        # 6. Unfreeze UI and assert live telemetry resumes
        resumed = state.toggle_ui_pause(clock)
        assert resumed is False
        assert state.ui_paused is False
        assert state.frozen_snapshot is None

        # Fetch fresh live snapshot
        live_snap = state.get_snapshot(clock)
        assert live_snap is not None
        assert live_snap is not frozen_ref
        assert live_snap.tx_count == state.tx_count
        assert live_snap.rx_count == state.rx_count
        assert live_snap.tx_count > frozen_tx_initial


class TestRingBufferOverflowGuarantees:
    """Adversarial stress testing of Ring Buffer overflow limits."""

    def test_ring_buffers_never_exceed_configured_maxlens_under_10k_inserts(self):
        """Inserts 10,000 records into recent_server_logs, sync_history, and offset_samples.
        
        Asserts:
        - len(recent_server_logs) never exceeds 10.
        - len(sync_history) never exceeds 10.
        - len(offset_samples) never exceeds 30.
        - FIFO order preservation and correct eviction.
        """
        state = SharedState(stratum=1, port=5000)
        clock = VirtualClock()

        # Insert 10,000 entries across 5 threads
        num_inserts_per_thread = 2000
        num_threads = 5

        def inserter(tid: int):
            for i in range(num_inserts_per_thread):
                idx = tid * num_inserts_per_thread + i
                state.record_server_request(
                    client=f"client_{idx}",
                    t2=100.0 + idx,
                    t3=100.0 + idx + 0.001,
                )
                state.record_sync(
                    peer=f"peer_{idx}",
                    delay_ms=1.0,
                    theta_s=0.001 * idx,
                    mode="STEP",
                    before=100.0,
                    after=100.001,
                )
                state.add_offset_sample(float(idx))

        threads = [threading.Thread(target=inserter, args=(t,), daemon=True) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
            assert not t.is_alive()

        # Check raw deque maxlens and lengths
        assert state.recent_server_logs.maxlen == 10
        assert len(state.recent_server_logs) == 10
        assert state.sync_history.maxlen == 10
        assert len(state.sync_history) == 10
        assert state.offset_samples.maxlen == 30
        assert len(state.offset_samples) == 30

        # Verify through snapshot representation as well
        snap = state.get_snapshot(clock)
        assert len(snap.server_logs) == 10
        assert len(snap.sync_history) == 10
        assert len(snap.offset_samples) == 30

    def test_ring_buffer_fifo_eviction_semantics(self):
        """Verifies that oldest items are cleanly evicted when capacity is reached."""
        state = SharedState(stratum=1, port=5000)

        # 1. recent_server_logs uses appendleft (newest at index 0, oldest at index -1)
        for i in range(15):
            state.record_server_request(f"client_{i}", float(i), float(i + 1))
        
        assert len(state.recent_server_logs) == 10
        # Newest item must be client_14
        assert state.recent_server_logs[0].client == "client_14"
        # Oldest item remaining must be client_5 (0..4 were evicted)
        assert state.recent_server_logs[-1].client == "client_5"

        # 2. sync_history uses appendleft (newest at index 0, oldest at index -1)
        for i in range(15):
            state.record_sync(
                peer=f"peer_{i}",
                delay_ms=float(i),
                theta_s=float(i),
                mode="STEP",
                before=0.0,
                after=1.0,
            )
        assert len(state.sync_history) == 10
        assert state.sync_history[0].peer == "peer_14"
        assert state.sync_history[-1].peer == "peer_5"

        # 3. offset_samples uses append (oldest at index 0, newest at index -1)
        for i in range(50):
            state.add_offset_sample(float(i))
        assert len(state.offset_samples) == 30
        # 0..19 evicted, remaining are 20..49
        assert state.offset_samples[0] == 20.0
        assert state.offset_samples[-1] == 49.0

    def test_ring_buffer_memory_reclamation_weakref(self):
        """Verifies that evicted entries are garbage collected and do not leak memory."""
        import gc
        import weakref

        state = SharedState(stratum=1, port=5000)
        refs = []
        for i in range(50):
            state.record_server_request(f"client_{i}", float(i), float(i + 1))
            refs.append(weakref.ref(state.recent_server_logs[0]))

        gc.collect()
        alive = [r() for r in refs if r() is not None]
        # Only the 10 most recent entries in the ring buffer must remain alive
        assert len(alive) == 10
        assert len(state.recent_server_logs) == 10


class TestSharedStateEdgeCasesAndReset:
    """Stress tests for resets, toggles, and edge cases."""

    def test_concurrent_toggle_pause_no_race_or_deadlock(self):
        """Rapidly toggle pause across 10 threads while mutating state."""
        state = SharedState(stratum=1, port=5000)
        clock = VirtualClock()
        stop_event = threading.Event()
        errors: List[Exception] = []

        def toggler():
            try:
                for _ in range(500):
                    state.toggle_ui_pause(clock)
            except Exception as e:
                errors.append(e)

        def mutator():
            try:
                for i in range(500):
                    state.record_server_request("1.2.3.4", 1.0, 2.0)
                    state.get_snapshot(clock)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=toggler, daemon=True) for _ in range(5)
        ] + [
            threading.Thread(target=mutator, daemon=True) for _ in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
            assert not t.is_alive()

        assert len(errors) == 0, f"Errors in concurrent toggle: {errors}"

    def test_reset_counters_atomic_under_concurrency(self):
        """Reset counters while mutations occur and assert consistent non-negative counts."""
        state = SharedState(stratum=1, port=5000)
        clock = VirtualClock()

        for _ in range(100):
            state.record_server_request("1.1.1.1", 1.0, 2.0)
        
        state.reset_counters()
        assert state.tx_count == 0
        assert state.rx_count == 0
        assert state.server_req_count == 0
        assert state.sync_count == 0

        # Now test concurrent reset with active writes
        def writer():
            for _ in range(1000):
                state.record_server_request("2.2.2.2", 1.0, 2.0)

        def resetter():
            for _ in range(50):
                time.sleep(0.001)
                state.reset_counters()

        w = threading.Thread(target=writer, daemon=True)
        r = threading.Thread(target=resetter, daemon=True)
        w.start()
        r.start()
        w.join(timeout=5.0)
        r.join(timeout=5.0)

        # Counters must remain coherent and non-negative
        assert state.tx_count >= 0
        assert state.rx_count >= 0
        assert state.tx_count == state.rx_count
        assert state.server_req_count == state.tx_count

    def test_zero_state_tearing_in_snapshot_under_heavy_concurrency(self):
        """Asserts zero state tearing across snapshot properties under rapid concurrent writes."""
        state = SharedState(stratum=1, port=5000)
        clock = VirtualClock()
        stop_flag = threading.Event()
        tearing_errors: List[str] = []

        def writer(tid: int):
            for i in range(2000):
                state.record_server_request(f"client_{tid}_{i}", 100.0, 100.001)

        def reader():
            while not stop_flag.is_set():
                snap = state.get_snapshot(clock)
                if snap is not None:
                    # Invariant: every record_server_request increments tx_count and rx_count identically
                    if snap.tx_count != snap.rx_count:
                        tearing_errors.append(f"Torn state: tx={snap.tx_count} != rx={snap.rx_count}")
                    if len(snap.server_logs) > 10:
                        tearing_errors.append(f"Overflow logs in snapshot: len={len(snap.server_logs)}")

        writers = [threading.Thread(target=writer, args=(w,), daemon=True) for w in range(8)]
        readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]

        for r in readers:
            r.start()
        for w in writers:
            w.start()

        for w in writers:
            w.join(timeout=10.0)
            assert not w.is_alive()

        stop_flag.set()
        for r in readers:
            r.join(timeout=5.0)

        assert len(tearing_errors) == 0, f"Detected state tearing: {tearing_errors[:5]}"
        assert state.tx_count == 8 * 2000
        assert state.rx_count == 8 * 2000

    def test_frozen_snapshot_clock_invariance(self):
        """Asserts that virtual clock adjustments after pause do NOT mutate frozen snapshot."""
        clock = VirtualClock(initial_offset=2.5)
        state = SharedState(stratum=1, port=5000)

        state.toggle_ui_pause(clock)
        assert state.ui_paused is True
        snap = state.frozen_snapshot
        assert snap is not None

        freeze_offset = snap.offset
        freeze_virtual = snap.now_virtual

        # Mutate clock heavily
        clock.step(100.0)
        clock.start_slew(-30.0)
        clock.tick(dt=1.0)

        # Assert frozen snapshot still has original offset and virtual time
        assert state.frozen_snapshot.offset == freeze_offset
        assert state.frozen_snapshot.now_virtual == freeze_virtual

        # Resume and assert live snapshot reflects new clock state
        state.toggle_ui_pause(clock)
        live_snap = state.get_snapshot(clock)
        assert live_snap.offset == clock.get_offset()
        assert live_snap.offset != freeze_offset

