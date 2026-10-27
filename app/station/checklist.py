"""The tester checklist: every part of the station display, where to find it and how to try it.

The operator ticks "Found it" and "Tried it", says whether they like it or want a change, and
writes notes. The answers are kept in ~/station/data/checklist.json, so they survive updates; new
versions add items (with "added" set to that version, so the page can mark them new).
Whoever looks after the station reads them with "station feedback" (at VE6LDM: Lee tests,
Travis reads).

Words in braces are filled in with the station's own names: {call}, {the_radio} ("the FT-950",
or "the radio" until it has a name) and {the_radio2}.
"""
import json
import os
import re
import tempfile
import threading
import time

# (section, [items]). An item's "when" says what it's waiting for, if it can't be tried yet.
SECTIONS = [
    ("Looking up a station", [
        {"id": "lookup", "title": "Look up a station",
         "where": "The box at the top that says “Type a callsign”.",
         "try": "Type any callsign (no need to click the box first) and press Enter. The card on the right shows "
                "where they are, their name and town if they're listed, which way to turn the beam, how far away "
                "they are and their local time."},
        {"id": "links", "title": "QRZ and Canada buttons",
         "where": "Beside the callsign on the station card.",
         "try": "Press QRZ to open the station's QRZ.com page in a browser window. For a Canadian call, Canada "
                "opens the Canadian callsign lookup. Close the browser window to come back."},
    ]),
    ("The maps", [
        {"id": "flat", "title": "Flat map",
         "where": "The Flat map button above the map.",
         "try": "The everyday map. The darker part is night, and the gold line is the gray line (sunrise and "
                "sunset), where long-distance contacts often open up. Blue dots are stations spotted on the band "
                "you picked, and the amber dot is you."},
        {"id": "globe", "title": "Globe",
         "where": "The Globe button, or look up a station while the flat map is showing.",
         "try": "Look up a far-away station: the map turns into a globe and swings round to show the path. Drag "
                "it with the mouse to turn it. Close the station card (the X, or Esc) to go back to the flat map."},
        {"id": "beam", "title": "Beam map",
         "where": "The Beam map button.",
         "try": "You're in the middle. A straight line out from the middle is the shortest path, and its angle is "
                "the beam heading. Point anywhere to read its heading and distance. Look up a station to see where "
                "to aim, the long way round too."},
        {"id": "zones", "title": "CQ zones",
         "where": "The red numbers on the maps, or “CQ zones” at the bottom of the map.",
         "try": "Click a red number to see that zone's countries and prefixes, and which zones are already in "
                "your log. Your zone is 4."},
    ]),
    ("Your radio", [
        {"id": "radio", "title": "Radio box",
         "where": "At the top, beside the clocks.",
         "try": "Turn {the_radio}'s dial: the frequency and mode follow within a second. The bars are the "
                "S-meter. With the radio switched off, it says “Not answering”."},
        {"id": "onair", "title": "ON AIR light",
         "where": "In the radio box.",
         "try": "Key the mic: ON AIR lights up red and the box gets a red border."},
        {"id": "power", "title": "Power and SWR",
         "where": "In the radio box, while you transmit.",
         "try": "While you transmit, it shows the power going out and the SWR. Please compare the power with the "
                "power meter on {the_radio} and write both numbers in the notes. SWR turns amber at 1.5 and red at 2.5."},
        {"id": "tune", "title": "Tune to a spot",
         "where": "The “Tune to …” button on the station card.",
         "try": "Click a spot under On the air, then press “Tune to”. The radio should jump to that "
                "frequency. It only tunes the radio; it never transmits."},
        {"id": "two_radios", "title": "Two radios",
         "where": "The radio's name in the radio box.",
         "try": "With both radios on, the name gets arrows (⇄): click it to show the other radio. Turn either "
                "radio's dial and the box follows that one. “Tune to” picks a radio that has an antenna "
                "for the band.",
         "when": "Once {the_radio2}'s cable is connected"},
    ]),
    ("Spots and your log", [
        {"id": "spots", "title": "On the air",
         "where": "The On the air tab, on the right.",
         "try": "Pick a band with the number buttons (only the bands you have antennas for). Voice only hides the "
                "CW and digital parts of each band. Click a spot to look it up."},
        {"id": "badges", "title": "New-one badges",
         "where": "In the On the air list.",
         "try": "NEW means a country that isn't in your log yet, ZONE a new CQ zone, and BAND a country you've "
                "worked, but not on that band. New countries also get an amber ring on the maps."},
        {"id": "log", "title": "Log a contact",
         "where": "The “Log this contact” button on the station card.",
         "try": "Look up the station, check the band beside the button (pick the band for a radio the display "
                "doesn't follow, such as a 2 m rig), then press "
                "Log this contact. Made a slip? Press Undo on the message that pops up."},
        {"id": "mylog", "title": "My log",
         "where": "The My log tab, beside On the air.",
         "try": "Your latest contacts. “Save the log as ADIF” at the bottom saves a copy you can send "
                "to Logbook of the World or QRZ."},
        {"id": "stats", "title": "Stats",
         "where": "The Stats button at the top.",
         "try": "Your contacts, countries (toward DXCC's 100), CQ zones and continents, with charts. The "
                "Countries tab lists every country, filled in when it's in your log. Point at a bar for its numbers."},
    ]),
    ("Around the screen", [
        {"id": "conditions", "title": "Band conditions",
         "where": "The tile at the bottom left.",
         "try": "How the bands are doing by day and by night. The chip at the top of the tile shows space "
                "weather; click it for NOAA's page."},
        {"id": "alerts", "title": "Weather and space weather alerts",
         "where": "A banner above the map, only while there's an alert.",
         "try": "Full warning opens the alert in a browser window, Next shows the next one, and the X hides it. "
                "Thunderstorm alerts remind you to disconnect the antennas.",
         "when": "When there's an alert"},
        {"id": "sats", "title": "Satellite passes",
         "where": "The Satellite passes tile at the bottom.",
         "try": "Click a pass to see where to point, how high it goes, the frequencies (with the Doppler steps on "
                "70 cm), and whether others have heard the satellite lately. A message pops up 5 minutes before a "
                "good pass. SO-50 is a good one to try with your dual-band radio."},
        {"id": "openings", "title": "Band openings",
         "where": "The Who can hear you tile at the bottom.",
         "try": "Click a band to see, as green dots on the map, where stations near you are being heard right "
                "now. It's a quick way to see which bands are open, and to where."},
        {"id": "listen", "title": "Listen to yourself",
         "where": "“Listen to yourself” on the Who can hear you tile.",
         "try": "It opens a receiver far away, already tuned to your frequency. Say your call and listen for "
                "yourself a second or two later. Close that browser window to come back."},
        {"id": "spotted", "title": "Spotted you",
         "where": "The Who can hear you tile.",
         "try": "When someone spots {call} on the DX cluster, a message pops up and they're listed there, with a "
                "line out to them on the map.",
         "when": "When someone spots you"},
        {"id": "events", "title": "Coming up",
         "where": "The tile at the bottom right.",
         "try": "Contests and on-air events coming up, with a tip for each."},
        {"id": "favourites", "title": "Favourites",
         "where": "The Favourites button at the top.",
         "try": "Your ham websites. Click one to open it in a browser window, and close the "
                "window to come back."},
    ]),
    ("The station computer", [
        {"id": "welcome", "title": "Welcome banner", "added": "0.12.0",
         "where": "When the computer starts, or click {call} at the top left.",
         "try": "Click {call} to see it again (it's good for showing visitors). Click anywhere to skip it."},
        {"id": "reopen", "title": "The display looks after itself",
         "where": "The big screen.",
         "try": "Press Alt+F4 on the display: it comes back by itself after about 5 seconds. To close it for a "
                "while, use Menu > Close station display, and Menu > Station display brings it back."},
        {"id": "waterfall", "title": "Waterfall",
         "where": "The computer's own screen.",
         "try": "Stations show as stripes that slide sideways as you turn {the_radio}'s dial.",
         "when": "Once the waterfall's receiver hears {the_radio}"},
    ]),
]

