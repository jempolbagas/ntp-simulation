"""Rich TUI interface components and terminal signal handling."""
from __future__ import annotations

from ntp_simulation.ui.dashboard import build_dashboard
from ntp_simulation.ui.key_reader import KeyReader
from ntp_simulation.ui.widgets import (
    generate_sparkline,
    render_offset_gauge,
    render_topology_diagram,
)

__all__ = [
    "KeyReader",
    "build_dashboard",
    "generate_sparkline",
    "render_offset_gauge",
    "render_topology_diagram",
]
