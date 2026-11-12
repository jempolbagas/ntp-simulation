"""Unit tests for VirtualClock engine.

Validates offset time calculation, step dynamics (instant jump and slew clearing),
slew dynamics (rate-limited continuous adjustment at 0.40 s/s and progress tracking),
Brownian quartz drift random walk bounds and exponential relaxation,
concurrent thread safety, and timestamp formatting.
"""

import threading
import time
import pytest

pytest.importorskip("ntp_simulation.clock.virtual_clock")

from ntp_simulation.clock.virtual_clock import VirtualClock


class TestVirtualClockBasics:
    def test_initial_offset_and_now(self):
        clock = VirtualClock(initial_offset=5.0)
        assert clock.get_offset() == pytest.approx(5.0)
        t_sys = time.time()
        t_virt = clock.now()
        # Virtual time must be approximately system time + 5.0
        assert t_virt == pytest.approx(t_sys + 5.0, abs=0.2)

    def test_format_ts_static_method(self):
        # 1700000000.123 -> HH:MM:SS.mmm format
        formatted = VirtualClock.format_ts(1700000000.123)
        assert isinstance(formatted, str)
        assert len(formatted) == 12  # "HH:MM:SS.mmm"
        assert "." in formatted
        assert formatted.count(":") == 2


class TestClockStepping:
    def test_step_applies_instantaneous_jump(self):
        clock = VirtualClock(initial_offset=1.0)
        clock.step(3.5)
        assert clock.get_offset() == pytest.approx(4.5)
        clock.step(-2.0)
        assert clock.get_offset() == pytest.approx(2.5)

    def test_step_clears_active_slew(self):
        clock = VirtualClock(initial_offset=0.0)
        clock.start_slew(10.0)
        rem, target, ratio = clock.get_slew_info()
        assert target == pytest.approx(10.0)

        # Applying a step must clear any ongoing slew
        clock.step(1.0)
        rem_after, target_after, ratio_after = clock.get_slew_info()
        assert rem_after == 0.0
        assert target_after == 0.0
        assert ratio_after == 1.0


class TestClockSlewing:
    def test_slew_progresses_gradually_at_rate_limit(self):
        # Default slew_rate = 0.40 s/s
        clock = VirtualClock(initial_offset=0.0, slew_rate=0.40)
        clock.start_slew(1.0)

        now = 1000.0
        clock._last_tick = now

        # Tick 1: elapsed dt = 1.0s -> max step = 0.40s
        clock.tick(now_real=now + 1.0)
        assert clock.get_offset() == pytest.approx(0.40)
        rem, target, ratio = clock.get_slew_info()
        assert rem == pytest.approx(0.60)
        assert target == pytest.approx(1.00)
        assert ratio == pytest.approx(0.40)

        # Tick 2: elapsed dt = 1.0s -> max step = 0.40s (total offset = 0.80s)
        clock.tick(now_real=now + 2.0)
        assert clock.get_offset() == pytest.approx(0.80)
        rem, target, ratio = clock.get_slew_info()
        assert rem == pytest.approx(0.20)
        assert target == pytest.approx(1.00)
        assert ratio == pytest.approx(0.80)

        # Tick 3: elapsed dt = 1.0s -> remaining 0.20s completes
        clock.tick(now_real=now + 3.0)
        assert clock.get_offset() == pytest.approx(1.00)
        rem, target, ratio = clock.get_slew_info()
        assert rem == 0.0
        assert target == 0.0
        assert ratio == 1.0

    def test_negative_slew_convergence(self):
        clock = VirtualClock(initial_offset=5.0, slew_rate=0.40)
        clock.start_slew(-0.60)

        now = 1000.0
        clock._last_tick = now

        # Step by 1.0s -> -0.40 adjustment
        clock.tick(now_real=now + 1.0)
        assert clock.get_offset() == pytest.approx(4.60)

        # Step by 1.0s -> remaining -0.20 adjustment
        clock.tick(now_real=now + 2.0)
        assert clock.get_offset() == pytest.approx(4.40)
        rem, target, ratio = clock.get_slew_info()
        assert rem == 0.0

    def test_slew_idle_progress_ratio_is_one(self):
        clock = VirtualClock()
        rem, target, ratio = clock.get_slew_info()
        assert rem == 0.0
        assert target == 0.0
        assert ratio == 1.0


class TestBrownianDrift:
    def test_drift_activation_and_deactivation(self):
        clock = VirtualClock()
        assert not clock.is_drift_active()
        assert clock.get_drift_rate() == 0.0

        active = clock.toggle_drift()
        assert active is True
        assert clock.is_drift_active()
        assert abs(clock.get_drift_rate()) > 0.0

        active_again = clock.toggle_drift()
        assert active_again is False
        assert not clock.is_drift_active()
        assert clock.get_drift_rate() == 0.0

    def test_brownian_drift_target_clamped_within_bounds(self):
        # max_drift_target = 0.025 s/s (25 ms/s)
        clock = VirtualClock(max_drift_target=0.025, drift_shift_interval=0.05)
        clock.toggle_drift()

        now = 1000.0
        clock._last_tick = now
        clock._last_drift_shift = now

        # Simulate 100 ticks (10 seconds total)
        for i in range(1, 101):
            clock.tick(now_real=now + i * 0.1)
            # Drift target must always respect max bounds [-0.025, +0.025]
            assert -0.025001 <= clock._drift_target <= 0.025001
            # Current rate should remain bounded
            assert -0.030 <= clock.get_drift_rate() <= 0.030


class TestClockThreadSafety:
    def test_concurrent_access_does_not_deadlock_or_corrupt(self):
        clock = VirtualClock(initial_offset=0.0, slew_rate=1.0)
        clock.toggle_drift()
        stop_event = threading.Event()
        errors = []

        def reader():
            while not stop_event.is_set():
                try:
                    clock.now()
                    clock.get_offset()
                    clock.get_slew_info()
                except Exception as e:
                    errors.append(e)

        def modifier():
            while not stop_event.is_set():
                try:
                    clock.step(0.001)
                    clock.start_slew(0.01)
                    clock.tick()
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=reader) for _ in range(4)
        ] + [
            threading.Thread(target=modifier) for _ in range(4)
        ]

        for t in threads:
            t.start()

        time.sleep(0.3)
        stop_event.set()

        for t in threads:
            t.join()

        assert len(errors) == 0