ITEMS = {item["id"]: item for _, items in SECTIONS for item in items}
GENERAL = "general"          # the "anything else?" notes at the end
VERDICTS = ("", "like", "change")
MAX_NOTE = 2000


def words(cfg):
    """The station's own names, for the words in braces (cfg is the loaded config.ini)."""
    def the(name, unnamed, plain):
        return plain if name == unnamed else "the " + name
    return {"call": cfg.callsign, "operator": cfg.operator,
            "the_radio": the(cfg.radio_name, "Radio", "the radio"),
            "the_radio2": the(cfg.radio2_name, "Second radio", "the second radio")}


def _fill(item, names):
    """A copy of an item with the words in braces filled in."""
    out = dict(item)
    for key in ("title", "where", "try", "when"):
        if isinstance(out.get(key), str):
            out[key] = re.sub(r"\{(\w+)\}", lambda m: str(names.get(m.group(1), m.group(0))), out[key])
    return out


def _clean(text):
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text or ""))   # keeps new lines and tabs
    return text[:MAX_NOTE]


class Checklist:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"responses": {}, "general": {"comment": "", "updated": 0}}
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved.get("responses"), dict):
                self.data["responses"] = saved["responses"]
            if isinstance(saved.get("general"), dict):
                self.data["general"] = saved["general"]
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self):
        folder = self.path.parent
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=".checklist-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def answer(self, body):
        """Saves one change from the page: {"id", and any of found, tried, verdict, comment}."""
        item_id = str(body.get("id") or "")
        with self.lock:
            if item_id == GENERAL:
                if "comment" not in body:
                    raise ValueError("Nothing to save.")
                self.data["general"] = {"comment": _clean(body["comment"]), "updated": round(time.time())}
                self._save()
                return dict(self.data["general"])
            if item_id not in ITEMS:
                raise ValueError("That isn't on the checklist.")
            row = dict(self.data["responses"].get(item_id) or {})
            changed = False
            for key in ("found", "tried"):
                if key in body:
                    row[key] = bool(body[key])
                    changed = True
            if "verdict" in body:
                verdict = str(body["verdict"] or "")
                if verdict not in VERDICTS:
                    raise ValueError("Like it or Needs a change, please.")
                row["verdict"] = verdict
                changed = True
            if "comment" in body:
                row["comment"] = _clean(body["comment"])
                changed = True
            if not changed:
                raise ValueError("Nothing to save.")
            if row.get("tried"):
                row["found"] = True          # tried it means found it
            row["updated"] = round(time.time())
            self.data["responses"][item_id] = row
            self._save()
            return row

    def summary(self):
        with self.lock:
            rows = {k: v for k, v in self.data["responses"].items() if k in ITEMS}
            general = dict(self.data["general"])
        updated = max([r.get("updated", 0) for r in rows.values()] + [general.get("updated", 0)])
        tried = sum(1 for r in rows.values() if r.get("tried"))
        return {"items": len(ITEMS), "found": sum(1 for r in rows.values() if r.get("found") or r.get("tried")),
                "tried": tried, "left": len(ITEMS) - tried,
                "liked": sum(1 for r in rows.values() if r.get("verdict") == "like"),
                "change": sum(1 for r in rows.values() if r.get("verdict") == "change"),
                "notes": sum(1 for r in rows.values() if (r.get("comment") or "").strip())
                + (1 if general.get("comment", "").strip() else 0),
                "updated": updated or None}

    def snapshot(self, version, names=None):
        names = names or {}
        with self.lock:
            responses = {k: dict(v) for k, v in self.data["responses"].items() if k in ITEMS}
            general = dict(self.data["general"])
        minor = lambda v: ".".join(str(v or "").split(".")[:2])   # 0.12.1 still shows what 0.12.0 added as new
        sections = [{"title": title, "items": [dict(_fill(item, names), new=bool(item.get("added")) and minor(item.get("added")) == minor(version))
                                               for item in items]}
                    for title, items in SECTIONS]
        return {"version": version, "sections": sections, "responses": responses, "general": general,
                "summary": self.summary(), "operator": names.get("operator") or ""}


