"""Clock module for NTP simulation.

Exports VirtualClock with Brownian quartz drift and step/slew dynamics.
"""

from ntp_simulation.clock.virtual_clock import VirtualClock

__all__ = ["VirtualClock"]
