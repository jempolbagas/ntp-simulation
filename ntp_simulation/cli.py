"""Command line interface parsing and node startup for NTP simulation."""
from __future__ import annotations

import argparse
import random
import signal
import socket
import sys
import threading
import time
from typing import Any, List, Optional

from pydantic import ValidationError
from rich.console import Console
from rich.live import Live

from ntp_simulation.algorithms.marzullo import marzullo_evaluate
from ntp_simulation.clock.virtual_clock import VirtualClock
from ntp_simulation.models.config import NodeConfig
from ntp_simulation.models.peer import PeerAddress
from ntp_simulation.network.client import poll_peers_concurrently
from ntp_simulation.network.server import run_server
from ntp_simulation.state import SharedState
from ntp_simulation.ui.dashboard import build_dashboard
from ntp_simulation.ui.key_reader import KeyReader


def parse_upstream(value: str) -> PeerAddress:
    """Parses a 'HOST:PORT' string into a PeerAddress instance.

    Args:
        value: String formatted as HOST:PORT (e.g. '127.0.0.1:5001').

    Returns:
        PeerAddress instance.

    Raises:
        argparse.ArgumentTypeError: If value is not formatted properly.
    """
    try:
        return PeerAddress.parse(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid upstream address '{value}': expected format HOST:PORT (e.g. 127.0.0.1:5001)"
        ) from exc


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parses and validates command line arguments for the NTP node.

    Args:
        argv: Optional list of argument strings (defaults to sys.argv[1:]).

    Returns:
        argparse.Namespace with validated CLI arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m ntp_simulation",
        description="NTP Stratum Simulation Node (Engineering TUI & Marzullo Algorithm)",
    )
    parser.add_argument(
        "--stratum",
        type=int,
        required=True,
        choices=[1, 2, 3],
        help="Node stratum level (1=Master, 2=Relay, 3=Client)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="UDP port to listen on (default: 5000)",
    )
    parser.add_argument(
        "--upstream",
        type=parse_upstream,
        nargs="+",
        action="extend",
        default=[],
        help="Upstream server addresses in HOST:PORT format (e.g. 127.0.0.1:5001)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=4.0,
        help="Polling synchronization interval in seconds (default: 4.0)",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable interactive Rich TUI and output logs to stdout",
    )
    parser.add_argument(
        "--sync-mode",
        choices=["STEP", "SLEW"],
        default="STEP",
        help="Initial synchronization algorithm mode (default: STEP)",
    )
    parser.add_argument(
        "--drift",
        action="store_true",
        help="Start node with dynamic Brownian hardware drift enabled",
    )
    parser.add_argument(
        "--falseticker",
        action="store_true",
        help="Start node in manual falseticker fault mode (+60s skew)",
    )
    parser.add_argument(
        "--chaos",
        action="store_true",
        help="Start node in auto chaos fault mode (35%% sporadic glitches)",
    )
    parser.add_argument(
        "--bind-host",
        type=str,
        default="0.0.0.0",
        help="Interface IP address to bind UDP server on (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--dispersion-floor",
        type=float,
        default=0.050,
        help="Minimum dispersion / uncertainty floor in seconds (default: 0.050s = 50ms)",
    )
    parser.add_argument(
        "--initial-offset",
        type=float,
        default=None,
        help="Initial clock offset in seconds (default: random for Stratum > 1, 0.0 for Stratum 1)",
    )


    args = parser.parse_args(argv)
    if args.stratum > 1 and not args.upstream:
        parser.error(f"--upstream is required when --stratum is {args.stratum}")
    return args


