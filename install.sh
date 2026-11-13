#!/usr/bin/env bash
# Station app installer (the ham radio station HUD, first built for VE6LDM).
# Safe to run again: it updates the app in place and never touches the
# logbook or the settings file.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATION="${STATION_DIR:-$HOME/station}"
APP="$STATION/app"
DATA="$STATION/data"
UNITS="$HOME/.config/systemd/user"
VERSION="$(cat "$SRC/VERSION")"
PORT=8073

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m    %s\n' "$*"; }
note() { printf '    \033[33mnote\033[0m  %s\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then
  echo "Please run this as your normal user, without sudo."
  exit 1
fi

# A setting from ~/station/config.ini, for example: setting waterfall start no
# Before the first install, the settings that came with this package are read instead.
settings_file() {
  local f
  for f in "$STATION/config.ini" "$SRC/config.ini" "$SRC/config.example.ini"; do
    if [ -f "$f" ]; then printf '%s' "$f"; return; fi
  done
}
setting() {
  python3 - "$(settings_file)" "$1" "$2" "$3" <<'PY'
import configparser, sys
cp = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
cp.read(sys.argv[1], encoding="utf-8")
print(cp.get(sys.argv[2], sys.argv[3], fallback="").strip() or sys.argv[4])
PY
}
wants_waterfall() {
  case "$(setting waterfall start no | tr '[:upper:]' '[:lower:]')" in no|off|false|0) return 1 ;; esac
}
wants_ft8() {   # [ft8] enabled = yes, and the waterfall (whose receiver WSJT-X hears)
  wants_waterfall || return 1
  case "$(setting ft8 enabled no | tr '[:upper:]' '[:lower:]')" in yes|on|true|1) return 0 ;; esac
  return 1
}

CALL="$(setting station callsign '')"
if [ -n "$CALL" ]; then step "$CALL station app $VERSION"; else step "Station app $VERSION"; fi

if [ -z "${STATION_SKIP_APT:-}" ]; then
  step "Checking system packages (you may be asked for your password)"
  sudo apt-get update -qq
  sudo apt-get install -y -qq chromium curl libhamlib-utils x11-xserver-utils x11vnc >/dev/null
  ok "Chromium, curl, Hamlib, xrandr and screen sharing are installed"
  if sudo apt-get install -y -qq python3-sgp4 >/dev/null 2>&1; then
    ok "satellite orbit library (SGP4)"
  else
    note "the satellite orbit library didn't install; the ISS uses the app's built-in model instead"
  fi
  if wants_waterfall; then   # only with an SDR on the radio's IF ([waterfall] start = yes)
    if sudo apt-get install -y -qq rtl-sdr python3-numpy wmctrl >/dev/null 2>&1; then
      ok "waterfall programs (the RTL-SDR tools and NumPy)"
    else
      note "the waterfall programs didn't install; the waterfall stays off until they do"
    fi
    # The window layout beside the waterfall (bin/windows.py) talks to the desktop with python3-xlib.
    if sudo apt-get install -y -qq python3-xlib >/dev/null 2>&1; then
      ok "the window layout's library (python3-xlib)"
    else
      note "python3-xlib didn't install; windows open wherever they like until it does"
    fi
    # Stop Linux from claiming the RTL-SDR as a TV tuner, which locks the waterfall out.
    if ! grep -rqs '^blacklist dvb_usb_rtl28xxu' /etc/modprobe.d/; then
      echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl-dvb.conf >/dev/null
      note "the SDR needs one restart of the computer before the waterfall can use it"
    fi
  fi
fi

step "Copying the app into $APP"
mkdir -p "$STATION" "$DATA/fonts" "$HOME/station-backups"
rm -rf "$APP.new"
cp -a "$SRC/app" "$APP.new"
printf '%s\n' "$VERSION" > "$APP.new/VERSION"
chmod +x "$APP.new/server.py" "$APP.new/bin/"*
rm -rf "$APP.old"
if [ -d "$APP" ]; then mv "$APP" "$APP.old"; fi
mv "$APP.new" "$APP"
cp "$SRC/HELP.txt" "$STATION/HELP.txt"
[ -f "$SRC/ham-bookmarks.html" ] && cp "$SRC/ham-bookmarks.html" "$STATION/ham-bookmarks.html"
ok "app files in place"

