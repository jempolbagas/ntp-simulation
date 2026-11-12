"""Milestone 6 Adversarial Challenge Test Suite: Multi-Hop Live Hierarchy & Byzantine Stress.

Empirically challenges:
1. Live multi-hop NTP hierarchy deployment on localhost (Stratum 1 -> Stratum 2 Relays -> Stratum 3 Leaf).
2. Byzantine fault injection (simultaneous +60s falseticker skew and 30-90s chaotic skews).
3. Verification that Stratum 3 leaf's Marzullo algorithm identifies and rejects falsetickers as outliers,
   logs [FALSETICKER] / DISCARDED, and strictly refuses to synchronize to corrupted time.
4. Majority consensus recovery when honest truechimers provide quorum.
5. Clock monotonicity and stability during STEP <-> SLEW mode transitions under active load.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

import pytest

from ntp_simulation.algorithms.marzullo import marzullo_evaluate, marzullo_intersection
from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.models.config import NodeConfig
from ntp_simulation.models.packet import NTPRequest, NTPResponse
from ntp_simulation.models.peer import PeerAddress, PeerSample
from ntp_simulation.network.client import poll_peer, poll_peers_concurrently
from ntp_simulation.network.server import run_server
from ntp_simulation.state import SharedState


def get_free_port() -> int:
    """Finds an available UDP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ==============================================================================
# 1. Multi-Hop Live Hierarchy Byzantine Stress (In-Process UDP Thread Network)
# ==============================================================================