def client_worker(
    clock: VirtualClock,
    state: SharedState,
    interval: float,
    stop_event: threading.Event,
) -> None:
    """Client worker loop performing concurrent polling, Marzullo evaluation, and clock sync.

    Args:
        clock: Active VirtualClock to synchronize.
        state: Shared telemetry state.
        interval: Polling periodicity in seconds.
        stop_event: Shutdown coordination event.
    """
    while not stop_event.is_set():
        with state.lock:
            upstreams = list(state.upstreams)
            mode = state.sync_mode

        if upstreams:
            # 1. Concurrent UDP polling across all upstreams with nonce correlation
            samples_dict = poll_peers_concurrently(upstreams, clock, timeout=1.5)
            with state.lock:
                state.tx_count += len(upstreams)
                state.rx_count += sum(
                    1 for s in samples_dict.values() if getattr(s, "status", None) == "RESPONDED"
                )

            # 2. Execute Authentic Marzullo Algorithm with dispersion floor
            evaluations, chosen_peer = marzullo_evaluate(
                list(samples_dict.values()),
                dispersion_floor=getattr(state, "dispersion_floor", 0.050),
            )
            state.update_peer_evaluations(evaluations, chosen_peer)

            # 3. Apply clock adjustment if a Truechimer was selected
            if chosen_peer and chosen_peer in evaluations:
                target_eval = evaluations[chosen_peer]
                theta_val = target_eval.theta
                before_ts = clock.now()

                if mode == "STEP":
                    clock.step(theta_val)
                else:
                    clock.start_slew(theta_val)

                after_ts = clock.now()
                state.record_sync(
                    peer_name=chosen_peer,
                    delay=target_eval.delay,
                    theta=theta_val,
                    mode=mode,
                    before_ts=before_ts,
                    after_ts=after_ts,
                )

        stop_event.wait(interval)


def key_listener_worker(
    key_reader: Optional[KeyReader],
    state: SharedState,
    clock: VirtualClock,
    stop_event: threading.Event,
) -> None:
    """Listens for keyboard input to handle interactive runtime toggles.

    Hotkeys:
        [SPACE]: Freeze/resume UI telemetry rendering.
        [F]: Toggle manual falseticker fault mode (+60s skew).
        [C]: Toggle random chaos glitch mode.
        [M]: Toggle sync mode (STEP <-> SLEW).
        [D]: Toggle Brownian quartz crystal drift.
        [Q]: Graceful shutdown.
    """
    if key_reader is None:
        return

    while not stop_event.is_set():
        ch = key_reader.read_key(timeout=0.1)
        if not ch:
            continue

        if ch == " ":
            state.toggle_ui_pause(clock)
        else:
            key = ch.lower()
            if key == "f":
                state.toggle_falseticker()
            elif key == "c":
                state.toggle_chaos()
            elif key == "m":
                state.toggle_sync_mode()
            elif key == "d":
                active = clock.toggle_drift()
                with state.lock:
                    state.last_action_banner = (
                        "Dynamic Brownian Drift Enabled" if active else "Drift Disabled"
                    )
            elif key == "q":
                stop_event.set()
                break


