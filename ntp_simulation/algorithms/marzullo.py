"""Authentic Marzullo Intersection Algorithm for NTP falseticker rejection.

Implements Keith Marzullo's sweep-line algorithm (1984) for distributed time synchronization:
- Transforms peer offset and delay measurements into confidence intervals:
    [theta_i - delay_i / 2, theta_i + delay_i / 2]
- Decomposes intervals into an endpoint table sorted with start (-1) before end (+1).
- Sweeps line across endpoints searching for the minimum number of falsetickers f < n / 2
  such that a strict majority n - f > n / 2 of peers intersect in consensus interval [c_low, c_high].
- Classifies peers as [TRUECHIMER] vs [FALSETICKER].
- Selects the optimal synchronization candidate with minimum delay among verified truechimers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ntp_simulation.models.peer import PeerSample


@dataclass
class MarzulloResult:
    """Outcome of Marzullo sweep-line intersection filtering."""

    truechimers: List[str] = field(default_factory=list)
    falsetickers: List[str] = field(default_factory=list)
    chosen_peer: Optional[str] = None
    intersection_interval: Optional[Tuple[float, float]] = None
    max_overlap: int = 0
    total_peers: int = 0
    tolerated_falsetickers: int = 0
    peer_status: Dict[str, dict] = field(default_factory=dict)

    @property
    def has_consensus(self) -> bool:
        """Returns True if a majority consensus interval was found."""
        return self.chosen_peer is not None and self.intersection_interval is not None


def compute_interval(
    theta: float,
    delay: float,
    dispersion_floor: float = 0.0,
) -> Tuple[float, float]:
    """Computes confidence interval [theta - half_width, theta + half_width].
    
    Includes non-negative half-delay plus an optional dispersion/jitter floor
    (RFC 5905 synchronization distance floor) to prevent false positives on
    near-zero delay networks.
    """
    eff_delay = max(0.0, float(delay))
    half_width = (eff_delay / 2.0) + max(0.0, float(dispersion_floor))
    return (float(theta) - half_width, float(theta) + half_width)


def _extract_sample_data(sample: Any) -> Tuple[str, float, float]:
    """Extracts (peer_id, theta, delay) from a PeerSample or dict."""
    if isinstance(sample, dict):
        pid = str(sample.get("peer_id") or sample.get("peer") or "unknown")
        return pid, float(sample.get("theta", 0.0)), float(sample.get("delay", 0.0))
    return str(sample.peer_id), float(sample.theta), float(sample.delay)


def marzullo_intersection(
    samples: List[Any],
    dispersion_floor: float = 0.0,
) -> MarzulloResult:
    """Executes the authentic Marzullo sweep-line intersection algorithm.

    Given confidence intervals [theta_i - (delay_i / 2 + dispersion_floor), theta_i + (delay_i / 2 + dispersion_floor)] for n peers,
    finds the smallest interval containing points that lie in the largest number of
    intervals while tolerating up to f < n / 2 falsetickers (strict majority).

    Args:
        samples: List of peer samples (PeerSample, dict, or object with theta & delay).
        dispersion_floor: Optional minimum dispersion/jitter uncertainty floor in seconds (default 0.0).

    Returns:
        MarzulloResult with classified truechimers, falsetickers, consensus interval,
        and selected synchronization candidate.
    """
    deduped: Dict[str, Tuple[str, float, float, Any]] = {}
    for s in samples:
        pid, theta, delay = _extract_sample_data(s)
        deduped[pid] = (pid, theta, delay, s)
    extracted: List[Tuple[str, float, float, Any]] = list(deduped.values())
    n = len(extracted)
    if n == 0:
        return MarzulloResult()

    # 1. Build endpoint table: (coordinate, type, peer_id)
    # type: -1 = interval start, +1 = interval end
    endpoints: List[Tuple[float, int, str]] = []
    peer_intervals: Dict[str, Tuple[float, float]] = {}
    sample_by_id: Dict[str, Tuple[float, float, Any]] = {}

    for pid, theta, delay, original_sample in extracted:
        sample_by_id[pid] = (theta, delay, original_sample)
        low, high = compute_interval(theta, delay, dispersion_floor=dispersion_floor)
        peer_intervals[pid] = (low, high)
        endpoints.append((low, -1, pid))
        endpoints.append((high, 1, pid))

    # 2. Sort endpoints:
    # Primary key: coordinate value (ascending)
    # Secondary key: event type (-1 before +1, guaranteeing start before end at equal coordinates)
    # Tertiary key: peer_id (deterministic tie-breaker)
    endpoints.sort(key=lambda item: (item[0], item[1], str(item[2])))

    # 3. Sweep-line search for minimal falsetickers f < n / 2 (strict majority)
    best_interval: Optional[Tuple[float, float]] = None
    best_f = 0
    found = False

    max_allowed_f = (n - 1) // 2

    for f in range(0, max_allowed_f + 1):
        target_count = n - f

        # Forward sweep: find lower bound c_low
        count = 0
        c_low: Optional[float] = None
        for val, event_type, _ in endpoints:
            count -= event_type  # -(-1) = +1 (entered interval), -(+1) = -1 (exited interval)
            if count >= target_count:
                c_low = val
                break

        # Backward sweep: find upper bound c_high
        count = 0
        c_high: Optional[float] = None
        for val, event_type, _ in reversed(endpoints):
            count += event_type  # +(+1) = +1 (entered interval in reverse), +(-1) = -1
            if count >= target_count:
                c_high = val
                break

        if c_low is not None and c_high is not None and c_low <= c_high:
            best_interval = (c_low, c_high)
            best_f = f
            found = True
            break

    peer_status: Dict[str, dict] = {}
    truechimers: List[str] = []
    falsetickers: List[str] = []

    if not found or best_interval is None:
        # No majority consensus exists (f >= n / 2)
        for pid, theta, delay, _ in extracted:
            falsetickers.append(pid)
            peer_status[pid] = {
                "classification": "[FALSETICKER]",
                "action": "DISCARDED (No Consensus)",
                "interval": peer_intervals[pid],
                "theta": theta,
                "delay": delay,
            }
        return MarzulloResult(
            truechimers=[],
            falsetickers=falsetickers,
            chosen_peer=None,
            intersection_interval=None,
            max_overlap=0,
            total_peers=n,
            tolerated_falsetickers=n,
            peer_status=peer_status,
        )

    # 4. Classify truechimers vs falsetickers
    c_low, c_high = best_interval
    for pid, theta, delay, _ in extracted:
        p_low, p_high = peer_intervals[pid]
        # Intersection test: [p_low, p_high] intersects [c_low, c_high]
        if max(p_low, c_low) <= min(p_high, c_high):
            truechimers.append(pid)
            peer_status[pid] = {
                "classification": "[TRUECHIMER]",
                "action": "CANDIDATE",
                "interval": (p_low, p_high),
                "theta": theta,
                "delay": delay,
            }
        else:
            falsetickers.append(pid)
            peer_status[pid] = {
                "classification": "[FALSETICKER]",
                "action": "DISCARDED (Outlier)",
                "interval": (p_low, p_high),
                "theta": theta,
                "delay": delay,
            }

    # 5. Candidate selection: minimum delay among truechimers
    # Tie-break 1: minimum |theta|
    # Tie-break 2: peer_id
    chosen_peer = min(
        truechimers,
        key=lambda p: (
            sample_by_id[p][1],       # delay
            abs(sample_by_id[p][0]),  # abs(theta)
            str(p),                   # peer_id
        ),
    )
    peer_status[chosen_peer]["action"] = "SELECTED FOR SYNC"

    return MarzulloResult(
        truechimers=truechimers,
        falsetickers=falsetickers,
        chosen_peer=chosen_peer,
        intersection_interval=best_interval,
        max_overlap=n - best_f,
        total_peers=n,
        tolerated_falsetickers=best_f,
        peer_status=peer_status,
    )


def marzullo_evaluate(
    samples: List[Any],
    dispersion_floor: float = 0.050,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Evaluates peer samples using Marzullo intersection and constructs PeerEvaluation records.

    Handles both responsive samples and failed/timed-out peer samples.
    Includes an RFC 5905-compliant dispersion/jitter floor (default 50ms) to prevent
    spurious rejection of honest peers on low-latency local networks.

    Args:
        samples: List of PeerSample, dicts, or objects with peer_id/theta/delay.
        dispersion_floor: Minimum uncertainty floor in seconds (default 0.050s = 50ms).

    Returns:
        Tuple of (reports_dict, chosen_peer_key)
    """
    from ntp_simulation.models.peer import PeerEvaluation

    deduped: Dict[str, Any] = {}
    for s in samples:
        pid, theta, delay = _extract_sample_data(s)
        get = s.get if isinstance(s, dict) else lambda k, d=None, obj=s: getattr(obj, k, d)
        deduped[pid] = (
            s, theta, delay, get("status", "RESPONDED"),
            get("server_stratum"), get("t1"), get("t2"), get("t3"), get("t4"),
        )

    responded = [item[0] for item in deduped.values() if item[3] == "RESPONDED"]
    result = marzullo_intersection(responded, dispersion_floor=dispersion_floor) if responded else MarzulloResult()

    reports: Dict[str, PeerEvaluation] = {}
    for pid, (_, theta, delay, status, stratum, t1, t2, t3, t4) in deduped.items():
        info = result.peer_status.get(pid, {})
        ok = status == "RESPONDED"
        reports[pid] = PeerEvaluation(
            peer_key=pid,
            stratum=stratum,
            delay=delay,
            theta=theta,
            status=status,
            classification=info.get("classification", "[UNKNOWN]") if ok else "[UNREACHABLE]",
            action=info.get("action", "PENDING") if ok else f"DISCARDED ({status})",
            t1=t1,
            t2=t2,
            t3=t3,
            t4=t4,
            interval=info.get("interval", compute_interval(theta, delay, dispersion_floor=dispersion_floor)) if ok else None,
        )

    return reports, result.chosen_peer

