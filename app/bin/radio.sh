#!/usr/bin/env bash
# Connects a radio through Hamlib: finds its USB cable, its Hamlib model and the speed it answers
# at, saves them in ~/station/config.ini, and starts its radio link. Safe to run again.
#   radio.sh                  the radio, [radio] in config.ini
#   radio.sh radio2           the second radio, [radio2]
#   radio.sh [radio2] --check only test the cable and the radio; nothing is changed
# Remotely:  station radio2   (or: ssh -t you@station '~/station/app/bin/radio.sh radio2')
# The radio link only reads and tunes the radio. It never transmits.
set -uo pipefail

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${STATION_DIR:-$HOME/station}/config.ini"
SET="$APP/bin/set-setting.py"
SERIAL_DIR="${SERIAL_DIR:-/dev/serial/by-id}"
SPEEDS="${RADIO_SPEEDS:-4800 9600 19200 38400 57600 115200}"   # the usual CAT speeds, commonest first
SECTION=radio
CHECK=0
for arg in "$@"; do
  case "$arg" in
    radio|radio2) SECTION="$arg" ;;
    --check) CHECK=1 ;;
    *) echo "Usage: radio.sh [radio2] [--check]"; exit 2 ;;
  esac
done
if [ "$SECTION" = radio ]; then
  OTHER=radio2; SERVICE=station-rigctld; PORT=4532; WHICH="the radio"
else
  OTHER=radio; SERVICE=station-rigctld2; PORT=4533; WHICH="the second radio"
fi

ok()   { printf '    \033[32mok\033[0m    %s\n' "$*"; }
note() { printf '    \033[33mnote\033[0m  %s\n' "$*"; }
tell() { printf '    %s\n' "$*"; }
ask()  { local reply=""; [ -t 0 ] && read -r -p "    $1 " reply; printf '%s' "$reply"; }
setting() {
  python3 - "$CONFIG" "$1" "$2" "$3" <<'PY'
import configparser, sys
cp = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
cp.read(sys.argv[1], encoding="utf-8")
print(cp.get(sys.argv[2], sys.argv[3], fallback="").strip() or sys.argv[4])
PY
}
# The radios Hamlib knows that match a name: "NUMBER<tab>MAKER<tab>MODEL" lines, exact matches first.
find_models() {
  rigctl -l 2>/dev/null | python3 -c '
import re, sys
squash = lambda text: re.sub(r"[^A-Z0-9]", "", text.upper())
want = squash(sys.argv[1])
exact, close = [], []
for line in sys.stdin:
    parts = re.split(r"\s{2,}", line.strip())
    if len(parts) < 3 or not parts[0].isdigit() or not want:
        continue
    number, maker, model = parts[0], parts[1], parts[2]
    if squash(model) == want:
        exact.append((number, maker, model))
    elif len(want) >= 3 and want in squash(maker + model):
        close.append((number, maker, model))
for number, maker, model in (exact or close)[:12]:
    print("%s\t%s\t%s" % (number, maker, model))
' "$1"
}

if [ "$(id -u)" -eq 0 ]; then echo "Please run this as your normal user, without sudo."; exit 1; fi
NAME="$(setting "$SECTION" name '')"
typed_name=0
printf '\n\033[1;36m%s\033[0m\n' "${NAME:-${WHICH^}}$([ "$SECTION" = radio2 ] && [ -n "$NAME" ] && echo ' (the second radio)')$([ $CHECK -eq 1 ] && echo ': a check only, nothing is changed')"

if ! command -v rigctl >/dev/null 2>&1; then
  note "Hamlib's rigctl isn't installed. Run the station installer again, then this."
  exit 1
fi
if ! id -nG | grep -qw dialout; then
  note "$USER isn't allowed to use serial ports yet. Run the setup helper first, or run"
  tell "  sudo usermod -aG dialout $USER"
  tell "then restart the computer and run this again."
  exit 1
fi

