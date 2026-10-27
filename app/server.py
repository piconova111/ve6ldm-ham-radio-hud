#!/usr/bin/env python3
"""The station app (the ham radio station HUD): serves the big-screen display and a small local API.

Started by station-app.service. It only listens on 127.0.0.1, so nothing
outside this computer can reach it. Settings live in ~/station/config.ini.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from station import alerts, canada, checklist, cluster, conditions, config, contests, cty, favourites, geo, hamqth, kiwi, logbook, pskr, rig, satellites, waterfall  # noqa: E402
from station import USER_AGENT  # noqa: E402

VERSION = (APP_DIR / "VERSION").read_text().strip() if (APP_DIR / "VERSION").exists() else "dev"
STATIC = APP_DIR / "static"
CTY_URL = "https://www.country-files.com/cty/cty.dat"
CALL_RE = re.compile(r"^[A-Z0-9]+(?:/[A-Z0-9]+)*$")
CANADIAN = re.compile(r"^(?:V[A-GOXY]|C[F-KYZ]|X[J-O])[0-9]")  # Canada's prefixes, if the country file is missing
CACHE_SECONDS = 30 * 86400
TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".ttf": "font/ttf",
    ".woff2": "font/woff2", ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json; charset=utf-8",
}

CFG = config.load()
DATA = config.DATA_DIR
DATA.mkdir(parents=True, exist_ok=True)
LOG = logbook.Logbook(DATA / "logbook.sqlite3")
CTY = cty.CountryFile(DATA / "cty.dat")
HQ = hamqth.HamQTH(CFG.hamqth_user, CFG.hamqth_pass) if CFG.hamqth_user and CFG.hamqth_pass else None
HOSTS = {"127.0.0.1:%d" % CFG.port, "localhost:%d" % CFG.port}
BOOT = str(int(time.time()))  # lets the display notice a restart and reload itself


def system_boot():
    """When the computer was switched on (seconds since 1970), for the welcome banner."""
    try:
        with open("/proc/stat", encoding="ascii") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return int(BOOT)


SYSTEM_BOOT = int(os.environ.get("STATION_SYSTEM_BOOT") or system_boot())   # the setting is for tests
CHECKLIST = checklist.Checklist(config.DATA_DIR / "checklist.json")
FAVS = favourites.Favourites(config.STATION_DIR, say=lambda message: print(message, flush=True), callsign=CFG.callsign)
# Keeps the waterfall's scale (Gqrx, on the computer's own screen) on the radio's frequency.
SCALE = waterfall.Scale(port=CFG.waterfall_port, if_hz=CFG.waterfall_if_hz,
                        enabled=CFG.waterfall_follow and CFG.radio_model >= 2)

ALERTS = alerts.Alerts(DATA, CFG.weather_region, space=CFG.space_weather,
                       space_url=CFG.alerts_space_url, weather_url=CFG.alerts_weather_url)
CONTINENTS = ["NA", "SA", "EU", "AF", "AS", "OC"]

# Voice segments in kHz (IARU Region 2, from VE6LDM's band sheet). Spots outside these are CW or digital.
VOICE_KHZ = [(3600, 4000), (7125, 7300), (14150, 14350), (18110, 18168), (21200, 21450),
             (24930, 24990), (28300, 29700), (50100, 54000)]
NOT_VOICE = re.compile(r"\b(FT8|FT4|CW|RTTY|PSK\d*|JT65|JS8|SSTV|BEACON|BCN)\b", re.I)
VOICE_WORD = re.compile(r"\b(SSB|USB|LSB|PHONE)\b", re.I)
ORIGINS = {"http://" + host for host in HOSTS}


def say(message):
    sys.stdout.write("%s\n" % message)   # one write, so lines from different threads don't run together
    sys.stdout.flush()


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def valid_call(call):
    return 3 <= len(call) <= 15 and bool(CALL_RE.match(call))


def home_call(call):
    """The station's own callsign within a portable call: KH6/W1AW -> W1AW."""
    parts = [p for p in call.split("/") if p]
    return max(parts, key=len) if parts else call


def home_zones():
    """The station's own CQ and ITU zones: from config.ini, or else from the callsign's country."""
    entity = CTY.lookup(CFG.callsign) or {}
    return CFG.cq_zone or entity.get("cq"), CFG.itu_zone or entity.get("itu")


def is_canadian(call):
    entity = CTY.lookup(call)
    return entity.get("name") == "Canada" if entity else bool(CANADIAN.match(call))


def callbook_record(call):
    """HamQTH's record for a call: {} if HamQTH has none, None if it can't be reached.

    Answers are kept for 30 days, and older answers are still used while offline.
    """
    cached = LOG.cache_get(call)
    if cached and time.time() - cached[1] < CACHE_SECONDS:
        return cached[0]
    if HQ is None:
        return cached[0] if cached else None
    try:
        record = HQ.lookup(call)
    except Exception as exc:
        say("HamQTH look-up for %s failed: %s" % (call, exc))
        return cached[0] if cached else None
    LOG.cache_put(call, record)
    return record


