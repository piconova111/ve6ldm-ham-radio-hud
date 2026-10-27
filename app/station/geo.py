"""Grid squares, beam headings, distances and time zones. Standard library only."""
import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

EARTH_KM = 6371.0
_GRID = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$")
_COORD = re.compile(r"^([+-])(\d{2})(\d{2})(\d{2})?([+-])(\d{3})(\d{2})(\d{2})?$")


def valid_grid(grid):
    return bool(grid) and bool(_GRID.match(grid.strip().upper()))


def tidy_grid(grid):
    """'do33AB' -> 'DO33ab', the usual way grid squares are written."""
    g = grid.strip()
    return g[:4].upper() + g[4:6].lower()


def grid_to_latlon(grid):
    """Centre of a 4- or 6-character Maidenhead grid square."""
    g = grid.strip().upper()
    if not _GRID.match(g):
        raise ValueError("not a grid square: %r" % grid)
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) == 6:
        lon += (ord(g[4]) - 65) / 12 + 1 / 24
        lat += (ord(g[5]) - 65) / 24 + 1 / 48
    else:
        lon += 1
        lat += 0.5
    return round(lat, 4), round(lon, 4)


def latlon_to_grid(lat, lon):
    lon = (lon + 180.0) % 360.0
    lat = min(max(lat + 90.0, 0.0), 179.99999)
    return (chr(65 + int(lon // 20)) + chr(65 + int(lat // 10))
            + str(int((lon % 20) // 2)) + str(int(lat % 10))
            + chr(97 + int((lon % 2) * 12)) + chr(97 + int((lat % 1) * 24)))


def bearing(lat1, lon1, lat2, lon2):
    """Short-path beam heading in degrees from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


@lru_cache(maxsize=1)
def _zones():
    """[(lat, lon, zone name, standard UTC offset in hours)] from the system time zone tables."""
    year = datetime.now().year
    jan, jul = datetime(year, 1, 15, 12), datetime(year, 7, 15, 12)
    for table in ("zone1970.tab", "zone.tab"):
        path = Path("/usr/share/zoneinfo") / table
        if not path.exists():
            continue
        zones = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 3:
                continue
            m = _COORD.match(fields[1])
            if not m:
                continue
            lat = int(m.group(2)) + int(m.group(3)) / 60 + int(m.group(4) or 0) / 3600
            lon = int(m.group(6)) + int(m.group(7)) / 60 + int(m.group(8) or 0) / 3600
            lat = -lat if m.group(1) == "-" else lat
            lon = -lon if m.group(5) == "-" else lon
            try:
                tz = ZoneInfo(fields[2])
                std = min(tz.utcoffset(jan), tz.utcoffset(jul)).total_seconds() / 3600
            except Exception:
                continue
            zones.append((lat, lon, fields[2], std))
        if zones:
            return zones
    return []


def timezone_for(lat, lon, std_offset_hint=None):
    """Best guess at the time zone for a spot on the map.

    Takes the zone whose main city is nearest. If the country file gives the
    country's standard offset, a zone with that offset wins unless it is much
    farther away (that keeps Spain on Madrid time near the Portuguese border,
    while a Californian still gets Pacific time).
    """
    zones = _zones()
    if not zones:
        return None
    ranked = sorted(zones, key=lambda z: distance_km(lat, lon, z[0], z[1]))
    nearest = ranked[0]
    if std_offset_hint is None:
        return nearest[2]
    limit = 2 * distance_km(lat, lon, nearest[0], nearest[1]) + 300
    for z in ranked:
        if abs(z[3] - std_offset_hint) < 0.01:
            if distance_km(lat, lon, z[0], z[1]) <= limit:
                return z[2]
            break
    return nearest[2]
