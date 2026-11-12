"""Unit tests for NTP Simulation Pydantic models.

Validates packet schemas, cryptographic nonce integrity, peer addresses,
timing calculations (delay, theta, interval), node configuration rules,
telemetry structures, and adversarial boundary cases.
"""

import pytest
import pydantic

pytest.importorskip("ntp_simulation.models")

from ntp_simulation.models.packet import NTPRequest, NTPResponse
from ntp_simulation.models.peer import PeerAddress, PeerEvaluation, PeerSample
from ntp_simulation.models.config import NodeConfig
from ntp_simulation.models.telemetry import ServerLogEntry, SyncHistoryEntry, TelemetrySnapshot


# ==============================================================================
# NTPRequest Tests
# ==============================================================================

class TestNTPRequest:
    def test_create_auto_generates_cryptographic_nonce(self):
        t1 = 1700000000.123456
        req = NTPRequest.create(t1=t1)
        assert req.t1 == t1
        assert isinstance(req.nonce, str)
        assert len(req.nonce) >= 8
        assert req.client_id is None

    def test_create_with_explicit_nonce_and_client_id(self):
        t1 = 1700000000.5
        req = NTPRequest.create(t1=t1, nonce="aabbccddeeff0011", client_id="127.0.0.1:5004")
        assert req.t1 == t1
        assert req.nonce == "aabbccddeeff0011"
        assert req.client_id == "127.0.0.1:5004"

    def test_nonce_min_length_validation(self):
        # Nonce shorter than 8 chars must raise ValidationError
        with pytest.raises(pydantic.ValidationError):
            NTPRequest(t1=100.0, nonce="short")

    def test_serialization_and_deserialization_roundtrip(self):
        req = NTPRequest.create(t1=1700000010.25, nonce="0123456789abcdef", client_id="client:5000")
        raw_bytes = req.to_bytes()
        assert isinstance(raw_bytes, bytes)

        restored = NTPRequest.from_bytes(raw_bytes)
        assert restored.t1 == req.t1
        assert restored.nonce == req.nonce
        assert restored.client_id == req.client_id

    def test_from_bytes_invalid_payload_raises(self):
        with pytest.raises((pydantic.ValidationError, ValueError, Exception)):
            NTPRequest.from_bytes(b"invalid json bytes")

        with pytest.raises(pydantic.ValidationError):
            NTPRequest.from_bytes(b'{"t1": 100.0}')  # Missing required nonce

    def test_adversarial_client_id_special_characters(self):
        # Client ID with unicode, symbols, and formatting characters
        req = NTPRequest.create(t1=100.0, nonce="0123456789abcdef", client_id="client-α-β:5001 [NODE]")
        assert req.client_id == "client-α-β:5001 [NODE]"
        restored = NTPRequest.from_bytes(req.to_bytes())
        assert restored.client_id == "client-α-β:5001 [NODE]"

    def test_timestamp_boundary_values(self):
        # Far future timestamp beyond 32-bit Unix overflow (year 2038+)
        req_future = NTPRequest.create(t1=3000000000.999)
        assert req_future.t1 == 3000000000.999
        restored = NTPRequest.from_bytes(req_future.to_bytes())
        assert restored.t1 == pytest.approx(3000000000.999)

        # Zero timestamp
        req_zero = NTPRequest.create(t1=0.0)
        assert req_zero.t1 == 0.0


# ==============================================================================
# NTPResponse Tests
# ==============================================================================

class TestNTPResponse:
    def test_valid_response_creation(self):
        resp = NTPResponse(
            t1=100.0,
            t2=100.02,
            t3=100.025,
            nonce="0123456789abcdef",
            server_stratum=1,
            server_id="127.0.0.1:5001",
            is_falseticker=False,
            skew_applied=0.0,
        )
        assert resp.t1 == 100.0
        assert resp.t2 == 100.02
        assert resp.t3 == 100.025
        assert resp.nonce == "0123456789abcdef"
        assert resp.server_stratum == 1
        assert resp.server_id == "127.0.0.1:5001"
        assert resp.is_falseticker is False
        assert resp.skew_applied == 0.0

    def test_stratum_bounds_validation(self):
        # Stratum must be in range 1-16
        valid_kwargs = dict(
            t1=1.0, t2=2.0, t3=3.0, nonce="12345678", server_id="srv:5001"
        )
        # Valid stratum values
        NTPResponse(server_stratum=1, **valid_kwargs)
        NTPResponse(server_stratum=2, **valid_kwargs)
        NTPResponse(server_stratum=16, **valid_kwargs)

        # Invalid stratum 0 (ge=1 violated)
        with pytest.raises(pydantic.ValidationError):
            NTPResponse(server_stratum=0, **valid_kwargs)

        # Invalid stratum 17 (le=16 violated)
        with pytest.raises(pydantic.ValidationError):
            NTPResponse(server_stratum=17, **valid_kwargs)

        # Invalid negative stratum
        with pytest.raises(pydantic.ValidationError):
            NTPResponse(server_stratum=-1, **valid_kwargs)

    def test_response_nonce_min_length(self):
        with pytest.raises(pydantic.ValidationError):
            NTPResponse(
                t1=1.0, t2=2.0, t3=3.0, nonce="1234", server_stratum=1
            )

    def test_response_serialization_roundtrip(self):
        resp = NTPResponse(
            t1=100.0,
            t2=100.05,
            t3=100.06,
            nonce="fedcba9876543210",
            server_stratum=2,
            server_id="127.0.0.1:5002",
            is_falseticker=True,
            skew_applied=60.0,
        )
        raw_bytes = resp.to_bytes()
        assert isinstance(raw_bytes, bytes)

        restored = NTPResponse.from_bytes(raw_bytes)
        assert restored.t1 == resp.t1
        assert restored.t2 == resp.t2
        assert restored.t3 == resp.t3
        assert restored.nonce == resp.nonce
        assert restored.server_stratum == resp.server_stratum
        assert restored.is_falseticker is True
        assert restored.skew_applied == 60.0

    def test_adversarial_skew_and_falseticker_values(self):
        # Extreme negative skew (e.g. chaos glitch -90s)
        resp = NTPResponse(
            t1=100.0,
            t2=10.0,
            t3=10.002,
            nonce="0123456789abcdef",
            server_stratum=2,
            is_falseticker=True,
            skew_applied=-90.0,
        )
        assert resp.skew_applied == -90.0
        assert resp.is_falseticker is True