# Which radio it is, as Hamlib knows it.
model="$(setting "$SECTION" model 0)"
if [ "$model" = "0" ] || [ "$model" = "1" ]; then
  if [ -z "$NAME" ]; then
    NAME="$(ask "Which radio is it? Its model, as on the front panel (for example FT-950, IC-7300, TS-590SG):")"
    NAME="$(printf '%s' "$NAME" | tr -s ' ' | sed 's/^ //; s/ $//')"
    if [ -z "$NAME" ]; then
      note "No radio named. Put its name under [$SECTION] in $CONFIG (name = ...), then run this again."
      exit 1
    fi
    typed_name=1
  fi
  mapfile -t matches < <(find_models "$NAME")
  model="" chosen=""
  if [ ${#matches[@]} -eq 1 ]; then
    chosen="${matches[0]}"
  elif [ ${#matches[@]} -gt 1 ]; then
    tell "Hamlib knows more than one radio called something like $NAME:"
    for i in "${!matches[@]}"; do
      IFS=$'\t' read -r number maker name <<< "${matches[$i]}"
      tell "  $((i + 1))) $maker $name   (model $number)"
    done
    pick="$(ask 'Which one is it? (number)')"
    if [[ "$pick" =~ ^[0-9]+$ ]] && [ "$pick" -ge 1 ] && [ "$pick" -le ${#matches[@]} ]; then
      chosen="${matches[$((pick - 1))]}"
    fi
  fi
  if [ -n "$chosen" ]; then
    IFS=$'\t' read -r model maker name <<< "$chosen"
    if [ "$name" != "$NAME" ]; then NAME="$name"; typed_name=1; fi   # Hamlib's own name for it
  fi
  if [ -z "$model" ]; then
    number="$(ask "Hamlib doesn't list a radio called $NAME. Its Hamlib model number, if you know it (rigctl -l lists them):")"
    if [[ "$number" =~ ^[0-9]+$ ]] && [ "$number" -ge 2 ]; then
      model="$number"
    else
      note "Find the radio's number with:  rigctl -l | grep -i ${NAME%% *}"
      tell "then put it under [$SECTION] in $CONFIG (model = ...) and run this again."
      exit 1
    fi
  fi
fi
[ -n "$NAME" ] || NAME="$WHICH"

# The cable: a USB-serial cable that isn't the other radio's.
other="$(setting "$OTHER" device '')"
other_real="$(readlink -f "$other" 2>/dev/null || true)"
other_name="$(setting "$OTHER" name '')"
current="$(setting "$SECTION" device '')"
cables=()
tell "USB-serial cables plugged in:"
for dev in "$SERIAL_DIR"/*; do
  [ -e "$dev" ] || continue
  if [ -n "$other" ] && { [ "$dev" = "$other" ] || [ "$(readlink -f "$dev")" = "$other_real" ]; }; then
    tell "  $(basename "$dev")   (the ${other_name:-other radio}'s)"
    continue
  fi
  tell "  $(basename "$dev")"
  if [ "$dev" = "$current" ]; then cables=("$dev" "${cables[@]}"); else cables+=("$dev"); fi
done
if [ ${#cables[@]} -eq 0 ]; then
  [ -e "$SERIAL_DIR" ] || tell "  (none)"
  note "The $NAME's cable isn't showing up. Plug its USB end into this computer, then run this again."
  exit 1
fi

# The speeds and handshakes to try; the saved ones first when it was set up before.
combos=()
saved_model="$(setting "$SECTION" model 0)"
if [ "$saved_model" != "0" ] && [ "$saved_model" != "1" ] && [ -n "$(setting "$SECTION" baud '')" ]; then
  combos+=("$(setting "$SECTION" handshake ''):$(setting "$SECTION" baud '')")
fi
for hs in "" hardware none; do
  for baud in $SPEEDS; do
    [[ " ${combos[*]} " == *" $hs:$baud "* ]] || combos+=("$hs:$baud")
  done
done

# Its link holds the cable while it runs, so it's paused for the test.
was_running=0
if systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
  was_running=1
  systemctl --user stop "$SERVICE" 2>/dev/null
fi

tell "Talking to the radio (Hamlib model $model). Each try takes a few seconds; all of them can take a few minutes."
found="" freq="" hs="" baud=""
for dev in "${cables[@]}"; do
  for combo in "${combos[@]}"; do
    hs="${combo%%:*}" baud="${combo#*:}"
    args=(-m "$model" -r "$dev" -s "$baud")
    case "$hs" in
      hardware) args+=(-C serial_handshake=Hardware) ;;
      none) args+=(-C serial_handshake=None) ;;
    esac
    printf '    trying %s at %s baud%s ... ' "$(basename "$dev")" "$baud" "${hs:+, handshake $hs}"
    freq="$(timeout 8 rigctl "${args[@]}" f 2>/dev/null | head -n 1)"
    if [[ "$freq" =~ ^[0-9]+$ ]] && [ "$freq" -gt 0 ]; then
      printf 'it answered\n'
      found="$dev"
      break 2
    fi
    printf 'no answer\n'
  done
done

if [ -z "$found" ]; then
  [ $was_running -eq 1 ] && systemctl --user start "$SERVICE" 2>/dev/null
  note "The $NAME didn't answer at any speed. Things to check:"
  tell "  - the $NAME is switched on"
  tell "  - the cable goes into the radio's computer (CAT) socket, often marked CAT or COM"
  tell "  - some cables need a null modem adapter between the cable and the radio, and some"
  tell "    don't: if one is in, take it out (or put one in), then run this again"
  tell "  - the radio's own CAT speed is one of these: $SPEEDS"
  tell "  - on some radios (the FT-950, for one) a menu setting called CAT RTS must be OFF"
  exit 1
fi

mhz="$(awk -v f="$freq" 'BEGIN { printf "%.5f", f / 1000000 }')"
ok "the $NAME answered on $(basename "$found") at $baud baud${hs:+ (handshake $hs)}; it's on $mhz MHz"
if [ $CHECK -eq 1 ]; then
  [ $was_running -eq 1 ] && systemctl --user start "$SERVICE" 2>/dev/null
  tell "That was only a check. To start using it, run this again without --check."
  exit 0
fi

[ $typed_name -eq 1 ] && python3 "$SET" "$SECTION" name "$NAME"
python3 "$SET" "$SECTION" model "$model"
python3 "$SET" "$SECTION" device "$found"
python3 "$SET" "$SECTION" baud "$baud"
python3 "$SET" "$SECTION" handshake "$hs"
[ -n "$(setting "$SECTION" rigctld_port '')" ] || [ "$SECTION" = radio ] || python3 "$SET" "$SECTION" rigctld_port "$PORT"
ok "saved under [$SECTION] in $CONFIG"
systemctl --user --quiet enable "$SERVICE" 2>/dev/null
systemctl --user restart "$SERVICE" 2>/dev/null
systemctl --user restart station-app 2>/dev/null
ok "its radio link is running, and the station display shows its frequency within a minute"
tell "(The link only reads and tunes the radio. It never transmits.)"
if [ "$SECTION" = radio2 ]; then
  tell "The display shows the radio in use: the one transmitting, or the one tuned last."
  tell "With both on, click the radio's name on the display to show the other one."
fi