if [ -f "$STATION/config.ini" ]; then
  if ! grep -q '^\[personal\]' "$STATION/config.ini"; then
    printf '\n%s\n' "$(sed -n '/^\[personal\]/,/^helper =/p' "$SRC/config.example.ini")" >> "$STATION/config.ini"
    ok "added the new personal-touch settings to $STATION/config.ini"
  fi
  if ! grep -q '^\[waterfall\]' "$STATION/config.ini"; then
    printf '\n%s\n' "$(sed -n '/^\[waterfall\]/,/^ppm =/p' "$SRC/config.example.ini")" >> "$STATION/config.ini"
    ok "added the waterfall settings to $STATION/config.ini"
  else   # the settings for Gqrx (before 0.14) become the station app's own waterfall settings, keeping their values
    python3 "$APP/bin/update-waterfall-settings.py" "$SRC/config.example.ini" "$STATION/config.ini" \
      | while read -r line; do ok "$line"; done \
      || note "couldn't update the waterfall settings in $STATION/config.ini; the waterfall uses its usual settings"
  fi
  if ! grep -q '^\[satellites\]' "$STATION/config.ini"; then
    printf '\n%s\n' "$(sed -n '/^\[satellites\]/,/^fm =/p' "$SRC/config.example.ini")" >> "$STATION/config.ini"
    ok "added the FM satellite settings to $STATION/config.ini"
  fi
  if ! grep -q '^\[openings\]' "$STATION/config.ini"; then
    printf '\n%s\n' "$(sed -n '/^\[openings\]/,/^enabled =/p' "$SRC/config.example.ini")" >> "$STATION/config.ini"
    ok "added the band openings settings to $STATION/config.ini"
  fi
  if ! grep -q '^\[radio2\]' "$STATION/config.ini"; then
    printf '\n%s\n' "$(sed -n '/^\[radio2\]/,/^bands =/p' "$SRC/config.example.ini")" >> "$STATION/config.ini"
    ok "added the settings for a second radio to $STATION/config.ini"
  fi
  if [ -z "$(setting radio bands '')" ]; then
    python3 "$APP/bin/set-setting.py" radio bands "80m, 40m, 30m, 20m, 17m, 15m, 12m, 10m" \
      --note "$(printf '%s\n%s' "The bands this radio has antennas for. The spot list's band buttons, band openings and" \
                "\"Listen to yourself\" show these bands (and the second radio's).")"
    ok "added the radio's bands (its antennas) to $STATION/config.ini"
  fi
  if [ -z "$(setting cluster backups '')" ]; then
    python3 "$APP/bin/set-setting.py" cluster backups "dxc.ve7cc.net:23, dxc.nc7j.com:7373, k1ttt.net:7373, w3lpl.net:7373" \
      --note "$(printf '%s\n%s' "If that node stops answering or sending spots, the app moves to the next of these" \
                "(host:port), and tries the one above again every hour.")"
    ok "added backup DX clusters to $STATION/config.ini"
  fi
  if [ -z "$(setting display welcome '')" ]; then
    python3 "$APP/bin/set-setting.py" display welcome yes \
      --note "$(printf '%s\n%s' "The welcome banner when the computer starts (click the callsign at the top left to see it again)." \
                "no switches it off; welcome_text is the line under the callsign.")"
    python3 "$APP/bin/set-setting.py" display welcome_text ""
    ok "added the welcome banner's settings to $STATION/config.ini"
  fi
  if [ -z "$(setting display checklist '')" ]; then
    python3 "$APP/bin/set-setting.py" display checklist yes \
      --note "$(printf '%s\n%s' "The Tester checklist link under the greeting: things to try, with notes for whoever" \
                "looks after the station (station feedback shows them). no hides the link.")"
    ok "added the tester checklist's setting to $STATION/config.ini"
  fi
  if ! grep -q '^\[alerts\]' "$STATION/config.ini"; then
    printf '\n%s\n' "$(sed -n '/^\[alerts\]/,/^space_weather =/p' "$SRC/config.example.ini")" >> "$STATION/config.ini"
    ok "added the weather and space weather alert settings to $STATION/config.ini"
  fi
  if [ -f "$SRC/config.ini" ]; then   # this package came with saved settings: add any that are missing
    python3 "$APP/bin/merge-settings.py" "$SRC/config.ini" | while read -r line; do ok "$line"; done
  fi
  ok "kept your settings in $STATION/config.ini"
  if [ "$(setting radio model '')" = "1" ]; then
    python3 "$APP/bin/set-setting.py" radio model 0
    ok "switched off the practice radio (the setup helper connects your radio)"
  fi
