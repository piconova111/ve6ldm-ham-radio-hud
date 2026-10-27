"""Favourite ham websites, shown on the display and opened in the computer's web browser.

The display only ever sends an id ("f07", or "qrz" with a callsign); the address
itself always comes from this file, so nothing else can be opened through it.
To use your own list, put a favourites.json next to config.ini (the same shape as
GROUPS below; python3 favourites.py prints this list in that shape) and restart the app.
"{callsign}" in an address is filled in with the station's callsign.
"""
import json
import os
import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path

# The built-in list is VE6LDM's: the Ham folder from Lee's Edge favourites (October 2026),
# tidied up: duplicates removed, a dead link and a download-site link left out, and the
# FT-950 CAT manual pointed at Yaesu's copy instead of a copy in his Downloads folder.
GROUPS = [
    {"group": "Look up a call", "links": [
        {"name": "QRZ callsign lookup", "url": "https://www.qrz.com/lookup"},
        {"name": "Canadian callsign lookup", "url": "https://hamster.foxhollow.ca/ccd/?keywords={callsign}&searchType=1&db=CURRENT-DATABASE"},
        {"name": "HamCall lookup", "url": "https://hamcall.net/call"},
    ]},
    {"group": "Clubs, repeaters and maps", "links": [
        {"name": "CAARC, Central Alberta Amateur Radio Club", "url": "https://caarc.ca/"},
        {"name": "Repeater map of Canada", "url": "https://www.levinecentral.com/repeaters/google_mapping_canada.php"},
        {"name": "PSK Reporter map", "url": "https://www.pskreporter.info/pskmap.html"},
    ]},
    {"group": "Propagation", "links": [
        {"name": "Solar-terrestrial data", "url": "https://solar.sv5fri.eu/solar.php"},
        {"name": "How to read solar data", "url": "https://www.esa-spaceweather.net/how-to-read-solar-data-to-predict-hf-radio-propagation/"},
        {"name": "HF bands chart", "url": "https://www.hamuniverse.com/hfbands.html"},
    ]},
    {"group": "Radio manuals", "links": [
        {"name": "Yaesu FT-950 operating manual", "url": "https://www.radiomanual.info/schemi/YAESU_HF/FT-950_user.pdf"},
        {"name": "Yaesu FT-950 CAT manual", "url": "https://www.yaesu.com/Files/4CB893D7-1018-01AF-FA97E9E9AD48B50C/FT-950_CAT_Manual_ENG.pdf"},
        {"name": "Kenwood TS-480 manual", "url": "https://www.hamradio.co.uk/userfiles/file/Kenwood%20Manuals/B62-1735-30_English.pdf"},
        {"name": "Yaesu FT-2500M manual", "url": "https://www.manualslib.com/manual/381747/Yaesu-Ft-2500m.html?page=5#manual"},
        {"name": "Yaesu FT-8800 quick reference", "url": "https://www.pwcares.org/doc/ft-8800-qrc.pdf"},
        {"name": "Wouxun KG-UV6D manual", "url": "https://www.wouxun.com/resource/UV6D_PEN.pdf"},
        {"name": "Icom AH-2 tuner manual", "url": "https://www.fracassi.net/iw2ntf/manuali/IC-AH-2.pdf"},
        {"name": "Icom AH-2b antenna manual", "url": "https://www.ameradio.com/doc/Icom_AH-2B_instruction_manual.pdf"},
        {"name": "Daiwa CN-101L SWR meter", "url": "https://www.radiomuseum.org/r/dan_swr_power_meter_cn_101l.html#collection"},
        {"name": "Manual saved from manuals.plus", "url": "https://manuals.plus/m/b9d1f4dc753caece916c5ef9dc9ea97828c8e60dc5b54b7ad7c1f3f12d743cfc?utm_redirect=1"},
    ]},
    {"group": "Antennas", "links": [
        {"name": "Hustler RM75 resonator", "url": "https://www.radioworld.ca/product/hus-rm75/rm75-40-meter-resonator-400-watts-ssb-3-8-24-thread-32-1-8-inches"},
        {"name": "Hustler RM40 resonator", "url": "https://www.radioworld.ca/product/hus-rm40/rm40-40-meter-resonator-400-watts-ssb-3-8-24-thread-32-1-8-inches"},
        {"name": "Hustler RM20 resonator", "url": "https://www.radioworld.ca/product/hus-rm20/rm20-20-meter-resonator-400-watts-ssb-3-8-24-thread-18-7-16-inches"},
        {"name": "Hustler RM17 resonator", "url": "https://www.radioworld.ca/product/hus-rm17/rm17-17-meter-resonator-400-watts-ssb-3-8-24-thread-22-3-4-inches"},
        {"name": "Hustler resonator manual", "url": "https://www.new-tronics.com/main/manual/RM_Manua.pdf"},
        {"name": "Diamond X50A 2 m / 70 cm antenna", "url": "https://www.radioworld.ca/product/dia-x50a/x50a-dualband-base-repeater-2m-70cm-antenna-5-6-ft"},
        {"name": "Cushcraft AP8 manual", "url": "https://www.manualslib.com/manual/4261567/Cushcraft-Ap8.html"},
        {"name": "W4TI hexbeam", "url": "https://www.iw5edi.com/wp-content/uploads/2014/01/W4TI-Hexbeam.pdf"},
        {"name": "K6MM 160 m vertical", "url": "https://hamuniverse.com/k6mm160metervertical.html"},
        {"name": "ARRL Antenna Book", "url": "http://www.arrl.org/arrl-antenna-book"},
    ]},
    {"group": "SDR, the NooElec", "links": [
        {"name": "NooElec quick start", "url": "https://www.nooelec.com/store/qs#windows"},
        {"name": "SDR guide from Radio Hobbyist", "url": "https://radio-hobbyist.com/sdr-hub/"},
        {"name": "HDSDR", "url": "https://hdsdr.de/"},
    ]},
    {"group": "Ham terms", "links": [
        {"name": "Dictionary of ham radio terms", "url": "https://www.amateur-radio-wiki.net/dictionary-of-ham-radio-terms/"},
        {"name": "Ham codes and abbreviations", "url": "https://hamradioprepper.com/hamradiocodesabbreviations/"},
    ]},
]

