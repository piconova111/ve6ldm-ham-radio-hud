#!/usr/bin/env bash
# Opens the station display full-screen on the big monitor.
# If only one screen is connected, it opens on that one.
#
# Started by station-display.service, which opens it again a few seconds after it's closed
# (Alt+F4) or if Chromium stops, unless "keep_open = no" is set under [display] in config.ini.
#   Reopen it now:   systemctl --user restart station-display    (or Menu > Station display)
#   Close it:        Menu > Close station display, or: systemctl --user stop station-display
#   Its messages:    journalctl --user -u station-display -n 30
set -u

STATION="${STATION_DIR:-$HOME/station}"
PROFILE="${STATION_DISPLAY_PROFILE:-$HOME/.config/station-display}"
POLL="${STATION_DISPLAY_POLL:-5}"
BACKOFF="${STATION_DISPLAY_BACKOFF:-55}"   # the environment settings are for tests
export DISPLAY="${DISPLAY:-:0}"
[ -n "${XAUTHORITY:-}" ] || export XAUTHORITY="$HOME/.Xauthority"

say() { echo "display: $*"; }

setting() {  # setting SECTION KEY DEFAULT: a value from config.ini
  python3 - "$STATION/config.ini" "$1" "$2" "$3" <<'PY'
import configparser, sys
cp = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
cp.read(sys.argv[1], encoding="utf-8")
print(cp.get(sys.argv[2], sys.argv[3], fallback="").strip() or sys.argv[4])
PY
}

PORT="${STATION_PORT:-$(setting display port 8073)}"
URL="http://127.0.0.1:$PORT/"

BROWSER="$(command -v chromium || command -v chromium-browser || true)"
if [ -z "$BROWSER" ]; then
  say "Chromium isn't installed; run the station installer again"
  notify-send "Station display" "Chromium isn't installed. Run the station installer again." 2>/dev/null
  exit 3   # no point trying again until it's installed
fi

# 1. Wait for the desktop: after a restart this starts before the automatic login has finished.
told=0
until xrandr --query >/dev/null 2>&1; do
  [ "$told" -eq 1 ] || say "waiting for the desktop"; told=1
  sleep "$POLL"
done
# ...and for its window manager, so the window lands full-screen on the right monitor.
if command -v wmctrl >/dev/null 2>&1; then
  for _ in $(seq 1 24); do
    wmctrl -m >/dev/null 2>&1 && break
    sleep "$POLL"
  done
fi

# 2. Only one display. If it was opened some other way (by hand, or by an older version's
#    start-up entry), wait for that window to close instead of opening a second one.
told=0
while pgrep -u "$(id -u)" -f -- "--user-data-dir=$PROFILE( |$)" >/dev/null 2>&1; do
  [ "$told" -eq 1 ] || say "the station display is already open; this takes over when that window closes"; told=1
  sleep "$POLL"
done

# 3. Give the station app up to a minute to start answering.
for _ in $(seq 1 60); do
  curl -fs -o /dev/null "${URL}api/health" && break
  sleep 1
done

# 4. The big screen is the largest connected screen that isn't a laptop's own panel.
POS="$(xrandr --query 2>/dev/null | awk '
  $2 == "connected" && $1 !~ /^(LVDS|eDP)/ {
    for (i = 3; i <= NF; i++)
      if ($i ~ /^[0-9]+x[0-9]+[+][0-9]+[+][0-9]+$/) {
        split($i, g, /[x+]/)
        if (g[1] * g[2] > best) { best = g[1] * g[2]; pos = g[3] "," g[4] }
        break
      }
  }
  END { if (pos != "") print pos }')"
POS="${POS:-0,0}"

# After a power cut Chromium offers to "restore pages". Mark the last exit as normal instead.
PREFS="$PROFILE/Default/Preferences"
if [ -f "$PREFS" ]; then
  sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$PREFS"
fi

say "opening the station display (screen at $POS)"
started=$SECONDS
"$BROWSER" --kiosk --window-position="$POS" --user-data-dir="$PROFILE" \
  --password-store=basic --no-first-run --noerrdialogs --disable-infobars \
  --disable-session-crashed-bubble --disable-features=Translate \
  --check-for-update-interval=31536000 "$URL" >/dev/null 2>&1
status=$?

case "$(setting display keep_open yes | tr '[:upper:]' '[:lower:]')" in
  no|off|false|0)
    say "closed (status $status); it stays closed (keep_open = no). Reopen it from the menu: Station display"
    exit 3 ;;
esac
if [ $((SECONDS - started)) -lt "${STATION_DISPLAY_QUICK:-15}" ]; then
  say "Chromium closed straight away (status $status); trying again in a minute"
  sleep "$BACKOFF"
else
  say "closed (status $status); it opens again in a few seconds"
fi
exit 0
