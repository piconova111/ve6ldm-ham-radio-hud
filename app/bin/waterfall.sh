#!/usr/bin/env bash
# Keeps the waterfall open on the computer's own screen, beside the station display.
#
# The station app runs the SDR itself (through rtl_tcp) and serves the waterfall page at
# http://127.0.0.1:8073/waterfall; this opens that page in its own window on the other screen,
# filling it with the taskbar still showing.
#
# Started by station-waterfall.service. If the window is closed or Chromium stops, systemd starts
# this again a few seconds later, so the waterfall comes back by itself.
#   Close the window until it's turned on again:  systemctl --user disable --now station-waterfall
#   Turn it on again:                              systemctl --user enable --now station-waterfall
#   The receiver itself runs in the station app: "start = no" under [waterfall] in ~/station/config.ini
#   (then restart the app) turns off both.
#   Its messages: journalctl --user -u station-waterfall -n 30 (the receiver's are in the app's)
set -u

STATION="${STATION_DIR:-$HOME/station}"
PROFILE="${STATION_WF_PROFILE:-$HOME/.config/station-waterfall}"
POLL="${STATION_WF_POLL:-5}"
HUD_WAIT="${STATION_WF_HUD_WAIT:-120}"
export DISPLAY="${DISPLAY:-:0}"
[ -n "${XAUTHORITY:-}" ] || export XAUTHORITY="$HOME/.Xauthority"

say() { echo "waterfall: $*"; }

setting() {  # setting SECTION KEY DEFAULT: a value from config.ini
  python3 - "$STATION/config.ini" "$1" "$2" "$3" <<'PY'
import configparser, sys
cp = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
cp.read(sys.argv[1], encoding="utf-8")
print(cp.get(sys.argv[2], sys.argv[3], fallback="").strip() or sys.argv[4])
PY
}

case "$(setting waterfall start no | tr '[:upper:]' '[:lower:]')" in
  no|off|false|0) say "switched off in config.ini ([waterfall] start = no)"; exit 3 ;;
esac
PORT="${STATION_PORT:-$(setting display port 8073)}"
URL="http://127.0.0.1:$PORT/waterfall"
BROWSER="$(command -v chromium || command -v chromium-browser || true)"
if [ -z "$BROWSER" ]; then
  say "Chromium isn't installed; run the station installer again"
  exit 3
fi

# 1. Wait for the desktop, and its window manager, so the window can be put on the right screen.
told=0
until xrandr --query >/dev/null 2>&1; do
  [ "$told" -eq 1 ] || say "waiting for the desktop"; told=1
  sleep "$POLL"
done
if command -v wmctrl >/dev/null 2>&1; then
  for _ in $(seq 1 24); do
    wmctrl -m >/dev/null 2>&1 && break
    sleep "$POLL"
  done
fi

# 2. Only one waterfall window: if one was opened some other way, wait for it to close.
told=0
while pgrep -u "$(id -u)" -f -- "--user-data-dir=$PROFILE( |$)" >/dev/null 2>&1; do
  [ "$told" -eq 1 ] || say "the waterfall is already open; this takes over when that window closes"; told=1
  sleep "$POLL"
done

# 3. Give the station app up to a minute to start answering.
for _ in $(seq 1 60); do
  curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/health" && break
  sleep 1
done

