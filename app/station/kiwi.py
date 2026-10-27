"""Listen to yourself: public KiwiSDR receivers far enough away to hear the station by skywave.

KiwiSDRs are shortwave receivers on the internet that anyone can listen to in a
web browser. The public list comes from rx.linkfanel.net (a copy of the list on
kiwisdr.com, with each receiver's location, frequency range and free listener
slots); kiwisdr.com/public is used if that doesn't answer.

For the radio's band, receivers at a useful skywave distance are picked (beyond
ground wave, not past where the band usually reaches), preferring ones in the
beam's direction, with a free slot. A receiver opens in the web browser already
tuned to the radio's frequency and mode, using the KiwiSDR's own link format
(for example http://host:8073/?f=14200usb).
"""
import hashlib
import html
import json
import math
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

from . import USER_AGENT, geo

LIST_URL = "http://rx.linkfanel.net/kiwisdr_com.js"
FALLBACK_URL = "http://kiwisdr.com/public/"
REFRESH = 20 * 60          # the list is fetched again when it's older than this

# Useful listening distances by band (km): (closest, farthest, best). Inside the closest there is
# little but ground wave, and the band's skip often jumps over; past the farthest it rarely reaches.
RANGES = {
    "160m": (150, 1500, 600), "80m": (150, 1500, 700), "60m": (250, 2000, 900),
    "40m": (300, 2500, 1200), "30m": (600, 3500, 1800), "20m": (800, 4500, 2200),
    "17m": (1000, 5000, 2600), "15m": (1000, 5500, 2800), "12m": (1200, 6000, 3000),
    "10m": (1200, 6000, 3000), "6m": (700, 2500, 1500),
}
MODES = {"USB": "usb", "PKTUSB": "usb", "RTTYR": "usb", "LSB": "lsb", "PKTLSB": "lsb", "RTTY": "lsb",
         "CW": "cw", "CWR": "cw", "AM": "am", "FM": "nbfm", "PKTFM": "nbfm"}
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def say(message):
    sys.stdout.write("%s\n" % message)   # one write, so lines from different threads don't run together
    sys.stdout.flush()


def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _gps(text):
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", str(text or ""))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon


def _covers(bands, freq_hz):
    """'0-30000000' (Hz), sometimes several ranges separated by commas."""
    found = False
    for part in str(bands or "").split(","):
        m = re.match(r"\s*(\d+)\s*-\s*(\d+)", part)
        if m:
            found = True
            if int(m.group(1)) <= freq_hz <= int(m.group(2)):
                return True
    return not found     # no range given: assume the usual 0-30 MHz


def _clean_url(url):
    url = html.unescape(str(url or "")).strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c in url for c in " <>\"'"):
        return None
    return "%s://%s" % (parts.scheme, parts.netloc)


