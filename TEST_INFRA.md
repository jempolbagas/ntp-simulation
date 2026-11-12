# Test Infrastructure & Verification Architecture

## Overview
This document specifies the testing architecture, validation methodology, and verification framework for the **NTP Simulation Modular Architecture & Authentic Marzullo Engine**.

The test suite is structured as an opaque-box, contract-driven test system adhering strictly to `ORIGINAL_REQUEST.md` and `PROJECT.md` specifications.

---

## Test Architecture & Layout

```
ntp-simulation/tests/
├── __init__.py             # Test package initialization
├── test_models.py          # Tier 1: Pydantic schemas, Nonce, Timing Math, Config, Telemetry
├── test_marzullo.py        # Tier 2: Authentic Marzullo Algorithm & Falseticker Rejection
├── test_clock.py           # Tier 3: VirtualClock Engine, Stepping, Slewing, Brownian Drift
└── test_cli_cluster.py     # Tier 4: CLI Argument Parsing & Local Tmux Runner Script
```

---

## Test Tiers & Specifications

### Tier 1: Models & Packet Schemas (`test_models.py`)
- **Authoritative Source**: `ORIGINAL_REQUEST.md` (R1, R3), `PROJECT.md` (§ Interface Contracts 1, 2)
- **Coverage**:
  - `NTPRequest`: Cryptographic correlation nonce auto-generation (16-char hex, min length 8), client identifier, JSON serialization roundtrip (`to_bytes()` / `from_bytes()`), invalid payload rejection.
  - `NTPResponse`: Server timestamp echoes ($t_1, t_2, t_3$), stratum level bounds validation ($1 \le \text{stratum} \le 16$), fault injection metadata (`is_falseticker`, `skew_applied`), serialization fidelity.
  - `PeerAddress`: Parsing from `"HOST:PORT"` string, validation against malformed strings, port boundary checks ($1 \le \text{port} \le 65535$).
  - `PeerSample`: Exact mathematical delay calculation $\delta = (t_4 - t_1) - (t_3 - t_2)$, offset $\theta = ((t_2 - t_1) + (t_3 - t_4)) / 2$, confidence interval $[\theta - \delta/2, \theta + \delta/2]$ with non-negative delay clamping.
  - `NodeConfig`: Stratum validation ($1 \le \text{stratum} \le 3$), upstream requirements for Stratum 2 & 3, sync mode validation (`"STEP"` vs `"SLEW"`), numerical bounds on interval, slew rate, chaos probability.
  - `Telemetry Models`: `ServerLogEntry`, `SyncHistoryEntry`, and `TelemetrySnapshot`.

### Tier 2: Mathematical Algorithms (`test_marzullo.py`)
- **Authoritative Source**: Keith Marzullo (1984) Ph.D. Dissertation, `ORIGINAL_REQUEST.md` (R2)
- **Coverage**:
  - `compute_interval(theta, delay)`: Construction of $[\theta - \delta/2, \theta + \delta/2]$ with clamped $\delta \ge 0$.
  - **Endpoint Table Tie-Breaking**: Sorting $(A_i, -1, i)$ before $(B_k, +1, k)$ when $A_i = B_k$ at equal coordinates.
  - **Consensus Search**: Sweep-line search for minimal falsetickers $f < n/2$ maintaining strict majority ($n - f > n/2$).
  - **Falseticker Rejection Scenarios**:
    1. *Empty Peers* ($n=0$): Clean empty `MarzulloResult`.
    2. *Single Peer* ($n=1$): Unconditional truechimer selection.
    3. *Two Overlapping Peers*: Lowest delay candidate selected.
    4. *Three Peers (1 Falseticker with $+60$s skew)*: Outlier rejected into `falsetickers`, consensus interval identified.
    5. *Five Peers (2 Falsetickers)*: $f=2$ outliers rejected, candidate selected from remaining 3 truechimers.
    6. *Disjoint Split Quorum* ($2$ vs $2$ or $n=2$ disjoint): Strict majority failure correctly yields `chosen_peer = None`.
    7. *Touching Intervals*: Zero-width point consensus $[x, x]$ correctly captured.
    8. *Candidate Selection Tie-Breaks*: Minimum $\delta_i \to$ minimum $|\theta_i| \to$ alphabetical `peer_id`.
    9. *Adversarial Stress*: All-falsetickers, unanimous identical intervals, 10-peer cluster, dictionary input duck typing.

### Tier 3: Clock Dynamics & Timekeeping (`test_clock.py`)
- **Authoritative Source**: `ORIGINAL_REQUEST.md` (R1), `PROJECT.md` (§ Interface Contract 3)
- **Coverage**:
  - `now()`: Thread-safe virtual time calculation $\text{time.time}() + \text{offset}$.
  - `step(delta)`: Instantaneous offset jump, clearing of active slew remainders.
  - `start_slew(delta)` & `tick()`: Gradual continuous adjustment rate-limited at $R_{\text{slew}} = 0.40$ s/s, progress ratio tracking from $0.0 \to 1.0$.
  - **Brownian Quartz Drift Simulation**: Random walk target wander ($\pm 25$ ms/s bounds), exponential relaxation, offset phase accumulation.
  - **Concurrency & Thread Safety**: Multi-threaded read/write stress testing without deadlocks or state tearing.

### Tier 4: CLI & Cluster Runner (`test_cli_cluster.py`)
- **Authoritative Source**: `ORIGINAL_REQUEST.md` (R1, R4, R5), `PROJECT.md` (Features 19, 20)
- **Coverage**:
  - CLI argument parsing (`--stratum`, `--port`, `--upstream`, `--interval`, `--no-ui`, `--sync-mode`, `--drift`, `--falseticker`, `--chaos`).
  - Validation: Stratum $> 1$ without upstream raises `SystemExit`.
  - Cluster runner script `scripts/run_cluster_tmux.sh`: Executable permission verification (`chmod +x`), bash syntax validity check (`bash -n`), session name and port allocation validation (ports 5001–5006).

---

## Progressive Testability Design

The test suite is structured to support **Progressive Testability**:
- Tests for components under active development use standard `pytest.importorskip(...)` or fixture guards.
- When running `pytest tests/` during early milestones (e.g. Milestone 1), implemented modules (`test_models.py`, `test_marzullo.py`) execute fully, while downstream modules (`test_clock.py`, `test_cli_cluster.py`) cleanly skip without breaking test runs.
- As each subsequent milestone lands, the corresponding test tier activates automatically with zero test code modification.
- Upon completion of Milestone 6, all 4 tiers execute with 100% test pass rate.

---

## Test Execution

### Standard Runner Command
```bash
pytest tests/ -v
```

### Run by Specific Tier
```bash
# Models & Packet validation
pytest tests/test_models.py -v

# Marzullo Algorithm & Outlier Rejection
pytest tests/test_marzullo.py -v

# VirtualClock Dynamics
pytest tests/test_clock.py -v

# CLI & Cluster Runner
pytest tests/test_cli_cluster.py -v
```

### Run with Short Tracebacks
```bash
pytest tests/ -q --tb=short
```
