#!/usr/bin/env bash
# Short health report for the station. Run it here, or from another computer with: station status

state() { local s; s="$(systemctl --user is-active "$1" 2>/dev/null)"; echo "${s:-unknown}"; }
setting() {
  python3 - "$HOME/station/config.ini" "$1" "$2" "$3" <<'PY'
import configparser, sys
cp = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
cp.read(sys.argv[1], encoding="utf-8")
print(cp.get(sys.argv[2], sys.argv[3], fallback="").strip() or sys.argv[4])
PY
}
line() { local label="$1 "; while [ ${#label} -lt 20 ]; do label="$label."; done; printf '%s %s\n' "$label" "$2"; }
printf 'Station app ........ %s\n' "$(state station-app)"
line "$(setting radio name Radio) link" "$(state station-rigctld)"
if [ "$(setting radio2 model 0)" = "0" ]; then
  line "$(setting radio2 name 'Second radio') link" "not set up yet"
else
  line "$(setting radio2 name 'Second radio') link" "$(state station-rigctld2)"
fi
printf 'Nightly backup ..... %s\n' "$(state station-backup.timer)"
# The waterfall: the app runs the SDR itself; station-waterfall only keeps its window open.
python3 - "$(state station-waterfall)" "$(setting waterfall start no)" "$(state station-windows)" "$(setting layout keep yes)" <<'PY'
import json
import os
import sys
import time
import urllib.request

window, start = sys.argv[1], sys.argv[2].strip().lower()
layout_state, layout_keep = sys.argv[3], sys.argv[4].strip().lower()
def label(text):
    return (text + " ").ljust(20, ".") + " "
try:
    with urllib.request.urlopen("http://127.0.0.1:8073/api/waterfall/info", timeout=5) as r:
        info = json.load(r)
except Exception:
    info = None
if info is None:
    print(label("Waterfall") + "no answer from the app (its window: %s)" % window)
elif not info.get("on"):
    print(label("Waterfall") + ("off (start = no under [waterfall] in config.ini)" if start in ("no", "off", "false", "0")
                                else "off until the radio is set up (the setup helper sets it up)"))
else:
    st = info.get("status") or {}
    if st.get("running"):
        what = "receiving, %g lines a second" % (st.get("lines_per_s") or 0)
    elif st.get("problem"):
        what = "not receiving: %s" % st["problem"]
    else:
        what = "starting"
    print(label("Waterfall") + what + "; " + ("its window is open" if window == "active"
                                              else "its window isn't open (station-waterfall is %s)" % window))
    way = "turned round" if st.get("orientation") == "mirrored" else "as it comes"
    text = "%s, %s" % (way, st.get("orientation_how") or "not known")
    dc = st.get("dial_check")
    if dc:   # the last turn of the dial: did the stations stay on their frequencies as shown, or turned round?
        text += "; last dial turn (%g kHz at %s): %s (as shown %.2f, turned round %.2f)" % (
            abs(dc.get("moved_hz", 0)) / 1000, dc.get("when", "?"),
            {"as shown": "right as shown", "turned round": "it should be turned round",
             "unclear": "not enough stations to tell"}.get(dc.get("verdict"), "?"),
            round(dc.get("as_is", 0), 2) + 0.0, round(dc.get("flipped", 0), 2) + 0.0)   # + 0.0: no "-0.00"
    print(label("Waterfall picture") + text)
    # On frequency: checked against shortwave broadcast stations (their carriers sit on 5 kHz steps), and
    # small drift (the SDR's crystal warming up) corrected by the app itself.
    tr, auto = st.get("track"), st.get("auto_hz") or 0
    plural = lambda n: "%d broadcast station%s" % (n, "" if n == 1 else "s")
    on = " on %s" % tr["mode"] if tr and tr.get("mode") else ""   # the radio's mode at that check
    if not tr:
        text = "not checked yet (it checks against shortwave broadcast stations once the dial stays still for 20 seconds)"
    elif tr.get("off_hz") is None:
        text = "no broadcast stations clear enough to check against%s at %s (it tries again every minute)" % (on, tr.get("when", "?"))
    elif abs(tr["off_hz"]) <= 30:
        text = "on frequency%s%s (%s, %s)" % (on, " to within %d Hz" % abs(tr["off_hz"]) if abs(tr["off_hz"]) >= 5 else "",
                                              plural(tr["stations"]), tr["when"])
    else:
        text = "%d Hz off%s at %s (%s); it corrects that by itself" % (abs(tr["off_hz"]), on, tr["when"], plural(tr["stations"]))
    # The correction is kept for each of the radio's modes (the radio can put each a little differently at its IF).
    modes, settled = st.get("auto_modes") or {}, st.get("auto_settled") or []
    if len(modes) > 1:
        text += "; it has moved the picture by itself: %s (%s), on top of offset_hz %d" % (", ".join(
            "%s %+d Hz%s" % (m, v, "" if m in settled else " (still being lined up)") for m, v in sorted(modes.items())),
            st.get("auto_when") or "?", st.get("offset_hz") or 0)
    elif auto:
        text += "; it has moved the picture %+d Hz by itself%s (%s), on top of offset_hz %d" % (
            auto, " for %s" % next(iter(modes)) if modes else "", st.get("auto_when") or "?", st.get("offset_hz") or 0)
    if st.get("auto_note"):
        text += "; " + st["auto_note"]
    print(label("Waterfall tuning") + text)
    shows, needs = st.get("carrier_offset") or {}, st.get("carrier_needs") or {}
    if not shows:
        print(label("Waterfall carrier") + "not measured yet (the app measures it the next time the radio transmits)")
    else:   # the radio's own carrier while it transmits, as the waterfall shows it
        text = ", ".join("%+d Hz from the dial on %s" % (hz, mode) for mode, hz in sorted(shows.items()))
        if any(abs(hz) > 1000 for hz in shows.values()):   # more than the app corrects by itself
            if max(needs.values()) - min(needs.values()) > 60:
                text += "; those don't agree, so it needs more transmissions before offset_hz is changed"
            else:
                text += ("; offset_hz = %d under [waterfall] in config.ini would put it on the dial "
                         "(check it on a broadcast station first)" % needs.get("USB", sorted(needs.values())[len(needs) // 2]))
        print(label("Waterfall carrier") + text)
    # The window layout (bin/windows.py, station-windows): what opens on that screen goes beside the waterfall.
    try:
        lay = json.load(open(os.path.expanduser("~/station/data/windows.json")))
    except Exception:
        lay = None
    if layout_keep in ("no", "off", "false", "0"):
        text = "off (keep = no under [layout] in config.ini): windows open wherever they like"
    elif layout_state != "active":
        try:
            import Xlib   # noqa: F401
            text = "not running (station-windows is %s; journalctl --user -u station-windows -n 20 says why)" % layout_state
        except ImportError:
            text = "not running: python3-xlib isn't installed (the station installer adds it)"
    elif not lay:
        text = "starting"
    else:
        beside = [" ".join(str(t).split()) for t in lay.get("beside") or []]   # WSJT-X's own title has runs of spaces
        if lay.get("waterfall") == "narrow" and beside:
            text = "the waterfall makes room for %s, beside it on the right (%d px)" % (
                ", ".join(beside[:3]) + (" and %d more" % (len(beside) - 3) if len(beside) > 3 else ""), lay.get("side_width") or 0)
        elif lay.get("waterfall") == "none":
            text = "keeping new windows off the station display (the waterfall isn't open)"
        else:
            text = "the waterfall has the whole screen; what opens there goes beside it, on the right (%d px)" % (
                lay.get("side_width") or 0)
    print(label("Window layout") + text)
    # Listening from another computer (station listen): who's listening to what, and what it costs this computer.
    try:
        with urllib.request.urlopen("http://127.0.0.1:8073/api/listen/status", timeout=5) as r:
            ls = json.load(r)
    except Exception:
        ls = None
    if ls is not None:
        people = ls.get("listeners") or []
        if not people:
            text = "nobody listening (station listen, on another computer, plays the sound from the waterfall)"
        else:
            def mhz(hz):
                return "%d.%03d.%02d" % (hz // 1000000, hz // 1000 % 1000, hz % 1000 // 10) if hz else "?"
            text = "; ".join("%s %s%s, for %d min" % (mhz(p.get("freq_hz")), p.get("kind") or "",
                                                     ", following the radio" if p.get("follow") else "", p.get("for_s", 0) // 60)
                             for p in people)
            if ls.get("load") is not None:
                text += "; the sound takes %d%% of one processor core" % round(100 * ls["load"])
            if ls.get("dropped"):
                text += "; %d tenths of a second dropped (this computer couldn't keep up)" % ls["dropped"]
        print(label("Listening") + text)
    # FT8: WSJT-X's microphone, fed from the waterfall's receiver while WSJT-X is open.
    try:
        with urllib.request.urlopen("http://127.0.0.1:8073/api/ft8", timeout=5) as r:
            f8 = json.load(r)
    except Exception:
        f8 = None
    if f8 is not None and f8.get("on"):
        if not f8.get("installed"):
            text = "WSJT-X isn't installed yet (the station installer adds it)"
        elif f8.get("problem"):
            text = "not working: %s" % f8["problem"]
        elif f8.get("feeding"):
            hz = f8.get("heard_hz")
            text = "WSJT-X is open, hearing %s through the waterfall's receiver, for %d min" % (
                "%d.%03d.%02d" % (hz // 1000000, hz // 1000 % 1000, hz % 1000 // 10) if hz else "the radio",
                max(0, (int(time.time()) - (f8.get("since") or int(time.time()))) // 60))
            if f8.get("dropped"):
                text += "; %d tenths of a second dropped (WSJT-X's sound system fell behind)" % f8["dropped"]
        elif f8.get("mic"):
            text = "ready: WSJT-X isn't open; its microphone (station_receiver) hears the radio when it is"
        else:
            text = "WSJT-X's microphone isn't there yet (the app makes it within a few seconds)"
        print(label("FT8 (WSJT-X)") + text)
        # WSJT-X follows the radio through the station's receive-only link; and what WSJT-X's own settings say.
        lk, ws = f8.get("link") or {}, f8.get("wsjtx_settings")
        port = lk.get("port") or 4534
        if lk.get("problem"):
            text = "not working: %s" % lk["problem"]
        elif lk.get("connected"):
            text = "receive only: WSJT-X is following the radio through it (port %d)" % port
        else:
            text = "receive only, on port %d (WSJT-X connects to it while it's open)" % port
        if lk.get("transmit_refused"):
            when = time.strftime("%H:%M", time.localtime(lk["last_refused_at"])) if lk.get("last_refused_at") else "?"
            text += "; kept WSJT-X from making the radio transmit %d time%s (last: %s, at %s)" % (
                lk["transmit_refused"], "" if lk["transmit_refused"] == 1 else "s", lk.get("last_refused") or "?", when)
        if ws is None:
            text += "; WSJT-X has no settings yet (the installer gives it the station's)"
        elif not ws.get("readable"):
            text += "; couldn't read WSJT-X's settings (%s)" % ws.get("why", "?")
        else:
            radio = ws.get("radio")
            if radio == "direct":
                text += ("; WSJT-X is set to reach rigctld directly (%s), so it could make the radio transmit: in WSJT-X,"
                         " Settings, Radio, set Network Server to 127.0.0.1:%d" % (ws.get("server") or "port 4532", port))
            elif radio == "other":
                text += ("; WSJT-X is set to %s%s, not the station's link (Hamlib NET rigctl, 127.0.0.1:%d), so it isn't"
                         " following the radio through it" % (ws.get("rig") or "?", " " + ws["server"] if ws.get("server") else "", port))
            elif radio == "none":
                text += "; WSJT-X isn't set to follow the radio (its Rig is None)"
            if ws.get("input") != "station_receiver":
                text += "; WSJT-X's sound input is %s, not station_receiver" % (ws.get("input") or "not set")
        print(label("FT8 radio link") + text)
        # The stations WSJT-X has decoded lately: the app hears them from WSJT-X's own network messages, for the map.
        try:
            with urllib.request.urlopen("http://127.0.0.1:8073/api/ft8/heard?why=1", timeout=5) as r:
                fh = json.load(r)
        except Exception:
            fh = None
        if fh and fh.get("on"):
            rows, mins = fh.get("stations") or [], fh.get("window_min") or 15
            if fh.get("problem"):
                text = "the map can't show them: %s" % fh["problem"]
            elif rows:
                far = max((r for r in rows if isinstance(r.get("km"), (int, float))), key=lambda r: r["km"], default=None)
                text = "%d heard in the last %d min" % (len(rows), mins)
                if far:
                    text += ", farthest %s (%s, %s km)" % (far["call"], far.get("country") or "?", format(far["km"], ","))
                text += "; they show on the station display's map"
            else:   # why not, as best the app can tell (an older app doesn't say)
                text = "none heard in the last %d min" % mins + (fh.get("hint") or (
                    " (is the radio on an FT8 frequency? 20 m: 14.074)" if f8.get("wsjtx") else " (WSJT-X isn't open)"))
            if fh.get("errors"):
                text += "; %d message%s from WSJT-X couldn't be read (skipped; the app's messages say why)" % (
                    fh["errors"], "" if fh["errors"] == 1 else "s")
            print(label("FT8 stations") + text)
        # PSK Reporter: whether WSJT-X shares what it hears there, and as whom.
        if ws and ws.get("readable"):
            grid = ws.get("grid") or ""
            if ws.get("psk_reporter"):
                text = "WSJT-X shares the stations it hears there, as %s in %s" % (ws.get("call") or "?", grid or "no grid")
                if len(grid) > 4:
                    text += ("; that grid goes public there too, and its first four characters (%s) are all FT8 needs:"
                             " WSJT-X, File, Settings, General" % grid[:4])
            else:
                text = "off (WSJT-X doesn't share what it hears; WSJT-X, File, Settings, Reporting turns it on)"
            print(label("PSK Reporter") + text)
PY
printf 'Station display .... %s\n' "$(state station-display)"
printf 'Version ............ %s\n' "$(cat "$HOME/station/app/VERSION" 2>/dev/null || echo unknown)"

python3 - <<'PY'
import json
import urllib.request

def get(path, timeout=5):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8073" + path, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None

cfg = get("/api/config") or {}
health = get("/api/health") or {}
seen = health.get("display_seen_s")
if seen is not None:
    print("Display page ....... %s" % ("showing (checked in %d s ago)" % seen if seen < 120
                                        else "not answering for %d minutes (it restarts itself after 5)" % (seen // 60)))
spots = get("/api/spots?band=all&voice=0") or {}
cond = get("/api/conditions") or {}
log = get("/api/log/stats") or {}
radio = get("/api/rig") or {}
print("Station location ... %s" % (cfg.get("grid") or "not set yet (the setup helper sets it, or grid in config.ini)"))
if cfg and not cfg.get("callsign_set", True):
    print("Callsign ........... not set yet (callsign under [station] in config.ini)")
print("Callbook ........... %s" % ("HamQTH" if cfg.get("callbook") else "off (country file only)"))
ca = cfg.get("canada")
print("Canadian hams ...... %s" % ("{:,} in the list ({:,} placed in their towns), from {}".format(ca["count"], ca["placed"], ca["updated"])
                                    if ca else "not downloaded yet (the app fetches the list in the background)"))
WHY = {"quiet": "not answering (is it switched on?)", "cable": "its USB cable is unplugged",
       "link": "its radio link is starting"}
def label(text):
    return (text + " ").ljust(20, ".") + " "
def radio_line(r, showing):
    if r.get("ok"):
        return "on %.3f MHz %s%s" % (r["freq_hz"] / 1e6, r.get("mode") or "", ", showing on the display" if showing else "")
    if r.get("set_up") is False:
        return "not set up yet (run the setup helper once the cable is in)"
    return "not connected: " + WHY.get(r.get("reason"), "no answer")
if radio:
    both = any(o.get("ok") for o in radio.get("others", [])) and radio.get("ok")
    print(label(radio.get("name") or "Radio") + radio_line(radio, both))
    for other in radio.get("others", []):
        print(label(other.get("name") or "Radio") + radio_line(other, False))
else:
    print(label("Radio") + "no answer from the app")
shown = {radio.get("radio")} | {o.get("radio") for o in radio.get("others", [])}
for r in cfg.get("radios", []):
    if not r.get("set_up") and r["radio"] not in shown:
        print(label(r["name"]) + "not set up yet (once its cable is in: radio2.sh, or station radio2 remotely)")
state = spots.get("status", "unknown")   # connected, logging in, connecting, reconnecting, off, no callsign
if state == "connected" and spots.get("node"):
    state = "connected to %s" % spots["node"]
elif state == "no callsign":
    state = "off until the callsign is set"
print("DX cluster ......... %s, %d spots in the last 30 minutes" % (state, len(spots.get("spots", []))))
print("Band conditions .... %s" % (("report from " + cond["updated"]) if cond.get("ok") else "no report yet"))
alerts = get("/api/alerts") or {}
print("Alerts ............. %s" % ("; ".join(a["title"] for a in alerts.get("alerts", [])) or "none in effect"))
op = get("/api/openings")
if op is None or not op.get("on"):
    print("Band openings ...... %s" % ("off (switched off in config.ini)" if op else "no answer"))
else:
    bands = ", ".join("%s %d" % (b["band"], b["receivers"]) for b in op.get("bands", []))
    state = "connected" if op.get("connected") else "not connected" + (" (%s)" % op["error"] if op.get("error") else "")
    print("Band openings ...... PSK Reporter %s, %d reports since the app started; hearing your area: %s"
          % (state, op.get("messages", 0), bands or "nothing in the last 15 minutes"))
    if op.get("own"):
        print("Your digital signal  decoded by %d stations today" % len({r["call"] for r in op["own"]}))
sats = get("/api/sats") or {}
fm = [p for p in sats.get("passes", []) if p["sat"] != "ISS"]
if fm:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    p = fm[0]
    when = datetime.strptime(p["aos"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    when = when.astimezone(ZoneInfo(cfg.get("timezone") or "UTC"))
    print("FM satellite next .. %s, %s, up to %s degrees" % (p["sat"], when.strftime("%a %b %d, %H:%M"), p["max_el"]))
else:
    print("FM satellite next .. %s" % ("waiting for orbit data" if not sats.get("ready") else "none above 10 degrees in the next 2 days"))
kiwi = get("/api/kiwi?freq=14250000&mode=USB", timeout=60) or {}
print("Listen receivers ... %s" % ("%d public KiwiSDRs listed (from %s), %d suggested for 20 m"
                                    % (kiwi.get("listed", 0), kiwi.get("from", "?"), len(kiwi.get("receivers", [])))
                                    if kiwi.get("ok") else "the KiwiSDR list didn't load"))
iss = get("/api/iss") or {}
if iss.get("ok") and iss.get("passes"):
    p = iss["passes"][0]
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    when = datetime.strptime(p["aos"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    when = when.astimezone(ZoneInfo(cfg.get("timezone") or "UTC"))
    print("ISS next pass ...... %s, up to %s degrees" % (when.strftime("%a %b %d, %H:%M"), p["max_el"]))
else:
    print("ISS next pass ...... %s" % ("waiting for orbit data" if not iss.get("ok")
                                        else "working out the passes" if not iss.get("passes_ready")
                                        else "none above 10 degrees in the next 2 days"))
ck = (get("/api/checklist") or {}).get("summary")
if ck:
    who = cfg.get("operator") or ""
    print(label("%s's checklist" % who if who else "Tester checklist") + "tried %d of %d, %d liked, %d %s a change%s" % (
        ck["tried"], ck["items"], ck["liked"], ck["change"], "needs" if ck["change"] == 1 else "need",
        " (station feedback shows the notes)" if ck["notes"] or ck["change"] else ""))
total = log.get("total", "?")
print("Logbook ............ %s contact%s" % (total, "" if total == 1 else "s"))
PY

nights="$(ls "$HOME"/station-backups/logbook-*.sqlite3 2>/dev/null | wc -l)"
printf 'Backups ............ %s night%s kept in ~/station-backups\n' "$nights" "$([ "$nights" -eq 1 ] || echo s)"
printf 'Disk space free .... %s\n' "$(df -h --output=avail "$HOME" | tail -n 1 | tr -d ' ')"
# The clock: FT8 needs it right to within a second, so it should keep in step with internet time.
synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
case "$synced" in
  yes) printf 'Clock .............. in step with internet time\n' ;;
  no)  printf 'Clock .............. not in step with internet time (FT8 needs it right to within a second): sudo timedatectl set-ntp true\n' ;;
esac
# The processor and how busy it is (the load: how many processes wanted it, on average, over the last minute).
cpu="$(sed -n 's/^model name[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo 2>/dev/null | head -n 1)"
printf 'Processor .......... %s x %s; load %s\n' "${cpu:-unknown}" "$(nproc 2>/dev/null || echo '?')" \
  "$(cut -d ' ' -f 1 /proc/loadavg 2>/dev/null || echo '?')"
printf 'Running since ...... %s\n' "$(uptime -s)"