elif [ -f "$SRC/config.ini" ]; then
  cp "$SRC/config.ini" "$STATION/config.ini"
  ok "restored the saved settings that came with this package in $STATION/config.ini"
else
  cp "$SRC/config.example.ini" "$STATION/config.ini"
  ok "created $STATION/config.ini"
fi

step "Downloading the country file and the display fonts"
fetch() {
  curl -fsSL --retry 2 --connect-timeout 15 --max-time 120 -A "ham-radio-hud/$VERSION" -o "$2.part" "$1" \
    && mv "$2.part" "$2"
}
if fetch "https://www.country-files.com/cty/cty.dat" "$DATA/cty.dat"; then
  ok "country file"
else
  rm -f "$DATA/cty.dat.part"
  if [ -s "$DATA/cty.dat" ]; then
    note "couldn't refresh the country file, so the one already here is kept"
  else
    # Some ham programs ship a copy; use one if it's on this computer.
    for copy in /usr/share/tlf/cty.dat /usr/share/wsjtx/cty.dat /usr/share/cqrlog/ctyfiles/cty.dat; do
      if [ -s "$copy" ]; then cp "$copy" "$DATA/cty.dat"; break; fi
    done
    if [ -s "$DATA/cty.dat" ]; then
      note "used a copy of the country file already on this computer; the app updates it later"
    else
      note "couldn't download the country file; the app keeps trying every hour"
    fi
  fi
fi
FONTS="https://raw.githubusercontent.com/google/fonts/main/ofl"
missing=0
for f in b612/B612-Regular b612/B612-Bold b612/B612-Italic b612mono/B612Mono-Regular b612mono/B612Mono-Bold; do
  name="${f#*/}.ttf"
  if [ ! -s "$DATA/fonts/$name" ]; then
    fetch "$FONTS/$f.ttf" "$DATA/fonts/$name" || { rm -f "$DATA/fonts/$name.part"; missing=1; }
  fi
done
if [ "$missing" -eq 0 ]; then ok "B612 fonts"; else note "some fonts didn't download; the display uses a standard font until the next install"; fi

step "Setting up the background services"
mkdir -p "$UNITS"
cp "$SRC/systemd/station-app.service" "$SRC/systemd/station-rigctld.service" \
   "$SRC/systemd/station-backup.service" "$SRC/systemd/station-backup.timer" \
   "$SRC/systemd/station-waterfall.service" "$SRC/systemd/station-display.service" \
   "$SRC/systemd/station-rigctld2.service" "$SRC/systemd/station-windows.service" "$UNITS/"
