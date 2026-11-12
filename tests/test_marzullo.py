"""Unit tests for Authentic Marzullo Intersection Algorithm.

Validates interval construction, endpoint sorting with (-1 before +1) tie-breaking,
majority consensus interval evaluation, falseticker rejection under multiple scenarios
(single peer, unanimous agreement, +60s skew, multiple outliers, split quorum,
touching intervals), candidate selection with tie-breakers, and adversarial stress tests.
"""

import pytest

pytest.importorskip("ntp_simulation.algorithms")

from ntp_simulation.algorithms.marzullo import (
    MarzulloResult,
    compute_interval,
    marzullo_intersection,
)

# Support either dataclass PeerSample from algorithms or Pydantic PeerSample from models
try:
    from ntp_simulation.algorithms.marzullo import PeerSample
except ImportError:
    from ntp_simulation.models.peer import PeerAddress, PeerSample as ModelPeerSample

    def PeerSample(peer_id: str, theta: float, delay: float, stratum: int = 1):
        host, port = (peer_id.split(":") if ":" in peer_id else (peer_id, "5000"))
        return ModelPeerSample(
            peer=PeerAddress(host=host, port=int(port)),
            t1=100.0,
            t2=100.0 + theta + delay / 2.0,
            t3=100.0 + theta + delay / 2.0,
            t4=100.0 + delay,
            server_stratum=stratum,
        )


def make_sample(peer_id: str, theta: float, delay: float, stratum: int = 1):
    """Helper creating a sample compatible with marzullo_intersection."""
    try:
        return PeerSample(peer_id=peer_id, theta=theta, delay=delay, stratum=stratum)
    except TypeError:
        # If Pydantic model requiring peer object
        from ntp_simulation.models.peer import PeerAddress, PeerSample as PydanticSample
        return PydanticSample(
            peer=PeerAddress(host=peer_id, port=5000),
            t1=0.0,
            t2=theta + delay / 2.0,
            t3=theta + delay / 2.0,
            t4=delay,
            server_stratum=stratum,
        )


# ==============================================================================
# Interval Calculation Tests
# ==============================================================================

class TestIntervalComputation:
    def test_compute_interval_positive_delay(self):
        # theta = 0.050, delay = 0.020 -> [0.050 - 0.010, 0.050 + 0.010] = [0.040, 0.060]
        low, high = compute_interval(0.050, 0.020)
        assert low == pytest.approx(0.040, abs=1e-9)
        assert high == pytest.approx(0.060, abs=1e-9)

    def test_compute_interval_zero_delay(self):
        low, high = compute_interval(1.500, 0.0)
        assert low == high == pytest.approx(1.500, abs=1e-9)

    def test_compute_interval_negative_delay_clamped(self):
        # Negative delay must be clamped to 0.0 to prevent invalid inverted intervals
        low, high = compute_interval(2.000, -0.050)
        assert low == high == pytest.approx(2.000, abs=1e-9)


# ==============================================================================
# Sweep-Line and Outlier Rejection Scenarios
# ==============================================================================

