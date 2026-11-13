#!/usr/bin/env bash
# Shares the live screen for one viewing session. Used by "station screen" on another computer.
# It listens only on this computer (127.0.0.1), so the only way in is through the
# encrypted SSH connection, and it stops by itself when the viewer is closed
# (or after 30 seconds if no viewer connects).
#   share-screen.sh big      the big screen (the station display)
#   share-screen.sh laptop   the computer's own screen (where the waterfall is)
#   share-screen.sh both     both screens, shrunk to fit
# A second word, WxH, is the room the viewer has at the other end ("station screen" measures it); the picture is
# shrunk to fit it. Without it, the picture is kept to about 1600 x 880.

WHICH="${1:-big}"
ROOM="${2:-}"
[[ "$ROOM" =~ ^[0-9]{3,5}x[0-9]{3,5}$ ]] || ROOM="1600x880"
export DISPLAY=:0

# "big" is the largest screen that isn't a laptop's own panel (the same one the display uses).
GEOM="$(xrandr --query 2>/dev/null | awk -v want="$WHICH" '
  $2 == "connected" {
    laptop = ($1 ~ /^(LVDS|eDP)/)
    if ((want == "laptop") != laptop) next
    for (i = 3; i <= NF; i++)
      if ($i ~ /^[0-9]+x[0-9]+[+][0-9]+[+][0-9]+$/) {
        split($i, g, /[x+]/)
        if (g[1] * g[2] > best) { best = g[1] * g[2]; geom = $i }
        break
      }
  }
  END { if (geom != "") print geom }')"

ARGS=(-display :0 -localhost -rfbport 5900 -once -timeout 30 -nopw -shared -quiet -noxdamage)
if [ "$WHICH" != "both" ] && [ -n "$GEOM" ]; then
  ARGS+=(-clip "$GEOM")
  SIZE="${GEOM%%+*}"
else
  # Both screens, or the screen asked for isn't there (a desktop has no laptop panel): share everything.
  SIZE="$(xrandr --query 2>/dev/null | awk '/current/ { for (i = 1; i <= NF; i++) if ($i == "current") { print $(i+1) "x" $(i+3); exit } }' | tr -d ,)"
fi
# Shrink the picture so it fits the viewer's room at the other end (rounded down, so it never comes out bigger).
if [ -n "$SIZE" ]; then
  SCALE="$(awk -v s="$SIZE" -v r="$ROOM" 'BEGIN {
    split(s, d, "x"); split(r, m, "x"); k = m[1] / d[1]; if (m[2] / d[2] < k) k = m[2] / d[2]
    k = int(k * 100) / 100; if (k < 1) printf "%.2f", k }')"
  [ -n "$SCALE" ] && ARGS+=(-scale "$SCALE")
fi

exec x11vnc "${ARGS[@]}" >/dev/null
