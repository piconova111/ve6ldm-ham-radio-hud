#!/usr/bin/env python3
"""Gives WSJT-X this station's settings, the first time only (when WSJT-X has no settings file yet):

  - the callsign, and the grid square to 4 characters (WSJT-X sends only 4 in FT8 anyway, and that's all
    it would ever put on the internet, if PSK Reporter spotting were turned on in WSJT-X)
  - the radio: Hamlib NET rigctl through the station's receive-only radio link (127.0.0.1, [ft8] rig_port),
    so WSJT-X follows the FT-950 but can't make it transmit
  - the sound: input station_receiver (the waterfall's receiver), output station_no_transmit (goes nowhere)

Everything else is left at WSJT-X's own defaults, which include PTT method VOX, mode and split "None"
(WSJT-X doesn't change the radio's mode), PSK Reporter spotting off, and FT8.

  wsjtx-preset.py          writes ~/.config/WSJT-X.ini if it isn't there; prints what it did

If WSJT-X already has settings, they're never changed (the health report says if they reach the radio
some other way).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from station import config, ft8   # noqa: E402


def preset_text(callsign, grid, link_port):
    lines = ["[Configuration]"]
    if callsign:
        lines.append("MyCall=" + callsign)
    if grid:
        lines.append("MyGrid=" + grid[:4])
    lines += ["Rig=Hamlib NET rigctl",
              "CATNetworkPort=127.0.0.1:%d" % link_port,
              "SoundInName=" + ft8.SOURCE,
              "SoundOutName=" + ft8.SINK,
              ""]
    return "\n".join(lines)


def main():
    cfg = config.load()
    path = ft8.wsjtx_ini_path()
    if path.exists():
        print("WSJT-X already has its own settings (%s), so they're left as they are" % path)
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    text = preset_text(cfg.callsign if cfg.callsign_set else "", cfg.grid if cfg.location_set else "", cfg.ft8_rig_port)
    tmp = path.with_name(path.name + ".station-new")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    print("WSJT-X's settings: %s%sthe radio through the station's receive-only link, sound from the waterfall's receiver"
          % (cfg.callsign + ", " if cfg.callsign_set else "", cfg.grid[:4] + ", " if cfg.location_set else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
