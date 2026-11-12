"""Unicode sparklines, offset gauge meter, and topology diagrams."""
from __future__ import annotations

from typing import Sequence
from rich.text import Text


def generate_sparkline(values: Sequence[float], width: int = 16) -> str:
    """Renders a Unicode sparkline graph showing offset convergence trend.

    Args:
        values: Sequence of numerical offset measurements.
        width: Maximum number of character columns in the output string.

    Returns:
        Fixed-width string of Unicode block characters depicting trend history.
    """
    if width <= 0:
        return ""
    if not values:
        return " " * width

    bars = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    trimmed = list(values)[-width:]
    abs_vals = [abs(float(v)) for v in trimmed]
    max_val = max(abs_vals) if abs_vals and max(abs_vals) > 0.0 else 1.0

    result = []
    for v in trimmed:
        ratio = min(1.0, abs(float(v)) / max_val)
        idx = int(ratio * (len(bars) - 1))
        result.append(bars[idx])

    padding = " " * (width - len(result))
    return padding + "".join(result)


def render_offset_gauge(offset: float, width: int = 36) -> Text:
    """Renders calibrated deviation gauge meter with needle indicator and status badge.

    Args:
        offset: Clock offset delta in seconds.
        width: Gauge bar width in characters.

    Returns:
        Rich Text instance displaying needle position and color-coded status badge.
    """
    abs_val = abs(float(offset))
    gauge = Text()

    if abs_val > 1.0:
        max_bound = 30.0
        label_l, label_r = "-30s", "+30s"
    elif abs_val > 0.05:
        max_bound = 1.0
        label_l, label_r = "-1.0s", "+1.0s"
    else:
        max_bound = 0.05
        label_l, label_r = "-50ms", "+50ms"

    half_w = width // 2
    clamped_offset = max(-max_bound, min(max_bound, float(offset)))
    pos = int((clamped_offset / max_bound) * half_w)

    bar_chars = []
    for i in range(-half_w, half_w + 1):
        if i == pos:
            bar_chars.append("█")
        elif i == 0:
            bar_chars.append("│")
        else:
            bar_chars.append("─")

    gauge_str = "".join(bar_chars)

    if abs_val < 0.005:
        status_color = "bold green"
        desc = "LOCKED (< 5ms)"
    elif abs_val < 0.05:
        status_color = "green"
        desc = "ACCURATE (< 50ms)"
    elif abs_val < 1.0:
        status_color = "yellow"
        desc = "MODERATE DRIFT"
    else:
        status_color = "bold red"
        desc = "HIGH SKEW"

    gauge.append(f"{label_l:>6} [")
    gauge.append(gauge_str, style=status_color)
    gauge.append(f"] {label_r:<6} ")
    gauge.append(f"[{desc}]", style=status_color)
    return gauge


def render_topology_diagram(stratum: int) -> Text:
    """Renders visual ASCII topology hierarchy diagram highlighting the current node.

    Args:
        stratum: Stratum tier of this node (1, 2, or 3).

    Returns:
        Rich Text instance with styled topology hierarchy representation.
    """
    diag = Text()
    if stratum == 1:
        diag.append("[STRATUM 1: MASTER]", style="bold black on bright_yellow")
        diag.append(" --> [Stratum 2 Relays] --> [Stratum 3 Clients]", style="dim")
    elif stratum == 2:
        diag.append("[Stratum 1 Master] --> ", style="dim")
        diag.append("[STRATUM 2: THIS RELAY]", style="bold white on dodger_blue1")
        diag.append(" --> [Stratum 3 Clients]", style="dim")
    else:
        diag.append("[Stratum 1 Master] --> [Stratum 2 Relays] --> ", style="dim")
        diag.append("[STRATUM 3: THIS CLIENT]", style="bold white on dark_green")
    return diag


