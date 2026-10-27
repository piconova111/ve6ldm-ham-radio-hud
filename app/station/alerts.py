"""Alerts for the station display: space weather from NOAA, and weather warnings from Environment Canada.

Space weather: NOAA's scales (www.spaceweather.gov), checked every 10 minutes.
  R = radio blackouts from solar flares (HF fades on the sunlit side of the Earth),
  S = solar radiation storms (HF over the poles), G = geomagnetic storms (HF at high latitudes).
  Level 1 (minor) shows on the space weather chip only; level 2 and up also gets a banner.
Weather: the Environment Canada alert feed for the station's area (a "battleboard" ATOM feed), checked every
5 minutes. Thunderstorm and tornado alerts carry a reminder to disconnect the antennas.

Each alert has a link to the full warning. The display only ever sends an alert's id; the address
comes from here, and only addresses on the agencies' own sites are opened.
"""
import hashlib
import html
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from . import USER_AGENT

SPACE_URL = "https://services.swpc.noaa.gov/products/noaa-scales.json"
SPACE_PAGE = "https://www.spaceweather.gov/communities/radio-communications"
WEATHER_URL = "https://weather.gc.ca/rss/battleboard/{region}_e.xml"
WEATHER_PAGE = "https://weather.gc.ca/warnings/report_e.html?{region}"
ALLOWED_HOSTS = {"weather.gc.ca", "www.weather.gc.ca", "meteo.gc.ca", "www.spaceweather.gov",
                 "spaceweather.gov", "www.swpc.noaa.gov", "swpc.noaa.gov"}
ATOM = "{http://www.w3.org/2005/Atom}"

LEVEL = {1: "minor", 2: "moderate", 3: "strong", 4: "severe", 5: "extreme"}
# What each level means for HF, in NOAA's words where it gives them (www.spaceweather.gov/noaa-scales-explanation).
R_TEXT = {1: "Weak or minor fading of HF on the sunlit side of the Earth, with occasional loss of contact.",
          2: "Limited HF blackout on the sunlit side: contacts can drop out for tens of minutes.",
          3: "Wide-area HF blackout on the sunlit side, for about an hour.",
          4: "HF blackout on most of the sunlit side of the Earth for one to two hours.",
          5: "Complete HF blackout on the whole sunlit side of the Earth, for hours."}
S_TEXT = {1: "Minor effects on HF over the polar regions.",
          2: "HF paths over the poles may weaken (from North America, many paths to Europe and Asia cross them).",
          3: "HF over the poles is degraded (from North America, many paths to Europe and Asia cross them).",
          4: "HF over the poles may black out, possibly for days.",
          5: "HF through the polar regions may be impossible, possibly for days."}
G_TEXT = {1: "A minor geomagnetic storm. HF can be noisier on paths over the Arctic, and northern lights are possible.",
          2: "HF can fade at high latitudes, especially on paths over the Arctic.",
          3: "HF may come and go.",
          4: "HF propagation is sporadic.",
          5: "HF may be impossible in many areas for one to two days."}
LIGHTNING = re.compile(r"THUNDER|TORNADO|LIGHTNING", re.I)


def say(message):
    sys.stdout.write("%s\n" % message)   # one write, so lines from different threads don't run together
    sys.stdout.flush()


def _scale(part, key="Scale"):
    try:
        return int(str((part or {}).get(key)).strip())
    except (TypeError, ValueError):
        return None


def parse_space(data):
    """NOAA's noaa-scales.json -> (alerts, summary line for the band conditions tile)."""
    now, tomorrow = data.get("0") or {}, data.get("1") or {}
    alerts = []
    for letter, name, short, texts in (("R", "Radio blackout", "blackout", R_TEXT),
                                       ("S", "Solar radiation storm", "radiation storm", S_TEXT),
                                       ("G", "Geomagnetic storm", "storm now", G_TEXT)):
        level = _scale(now.get(letter))
        if level and level >= 1:
            alerts.append({
                "id": "space-" + letter.lower(), "kind": "space", "level": "warning" if level >= 2 else "advisory",
                "title": "%s %s%d (%s) now" % (name, letter, level, LEVEL.get(level, "")),
                "short": "%s%d %s" % (letter, level, short), "severe": level >= 3,
                # Minor (level 1) events are common and short, so they show on the chip but don't get a banner.
                "banner": level >= 2,
                "text": texts.get(level, ""), "url": SPACE_PAGE,
            })
    g_now, g_next = _scale(now.get("G")) or 0, _scale(tomorrow.get("G"))
    if g_next and g_next >= 1 and g_next > g_now:   # a storm on the way, or a bigger one
        alerts.append({
            "id": "space-g-watch", "kind": "space", "level": "watch",
            "title": "Geomagnetic storm G%d (%s) expected tomorrow" % (g_next, LEVEL.get(g_next, "")),
            "short": "G%d due tomorrow" % g_next, "severe": False, "banner": g_next >= 2,
            "text": G_TEXT.get(g_next, ""), "url": SPACE_PAGE,
        })
    if alerts:
        summary = "; ".join(a["title"] for a in alerts)
    else:
        summary = "Space weather is quiet: no solar flare blackouts or geomagnetic storms."
    return alerts, summary