def lookup(raw):
    call = (raw or "").strip().upper()
    if not valid_call(call):
        return {"found": False, "call": call,
                "message": "That doesn't look like a callsign. Check it and try again."}
    if home_call(call) == CFG.callsign:
        own = is_canadian(CFG.callsign)
        return {"found": True, "self": True, "call": CFG.callsign, "full_name": CFG.full_name,
                "place": CFG.place, "grid": CFG.grid, "cq": home_zones()[0], "itu": home_zones()[1],
                "licence": CFG.licence, "location_set": CFG.location_set, "note": CFG.note,
                "lookups": FAVS.lookups_for(own)}

    entity = CTY.lookup(call) or {}
    online = callbook_record(home_call(call)) or {}
    canadian = CANADA.lookup(home_call(call)) if entity.get("name") == "Canada" else None
    missing = {"found": False, "call": call,
               "message": "No record found for %s. Check the callsign, or log the contact by hand." % call}
    if not entity and not online:
        return missing

    lat, lon = _num(online.get("latitude")), _num(online.get("longitude"))
    precise = lat is not None and lon is not None and (lat, lon) != (0.0, 0.0)
    grid = (online.get("grid") or "").strip()
    if not precise and geo.valid_grid(grid):
        lat, lon = geo.grid_to_latlon(grid)
        precise = True
    if not precise and canadian and canadian["lat"] is not None:  # their town, from Canada's licence list
        lat, lon = canadian["lat"], canadian["lon"]
        precise = True
    if not precise:
        lat, lon = entity.get("lat"), entity.get("lon")
    if lat is None or lon is None:
        return missing

    heading = geo.bearing(CFG.lat, CFG.lon, lat, lon)
    return {
        "found": True, "self": False, "call": call,
        "name": online.get("adr_name") or online.get("nick") or (canadian or {}).get("name") or "",
        "qth": online.get("qth") or online.get("adr_city") or (canadian or {}).get("city") or "",
        "country": entity.get("name") or online.get("country") or "",
        "region": canadian["province"] if canadian else ("" if precise else entity.get("region", "")),
        "grid": geo.tidy_grid(grid) if geo.valid_grid(grid) else "",
        "cq": _int(online.get("cq")) or entity.get("cq"),
        "itu": _int(online.get("itu")) or entity.get("itu"),
        "lat": round(lat, 4), "lon": round(lon, 4), "precise": precise,
        "timezone": geo.timezone_for(lat, lon, entity.get("utc")),
        "bearing": round(heading, 1), "long_path": round((heading + 180) % 360, 1),
        "distance_km": round(geo.distance_km(CFG.lat, CFG.lon, lat, lon)),
        "history": LOG.history(call),
        "lookups": FAVS.lookups_for(entity.get("name") == "Canada"),
    }


def is_voice(spot):
    if NOT_VOICE.search(spot["comment"]):
        return False
    if VOICE_WORD.search(spot["comment"]):
        return True
    return any(low <= spot["freq_khz"] <= high for low, high in VOICE_KHZ)


def place_of(call):
    """Country, region and map position for a callsign (Canadian hams in their own towns)."""
    entity = CTY.lookup(call) or {}
    place = {"country": entity.get("name", ""), "region": entity.get("region", ""),
             "lat": entity.get("lat"), "lon": entity.get("lon"), "cq": entity.get("cq"),
             "continent": entity.get("continent", "")}
    if place["country"] == "Canada":
        canadian = CANADA.lookup(home_call(call))
        if canadian and canadian["lat"] is not None:
            place.update(region=canadian["city"] or canadian["province"], lat=canadian["lat"], lon=canadian["lon"])
    return place


def iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


_WORKED = {"at": 0.0, "data": None}


def worked_now():
    """What's in the log (countries and zones, with bands), re-read at most every 15 seconds."""
    if _WORKED["data"] is None or time.time() - _WORKED["at"] > 15:
        _WORKED["data"], _WORKED["at"] = LOG.worked(), time.time()
    return _WORKED["data"]


def new_for_log(item, worked):
    """'country' if the spot's country isn't in the log, 'zone' for a new CQ zone, 'band' for a country
    worked before but not on this band, otherwise None."""
    country = item.get("country")
    if country and country not in worked["countries"]:
        return "country"
    if item.get("cq") and item["cq"] not in worked["zones"]:
        return "zone"
    if country and item.get("band") and item["band"] not in worked["countries"][country]["bands"]:
        return "band"
    return None