# ==============================================================================
# PeerAddress Tests
# ==============================================================================

class TestPeerAddress:
    def test_parse_valid_ipv4_and_hostname(self):
        addr1 = PeerAddress.parse("127.0.0.1:5001")
        assert addr1.host == "127.0.0.1"
        assert addr1.port == 5001
        assert addr1.key == "127.0.0.1:5001"
        assert addr1.to_tuple() == ("127.0.0.1", 5001)

        addr2 = PeerAddress.parse("ntp.example.org:123")
        assert addr2.host == "ntp.example.org"
        assert addr2.port == 123
        assert addr2.key == "ntp.example.org:123"

    @pytest.mark.parametrize("invalid_addr", [
        "127.0.0.1",          # missing port
        "127.0.0.1:notaport", # non-integer port
        "127.0.0.1:0",        # port out of range (ge=1)
        "127.0.0.1:70000",    # port out of range (le=65535)
        ":5001",              # empty host
    ])
    def test_parse_invalid_address_raises(self, invalid_addr):
        with pytest.raises((ValueError, pydantic.ValidationError)):
            PeerAddress.parse(invalid_addr)


# ==============================================================================
# PeerSample Tests (Timing Mathematics)
# ==============================================================================

class TestPeerSample:
    def test_delay_and_theta_calculation_symmetric(self):
        # Peer has 0 clock offset, 20ms one-way delay (40ms RTT)
        # Client departure t1 = 100.000
        # Server arrival   t2 = 100.020
        # Server transmit  t3 = 100.025 (5ms server processing)
        # Client arrival   t4 = 100.045
        peer = PeerAddress(host="127.0.0.1", port=5001)
        sample = PeerSample(
            peer=peer,
            t1=100.000,
            t2=100.020,
            t3=100.025,
            t4=100.045,
            server_stratum=1,
            nonce_matched=True,
            status="RESPONDED",
        )
        # Delay delta = (t4 - t1) - (t3 - t2) = (0.045) - (0.005) = 0.040s (40ms)
        assert sample.delay == pytest.approx(0.040, abs=1e-9)

        # Theta = ((t2 - t1) + (t3 - t4)) / 2 = (0.020 + (-0.020)) / 2 = 0.000s
        assert sample.theta == pytest.approx(0.000, abs=1e-9)

        # Interval = [theta - delay/2, theta + delay/2] = [-0.020, +0.020]
        low, high = sample.interval
        assert low == pytest.approx(-0.020, abs=1e-9)
        assert high == pytest.approx(0.020, abs=1e-9)

    def test_delay_and_theta_calculation_with_offset(self):
        # Server clock is ahead by +2.0 seconds.
        # Transit time 10ms each way (20ms RTT).
        # t1 = 50.000
        # t2 = 52.010 (offset +2, transit +0.010)
        # t3 = 52.012 (processing +0.002)
        # t4 = 50.022 (transit +0.010)
        peer = PeerAddress(host="127.0.0.1", port=5001)
        sample = PeerSample(
            peer=peer,
            t1=50.000,
            t2=52.010,
            t3=52.012,
            t4=50.022,
            server_stratum=1,
        )
        # delta = (50.022 - 50.000) - (52.012 - 52.010) = 0.022 - 0.002 = 0.020s
        assert sample.delay == pytest.approx(0.020, abs=1e-9)

        # theta = ((52.010 - 50.000) + (52.012 - 50.022)) / 2 = (2.010 + 1.990) / 2 = 2.000s
        assert sample.theta == pytest.approx(2.000, abs=1e-9)

        # interval = [2.000 - 0.010, 2.000 + 0.010] = [1.990, 2.010]
        low, high = sample.interval
        assert low == pytest.approx(1.990, abs=1e-9)
        assert high == pytest.approx(2.010, abs=1e-9)

    def test_negative_delay_clamping_in_interval(self):
        # If timestamp anomaly produces negative delay, interval must clamp to [theta, theta]
        peer = PeerAddress(host="127.0.0.1", port=5001)
        sample = PeerSample(
            peer=peer,
            t1=100.0,
            t2=100.0,
            t3=100.1,  # server claims it took 100ms
            t4=100.05, # but client measured total 50ms (negative apparent delay)
        )
        assert sample.delay < 0.0
        low, high = sample.interval
        assert low == high == pytest.approx(sample.theta)