def main(argv: Optional[List[str]] = None) -> None:
    """Package entrypoint coordinating background threads and TUI rendering.

    Args:
        argv: Command-line arguments. If None, sys.argv[1:] is used.
    """
    args = parse_args(argv)
    upstreams = args.upstream if args.upstream else []

    try:
        config = NodeConfig(
            stratum=args.stratum,
            port=args.port,
            bind_host=getattr(args, "bind_host", "0.0.0.0"),
            upstreams=upstreams,
            interval=args.interval,
            no_ui=args.no_ui,
            sync_mode=args.sync_mode,
            dispersion_floor=getattr(args, "dispersion_floor", 0.050),
        )
    except ValidationError as exc:
        print(f"Error: Invalid configuration:\n{exc}", file=sys.stderr)
        sys.exit(2)

    # Early socket bind check for server nodes (Stratum 1 & 2)
    if config.stratum in (1, 2):
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            test_sock.bind((config.bind_host, config.port))
            test_sock.close()
        except OSError as exc:
            print(
                f"Error: Unable to bind UDP port {config.port} on {config.bind_host}: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

    if getattr(args, "initial_offset", None) is not None:
        initial_offset = float(args.initial_offset)
    else:
        initial_offset = (
            0.0 if args.stratum == 1 else random.choice([-1.0, 1.0]) * random.uniform(5.0, 30.0)
        )
    clock = VirtualClock(initial_offset=initial_offset)
    if args.drift:
        clock.set_drift(True)

    state = SharedState(config)
    if args.falseticker:
        state.toggle_falseticker()
    if args.chaos:
        state.toggle_chaos()

    stop_event = threading.Event()
    threads: List[threading.Thread] = []

    # 1. Main-thread KeyReader instantiation (before any threads are created)
    key_reader: Optional[KeyReader] = None
    if not config.no_ui:
        try:
            key_reader = KeyReader()
        except Exception:
            key_reader = None

    # 2. Main-thread signal handlers for graceful shutdown (SIGINT & SIGTERM)
    def _handle_shutdown_signal(signum: int, frame: Any) -> None:
        stop_event.set()
        raise KeyboardInterrupt()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_shutdown_signal)
        except (ValueError, OSError):
            pass

    # 3. Clock tick worker (slewing & Brownian drift integration at 25 Hz)
    def clock_ticker() -> None:
        while not stop_event.is_set():
            clock.tick()
            time.sleep(0.04)

    ticker_thread = threading.Thread(target=clock_ticker, name="clock_ticker", daemon=True)
    ticker_thread.start()
    threads.append(ticker_thread)

    # 4. Key listener worker (if interactive / not headless)
    if not config.no_ui and key_reader is not None:
        key_thread = threading.Thread(
            target=key_listener_worker,
            args=(key_reader, state, clock, stop_event),
            name="key_listener",
            daemon=True,
        )
        key_thread.start()
        threads.append(key_thread)

    # 5. Server listener worker (Stratum 1 & Stratum 2)
    if config.stratum in (1, 2):
        srv_thread = threading.Thread(
            target=run_server,
            args=(clock, state, stop_event, config.bind_host),
            name="udp_server",
            daemon=True,
        )
        srv_thread.start()
        threads.append(srv_thread)

    # 6. Client sync worker (Stratum 2 & Stratum 3 with configured upstreams)
    if config.stratum in (2, 3) and config.upstreams:
        cli_thread = threading.Thread(
            target=client_worker,
            args=(clock, state, config.interval, stop_event),
            name="udp_client",
            daemon=True,
        )
        cli_thread.start()
        threads.append(cli_thread)

    # 7. Main loop: Rich Live TUI or Headless console
    try:
        if not config.no_ui:
            console = Console()
            with Live(
                build_dashboard(clock, state),
                console=console,
                refresh_per_second=10,
                screen=True,
            ) as live:
                while not stop_event.is_set():
                    live.update(build_dashboard(clock, state))
                    time.sleep(0.1)
        else:
            print(
                f"=== NTP Node Running (Stratum {config.stratum}) on {state.local_ip}:{config.port} ===",
                flush=True,
            )
            while not stop_event.is_set():
                now_os = time.time()
                now_virt = clock.now()
                offset = clock.get_offset()
                with state.lock:
                    peer_count = len(state.peer_reports)
                    sync_count = state.sync_count
                    mode = state.sync_mode
                print(
                    f"[{VirtualClock.format_ts(now_os)}] Stratum {config.stratum} | "
                    f"Virtual: {VirtualClock.format_ts(now_virt)} | "
                    f"Offset: {offset:+.6f}s | Mode: {mode} | "
                    f"Peers: {peer_count} | Syncs: {sync_count}",
                    flush=True,
                )
                stop_event.wait(config.interval)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if key_reader is not None:
            key_reader.restore()
        for t in threads:
            t.join(timeout=0.5)


if __name__ == "__main__":
    main()
