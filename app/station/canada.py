"""Canadian hams' home towns, so a VE6 call in Red Deer points at Red Deer, not the middle of Alberta.

Sources, downloaded once a month and kept on disk so look-ups work offline:
  ISED amateur call sign list (Open Government Licence - Canada)
    https://apc-cap.ic.gc.ca/datafiles/amateur_delim.zip
  GeoNames postal-code locations for Canada (CC BY 4.0)
    https://download.geonames.org/export/zip/CA.zip
Only each ham's name, town and province are kept. Street addresses are dropped.
A download is used only if it looks right: thousands of entries, including this station's own call.
"""
import csv
import io
import re
import sqlite3
import sys
import threading
import time
import urllib.request
import zipfile

from . import USER_AGENT
from pathlib import Path

ISED_URL = "https://apc-cap.ic.gc.ca/datafiles/amateur_delim.zip"
GEONAMES_URL = "https://download.geonames.org/export/zip/CA.zip"
REFRESH_DAYS = 30

# The list's columns, in order, for a file without a header line.
FIELDS = ["callsign", "first_name", "surname", "address_line", "city", "prov_cd", "postal_code",
          "qual_a", "qual_b", "qual_c", "qual_d", "qual_e", "club_name", "club_name_2",
          "club_address", "club_city", "club_prov_cd", "club_postal_code"]
PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories",
    "NU": "Nunavut", "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}
CALL = re.compile(r"^[A-Z0-9]{3,8}$")
CANADIAN_CALL = re.compile(r"^(V[A-GOXY]|C[F-KYZ]|X[J-O])\d")


def say(message):
    sys.stdout.write("%s\n" % message)   # one write, so lines from different threads don't run together
    sys.stdout.flush()


def tidy(text):
    """'RED  DEER' -> 'Red Deer'; names already in mixed case are left alone."""
    text = " ".join((text or "").split())
    return text.title() if text.isupper() else text


def read_postal_codes(text):
    """GeoNames CA.txt -> ({'T4N': (lat, lon)}, [(province, place in lower case, lat, lon)])."""
    fsa, places = {}, []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 11:
            continue
        try:
            lat, lon = float(parts[9]), float(parts[10])
        except ValueError:
            continue
        code = parts[1].replace(" ", "").upper()[:3]
        if len(code) == 3:
            fsa[code] = (lat, lon)
        places.append((parts[4].strip().upper(), parts[2].strip().lower(), lat, lon))
    return fsa, places


def _columns(header):
    """Which column holds which field, from a header line like callsign;first_name;surname;..."""
    names = [h.strip().lower() for h in header]

    def find(*words, club=False):
        for i, name in enumerate(names):
            if name.startswith("club") == club and any(w in name for w in words):
                return i
        return None

    return {
        "callsign": find("call"), "first_name": find("first", "given"), "surname": find("surname", "last"),
        "city": find("city"), "prov_cd": find("prov"), "postal_code": find("postal"),
        "club_name": find("club_name", "name", club=True), "club_city": find("city", club=True),
        "club_prov_cd": find("prov", club=True), "club_postal_code": find("postal", club=True),
    }


def read_call_list(text, fsa, places):
    """ISED's list -> [(call, name, town, province code, lat, lon)]; lat/lon are None if the town isn't found."""
    lines = text.splitlines()
    if not lines:
        return []
    delimiter = max(";|\t,", key=lines[0].count)
    rows = csv.reader(lines, delimiter=delimiter)
    first = next(rows, [])
    if any("call" in field.lower() for field in first):
        cols = _columns(first)
    else:
        cols = {name: i for i, name in enumerate(FIELDS)}
        rows = csv.reader(lines, delimiter=delimiter)
    second_name = cols.get("club_name")
    second_name = None if second_name is None else second_name + 1  # club_name_2 follows club_name

    def get(row, field):
        i = cols.get(field) if field != "club_name_2" else second_name
        return row[i].strip() if i is not None and i < len(row) else ""

    town_cache, out = {}, []
    for row in rows:
        call = get(row, "callsign").upper()
        if not CALL.match(call):
            continue
        name = " ".join(p for p in (tidy(get(row, "first_name")), tidy(get(row, "surname"))) if p)
        city, prov, postal = get(row, "city"), get(row, "prov_cd").upper(), get(row, "postal_code")
        if not name:  # a club station
            name = tidy(" ".join(p for p in (get(row, "club_name"), get(row, "club_name_2")) if p))
            if get(row, "club_city"):
                city, prov, postal = get(row, "club_city"), get(row, "club_prov_cd").upper(), get(row, "club_postal_code")
        city = tidy(city)
        where = fsa.get(postal.replace(" ", "").upper()[:3]) if postal else None
        if where is None and city:
            key = (prov, city.lower())
            if key not in town_cache:
                town = city.lower()
                word = re.compile(r"\b%s\b" % re.escape(town))
                same = [(place, la, lo) for p, place, la, lo in places if p == prov]
                hits = [(la, lo) for place, la, lo in same if place.startswith(town)] \
                    or [(la, lo) for place, la, lo in same if word.search(place)]
                town_cache[key] = (sum(h[0] for h in hits) / len(hits), sum(h[1] for h in hits) / len(hits)) if hits else None
            where = town_cache[key]
        lat, lon = where if where else (None, None)
        out.append((call, name, city, prov, lat, lon))
    return out


