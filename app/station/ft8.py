"""FT8 (and the other digital modes) with WSJT-X, heard through the waterfall's receiver.

WSJT-X, the standard program for FT8, decodes the radio's sound. The FT-950 has no sound card of its own, but
the waterfall's receiver hears everything the radio hears, so the station app gives WSJT-X a virtual
microphone: PipeWire's pipe source "station_receiver" (shown as "Station receiver"), which plays whatever is
written into a pipe (receiver.fifo in the data folder). While WSJT-X runs, the app writes the radio's
frequency into it as USB (100 to 3100 Hz), 12,000 samples a second, at a steady level for WSJT-X's meter
(about 30 dB), and silence while the radio transmits. (See listen.py for how the sound is made.)

There's a virtual speaker too, "station_no_transmit" ("Station (no transmit)"), for WSJT-X's transmit sound:
it goes nowhere, so until the radio has an interface for digital modes, WSJT-X can't transmit through it.
(WSJT-X lists the two by these names, station_receiver and station_no_transmit; other programs show the
descriptions in brackets.)

WSJT-X follows the radio (frequency and mode) through a receive-only radio link (catguard.py, port 4534),
which passes its reads and tuning on to rigctld but refuses anything that would make the radio transmit.

The waterfall's FT8 button opens WSJT-X through the menu's own launcher (bin/wsjtx.sh), or brings it to the
front if it's open; WSJT-X then gets a scope of its own, so restarting the app doesn't close it (desktop.py).

The pipe is kept small (about a third of a second), so the sound reaches WSJT-X with little delay (FT8 needs
its timing right to within a second or so); when it's full, a tenth of a second is dropped rather than
letting the delay grow. When the receiver stops, silence keeps WSJT-X's sound going.

It only listens. Nothing here sends anything to the radio.
"""
import configparser
import json
import errno
import fcntl
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

from . import catguard, desktop

SOURCE = "station_receiver"
SINK = "station_no_transmit"
RATE = 12_000                 # the sound's samples a second (listen.OUT_RATE), 16-bit, one channel
PIPE_BYTES = 8192             # about a third of a second at 24,000 bytes a second
CHECK_EVERY = 5.0             # seconds between looks for WSJT-X and the virtual microphone
DEFAULTS_WATCH = 30.0         # seconds after making the virtual devices to keep the computer's own as defaults
SILENT_AFTER = 0.5            # seconds without sound from the receiver before silence is written instead
F_SETPIPE_SZ = getattr(fcntl, "F_SETPIPE_SZ", 1031)
LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "wsjtx.sh"   # the menu's WSJT-X (FT8)
WINDOWS = Path(__file__).resolve().parent.parent / "bin" / "windows.py"   # the window layout (station/layout.py)
OPENING_WAIT = 20.0           # seconds after the FT8 button opens WSJT-X when pressing it again doesn't open another


