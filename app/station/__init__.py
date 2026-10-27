"""The station app's modules: the ham radio station HUD, first built for VE6LDM."""

from pathlib import Path as _Path

PROJECT_URL = "https://github.com/piconova111/ve6ldm-ham-radio-hud"
try:
    VERSION = (_Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    VERSION = "dev"
# How the app introduces itself to the websites it gets data from.
USER_AGENT = "ham-radio-hud/%s (+%s)" % (VERSION, PROJECT_URL)
