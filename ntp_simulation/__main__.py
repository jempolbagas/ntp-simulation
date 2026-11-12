"""Entrypoint module for python3 -m ntp_simulation execution."""
from __future__ import annotations

import sys
from ntp_simulation.cli import main

if __name__ == "__main__":
    main(sys.argv[1:])
