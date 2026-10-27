"""Callsign -> country, CQ/ITU zones and rough location, from the AD1C country file (cty.dat).

Works offline. The file format is described at https://www.country-files.com/cty-dat-format/
(longitudes and time offsets in the file are positive for WEST; they are flipped here).
"""
import re
import threading
from pathlib import Path

_TOKEN = re.compile(r"^(=?)([A-Z0-9/]+)(.*)$")
_OVERRIDE = re.compile(r"\((\d+)\)|\[(\d+)\]|<([-\d.]+)/([-\d.]+)>|\{(\w+)\}|~([-\d.]+)~")

# Endings that say how someone is operating, not where.
SUFFIXES = {"P", "M", "QRP", "A", "B", "R", "J", "LH", "BCN", "LGT", "Q"}

# Canadian call areas, so a VE7 points at British Columbia instead of the middle of Canada.
CANADA_AREAS = {
    "1": ("Nova Scotia", 44.9, -63.2), "2": ("Quebec", 46.8, -71.2), "3": ("Ontario", 44.2, -79.6),
    "4": ("Manitoba", 49.9, -97.1), "5": ("Saskatchewan", 51.5, -105.7), "6": ("Alberta", 52.5, -114.0),
    "7": ("British Columbia", 49.6, -122.6), "8": ("Northwest Territories", 62.5, -114.4),
    "9": ("New Brunswick", 46.2, -66.0),
}
CANADA_SPECIAL = {
    "VO1": ("Newfoundland", 47.6, -52.8), "VO2": ("Labrador", 53.3, -60.4), "VY0": ("Nunavut", 63.7, -68.5),
    "VY1": ("Yukon", 60.7, -135.1), "VY2": ("Prince Edward Island", 46.3, -63.1),
}


def prefix_part(call):
    """The part of a callsign that decides the country: KH6/W1AW -> KH6, DL1ABC/P -> DL1ABC."""
    parts = [p for p in call.upper().split("/") if p]
    if not parts or parts[-1] in ("MM", "AM"):  # maritime / aeronautical mobile: no country
        return None
    parts = [p for p in parts if p not in SUFFIXES and not (len(p) == 1 and p.isdigit())]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    shortest = min(parts, key=len)
    return shortest if len(shortest) <= 4 else parts[0]


def parse(text):
    entities, prefixes, exact = [], {}, {}
    for record in text.split(";"):
        record = record.strip()
        if not record:
            continue
        parts = record.split(":", 8)
        if len(parts) < 9:
            continue
        name, cq, itu, cont, lat, lon, utc, primary = (p.strip() for p in parts[:8])
        try:
            entity = {
                "name": name, "cq": int(cq), "itu": int(itu), "continent": cont,
                "lat": float(lat), "lon": -float(lon), "utc": -float(utc),
                "prefix": primary.lstrip("*"),
                "dxcc": not primary.startswith("*"),  # a * marks areas that only count for other awards (WAE)
            }
        except ValueError:
            continue
        index = len(entities)
        entities.append(entity)
        for raw in parts[8].replace("\r", " ").replace("\n", " ").split(","):
            m = _TOKEN.match(raw.strip())
            if not m:
                continue
            overrides = {}
            for o in _OVERRIDE.finditer(m.group(3)):
                if o.group(1):
                    overrides["cq"] = int(o.group(1))
                elif o.group(2):
                    overrides["itu"] = int(o.group(2))
                elif o.group(3):
                    overrides["lat"], overrides["lon"] = float(o.group(3)), -float(o.group(4))
                elif o.group(5):
                    overrides["continent"] = o.group(5)
                elif o.group(6):
                    overrides["utc"] = -float(o.group(6))
            (exact if m.group(1) == "=" else prefixes)[m.group(2)] = (index, overrides)
    return entities, prefixes, exact


class CountryFile:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = ([], {}, {})
        self.reload()

    @property
    def loaded(self):
        return bool(self._data[0])

    def reload(self):
        try:
            text = self.path.read_text(encoding="latin-1")
        except OSError:
            return False
        data = parse(text)
        if not data[0]:
            return False
        with self._lock:
            self._data = data
        return True

    def entities(self):
        """Every country (DXCC entity) in the file, in file order."""
        with self._lock:
            return [dict(e) for e in self._data[0]]

    def by_name(self, name):
        with self._lock:
            for e in self._data[0]:
                if e["name"] == name:
                    return dict(e)
        return None

    def lookup(self, call):
        call = call.strip().upper()
        with self._lock:
            entities, prefixes, exact = self._data
        if not entities:
            return None
        hit = exact.get(call)
        if hit is None:
            base = prefix_part(call)
            if not base:
                return None
            for i in range(len(base), 0, -1):
                hit = prefixes.get(base[:i])
                if hit:
                    break
        if not hit:
            return None
        index, overrides = hit
        result = dict(entities[index], region="")
        result.update(overrides)
        if result["prefix"] == "VE":
            self._canadian_area(call, result)
        return result

    @staticmethod
    def _canadian_area(call, result):
        m = re.match(r"^([A-Z]{1,2})(\d)", prefix_part(call) or "")
        if not m:
            return
        area = CANADA_SPECIAL.get(m.group(1) + m.group(2))
        if area is None and m.group(1) in ("VE", "VA"):
            area = CANADA_AREAS.get(m.group(2))
        if area:
            result["region"], result["lat"], result["lon"] = area