# ==============================================================================
# NodeConfig Validation Tests
# ==============================================================================

class TestNodeConfig:
    def test_stratum1_valid_without_upstreams(self):
        config = NodeConfig(stratum=1, port=5001)
        assert config.stratum == 1
        assert config.port == 5001
        assert config.upstreams == []
        assert config.sync_mode == "STEP"
        assert config.slew_rate == 0.40

    def test_stratum2_requires_upstream(self):
        # Stratum > 1 must have at least one upstream peer
        with pytest.raises(pydantic.ValidationError) as excinfo:
            NodeConfig(stratum=2, port=5002, upstreams=[])
        assert "upstream" in str(excinfo.value).lower()

    def test_stratum3_requires_upstream(self):
        with pytest.raises(pydantic.ValidationError) as excinfo:
            NodeConfig(stratum=3, port=5004, upstreams=[])
        assert "upstream" in str(excinfo.value).lower()

    def test_stratum2_with_upstream_is_valid(self):
        config = NodeConfig(
            stratum=2,
            port=5002,
            upstreams=[PeerAddress(host="127.0.0.1", port=5001)],
        )
        assert config.stratum == 2
        assert len(config.upstreams) == 1

    def test_stratum2_with_string_upstream_coercion(self):
        # Verify convenience string coercion in config
        config = NodeConfig(
            stratum=2,
            port=5002,
            upstreams=["127.0.0.1:5001"],
        )
        assert len(config.upstreams) == 1
        assert isinstance(config.upstreams[0], PeerAddress)
        assert config.upstreams[0].port == 5001

    def test_stratum_bounds(self):
        with pytest.raises(pydantic.ValidationError):
            NodeConfig(stratum=0, port=5000)
        with pytest.raises(pydantic.ValidationError):
            NodeConfig(stratum=4, port=5000)

    def test_sync_mode_validation(self):
        # Valid modes
        c1 = NodeConfig(stratum=1, sync_mode="STEP")
        c2 = NodeConfig(stratum=1, sync_mode="SLEW")
        assert c1.sync_mode == "STEP"
        assert c2.sync_mode == "SLEW"

        # Invalid mode
        with pytest.raises(pydantic.ValidationError):
            NodeConfig(stratum=1, sync_mode="INSTANT")

    def test_numerical_bounds_validation(self):
        # Interval must be > 0.1
        with pytest.raises(pydantic.ValidationError):
            NodeConfig(stratum=1, interval=0.05)

        # Slew rate must be > 0.0
        with pytest.raises(pydantic.ValidationError):
            NodeConfig(stratum=1, slew_rate=0.0)

        # Chaos probability must be within [0.0, 1.0]
        with pytest.raises(pydantic.ValidationError):
            NodeConfig(stratum=1, chaos_probability=-0.01)
        with pytest.raises(pydantic.ValidationError):
            NodeConfig(stratum=1, chaos_probability=1.01)

        # Socket timeout must be > 0.0
        with pytest.raises(pydantic.ValidationError):
            NodeConfig(stratum=1, socket_timeout=0.0)


# ==============================================================================
# Telemetry Models Tests
# ==============================================================================

class TestTelemetryModels:
    def test_server_log_entry_schema(self):
        entry = ServerLogEntry(
            time=1700000000.0,
            client="127.0.0.1:45678",
            t2=1700000000.010,
            t3=1700000000.015,
            is_fake=False,
            skew=0.0,
        )
        assert entry.client == "127.0.0.1:45678"
        assert entry.is_fake is False

    def test_sync_history_entry_schema(self):
        entry = SyncHistoryEntry(
            time=1700000000.0,
            peer="127.0.0.1:5001",
            delay_ms=12.5,
            theta_s=0.0345,
            mode="STEP",
            before=1700000000.0,
            after=1700000000.0345,
        )
        assert entry.peer == "127.0.0.1:5001"
        assert entry.mode == "STEP"
        assert entry.theta_s == 0.0345

    def test_telemetry_snapshot_schema(self):
        snap = TelemetrySnapshot(
            pause_time=1700000000.0,
            now_os=1700000000.0,
            now_virtual=1700000000.05,
            offset=0.05,
            drift_rate=0.001,
            slew_rem=0.0,
            slew_target=0.0,
            slew_ratio=1.0,
            peer_reports={},
            server_logs=[],
            sync_history=[],
            offset_samples=[0.05],
            tx_count=10,
            rx_count=8,
        )
        assert snap.offset == 0.05
        assert snap.tx_count == 10
        assert snap.rx_count == 8
