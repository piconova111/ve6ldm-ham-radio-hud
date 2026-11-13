#!/usr/bin/env bash
# Opens WSJT-X (FT8 and the other digital modes) on the computer's own screen, not over the station display.
#
# The menu entry "WSJT-X (FT8)" runs this, and so does the waterfall's FT8 button (through the station app). If
# WSJT-X is already open, its windows are brought to the front instead, the main one last so it ends up on top.
# WSJT-X remembers where its windows were, so this only steps in when one of them opens over the station
# display (the first time, or after its windows were moved there): for the first half minute, any WSJT-X
# window there is moved to the other screen, at the same place on it. WSJT-X itself is set up by the
# station installer (see HELP.txt, "FT8").
# Since 0.15.8 the window layout (bin/windows.py, station-windows) puts WSJT-X's windows beside the waterfall as
# they open, and the FT8 button asks it first (to bring WSJT-X to the front or put it away); this launcher's own
# moving and raising are what's left when the layout isn't running.
set -u

export DISPLAY="${DISPLAY:-:0}"
WAIT="${STATION_WSJTX_WAIT:-30}"   # seconds to watch for WSJT-X's windows (its splash, main window, waterfall)
say() { echo "wsjtx: $*"; }

running() {  # a WSJT-X of this user's that's really running (not one that just ended)
  local p
  for p in $(pgrep -u "$(id -u)" -x wsjtx 2>/dev/null); do
    grep -qs '^State:[[:space:]]*Z' "/proc/$p/status" || return 0
  done
  return 1
}
front() {  # every window of the running WSJT-X to the front: its others first (the Wide Graph...), then the main one
  command -v wmctrl >/dev/null 2>&1 || return 0
  local p pids="" main="" id title
  for p in $(pgrep -u "$(id -u)" -x wsjtx 2>/dev/null); do pids="$pids $p"; done
  # wmctrl -lp: id, desktop, pid, host, title. The main window's title is "WSJT-X   v3.0.2 ..."; the others'
  # have " - " in them ("WSJT-X - Wide Graph").
  while read -r id title; do
    case "$title" in
      *" - "*) wmctrl -i -a "$id" >/dev/null 2>&1 ;;
      *) main="$main $id" ;;
    esac
  done < <(wmctrl -lp 2>/dev/null | awk -v pids="$pids" '
    BEGIN { n = split(pids, a, " "); for (i = 1; i <= n; i++) want[a[i]] = 1 }
    ($3 in want) { id = $1; $1 = $2 = $3 = $4 = ""; sub(/^ +/, ""); print id, $0 }')
  for id in $main; do wmctrl -i -a "$id" >/dev/null 2>&1; done
}
if running; then
  front
  say "WSJT-X is already open: brought it to the front"
  exit 0
fi
if ! command -v wsjtx >/dev/null 2>&1; then
  say "WSJT-X isn't installed; run the station installer (station update) with [ft8] enabled = yes"
  exit 1
fi
wsjtx "$@" >/dev/null 2>&1 &
pid=$!
command -v wmctrl >/dev/null 2>&1 && command -v xrandr >/dev/null 2>&1 || exit 0