def spots_json(band, voice_only, minutes=30):
    if CLUSTER is None:   # no callsign yet (a node needs one to sign in), or no node set
        return {"status": "off" if CFG.callsign_set else "no callsign", "spots": [], "counts": {}}
    now = datetime.now(timezone.utc)
    counts, out = {}, []
    worked = worked_now()
    for spot in CLUSTER.spots(minutes):
        if voice_only and not is_voice(spot):
            continue
        if home_call(spot["call"]) == CFG.callsign:
            continue  # the station's own spots go to "Who heard ..." instead
        counts[spot["band"]] = counts.get(spot["band"], 0) + 1
        if band not in ("all", spot["band"]):
            continue
        item = dict(spot, **place_of(spot["call"]))
        item["time"] = iso(spot["time"])
        item["age_min"] = max(0, int((now - spot["time"]).total_seconds() // 60))
        if item["lat"] is not None:
            item["bearing"] = round(geo.bearing(CFG.lat, CFG.lon, item["lat"], item["lon"]))
        item["new"] = new_for_log(item, worked)
        out.append(item)
    return {"status": CLUSTER.status, "node": CLUSTER.host, "spots": out[:60], "counts": counts}


def heard_json(hours=24):
    since = iso(datetime.now(timezone.utc) - timedelta(hours=hours))
    out = []
    for row in LOG.heard_since(since):
        item = dict(row, **place_of(row["spotter"]))
        if item["lat"] is not None:
            item["distance_km"] = round(geo.distance_km(CFG.lat, CFG.lon, item["lat"], item["lon"]))
        out.append(item)
    return out


def note_spot(spot):
    """Called for every spot from the cluster; keeps the ones of this station."""
    if home_call(spot["call"]) == CFG.callsign and LOG.add_heard(spot):
        say("%s spotted by %s on %.1f kHz" % (CFG.callsign, spot["spotter"], spot["freq_khz"]))


CLUSTER = cluster.Cluster(CFG.cluster_host, CFG.cluster_port, CFG.cluster_login, on_spot=note_spot,
                          backups=cluster.parse_nodes(CFG.cluster_backups) if CFG.cluster_backups else cluster.BACKUPS) \
    if CFG.cluster_host and CFG.callsign_set else None
CONDITIONS = conditions.Conditions(DATA / "conditions.xml", CFG.conditions_url or conditions.URL)
ISS = satellites.Tracker(DATA, CFG.lat, CFG.lon, url=CFG.iss_url or satellites.TLE_URL)
FLEET = satellites.Fleet(DATA, CFG.lat, CFG.lon, ISS, url=CFG.amateur_url or satellites.AMATEUR_URL,
                         amsat_url=CFG.amsat_url or satellites.AMSAT_URL,
                         sats=[s for s in satellites.FM_SATS if s["id"] in CFG.fm_sats])
OPENINGS = pskr.Openings(DATA, CFG.callsign if CFG.callsign_set else "", CFG.grid, CFG.lat, CFG.lon, CTY.lookup,
                         host=CFG.openings_host or pskr.HOST, port=CFG.openings_port, tls=CFG.openings_tls) \
    if CFG.openings else None
KIWIS = kiwi.Kiwis(DATA, CFG.lat, CFG.lon, url=CFG.kiwi_url or kiwi.LIST_URL,
                   fallback=CFG.kiwi_fallback or kiwi.FALLBACK_URL)
CANADA = canada.CanadaList(DATA, CFG.callsign, ised_url=CFG.canada_ised_url or canada.ISED_URL,
                           geonames_url=CFG.canada_geonames_url or canada.GEONAMES_URL)


class RadioWatch:
    """Keeps an eye on the radio link.

    rigctld reconnects by itself when the radio is switched back on, so restarting it is only a
    safety net: straight away if the radio's USB cable shows up again after being unplugged, once
    after 5 minutes without an answer, then at most once an hour while the radio stays quiet
    (for example, switched off for the night). It also remembers the power used while transmitting,
    for the logbook.
    """
    QUIET = int(os.environ.get("STATION_RADIO_QUIET", "300"))      # seconds; the environment settings are for tests
    REPEAT = int(os.environ.get("STATION_RADIO_REPEAT", "3600"))

    def __init__(self, device, enabled, command, name="the radio"):
        self.device, self.enabled, self.command, self.name = device, enabled, command, name
        self.lock = threading.Lock()
        self.quiet_since = None
        self.last_restart = 0.0
        self.cable = self._cable()
        self.power = []   # (time, measured watts, setting in watts) while transmitting

    def _cable(self):
        """Something that changes when the cable is unplugged and plugged in again (or None if it's out)."""
        if not self.device:
            return None
        try:
            link, node = os.lstat(self.device), os.stat(self.device)
        except OSError:
            return None
        return (link.st_ino, int(link.st_ctime), node.st_ino, node.st_rdev)

    def seen(self, ok):
        if not self.enabled:
            return
        now, cable, why = time.time(), self._cable(), None
        with self.lock:
            if ok:
                self.quiet_since, self.cable = None, cable
                return
            if self.quiet_since is None:
                self.quiet_since = now
            if cable is not None and cable != self.cable:
                why = "%s's USB cable was plugged in again" % self.name
            elif now - self.quiet_since >= self.QUIET and now - self.last_restart >= self.REPEAT:
                why = "%s hadn't answered for %d minutes" % (self.name, (now - self.quiet_since) // 60)
            if cable is not None:
                self.cable = cable
            if why:
                self.last_restart = now
        if why:
            say("Restarting the radio link: " + why)
            try:
                subprocess.Popen(shlex.split(self.command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                say("Couldn't restart the radio link: %s" % exc)

    def reason(self, rig_reason):
        if self.device and not os.path.exists(self.device):
            return "cable"
        return rig_reason or "quiet"

    def note_power(self, meters):
        if meters.get("power_w") is None and meters.get("power_set_w") is None:
            return
        now = time.time()
        with self.lock:
            self.power = [p for p in self.power if now - p[0] < 900] + [(now, meters.get("power_w"), meters.get("power_set_w"))]

    def recent_power(self):
        """The power used in the last 15 minutes of transmitting: the knob's setting, or the highest reading."""
        now = time.time()
        with self.lock:
            recent = [p for p in self.power if now - p[0] < 900]
        settings = [p[2] for p in recent if p[2]]
        if settings:
            return settings[-1]
        readings = [p[1] for p in recent if p[1]]
        return round(max(readings)) if readings else None


class RadioUnit:
    """One radio: its link (rigctld), read in the background (twice a second while it's the one on
    the display, every 1.5 seconds otherwise), so a radio that's switched off (slow to time out)
    never holds up the display or the other radio."""
    ON_AFTER = int(os.environ.get("STATION_RADIO_ON_AFTER", "30"))   # seconds quiet before answering = switched on

    def __init__(self, key, name, model, port, device, max_watts, bands, restart, primary=False):
        self.key, self.name, self.model, self.bands, self.primary = key, name, model, bands, primary
        self.rig = rig.Rig(port=port, model=model, max_watts=max_watts)
        self.watch = RadioWatch(device, enabled=model >= 2, command=restart, name="the " + name)
        self.status = {"ok": False, "reason": "link"} if model else {"ok": False, "set_up": False}
        self.changed = 0.0      # when it was last tuned, changed mode, started or stopped sending, or was switched on
        self._last = None       # the last good reading's (frequency, mode, transmitting)
        self._off_since = None  # when it stopped answering
        self.shown = lambda: True   # Radios replaces this: is it the radio on the display?
        self.lock = threading.Lock()

    @property
    def set_up(self):
        return self.model != 0

    def poll(self):
        status = self.rig.status()
        self.watch.seen(status.get("ok"))
        if status.get("ok"):
            if self.primary:
                SCALE.note(status.get("freq_hz"))    # the waterfall belongs to the first radio
            if status.get("tx"):                     # read-only meter readings while transmitting
                meters = self.rig.meters()
                status.update(meters)
                self.watch.note_power(meters)
            key = (status.get("freq_hz"), status.get("mode"), bool(status.get("tx")))
        else:
            status["reason"] = self.watch.reason(status.get("reason"))
            key = None
        now = time.time()
        with self.lock:
            if key is not None:
                if self._last is not None and key != self._last:
                    self.changed = now         # tuned, changed mode, or started or stopped transmitting
                elif self._off_since is not None and now - self._off_since > self.ON_AFTER:
                    self.changed = now         # switched on (a short blip in the link doesn't count)
                self._last, self._off_since = key, None
            elif self._off_since is None:
                self._off_since = now
            self.status = status

    def run(self):
        while True:
            started = time.time()
            try:
                self.poll()
            except Exception as exc:      # never let one bad reading stop the radio's reader
                say("%s reading failed: %s" % (self.name, exc))
            every = 0.5 if self.shown() else 1.5
            time.sleep(max(0.25, every - (time.time() - started)))

    def start(self):
        if self.set_up:
            threading.Thread(target=self.run, daemon=True, name="radio-" + self.key).start()

    def reading(self):
        with self.lock:
            return dict(self.status, name="Practice" if self.model == 1 else self.name, radio=self.key)


class Radios:
    """The radio and (once it's set up) the second radio. The display follows the one in use: the one
    transmitting, else the one tuned most recently (or picked by clicking its name), else whichever is on."""

    def __init__(self, units):
        self.all = units                                   # as in the settings, set up or not
        self.units = [u for u in units if u.primary or u.set_up]
        self.picked, self.picked_at = None, 0.0
        for u in self.units:
            u.shown = (lambda unit: lambda: self.active() is unit)(u)

    def start(self):
        for u in self.units:
            u.start()

    def unit(self, key):
        return next((u for u in self.units if u.key == key), None)

    def active(self):
        on = [u for u in self.units if u.status.get("ok")]
        if not on:
            return self.units[0]
        sending = [u for u in on if u.status.get("tx")]
        if sending:
            return sending[0]
        def score(u):
            return max(u.changed, self.picked_at if u is self.picked else 0.0)
        return max(on, key=score)

    def pick(self, key):
        u = self.unit(key)
        if u is None or not u.status.get("ok"):
            return False
        self.picked, self.picked_at = u, time.time()
        return True

    def snapshot(self):
        """What /api/rig returns: the radio in use, and a short line for each other radio."""
        active = self.active()
        out = active.reading()
        out["others"] = [{k: v for k, v in u.reading().items()
                          if k in ("radio", "name", "ok", "freq_hz", "mode", "tx", "set_up", "reason")}
                         for u in self.units if u is not active]
        return out

    def tune(self, freq_hz, mode):
        """Tunes the radio in use; if it has no antenna for that band and the other radio does (and is on), that one."""
        band = logbook.band_for(int(freq_hz or 0))
        active = self.active()
        target, note = active, None
        if band and band not in active.bands:
            other = next((u for u in self.units if u is not active and u.status.get("ok") and band in u.bands), None)
            if other:
                target, note = other, "the %s has no %s antenna" % (active.name, band.replace("m", " m"))
            else:
                return {"ok": False, "radio": active.name, "no_antenna": band}
        ok = target.rig.tune(freq_hz, mode)
        if ok:
            self.pick(target.key)
        return {"ok": ok, "radio": target.name, "note": note}


RADIOS = Radios([
    RadioUnit("radio", CFG.radio_name, CFG.radio_model, CFG.rig_port, CFG.radio_device, CFG.radio_max_watts,
              CFG.radio_bands, os.environ.get("STATION_RESTART_RADIO", "systemctl --user restart station-rigctld"),
              primary=True),
    RadioUnit("radio2", CFG.radio2_name, CFG.radio2_model, CFG.radio2_port, CFG.radio2_device, CFG.radio2_max_watts,
              CFG.radio2_bands, os.environ.get("STATION_RESTART_RADIO2", "systemctl --user restart station-rigctld2")),
])


class DisplayWatch:
    """Keeps an eye on the station display, the full-screen window on the big monitor.

    station-display.service opens the window again if it's closed. This catches the rarer case
    where the window stays open but the page in it has stopped (Chromium's "Aw, Snap!" page, say):
    the display asks for the radio reading every second, so if it hasn't asked for 5 minutes while
    the service is running, the display is restarted, at most every 15 minutes.
    """
    QUIET = int(os.environ.get("STATION_DISPLAY_QUIET", "300"))      # seconds; the environment settings are for tests
    REPEAT = int(os.environ.get("STATION_DISPLAY_REPEAT", "900"))
    EVERY = int(os.environ.get("STATION_DISPLAY_EVERY", "30"))
    RESTART = os.environ.get("STATION_RESTART_DISPLAY", "systemctl --user restart station-display")
    ACTIVE = os.environ.get("STATION_DISPLAY_ACTIVE", "systemctl --user is-active --quiet station-display")

    def __init__(self):
        self.lock = threading.Lock()
        self.last_seen = None
        self.last_restart = 0.0

    def seen(self):
        with self.lock:
            self.last_seen = time.time()

    def seconds_since(self):
        with self.lock:
            return None if self.last_seen is None else round(time.time() - self.last_seen)

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="display-watch").start()

    def _loop(self):
        while True:
            time.sleep(self.EVERY)
            try:
                self.check()
            except Exception as exc:
                say("Display check failed: %s" % exc)

    def check(self):
        now = time.time()
        with self.lock:
            if self.last_seen is None or now - self.last_seen < self.QUIET or now - self.last_restart < self.REPEAT:
                return False
            quiet = now - self.last_seen
        try:   # closed on purpose ("systemctl --user stop station-display", or keep_open = no)?
            running = subprocess.run(shlex.split(self.ACTIVE), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            running = False
        if not running:
            return False
        with self.lock:
            self.last_restart = now
        say("Restarting the station display: its page hadn't checked in for %d minutes" % (quiet // 60))
        try:
            subprocess.Popen(shlex.split(self.RESTART), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            say("Couldn't restart the station display: %s" % exc)
        return True


SCREEN = DisplayWatch()


def radio_status():
    return RADIOS.snapshot()


def add_contact(body):
    call = str(body.get("call") or "").strip().upper()
    if not valid_call(call):
        raise ValueError("That doesn't look like a callsign.")
    country = str(body.get("country") or "").strip()
    new_country = bool(country) and not LOG.has_country(country)
    zone = _int(body.get("cq"))
    zone = zone if zone and 1 <= zone <= 40 else None
    new_zone = zone is not None and not LOG.has_zone(zone)
    band = str(body.get("band") or "").strip().lower() or None
    if band and band not in logbook.BAND_NAMES:
        raise ValueError("That band isn't one the log knows.")
    # The radio the contact was made on: the one in use if it's on that band, else the other one if it is.
    active = RADIOS.active()
    unit = radio = None
    for u in [active] + [u for u in RADIOS.units if u is not active]:
        reading = u.reading()
        if reading.get("ok") and (band is None or logbook.band_for(reading["freq_hz"]) == band):
            unit, radio = u, reading
            break
    spot_freq = _int(body.get("spot_freq_hz"))
    freq = mode = None
    if radio:
        freq, mode = radio["freq_hz"], radio.get("mode")  # the radio knows exactly where it's tuned
    elif spot_freq and (band is None or logbook.band_for(spot_freq) == band):
        freq, mode = spot_freq, body.get("mode")  # the spot that was clicked
    if freq is None and band:  # a contact on a radio the app doesn't follow, such as a 2 m rig
        mode = "FM" if band in ("2m", "1.25m", "70cm") else ("LSB" if band in ("160m", "80m", "40m") else "USB")
    saved = LOG.add({
        "call": call,
        "freq_hz": freq,
        "band": band,
        "mode": mode,
        "rst_sent": body.get("rst_sent"), "rst_rcvd": body.get("rst_rcvd"),
        "name": body.get("name"), "qth": body.get("qth"), "grid": body.get("grid"),
        "country": body.get("country"), "cq_zone": body.get("cq"), "itu_zone": body.get("itu"),
        "notes": body.get("notes"),
        "distance_km": body.get("distance_km"),
        "tx_pwr": unit.watch.recent_power() if unit else None,
        "my_rig": unit.name if unit else None,
    })
    _WORKED["data"] = None   # the next spot list sees this contact
    stats = LOG.stats()
    saved["celebrate"] = {"new_country": new_country, "countries": stats["countries"], "total": stats["total"],
                          "new_zone": new_zone, "zone": zone, "zones": stats["zones"]}
    return saved


def contact_km(contact, by_name):
    """Distance to a logged contact: saved when it was logged, or from their grid square or country."""
    if contact.get("distance_km"):
        return contact["distance_km"]
    if geo.valid_grid(contact.get("grid") or ""):
        lat, lon = geo.grid_to_latlon(contact["grid"])
        return geo.distance_km(CFG.lat, CFG.lon, lat, lon)
    entity = by_name.get(contact.get("country") or "")
    if entity:
        return geo.distance_km(CFG.lat, CFG.lon, entity["lat"], entity["lon"])
    return None


def stats_json():
    contacts = LOG.all_contacts()
    worked = LOG.worked()
    entities = CTY.entities()
    by_name = {e["name"]: e for e in entities}
    dxcc = [e for e in entities if e["dxcc"]]
    worked_dxcc = [name for name in worked["countries"] if by_name.get(name, {}).get("dxcc")]
    per_continent = {c: {"worked": 0, "total": sum(1 for e in dxcc if e["continent"] == c)} for c in CONTINENTS}
    for name in worked_dxcc:
        cont = by_name[name]["continent"]
        if cont in per_continent:
            per_continent[cont]["worked"] += 1
    bands = []
    for _, _, band in logbook.BANDS:
        rows = [c for c in contacts if c["band"] == band]
        if rows:
            bands.append({"band": band, "contacts": len(rows), "countries": len({c["country"] for c in rows if c["country"]})})
    modes = {}
    for c in contacts:
        mode = c["submode"] if c["mode"] == "SSB" and c["submode"] else (c["mode"] or "Other")
        modes[mode] = modes.get(mode, 0) + 1
    now = datetime.now(timezone.utc)
    months = []
    for back in range(11, -1, -1):
        year, month = now.year, now.month - back
        while month < 1:
            year, month = year - 1, month + 12
        key = "%04d-%02d" % (year, month)
        months.append({"month": key, "contacts": sum(1 for c in contacts if c["time_on"].startswith(key))})
    furthest = None
    for c in contacts:
        km = contact_km(c, by_name)
        if km and (furthest is None or km > furthest["km"]):
            furthest = {"call": c["call"], "country": c["country"] or "", "km": round(km), "time": c["time_on"]}
    firsts = [{"kind": "country", "name": name, "call": w["first_call"], "time": w["first_time"]}
              for name, w in worked["countries"].items()]
    firsts += [{"kind": "zone", "name": "CQ zone %d" % zone, "call": w["first_call"], "time": w["first_time"]}
               for zone, w in worked["zones"].items()]
    firsts.sort(key=lambda f: f["time"], reverse=True)
    return {
        "total": len(contacts),
        "this_month": sum(1 for c in contacts if c["time_on"].startswith(now.strftime("%Y-%m"))),
        "first": contacts[0]["time_on"] if contacts else None,
        "countries": {"worked": len(worked_dxcc), "total": len(dxcc), "by_continent": per_continent},
        "zones": {"worked": len(worked["zones"]), "total": 40},
        "continents": {"worked": [c for c in CONTINENTS if per_continent[c]["worked"]], "total": len(CONTINENTS)},
        "bands": bands,
        "modes": [{"mode": m, "contacts": n} for m, n in sorted(modes.items(), key=lambda kv: -kv[1])],
        "months": months,
        "furthest": furthest,
        "new_ones": firsts[:6],
        "country_file": CTY.loaded,
    }


def countries_json():
    """Every DXCC country from the country file, with what's in the log for each."""
    worked = LOG.worked()["countries"]
    out = []
    for e in CTY.entities():
        if not e["dxcc"]:
            continue
        w = worked.get(e["name"])
        out.append({"name": e["name"], "prefix": e["prefix"], "continent": e["continent"], "cq": e["cq"],
                    "lat": e["lat"], "lon": e["lon"],
                    "worked": None if not w else {k: w[k] for k in ("count", "bands", "last_call", "last_time", "first_time")}})
    out.sort(key=lambda c: (CONTINENTS.index(c["continent"]) if c["continent"] in CONTINENTS else 9, c["name"]))
    return {"countries": out, "home": (CTY.lookup(CFG.callsign) or {}).get("name", "")}


def kiwi_json(query):
    """Receivers to listen to the station on, for the radio's frequency (the display sends it)."""
    freq = _int(query.get("freq", [""])[0])
    band = logbook.band_for(freq) if freq else None
    if not band or freq > 30_000_000:
        return {"ok": False, "error": "band"}
    mode = re.sub(r"[^A-Z]", "", (query.get("mode", ["USB"])[0] or "USB").upper())[:8] or "USB"
    bearing = _num(query.get("bearing", [""])[0])
    picked = KIWIS.pick(freq, band, bearing if bearing is not None and 0 <= bearing < 360 else None)
    return {"ok": True, "band": band, "freq_hz": freq, "mode": mode, "receivers": picked,
            "listed": len(KIWIS.rows), "from": "rx.linkfanel.net" if "linkfanel" in KIWIS.source else "kiwisdr.com"}


def sats_json():
    passes = FLEET.passes(count=8)
    info = {}
    for s in [satellites.ISS_FM] + FLEET.sats:
        status = FLEET.status.get(s["id"]) or {}
        info[s["id"]] = {"down": s["down"], "up": s["up"], "tone": s["tone"], "sunlight_only": bool(s.get("sunlight")),
                         "last_heard": status.get("last_heard")}
    return {"passes": passes, "ready": ISS.ready or any(t.ready for t in FLEET.trackers.values()),
            "sats": ["ISS"] + [s["id"] for s in FLEET.sats],
            "sunlight_only": [s["id"] for s in FLEET.sats if s.get("sunlight")], "info": info}


def open_link(body):
    """Opens one of the favourites, or a look-up for a callsign, in the computer's web browser."""
    call = str(body.get("call") or "").strip().upper()
    if call and not valid_call(call):
        raise ValueError("That doesn't look like a callsign.")
    link_id = str(body.get("id") or "")
    if link_id.startswith("kiwi:"):   # a receiver from the KiwiSDR list, tuned to the radio
        freq = _int(body.get("freq_hz"))
        if not freq or not 100_000 <= freq <= 30_000_000:
            raise ValueError("That frequency isn't one a KiwiSDR covers.")
        found = KIWIS.link(link_id[len("kiwi:"):], freq, str(body.get("mode") or "USB"))
        if not found:
            raise ValueError("That receiver isn't in the list any more.")
        name, url = found
        return {"ok": favourites.open_in_browser(url), "name": name}
    if link_id == "checklist":   # the tester checklist, a page of this app
        return {"ok": favourites.open_in_browser("http://127.0.0.1:%d/checklist" % CFG.port), "name": "The tester checklist"}
    if link_id.startswith("alert:"):
        found = ALERTS.url_for(link_id[len("alert:"):])
        if not found:
            raise ValueError("That alert has ended.")
        name, url = found
        return {"ok": favourites.open_in_browser(url), "name": name}
    base = home_call(call) if call else None
    canadian = bool(base) and is_canadian(base)
    found = FAVS.address(link_id, base, canadian)
    if not found:
        raise ValueError("That isn't one of the favourites.")
    name, url = found
    return {"ok": favourites.open_in_browser(url), "name": name}


def settings():
    return {"callsign": CFG.callsign, "operator": CFG.operator, "place": CFG.place,
            "timezone": CFG.timezone, "lat": CFG.lat, "lon": CFG.lon, "grid": CFG.grid,
            "location_set": CFG.location_set, "show_zones": CFG.show_zones, "version": VERSION,
            "cq_zone": home_zones()[0], "itu_zone": home_zones()[1], "globe_on_lookup": CFG.globe_on_lookup,
            "callsign_set": CFG.callsign_set,
            "voice_only": CFG.voice_only, "cluster": CFG.cluster_host or "",
            "licensed": CFG.licensed, "birthday": CFG.birthday,
            "callbook": HQ is not None, "country_file": CTY.loaded, "canada": CANADA.info(),
            # the welcome banner: shown once after the computer starts (the display keeps which start it last showed)
            "welcome": CFG.welcome, "welcome_text": CFG.welcome_text, "note": CFG.note,
            "system_boot": SYSTEM_BOOT, "uptime_s": max(0, round(time.time() - SYSTEM_BOOT)),
            "checklist": CFG.checklist, "helper": CFG.helper,
            # the HF bands the radios have antennas for: the spot list's buttons, band openings, listening
            "bands": [b for b in config.BAND_ORDER if b in set(CFG.radio_bands) | set(CFG.radio2_bands if CFG.radio2_model else [])],
            "radios": [{"radio": u.key, "name": u.name, "bands": u.bands, "set_up": u.set_up} for u in RADIOS.all]}


class Handler(BaseHTTPRequestHandler):
    server_version = "ham-radio-hud/" + VERSION

    def log_message(self, fmt, *args):
        pass  # keep the journal quiet; problems are reported with say()

    def _send(self, status, body, ctype, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _static(self, rel):
        base = DATA / "fonts" if rel.startswith("fonts/") else STATIC
        name = rel[len("fonts/"):] if rel.startswith("fonts/") else rel
        try:
            path = (base / name).resolve()
            path.relative_to(base.resolve())
        except (ValueError, OSError):
            return self._json({"error": "not found"}, 404)
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        cache = "max-age=604800" if path.suffix in (".ttf", ".woff2") else "no-cache"
        self._send(200, path.read_bytes(), TYPES.get(path.suffix.lower(), "application/octet-stream"),
                   {"Cache-Control": cache})

    def _host_ok(self):
        return (self.headers.get("Host") or "").lower() in HOSTS

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden"}, 403)
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path == "/checklist":
                return self._static("checklist.html")
            if path == "/api/checklist":
                return self._json(CHECKLIST.snapshot(VERSION, checklist.words(CFG)))
            if path.startswith("/static/"):
                return self._static(urllib.parse.unquote(path[len("/static/"):]))
            if path == "/api/health":
                return self._json({"ok": True, "version": VERSION, "boot": BOOT, "display_seen_s": SCREEN.seconds_since()})
            if path == "/api/spots":
                return self._json(spots_json(query.get("band", ["all"])[0],
                                             query.get("voice", ["1"])[0] != "0"))
            if path == "/api/heard":
                return self._json(heard_json())
            if path == "/api/iss":
                return self._json(ISS.snapshot())
            if path == "/api/sats":
                return self._json(sats_json())
            if path == "/api/sat":
                detail = FLEET.detail(query.get("id", [""])[0].upper())
                return self._json(detail) if detail else self._json({"error": "unknown satellite"}, 404)
            if path == "/api/openings":
                return self._json(OPENINGS.summary() if OPENINGS else {"on": False, "bands": [], "own": []})
            if path == "/api/kiwi":
                return self._json(kiwi_json(query))
            if path == "/api/conditions":
                return self._json(CONDITIONS.data or {"ok": False})
            if path == "/api/config":
                return self._json(settings())
            if path == "/api/lookup":
                return self._json(lookup(query.get("call", [""])[0]))
            if path == "/api/rig":
                if query.get("from") == ["display"]:
                    SCREEN.seen()   # the station display's page is alive
                return self._json(radio_status())
            if path == "/api/log/recent":
                return self._json(LOG.recent(_int(query.get("limit", ["12"])[0]) or 12))
            if path == "/api/log/stats":
                return self._json(LOG.stats())
            if path == "/api/log/zones":
                return self._json({"zones": LOG.zones(), "home": home_zones()[0]})
            if path == "/api/stats":
                return self._json(stats_json())
            if path == "/api/countries":
                return self._json(countries_json())
            if path == "/api/alerts":
                return self._json(ALERTS.listing())
            if path == "/api/contests":
                return self._json(contests.upcoming())
            if path == "/api/favourites":
                return self._json(FAVS.listing())
            if path == "/api/log/export.adi":
                body = LOG.export_adif(CFG.callsign, VERSION).encode("ascii", "replace")
                name = "%s-log-%s.adi" % (CFG.callsign.lower(), time.strftime("%Y%m%d"))
                return self._send(200, body, "text/plain; charset=us-ascii",
                                  {"Content-Disposition": 'attachment; filename="%s"' % name,
                                   "Cache-Control": "no-store"})
            return self._json({"error": "not found"}, 404)
        except Exception:
            say("Error answering GET %s:\n%s" % (path, traceback.format_exc()))
            return self._json({"error": "internal"}, 500)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "forbidden"}, 403)
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in ORIGINS:
            return self._json({"error": "forbidden"}, 403)
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json({"error": "send JSON"}, 415)
        length = _int(self.headers.get("Content-Length")) or 0
        if length > 65536:
            return self._json({"error": "too large"}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad JSON"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "bad JSON"}, 400)
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/log":
                return self._json(add_contact(body))
            if path == "/api/log/delete":
                LOG.delete(_int(body.get("id")) or 0)
                return self._json({"ok": True})
            if path == "/api/rig/tune":
                return self._json(RADIOS.tune(body.get("freq_hz"), body.get("mode") or None))
            if path == "/api/rig/pick":
                return self._json({"ok": RADIOS.pick(str(body.get("radio") or ""))})
            if path == "/api/open":
                return self._json(open_link(body))
            if path == "/api/checklist":
                return self._json({"ok": True, "response": CHECKLIST.answer(body), "summary": CHECKLIST.summary()})
            return self._json({"error": "not found"}, 404)
        except (ValueError, TypeError) as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception:
            say("Error answering POST %s:\n%s" % (path, traceback.format_exc()))
            return self._json({"error": "internal"}, 500)


def keep_country_file_fresh():
    """Downloads cty.dat when it is missing or more than two weeks old."""
    path = DATA / "cty.dat"
    while True:
        try:
            if not path.exists() or time.time() - path.stat().st_mtime > 14 * 86400:
                request = urllib.request.Request(CTY_URL, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=60) as response:
                    content = response.read()
                if len(content) > 20000 and b"Canada" in content:
                    part = path.with_suffix(".part")
                    part.write_bytes(content)
                    part.replace(path)
                    CTY.reload()
                    say("Country file updated")
            time.sleep(6 * 3600)
        except Exception as exc:
            say("Country file update failed (will retry in an hour): %s" % exc)
            time.sleep(3600)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", CFG.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=keep_country_file_fresh, daemon=True).start()
    CONDITIONS.start()
    ALERTS.start()
    SCREEN.start()
    ISS.start()
    FLEET.start()
    RADIOS.start()
    if OPENINGS:
        OPENINGS.start()
    CANADA.start()
    SCALE.start()
    if CLUSTER:
        CLUSTER.start()
    say("Station app %s for %s on http://127.0.0.1:%d (country file %s, callbook %s, location %s)" % (
        VERSION, CFG.callsign if CFG.callsign_set else "a station with no callsign set yet", CFG.port,
        "loaded" if CTY.loaded else "missing", "HamQTH" if HQ else "off",
        CFG.grid if CFG.location_set else "placeholder"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
