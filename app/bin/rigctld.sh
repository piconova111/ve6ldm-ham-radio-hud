#!/usr/bin/env bash
# Starts Hamlib's rigctld (the radio link) with the settings for one radio from ~/station/config.ini:
#   rigctld.sh          the [radio] section (at VE6LDM, the FT-950), on port 4532
#   rigctld.sh radio2   the [radio2] section (at VE6LDM, the TS-480), on port 4533
# Model 1 is Hamlib's practice radio, so everything works with no radio attached; model 0 means
# that radio isn't set up yet, so nothing is started. It only listens on this computer (127.0.0.1).

CONFIG="${STATION_DIR:-$HOME/station}/config.ini"
SECTION="${1:-radio}"

setting() {
  python3 - "$CONFIG" "$1" "$2" "$3" <<'PY'
import configparser, sys
cp = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
cp.read(sys.argv[1], encoding="utf-8")
print(cp.get(sys.argv[2], sys.argv[3], fallback=sys.argv[4]).strip())
PY
}

if [ "$SECTION" = "radio" ]; then
  DEFAULT_MODEL=1; DEFAULT_BAUD=4800; DEFAULT_PORT=4532
else
  DEFAULT_MODEL=0; DEFAULT_BAUD=9600; DEFAULT_PORT=4533
fi
MODEL="$(setting "$SECTION" model "$DEFAULT_MODEL")"
DEVICE="$(setting "$SECTION" device '')"
BAUD="$(setting "$SECTION" baud "$DEFAULT_BAUD")"
HANDSHAKE="$(setting "$SECTION" handshake '')"
PORT="$(setting "$SECTION" rigctld_port "$DEFAULT_PORT")"

if [ "${MODEL:-0}" = "0" ]; then
  exec sleep infinity   # this radio isn't set up yet; nothing to talk to
fi

ARGS=(-m "$MODEL" -T 127.0.0.1 -t "${PORT:-$DEFAULT_PORT}")
if [ "$MODEL" != "1" ] && [ -n "$DEVICE" ]; then
  ARGS+=(-r "$DEVICE" -s "${BAUD:-$DEFAULT_BAUD}")
  case "$HANDSHAKE" in
    none) ARGS+=(-C serial_handshake=None) ;;
    hardware) ARGS+=(-C serial_handshake=Hardware) ;;
  esac
fi

exec rigctld "${ARGS[@]}"