def wsjtx_ini_path():
    """Where WSJT-X keeps its settings on Linux (Qt's configuration folder, then WSJT-X.ini)."""
    if os.environ.get("STATION_WSJTX_INI"):
        return Path(os.environ["STATION_WSJTX_INI"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "WSJT-X.ini"


def wsjtx_settings(path, link_port, rig_port):
    """What WSJT-X's own settings say about the radio and sound (for the health report): None if WSJT-X has
    no settings file yet."""
    path = Path(path)
    if not path.exists():
        return None
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError) as exc:
        return {"readable": False, "why": str(exc)[:120]}
    get = lambda key: cp.get("Configuration", key, fallback="").strip().strip('"')
    rig, server = get("Rig"), get("CATNetworkPort")
    host, _, port = server.rpartition(":")
    local = host in ("127.0.0.1", "localhost", "::1", "")
    if rig in ("", "None"):
        radio = "none"            # WSJT-X doesn't follow the radio (and can't key it)
    elif rig == "Hamlib NET rigctl" and local and port == str(link_port):
        radio = "link"            # through the receive-only link
    elif rig == "Hamlib NET rigctl" and local and port in ("", str(rig_port)):
        radio = "direct"          # straight to rigctld: it could transmit
    else:
        radio = "other"           # something else (another program, or the radio's own port)
    return {"readable": True, "radio": radio, "rig": rig, "server": server, "input": get("SoundInName"),
            "output": get("SoundOutName"), "call": get("MyCall"), "grid": get("MyGrid"),
            "psk_reporter": get("PSKReporter").lower() == "true",   # Settings, Reporting: shares what it hears
            # Settings, Reporting, UDP Server: where WSJT-X sends its reports (WSJT-X's defaults if not saved yet)
            "udp_server": get("UDPServer") or "127.0.0.1", "udp_port": get("UDPServerPort") or "2237"}


class Ft8:
    def __init__(self, listening, data_dir, say=print, enabled=True, pactl=None, proc="/proc", rig_port=4532,
                 link_port=4534, wsjtx_ini=None, launcher=None, windows=None):
        self.listening = listening
        self.launcher = Path(launcher) if launcher else LAUNCHER
        self.windows = Path(windows) if windows else WINDOWS
        self.open_lock = threading.Lock()   # the FT8 button, pressed twice at once
        self.opening_until = 0.0            # until when a press doesn't open WSJT-X again (it's still starting)
        self.fifo = Path(data_dir) / "receiver.fifo"
        self.say = say
        self.enabled = enabled
        self.pactl = pactl if pactl is not None else shutil.which("pactl")
        self.proc = proc
        self.rig_port, self.link_port = rig_port, link_port
        self.wsjtx_ini = Path(wsjtx_ini) if wsjtx_ini else wsjtx_ini_path()
        self.link = catguard.CatGuard(port=link_port, rig_port=rig_port, say=say)
        self.lock = threading.Lock()   # writes to the pipe, from two threads
        self.fd = None
        self.lst = None
        self.since = None
        self.written = 0               # bytes of sound handed to WSJT-X's microphone
        self.dropped = 0               # tenths of a second dropped because the pipe was full, since the feed started
        self.silence = 0               # bytes of silence written while the receiver gave nothing
        self.last = 0.0                # when sound from the receiver last came
        self.state = {"wsjtx": False, "mic": False, "speaker": False, "problem": None}
        self.defaults = None           # (until when, the computer's own default speaker and microphone)

    # ---------- what the server and the health report get ----------

    def status(self):
        return dict(self.state, on=self.enabled, installed=bool(shutil.which("wsjtx")), feeding=self.lst is not None,
                    since=round(self.since) if self.since else None, seconds=round(self.written / (2 * RATE)),
                    dropped=self.dropped, silent_s=round(self.silence / (2 * RATE), 1),
                    level_db=(self.lst.info.get("level_db") if self.lst is not None else None),
                    heard_hz=(self.lst.info.get("heard_hz") if self.lst is not None else None),
                    link=self.link.status() if self.enabled else None,
                    wsjtx_settings=wsjtx_settings(self.wsjtx_ini, self.link_port, self.rig_port) if self.enabled else None)

    def start(self):
        if self.enabled:
            self.link.start()
            threading.Thread(target=self._run, daemon=True, name="ft8").start()
        return self

    # ---------- the waterfall's FT8 button ----------

    def show(self):
        """The waterfall's FT8 button: opens WSJT-X on the computer's own screen; if it's open, brings it to the
        front, or puts it away (minimized) if it's in front already. What it did goes back to the button:
        "opening", "front" or "away"."""
        if not self.enabled:
            return {"ok": False, "error": "FT8 is turned off (enabled = no under [ft8] in config.ini)."}
        if not shutil.which("wsjtx"):
            return {"ok": False, "error": "WSJT-X isn't installed yet (the station installer adds it)."}
        if not self.launcher.is_file():
            return {"ok": False, "error": "WSJT-X's launcher (%s) is missing: run the station installer again."
                    % self.launcher}
        with self.open_lock:
            if self.wsjtx_running():
                action = "front"
            elif time.time() < self.opening_until:
                return {"ok": True, "action": "opening"}   # pressed again while it starts: one WSJT-X is plenty
            else:
                action = "opening"
                self.opening_until = time.time() + OPENING_WAIT
        if action == "opening":
            self.say("FT8: opening WSJT-X (the waterfall's FT8 button)")
        else:
            turned = self._turn()
            if turned in ("front", "away"):
                return {"ok": True, "action": turned}
            # it couldn't tell (no python3-xlib, or WSJT-X's windows not up yet): the launcher brings it forward
        threading.Thread(target=self._launch, args=(action == "opening",), daemon=True, name="wsjtx-open").start()
        return {"ok": True, "action": action}

    def _turn(self):
        """WSJT-X open: brings it to the front, or puts it away if it's in front already (bin/windows.py ft8, on
        the desktop's own screen). Its answer, "front" or "away"; None if it couldn't say."""
        if not self.windows.is_file():
            return None
        try:
            done = subprocess.run([shutil.which("python3") or "/usr/bin/python3", str(self.windows), "ft8"],
                                  env=desktop.screen_env(self.proc), capture_output=True, text=True, timeout=8)
            answer = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            self.say("FT8: couldn't ask the window layout about WSJT-X (%s)" % exc)
            return None
        if answer.get("action") in ("front", "away"):
            if answer["action"] == "away":
                self.say("FT8: put WSJT-X away (the waterfall's FT8 button); it keeps listening")
            return answer["action"]
        if answer.get("error"):
            self.say("FT8: the window layout couldn't help (%s)" % answer["error"])
        return None

    def _launch(self, opening):
        """Runs WSJT-X's launcher (bin/wsjtx.sh, the menu's WSJT-X (FT8)) on the computer's own screen: it opens
        WSJT-X, or brings it to the front if it's open. When it opens it, WSJT-X gets a scope of its own, so
        restarting the app doesn't close it, and its main window is brought to the front once it appears. The
        launcher's messages go to wsjtx-open.log in the data folder."""
        env = desktop.screen_env(self.proc)
        if opening:
            env["STATION_WSJTX_RAISE"] = "1"
        cmd = [shutil.which("bash") or "/bin/bash", str(self.launcher)]
        try:
            out = open(self.fifo.parent / "wsjtx-open.log", "w")
        except OSError:
            out = None
        try:
            child, scoped = desktop.start(cmd, env, scope=opening, out=out,
                                          description="WSJT-X (FT8), opened from the station's waterfall")
            try:
                code = child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                code = None   # still going: the launcher keeps an eye on WSJT-X's windows for half a minute
            if code and scoped:
                # The launcher doesn't end with an error this fast, so it was systemd-run that couldn't make the
                # scope: open WSJT-X directly instead (it then closes if the app restarts)
                self.say("FT8: systemd-run couldn't give WSJT-X a scope of its own (it ended with %d), so WSJT-X was "
                         "opened directly: it will close if the station app restarts" % code)
                child, _ = desktop.start(cmd, env, out=out)
                code = None
        except OSError as exc:
            self.say("FT8: couldn't run WSJT-X's launcher (%s)" % exc)
            with self.open_lock:
                self.opening_until = 0.0
            return
        finally:
            if out is not None:
                out.close()
        if code is None:
            child.wait()   # this thread tidies up after the launcher when it ends

    # ---------- the work ----------

    def _run(self):
        next_check = 0.0
        while True:
            now = time.time()
            if now >= next_check:
                next_check = now + CHECK_EVERY
                try:
                    self._check()
                except Exception as exc:   # a check that goes wrong mustn't stop the next one
                    self._problem("the check went wrong (%s)" % exc)
            self._fill(now)
            time.sleep(0.25)

    def _problem(self, text):
        if text != self.state.get("problem") and text:
            self.say("FT8: %s" % text)
        self.state["problem"] = text

    def _check(self):
        running = self.wsjtx_running()
        self.state["wsjtx"] = running
        if self.lst is not None and self.fd is None:   # the sound system stopped reading the pipe: start again
            self._stop_feed("the virtual microphone stopped reading")
            self._reset_mic()
        mic = self._devices()
        if running and mic and self.lst is None:
            self._start_feed()
        elif self.lst is not None and not (running and mic):
            self._stop_feed("WSJT-X has closed" if not running else "the virtual microphone has gone")

    def wsjtx_running(self):
        """Is WSJT-X running (a process called wsjtx)?"""
        try:
            names = os.listdir(self.proc)
        except OSError:
            return False
        for name in names:
            if name.isdigit():
                try:
                    with open(os.path.join(self.proc, name, "comm"), encoding="utf-8", errors="replace") as f:
                        if f.read().strip() == "wsjtx":
                            return True
                except OSError:
                    continue
        return False

    def _pactl(self, *args):
        try:
            done = subprocess.run([self.pactl] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, timeout=10, text=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        return done.returncode == 0, (done.stdout if done.returncode == 0 else (done.stderr or done.stdout).strip())

    def _names(self, kind):
        ok, out = self._pactl("list", "short", kind)
        return {line.split("\t")[1] for line in out.splitlines() if line.count("\t") >= 1} if ok else None

    def _devices(self):
        """Makes the virtual microphone and speaker if they aren't there. True if the microphone is there."""
        if not self.pactl:
            self._problem("pactl isn't installed (it comes with pulseaudio-utils): run the station installer again")
            return False
        sources = self._names("sources")
        if sources is None:
            self._problem("the sound system (PipeWire) isn't answering")
            return False
        sinks = self._names("sinks") or set()
        if SOURCE not in sources or SINK not in sinks:
            # The computer's own speaker and microphone now, so they can be put back if the sound system
            # switches to a virtual one when it appears (it shouldn't: they have no priority of their own).
            self.defaults = (time.time() + DEFAULTS_WATCH, {"sink": self._default("sink"), "source": self._default("source")})
        if SOURCE not in sources:
            if not self._make_fifo():
                return False
            base = ["load-module", "module-pipe-source", "source_name=" + SOURCE, "file=" + str(self.fifo),
                    "format=s16le", "rate=%d" % RATE, "channels=1"]
            ok, out = self._pactl(*(base + ['source_properties=device.description="Station receiver"']))
            if not ok:   # the name it shows is a nicety: without it, the microphone still works
                ok, out = self._pactl(*base)
            if not ok:
                self._problem("couldn't make the virtual microphone (%s)" % out)
                return False
            sources = self._names("sources") or set()
            self.say("FT8: made the virtual microphone for WSJT-X (station_receiver)")
        if SINK not in sinks:
            base = ["load-module", "module-null-sink", "sink_name=" + SINK]
            ok, _ = self._pactl(*(base + ['sink_properties=device.description="Station (no transmit)"']))
            if not ok:
                ok, _ = self._pactl(*base)
            sinks = self._names("sinks") or set()
        self._keep_defaults()
        self.state["mic"] = SOURCE in sources
        self.state["speaker"] = SINK in sinks
        if self.state["mic"]:
            self._problem(None)
        return self.state["mic"]

    def _default(self, kind):
        """The computer's default speaker ("sink") or microphone ("source"), or None if it can't be read."""
        ok, out = self._pactl("get-default-" + kind)
        return (out.strip() or None) if ok else None

    def _keep_defaults(self):
        """For a little while after making the virtual devices: if the sound system has made one of them the
        computer's default speaker or microphone, put the computer's own back (WSJT-X picks the virtual ones by
        name; everything else should go on using the real ones)."""
        if not self.defaults or time.time() > self.defaults[0]:
            self.defaults = None
            return
        for kind, ours, what in (("sink", SINK, "speaker"), ("source", SOURCE, "microphone")):
            was = self.defaults[1].get(kind)
            if was and was not in (SINK, SOURCE) and self._default(kind) == ours:
                ok, _ = self._pactl("set-default-" + kind, was)
                if ok:
                    self.say("FT8: the sound system had switched the computer's %s to the virtual one; put %s back"
                             % (what, was))

    def _make_fifo(self):
        try:
            self.fifo.parent.mkdir(parents=True, exist_ok=True)
            if self.fifo.exists() and not stat.S_ISFIFO(self.fifo.stat().st_mode):
                self.fifo.unlink()
            if not self.fifo.exists():
                os.mkfifo(self.fifo, 0o600)
            return True
        except OSError as exc:
            self._problem("couldn't make the pipe for the virtual microphone (%s)" % exc)
            return False

    def _reset_mic(self):
        """Unloads a virtual microphone that's listed but not reading its pipe, so the next check makes it again."""
        ok, out = self._pactl("list", "short", "modules")
        if not ok:
            return
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[1] == "module-pipe-source" and "source_name=" + SOURCE in parts[2]:
                self._pactl("unload-module", parts[0])
        self.state["mic"] = False

    def _start_feed(self):
        try:
            fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            # ENXIO: nothing reads the pipe; ENOENT: the pipe has gone. Either way the microphone is listed but
            # broken: unload it, and the next check makes it again.
            self._problem("couldn't reach the virtual microphone (%s): making it again" % exc.strerror)
            self._reset_mic()
            return
        try:
            fcntl.fcntl(fd, F_SETPIPE_SZ, PIPE_BYTES)
        except OSError:
            pass   # a bigger pipe only means a little more delay
        with self.lock:
            self.fd = fd
            self.last = time.time()
        try:
            self.lst = self.listening.add_local(self._write)
        except ValueError as exc:
            self._problem(str(exc))
            self._close()
            return
        # counted from here, as "for N min" is: the moments after WSJT-X closes, before the next check notices,
        # fill the pipe nobody reads any more, and those aren't this session's
        self.since, self.dropped = time.time(), 0
        self.say("FT8: WSJT-X is open: its microphone (station_receiver) now hears the waterfall's receiver")

    def _stop_feed(self, why):
        lst, self.lst = self.lst, None
        if lst is not None:
            self.listening.remove(lst)
        self._close()
        self.since = None
        self.say("FT8: %s: stopped feeding its microphone" % why)

    def _close(self):
        with self.lock:
            fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _write(self, data, silent=False):
        """Hands sound to WSJT-X's microphone (from the sound's own thread): never waits."""
        with self.lock:
            if self.fd is None:
                return
            try:
                n = os.write(self.fd, data)
                if silent:
                    self.silence += n
                else:
                    self.written += n
                if n < len(data) and not silent:
                    self.dropped += 1
            except BlockingIOError:   # the pipe is full: drop this rather than fall behind
                if not silent:
                    self.dropped += 1
            except OSError as exc:
                if exc.errno in (errno.EPIPE, errno.EBADF):   # the sound system let go of the pipe
                    os.close(self.fd)
                    self.fd = None
                    self.state["problem"] = "the virtual microphone stopped reading"
            if not silent:
                self.last = time.time()

    def _fill(self, now):
        """Silence while the receiver gives nothing (a quarter of a second at each of this thread's quarter-second
        steps), so WSJT-X's sound doesn't stop."""
        if self.lst is not None and self.fd is not None and now - self.last > SILENT_AFTER:
            self._write(bytes(int(2 * RATE * 0.25)), silent=True)
