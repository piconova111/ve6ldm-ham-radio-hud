#!/usr/bin/env python3
"""Adds settings that ~/station/config.ini doesn't have yet, from a file of saved settings.

  merge-settings.py SAVED.ini

The installer runs this when a package comes with saved settings of its own (a config.ini
beside install.sh). Each setting in SAVED.ini that's missing from ~/station/config.ini is
added, with the note lines written just above it. Settings already there are never changed.
"""
import configparser
import importlib.util
import re
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent


def saved_settings(path):
    """[(section, key, value, note)] in file order; note is the comment lines just above the key."""
    out, section, notes = [], None, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        header = re.match(r"^\[([^\]]+)\]$", stripped)
        if header:
            section, notes = header.group(1), []
        elif stripped.startswith(("#", ";")):
            notes.append(re.sub(r"^[#;] ?", "", stripped))
        elif not stripped:
            notes = []
        elif section and "=" in stripped:
            key, value = stripped.split("=", 1)
            out.append((section, key.strip(), value.strip(), "\n".join(notes) or None))
            notes = []
    return out


def main(args):
    if len(args) != 1:
        print(__doc__)
        return 2
    spec = importlib.util.spec_from_file_location("set_setting", BIN / "set-setting.py")
    setter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setter)
    have = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
    have.read(setter.PATH, encoding="utf-8")
    added = 0
    for section, key, value, note in saved_settings(args[0]):
        if not have.has_option(section, key):
            setter.set_value(section, key, value, note)
            have.read(setter.PATH, encoding="utf-8")
            print("added %s under [%s]" % (key, section))
            added += 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