class TestMarzulloAlgorithm:
    def test_zero_peers_returns_empty_result(self):
        result = marzullo_intersection([])
        assert isinstance(result, MarzulloResult)
        assert result.truechimers == []
        assert result.falsetickers == []
        assert result.chosen_peer is None
        assert result.intersection_interval is None
        assert result.has_consensus is False

    def test_single_peer_is_always_truechimer(self):
        s1 = make_sample("peer_1", theta=0.020, delay=0.010)
        result = marzullo_intersection([s1])
        assert result.truechimers == ["peer_1"]
        assert result.falsetickers == []
        assert result.chosen_peer == "peer_1"
        assert result.intersection_interval == (pytest.approx(0.015), pytest.approx(0.025))
        assert "peer_1" in result.peer_status
        assert result.peer_status["peer_1"]["classification"] == "[TRUECHIMER]"
        assert result.has_consensus is True

    def test_two_peers_overlapping_selects_lowest_delay(self):
        # Peer A: theta=0.000, delay=0.020 -> [-0.010, 0.010]
        # Peer B: theta=0.005, delay=0.010 -> [0.000, 0.010]
        # Both overlap in [0.000, 0.010]. Both are truechimers.
        # Peer B has lower delay (0.010 < 0.020) -> must be chosen for sync.
        sA = make_sample("peer_a", theta=0.000, delay=0.020)
        sB = make_sample("peer_b", theta=0.005, delay=0.010)
        result = marzullo_intersection([sA, sB])

        assert set(result.truechimers) == {"peer_a", "peer_b"}
        assert result.falsetickers == []
        assert result.chosen_peer == "peer_b"
        assert result.intersection_interval == (pytest.approx(0.000), pytest.approx(0.010))
        assert "SELECTED" in result.peer_status["peer_b"]["action"]
        assert result.has_consensus is True

    def test_three_peers_rejects_falseticker_with_60s_skew(self):
        # Standard NTP presentation scenario:
        # Peer 1 (honest): theta = 0.000, delay = 0.020 -> [-0.010, 0.010]
        # Peer 2 (honest): theta = 0.004, delay = 0.010 -> [-0.001, 0.009]
        # Peer 3 (faulty): theta = 60.000, delay = 0.020 -> [59.990, 60.010]
        # With n=3: f=1 tolerated (n-f = 2 > 3/2 = 1.5).
        # Peer 3 must be classified as FALSETICKER and rejected.
        s1 = make_sample("honest_1", theta=0.000, delay=0.020)
        s2 = make_sample("honest_2", theta=0.004, delay=0.010)
        s3 = make_sample("falseticker_3", theta=60.000, delay=0.020)

        result = marzullo_intersection([s1, s2, s3])

        assert set(result.truechimers) == {"honest_1", "honest_2"}
        assert result.falsetickers == ["falseticker_3"]
        assert result.chosen_peer == "honest_2"  # Lower delay
        assert result.tolerated_falsetickers == 1
        assert result.peer_status["falseticker_3"]["classification"] == "[FALSETICKER]"
        assert "DISCARDED" in result.peer_status["falseticker_3"]["action"]
        assert result.has_consensus is True

    def test_five_peers_tolerates_two_falsetickers(self):
        # n = 5: max tolerated f = (5-1)//2 = 2 (n-f = 3 > 2.5).
        # 3 honest peers clustered around 0, 2 extreme falsetickers.
        s1 = make_sample("p1", theta=0.000, delay=0.010)  # [-0.005, 0.005]
        s2 = make_sample("p2", theta=0.002, delay=0.020)  # [-0.008, 0.012]
        s3 = make_sample("p3", theta=-0.001, delay=0.016) # [-0.009, 0.007]
        s4 = make_sample("fault_pos", theta=60.0, delay=0.010) # +60s
        s5 = make_sample("fault_neg", theta=-45.0, delay=0.020) # -45s

        result = marzullo_intersection([s1, s2, s3, s4, s5])

        assert set(result.truechimers) == {"p1", "p2", "p3"}
        assert set(result.falsetickers) == {"fault_pos", "fault_neg"}
        assert result.chosen_peer == "p1"  # delay 0.010 is min among p1(0.010), p2(0.020), p3(0.016)
        assert result.tolerated_falsetickers == 2

    def test_disjoint_split_quorum_rejects_both(self):
        # 2 peers with disjoint intervals:
        # Peer 1: [0.0, 1.0] (theta=0.5, delay=1.0)
        # Peer 2: [5.0, 6.0] (theta=5.5, delay=1.0)
        # For n=2: max allowed f = 0 (n-f = 2 > 1.0).
        # Since they don't intersect, no majority exists.
        s1 = make_sample("peer_1", theta=0.5, delay=1.0)
        s2 = make_sample("peer_2", theta=5.5, delay=1.0)

        result = marzullo_intersection([s1, s2])

        assert result.truechimers == []
        assert result.chosen_peer is None
        assert result.intersection_interval is None
        assert set(result.falsetickers) == {"peer_1", "peer_2"}
        assert result.has_consensus is False

    def test_four_peers_two_vs_two_no_consensus(self):
        # n = 4: max allowed f = (4-1)//2 = 1 -> target count = 4 - 1 = 3 > 2.0.
        # Two peers at 0.0, two peers at 50.0. Neither group has 3 votes.
        s1 = make_sample("c1_a", theta=0.0, delay=0.1)
        s2 = make_sample("c1_b", theta=0.0, delay=0.1)
        s3 = make_sample("c2_a", theta=50.0, delay=0.1)
        s4 = make_sample("c2_b", theta=50.0, delay=0.1)

        result = marzullo_intersection([s1, s2, s3, s4])

        assert result.chosen_peer is None
        assert result.truechimers == []
        assert result.intersection_interval is None
        assert result.has_consensus is False

    def test_touching_intervals_form_point_intersection(self):
        # Due to tie-breaking (-1 for interval start before +1 for interval end),
        # touching intervals [0.0, 2.0] and [2.0, 4.0] intersect at point 2.0.
        # Peer A: theta=1.0, delay=2.0 -> [0.0, 2.0]
        # Peer B: theta=3.0, delay=2.0 -> [2.0, 4.0]
        sA = make_sample("peer_a", theta=1.0, delay=2.0)
        sB = make_sample("peer_b", theta=3.0, delay=2.0)

        result = marzullo_intersection([sA, sB])

        assert set(result.truechimers) == {"peer_a", "peer_b"}
        assert result.falsetickers == []
        assert result.intersection_interval == (pytest.approx(2.0), pytest.approx(2.0))

    def test_candidate_selection_tie_breaking_by_abs_theta(self):
        # Two truechimers with identical delay (0.010s):
        # Peer 1: theta = 0.008 (abs = 0.008)
        # Peer 2: theta = 0.002 (abs = 0.002)
        # Peer 2 has smaller absolute offset -> selected.
        s1 = make_sample("p1", theta=0.008, delay=0.010)
        s2 = make_sample("p2", theta=0.002, delay=0.010)

        result = marzullo_intersection([s1, s2])
        assert result.chosen_peer == "p2"

    def test_candidate_selection_tie_breaking_by_peer_id(self):
        # Two truechimers with identical delay (0.010s) and identical theta (0.005s):
        # Tie-break alphabetically by peer_id: "alpha" < "beta".
        s1 = make_sample("beta", theta=0.005, delay=0.010)
        s2 = make_sample("alpha", theta=0.005, delay=0.010)

        result = marzullo_intersection([s1, s2])
        assert result.chosen_peer == "alpha"

    def test_adversarial_all_peers_falsetickers(self):
        # 3 peers with completely disjoint intervals:
        # Peer 1: [0.0, 0.1]
        # Peer 2: [100.0, 100.1]
        # Peer 3: [200.0, 200.1]
        # With n=3, target majority is n-f >= 2. Overlap is never > 1.
        s1 = make_sample("p1", theta=0.05, delay=0.1)
        s2 = make_sample("p2", theta=100.05, delay=0.1)
        s3 = make_sample("p3", theta=200.05, delay=0.1)

        result = marzullo_intersection([s1, s2, s3])
        assert result.has_consensus is False
        assert result.chosen_peer is None
        assert len(result.truechimers) == 0
        assert set(result.falsetickers) == {"p1", "p2", "p3"}

    def test_adversarial_unanimous_identical_intervals(self):
        # 4 peers with exact same interval [0.0, 0.02]
        samples = [make_sample(f"peer_{i}", theta=0.01, delay=0.02) for i in range(4)]
        result = marzullo_intersection(samples)
        assert result.has_consensus is True
        assert len(result.truechimers) == 4
        assert len(result.falsetickers) == 0
        assert result.chosen_peer == "peer_0"  # Alphabetical tie-break

    def test_adversarial_ten_peers_cluster(self):
        # n = 10: max allowed f = (10-1)//2 = 4 (target count >= 10 - 4 = 6).
        # 7 honest peers in [-0.01, +0.01], 3 malicious peers (+60, -30, +100).
        honest = [
            make_sample(f"honest_{i}", theta=i * 0.001, delay=0.01 + i * 0.001)
            for i in range(7)
        ]
        faults = [
            make_sample("fault_1", theta=60.0, delay=0.01),
            make_sample("fault_2", theta=-30.0, delay=0.02),
            make_sample("fault_3", theta=100.0, delay=0.01),
        ]
        result = marzullo_intersection(honest + faults)

        assert result.has_consensus is True
        assert len(result.truechimers) == 7
        assert len(result.falsetickers) == 3
        assert set(result.falsetickers) == {"fault_1", "fault_2", "fault_3"}
        assert result.chosen_peer == "honest_0"  # Lowest delay (0.010)

    def test_dict_input_compatibility(self):
        # marzullo_intersection should accept plain dictionaries with theta and delay
        raw_samples = [
            {"peer_id": "d1", "theta": 0.000, "delay": 0.020},
            {"peer_id": "d2", "theta": 0.002, "delay": 0.010},
            {"peer_id": "d3", "theta": 50.000, "delay": 0.020},
        ]
        result = marzullo_intersection(raw_samples)
        assert result.has_consensus is True
        assert set(result.truechimers) == {"d1", "d2"}
        assert result.falsetickers == ["d3"]
        assert result.chosen_peer == "d2"

    def test_dispersion_floor_prevents_false_positives_on_low_latency_lan(self):
        """On localhost/LAN with 0.2ms delay, two honest peers with 12ms natural difference
        must be rescued by the RFC 5905 dispersion floor (50ms) rather than discarded."""
        p1 = make_sample("p1", theta=0.008, delay=0.0002)
        p2 = make_sample("p2", theta=-0.004, delay=0.0002)

        # Without dispersion floor (pure theoretical): disjoint intervals, no consensus
        raw_result = marzullo_intersection([p1, p2], dispersion_floor=0.0)
        assert raw_result.has_consensus is False
        assert len(raw_result.truechimers) == 0

        # With RFC 5905 dispersion floor (50ms): overlapping intervals, consensus achieved
        calibrated_result = marzullo_intersection([p1, p2], dispersion_floor=0.050)
        assert calibrated_result.has_consensus is True
        assert set(calibrated_result.truechimers) == {"p1", "p2"}
        assert len(calibrated_result.falsetickers) == 0

    def test_dispersion_floor_still_decisively_rejects_falsetickers(self):
        """Even with a 50ms dispersion floor, a +60s falseticker is rejected."""
        honest1 = make_sample("h1", theta=0.005, delay=0.001)
        honest2 = make_sample("h2", theta=-0.002, delay=0.001)
        faulty = make_sample("fault", theta=60.000, delay=0.001)

        result = marzullo_intersection([honest1, honest2, faulty], dispersion_floor=0.050)
        assert result.has_consensus is True
        assert set(result.truechimers) == {"h1", "h2"}
        assert result.falsetickers == ["fault"]
