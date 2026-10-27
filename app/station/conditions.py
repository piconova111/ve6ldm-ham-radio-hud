"""Solar numbers and HF band conditions from N0NBH (www.hamqsl.com).

Checked once an hour (the report itself changes every few hours). The last
good report is kept on disk, so the display has something to show straight
after a restart or while the internet is down.
"""
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

from . import USER_AGENT
from datetime import datetime, timezone
from pathlib import Path

URL = "https://www.hamqsl.com/solarxml.php"
GROUPS = ["80m-40m", "30m-20m", "17m-15m", "12m-10m"]


def say(message):
    sys.stdout.write("%s\n" % message)   # one write, so lines from different threads don't run together
    sys.stdout.flush()


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    data = root if root.tag == "solardata" else root.find("solardata")
    if data is None:
        raise ValueError("no <solardata> in the report")

    def text(tag):
        return (data.findtext(tag) or "").strip()

    ratings = {}
    for band in data.findall("calculatedconditions/band"):
        ratings.setdefault(band.get("name", ""), {})[band.get("time", "")] = (band.text or "").strip()
    if not ratings:
        raise ValueError("no band conditions in the report")

    updated_utc = ""
    try:  # e.g. " 02 Oct 2026 1500 GMT"
        updated_utc = datetime.strptime(text("updated"), "%d %b %Y %H%M GMT").replace(
            tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    return {
        "ok": True,
        "updated": updated_utc,
        "sfi": text("solarflux"), "a": text("aindex"), "k": text("kindex"),
        "sunspots": text("sunspots"), "geomag": text("geomagfield"), "noise": text("signalnoise"),
        "bands": [{"name": g.replace("m", "").replace("-", "–") + " m",
                   "day": ratings.get(g, {}).get("day", ""), "night": ratings.get(g, {}).get("night", "")}
                  for g in GROUPS if g in ratings],
    }


class Conditions:
    def __init__(self, cache_path, url=URL):
        self.cache = Path(cache_path)
        self.url = url
        self.data = None
        try:
            self.data = parse(self.cache.read_bytes())
        except Exception:
            pass

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="conditions").start()

    def _run(self):
        while True:
            try:
                request = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read()
                self.data = parse(body)
                self.cache.write_bytes(body)
                time.sleep(3600)
            except Exception as exc:
                say("Band conditions update failed (will retry in 10 minutes): %s" % exc)
                time.sleep(600)
