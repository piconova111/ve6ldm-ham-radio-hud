#!/usr/bin/env bash
# Setup helper for the station app. Walks through each job one at a time:
#   1 internet   2 callsign and name   3 location   4 the radio   5 the callbook (optional)
#   6 remote access (optional)   7 start by itself after power-on
# Safe to run again any time. Press Enter (or type skip) to leave a step as it is.
set -uo pipefail

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SET="$APP/bin/set-setting.py"
CONFIG="${STATION_DIR:-$HOME/station}/config.ini"

title() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
ok()    { printf '    \033[32mok\033[0m    %s\n' "$*"; }
note()  { printf '    \033[33mnote\033[0m  %s\n' "$*"; }
tell()  { printf '    %s\n' "$*"; }
ask()   { local reply; read -r -p "    $1 " reply; printf '%s' "$reply"; }
setting() {
  python3 - "$CONFIG" "$1" "$2" "$3" <<'PY'
import configparser, sys
cp = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
cp.read(sys.argv[1], encoding="utf-8")
print(cp.get(sys.argv[2], sys.argv[3], fallback="").strip() or sys.argv[4])
PY
}

if [ "$(id -u)" -eq 0 ]; then echo "Please run this as your normal user, without sudo."; exit 1; fi

# ---------------------------------------------------------------------------
title "Step 1 of 7: Internet"
while ! curl -fsS --max-time 10 -o /dev/null https://www.hamqsl.com/solarxml.php 2>/dev/null; do
  note "No internet yet."
  tell "Click the network icon at the bottom right of the screen, pick your Wi-Fi"
  tell "and type its password. (Or plug in the network cable.)"
  [ "$(ask 'Press Enter to check again, or type skip:')" = "skip" ] && break
done
curl -fsS --max-time 10 -o /dev/null https://www.hamqsl.com/solarxml.php 2>/dev/null && ok "the internet is working"

# ---------------------------------------------------------------------------
title "Step 2 of 7: Your callsign and name"
current="$(setting station callsign '')"
while true; do
  call="$(ask "Your callsign${current:+ (Enter keeps $current)}:")"
  call="$(printf '%s' "$call" | tr '[:lower:]' '[:upper:]' | tr -d ' ')"
  if [ -z "$call" ] || [ "$call" = "SKIP" ]; then
    [ -n "$current" ] && ok "callsign $current" || note "No callsign yet: the display shows N0CALL and live DX spots stay off."
    break
  fi
  if [[ "$call" =~ ^[A-Z0-9/]{3,15}$ ]] && [[ "$call" =~ [0-9] ]] && [[ "$call" =~ [A-Z] ]]; then
    python3 "$SET" station callsign "$call"
    ok "callsign $call saved"
    break
  fi
  note "That doesn't look like a callsign (letters and numbers, like VE6LDM). Try again, or press Enter to skip."
done
oper="$(setting station operator '')"
name="$(ask "Your first name, for the greeting${oper:+ (Enter keeps $oper)}:")"
if [ -n "$name" ] && [ "$name" != "skip" ]; then
  python3 "$SET" station operator "$name"
  ok "the display will say hello to $name"
fi

# ---------------------------------------------------------------------------
title "Step 3 of 7: Location"
tell "This makes the beam headings, distances and satellite passes exact for the station."
tell "Easiest: open a map website, right-click the station's spot, and copy the coordinates"
tell "(like 52.2681, -113.8112). A grid square works too (like DO32ab)."
while true; do
  place="$(ask 'Paste the coordinates or grid square (Enter to skip):')"
  [ -z "$place" ] || [ "$place" = "skip" ] && { tell "Location left as it was."; break; }
  description="$(python3 "$SET" --location "$place" --check)"; status=$?
  if [ $status -ne 0 ]; then note "$description"; continue; fi
  tell "That's $description."
  python3 "$SET" --location "$place" | while read -r line; do ok "$line"; done
  break
done

