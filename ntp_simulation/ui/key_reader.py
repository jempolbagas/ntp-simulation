"""Signal-safe non-blocking KeyReader with atexit and signal restoration."""
from __future__ import annotations

import atexit
import os
import signal
import sys
import time
from typing import Any, Dict, Optional


class KeyReader:
    """Reads single keypresses non-blockingly while guaranteeing terminal restoration.

    Configures stdin in cbreak mode on POSIX terminals so that characters are delivered
    immediately without waiting for the Enter key. Registers atexit cleanup and signal
    hooks for SIGINT and SIGTERM to ensure the terminal is always returned to canonical
    mode upon exit, interrupt, or error.
    """

    def __init__(self) -> None:
        self.is_windows: bool = os.name == "nt"
        self.old_settings: Optional[Any] = None
        self._restored: bool = False
        self._orig_handlers: Dict[int, Any] = {}

        if not self.is_windows:
            try:
                is_atty = bool(sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())
            except Exception:
                is_atty = False

            if is_atty:
                try:
                    import termios
                    import tty

                    fd = sys.stdin.fileno()
                    self.old_settings = termios.tcgetattr(fd)
                    tty.setcbreak(fd)

                    # Register immediate cleanup on normal exit
                    atexit.register(self.restore)

                    # Register signal hooks for abrupt termination
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        try:
                            orig = signal.signal(sig, self._handle_signal)
                            self._orig_handlers[sig] = orig
                        except (ValueError, OSError):
                            pass
                except Exception:
                    self.old_settings = None

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Signal handler ensuring terminal restoration before exit or handler propagation."""
        orig = self._orig_handlers.get(signum)
        self.restore()
        if callable(orig) and orig not in (self._handle_signal, signal.SIG_IGN, signal.SIG_DFL):
            orig(signum, frame)
        elif orig == signal.SIG_DFL:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        else:
            sys.exit(128 + signum)

    def read_key(self, timeout: float = 0.1) -> Optional[str]:
        """Reads a single keypress without blocking indefinitely.

        Args:
            timeout: Maximum seconds to wait for input (default 0.1s).

        Returns:
            Single character pressed, or None if timeout expired or non-interactive.
        """
        if self.is_windows:
            try:
                import msvcrt

                start = time.time()
                while time.time() - start < timeout:
                    if msvcrt.kbhit():
                        ch = msvcrt.getch()
                        try:
                            return ch.decode("utf-8")
                        except UnicodeDecodeError:
                            return None
                    time.sleep(0.02)
                return None
            except Exception:
                time.sleep(timeout)
                return None
        else:
            try:
                is_atty = bool(sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())
            except Exception:
                is_atty = False

            if not is_atty or self.old_settings is None:
                time.sleep(timeout)
                return None

            try:
                import select

                rlist, _, _ = select.select([sys.stdin], [], [], timeout)
                if rlist:
                    return sys.stdin.read(1)
                return None
            except Exception:
                return None

    def restore(self) -> None:
        """Restores normal canonical terminal settings and signal handlers."""
        if not self._restored:
            try:
                if self.old_settings is not None and not self.is_windows:
                    try:
                        is_atty = bool(sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())
                    except Exception:
                        is_atty = False

                    if is_atty:
                        import termios

                        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass
            finally:
                self._restored = True
                for sig, orig in list(self._orig_handlers.items()):
                    try:
                        signal.signal(sig, orig)
                    except (ValueError, OSError):
                        pass
                self._orig_handlers.clear()
                try:
                    atexit.unregister(self.restore)
                except Exception:
                    pass

    def __enter__(self) -> KeyReader:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.restore()