def render_line_chart(
    values: Sequence[float],
    width: int = 34,
    height: int = 7,
) -> Text:
    """Renders a multi-line ASCII/Unicode terminal line chart of offset convergence history.

    Features:
    - Symmetrical Y-axis bounds around 0.0 with dynamic unit auto-scaling (ms vs s).
    - Centerline indicator (0ms target locked axis).
    - Connected curve points with color-coding based on proximity to 0.0s.
    - X-axis timeline footer with time-direction indicators.

    Args:
        values: Sequence of historical offset measurements.
        width: Chart plot width in columns.
        height: Total row count of the chart (default 7 rows).

    Returns:
        Rich Text instance containing the styled multi-line terminal line chart.
    """
    import math

    valid_vals = [
        float(v)
        for v in values
        if v is not None and not (math.isnan(float(v)) or math.isinf(float(v)))
    ]
    if not valid_vals:
        valid_vals = [0.0]

    limit = max(max(abs(v) for v in valid_vals), 0.005)
    if limit >= 1.0:
        top_lbl = f"{limit:+6.1f}s"
        mid_lbl = "   0.0s"
        bot_lbl = f"{-limit:+6.1f}s"
    elif limit >= 0.05:
        top_lbl = f"{limit * 1000:+6.0f}ms"
        mid_lbl = "    0ms"
        bot_lbl = f"{-limit * 1000:+6.0f}ms"
    else:
        top_lbl = f"{limit * 1000:+6.1f}ms"
        mid_lbl = "    0ms"
        bot_lbl = f"{-limit * 1000:+6.1f}ms"

    plot_vals = [None] * max(0, width - len(valid_vals)) + valid_vals[-width:]
    plot_vals = plot_vals[-width:]

    mid_row = height // 2
    col_rows: List[Optional[int]] = []
    for v in plot_vals:
        if v is None:
            col_rows.append(None)
        else:
            norm = max(-1.0, min(1.0, float(v) / limit))
            r = int(round(mid_row - norm * mid_row))
            col_rows.append(max(0, min(height - 1, r)))

    grid = [[" " for _ in range(width)] for _ in range(height)]
    styles = [["" for _ in range(width)] for _ in range(height)]

    for c in range(width):
        grid[mid_row][c] = "┄"
        styles[mid_row][c] = "dim cyan"

    prev_r: Optional[int] = None
    for c in range(width):
        curr_r = col_rows[c]
        if curr_r is not None:
            v_val = plot_vals[c]
            abs_v = abs(float(v_val)) if v_val is not None else 0.0
            if abs_v < 0.005:
                pt_style = "bold bright_green"
                char = "█" if curr_r == mid_row else "●"
            elif abs_v < 0.05:
                pt_style = "bold green"
                char = "●"
            elif abs_v < 1.0:
                pt_style = "bold yellow"
                char = "●"
            else:
                pt_style = "bold red"
                char = "▲" if v_val > 0 else "▼"

            if prev_r is not None and prev_r != curr_r:
                step = 1 if curr_r > prev_r else -1
                for r in range(prev_r + step, curr_r, step):
                    grid[r][c] = "│"
                    styles[r][c] = "cyan" if abs_v < 0.05 else ("yellow" if abs_v < 1.0 else "red")

            grid[curr_r][c] = char
            styles[curr_r][c] = pt_style
            prev_r = curr_r

    result = Text()
    for r in range(height):
        if r == 0:
            result.append(f"{top_lbl} ┤", style="cyan")
        elif r == mid_row:
            result.append(f"{mid_lbl} ┼", style="bold cyan")
        elif r == height - 1:
            result.append(f"{bot_lbl} ┤", style="cyan")
        else:
            result.append("        │", style="dim cyan")

        for c in range(width):
            ch = grid[r][c]
            st = styles[r][c]
            result.append(ch, style=st if st else None)
        result.append("\n")

    result.append("        └" + "─" * width + "\n", style="dim cyan")
    latest_val = valid_vals[-1]
    latest_str = (
        f"Now: {latest_val * 1000:+.2f}ms"
        if abs(latest_val) < 1.0
        else f"Now: {latest_val:+.4f}s"
    )
    pad_len = max(1, width - len(latest_str) - 8)
    result.append("        -history" + " " * pad_len, style="dim")
    status_color = (
        "bold bright_green"
        if abs(latest_val) < 0.005
        else ("green" if abs(latest_val) < 0.05 else ("yellow" if abs(latest_val) < 1.0 else "bold red"))
    )
    result.append(f"[{latest_str}]", style=status_color)
    return result