# 4. Find the screen beside the station display.
screens() {  # one line per connected screen: NAME WxH+X+Y
  xrandr --query 2>/dev/null | awk '$2 == "connected" {
    for (i = 3; i <= NF; i++) if ($i ~ /^[0-9]+x[0-9]+[+][0-9]+[+][0-9]+$/) { print $1, $i; break } }'
}
hud_spot() {  # the middle of the station display's window, if it's open: X Y
  wmctrl -lG 2>/dev/null | awk '/Station display/ { print $3 + int($5 / 2), $4 + int($6 / 2); exit }'
}
pick_screen() {  # WxH+X+Y of a screen that doesn't hold the station display (nothing if there's only one)
  screens | awk -v spot="$1" '
    BEGIN { have = (split(spot, s, " ") == 2) }
    { split($2, g, /[x+]/); geom[NR] = $2; w[NR] = g[1]; h[NR] = g[2]; x[NR] = g[3]; y[NR] = g[4]
      panel[NR] = ($1 ~ /^(LVDS|eDP|DSI)/) }
    END {
      if (NR < 2) exit
      hud = 0
      if (have) for (i = 1; i <= NR; i++)
        if (s[1] >= x[i] && s[1] < x[i] + w[i] && s[2] >= y[i] && s[2] < y[i] + h[i]) hud = i
      if (!hud) {  # the display is not open yet: same rule as kiosk.sh (largest screen that is not a built-in panel)
        best = 0
        for (i = 1; i <= NR; i++) if (!panel[i] && w[i] * h[i] > best) { best = w[i] * h[i]; hud = i }
        if (!hud) hud = 1
      }
      for (i = 1; i <= NR; i++) if (i != hud && panel[i]) { print geom[i]; exit }
      for (i = 1; i <= NR; i++) if (i != hud) { print geom[i]; exit }
    }'
}

spot="$(hud_spot)"
deadline=$((SECONDS + HUD_WAIT))
while [ -z "$spot" ] && [ "$SECONDS" -lt "$deadline" ]; do
  sleep "$POLL"
  spot="$(hud_spot)"
done
[ -n "$spot" ] || say "the station display isn't open, so the waterfall goes on the screen it doesn't normally use"

told=0
target="$(pick_screen "$spot")"
while [ -z "$target" ]; do
  [ "$told" -eq 1 ] || say "only one screen is connected; the waterfall waits so it doesn't cover the station display"; told=1
  sleep 60
  target="$(pick_screen "$(hud_spot)")"
done
IFS='x+' read -r w h x y <<<"$target"

# 5. Open the page in its own window there. After a power cut Chromium offers to "restore pages":
#    mark the last exit as normal instead.
PREFS="$PROFILE/Default/Preferences"
if [ -f "$PREFS" ]; then
  sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$PREFS"
fi
say "opening the waterfall (screen $target)"
"$BROWSER" --app="$URL" --user-data-dir="$PROFILE" \
  --window-position="$((x + 40)),$((y + 40))" --window-size="$((w * 3 / 4)),$((h * 3 / 4))" \
  --password-store=basic --no-first-run --noerrdialogs --disable-infobars \
  --disable-session-crashed-bubble --disable-features=Translate \
  --check-for-update-interval=31536000 >/dev/null 2>&1 &
pid=$!

# 6. Fill that screen (the taskbar stays visible). Chromium may put the window back where it was
#    last time, so it's moved onto this screen first.
find_window() {  # the waterfall's window: one that belongs to its own Chromium (found by its profile)
  local pids
  pids="$(pgrep -u "$(id -u)" -f -- "--user-data-dir=$PROFILE( |$)" | tr '\n' ' ')"
  [ -n "$pids" ] || return 0
  wmctrl -lp 2>/dev/null | awk -v pids="$pids" '
    BEGIN { n = split(pids, p, " "); for (i = 1; i <= n; i++) mine[p[i]] = 1 }
    ($3 in mine) {   # wmctrl -lp: id, desktop, pid, host, title
      title = $0; sub(/^[^ ]+ +[^ ]+ +[^ ]+ +[^ ]+ /, "", title)
      if (title ~ /^Waterfall/) { print $1; found = 1; exit }
      if (first == "") first = $1
    }
    END { if (!found && first != "") print first }'
}
win=""
for _ in $(seq 1 30); do
  win="$(find_window)"
  [ -n "$win" ] && break
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done
if [ -n "$win" ]; then
  wmctrl -i -r "$win" -b remove,maximized_vert,maximized_horz,fullscreen
  wmctrl -i -r "$win" -e "0,$((x + 40)),$((y + 40)),$((w * 3 / 4)),$((h * 3 / 4))"
  sleep 0.5
  wmctrl -i -r "$win" -b add,maximized_vert,maximized_horz
else
  say "couldn't find the waterfall's window to fill the screen"
fi

wait "$pid"
say "closed (status $?); it opens again in a few seconds"
