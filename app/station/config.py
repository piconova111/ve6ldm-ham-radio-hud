"""Reads ~/station/config.ini. Every setting has a safe default."""
import configparser
import os
from pathlib import Path
from types import SimpleNamespace

from . import geo

STATION_DIR = Path(os.environ.get("STATION_DIR") or (Path.home() / "station"))
DATA_DIR = STATION_DIR / "data"
CONFIG_PATH = STATION_DIR / "config.ini"
PLACEHOLDER = (53.0, -114.0)  # central Alberta (where the app was first set up), until the real location is set
PLACEHOLDER_CALL = "N0CALL"   # shown until the callsign is set; live DX spots stay off until then


def _float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _int(text, default):
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def _yes(text):
    return text.strip().lower() not in ("no", "false", "off", "0")


def _timezone(name):
    """The time zone set in config.ini if it's a real one, otherwise this computer's (or UTC)."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return name or "UTC"
    names = [name]
    try:
        names.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    target = os.path.realpath("/etc/localtime")
    if "/zoneinfo/" in target:
        names.append(target.split("/zoneinfo/", 1)[1])
    for candidate in names:
        if candidate and ("/" in candidate or candidate == "UTC"):
            try:
                ZoneInfo(candidate)
                return candidate
            except Exception:
                continue
    return "UTC"


BAND_ORDER = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m", "2m", "1.25m", "70cm"]


def _bands(text, default):
    """'80m, 40m, 20 m' -> ['80m', '40m', '20m'] in band order (the bands a radio has antennas for)."""
    def names(t):
        return {b.strip().lower().replace(" ", "") for b in t.split(",")}
    found = names(text or default)
    return [b for b in BAND_ORDER if b in found] or [b for b in BAND_ORDER if b in names(default)]


def load():
    cp = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
    cp.read(CONFIG_PATH, encoding="utf-8")

    def get(section, key, default=""):
        return cp.get(section, key, fallback=default).strip()

    grid = get("station", "grid").replace(" ", "")
    lat, lon = _float(get("station", "latitude")), _float(get("station", "longitude"))
    location_set = True
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        pass
    elif geo.valid_grid(grid):
        lat, lon = geo.grid_to_latlon(grid)
    else:
        lat, lon = PLACEHOLDER
        location_set = False
    if location_set and not geo.valid_grid(grid):
        grid = geo.latlon_to_grid(lat, lon)

    callsign = (get("station", "callsign") or PLACEHOLDER_CALL).replace(" ", "").upper()
    place = get("station", "place")
    return SimpleNamespace(
        callsign=callsign,
        callsign_set=callsign != PLACEHOLDER_CALL,
        operator=get("station", "operator"),
        full_name=get("station", "full_name"),
        licence=get("station", "licence"),
        place=place,
        timezone=_timezone(get("station", "timezone")),
        cq_zone=_int(get("station", "cq_zone"), None),     # None: worked out from the callsign's country
        itu_zone=_int(get("station", "itu_zone"), None),
        grid=geo.tidy_grid(grid) if location_set else "",
        lat=lat,
        lon=lon,
        location_set=location_set,
        hamqth_user=get("hamqth", "username"),
        hamqth_pass=get("hamqth", "password"),
        radio_name=get("radio", "name") or "Radio",
        radio_model=_int(get("radio", "model", "1"), 1),
        rig_port=_int(get("radio", "rigctld_port", "4532"), 4532),
        radio_device=get("radio", "device"),
        radio_max_watts=_int(get("radio", "max_watts", "100"), 100),
        radio_bands=_bands(get("radio", "bands"), "80m, 40m, 30m, 20m, 17m, 15m, 12m, 10m"),
        radio2_name=get("radio2", "name") or "Second radio",
        radio2_model=_int(get("radio2", "model", "0"), 0),
        radio2_port=_int(get("radio2", "rigctld_port", "4533"), 4533),
        radio2_device=get("radio2", "device"),
        radio2_max_watts=_int(get("radio2", "max_watts", "100"), 100),
        radio2_bands=_bands(get("radio2", "bands"), "80m, 40m, 30m, 20m, 17m, 15m, 12m, 10m"),
        port=_int(get("display", "port", "8073"), 8073),
        show_zones=_yes(get("display", "show_zones", "yes")),
        globe_on_lookup=_yes(get("display", "globe_on_lookup", "yes")),
        welcome=_yes(get("display", "welcome", "yes")),
        welcome_text=get("display", "welcome_text") or "On the air around the world" + (" from %s" % place if place else ""),
        checklist=_yes(get("display", "checklist", "yes")),
        weather_region=get("alerts", "weather_region"),
        space_weather=_yes(get("alerts", "space_weather", "yes")),
        alerts_space_url=get("alerts", "space_url") or None,
        alerts_weather_url=get("alerts", "weather_url") or None,
        voice_only=_yes(get("display", "voice_only", "yes")),
        cluster_host=get("cluster", "host", "dxc.ve7cc.net"),
        cluster_port=_int(get("cluster", "port", "23"), 23),
        cluster_login=(get("cluster", "login") or "%s-2" % callsign).upper(),
        cluster_backups=get("cluster", "backups"),
        conditions_url=get("conditions", "url") or None,
        iss_url=get("satellites", "url") or None,
        amateur_url=get("satellites", "amateur_url") or None,
        amsat_url=get("satellites", "amsat_url") or None,
        fm_sats=[s.strip().upper() for s in get("satellites", "fm", "SO-50, AO-123, AO-91, PO-101").split(",") if s.strip()],
        openings=_yes(get("openings", "enabled", "yes")),
        openings_host=get("openings", "host") or None,
        openings_port=_int(get("openings", "port"), None),
        openings_tls=_yes(get("openings", "tls", "yes")),
        kiwi_url=get("kiwi", "list_url") or None,
        kiwi_fallback=get("kiwi", "fallback_url") or None,
        canada_ised_url=get("canada", "ised_url") or None,
        canada_geonames_url=get("canada", "geonames_url") or None,
        licensed=get("personal", "licensed"),
        birthday=get("personal", "birthday"),
        note=get("personal", "note"),
        helper=get("personal", "helper"),    # who looks after the station: the checklist notes are for them
        waterfall_start=_yes(get("waterfall", "start", "no")),
        waterfall_follow=_yes(get("waterfall", "follow_radio", "yes")),
        waterfall_if_hz=_int(get("waterfall", "if_hz", "69450000"), 69450000),
        waterfall_port=_int(get("waterfall", "remote_port", "7356"), 7356),
    )