WATERFALL="$(setting waterfall start no | tr '[:upper:]' '[:lower:]')"
if [ -z "${STATION_SKIP_SYSTEMD:-}" ]; then
  systemctl --user daemon-reload
  # The waterfall's window lets go of the SDR first (before 0.14 it was Gqrx, which held it), so the
  # app's own waterfall can have it as soon as the app starts. It opens again further down.
  systemctl --user stop station-waterfall.service 2>/dev/null || true
  systemctl --user --quiet enable station-app.service station-rigctld.service station-rigctld2.service station-backup.timer
  systemctl --user restart station-app.service station-rigctld.service station-rigctld2.service
  systemctl --user start station-backup.timer
  if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q 'Linger=yes'; then
    sudo loginctl enable-linger "$USER"
  fi
  ok "the app, the radio links and the nightly backup are running"
  if [ "$(setting radio2 model 0)" = "0" ] && [ -n "$(setting radio2 name '')" ]; then
    note "the $(setting radio2 name '') isn't connected yet; once its cable is in, run ~/station/app/bin/radio2.sh (remotely: station radio2)"
  fi
  case "$WATERFALL" in
    no|off|false|0)
      systemctl --user --quiet disable --now station-waterfall.service station-windows.service 2>/dev/null || true
      note "the waterfall is off ([waterfall] start = no in config.ini; it needs an SDR on the radio's IF)"
      ;;
    *)
      if command -v rtl_tcp >/dev/null 2>&1 && python3 -c "import numpy" 2>/dev/null; then
        systemctl --user --quiet enable station-waterfall.service
        systemctl --user restart station-waterfall.service
        ok "the waterfall opens on the computer's own screen and reopens if it's closed"
      else
        note "the waterfall's programs (rtl-sdr and python3-numpy) aren't installed, so it can't start yet"
      fi
      # The window layout: what opens on that screen goes beside the waterfall ([layout] in config.ini).
      case "$(setting layout keep yes | tr '[:upper:]' '[:lower:]')" in
        no|off|false|0)
          systemctl --user --quiet disable --now station-windows.service 2>/dev/null || true
          note "the window layout is off ([layout] keep = no in config.ini)"
          ;;
        *)
          if python3 -c "import Xlib" 2>/dev/null; then
            systemctl --user --quiet enable station-windows.service
            systemctl --user restart station-windows.service
            ok "what opens on the computer's own screen goes beside the waterfall, which makes room for it"
          else
            note "python3-xlib isn't installed, so windows open wherever they like for now"
          fi
          ;;
      esac
      ;;
  esac
fi

step "Setting up the big-screen display"
# Since 0.9.0 a background service opens the display (and reopens it if it's closed), so the
# start-up entry older versions used is removed. The menu entry asks the service to reopen it.
rm -f "$HOME/.config/autostart/station-display.desktop"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/station-display.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Station display
Comment=Open (or reopen) the station display on the big screen
Exec=systemctl --user restart station-display.service
Icon=network-wireless
Terminal=false
Categories=Utility;
EOF
cat > "$HOME/.local/share/applications/station-display-close.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Close station display
Comment=Close the station display until it's opened again (or the computer restarts)
Exec=systemctl --user stop station-display.service
Icon=window-close
Terminal=false
Categories=Utility;
EOF
if [ -z "${STATION_SKIP_SYSTEMD:-}" ]; then
  systemctl --user --quiet enable station-display.service
  case "$(setting display keep_open yes | tr '[:upper:]' '[:lower:]')" in
    no|off|false|0) ok "opens by itself after a restart, and from the menu as Station display" ;;
    *) ok "opens by itself after a restart and reopens if it's closed; also in the menu as Station display" ;;
  esac
fi

