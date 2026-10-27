#!/usr/bin/env bash
# Connects the second radio ([radio2] in ~/station/config.ini): the same as "radio.sh radio2".
#   radio2.sh           find it, save it, and show it on the station display
#   radio2.sh --check   only test the cable and the radio; nothing is changed
# Remotely:  station radio2   (or station radio2 --check)
exec "$(dirname "${BASH_SOURCE[0]}")/radio.sh" radio2 "$@"
