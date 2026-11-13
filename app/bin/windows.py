#!/usr/bin/env python3
"""The computer's own screen kept tidy: the waterfall on the left, and what opens beside it (see station/layout.py).

  windows.py keep    keeps it so (station-windows.service runs this)
  windows.py ft8     the waterfall's FT8 button, with WSJT-X open: brings it to the front or puts it away; prints
                     {"action": "front" | "away" | "open"} ("open": no WSJT-X window to be found)
  windows.py show    lists the screens and windows, and what it makes of each (for checking)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from station import layout  # noqa: E402

if __name__ == "__main__":
    sys.exit(layout.main(sys.argv[1:]))
