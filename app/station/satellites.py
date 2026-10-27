"""Where the ISS and the FM satellites are, when they next pass over the station, and how to tune them.

Orbit data (a "TLE") comes from CelesTrak: the ISS every six hours, the amateur
satellites every twelve. The last copies are kept on disk so tracking carries on
without internet for days. Positions come from the standard SGP4 model (the
python3-sgp4 package). If that isn't installed, a simpler built-in model is used
instead; it puts pass times within about a minute, which is plenty for listening.

For the FM satellites it also works out the Doppler shift (where to tune as the
satellite comes and goes) and fetches AMSAT's recent "heard / not heard" reports.
"""
import json
import math
import sys
import threading
import time
import urllib.parse
import urllib.request

from . import USER_AGENT
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from sgp4.api import Satrec
except ImportError:  # the built-in model takes over
    Satrec = None

TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
AMATEUR_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle"
ONE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=TLE"
AMSAT_URL = "https://www.amsat.org/status/api/v1/sat_info.php?name={name}&hours=96"
LIGHT_KMS = 299792.458
UTC = timezone.utc
MU = 398600.4418            # km^3/s^2
RE = 6378.137               # km, equator
J2 = 1.08262668e-3
FLAT = 1 / 298.257223563
E2 = FLAT * (2 - FLAT)
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
MIN_ELEVATION = 10          # passes lower than this are hard to hear


def say(message):
    sys.stdout.write("%s\n" % message)   # one write, so lines from different threads don't run together
    sys.stdout.flush()