def _text(fragment):
    """The plain text of an HTML snippet."""
    text = re.sub(r"<br\s*/?>|</p>", "\n", fragment or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\s*\n\s*", "\n", text)).strip()


def parse_weather(xml_bytes, region):
    """An Environment Canada battleboard ATOM feed -> alerts in effect."""
    root = ET.fromstring(xml_bytes)
    alerts = []
    for entry in root.findall(ATOM + "entry"):
        title = (entry.findtext(ATOM + "title") or "").strip()
        if not title or title.lower().startswith("no alerts") or " ENDED" in title.upper():
            continue
        name = title.rsplit(",", 1)[0].strip()   # "SEVERE THUNDERSTORM WARNING, Red Deer Co. near Pine Lake"
        upper = name.upper()
        level = ("warning" if "WARNING" in upper else "watch" if "WATCH" in upper
                 else "advisory" if "ADVISORY" in upper else "statement")
        link = entry.find(ATOM + "link")
        url = link.get("href") if link is not None else ""
        summary = _text(entry.findtext(ATOM + "summary") or "")
        issued = ""
        first, _, rest = summary.partition("\n")
        if first.lower().startswith("issued:"):   # "Issued: 3:02 PM MDT Thursday 22 October 2026" -> "3:02 PM Thursday"
            when = first.split(":", 1)[1].strip()
            m = re.match(r"(\d{1,2}:\d{2}\s*[AP]M)\s+[A-Z]{2,4}\s+([A-Za-z]+)", when)
            issued = "%s %s" % (m.group(1), m.group(2)) if m else when
            summary = rest.strip()
        # The same id for as long as the alert stands, so a hidden alert stays hidden; a new or updated one shows.
        key = (entry.findtext(ATOM + "id") or "") + "|" + title + "|" + (entry.findtext(ATOM + "updated") or "")
        alerts.append({
            "id": "weather-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], "kind": "weather", "level": level,
            "title": name[:1] + name[1:].lower() if name.isupper() else name,
            "text": summary[:400], "issued": issued, "url": url or WEATHER_PAGE.format(region=region),
            "lightning": bool(LIGHTNING.search(title)), "updated": entry.findtext(ATOM + "updated") or "",
        })
    return alerts


def allowed(url):
    parts = urllib.parse.urlsplit(url or "")
    return parts.scheme == "https" and (parts.hostname or "").lower() in ALLOWED_HOSTS


class Alerts:
    def __init__(self, data_dir, region="", space=True, space_url=None, weather_url=None):
        self.data_dir = Path(data_dir)
        self.region = (region or "").strip().lower()
        self.space_on = space
        self.space_url = space_url or SPACE_URL
        self.weather_url = weather_url or (WEATHER_URL.format(region=self.region) if self.region else None)
        self.space, self.space_summary, self.weather = [], "", []
        self.space_checked = self.weather_checked = None
        self._lock = threading.Lock()
        try:   # the last good space weather report, so there's something to show after a restart
            self.space, self.space_summary = parse_space(json.loads((self.data_dir / "space-weather.json").read_text()))
        except Exception:
            pass

    def start(self):
        if self.space_on:
            threading.Thread(target=self._loop, args=(self._check_space, 600), daemon=True, name="space").start()
        if self.weather_url:
            threading.Thread(target=self._loop, args=(self._check_weather, 300), daemon=True, name="weather").start()

    @staticmethod
    def _loop(check, every):
        while True:
            try:
                check()
                time.sleep(every)
            except Exception as exc:
                say("Alert update failed (will retry in 5 minutes): %s" % exc)
                time.sleep(300)

    @staticmethod
    def _fetch(url):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def _check_space(self):
        body = self._fetch(self.space_url)
        alerts, summary = parse_space(json.loads(body))
        with self._lock:
            self.space, self.space_summary, self.space_checked = alerts, summary, time.time()
        (self.data_dir / "space-weather.json").write_bytes(body)

    def _check_weather(self):
        alerts = parse_weather(self._fetch(self.weather_url), self.region)
        with self._lock:
            self.weather, self.weather_checked = alerts, time.time()

    def listing(self):
        with self._lock:
            items = self.weather + self.space
            return {
                "alerts": [{k: v for k, v in a.items() if k != "url"} for a in items],
                "space": self.space_summary if self.space_on else "",
                "weather_on": bool(self.weather_url),
                # False until both feeds have been read once since the app started
                "ready": (not self.space_on or self.space_checked is not None)
                         and (not self.weather_url or self.weather_checked is not None),
            }

    def url_for(self, alert_id):
        """The full warning's address for an alert id from the display, or None."""
        if alert_id == "space" and self.space_on:   # the space weather chip on the band conditions tile
            return "NOAA's space weather page", SPACE_PAGE
        with self._lock:
            for a in self.weather + self.space:
                if a["id"] == alert_id and allowed(a["url"]):
                    return a["title"], a["url"]
        return None
