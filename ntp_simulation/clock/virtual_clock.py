"""Thread-safe virtual clock with Brownian quartz drift and step/slew dynamics.

Simulates quartz crystal frequency instability via a bounded random walk (Wiener process)
with first-order exponential smoothing, and provides both instantaneous step adjustments
and continuous rate-limited slewing (±0.40 s/s).
"""

from __future__ import annotations

import math
import random
import threading
import time
from datetime import datetime
from typing import Optional, Tuple


class VirtualClock:
    """Thread-safe virtual clock simulating quartz drift, step jumps, and slewing."""

    def __init__(
        self,
        initial_offset: float = 0.0,
        slew_rate: float = 0.40,
        max_drift_target: float = 0.025,
        drift_shift_interval: float = 1.2,
    ) -> None:
        """Initializes virtual clock with given initial offset and slew rate.

        Args:
            initial_offset: Starting offset in seconds relative to OS time.
            slew_rate: Slewing adjustment speed in seconds per second (default 0.40 s/s).
            max_drift_target: Maximum target drift magnitude in s/s (default 0.025 = ±25 ms/s).
            drift_shift_interval: Interval in seconds between target drift updates (default 1.2 s).
        """
        self._offset: float = float(initial_offset)
        self._slew_total_target: float = 0.0
        self._slew_remaining: float = 0.0
        self._slew_rate: float = float(slew_rate)

        # Brownian quartz drift simulation parameters
        self._drift_active: bool = False
        self._drift_rate: float = 0.0
        self._drift_target: float = 0.0
        self._max_drift_target: float = float(max_drift_target)
        self._drift_shift_interval: float = float(drift_shift_interval)
        self._last_drift_shift: float = time.time()

        self._last_tick: float = time.time()
        self._lock: threading.Lock = threading.Lock()

    @property
    def offset(self) -> float:
        """Current offset relative to OS clock."""
        return self.get_offset()

    @property
    def slew_rate(self) -> float:
        """Configured maximum slewing rate in s/s."""
        return self._slew_rate

    @property
    def drift_rate(self) -> float:
        """Instantaneous drift rate in s/s."""
        return self.get_drift_rate()

    @property
    def drift_active(self) -> bool:
        """Whether Brownian drift simulation is currently active."""
        return self.is_drift_active()

    def now(self) -> float:
        """Returns current virtual time (system time + offset) under lock."""
        with self._lock:
            return time.time() + self._offset

    def get_offset(self) -> float:
        """Returns current clock offset relative to OS clock in seconds."""
        with self._lock:
            return self._offset

    def step(self, delta: float) -> None:
        """Applies instantaneous offset jump, clearing any active slew.

        Args:
            delta: Time adjustment in seconds to apply immediately.
        """
        with self._lock:
            self._offset += float(delta)
            self._slew_remaining = 0.0
            self._slew_total_target = 0.0

    def start_slew(self, delta: float) -> None:
        """Queues continuous rate-limited slew adjustment.

        Args:
            delta: Total time adjustment in seconds to slew gradually.
        """
        with self._lock:
            self._slew_remaining += float(delta)
            self._slew_total_target = self._slew_remaining

    def tick(self, now_real: Optional[float] = None, dt: Optional[float] = None) -> None:
        """Advances clock: integrates Brownian drift and steps active slew.

        Args:
            now_real: Optional wall-clock timestamp or delta for deterministic testing.
            dt: Optional explicit elapsed time delta in seconds.
        """
        with self._lock:
            if dt is not None:
                elapsed = max(0.0, float(dt))
                now = self._last_tick + elapsed
            elif now_real is not None:
                now_val = float(now_real)
                # If caller passed a small relative dt positionally (< 10000.0)
                # while _last_tick is set to epoch time (> 10000.0)
                if now_val < 10000.0 and self._last_tick > 10000.0:
                    elapsed = max(0.0, now_val)
                    now = self._last_tick + elapsed
                else:
                    now = now_val
                    elapsed = max(0.0, now - self._last_tick)
            else:
                now = time.time()
                elapsed = max(0.0, now - self._last_tick)

            self._last_tick = now

            if elapsed <= 0.0:
                return

            # 1. Brownian quartz drift simulation
            if self._drift_active:
                if (now - self._last_drift_shift > self._drift_shift_interval) or (self._last_drift_shift > now):
                    self._drift_target += random.uniform(-0.006, 0.006)
                    self._drift_target = max(
                        -self._max_drift_target,
                        min(self._max_drift_target, self._drift_target),
                    )
                    self._last_drift_shift = now

                # Exponential relaxation toward target drift: dD/dt = kappa * (target - D)
                drift_step = (self._drift_target - self._drift_rate) * min(1.0, elapsed * 2.0)
                self._drift_rate += drift_step
                self._offset += self._drift_rate * elapsed

            # 2. Slewing rate-limited adjustment
            if self._slew_remaining != 0.0:
                max_step = self._slew_rate * elapsed
                if abs(self._slew_remaining) <= max_step:
                    self._offset += self._slew_remaining
                    self._slew_remaining = 0.0
                    self._slew_total_target = 0.0
                else:
                    step = math.copysign(max_step, self._slew_remaining)
                    self._offset += step
                    self._slew_remaining -= step

    def get_slew_info(self) -> Tuple[float, float, float]:
        """Returns slewing progress tuple: (remaining_slew, total_target_slew, progress_ratio).

        Progress ratio is 1.0 when no slew is pending, otherwise in [0.0, 1.0].
        """
        with self._lock:
            if self._slew_total_target == 0.0:
                return (0.0, 0.0, 1.0)
            completed = abs(self._slew_total_target) - abs(self._slew_remaining)
            ratio = max(0.0, min(1.0, completed / max(0.0001, abs(self._slew_total_target))))
            return (self._slew_remaining, self._slew_total_target, ratio)

    def get_drift_rate(self) -> float:
        """Returns instantaneous drift rate in s/s (0.0 if drift disabled)."""
        with self._lock:
            return self._drift_rate if self._drift_active else 0.0

    def is_drift_active(self) -> bool:
        """Returns whether Brownian drift simulation is currently active."""
        with self._lock:
            return self._drift_active

    def toggle_drift(self) -> bool:
        """Toggles dynamic Brownian drift simulation on/off.

        When enabled, seeds drift target in ±[0.005, 0.018] s/s.
        Returns the new active state.
        """
        with self._lock:
            self._drift_active = not self._drift_active
            if self._drift_active:
                self._drift_target = random.choice([-1.0, 1.0]) * random.uniform(0.005, 0.018)
                self._drift_rate = self._drift_target
                self._last_drift_shift = self._last_tick
            else:
                self._drift_rate = 0.0
                self._drift_target = 0.0
            return self._drift_active

    def set_drift(self, active: bool) -> None:
        """Explicitly sets drift active state."""
        with self._lock:
            if self._drift_active != active:
                self._drift_active = active
                if self._drift_active:
                    self._drift_target = random.choice([-1.0, 1.0]) * random.uniform(0.005, 0.018)
                    self._drift_rate = self._drift_target
                    self._last_drift_shift = self._last_tick
                else:
                    self._drift_rate = 0.0
                    self._drift_target = 0.0

    def reset(self, initial_offset: float = 0.0) -> None:
        """Resets virtual clock state to specified initial offset."""
        with self._lock:
            self._offset = float(initial_offset)
            self._slew_total_target = 0.0
            self._slew_remaining = 0.0
            self._drift_active = False
            self._drift_rate = 0.0
            self._drift_target = 0.0
            self._last_tick = time.time()
            self._last_drift_shift = self._last_tick

    @staticmethod
    def format_ts(ts: float) -> str:
        """Formats an epoch timestamp as '%H:%M:%S.%f' truncated to milliseconds."""
        try:
            return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        except (ValueError, OSError, OverflowError):
            return "00:00:00.000"

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"VirtualClock(offset={self._offset:+.6f}s, "
                f"slew_remaining={self._slew_remaining:+.6f}s, "
                f"drift_active={self._drift_active}, "
                f"drift_rate={self._drift_rate:+.6f}s/s)"
            )