# ---------------------------------------------------------------------------
title "Step 4 of 7: The radio"
# Using the radio's serial port needs membership of the dialout group, and a new
# group only takes effect after a restart.
if ! id -nG | grep -qw dialout; then
  if ! id -nG "$USER" | grep -qw dialout; then
    sudo usermod -aG dialout "$USER" && ok "allowed $USER to use the radio's port"
  fi
  note "The radio's port can be used after a restart. Restart the computer, then run"
  tell "this helper again. Press Enter at the steps that are already done."
else
  tell "Plug the radio's computer (CAT) cable into this computer and turn the radio on."
  tell "The helper finds the cable and the speed the radio answers at. (The station app only"
  tell "reads and tunes the radio. It never transmits.)"
  while true; do
    [ "$(ask 'Press Enter when that is done, or type skip:')" = "skip" ] && break
    "$APP/bin/radio.sh" radio && break
  done
fi

# ---------------------------------------------------------------------------
title "Step 5 of 7: The callbook (optional)"
tell "A free HamQTH account adds names and exact locations to look-ups. Sign up at"
tell "www.hamqth.com (with your callsign and email), then type the details here."
while true; do
  user="$(ask 'HamQTH username (Enter to skip):')"
  [ -z "$user" ] || [ "$user" = "skip" ] && { tell "Callbook left as it was."; break; }
  read -r -s -p "    HamQTH password: " pass; echo
  if HQ_USER="$user" HQ_PASS="$pass" python3 -c "
import os, sys
sys.path.insert(0, '$APP')
from station import hamqth
hamqth.HamQTH(os.environ['HQ_USER'], os.environ['HQ_PASS'])._session_id(renew=True)
" 2>/dev/null; then
    python3 "$SET" hamqth username "$user"
    SETTING_VALUE="$pass" python3 "$SET" hamqth password
    ok "HamQTH accepted the login; look-ups will now show names and towns"
    break
  else
    note "HamQTH didn't accept that username and password. Try again, or press Enter to skip."
  fi
done

# ---------------------------------------------------------------------------
title "Step 6 of 7: Remote access (optional)"
tell "With Tailscale (free for personal use), someone can look after this station from"
tell "another computer, for example with the station command in the app's remote folder."
ts_name() {   # the name Tailscale gave this computer
  tailscale status --json 2>/dev/null \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["Self"]["DNSName"].split(".")[0])' 2>/dev/null
}
ts_report() {
  local name; name="$(ts_name)"
  ok "Tailscale is connected; your other computers can reach this one as $USER@${name:-$(hostname)}"
}
if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
  ts_report
elif [ "$(ask 'Set up remote access with Tailscale? (y/n)')" = "y" ]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    tell "Installing Tailscale (you may be asked for your password)..."
    if curl -fsSL https://tailscale.com/install.sh | sh; then
      ok "Tailscale is installed"
    else
      note "Tailscale didn't install. Check the internet, then run this helper again."
    fi
  fi
  if command -v tailscale >/dev/null 2>&1; then
    tell "Tailscale needs signing in once. A link will appear: open it in a browser"
    tell "(here or on your phone) and sign in with your Tailscale account."
    if sudo tailscale up && tailscale status >/dev/null 2>&1; then
      ts_report
    else
      note "Tailscale didn't connect. You can run this helper again later."
    fi
  fi
else
  tell "Remote access left as it was."
fi

# ---------------------------------------------------------------------------
title "Step 7 of 7: Start by itself after power-on"
if [ -f /etc/lightdm/lightdm.conf.d/90-station-autologin.conf ]; then
  ok "automatic login is already on"
elif [ "$(ask 'Turn on automatic login, so the display comes up by itself? (y/n)')" = "y" ]; then
  "$APP/bin/autologin.sh" on
else
  tell "Automatic login left off. Someone will need to type the password after a restart."
fi

# ---------------------------------------------------------------------------
title "Finishing"
systemctl --user restart station-app station-rigctld 2>/dev/null && ok "restarted the app with the new settings"
sleep 3
"$APP/bin/status.sh"
echo
tell "All done. To be sure everything starts by itself, restart the computer once:"
tell "    sudo reboot"
