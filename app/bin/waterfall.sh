#!/usr/bin/env bash
# Keeps the SDR waterfall (Gqrx) open on the computer's own screen, beside the station display.
#
# Started by station-waterfall.service. If Gqrx is closed or stops, systemd starts this again
# a few seconds later, so the waterfall comes back by itself.
#   Turn it off:  systemctl --user disable --now station-waterfall
#   Turn it on:   systemctl --user enable --now station-waterfall
#   Or set "start = no" under [waterfall] in ~/station/config.ini.
#
# Each time, Gqrx starts in a known state: on the radio's IF (if_hz; the FT-950's is 69.450 MHz), with its own
# sound off (the radio does the listening) and its remote control on, so the station app can
# keep the waterfall's scale on the radio's frequency.
set -u

STATION="${STATION_DIR:-$HOME/station}"
APP="$STATION/app"
CONF_DIR="${GQRX_CONF_DIR:-$HOME/.config/gqrx}"
CONF="$CONF_DIR/station.conf"
POLL="${STATION_WF_POLL:-5}"
HUD_WAIT="${STATION_WF_HUD_WAIT:-120}"
LOG="$STATION/data/gqrx.log"
export DISPLAY="${DISPLAY:-:0}"
[ -n "${XAUTHORITY:-}" ] || export XAUTHORITY="$HOME/.Xauthority"

say() { echo "waterfall: $*"; }

setting() {  # setting KEY DEFAULT: a value from [waterfall] in config.ini
  python3 - "$STATION/config.ini" "$1" "$2" <<'PY'
import configparser, sys
cp = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
cp.read(sys.argv[1], encoding="utf-8")
print(cp.get("waterfall", sys.argv[2], fallback="").strip() or sys.argv[3])
PY
}

case "$(setting start no | tr '[:upper:]' '[:lower:]')" in
  no|off|false|0) say "switched off in config.ini ([waterfall] start = no)"; exit 3 ;;
esac
IF_HZ="$(setting if_hz 69450000)"
RC_PORT="$(setting remote_port 7356)"

# 1. Wait for the desktop.
told=0
until xrandr --query >/dev/null 2>&1; do
  [ "$told" -eq 1 ] || say "waiting for the desktop"; told=1
  sleep "$POLL"
done

# 2. Wait for the NooElec. Gqrx shows error boxes if it starts without it.
nooelec() { lsusb -d 0bda:2838 >/dev/null 2>&1 || lsusb -d 0bda:2832 >/dev/null 2>&1; }
told=0
until nooelec; do
  [ "$told" -eq 1 ] || say "waiting for the NooElec to be plugged in"; told=1
  sleep $((POLL * 6))
done

# Two copies of Gqrx can't share the NooElec, so if one was opened from the menu, wait for it.
# (When this service restarts, systemd has already closed the Gqrx it started.)
told=0
while pgrep -x gqrx >/dev/null 2>&1; do
  [ "$told" -eq 1 ] || say "Gqrx is already open (from the menu?); the waterfall starts when it's closed"; told=1
  sleep $((POLL * 6))
done

# 3. Find the screen beside the station display.
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

# 4. Start Gqrx in a known state.
mkdir -p "$CONF_DIR" "$STATION/data"
[ -f "$CONF" ] || cp "$APP/share/gqrx-station.conf" "$CONF"
python3 "$APP/bin/gqrx-conf.py" "$CONF" General/crashed=false "input/frequency=$IF_HZ" input/lnb_lo= \
  receiver/offset=0 "receiver/demod=Demod Off" remote_control/enabled=true "remote_control/port=$RC_PORT"

gqrx -c station.conf >"$LOG" 2>&1 &
pid=$!
say "Gqrx started (screen $target)"

# 5. Put its window on that screen, filling it (the taskbar stays visible).
win=""
for _ in $(seq 1 30); do
  win="$(wmctrl -l 2>/dev/null | awk '$4 ~ /^Gqrx/ { print $1; exit }')"
  [ -n "$win" ] && break
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done
if [ -n "$win" ]; then
  IFS='x+' read -r w h x y <<<"$target"
  wmctrl -i -r "$win" -b remove,maximized_vert,maximized_horz,fullscreen
  wmctrl -i -r "$win" -e "0,$((x + 40)),$((y + 40)),$((w * 3 / 4)),$((h * 3 / 4))"
  sleep 0.5
  wmctrl -i -r "$win" -b add,maximized_vert,maximized_horz
else
  say "couldn't find Gqrx's window to place it"
fi

# 6. Start receiving (Gqrx always opens stopped).
for _ in $(seq 1 15); do
  reply="$(python3 "$APP/station/waterfall.py" send "U DSP 1" "$RC_PORT" 2>/dev/null)"
  case "$reply" in RPRT\ 0*) say "receiving"; break ;; esac
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done

wait "$pid"
say "Gqrx closed (status $?); it opens again in a few seconds"