def _download(url, timeout=120):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _first_text_file(zip_bytes, prefer=None):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".txt") and "readme" not in n.lower()]
        if prefer:
            names.sort(key=lambda n: prefer.lower() not in n.lower())
        if not names:
            raise ValueError("no text file inside the download")
        raw = z.read(names[0])
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


class CanadaList:
    def __init__(self, data_dir, own_call, ised_url=ISED_URL, geonames_url=GEONAMES_URL):
        self.path = Path(data_dir) / "canada.sqlite3"
        self.own_call = own_call.upper()
        self.ised_url, self.geonames_url = ised_url, geonames_url
        self._lock = threading.Lock()

    # ---------- reading ----------
    def lookup(self, call):
        """{'name', 'city', 'prov', 'province', 'lat', 'lon'} for a Canadian call, or None."""
        if not self.path.exists():
            return None
        try:
            db = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=5)
            try:
                row = db.execute("SELECT name, city, prov, lat, lon FROM canada WHERE call = ?",
                                 (call.upper(),)).fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            return None
        if not row:
            return None
        name, city, prov, lat, lon = row
        return {"name": name, "city": city, "prov": prov, "province": PROVINCES.get(prov, prov),
                "lat": lat, "lon": lon}

    def info(self):
        if not self.path.exists():
            return None
        try:
            db = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=5)
            try:
                count = db.execute("SELECT COUNT(*) FROM canada").fetchone()[0]
                placed = db.execute("SELECT COUNT(*) FROM canada WHERE lat IS NOT NULL").fetchone()[0]
                updated = db.execute("SELECT value FROM info WHERE key = 'updated'").fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            return None
        return {"count": count, "placed": placed, "updated": updated[0] if updated else ""}

    # ---------- refreshing ----------
    def refresh(self, ised_bytes=None, geonames_bytes=None):
        """Downloads (or takes) both files and rebuilds the list. Returns the number of hams."""
        ised_bytes = ised_bytes if ised_bytes is not None else _download(self.ised_url)
        geonames_bytes = geonames_bytes if geonames_bytes is not None else _download(self.geonames_url)
        fsa, places = read_postal_codes(_first_text_file(geonames_bytes, prefer="CA.txt"))
        records = read_call_list(_first_text_file(ised_bytes, prefer="amateur"), fsa, places)
        calls = {r[0] for r in records}
        if len(records) < 1000:
            raise ValueError("only %d entries in the call sign list; keeping the old one" % len(records))
        if CANADIAN_CALL.match(self.own_call) and self.own_call not in calls:
            raise ValueError("%s isn't in the call sign list, so it may not have been read correctly"
                             % self.own_call)
        part = self.path.with_suffix(".part")
        if part.exists():
            part.unlink()
        db = sqlite3.connect(part)
        try:
            db.execute("CREATE TABLE canada (call TEXT PRIMARY KEY, name TEXT, city TEXT, prov TEXT, lat REAL, lon REAL)")
            db.executemany("INSERT OR REPLACE INTO canada VALUES (?, ?, ?, ?, ?, ?)", records)
            db.execute("CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO info VALUES ('updated', ?)", (time.strftime("%Y-%m-%d"),))
            db.commit()
        finally:
            db.close()
        with self._lock:
            part.replace(self.path)
        return len(calls)

    def due(self):
        return not self.path.exists() or time.time() - self.path.stat().st_mtime > REFRESH_DAYS * 86400

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="canada").start()

    def _run(self):
        while True:
            if self.due():
                try:
                    count = self.refresh()
                    say("Canadian call sign list updated: %d hams" % count)
                except Exception as exc:
                    say("Canadian call sign list update failed (will retry in 6 hours): %s" % exc)
            time.sleep(6 * 3600)