def parse_js(text):
    """rx.linkfanel.net's kiwisdr_com.js: 'var kiwisdr_com = [ {...}, ... ];' (close to JSON)."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        return []
    body = text[start:end + 1]
    try:
        rows = json.loads(body)
    except ValueError:
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        body = re.sub(r"(?m)^\s*//[^\n]*$", "", body)                # whole-line comments
        body = re.sub(r",\s*([\]}])", r"\1", body)                   # trailing commas
        try:
            rows = json.loads(body)
        except ValueError:
            rows = []
            for chunk in re.findall(r"\{[^{}]*\}", body):            # last resort: one receiver at a time
                try:
                    rows.append(json.loads(re.sub(r",\s*}", "}", chunk)))
                except ValueError:
                    continue
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def parse_public(text):
    """kiwisdr.com/public: one block per receiver, its details in <!-- key=value --> comments."""
    marker = "cl-entry" if re.search(r"class=['\"][^'\"]*cl-entry", text) else "cl-info"
    chunks = re.split(r"<div[^>]+class=['\"][^'\"]*" + marker, text)
    href = r"<a[^>]+href=['\"]([^'\"]+)['\"]"
    rows = []
    for i in range(1, len(chunks)):
        block = chunks[i]
        row = dict((k.strip(), v.strip()) for k, v in re.findall(r"<!--\s*([\w-]+)\s*=\s*(.*?)\s*-->", block))
        if "url" not in row:
            if marker == "cl-entry":       # the link is inside the receiver's block
                link = re.search(href, block)
                url = link.group(1) if link else None
            else:                          # the link comes just before its details
                links = re.findall(href, chunks[i - 1])
                url = links[-1] if links else None
            if url:
                row["url"] = url
        if row:
            rows.append(row)
    return rows


class Kiwis:
    def __init__(self, data_dir, lat, lon, url=LIST_URL, fallback=FALLBACK_URL):
        self.lat, self.lon = lat, lon
        self.url, self.fallback = url, fallback
        self.cache_path = data_dir / "kiwisdr.json"
        self.rows, self.fetched = [], 0.0
        self.source = ""
        self._lock = threading.Lock()
        try:
            saved = json.loads(self.cache_path.read_text())
            self.rows, self.fetched, self.source = saved["rows"], saved["fetched"], saved.get("source", "")
        except Exception:
            pass

    @staticmethod
    def _fetch(url):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.read(8 * 1024 * 1024).decode("utf-8", "replace")

    def _tidy(self, raw):
        out = []
        for r in raw:
            gps, url = _gps(r.get("gps")), _clean_url(r.get("url"))
            if not gps or not url:
                continue
            out.append({
                "id": hashlib.sha1(url.encode()).hexdigest()[:12], "url": url,
                "name": html.unescape(str(r.get("name") or ""))[:120],
                "loc": html.unescape(str(r.get("loc") or ""))[:80],
                "antenna": html.unescape(str(r.get("antenna") or ""))[:100],
                "lat": gps[0], "lon": gps[1], "bands": str(r.get("bands") or ""),
                "users": _int(r.get("users")), "users_max": _int(r.get("users_max"), 4),
                "offline": str(r.get("offline") or "").lower() == "yes" or str(r.get("status") or "").lower() == "offline",
                "snr": _int(str(r.get("snr") or "").split(",")[-1], 0),
            })
        return out

    def refresh(self, force=False):
        """The receiver list, fetched again if it's older than 20 minutes. Never raises."""
        with self._lock:
            if not force and self.rows and time.time() - self.fetched < REFRESH:
                return self.rows
            for url, parse in ((self.url, parse_js), (self.fallback, parse_public)):
                if not url:
                    continue
                try:
                    rows = self._tidy(parse(self._fetch(url)))
                except Exception as exc:
                    say("KiwiSDR list from %s didn't load: %s" % (url, exc))
                    continue
                if rows:
                    self.rows, self.fetched, self.source = rows, time.time(), url
                    try:
                        self.cache_path.write_text(json.dumps({"rows": rows, "fetched": self.fetched, "source": url}))
                    except OSError:
                        pass
                    break
            return self.rows

    def pick(self, freq_hz, band, bearing=None, count=6):
        """Receivers worth trying for this band, best first; in-beam ones first when a heading is given."""
        rows = self.refresh()
        low, high, best = RANGES.get(band, (800, 4500, 2200))
        usable = []
        for r in rows:
            if r["offline"] or r["users"] >= r["users_max"] or not _covers(r["bands"], freq_hz):
                continue
            km = geo.distance_km(self.lat, self.lon, r["lat"], r["lon"])
            heading = geo.bearing(self.lat, self.lon, r["lat"], r["lon"])
            usable.append(dict(r, km=round(km), bearing=round(heading),
                               dir=DIRS[int(((heading + 22.5) % 360) // 45)]))
        for widen in (1.0, 1.5, 2.5):                    # not enough at the usual distances: look wider
            lo, hi = low / widen, high * widen
            chosen = [r for r in usable if lo <= r["km"] <= hi]
            if len(chosen) >= count:
                break
        for r in chosen:
            off = None if bearing is None else abs((r["bearing"] - bearing + 180) % 360 - 180)
            r["in_beam"] = off is not None and off <= 30
            r["score"] = (abs(math.log(max(r["km"], 50) / best)) + (0 if r["in_beam"] else 0.8 if bearing is not None else 0)
                          - 0.05 * min(r["users_max"] - r["users"], 4) - 0.01 * min(r["snr"], 30))
        chosen.sort(key=lambda r: r["score"])
        picked, places = [], []
        for r in chosen:                                # not three from the same town
            if any(abs(r["lat"] - p[0]) < 0.5 and abs(r["lon"] - p[1]) < 0.5 for p in places):
                continue
            places.append((r["lat"], r["lon"]))
            picked.append({k: r[k] for k in ("id", "name", "loc", "antenna", "km", "bearing", "dir", "users",
                                             "users_max", "in_beam", "lat", "lon")})
            if len(picked) == count:
                break
        return picked

    def link(self, receiver_id, freq_hz, mode):
        """The receiver's address, tuned to the frequency (kHz) and mode, or None if it isn't in the list."""
        r = next((r for r in self.rows if r["id"] == receiver_id), None)
        if r is None:
            return None
        khz = ("%.2f" % (freq_hz / 1000)).rstrip("0").rstrip(".")
        return r["name"] or r["loc"] or "the receiver", "%s/?f=%s%s" % (r["url"], khz, MODES.get(str(mode).upper(), "usb"))
