#!/usr/bin/env bash
# Turns automatic login on or off for this account, so after power-on the
# station display comes up without anyone typing a password.
#   autologin.sh on     autologin.sh off
set -euo pipefail

FILE=/etc/lightdm/lightdm.conf.d/90-station-autologin.conf

case "${1:-on}" in
  on)
    sudo mkdir -p /etc/lightdm/lightdm.conf.d
    printf '[Seat:*]\nautologin-user=%s\nautologin-user-timeout=0\n' "$USER" | sudo tee "$FILE" >/dev/null
    echo "Automatic login is on for $USER. It takes effect from the next restart."
    ;;
  off)
    sudo rm -f "$FILE"
    echo "Automatic login is off. The password will be asked for at the next restart."
    ;;
  *)
    echo "Use:  autologin.sh on   or   autologin.sh off"
    exit 1
    ;;
esac
