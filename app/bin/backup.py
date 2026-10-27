#!/usr/bin/env python3
"""Nightly logbook backup, run by station-backup.timer (and once by the installer).

Writes ~/station-backups/logbook-YYYY-MM-DD.sqlite3 (a complete copy) and
logbook-YYYY-MM-DD.adi (the same contacts as ADIF), plus the latest settings
file, and keeps the most recent 60 of each.
"""
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from station import config, logbook  # noqa: E402

KEEP = 60


def main():
    cfg = config.load()
    version = (APP_DIR / "VERSION").read_text().strip() if (APP_DIR / "VERSION").exists() else "dev"
    dest = Path.home() / "station-backups"
    dest.mkdir(exist_ok=True)
    source = config.DATA_DIR / "logbook.sqlite3"
    book = logbook.Logbook(source)  # creates an empty logbook if there isn't one yet
    stamp = date.today().isoformat()

    copy_path = dest / ("logbook-%s.sqlite3" % stamp)
    src = sqlite3.connect(source)
    out = sqlite3.connect(copy_path)
    with out:
        src.backup(out)
    out.close()
    src.close()
    (dest / ("logbook-%s.adi" % stamp)).write_text(book.export_adif(cfg.callsign, version), encoding="ascii")
    if config.CONFIG_PATH.exists():
        shutil.copy2(config.CONFIG_PATH, dest / "config.ini")

    for pattern in ("logbook-*.sqlite3", "logbook-*.adi"):
        for old in sorted(dest.glob(pattern))[:-KEEP]:
            old.unlink()
    print("Backed up %d contacts to %s" % (book.stats()["total"], dest))


if __name__ == "__main__":
    main()