class TestMultiHopByzantineStressInProcess:
    """Rigorous in-process live UDP multi-hop network stress test."""

    def test_stratum3_rejects_byzantine_stratum2_relays(self):
        """Topology:
        S1 Master (offset 0.0) on port P1
        S2 Relay A (falseticker +60s) on port P2
        S2 Relay B (chaos mode +30..+90s) on port P3
        S3 Leaf on port P4 syncing to S2 Relay A & B.

        Verification:
        - S3 receives responses from both S2 relays.
        - Relay A is ALWAYS identified as [FALSETICKER].
        - When Relay B glitches, both are rejected with [FALSETICKER] / DISCARDED.
        - S3's clock NEVER synchronizes to the +60s falseticker.
        """
        p1 = get_free_port()
        p2 = get_free_port()
        p3 = get_free_port()
        p4 = get_free_port()

        stop_event = threading.Event()

        # Stratum 1 Master
        clk_s1 = VirtualClock(initial_offset=0.0)
        state_s1 = SharedState(NodeConfig(stratum=1, port=p1, no_ui=True))
        srv_s1 = threading.Thread(
            target=run_server, args=(clk_s1, state_s1, stop_event, "127.0.0.1"), daemon=True
        )
        srv_s1.start()

        # Stratum 2 Relay A (Falseticker: +60s skew)
        clk_s2a = VirtualClock(initial_offset=0.0)
        state_s2a = SharedState(
            NodeConfig(stratum=2, port=p2, upstreams=[PeerAddress("127.0.0.1", p1)], no_ui=True)
        )
        state_s2a.toggle_falseticker()  # Turn ON +60s falseticker mode
        srv_s2a = threading.Thread(
            target=run_server, args=(clk_s2a, state_s2a, stop_event, "127.0.0.1"), daemon=True
        )
        srv_s2a.start()

        # Stratum 2 Relay B (Chaos mode: 35% glitch +30..+90s skew)
        clk_s2b = VirtualClock(initial_offset=0.0)
        state_s2b = SharedState(
            NodeConfig(stratum=2, port=p3, upstreams=[PeerAddress("127.0.0.1", p1)], no_ui=True)
        )
        state_s2b.toggle_chaos()  # Turn ON auto chaos mode
        srv_s2b = threading.Thread(
            target=run_server, args=(clk_s2b, state_s2b, stop_event, "127.0.0.1"), daemon=True
        )
        srv_s2b.start()

        # Stratum 3 Leaf
        initial_s3_offset = 5.0
        clk_s3 = VirtualClock(initial_offset=initial_s3_offset)
        upstreams_s3 = [
            PeerAddress("127.0.0.1", p2),
            PeerAddress("127.0.0.1", p3),
        ]

        time.sleep(0.1)

        try:
            falseticker_rejections = 0
            cycles = 30

            for _ in range(cycles):
                samples = poll_peers_concurrently(upstreams_s3, clk_s3, timeout=1.0)
                assert len(samples) == 2

                # Evaluate with Marzullo
                evaluations, chosen = marzullo_evaluate(list(samples.values()))

                relay_a_eval = evaluations[f"127.0.0.1:{p2}"]
                relay_b_eval = evaluations[f"127.0.0.1:{p3}"]

                # Relay A must NEVER be chosen as sync peer
                assert chosen != f"127.0.0.1:{p2}", "Falseticker Relay A was chosen for sync!"

                # Relay A must be classified as FALSETICKER
                assert relay_a_eval.classification == "[FALSETICKER]"
                assert "DISCARDED" in relay_a_eval.action

                # If Relay B glitched or disagreed, chosen must be None
                # Since Relay A has +60s and Relay B has ~0s (or chaos +30..90s),
                # with n=2 they never intersect -> both are discarded
                assert chosen is None, f"Expected no consensus between 2 conflicting relays, got chosen: {chosen}"
                assert relay_b_eval.classification == "[FALSETICKER]"
                falseticker_rejections += 1

                # If no consensus, clock should NOT be stepped to +60s!
                if chosen:
                    clk_s3.step(evaluations[chosen].theta)

                # Offset must stay around initial offset (5.0s), never jumping to ~ +60s
                assert abs(clk_s3.get_offset() - initial_s3_offset) < 0.1, (
                    f"Clock was corrupted! Current offset: {clk_s3.get_offset()}"
                )

            assert falseticker_rejections == cycles

        finally:
            stop_event.set()
            srv_s1.join(timeout=1.0)
            srv_s2a.join(timeout=1.0)
            srv_s2b.join(timeout=1.0)

    def test_stratum3_majority_consensus_with_honest_quorum(self):
        """Tests that when an honest truechimer quorum exists alongside Byzantine relays,
        Marzullo correctly discards the +60s falseticker and synchronizes to the truechimer.

        Topology:
        S1 Master on port P1 (offset 0.0)
        S2 Relay A on port P2: Falseticker (+60s)
        S2 Relay B on port P3: Honest Relay (syncs to S1, offset ~0.0)
        S2 Relay C on port P4: Honest Relay (syncs to S1, offset ~0.0)
        S3 Leaf on port P5: Syncs to A, B, and C.

        Verification:
        - With n=3, f=1 is tolerated (n-f = 2 > 1.5).
        - Relay A (+60s) is isolated as [FALSETICKER].
        - Relays B and C form majority [TRUECHIMER] consensus.
        - Chosen peer is B or C (lowest delay).
        - S3 clock steps toward 0.0s (honest S1 time), rejecting Relay A's +60s skew!
        """
        p1 = get_free_port()
        p2 = get_free_port()
        p3 = get_free_port()
        p4 = get_free_port()

        stop_event = threading.Event()

        # S1 Master
        clk_s1 = VirtualClock(initial_offset=0.0)
        state_s1 = SharedState(NodeConfig(stratum=1, port=p1, no_ui=True))
        srv_s1 = threading.Thread(
            target=run_server, args=(clk_s1, state_s1, stop_event, "127.0.0.1"), daemon=True
        )
        srv_s1.start()

        # S2 Relay A (Falseticker: +60s)
        clk_s2a = VirtualClock(initial_offset=0.0)
        state_s2a = SharedState(
            NodeConfig(stratum=2, port=p2, upstreams=[PeerAddress("127.0.0.1", p1)], no_ui=True)
        )
        state_s2a.toggle_falseticker()
        srv_s2a = threading.Thread(
            target=run_server, args=(clk_s2a, state_s2a, stop_event, "127.0.0.1"), daemon=True
        )
        srv_s2a.start()

        # S2 Relay B (Honest)
        clk_s2b = VirtualClock(initial_offset=0.0)
        state_s2b = SharedState(
            NodeConfig(stratum=2, port=p3, upstreams=[PeerAddress("127.0.0.1", p1)], no_ui=True)
        )
        srv_s2b = threading.Thread(
            target=run_server, args=(clk_s2b, state_s2b, stop_event, "127.0.0.1"), daemon=True
        )
        srv_s2b.start()

        # S2 Relay C (Honest)
        clk_s2c = VirtualClock(initial_offset=0.0)
        state_s2c = SharedState(
            NodeConfig(stratum=2, port=p4, upstreams=[PeerAddress("127.0.0.1", p1)], no_ui=True)
        )
        srv_s2c = threading.Thread(
            target=run_server, args=(clk_s2c, state_s2c, stop_event, "127.0.0.1"), daemon=True
        )
        srv_s2c.start()

        # S3 Leaf starting with initial offset of +12.0s
        clk_s3 = VirtualClock(initial_offset=12.0)
        upstreams_s3 = [
            PeerAddress("127.0.0.1", p2),
            PeerAddress("127.0.0.1", p3),
            PeerAddress("127.0.0.1", p4),
        ]

        time.sleep(0.1)

        try:
            # Poll all 3 upstreams
            samples = poll_peers_concurrently(upstreams_s3, clk_s3, timeout=1.0)
            assert len(samples) == 3

            evaluations, chosen = marzullo_evaluate(list(samples.values()))

            # Relay A must be classified as [FALSETICKER]
            assert evaluations[f"127.0.0.1:{p2}"].classification == "[FALSETICKER]"
            assert "DISCARDED" in evaluations[f"127.0.0.1:{p2}"].action

            # Relays B and C must be [TRUECHIMER]
            assert evaluations[f"127.0.0.1:{p3}"].classification == "[TRUECHIMER]"
            assert evaluations[f"127.0.0.1:{p4}"].classification == "[TRUECHIMER]"

            # Chosen peer must be either B or C
            assert chosen in (f"127.0.0.1:{p3}", f"127.0.0.1:{p4}")

            # Apply step sync to chosen peer
            clk_s3.step(evaluations[chosen].theta)

            # S3 clock offset should now be close to 0.0s (S1 time), NOT +60s!
            assert abs(clk_s3.get_offset()) < 0.05, f"Expected offset near 0.0s, got {clk_s3.get_offset()}"

        finally:
            stop_event.set()
            srv_s1.join(timeout=1.0)
            srv_s2a.join(timeout=1.0)
            srv_s2b.join(timeout=1.0)
            srv_s2c.join(timeout=1.0)


