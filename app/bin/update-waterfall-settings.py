#!/usr/bin/env python3
"""Brings an older [waterfall] section in ~/station/config.ini up to date, once.

  update-waterfall-settings.py EXAMPLE.ini [CONFIG.ini]

Before 0.14 the waterfall was Gqrx, and its section had notes about Gqrx and two settings only Gqrx
used (follow_radio and remote_port). This writes the section out again the way EXAMPLE.ini has it
today, each setting with its note, keeping every value that was already set (start, if_hz, and any
others). Settings it doesn't know are kept at the end of the section. A copy of the old file is kept
beside it (config.ini.before-0.14). A section that's already up to date is left alone.
"""
import os
import re
import sys
from pathlib import Path

OLD_ONLY = ("follow_radio", "remote_port")


def section_span(lines, name):
    """(first, end) line indexes of [name]: its header line to just before the next section (or the end)."""
    first = next((i for i, line in enumerate(lines) if line.strip() == "[%s]" % name), None)
    if first is None:
        return None
    end = next((i for i in range(first + 1, len(lines)) if re.match(r"^\s*\[[^\]]+\]\s*$", lines[i])), len(lines))
    while end > first + 1 and not lines[end - 1].strip():   # leave the blank lines before the next section
        end -= 1
    return first, end


def settings_in(lines):
    """{key: value} for the key = value lines (the value as written, without a trailing " # note")."""
    out = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith(("#", ";", "[")) or "=" not in s:
            continue
        key, value = s.split("=", 1)
        out[key.strip()] = re.split(r"\s[#;]", value, maxsplit=1)[0].strip()
    return out


def main(args):
    if len(args) not in (1, 2):
        print(__doc__)
        return 2
    example = Path(args[0])
    config = Path(args[1]) if len(args) == 2 else Path(os.environ.get("STATION_DIR", Path.home() / "station")) / "config.ini"
    if not config.is_file():
        return 0
    text = config.read_text(encoding="utf-8")
    lines = text.splitlines()
    span = section_span(lines, "waterfall")
    if span is None:
        return 0
    first, end = span
    old = lines[first:end]
    if not any("gqrx" in line.lower() or line.strip().split("=")[0].strip() in OLD_ONLY for line in old):
        return 0   # already up to date
    have = settings_in(old)
    ex_lines = example.read_text(encoding="utf-8").splitlines()
    ex_span = section_span(ex_lines, "waterfall")
    if ex_span is None:
        print("update-waterfall-settings: %s has no [waterfall] section" % example)
        return 1
    new, known = [], set()
    for line in ex_lines[ex_span[0]:ex_span[1]]:
        s = line.strip()
        if s and not s.startswith(("#", ";", "[")) and "=" in s:
            key = s.split("=", 1)[0].strip()
            known.add(key)
            if key in have:
                line = "%s = %s" % (key, have[key])
        new.append(line)
    extra = [line for line in old if "=" in line and not line.strip().startswith(("#", ";"))
             and line.split("=", 1)[0].strip() not in known | set(OLD_ONLY)]
    lines[first:end] = new + extra
    backup = config.with_name(config.name + ".before-0.14")
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")
    tmp = config.with_name(config.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, config)
    print("updated the [waterfall] settings for the new waterfall (the old file is kept as %s)" % backup.name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
