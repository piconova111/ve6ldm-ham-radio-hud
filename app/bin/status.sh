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
wf="$(state station-waterfall)"
if [ "$wf" = active ]; then
  case "$(python3 "$HOME/station/app/station/waterfall.py" send "u DSP" 2>/dev/null)" in
    1) wf="active, receiving" ;;
    0) wf="active, not receiving" ;;
    *) wf="active, starting up" ;;
  esac
fi
printf 'Waterfall .......... %s\n' "$wf"
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
printf 'Running since ...... %s\n' "$(uptime -s)"