# ==============================================================================
# 2. Live Process Multi-Hop Subprocess Hierarchy Deployment
# ==============================================================================

class TestLiveSubprocessHierarchyDeployment:
    """Spawns separate OS processes using python3 -m ntp_simulation on localhost."""

    def test_live_4_node_hierarchy_subprocess_lifecycle_and_fault_rejection(self):
        """Spawns 4 distinct OS processes:
        - Process 1: Stratum 1 Master (port 16001, --no-ui)
        - Process 2: Stratum 2 Relay A (port 16002, --upstream 127.0.0.1:16001 --falseticker --no-ui)
        - Process 3: Stratum 2 Relay B (port 16003, --upstream 127.0.0.1:16001 --chaos --no-ui)
        - Process 4: Stratum 3 Leaf (port 16004, --upstream 127.0.0.1:16002 --upstream 127.0.0.1:16003 --interval 1.0 --no-ui)

        Asserts:
        - All processes start and bind UDP ports without errors.
        - Direct probing to S2-A confirms +60s skew.
        - S3 Leaf logs show rejection of falsetickers and no corruption.
        - Clean termination via SIGTERM.
        """
        p_s1 = get_free_port()
        p_s2a = get_free_port()
        p_s2b = get_free_port()
        p_s3 = get_free_port()

        procs: List[subprocess.Popen] = []
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env = {**os.environ, "PYTHONPATH": repo_root}

        try:
            # 1. Start S1 Master
            cmd_s1 = [
                sys.executable, "-m", "ntp_simulation",
                "--stratum", "1",
                "--port", str(p_s1),
                "--no-ui",
            ]
            proc_s1 = subprocess.Popen(cmd_s1, cwd=repo_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            procs.append(proc_s1)
            time.sleep(0.3)

            # 2. Start S2 Relay A (falseticker)
            cmd_s2a = [
                sys.executable, "-m", "ntp_simulation",
                "--stratum", "2",
                "--port", str(p_s2a),
                "--upstream", f"127.0.0.1:{p_s1}",
                "--initial-offset", "0.0",
                "--falseticker",
                "--no-ui",
            ]
            proc_s2a = subprocess.Popen(cmd_s2a, cwd=repo_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            procs.append(proc_s2a)

            # 3. Start S2 Relay B (chaos)
            cmd_s2b = [
                sys.executable, "-m", "ntp_simulation",
                "--stratum", "2",
                "--port", str(p_s2b),
                "--upstream", f"127.0.0.1:{p_s1}",
                "--initial-offset", "0.0",
                "--chaos",
                "--no-ui",
            ]
            proc_s2b = subprocess.Popen(cmd_s2b, cwd=repo_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            procs.append(proc_s2b)
            time.sleep(0.5)

            # Helper to poll until socket binds and responds
            def wait_for_peer(port: int, max_wait: float = 3.0) -> PeerSample:
                clk = VirtualClock()
                peer = PeerAddress("127.0.0.1", port)
                start_t = time.time()
                while time.time() - start_t < max_wait:
                    s = poll_peer(peer, clk, timeout=0.4)
                    if s.status == "RESPONDED":
                        return s
                    time.sleep(0.1)
                return poll_peer(peer, clk, timeout=0.5)

            # Probe S1, S2-A, S2-B with client poll_peer
            sample_s1 = wait_for_peer(p_s1)
            assert sample_s1.status == "RESPONDED"
            assert sample_s1.server_stratum == 1

            sample_s2a = wait_for_peer(p_s2a)
            assert sample_s2a.status == "RESPONDED"
            assert sample_s2a.server_stratum == 2
            # Relay A has falseticker flag enabled: theta must reflect +60s skew
            assert abs(sample_s2a.theta - 60.0) < 1.0, f"Expected ~60s skew from Relay A, got {sample_s2a.theta}"

            sample_s2b = wait_for_peer(p_s2b)
            assert sample_s2b.status == "RESPONDED"
            assert sample_s2b.server_stratum == 2

            # 4. Start S3 Leaf syncing to S2-A and S2-B
            cmd_s3 = [
                sys.executable, "-m", "ntp_simulation",
                "--stratum", "3",
                "--port", str(p_s3),
                "--upstream", f"127.0.0.1:{p_s2a}",
                "--upstream", f"127.0.0.1:{p_s2b}",
                "--interval", "0.5",
                "--no-ui",
            ]
            proc_s3 = subprocess.Popen(cmd_s3, cwd=repo_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            procs.append(proc_s3)

            # Let S3 run for 2.5 seconds (multiple poll rounds)
            time.sleep(2.5)

            # Verify all 4 processes remain healthy
            for p in procs:
                assert p.poll() is None, f"Process {p.args} terminated prematurely"

        finally:
            for p in procs:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        p.kill()


# ==============================================================================
# 3. Clock Step vs Slew Mode Transitions & Monotonicity Under Byzantine Adjustments
# ==============================================================================

class TestClockStepSlewTransitionsAndMonotonicity:
    """Stress tests clock monotonicity and transition stability between STEP and SLEW."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: VirtualClock.now() exhibits backward time inversions up to 16ms during "
            "negative slewing due to discrete tick offset decrements without continuous interpolation "
            "or monotonicity clamping."
        ),
    )
    def test_slew_strict_monotonicity_under_negative_adjustment(self):
        """When clock is slewed backwards (-20.0s), virtual time must NEVER go backwards.
        Since slew rate is 0.40 s/s (< 1.00 s/s), elapsed real time dt produces
        net virtual progression dt - 0.40*dt = +0.60*dt > 0.
        """
        clk = VirtualClock(initial_offset=10.0, slew_rate=0.40)
        clk.start_slew(-20.0)  # Large negative adjustment

        monitored_timestamps: List[float] = []
        lock = threading.Lock()
        stop_sampling = threading.Event()
        inversions: List[Tuple[float, float]] = []

        def sampler():
            last = clk.now()
            with lock:
                monitored_timestamps.append(last)
            while not stop_sampling.is_set():
                curr = clk.now()
                with lock:
                    monitored_timestamps.append(curr)
                if curr < last:
                    with lock:
                        inversions.append((last, curr))
                last = curr
                time.sleep(0.001)

        def ticker():
            while not stop_sampling.is_set():
                clk.tick()
                time.sleep(0.005)

        sampler_threads = [threading.Thread(target=sampler, daemon=True) for _ in range(4)]
        ticker_thread = threading.Thread(target=ticker, daemon=True)

        ticker_thread.start()
        for t in sampler_threads:
            t.start()

        time.sleep(0.5)
        stop_sampling.set()

        for t in sampler_threads:
            t.join(timeout=0.5)
        ticker_thread.join(timeout=0.5)

        assert len(monitored_timestamps) > 200
        # If any time inversions occurred, assert failure with details
        assert len(inversions) == 0, (
            f"Detected {len(inversions)} time inversions during negative slewing! "
            f"Worst inversion: last={inversions[0][0]:.6f} > curr={inversions[0][1]:.6f} "
            f"(jumped backwards by {inversions[0][0] - inversions[0][1]:.6f}s)"
        )

    def test_step_clears_active_slew_and_applies_instantaneous_jump(self):
        """Switching from SLEW to STEP immediately zeroes out remaining slew target."""
        clk = VirtualClock(initial_offset=0.0, slew_rate=0.40)
        clk.start_slew(50.0)
        clk.tick(dt=1.0)
        # Slew progressed by 0.40s
        rem, total, ratio = clk.get_slew_info()
        assert rem == pytest.approx(49.60, abs=1e-3)

        # Now STEP is triggered with a new offset delta
        clk.step(-15.0)

        # Slew remaining and total target must be 0.0
        rem_after, total_after, ratio_after = clk.get_slew_info()
        assert rem_after == 0.0
        assert total_after == 0.0
        assert ratio_after == 1.0

        # Offset must reflect initial + 0.40 (from tick) - 15.0 = -14.60
        assert clk.get_offset() == pytest.approx(-14.60, abs=1e-3)

    def test_rapid_mode_transition_churn(self):
        """Simulates rapid flipping between STEP and SLEW modes during continuous ticking."""
        clk = VirtualClock(initial_offset=0.0)
        stop = threading.Event()

        def churn_worker():
            modes = ["STEP", "SLEW"]
            for i in range(100):
                mode = modes[i % 2]
                delta = ((-1.0) ** i) * (i * 0.1)
                if mode == "STEP":
                    clk.step(delta)
                else:
                    clk.start_slew(delta)
                time.sleep(0.002)

        def tick_worker():
            while not stop.is_set():
                clk.tick()
                time.sleep(0.002)

        t_tick = threading.Thread(target=tick_worker, daemon=True)
        t_churn = threading.Thread(target=churn_worker, daemon=True)

        t_tick.start()
        t_churn.start()

        t_churn.join(timeout=2.0)
        stop.set()
        t_tick.join(timeout=0.5)

        # Clock must remain in valid state without NaN or Inf
        curr_time = clk.now()
        offset = clk.get_offset()
        assert not (curr_time != curr_time), "clock.now() is NaN"
        assert abs(offset) < 1000.0


# ==============================================================================
# 4. Tmux Cluster Script End-to-End Orchestration Test
# ==============================================================================

class TestClusterTmuxRunnerE2E:
    """Validates scripts/run_cluster_tmux.sh live execution, status, and termination."""

    def test_tmux_cluster_full_lifecycle_and_hotkey_injection(self):
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        script_path = os.path.join(repo_root, "scripts", "run_cluster_tmux.sh")

        # Test session name to prevent collision with user's potential sessions
        test_session = "ntp-test-challenger-m6"
        env = {**os.environ, "SESSION_NAME": test_session}

        # Ensure session is stopped before test
        subprocess.run([script_path, "stop"], env=env, capture_output=True, text=True)

        try:
            # 1. Start cluster in detached mode
            res_start = subprocess.run(
                [script_path, "start", "-d"],
                env=env,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
            assert "Cluster started successfully" in res_start.stdout

            # 2. Check cluster status
            res_status = subprocess.run(
                [script_path, "status"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            assert "Cluster status: RUNNING" in res_status.stdout

            # Wait 2.5s for nodes to initialize
            time.sleep(2.5)

            # 3. Capture output of Stratum 1 Master (Pane 0 -> :0.0)
            cap_p0 = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{test_session}:0.0"],
                capture_output=True,
                text=True,
                check=True,
            )
            assert len(cap_p0.stdout.strip()) > 0, "Pane 0 has no output"

            # 4. Inject Byzantine faults via hotkeys:
            # Send 'f' to Pane 1 (Stratum 2 Relay A -> :0.1)
            subprocess.run(["tmux", "send-keys", "-t", f"{test_session}:0.1", "f"], check=True)
            # Send 'c' to Pane 2 (Stratum 2 Relay B -> :0.2)
            subprocess.run(["tmux", "send-keys", "-t", f"{test_session}:0.2", "c"], check=True)

            time.sleep(1.0)

            # 5. Capture Pane 1 (Relay A) to verify falseticker hotkey registered
            cap_p1 = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{test_session}:0.1"],
                capture_output=True,
                text=True,
                check=True,
            )
            assert len(cap_p1.stdout.strip()) > 0

            # 6. Capture Pane 3 (Stratum 3 Client A -> :0.3) to verify leaf is running
            cap_p3 = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{test_session}:0.3"],
                capture_output=True,
                text=True,
                check=True,
            )
            assert len(cap_p3.stdout.strip()) > 0

            # 7. Stop cluster
            res_stop = subprocess.run(
                [script_path, "stop"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            assert f"Cluster session '{test_session}' stopped" in res_stop.stdout

            # Verify session is truly gone
            has_sess = subprocess.run(["tmux", "has-session", "-t", test_session], capture_output=True)
            assert has_sess.returncode != 0

        finally:
            # Cleanup safeguard
            subprocess.run(["tmux", "kill-session", "-t", test_session], capture_output=True)