def compass(azimuth):
    return DIRS[int(((azimuth + 22.5) % 360) // 45)]


def julian(t):
    jd = t.timestamp() / 86400 + 2440587.5
    whole = math.floor(jd)
    return whole, jd - whole


def gmst(jd_whole, fraction):
    """Greenwich sidereal angle in radians (IAU 1982, as SGP4 uses)."""
    t = (jd_whole - 2451545.0 + fraction) / 36525.0
    seconds = -6.2e-6 * t ** 3 + 0.093104 * t ** 2 + (876600.0 * 3600 + 8640184.812866) * t + 67310.54841
    return math.radians(seconds / 240.0) % (2 * math.pi)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class SimpleOrbit:
    """Kepler orbit with the main effects of Earth's bulge (J2) and air drag.

    Same calling style as sgp4's Satrec: sgp4(jd, fraction) -> (error, r, v),
    positions in km in the same frame SGP4 uses.
    """

    def __init__(self, line1, line2):
        year = int(line1[18:20])
        year += 2000 if year < 57 else 1900
        epoch = datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=float(line1[20:32]) - 1)
        self.epoch_whole, self.epoch_fraction = julian(epoch)
        self.ndot = float(line1[33:43])                        # rev/day^2 (half the rate of change)
        self.inc = math.radians(float(line2[8:16]))
        self.raan = math.radians(float(line2[17:25]))
        self.ecc = float("0." + line2[26:33].strip())
        self.argp = math.radians(float(line2[34:42]))
        self.mean_anomaly = math.radians(float(line2[43:51]))
        self.n0 = float(line2[52:63])                          # rev/day

    def sgp4(self, jd_whole, fraction):
        days = (jd_whole - self.epoch_whole) + (fraction - self.epoch_fraction)
        seconds = days * 86400
        n = (self.n0 + 2 * self.ndot * days) * 2 * math.pi / 86400   # rad/s, rising slowly with drag
        a = (MU / n ** 2) ** (1 / 3)
        e, i = self.ecc, self.inc
        k = 1.5 * J2 * (RE / (a * (1 - e * e))) ** 2
        raan = self.raan - k * n * math.cos(i) * seconds
        argp = self.argp + 0.5 * k * n * (5 * math.cos(i) ** 2 - 1) * seconds
        m = (self.mean_anomaly + 2 * math.pi * (self.n0 * days + self.ndot * days * days)
             + 0.5 * k * n * math.sqrt(1 - e * e) * (3 * math.cos(i) ** 2 - 1) * seconds)
        big_e = m
        for _ in range(8):
            big_e -= (big_e - e * math.sin(big_e) - m) / (1 - e * math.cos(big_e))
        nu = 2 * math.atan2(math.sqrt(1 + e) * math.sin(big_e / 2), math.sqrt(1 - e) * math.cos(big_e / 2))
        r = a * (1 - e * math.cos(big_e))
        u = argp + nu
        x = r * (math.cos(raan) * math.cos(u) - math.sin(raan) * math.sin(u) * math.cos(i))
        y = r * (math.sin(raan) * math.cos(u) + math.cos(raan) * math.sin(u) * math.cos(i))
        z = r * math.sin(u) * math.sin(i)
        speed = math.sqrt(MU * (2 / r - 1 / a))
        return 0, (x, y, z), (speed, 0.0, 0.0)


class Tracker:
    """One satellite, by its catalog number (the ISS is 25544)."""

    def __init__(self, data_dir, lat, lon, height_km=0.85, url=TLE_URL, norad=25544, tle_file="iss.tle", name="ISS"):
        self.tle_path = Path(data_dir) / tle_file
        self.url = url
        self.norad, self.name = norad, name
        self.lat, self.lon = math.radians(lat), math.radians(lon)
        n = RE / math.sqrt(1 - E2 * math.sin(self.lat) ** 2)
        self.home = ((n + height_km) * math.cos(self.lat) * math.cos(self.lon),
                     (n + height_km) * math.cos(self.lat) * math.sin(self.lon),
                     (n * (1 - E2) + height_km) * math.sin(self.lat))
        self.sat = None
        self.model = "sgp4" if Satrec else "simple"
        self.tle_epoch = None
        self.passes = []
        self.passes_ready = False
        self._lock = threading.Lock()
        self._load_tle()

    # ---------- orbit data ----------
    def _load_tle(self, text=None):
        try:
            text = text if text is not None else self.tle_path.read_text()
        except OSError:
            return False
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        line1 = next((ln for ln in lines if ln.startswith("1 %05d" % self.norad)), None)
        line2 = next((ln for ln in lines if ln.startswith("2 %05d" % self.norad)), None)
        if not (line1 and line2):
            return False
        try:
            sat = Satrec.twoline2rv(line1, line2) if Satrec else SimpleOrbit(line1, line2)
            year = int(line1[18:20])
            epoch = datetime(year + (2000 if year < 57 else 1900), 1, 1, tzinfo=UTC) \
                + timedelta(days=float(line1[20:32]) - 1)
        except Exception as exc:
            say("%s orbit data couldn't be read: %s" % (self.name, exc))
            return False
        with self._lock:
            self.sat, self.tle_epoch = sat, epoch
        return True

    @property
    def ready(self):
        return self.sat is not None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="iss").start()

    def _run(self):
        last_fetch = 0.0
        while True:
            if time.time() - last_fetch > 6 * 3600 or not self.ready:
                try:
                    request = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(request, timeout=30) as response:
                        text = response.read().decode("ascii", "replace")
                    if self._load_tle(text):
                        self.tle_path.write_text(text)
                        last_fetch = time.time()
                except Exception as exc:
                    say("ISS orbit data update failed (will retry): %s" % exc)
                    last_fetch = time.time() - 6 * 3600 + 900   # try again in 15 minutes
            if self.ready:
                try:
                    found = self.find_passes(datetime.now(UTC) - timedelta(minutes=15), hours=48)
                    with self._lock:
                        self.passes = found
                        self.passes_ready = True
                except Exception as exc:
                    say("ISS pass prediction failed: %s" % exc)
            time.sleep(600)

    # ---------- positions ----------
    def _eci(self, t):
        whole, fraction = julian(t)
        error, r, v = self.sat.sgp4(whole, fraction)
        if error:
            raise ValueError("SGP4 error %d" % error)
        return r, v, gmst(whole, fraction)

    def _ecef(self, t):
        r, v, theta = self._eci(t)
        c, s = math.cos(theta), math.sin(theta)
        return (c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2]), r, v

    def subpoint(self, t):
        (x, y, z), _, _ = self._ecef(t)
        lon = math.atan2(y, x)
        p = math.hypot(x, y)
        lat = math.atan2(z, p * (1 - E2))
        for _ in range(5):
            n = RE / math.sqrt(1 - E2 * math.sin(lat) ** 2)
            alt = p / math.cos(lat) - n
            lat = math.atan2(z, p * (1 - E2 * n / (n + alt)))
        return math.degrees(lat), math.degrees(lon), alt

    def range_km(self, t):
        (x, y, z), _, _ = self._ecef(t)
        return math.sqrt((x - self.home[0]) ** 2 + (y - self.home[1]) ** 2 + (z - self.home[2]) ** 2)

    def range_rate(self, t):
        """How fast the satellite is moving away from the station, in km/s (negative while it comes closer)."""
        return self.range_km(t + timedelta(seconds=0.5)) - self.range_km(t - timedelta(seconds=0.5))

    def look(self, t):
        """Azimuth and elevation of the satellite as seen from the station, in degrees."""
        (x, y, z), _, _ = self._ecef(t)
        dx, dy, dz = x - self.home[0], y - self.home[1], z - self.home[2]
        sl, cl, sp, cp = math.sin(self.lon), math.cos(self.lon), math.sin(self.lat), math.cos(self.lat)
        east = -sl * dx + cl * dy
        north = -sp * cl * dx - sp * sl * dy + cp * dz
        up = cp * cl * dx + cp * sl * dy + sp * dz
        return (math.degrees(math.atan2(east, north)) + 360) % 360, math.degrees(math.atan2(up, math.hypot(east, north)))

    def _elevation(self, t):
        return self.look(t)[1]

    # ---------- the sun: can the ISS be seen with the eye? ----------
    @staticmethod
    def _sun(t):
        d = t.timestamp() / 86400 + 2440587.5 - 2451545.0
        g = math.radians(357.529 + 0.98560028 * d)
        lon = math.radians(280.459 + 0.98564736 * d + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
        eps = math.radians(23.439 - 0.00000036 * d)
        ra = math.atan2(math.cos(eps) * math.sin(lon), math.cos(lon))
        dec = math.asin(math.sin(eps) * math.sin(lon))
        return ra, dec

    def sunlit(self, t):
        """True if the satellite is in sunlight (not in Earth's shadow)."""
        ra, dec = self._sun(t)
        r, _, _ = self._eci(t)
        s = (math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec))
        along = r[0] * s[0] + r[1] * s[1] + r[2] * s[2]
        across = math.sqrt(max(0.0, r[0] ** 2 + r[1] ** 2 + r[2] ** 2 - along ** 2))
        return along > 0 or across > RE

    def _visible(self, t):
        ra, dec = self._sun(t)
        whole, fraction = julian(t)
        sub_lon = ra - gmst(whole, fraction)
        sun_elevation = math.degrees(math.asin(math.sin(self.lat) * math.sin(dec)
                                               + math.cos(self.lat) * math.cos(dec) * math.cos(self.lon - sub_lon)))
        if sun_elevation > -6:      # sky still too bright
            return False
        r, _, _ = self._eci(t)
        s = (math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec))
        along = r[0] * s[0] + r[1] * s[1] + r[2] * s[2]
        across = math.sqrt(max(0.0, r[0] ** 2 + r[1] ** 2 + r[2] ** 2 - along ** 2))
        return along > 0 or across > RE   # sunlit unless inside Earth's shadow

    # ---------- passes ----------
    def _edge(self, t0, t1, rising):
        for _ in range(12):                 # 30 s -> under 0.01 s
            mid = t0 + (t1 - t0) / 2
            if (self._elevation(mid) > 0) == rising:
                t1 = mid
            else:
                t0 = mid
        return t1 if rising else t0

    def find_passes(self, start, hours=48, step=30):
        passes, t = [], start
        end = start + timedelta(hours=hours)
        dt = timedelta(seconds=step)
        prev = self._elevation(t)
        aos = t if prev > 0 else None
        while t < end:
            t2 = t + dt
            el = self._elevation(t2)
            if prev <= 0 < el:
                aos = self._edge(t, t2, rising=True)
            elif prev > 0 >= el and aos is not None:
                passes.append(self._describe(aos, self._edge(t, t2, rising=False)))
                aos = None
            t, prev = t2, el
        return [p for p in passes if p["max_el"] >= MIN_ELEVATION]

    def _describe(self, aos, los):
        best_t, best_el, visible = aos, -90.0, False
        t = aos
        while t <= los:
            el = self._elevation(t)
            if el > best_el:
                best_t, best_el = t, el
            if not visible and el >= MIN_ELEVATION and self._visible(t):
                visible = True
            t += timedelta(seconds=10)
        aos_az, los_az = self.look(aos)[0], self.look(los)[0]
        return {"sat": self.name, "aos": iso(aos), "los": iso(los), "peak": iso(best_t), "max_el": round(best_el),
                "aos_az": round(aos_az), "los_az": round(los_az), "aos_dir": compass(aos_az),
                "los_dir": compass(los_az), "peak_dir": compass(self.look(best_t)[0]),
                "minutes": max(1, round((los - aos).total_seconds() / 60)), "visible": visible,
                "sunlit": self.sunlit(best_t)}

    def sky_track(self, aos, los, step=15):
        """[[azimuth, elevation], ...] from rise to set, for drawing the pass on a sky chart."""
        start = datetime.strptime(aos, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        end = datetime.strptime(los, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        points, t = [], start
        while t <= end + timedelta(seconds=step - 1):
            az, el = self.look(min(t, end))
            points.append([round(az, 1), round(max(0.0, el), 1)])
            t += timedelta(seconds=step)
        return points

    # ---------- everything the display needs ----------
    def snapshot(self, now=None, count=4):
        if not self.ready:
            return {"ok": False}
        now = now or datetime.now(UTC)
        lat, lon, alt = self.subpoint(now)
        az, el = self.look(now)
        speed = (MU / (RE + alt)) ** 0.5 if alt > 0 else 0   # km/s (these orbits are nearly circular)
        track = []
        for minute in range(-10, 96):
            la, lo, _ = self.subpoint(now + timedelta(minutes=minute))
            track.append([round(la, 2), round(lo, 2)])
        with self._lock:
            upcoming = [p for p in self.passes if p["los"] >= iso(now)][:count]
        return {
            "ok": True, "model": self.model, "name": self.name,
            "data_age_hours": round((now - self.tle_epoch).total_seconds() / 3600, 1),
            "now": {"lat": round(lat, 2), "lon": round(lon, 2), "alt_km": round(alt),
                    "speed_kmh": round(speed * 3600, -1),
                    "az": round(az), "el": round(el, 1), "dir": compass(az), "in_range": el > 0,
                    "range_km": round(self.range_km(now)), "range_rate": round(self.range_rate(now), 3),
                    "sunlit": self.sunlit(now)},
            "track": track,
            "passes": upcoming,
            "passes_ready": self.passes_ready,
        }


def doppler(f_mhz, range_rate):
    """Where to tune: the frequency heard on the ground (downlink) or the one to send (uplink)."""
    down = f_mhz * (1 - range_rate / LIGHT_KMS)
    up = f_mhz / (1 - range_rate / LIGHT_KMS)
    return down, up


# The FM satellites, from AMSAT's list (www.amsat.org/live-fm-satellites, Oct 2026). Frequencies in MHz.
# "down" is what you listen to, "up" is what you transmit on, "tone" the CTCSS tone for the uplink.
FM_SATS = [
    {"id": "SO-50", "norad": 27607, "full": "SaudiSat-1C", "down": 436.795, "up": 145.850, "tone": 67.0,
     "amsat": "SO-50",
     "note": "Switch on its 10-minute timer first: key up for 2 seconds with a 74.4 Hz tone, then use 67 Hz."},
    {"id": "AO-123", "norad": 61781, "full": "ASRTU-1", "down": 435.400, "up": 145.850, "tone": 67.0,
     "amsat": "AO-123", "note": ""},
    {"id": "AO-91", "norad": 43017, "full": "Fox-1B (RadFxSat)", "down": 145.960, "up": 435.250, "tone": 67.0,
     "amsat": "AO-91", "sunlight": True,
     "note": "Its battery is worn out, so it only works while it's in sunlight."},
    {"id": "PO-101", "norad": 43678, "full": "Diwata-2", "down": 145.900, "up": 437.500, "tone": 141.3,
     "amsat": "PO-101",
     "note": "Only switched on at times its operators announce, and reported to be failing."},
]
ISS_FM = {"id": "ISS", "norad": 25544, "full": "International Space Station", "down": 437.800, "up": 145.990,
          "tone": 67.0, "amsat": "ISS-FM",
          "note": "The FM repeater isn't always switched on. Voice and SSTV pictures come down on 145.800."}


class Fleet:
    """The FM satellites: their orbits, passes, Doppler, and AMSAT's recent reports."""

    def __init__(self, data_dir, lat, lon, iss, url=AMATEUR_URL, amsat_url=AMSAT_URL, sats=None):
        self.data_dir, self.url, self.amsat_url = Path(data_dir), url, amsat_url
        self.iss = iss
        self.sats = [dict(s) for s in (sats if sats is not None else FM_SATS)]
        self.trackers = {s["id"]: Tracker(data_dir, lat, lon, url=None, norad=s["norad"], tle_file="amateur.tle",
                                          name=s["id"]) for s in self.sats}
        self.status = {}
        self._lock = threading.Lock()
        try:
            self.status = json.loads((self.data_dir / "amsat-status.json").read_text())
        except Exception:
            pass

    def info(self, sat_id):
        if sat_id == "ISS":
            return ISS_FM
        return next((s for s in self.sats if s["id"] == sat_id), None)

    def tracker(self, sat_id):
        return self.iss if sat_id == "ISS" else self.trackers.get(sat_id)

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="fm-sats").start()

    @staticmethod
    def _fetch(url):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=40) as response:
            return response.read().decode("utf-8", "replace")

    def _refresh_orbits(self):
        text = self._fetch(self.url)
        missing = [s for s in self.sats if ("1 %05d" % s["norad"]) not in text]
        for s in missing:   # not in CelesTrak's amateur group: ask for it by number
            try:
                text += "\n" + self._fetch(ONE_URL.format(norad=s["norad"]))
            except Exception as exc:
                say("Orbit data for %s didn't download: %s" % (s["id"], exc))
        loaded = [s["id"] for s in self.sats if self.trackers[s["id"]]._load_tle(text)]
        if loaded:
            (self.data_dir / "amateur.tle").write_text(text)
        return loaded

    def _refresh_status(self):
        out = {}
        for s in self.sats + [ISS_FM]:
            try:
                rows = json.loads(self._fetch(self.amsat_url.format(name=urllib.parse.quote(s["amsat"]))))
            except Exception as exc:
                say("AMSAT status for %s didn't download: %s" % (s["id"], exc))
                if s["id"] in self.status:
                    out[s["id"]] = self.status[s["id"]]
                continue
            out[s["id"]] = summarize_reports(rows)
        with self._lock:
            self.status = out
        try:
            (self.data_dir / "amsat-status.json").write_text(json.dumps(out))
        except OSError:
            pass

    def _run(self):
        last_orbits = last_status = 0.0
        while True:
            now = time.time()
            if now - last_orbits > 12 * 3600 or not any(t.ready for t in self.trackers.values()):
                try:
                    self._refresh_orbits()
                    last_orbits = now
                except Exception as exc:
                    say("FM satellite orbit update failed (will retry): %s" % exc)
                    last_orbits = now - 12 * 3600 + 1800   # try again in 30 minutes
            if now - last_status > 2 * 3600:
                self._refresh_status()
                last_status = now
            for sat_id, tracker in self.trackers.items():
                if tracker.ready:
                    try:
                        found = tracker.find_passes(datetime.now(UTC) - timedelta(minutes=15), hours=48)
                        with tracker._lock:
                            tracker.passes, tracker.passes_ready = found, True
                    except Exception as exc:
                        say("%s pass prediction failed: %s" % (sat_id, exc))
            time.sleep(600)

    def passes(self, now=None, count=6):
        """The next passes of all the FM satellites and the ISS, soonest first."""
        now = now or datetime.now(UTC)
        out = []
        for sat_id in ["ISS"] + [s["id"] for s in self.sats]:
            tracker = self.tracker(sat_id)
            if tracker is None or not tracker.ready:
                continue
            with tracker._lock:
                out += [dict(p, sat=sat_id) for p in tracker.passes if p["los"] >= iso(now)]
        out.sort(key=lambda p: p["aos"])
        return out[:count]

    def detail(self, sat_id, now=None):
        """Everything the satellite card needs: where it is, its passes with sky tracks, and how to tune it."""
        info, tracker = self.info(sat_id), self.tracker(sat_id)
        if info is None or tracker is None:
            return None
        now = now or datetime.now(UTC)
        snap = tracker.snapshot(now, count=3)
        out = {"id": sat_id, "full": info["full"], "down": info["down"], "up": info["up"], "tone": info["tone"],
               "note": info["note"], "sunlight_only": bool(info.get("sunlight")),
               "status": self.status.get(sat_id), "snap": snap}
        if snap.get("ok"):
            for p in snap["passes"]:
                p["track"] = tracker.sky_track(p["aos"], p["los"])
            rr = snap["now"]["range_rate"]
            out["tune"] = {"down": round(doppler(info["down"], rr)[0], 4), "up": round(doppler(info["up"], rr)[1], 4)}
            first = snap["passes"][0] if snap["passes"] else None
            if first:   # where to tune at the start, the middle and the end of the next pass
                steps = []
                for key in ("aos", "peak", "los"):
                    t = datetime.strptime(first[key], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                    r = tracker.range_rate(t)
                    steps.append({"at": key, "down": round(doppler(info["down"], r)[0], 3),
                                  "up": round(doppler(info["up"], r)[1], 3)})
                out["steps"] = steps
        return out


def summarize_reports(rows):
    """AMSAT's reports for one satellite -> when it was last heard, and how many heard it lately."""
    heard = not_heard = 0
    last_heard = last_report = None
    for row in rows if isinstance(rows, list) else []:
        when = str(row.get("reported_time") or row.get("reported_at") or "")
        report = str(row.get("report") or row.get("status") or "")
        if not when:
            continue
        if last_report is None or when > last_report["time"]:
            last_report = {"time": when, "report": report}
        if report.lower() in ("heard", "crew active"):
            heard += 1
            if last_heard is None or when > last_heard:
                last_heard = when
        elif report.lower() == "not heard":
            not_heard += 1
    return {"heard": heard, "not_heard": not_heard, "last_heard": last_heard, "last_report": last_report}
