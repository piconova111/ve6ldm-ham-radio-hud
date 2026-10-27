#!/usr/bin/env python3
"""The answers on the tester checklist, as plain text to read or paste.

Run it here, or from another computer with:  station feedback
"""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from station import checklist, config  # noqa: E402


def main():
    try:
        version = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        version = "dev"
    data = checklist.Checklist(config.DATA_DIR / "checklist.json").snapshot(version, checklist.words(config.load()))
    print(checklist.report(data))


if __name__ == "__main__":
    main()