# Look-ups for one callsign, from the station card.
LOOKUPS = {
    "qrz": {"short": "QRZ", "where": "on QRZ", "url": "https://www.qrz.com/db/{call}"},
    "ccd": {"short": "Canada", "where": "in the Canadian callsign lookup",
            "url": "https://hamster.foxhollow.ca/ccd/?keywords={call}&searchType=1&db=CURRENT-DATABASE", "canada_only": True},
}


def _web(url):
    return urllib.parse.urlsplit(str(url)).scheme in ("http", "https")


class Favourites:
    def __init__(self, station_dir, say=print, callsign=""):
        self.path = Path(station_dir) / "favourites.json"
        self.say = say
        self.groups = self._load()
        for group in self.groups:
            for link in group["links"]:
                link["url"] = link["url"].replace("{callsign}", urllib.parse.quote(callsign or ""))
        self.by_id = {}
        for group in self.groups:
            for link in group["links"]:
                link["id"] = "f%02d" % (len(self.by_id) + 1)
                self.by_id[link["id"]] = link

    def _load(self):
        """The station's own list if there's a favourites.json, otherwise the built-in one."""
        source = GROUPS
        if self.path.exists():
            try:
                source = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.say("favourites.json couldn't be read, so the built-in list is used: %s" % exc)
        groups = self._tidy(source)
        if not groups and source is not GROUPS:
            self.say("favourites.json has no usable links, so the built-in list is used")
            groups = self._tidy(GROUPS)
        return groups

    @staticmethod
    def _tidy(source):
        """Fresh copies of the groups, keeping only web links (http or https)."""
        groups = []
        for group in source if isinstance(source, list) else []:
            if not isinstance(group, dict):
                continue
            links = [{"name": str(link.get("name") or link.get("url")).strip(), "url": str(link["url"]).strip()}
                     for link in group.get("links") or [] if isinstance(link, dict) and _web(link.get("url"))]
            if links:
                groups.append({"group": str(group.get("group") or "Favourites").strip(), "links": links})
        return groups

    def listing(self):
        return {"groups": [{"group": g["group"],
                            "links": [{"id": link["id"], "name": link["name"],
                                       "site": urllib.parse.urlsplit(link["url"]).hostname.removeprefix("www.")}
                                      for link in g["links"]]}
                           for g in self.groups]}

    @staticmethod
    def lookups_for(canadian):
        """The look-up buttons for a station card."""
        return [{"id": key, "short": item["short"], "where": item["where"]} for key, item in LOOKUPS.items()
                if canadian or not item.get("canada_only")]

    def address(self, link_id, call=None, canadian=False):
        """The address for an id from the display, or None. A call is only used for look-ups."""
        if link_id in self.by_id:
            link = self.by_id[link_id]
            return link["name"], link["url"]
        lookup = LOOKUPS.get(link_id)
        if lookup and call and (canadian or not lookup.get("canada_only")):
            return "%s %s" % (call, lookup["where"]), lookup["url"].format(call=urllib.parse.quote(call, safe="/"))
        return None


def open_in_browser(url):
    """Opens a web page in the computer's default web browser, on the station's desktop. True if it started.

    xdg-open uses whichever browser is set as the default (Chromium or Firefox); if it's
    missing, Firefox or Chromium is started directly.
    """
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")  # the app runs as a background service, so point it at the screen
    xauth = Path.home() / ".Xauthority"
    if "XAUTHORITY" not in env and xauth.exists():
        env["XAUTHORITY"] = str(xauth)
    runtime = "/run/user/%d" % os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(runtime + "/bus"):
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/bus" % runtime
    for command in (["xdg-open", url], ["firefox", "--new-window", url], ["chromium", "--new-window", url]):
        if shutil.which(command[0], path=env.get("PATH")):
            child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            threading.Thread(target=child.wait, daemon=True).start()  # tidy up when it exits
            return True
    return False


open_in_firefox = open_in_browser  # the name used by version 0.7.0 and 0.7.1


if __name__ == "__main__":
    # Prints the built-in list as JSON, to start a favourites.json from:
    #   python3 ~/station/app/station/favourites.py > ~/station/favourites.json
    print(json.dumps(GROUPS, indent=2))
