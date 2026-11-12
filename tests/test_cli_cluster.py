"""Tests for CLI argument parsing and cluster runner script.

Validates command-line options parsing, stratum upstream constraints,
and validates syntax and executable permissions of scripts/run_cluster_tmux.sh.
"""

import os
from pathlib import Path
import subprocess
import pytest

cli_module = pytest.importorskip("ntp_simulation.cli")
parse_args = cli_module.parse_args


# ==============================================================================
# CLI Argument Parsing Tests
# ==============================================================================

class TestCLIParsing:
    def test_stratum1_minimal_args(self):
        args = parse_args(["--stratum", "1", "--port", "5001"])
        assert args.stratum == 1
        assert args.port == 5001
        assert args.interval == 4.0
        assert args.no_ui is False
        assert args.sync_mode == "STEP"
        assert args.drift is False
        assert args.falseticker is False
        assert args.chaos is False

    def test_stratum2_with_single_upstream(self):
        args = parse_args([
            "--stratum", "2",
            "--port", "5002",
            "--upstream", "127.0.0.1:5001"
        ])
        assert args.stratum == 2
        assert args.port == 5002
        assert len(args.upstream) == 1
        assert args.upstream[0].host == "127.0.0.1"
        assert args.upstream[0].port == 5001

    def test_stratum3_with_multiple_upstreams(self):
        args = parse_args([
            "--stratum", "3",
            "--port", "5004",
            "--upstream", "127.0.0.1:5002", "127.0.0.1:5003",
            "--interval", "2.5",
            "--sync-mode", "SLEW",
        ])
        assert args.stratum == 3
        assert len(args.upstream) == 2
        assert args.interval == 2.5
        assert args.sync_mode == "SLEW"

    def test_stratum2_missing_upstream_raises_system_exit(self):
        with pytest.raises(SystemExit):
            parse_args(["--stratum", "2", "--port", "5002"])

    def test_stratum3_missing_upstream_raises_system_exit(self):
        with pytest.raises(SystemExit):
            parse_args(["--stratum", "3", "--port", "5004"])

    def test_invalid_stratum_value_raises(self):
        with pytest.raises(SystemExit):
            parse_args(["--stratum", "4"])

    def test_invalid_upstream_address_format_raises(self):
        with pytest.raises(SystemExit):
            parse_args(["--stratum", "2", "--upstream", "missing_port_host"])

    def test_interactive_flags(self):
        args = parse_args([
            "--stratum", "1",
            "--no-ui",
            "--drift",
            "--falseticker",
            "--chaos",
        ])
        assert args.no_ui is True
        assert args.drift is True
        assert args.falseticker is True
        assert args.chaos is True


# ==============================================================================
# Cluster Tmux Script Validation
# ==============================================================================

class TestClusterScript:
    @pytest.fixture
    def script_path(self) -> Path:
        # Project root is parent of tests/
        root = Path(__file__).resolve().parent.parent
        return root / "scripts" / "run_cluster_tmux.sh"

    def test_cluster_script_executable_and_syntax(self, script_path):
        if not script_path.exists():
            pytest.skip("scripts/run_cluster_tmux.sh not implemented yet (Milestone M5)")

        # Verify executable permissions
        assert os.access(script_path, os.X_OK), f"{script_path} must have executable permissions (chmod +x)"

        # Verify bash syntax without execution
        result = subprocess.run(
            ["bash", "-n", str(script_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax check failed: {result.stderr}"

    def test_cluster_script_contains_expected_topology(self, script_path):
        if not script_path.exists():
            pytest.skip("scripts/run_cluster_tmux.sh not implemented yet (Milestone M5)")

        content = script_path.read_text(encoding="utf-8")
        # Must reference ports 5001 through 5006
        for port in [5001, 5002, 5003, 5004, 5005, 5006]:
            assert str(port) in content, f"Port {port} expected in cluster script"

        # Must mention session name and subcommands
        assert "ntp-cluster" in content
        assert "start" in content
        assert "stop" in content
