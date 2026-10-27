"""The station logbook (SQLite), its ADIF export, and the callbook look-up cache."""
import json
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BANDS = [  # (lowest Hz, highest Hz, ADIF band name)
    (1_800_000, 2_000_000, "160m"), (3_500_000, 4_000_000, "80m"), (5_250_000, 5_450_000, "60m"),
    (7_000_000, 7_300_000, "40m"), (10_100_000, 10_150_000, "30m"), (14_000_000, 14_350_000, "20m"),
    (18_068_000, 18_168_000, "17m"), (21_000_000, 21_450_000, "15m"), (24_890_000, 24_990_000, "12m"),
    (28_000_000, 29_700_000, "10m"), (50_000_000, 54_000_000, "6m"), (144_000_000, 148_000_000, "2m"),
    (222_000_000, 225_000_000, "1.25m"), (420_000_000, 450_000_000, "70cm"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS qso (
    id        INTEGER PRIMARY KEY,
    call      TEXT NOT NULL,
    time_on   TEXT NOT NULL,              -- UTC, e.g. 2026-10-01T19:42:00Z
    freq_hz   INTEGER,
    band      TEXT,
    mode      TEXT,
    submode   TEXT,
    rst_sent  TEXT,
    rst_rcvd  TEXT,
    name      TEXT,
    qth       TEXT,
    grid      TEXT,
    country   TEXT,
    cq_zone   INTEGER,
    itu_zone  INTEGER,
    notes     TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    distance_km REAL,                    -- from the station, worked out when it was logged
    tx_pwr    REAL,                      -- watts, read from the radio while transmitting
    my_rig    TEXT                       -- which radio (its name from config.ini), when it was read from the radio
);
CREATE INDEX IF NOT EXISTS qso_call ON qso(call);
CREATE INDEX IF NOT EXISTS qso_time ON qso(time_on);
CREATE TABLE IF NOT EXISTS heard (           -- spots of this station by other hams
    id        INTEGER PRIMARY KEY,
    spotter   TEXT NOT NULL,
    freq_khz  REAL,
    band      TEXT,
    comment   TEXT,
    time      TEXT NOT NULL,
    UNIQUE (spotter, time)
);
CREATE TABLE IF NOT EXISTS lookup_cache (
    call       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
"""

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
PHONE = {"SSB", "AM", "FM"}


BAND_NAMES = {name for _, _, name in BANDS}


def band_for(freq_hz):
    if not freq_hz:
        return None
    for low, high, name in BANDS:
        if low <= freq_hz <= high:
            return name
    return None


def adif_mode(rig_mode):
    """Hamlib's mode name -> (ADIF MODE, SUBMODE)."""
    m = (rig_mode or "").strip().upper()
    if m in ("USB", "LSB"):
        return "SSB", m
    if m in ("PKTUSB", "PKTLSB"):
        return "SSB", m[3:]
    if m.startswith("CW"):
        return "CW", ""
    if m in ("AM", "SAM", "AMS", "PKTAM"):
        return "AM", ""
    if m in ("FM", "FMN", "WFM", "PKTFM"):
        return "FM", ""
    if m.startswith("RTTY"):
        return "RTTY", ""
    return m, ""


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value, limit=120):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number >= 0 else None   # not NaN, not negative


def _ascii(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


class Logbook:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)
            have = {row["name"] for row in db.execute("PRAGMA table_info(qso)")}
            for column, kind in (("distance_km", "REAL"), ("tx_pwr", "REAL"),   # added in version 0.9.0
                                 ("my_rig", "TEXT")):                          # added in version 0.11.0
                if column not in have:
                    db.execute("ALTER TABLE qso ADD COLUMN %s %s" % (column, kind))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            yield db
            db.commit()
        finally:
            db.close()

    def add(self, q):
        call = (_clean(q.get("call"), 20) or "").upper()
        if not call:
            raise ValueError("A callsign is needed.")
        freq = _int(q.get("freq_hz"))
        mode, submode = adif_mode(q.get("mode"))
        default_rst = "599" if mode in ("CW", "RTTY") else "59"
        time_on = _clean(q.get("time_on"), 20)
        row = {
            "call": call,
            "time_on": time_on if time_on and _ISO.match(time_on) else utc_now(),
            "freq_hz": freq,
            "band": band_for(freq) or (q.get("band") if q.get("band") in BAND_NAMES else None),
            "mode": mode or None,
            "submode": submode or None,
            "rst_sent": _clean(q.get("rst_sent"), 8) or default_rst,
            "rst_rcvd": _clean(q.get("rst_rcvd"), 8) or default_rst,
            "name": _clean(q.get("name")),
            "qth": _clean(q.get("qth")),
            "grid": _clean(q.get("grid"), 8),
            "country": _clean(q.get("country")),
            "cq_zone": _int(q.get("cq_zone")),
            "itu_zone": _int(q.get("itu_zone")),
            "notes": _clean(q.get("notes"), 500),
            "distance_km": _num(q.get("distance_km")),
            "tx_pwr": _num(q.get("tx_pwr")),
            "my_rig": _clean(q.get("my_rig"), 40),
        }
        with self._db() as db:
            cur = db.execute(
                "INSERT INTO qso (%s) VALUES (%s)" % (", ".join(row), ", ".join(":" + k for k in row)), row)
            new_id = cur.lastrowid
        return self.get(new_id)

    def get(self, qso_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM qso WHERE id = ?", (qso_id,)).fetchone()
        return dict(row) if row else None

    def delete(self, qso_id):
        with self._db() as db:
            db.execute("DELETE FROM qso WHERE id = ?", (int(qso_id),))

    def recent(self, limit=12):
        with self._db() as db:
            rows = db.execute(
                "SELECT id, call, time_on, freq_hz, band, mode, submode, country FROM qso "
                "ORDER BY time_on DESC, id DESC LIMIT ?", (max(1, min(int(limit), 200)),)).fetchall()
        return [dict(r) for r in rows]

    def history(self, call):
        with self._db() as db:
            count = db.execute("SELECT COUNT(*) FROM qso WHERE call = ?", (call,)).fetchone()[0]
            last = db.execute("SELECT time_on, band, mode, submode FROM qso WHERE call = ? "
                              "ORDER BY time_on DESC, id DESC LIMIT 1", (call,)).fetchone()
        return {"count": count, "last": dict(last) if last else None}

    def has_country(self, country):
        if not country:
            return True
        with self._db() as db:
            return db.execute("SELECT 1 FROM qso WHERE country = ? LIMIT 1", (country,)).fetchone() is not None

    def has_zone(self, zone):
        if not zone:
            return True
        with self._db() as db:
            return db.execute("SELECT 1 FROM qso WHERE cq_zone = ? LIMIT 1", (int(zone),)).fetchone() is not None

    def zones(self):
        """CQ zones in the log: {zone: {"count", "last_call", "last_time"}} for zones 1 to 40."""
        with self._db() as db:
            rows = db.execute(
                "SELECT cq_zone, COUNT(*) AS n, MAX(time_on) AS last FROM qso "
                "WHERE cq_zone BETWEEN 1 AND 40 GROUP BY cq_zone").fetchall()
            out = {}
            for r in rows:
                call = db.execute("SELECT call FROM qso WHERE cq_zone = ? ORDER BY time_on DESC, id DESC LIMIT 1",
                                  (r["cq_zone"],)).fetchone()
                out[r["cq_zone"]] = {"count": r["n"], "last_call": call["call"] if call else "", "last_time": r["last"]}
        return out

    def stats(self):
        month_start = datetime.now(timezone.utc).strftime("%Y-%m-01T00:00:00Z")
        with self._db() as db:
            total = db.execute("SELECT COUNT(*) FROM qso").fetchone()[0]
            countries = db.execute("SELECT COUNT(DISTINCT country) FROM qso "
                                   "WHERE country IS NOT NULL AND country != ''").fetchone()[0]
            zones = db.execute("SELECT COUNT(DISTINCT cq_zone) FROM qso WHERE cq_zone BETWEEN 1 AND 40").fetchone()[0]
            month = db.execute("SELECT COUNT(*) FROM qso WHERE time_on >= ?", (month_start,)).fetchone()[0]
        return {"total": total, "countries": countries, "zones": zones, "this_month": month}

    def worked(self):
        """Countries and CQ zones in the log, with the bands each was worked on."""
        countries, zones = {}, {}
        with self._db() as db:
            rows = db.execute("SELECT call, country, cq_zone, band, time_on FROM qso ORDER BY time_on, id").fetchall()
        for r in rows:
            for table, key in ((countries, r["country"]), (zones, r["cq_zone"] if r["cq_zone"] and 1 <= r["cq_zone"] <= 40 else None)):
                if not key:
                    continue
                item = table.setdefault(key, {"count": 0, "bands": [], "first_call": r["call"], "first_time": r["time_on"]})
                item["count"] += 1
                item["last_call"], item["last_time"] = r["call"], r["time_on"]
                if r["band"] and r["band"] not in item["bands"]:
                    item["bands"].append(r["band"])
        return {"countries": countries, "zones": zones}

    def all_contacts(self):
        """Every contact, oldest first (for the stats panel)."""
        with self._db() as db:
            rows = db.execute("SELECT id, call, time_on, band, mode, submode, grid, country, cq_zone, "
                              "distance_km, tx_pwr FROM qso ORDER BY time_on, id").fetchall()
        return [dict(r) for r in rows]

    def add_heard(self, spot):
        """Records a spot of this station. Returns True if it is new."""
        with self._db() as db:
            cur = db.execute("INSERT OR IGNORE INTO heard (spotter, freq_khz, band, comment, time) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (spot["spotter"], spot["freq_khz"], spot["band"], spot["comment"],
                              spot["time"].strftime("%Y-%m-%dT%H:%M:%SZ")))
            return cur.rowcount == 1

    def heard_since(self, since_iso, limit=20):
        with self._db() as db:
            rows = db.execute("SELECT * FROM heard WHERE time >= ? ORDER BY time DESC, id DESC LIMIT ?",
                              (since_iso, limit)).fetchall()
        return [dict(r) for r in rows]

    def cache_get(self, call):
        with self._db() as db:
            row = db.execute("SELECT data, fetched_at FROM lookup_cache WHERE call = ?", (call,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["data"]), row["fetched_at"]
        except ValueError:
            return None

    def cache_put(self, call, data):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO lookup_cache (call, data, fetched_at) VALUES (?, ?, ?)",
                       (call, json.dumps(data), time.time()))

    def export_adif(self, station_call, version):
        def field(name, value):
            if value is None or value == "":
                return ""
            text = _ascii(str(value))
            return "<%s:%d>%s " % (name, len(text), text)

        created = datetime.now(timezone.utc).strftime("%Y%m%d %H%M%S")
        out = ["%s station logbook\n" % station_call,
               field("ADIF_VER", "3.1.4") + field("PROGRAMID", "Ham-Radio-HUD")
               + field("PROGRAMVERSION", version) + field("CREATED_TIMESTAMP", created) + "<EOH>\n\n"]
        with self._db() as db:
            rows = db.execute("SELECT * FROM qso ORDER BY time_on, id").fetchall()
        for r in rows:
            t = r["time_on"]
            freq = "%.6f" % (r["freq_hz"] / 1e6) if r["freq_hz"] else ""
            out.append(
                field("CALL", r["call"]) + field("QSO_DATE", t[0:4] + t[5:7] + t[8:10])
                + field("TIME_ON", t[11:13] + t[14:16] + t[17:19]) + field("BAND", r["band"])
                + field("FREQ", freq) + field("MODE", r["mode"]) + field("SUBMODE", r["submode"])
                + field("RST_SENT", r["rst_sent"]) + field("RST_RCVD", r["rst_rcvd"])
                + field("NAME", r["name"]) + field("QTH", r["qth"]) + field("GRIDSQUARE", r["grid"])
                + field("COUNTRY", r["country"]) + field("CQZ", r["cq_zone"]) + field("ITUZ", r["itu_zone"])
                + field("DISTANCE", "%d" % round(r["distance_km"]) if r["distance_km"] else "")
                + field("TX_PWR", "%g" % round(r["tx_pwr"]) if r["tx_pwr"] else "")
                + field("MY_RIG", r["my_rig"])
                + field("COMMENT", r["notes"]) + field("STATION_CALLSIGN", station_call) + "<EOR>\n")
        return "".join(out)
