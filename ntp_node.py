import argparse
import json
import random
import socket
import sys
import threading
import time
from datetime import datetime


class VirtualClock:
    """Thread-safe virtual clock that simulates local time with an adjustable offset."""

    def __init__(self, initial_offset: float = 0.0):
        self._offset = initial_offset
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return time.time() + self._offset

    def adjust(self, delta: float) -> None:
        with self._lock:
            self._offset += delta

    def get_offset(self) -> float:
        with self._lock:
            return self._offset

    @staticmethod
    def format_ts(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def run_server(clock: VirtualClock, port: int, stratum: int, stop_event: threading.Event) -> None:
    """UDP NTP server listener handling incoming time sync requests."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(1.0)

    print(f"[SERVER] Listening on 0.0.0.0:{port} (Stratum {stratum})")

    try:
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break

            t2 = clock.now()
            try:
                payload = json.loads(data.decode("utf-8"))
                t1 = float(payload["t1"])
            except (ValueError, KeyError, UnicodeDecodeError):
                continue

            t3 = clock.now()
            response = json.dumps({
                "t1": t1,
                "t2": t2,
                "t3": t3,
                "server_stratum": stratum,
            }).encode("utf-8")

            sock.sendto(response, addr)
            print(
                f"[SERVER <- {addr[0]}:{addr[1]}] "
                f"Served T2={VirtualClock.format_ts(t2)}, T3={VirtualClock.format_ts(t3)}"
            )
    finally:
        sock.close()


def run_client(
    clock: VirtualClock,
    upstream_host: str,
    upstream_port: int,
    interval: float,
    stop_event: threading.Event,
) -> None:
    """UDP NTP client polling upstream server and applying offset correction."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)

    print(f"[CLIENT] Polling upstream {upstream_host}:{upstream_port} every {interval}s")

    try:
        while not stop_event.is_set():
            t1 = clock.now()
            request = json.dumps({"t1": t1}).encode("utf-8")

            try:
                sock.sendto(request, (upstream_host, upstream_port))
                data, _ = sock.recvfrom(1024)
                t4 = clock.now()

                resp = json.loads(data.decode("utf-8"))
                t2 = float(resp["t2"])
                t3 = float(resp["t3"])
                srv_stratum = resp.get("server_stratum", "?")

                delay = (t4 - t1) - (t3 - t2)
                theta = ((t2 - t1) + (t3 - t4)) / 2.0

                before_ts = clock.now()
                clock.adjust(theta)
                after_ts = clock.now()

                print("-" * 65)
                print(
                    f"[SYNC -> Stratum {srv_stratum} ({upstream_host}:{upstream_port})]\n"
                    f"  T1 (Client Send) : {VirtualClock.format_ts(t1)}\n"
                    f"  T2 (Server Recv) : {VirtualClock.format_ts(t2)}\n"
                    f"  T3 (Server Send) : {VirtualClock.format_ts(t3)}\n"
                    f"  T4 (Client Recv) : {VirtualClock.format_ts(t4)}\n"
                    f"  Delay (delta)    : {delay * 1000:+.3f} ms\n"
                    f"  Offset (theta)   : {theta:+.6f} s ({theta * 1000:+.3f} ms)\n"
                    f"  Virtual Clock    : {VirtualClock.format_ts(before_ts)} -> {VirtualClock.format_ts(after_ts)}\n"
                    f"  Remaining Offset : {clock.get_offset():+.6f} s"
                )
            except socket.timeout:
                print(f"[CLIENT] Timeout waiting for {upstream_host}:{upstream_port}")
            except (OSError, ValueError, KeyError) as exc:
                print(f"[CLIENT] Sync error: {exc}")

            stop_event.wait(interval)
    finally:
        sock.close()


def parse_upstream(value: str) -> tuple[str, int]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("Upstream must be in HOST:PORT format (e.g. 192.168.1.10:5000)")
    host, port_str = value.rsplit(":", 1)
    try:
        port = int(port_str)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Upstream port must be an integer") from exc
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError("Upstream port must be between 1 and 65535")
    return host, port


def main() -> None:
    parser = argparse.ArgumentParser(description="NTP Stratum Simulation Node")
    parser.add_argument("--stratum", type=int, required=True, choices=[1, 2, 3], help="Node stratum level (1, 2, or 3)")
    parser.add_argument("--port", type=int, default=5000, help="UDP port to listen on (for Stratum 1 & 2, default: 5000)")
    parser.add_argument("--upstream", type=parse_upstream, help="Upstream server HOST:PORT (required for Stratum 2 & 3)")
    parser.add_argument("--interval", type=float, default=5.0, help="Sync interval in seconds (default: 5.0)")
    args = parser.parse_args()

    if args.stratum > 1 and args.upstream is None:
        parser.error(f"--upstream is required when --stratum is {args.stratum}")

    if args.stratum == 1:
        initial_offset = 0.0
    else:
        # Random initial offset between -30s and -5s or +5s and +30s
        initial_offset = random.choice([-1, 1]) * random.uniform(5.0, 30.0)

    clock = VirtualClock(initial_offset=initial_offset)
    stop_event = threading.Event()
    threads: list[threading.Thread] = []

    print("=" * 65)
    print(f"NTP Node Started | Stratum: {args.stratum} | Initial Offset: {initial_offset:+.3f}s")
    print(f"OS Time: {VirtualClock.format_ts(time.time())} | Virtual Time: {VirtualClock.format_ts(clock.now())}")
    print("=" * 65)

    # Stratum 1 & 2 act as servers for lower strata
    if args.stratum in (1, 2):
        srv_thread = threading.Thread(
            target=run_server,
            args=(clock, args.port, args.stratum, stop_event),
            daemon=True,
        )
        srv_thread.start()
        threads.append(srv_thread)

    # Stratum 2 & 3 act as clients syncing to an upstream server
    if args.stratum in (2, 3) and args.upstream is not None:
        up_host, up_port = args.upstream
        cli_thread = threading.Thread(
            target=run_client,
            args=(clock, up_host, up_port, args.interval, stop_event),
            daemon=True,
        )
        cli_thread.start()
        threads.append(cli_thread)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[SYSTEM] Shutting down node...")
        stop_event.set()
        for t in threads:
            t.join(timeout=2.0)
        sys.exit(0)


if __name__ == "__main__":
    main()