def report(data, when=None):
    """The answers as plain text, for whoever looks after the station (station feedback)."""
    when = when or (lambda t: time.strftime("%a %b %d, %H:%M", time.localtime(t)))
    s = data["summary"]
    who = data.get("operator") or ""
    lines = ["%s (station app %s)" % ("%s's tester checklist" % who if who else "Tester checklist", data["version"]),
             "  Tried %d of %d, found %d, liked %d, needs a change %d%s" % (
                 s["tried"], s["items"], s["found"], s["liked"], s["change"],
                 (", last answer %s" % when(s["updated"])) if s["updated"] else "")]
    rows = data["responses"]
    items = [item for section in data["sections"] for item in section["items"]]

    def line(item, row):
        marks = ["tried" if row.get("tried") else "found" if row.get("found") else "not found yet"]
        if row.get("verdict") == "like":
            marks.append("likes it")
        note = (row.get("comment") or "").strip()
        text = "  - %s (%s)" % (item["title"], ", ".join(marks))
        if note:
            text += ": \"%s\"" % note.replace("\n", " / ")
        if row.get("updated"):
            text += "  [%s]" % when(row["updated"])
        return text

    groups = [
        ("Needs a change", lambda r: r.get("verdict") == "change"),
        ("Notes", lambda r: r.get("verdict") != "change" and (r.get("comment") or "").strip()),
    ]
    listed = set()
    for title, test in groups:
        picked = [(item, rows[item["id"]]) for item in items if item["id"] in rows and test(rows[item["id"]])]
        if picked:
            lines += ["", title] + [line(item, row) for item, row in picked]
            listed.update(item["id"] for item, _ in picked)
    liked = [item["title"] for item in items
             if rows.get(item["id"], {}).get("verdict") == "like" and not (rows[item["id"]].get("comment") or "").strip()]
    if liked:
        lines += ["", "Likes (no notes)", "  " + ", ".join(liked)]
    found = [item["title"] for item in items if item["id"] not in listed
             and rows.get(item["id"], {}).get("found") and not rows[item["id"]].get("tried")]
    if found:
        lines += ["", "Found, not tried yet", "  " + ", ".join(found)]
    waiting = [item["title"] + " (" + item["when"][:1].lower() + item["when"][1:] + ")" for item in items
               if item.get("when") and not rows.get(item["id"], {}).get("tried")]
    untouched = [item["title"] for item in items
                 if not item.get("when") and not (rows.get(item["id"], {}).get("found") or rows.get(item["id"], {}).get("tried"))]
    if untouched:
        lines += ["", "Not found yet", "  " + ", ".join(untouched)]
    if waiting:
        lines += ["", "Waiting", "  " + ", ".join(waiting)]
    general = (data["general"].get("comment") or "").strip()
    if general:
        lines += ["", "Anything else", "  \"%s\"" % general.replace("\n", " / ")]
    return "\n".join(lines)
