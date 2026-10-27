#!/usr/bin/env python3
"""Changes settings in ~/station/config.ini, keeping every other line and note as it was.

  set-setting.py SECTION KEY [VALUE]        (or pass the value in SETTING_VALUE)
  set-setting.py SECTION KEY VALUE --note "TEXT"   (a new setting gets the note above it)
  set-setting.py --location "TEXT" [--check]

--location takes map coordinates ("52.2681, -113.8112") or a grid square ("DO32ab").
With --check it only describes the place; the exit code is 0 for a place it
understood, and 1 if it couldn't be understood.
"""
import os
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from station import config, geo  # noqa: E402

PATH = config.CONFIG_PATH


def set_value(section, key, value, note=None):
    lines = PATH.read_text(encoding="utf-8").splitlines() if PATH.exists() else []
    out, current, done = [], None, False
    notes = ["# " + line for line in note.splitlines()] if note else []
    for line in lines:
        header = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if header:
            if current == section and not done:
                while out and not out[-1].strip():
                    out.pop()
                out += notes + ["%s = %s" % (key, value), ""]
                done = True
            current = header.group(1)
        elif current == section and not done and re.match(r"^\s*%s\s*=" % re.escape(key), line):
            out.append("%s = %s" % (key, value))
            done = True
            continue
        out.append(line)
    if not done:
        if current != section:
            out += ["", "[%s]" % section]
        out += notes + ["%s = %s" % (key, value)]
    PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def read_location(text):
    """(lat, lon, grid) from coordinates or a grid square, or None."""
    text = text.strip()
    if geo.valid_grid(text):
        lat, lon = geo.grid_to_latlon(text)
        return lat, lon, geo.tidy_grid(text)
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$", text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return round(lat, 5), round(lon, 5), None
    return None


def location(text, check_only):
    place = read_location(text)
    if place is None:
        print("That isn't a grid square (like DO32ab) or map coordinates (like 52.2681, -113.8112).")
        return 1
    lat, lon, grid = place
    shown = grid or geo.tidy_grid(geo.latlon_to_grid(lat, lon))
    if check_only:
        print("%.4f, %.4f (grid %s)" % (lat, lon, shown))
        return 0
    if grid:   # a grid square: let the app work out the middle of it
        set_value("station", "grid", grid)
        set_value("station", "latitude", "")
        set_value("station", "longitude", "")
    else:      # exact coordinates are better than any grid square
        set_value("station", "latitude", "%s" % lat)
        set_value("station", "longitude", "%s" % lon)
        set_value("station", "grid", "")
    print("Location saved: %.4f, %.4f (grid %s)" % (lat, lon, shown))
    return 0


def main(args):
    if args and args[0] == "--location" and len(args) >= 2:
        return location(args[1], "--check" in args[2:])
    note = None
    if "--note" in args:
        i = args.index("--note")
        note = args[i + 1] if i + 1 < len(args) else None
        args = args[:i] + args[i + 2:]
    if len(args) in (2, 3):
        value = args[2] if len(args) == 3 else os.environ.get("SETTING_VALUE", "")
        set_value(args[0], args[1], value, note)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
