"""Starting programs on the station computer's own screen from the station app.

The app runs as a background service, so a program it starts needs the desktop's settings: which screen, the
key to it, the sound system's folder and so on. The best source is the desktop itself: the environment of the
user's desktop shell (Cinnamon on Mint), which is what its menu starts programs with. Failing that, the screen
(:0), its key (~/.Xauthority) and the session's folders are filled in by hand, as favourites.py does for the
web browser.

A program the app starts also counts as part of the app's service, so restarting the app (for an update, say)
would close it. With scope=True, start() gives it a scope of its own (systemd-run --user --scope), as the
desktop does for programs from its menu, so it stays open; where the user's service manager can't be reached,
it's started directly instead.
"""
import os
import shutil
import subprocess
from pathlib import Path

# Desktop shells whose environment is the desktop's own (the first one found that has a screen is used)
SHELLS = ("cinnamon", "gnome-shell", "xfce4-session", "mate-session", "plasmashell", "lxsession", "budgie-wm")


def session_env(proc="/proc"):
    """The environment of this user's desktop shell, or None if none is running (or it has no screen)."""
    uid = os.getuid()
    try:
        names = os.listdir(proc)
    except OSError:
        return None
    for name in sorted(names):
        if not name.isdigit():
            continue
        base = os.path.join(proc, name)
        try:
            if os.stat(base).st_uid != uid:
                continue
            with open(os.path.join(base, "comm"), encoding="utf-8", errors="replace") as f:
                if f.read().strip() not in SHELLS:
                    continue
            with open(os.path.join(base, "environ"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        env = {}
        for item in raw.split(b"\0"):
            key, sep, value = item.partition(b"=")
            if sep and key:
                env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
        if env.get("DISPLAY"):
            return env
    return None


def screen_env(proc="/proc"):
    """An environment for a program on the station's screen: the desktop's own if it can be read, otherwise
    this one with the screen, its key and the session's folders filled in."""
    env = session_env(proc)
    if env is None:
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":0")
    env.setdefault("HOME", str(Path.home()))
    xauth = Path(env["HOME"]) / ".Xauthority"
    if "XAUTHORITY" not in env and xauth.exists():
        env["XAUTHORITY"] = str(xauth)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    runtime = env["XDG_RUNTIME_DIR"]
    if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(os.path.join(runtime, "bus")):
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/bus" % runtime
    return env


def start(cmd, env, scope=False, out=None, description=None):
    """Starts cmd (a list of words) with env, its output to the file out (or nowhere). With scope, it gets a
    scope of its own when the user's service manager is there to make one. Returns (the process, True if it
    went through systemd-run): the caller can tell that systemd-run failed from it ending at once."""
    scoped = False
    if scope:
        run = shutil.which("systemd-run", path=env.get("PATH"))
        runtime = env.get("XDG_RUNTIME_DIR") or ""
        if run and runtime and os.path.exists(os.path.join(runtime, "systemd", "private")):
            words = [run, "--user", "--scope", "--quiet", "--collect"]
            if description:
                words.append("--description=" + description)
            cmd = words + ["--"] + list(cmd)
            scoped = True
    sink = out if out is not None else subprocess.DEVNULL
    child = subprocess.Popen(list(cmd), env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT
                             if out is not None else subprocess.DEVNULL, start_new_session=True, close_fds=True)
    return child, scoped