screens() {  # one line per connected screen: NAME WxH+X+Y
  xrandr --query 2>/dev/null | awk '$2 == "connected" {
    for (i = 3; i <= NF; i++) if ($i ~ /^[0-9]+x[0-9]+[+][0-9]+[+][0-9]+$/) { print $1, $i; break } }'
}
# The station display's screen and the one to use instead (as waterfall.sh picks them): "HUD TARGET", each
# WxH+X+Y, or nothing if there's only one screen.
pick() {
  local spot
  spot="$(wmctrl -lG 2>/dev/null | awk '/Station display/ { print $3 + int($5 / 2), $4 + int($6 / 2); exit }')"
  screens | awk -v spot="$spot" '
    BEGIN { have = (split(spot, s, " ") == 2) }
    { split($2, g, /[x+]/); geom[NR] = $2; w[NR] = g[1]; h[NR] = g[2]; x[NR] = g[3]; y[NR] = g[4]
      panel[NR] = ($1 ~ /^(LVDS|eDP|DSI)/) }
    END {
      if (NR < 2) exit
      hud = 0
      if (have) for (i = 1; i <= NR; i++)
        if (s[1] >= x[i] && s[1] < x[i] + w[i] && s[2] >= y[i] && s[2] < y[i] + h[i]) hud = i
      if (!hud) {  # the display is not open: same rule as kiosk.sh (largest screen that is not a built-in panel)
        best = 0
        for (i = 1; i <= NR; i++) if (!panel[i] && w[i] * h[i] > best) { best = w[i] * h[i]; hud = i }
        if (!hud) hud = 1
      }
      for (i = 1; i <= NR; i++) if (i != hud && panel[i]) { print geom[hud], geom[i]; exit }
      for (i = 1; i <= NR; i++) if (i != hud) { print geom[hud], geom[i]; exit }
    }'
}
read -r hud target <<<"$(pick)"
# Opened from the waterfall's FT8 button (the station app sets this): WSJT-X's main window is brought to the front
# once it appears, as it would be from the menu, in case the desktop opened it behind the waterfall.
raise="${STATION_WSJTX_RAISE:-}"
if [ -z "${target:-}" ] && [ -z "$raise" ]; then
  exit 0   # one screen: nothing to keep clear
fi
if [ -n "${target:-}" ]; then
  IFS='x+' read -r hw hh hx hy <<<"$hud"
  IFS='x+' read -r tw th tx ty <<<"$target"
fi

moved=" "
end=$((SECONDS + WAIT))
while [ "$SECONDS" -lt "$end" ] && kill -0 "$pid" 2>/dev/null; do
  # wmctrl -lpG: id, desktop, pid, x, y, width, height, host, title
  while read -r id wx wy ww wh title; do
    if [ -n "$raise" ]; then
      case "$title" in
        *" - "*) ;;                    # not the main window (the Wide Graph...)
        *" v"[0-9]*)                   # the main window: "WSJT-X   v3.0.2 ..." (the splash before it has no version)
          wmctrl -i -a "$id" >/dev/null 2>&1
          say "brought WSJT-X's main window to the front"
          raise="" ;;
      esac
    fi
    [ -n "${target:-}" ] || continue
    case "$moved" in *" $id "*) continue ;; esac
    cx=$((wx + ww / 2)); cy=$((wy + wh / 2))
    if [ "$cx" -ge "$hx" ] && [ "$cx" -lt $((hx + hw)) ] && [ "$cy" -ge "$hy" ] && [ "$cy" -lt $((hy + hh)) ]; then
      nx=$((tx + wx - hx)); ny=$((ty + wy - hy))                       # the same place on the other screen,
      [ $((nx + ww)) -gt $((tx + tw)) ] && nx=$((tx + tw - ww))         # kept on it
      [ $((ny + wh)) -gt $((ty + th)) ] && ny=$((ty + th - wh))
      [ "$nx" -lt "$tx" ] && nx=$tx
      [ "$ny" -lt "$ty" ] && ny=$ty
      wmctrl -i -r "$id" -b remove,maximized_vert,maximized_horz,fullscreen >/dev/null 2>&1
      wmctrl -i -r "$id" -e "0,$nx,$ny,-1,-1" >/dev/null 2>&1
      say "moved a WSJT-X window off the station display (to $nx,$ny)"
      moved="$moved$id "   # once each: if it's put back there by hand, it stays
    fi
  done < <(wmctrl -lpG 2>/dev/null | awk -v pid="$pid" '$3 == pid {
    t = ""; for (i = 9; i <= NF; i++) t = t (i > 9 ? " " : "") $i
    print $1, $4, $5, $6, $7, t }')
  [ -z "${target:-}" ] && [ -z "$raise" ] && break   # one screen: done once the main window is in front
  sleep 0.5
done
exit 0