if wants_ft8; then
  step "FT8 with WSJT-X (receive only)"
  # WSJT-X 3.0.2 from its makers (built on Ubuntu 24.04, which Mint 22 is based on). Ubuntu's own copy is
  # a test version (2.7.0-rc3) that may refuse to start after its expiry date, so it isn't used.
  WSJTX_VERSION=3.0.2
  WSJTX_DEB="wsjtx-$WSJTX_VERSION-linux-x86_64.deb"
  WSJTX_URL="https://github.com/WSJTX/wsjtx/releases/download/v$WSJTX_VERSION/$WSJTX_DEB"
  if [ -z "${STATION_SKIP_APT:-}" ]; then
    if command -v pactl >/dev/null 2>&1 || sudo apt-get install -y -qq pulseaudio-utils >/dev/null 2>&1; then
      ok "pactl (makes WSJT-X's virtual microphone and speaker)"
    else
      note "pactl (pulseaudio-utils) didn't install, so WSJT-X's microphone can't be made yet; run the installer again later"
    fi
    have="$(dpkg-query -W -f='${Version}' wsjtx 2>/dev/null || true)"
    if [ -n "$have" ] && dpkg --compare-versions "$have" ge "$WSJTX_VERSION"; then
      ok "WSJT-X $have"
    elif [ "$(dpkg --print-architecture)" != "amd64" ]; then
      note "WSJT-X wasn't installed: its package is for amd64 computers, and this is $(dpkg --print-architecture)"
    else
      tmp="$(mktemp -d)"
      chmod 755 "$tmp"   # so apt's own user can read the package
      # A big download (tens of MB): up to 20 minutes, for a slow line.
      if curl -fsSL --retry 2 --connect-timeout 15 --max-time 1200 -A "ham-radio-hud/$VERSION" \
           -o "$tmp/$WSJTX_DEB" "$WSJTX_URL" \
         && [ "$(dpkg-deb -f "$tmp/$WSJTX_DEB" Package 2>/dev/null)" = "wsjtx" ] \
         && [ "$(dpkg-deb -f "$tmp/$WSJTX_DEB" Version 2>/dev/null)" = "$WSJTX_VERSION" ]; then
        chmod 644 "$tmp/$WSJTX_DEB"
        if out="$(sudo apt-get install -y -qq "$tmp/$WSJTX_DEB" 2>&1)"; then
          ok "WSJT-X $WSJTX_VERSION"
        else
          note "WSJT-X $WSJTX_VERSION didn't install ($(printf '%s' "$out" | tail -n 1)); run the installer again later"
        fi
      else
        note "couldn't download WSJT-X $WSJTX_VERSION (from github.com/WSJTX/wsjtx); run the installer again later"
      fi
      rm -rf "$tmp"
    fi
  fi
  python3 "$APP/bin/wsjtx-preset.py" | while read -r line; do ok "$line"; done \
    || note "couldn't give WSJT-X its settings; see HELP.txt (FT8) to set them in WSJT-X"
  # The menu entry: the same file name as WSJT-X's own (so it takes its place), opening WSJT-X beside the
  # station display rather than over it.
  cat > "$HOME/.local/share/applications/wsjtx.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=WSJT-X (FT8)
Comment=FT8 and the other digital modes, receive only, heard through the waterfall's receiver
Exec=$APP/bin/wsjtx.sh
Icon=wsjtx_icon
Terminal=false
Categories=AudioVideo;Audio;HamRadio;
StartupNotify=true
EOF
  ok "in the menu as WSJT-X (FT8); it opens on the computer's own screen, beside the station display"
fi

# Mint's desktop effects (compositing) hide windows from screen sharing, so "station screen"
# from another computer only showed the wallpaper. With them off, it shows the real screens.
if [ -z "${STATION_SKIP_DISPLAY:-}" ] && command -v xfconf-query >/dev/null 2>&1; then
  BUS="/run/user/$(id -u)/bus"
  xq() { DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS="unix:path=$BUS" xfconf-query -c xfwm4 -p /general/use_compositing "$@"; }
  if [ -S "$BUS" ] && [ "$(xq 2>/dev/null)" = "true" ]; then
    if xq -s false 2>/dev/null; then
      ok "desktop effects off, so remote screen sharing shows the windows"
    else
      note "couldn't switch the desktop effects off; screen sharing may only show the wallpaper"
    fi
  fi
fi

step "Checking that everything answers"
if [ -z "${STATION_SKIP_SYSTEMD:-}" ]; then
  up=0
  for _ in $(seq 1 20); do
    if curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/health"; then up=1; break; fi
    sleep 1
  done
  if [ "$up" -eq 1 ]; then
    ok "the app answers at http://127.0.0.1:$PORT"
  else
    note "the app hasn't answered yet; see: journalctl --user -u station-app -n 30"
  fi
fi
if python3 "$APP/bin/backup.py" >/dev/null; then
  ok "backup written to ~/station-backups"
else
  note "the first backup didn't run; it will try again tonight"
fi

if [ -z "${STATION_SKIP_SYSTEMD:-}" ] && [ -z "${STATION_SKIP_DISPLAY:-}" ]; then
  # Close a display an older version opened, then let the service open the new one.
  pkill -f -- "--user-data-dir=$HOME/.config/station-display" 2>/dev/null || true
  sleep 1
  systemctl --user restart station-display.service
  ok "opening the station display on the big screen"
fi

step "Done"
echo "    Version $VERSION is installed."
if [ -z "$(setting station callsign '')" ]; then
  echo "    Next: run the setup helper for your callsign, location and radio:"
  echo "          $APP/bin/setup.sh"
fi
echo "    Settings:        $STATION/config.ini"
echo "    Handy commands:  $STATION/HELP.txt"
