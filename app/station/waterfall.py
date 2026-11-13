"""The waterfall: a live picture of the band around the radio's frequency.

An RTL-SDR (at VE6LDM, a NooElec inside the FT-950) listens at the radio's first IF, where the radio
has already moved whatever it's tuned to (69.450 MHz on the FT-950; if_hz in config.ini). rtl_tcp,
from the rtl-sdr package, runs the SDR and hands out its samples on a local socket. This module starts
rtl_tcp, reads the samples, and makes about ten lines a second with numpy's FFT: how strong the signals
are across the 1.8 MHz the SDR hears. Each line is stamped with where the radio was tuned and whether it
was transmitting, so the waterfall page can put the picture on the radio's own frequencies, slide it as
the dial turns, and mark transmissions.

The SDR is tuned 250 kHz away from the IF, so its own small spike in the middle of every picture lands
well away from the radio's frequency.

Two things about the tap are measured rather than assumed, because no one has confirmed them yet:
- Which way round the picture is. A radio that mixes down from above turns the band back to front.
  With swap_iq = auto, the waterfall watches which way the stations move when the dial turns, and after
  three dial turns that agree it settles on the answer (and keeps checking).
- Where the radio's frequency sits in the picture. While the radio transmits, its own signal shows on
  the tap with a thin line where its carrier is. The waterfall measures that line and reports how far
  it is from where the picture puts the radio's frequency; offset_hz in config.ini corrects it.
And one thing is kept right by itself: the SDR's own crystal drifts a little (as it warms up, say), which
moves the whole picture. About once a minute, while the dial stays still, the waterfall checks the
carriers of shortwave broadcast stations and time stations within reach, which sit exactly on 5 kHz
steps, and when two checks in a row agree, it moves the picture back onto them (up to 1.5 kHz on top of
offset_hz, in steps of at most 200 Hz). The radio can put its signal a little differently at the IF for
each mode (another FT-950 owner with an IF tap found USB about 500 Hz from LSB), so the correction is kept
for each mode: a mode checked for the first time learns its own (in steps of up to 500 Hz), starting from
the last one confirmed; after that, a correction in any confirmed mode is drift, and moves them all.
The findings are kept in waterfall.json in the data folder and shown in the health report.

The same samples can be turned into sound for someone listening from another computer (listen.py): each
tenth of a second goes there with where the radio was, so the sound is placed exactly as the picture is.

It only listens. Nothing here sends anything to the radio.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

try:
    import numpy as np
except ImportError:   # only the waterfall needs python3-numpy; the rest of the app runs without it
    np = None

RATE = 1_800_000          # samples a second: 1.8 MHz of band
FFT = 65536               # bins in a line: about 27.5 Hz each
LINES = 10                # lines a second
FRAMES = 2                # FFTs averaged into each line (smoother, still quick)
SHIFT = 250_000           # how far from the IF the SDR is tuned, to keep its spike off the radio's frequency
KEEP = 360                # lines kept (36 seconds, about 23 MB): a page opening, zooming or changing band
                          # gets the picture back at once
PORT = 1234               # rtl_tcp's own default port, on 127.0.0.1 only
GAINS = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7,
         32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6]   # the R820T's steps (dB)
LEARN_VOTES = 3           # dial turns that must agree before the orientation is settled
SETTLE = 2.0              # seconds the dial must stay put around a turn before it counts
SSB_EDGE_HZ = 3000        # how far an SSB signal reaches from its frequency
TRACK_EVERY = 60.0        # seconds between checks against broadcast stations (while the dial stays still)
TRACK_LINES = 200         # receive lines (20 s) with the dial still that one check looks at
TRACK_RESIDUAL = 1000     # Hz: a carrier further than this from a 5 kHz step doesn't count
TRACK_GROUP = 40          # Hz: carriers this close in how far they're off count as agreeing
TRACK_AGREE = 60          # Hz: two checks in a row must agree this well before the picture moves
TRACK_STEP = 200          # Hz: the most one correction moves the picture (drift, in a mode confirmed before)
TRACK_FIRST = 500         # Hz: the most, in a mode being lined up for the first time (its own offset at the IF)
TRACK_LIMIT = 1500        # Hz: the most the waterfall corrects by itself, on top of offset_hz
# Shortwave broadcast bands (kHz), where AM stations' carriers sit exactly on 5 kHz steps. The stretches
# that are amateur bands in the Americas (7.2-7.3 and 3.9-4.0 MHz) are left out: hams' carriers aren't exact.
BROADCAST_KHZ = ((2300, 2495), (3200, 3400), (4750, 5060), (5900, 6200), (7300, 7450), (9400, 9900),
                 (11600, 12100), (13570, 13870), (15100, 15800), (17480, 17900), (18900, 19020),
                 (21460, 21850), (25670, 26100))
# Time stations, whose carriers are exact: WWV and WWVH (2.5 to 25 MHz) and CHU (3.33, 7.85, 14.67 MHz).
TIME_KHZ = (2500, 3330, 5000, 7850, 10000, 14670, 15000, 20000, 25000)


def mirror(a):
    """The line turned back to front around its middle (bin N/2 + k <-> N/2 - k)."""
    return np.concatenate((a[:1], a[:0:-1]))


def pixels(data, rf_center, bin_hz, lo, hi, n):
    """A line's strength (1 dB steps, 0 = nothing heard there) for n pixels from lo to hi Hz:
    the strongest bin under each pixel, or the one bin a pixel falls in when it's narrower."""
    size = len(data)
    edges = lo + (hi - lo) * np.arange(n + 1, dtype=np.float64) / n
    pos = (edges - rf_center) / bin_hz + size / 2
    start = np.floor(pos[:-1]).astype(np.int64)
    stop = np.maximum(np.floor(pos[1:]).astype(np.int64), start + 1)
    inside = (start >= 0) & (stop <= size)
    out = np.zeros(n, dtype=np.uint8)
    if not inside.any():
        return out
    first = np.clip(start, 0, size - 1)
    best = np.maximum.reduceat(data, first)      # [first[i], first[i+1]); one bin when they're equal
    last = int(np.flatnonzero(inside)[-1])
    best[last] = data[first[last]:max(int(stop[last]), int(first[last]) + 1)].max()
    out[inside] = best[inside]
    return out


def signals(line):
    """Just the stations in a line: how far each bin stands above the noise around it (0 for most bins),
    widened a bin either way; NaN at the SDR's own filter edges and its spike, so they're left out."""
    line = np.asarray(line, dtype=np.float32)
    size, block = len(line), 512
    noise = np.median(line.reshape(size // block, block), axis=1)   # the noise level, a little at a time
    floor = np.interp(np.arange(size), np.arange(size // block) * block + block / 2, noise)
    above = np.clip(line - floor - 6.0, 0, None)
    above = np.maximum(above, np.maximum(np.roll(above, 1), np.roll(above, -1)))
    out = np.full(size, np.nan, dtype=np.float32)
    keep = slice(size // 8, size - size // 8)
    out[keep] = above[keep]
    out[size // 2 - 8:size // 2 + 9] = np.nan
    return out


def correlation(a, b, shift):
    """How alike a[i] and b[i - shift] are (Pearson's r), over the bins both cover; None if too few."""
    size = len(a)
    if abs(shift) >= size // 2:
        return None
    if shift >= 0:
        x, y = a[shift:], b[:size - shift]
    else:
        x, y = a[:size + shift], b[-shift:]
    keep = np.isfinite(x) & np.isfinite(y)
    if keep.sum() < 1000:
        return None
    x, y = x[keep], y[keep]
    x = x - x.mean()
    y = y - y.mean()
    den = float(np.sqrt((x * x).sum() * (y * y).sum()))
    return float((x * y).sum() / den) if den > 0 else None


def reference_carriers(line, rf_center, bin_hz):
    """Steady, narrow carriers that should sit exactly on a 5 kHz step: AM stations in the shortwave broadcast
    bands, and the time stations. line is dB per bin (best the median of many lines, so speech and fading
    drop out), placed with rf_center the way the waterfall has it. Strongest first, one per station:
    [(frequency as the picture has it, how far that is from the nearest 5 kHz step, dB above the noise,
    whether it's a time station)]."""
    v = np.asarray(line, dtype=np.float32)
    size, block = len(v), 512
    noise = np.median(v.reshape(size // block, block), axis=1)
    floor = np.interp(np.arange(size), np.arange(size // block) * block + block / 2, noise)
    excess = v - floor
    khz = (rf_center + (np.arange(size) - size / 2) * bin_hz) / 1000.0
    keep = np.zeros(size, dtype=bool)
    for lo, hi in BROADCAST_KHZ:
        keep |= (khz >= lo - 1.2) & (khz <= hi + 1.2)    # stations sit right on the band edges too (13.570)
    for t in TIME_KHZ:
        keep |= np.abs(khz - t) <= 1.2
    edge = int(size * 0.08)                         # the SDR's own filter edges
    keep[:edge] = False
    keep[size - edge:] = False
    keep[size // 2 - 16:size // 2 + 17] = False     # its spike in the middle
    i = np.arange(12, size - 12)
    cand = i[keep[i] & (excess[i] >= 20) & (v[i] >= v[i - 1]) & (v[i] >= v[i + 1])]
    found = []
    for k in cand[np.argsort(-excess[cand], kind="stable")]:
        k = int(k)
        if any(abs(k - j) * bin_hz < 2000 for j, *_ in found):
            continue   # one per station
        # Narrow: 12 dB over what's 80 to 330 Hz away on each side. A voice, or the steep edge of a data
        # signal (DRM), has its own level on at least one side.
        if v[k] - max(float(np.median(v[k - 12:k - 2])), float(np.median(v[k + 3:k + 13]))) < 12:
            continue
        a, b, c = float(v[k - 1]), float(v[k]), float(v[k + 1])
        curve = a - 2 * b + c
        frac = max(-0.5, min(0.5, 0.5 * (a - c) / curve)) if curve < 0 else 0.0   # the top, between bins
        f = rf_center + (int(k) + frac - size / 2) * bin_hz
        step = round(f / 5000) * 5000
        found.append((k, f, f - step, float(excess[k]), any(abs(step - t * 1000) < 1 for t in TIME_KHZ)))
        if len(found) >= 20:
            break
    return [(f, r, e, t) for _, f, r, e, t in found]


def settle(found):
    """How far off the reference carriers say the picture is: (Hz, how many carriers agree), or None.
    Carriers more than TRACK_RESIDUAL from a step don't count. The biggest group within TRACK_GROUP of each
    other wins; each carrier in it counts once, and once more if it's very strong (40 dB over the noise) or
    a time station; the group must count at least 2. Its answer is the average, the strongest counting most."""
    usable = [(r, e, t) for _, r, e, t in found if abs(r) <= TRACK_RESIDUAL]
    best = None
    for r0, _, _ in usable:
        group = [(r, e, t) for r, e, t in usable if abs(r - r0) <= TRACK_GROUP]
        weight = sum(1 + (e >= 40) + bool(t) for _, e, t in group)
        if best is None or weight > best[0] or (weight == best[0] and len(group) > len(best[1])):
            best = (weight, group)
    if best is None or best[0] < 2:
        return None
    group = best[1]
    return sum(r * e for r, e, _ in group) / sum(e for _, e, _ in group), len(group)


class Waterfall:
    """Runs the SDR through rtl_tcp and keeps the last few seconds of waterfall lines.

    radio() gives the waterfall's radio's latest reading (the station app's first radio): its frequency,
    mode, whether it's transmitting, and its split. Nothing here asks the radio anything itself.
    """

    def __init__(self, data_dir, radio, if_hz=69_450_000, gain=25.0, ppm=0, swap="auto", offset_hz=0,
                 device="0", enabled=True, connect=None, log=print):
        self.data_dir = Path(data_dir)
        self.radio = radio
        self.if_hz, self.gain, self.ppm, self.offset_hz = int(if_hz), float(gain), int(ppm), int(offset_hz)
        self.swap = swap if swap in ("yes", "no") else "auto"
        self.device, self.enabled, self.say = str(device), enabled, log
        self.connect_to = connect        # "host:port" of an rtl_tcp that's already running (tests); else start one
        self.bin_hz = RATE / FFT
        self.lines = deque(maxlen=KEEP)
        self.seq = 0
        self.lock = threading.Lock()
        self.state = {"running": False, "problem": None, "since": None, "lines_per_s": 0.0}
        self.learned = self._load()
        if self.swap == "yes":
            self.mirrored, self.orientation_how = True, "set in config.ini (swap_iq = yes)"
        elif self.swap == "no":
            self.mirrored, self.orientation_how = False, "set in config.ini (swap_iq = no)"
        elif self.learned.get("orientation") in ("mirrored", "normal"):
            self.mirrored = self.learned["orientation"] == "mirrored"
            self.orientation_how = "learned from the dial (%s)" % self.learned.get("when", "earlier")
        else:   # the radio mixes down from above, which usually turns the band back to front; not confirmed yet
            self.mirrored, self.orientation_how = True, "not confirmed yet: turn the dial a few times"
        self._votes = deque(maxlen=LEARN_VOTES)
        self._history = deque(maxlen=LINES * 8)  # (time, dial, line) of recent receive lines, for the dial check
        self._dial_change = None         # (time, old dial, new dial) while waiting for the dial to settle
        self._tx_run = []                # this transmission's lines, for the carrier check
        self._proc = None
        self._window = self._norm = None
        self._keep_lock = threading.Lock()   # waterfall.json is written from two threads
        # The correction found from broadcast stations, on top of offset_hz, for each of the radio's modes
        # (auto_modes); a mode not checked yet uses auto_default, the last one confirmed. auto_settled: the
        # modes whose own correction a check has confirmed. A new offset_hz in config.ini (set by hand) starts
        # it all again from nothing. (Before 0.15.1 there was one correction, auto_hz: it becomes the default.)
        same = self.learned.get("auto_base") == self.offset_hz
        modes = self.learned.get("auto_modes") if same and isinstance(self.learned.get("auto_modes"), dict) else {}
        self.auto_modes = {str(m): self._limit(v) for m, v in modes.items() if self._limit(v) is not None}
        default = self.learned.get("auto_default", self.learned.get("auto_hz", 0)) if same else 0
        self.auto_default = self._limit(default) or 0.0
        settled = self.learned.get("auto_settled") if same else None
        self.auto_settled = {str(m) for m in settled} if isinstance(settled, list) else set()
        self.auto_note = None            # set when it has moved the picture as far as it does by itself
        self._track_at = self._track_try = 0.0
        self._track_prev = None          # (time, Hz off, mode) from the last check, waiting for one that agrees
        self._tracking = False           # a check is being worked out (in its own thread)
        self._mode = None                # the radio's mode on the latest line (kept while it doesn't answer)
        self.listening = None            # someone listening from another computer (listen.Listening), if set

    # ---------- what the page and the health report get ----------

    def status(self):
        with self.lock:
            st = dict(self.state)
        # Each transmission's carrier is kept as the offset_hz that would have put it on the dial, so a
        # change of offset_hz in config.ini doesn't leave old measurements saying the picture is still off.
        needs = self.learned.get("carrier_needs") or {}
        needed = {mode: int(round(float(np.median(v)))) for mode, v in needs.items() if v} if np is not None else {}
        mode = self._mode
        st.update(on=self.enabled, orientation="mirrored" if self.mirrored else "normal",
                  orientation_how=self.orientation_how, offset_hz=self.offset_hz,
                  auto_hz=int(round(self._correction(mode))), auto_mode=mode, auto_when=self.learned.get("auto_when"),
                  auto_note=self.auto_note, auto_modes={m: int(round(v)) for m, v in sorted(self.auto_modes.items())},
                  auto_settled=sorted(self.auto_settled), auto_default=int(round(self.auto_default)),
                  carrier_offset={m: self._offset(m if m in ("USB", "LSB") else None) - hz for m, hz in needed.items()},
                  carrier_needs=needed, if_hz=self.if_hz, rate=RATE, bin_hz=self.bin_hz, gain=self.gain,
                  listening=self.listening.count() if self.listening is not None else 0)
        return st

    def view(self, since, lo, hi, n, limit=40):
        """New lines since seq `since`, each as n pixels from lo to hi Hz (on the radio's own frequencies),
        oldest first; at most `limit` of them (the newest)."""
        n = max(16, min(int(n), 4096))
        limit = max(1, min(int(limit), KEEP))
        lo, hi = float(lo), float(hi)
        if not hi > lo:
            raise ValueError("The frequency range is empty.")
        with self.lock:
            if since > self.seq:   # the app restarted since the page last asked
                since = 0
            recs = [r for r in self.lines if r["seq"] > since][-limit:]
            latest = self.seq
        out = []
        for r in recs:
            px = pixels(r["data"], r["rf_center"], self.bin_hz, lo, hi, n)
            out.append({"seq": r["seq"], "t": r["t"], "tx": r["tx"], "tx_hz": r["tx_hz"],
                        "data": base64.b64encode(px.tobytes()).decode("ascii")})
        return {"seq": latest, "lines": out}

    # ---------- the SDR ----------

    def start(self):
        if self.enabled:
            threading.Thread(target=self._run, daemon=True, name="waterfall").start()
        return self

    def _problem(self, text):
        with self.lock:
            changed = self.state.get("problem") != text
            self.state.update(running=False, problem=text)
        if changed and text:
            self.say("Waterfall: %s" % text)

    def _run(self):
        if np is None:
            self._problem("it needs python3-numpy: run the station installer again")
            return
        wait = 5
        while True:
            started = time.time()
            try:
                self._session()
            except Exception as exc:   # anything unexpected: report it and start over
                self._problem("stopped (%s); starting again" % exc)
            self._stop_rtl_tcp()
            wait = 5 if time.time() - started > 60 else min(wait * 2, 60)
            time.sleep(wait)

    def _sdr_hz(self):
        return self.if_hz + SHIFT

    def _start_rtl_tcp(self):
        """Starts rtl_tcp on 127.0.0.1. Returns (host, port), or None with the problem reported."""
        if self.connect_to:
            host, _, port = self.connect_to.rpartition(":")
            return host or "127.0.0.1", int(port)
        program = shutil.which("rtl_tcp")
        if not program:
            self._problem("rtl_tcp isn't installed (it comes with the rtl-sdr package): run the station installer again")
            return None
        cmd = [program, "-a", "127.0.0.1", "-p", str(PORT), "-d", self.device,
               "-f", str(self._sdr_hz()), "-s", str(RATE)]
        if self.gain > 0:
            cmd += ["-g", "%g" % self.gain]
        if self.ppm:
            cmd += ["-P", str(self.ppm)]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        log = open(self.data_dir / "rtl_tcp.log", "w")
        self._proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        log.close()
        return "127.0.0.1", PORT

    def _stop_rtl_tcp(self):
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _why_rtl_tcp_stopped(self):
        try:
            text = (self.data_dir / "rtl_tcp.log").read_text(errors="replace")
        except OSError:
            text = ""
        if "No supported devices" in text:
            return "the SDR isn't found: is it plugged in?"
        if "usb_claim_interface" in text or "claimed by second instance" in text:
            if subprocess.run(["pgrep", "-x", "gqrx"], stdout=subprocess.DEVNULL).returncode == 0:
                return "Gqrx is open, and only one program can use the SDR at a time: close Gqrx"
            return "another program is using the SDR"
        return "rtl_tcp stopped: %s" % (text.strip().splitlines() or ["no message"])[-1]

    def _session(self):
        where = self._start_rtl_tcp()
        if where is None:
            return
        sock = None
        deadline = time.time() + 10
        while sock is None:
            try:
                sock = socket.create_connection(where, timeout=2)
            except OSError:
                if self._proc and self._proc.poll() is not None:
                    self._problem(self._why_rtl_tcp_stopped())
                    return
                if time.time() > deadline:
                    self._problem("rtl_tcp didn't answer")
                    return
                time.sleep(0.5)
        with sock:
            sock.settimeout(5)
            head = self._read(sock, 12)
            if head is None or head[:4] != b"RTL0":
                self._problem("the SDR's program (rtl_tcp) sent something unexpected")
                return

            def command(code, value):
                sock.sendall(struct.pack(">BI", code, value & 0xFFFFFFFF))
            command(0x02, RATE)
            command(0x01, self._sdr_hz())
            if self.gain > 0:
                command(0x03, 1)                                # manual gain
                command(0x04, int(round(self.gain * 10)))       # tenths of a dB; the SDR picks its nearest step
            else:
                command(0x03, 0)                                # the SDR's own automatic gain
            command(0x05, self.ppm)
            command(0x08, 0)                                    # the RTL2832's digital AGC off
            self._receive(sock)
        if self._proc and self._proc.poll() is not None:
            self._problem(self._why_rtl_tcp_stopped())

    @staticmethod
    def _read(sock, size, buf=None):
        view = memoryview(buf if buf is not None else bytearray(size))[:size]
        got = 0
        while got < size:
            try:
                n = sock.recv_into(view[got:], size - got)
            except socket.timeout:
                return None
            if n == 0:
                return None
            got += n
        return view.obj if buf is None else buf

    def _process(self, buf):
        """One line from a tenth of a second of samples: dB below full scale for each bin, middle = the SDR's centre."""
        if self._window is None:
            self._window = np.hanning(FFT).astype(np.float32)
            self._norm = float(self._window.sum()) ** 2 * 128.0 ** 2
        raw = np.frombuffer(buf, dtype=np.uint8, count=2 * FFT * FRAMES).astype(np.float32) - np.float32(127.5)
        iq = np.empty(FFT * FRAMES, dtype=np.complex64)
        iq.real, iq.imag = raw[0::2], raw[1::2]
        frames = iq.reshape(FRAMES, FFT)
        frames = frames - frames.mean(axis=1, keepdims=True)    # the SDR's own spike in the middle
        if self.mirrored:
            frames = np.conj(frames)
        spec = np.fft.fftshift(np.fft.fft(frames * self._window, axis=1), axes=1)
        power = (spec.real ** 2 + spec.imag ** 2).mean(axis=0) / self._norm
        return 10.0 * np.log10(power + 1e-20)

    def _receive(self, sock):
        chunk = 2 * (RATE // LINES)
        buf = bytearray(chunk)
        with self.lock:
            self.state.update(running=True, problem=None, since=time.time())
        self.say("Waterfall: receiving (SDR at %.3f MHz, gain %s)" % (self._sdr_hz() / 1e6,
                                                                      "auto" if self.gain <= 0 else "%g dB" % self.gain))
        made, counted = 0, time.time()
        while True:
            if self._read(sock, chunk, buf) is None:
                self._problem("the SDR stopped sending")
                return
            stamp = self._add_line(self._process(buf))
            listening = self.listening
            if listening is not None and listening.wanted():
                listening.feed(bytes(buf), stamp)   # a copy: buf is filled again with the next tenth of a second
            made += 1
            now = time.time()
            if now - counted >= 5:
                with self.lock:
                    self.state["lines_per_s"] = round(made / (now - counted), 1)
                made, counted = 0, now

    # ---------- lines ----------

    def _add_line(self, db, now=None):
        """Stamps one line with where the radio was and keeps it; db is float dB per bin (after any swap).
        Returns the stamp (where the radio was, and where its frequency is in the SDR's samples), for listening."""
        now = time.time() if now is None else now
        r = self.radio() or {}
        dial = r.get("freq_hz") if r.get("ok") else None
        tx = bool(r.get("tx")) if r.get("ok") else False
        split = r.get("split") or {}
        tx_hz = split.get("tx_hz") if split.get("on") and split.get("tx_hz") else dial
        if r.get("ok") and r.get("mode"):
            self._mode = str(r["mode"]).upper()
        mode = self._mode
        # While it transmits split, the radio's mixing follows the transmit frequency, so the picture does too.
        centre = (tx_hz if tx else dial)
        data = np.clip(np.round(db) + 200, 1, 255).astype(np.uint8)
        offset = self._offset(mode)
        # Where the radio's own frequency is in the SDR's samples (from their centre, the picture's way round):
        # the same whatever the dial says, since the radio has already moved its frequency to the IF.
        stamp = {"t": now, "mirrored": self.mirrored, "dial": dial, "mode": mode if r.get("ok") else None,
                 "tx": tx, "tx_hz": int(tx_hz) if tx and tx_hz else None, "known": centre is not None,
                 "dial_bb": (SHIFT if self.mirrored else -SHIFT) - offset, "rf_center": None}
        if centre is None:
            with self.lock:   # the radio isn't answering: nothing to put the picture against
                last = self.lines[-1] if self.lines else None
            if last is None:
                return stamp
            rf_center = last["rf_center"]
        else:
            rf_center = int(centre) + (-SHIFT if self.mirrored else SHIFT) + offset
        stamp["rf_center"] = rf_center
        with self.lock:
            self.seq += 1
            self.lines.append({"seq": self.seq, "t": round(now, 2), "rf_center": rf_center, "tx": tx,
                               "tx_hz": int(tx_hz) if tx and tx_hz else None, "data": data,
                               "known": centre is not None, "mode": mode})
        if centre is not None:
            self._learn(data, now, int(centre), tx, r.get("mode"))
            if not tx:
                self._track(now)
        return stamp

    # ---------- learning: which way round, and where the carrier sits ----------

    def _learn(self, data, now, centre, tx, mode):
        if tx:
            if len(self._tx_run) < LINES * 10:   # ten seconds of it is plenty
                self._tx_run.append((data, centre, mode))
            return
        if self._tx_run:
            run, self._tx_run = self._tx_run, []
            self._check_carrier(run)
        if self.swap != "auto":
            return
        self._history.append((now, centre, data))
        if len(self._history) < 2:
            return
        previous = self._history[-2][1]
        if centre != previous:
            self._dial_change = (now, previous, centre)
            return
        if self._dial_change is None:
            return
        changed_at, old, new = self._dial_change
        if now - changed_at < SETTLE:   # wait for the dial (and the radio's reading of it) to settle
            return
        # Lines from before the turn, well before the radio's reading changed (it lags the dial a little),
        # and lines since it settled; a few of each, averaged, so a station shows above the noise.
        before = [h for h in self._history if h[1] == old and h[0] <= changed_at - SETTLE]
        after = [h for h in self._history if h[1] == new and h[0] >= changed_at + SETTLE - 0.5]
        if len(after) < 3 and now - changed_at < 3 * SETTLE:
            return   # not enough lines yet
        self._dial_change = None
        if len(before) < 3 or len(after) < 3:
            return
        moved = new - old
        k = int(round(moved / self.bin_hz))
        if not 40 <= abs(k) <= FFT // 4:   # about 1 kHz to 450 kHz
            return
        a = signals(np.mean([h[2] for h in before[-5:]], axis=0))
        b = signals(np.mean([h[2] for h in after[-5:]], axis=0))
        as_is = correlation(a, b, k)       # the stations stayed put on the radio's frequencies
        flipped = correlation(a, b, -k)    # they'd stay put if the picture were the other way round
        if as_is is None or flipped is None:
            return
        unclear = max(as_is, flipped) < 0.3 or abs(as_is - flipped) < 0.15   # not enough stations to tell
        with self.lock:
            self.state["dial_check"] = {"moved_hz": int(moved), "as_is": round(as_is, 2), "flipped": round(flipped, 2),
                                        "when": time.strftime("%H:%M:%S", time.localtime(now)),
                                        "verdict": "unclear" if unclear else "as shown" if as_is > flipped else "turned round"}
        if unclear:
            return
        self._votes.append(as_is > flipped)
        if len(self._votes) == LEARN_VOTES and len(set(self._votes)) == 1:
            right = self._votes[0]
            when = time.strftime("%a %b %d, %H:%M", time.localtime(now))
            if not right:
                self.mirrored = not self.mirrored
                self._history.clear()
                self.say("Waterfall: the picture was back to front; turned it round (from %d dial turns)" % LEARN_VOTES)
            self.orientation_how = "learned from the dial (%s)" % when
            self._keep(orientation="mirrored" if self.mirrored else "normal", when=when)
            self._votes.clear()

    @staticmethod
    def _limit(value):
        """A correction as kept: a number of Hz within TRACK_LIMIT, or None if it isn't one."""
        try:
            return float(max(-TRACK_LIMIT, min(TRACK_LIMIT, float(value))))
        except (TypeError, ValueError):
            return None

    def _correction(self, mode=None):
        """The correction found by itself for one of the radio's modes (the default for a mode not checked yet)."""
        return self.auto_modes.get(mode, self.auto_default) if mode else self.auto_default

    def _offset(self, mode=None):
        """Where the picture is moved: offset_hz from config.ini plus the correction found by itself (for the
        radio's mode)."""
        return self.offset_hz + int(round(self._correction(mode)))

    def _track(self, now):
        """About once a minute, with the dial still for TRACK_LINES lines: a check against broadcast stations,
        worked out in its own thread so the picture never waits for it."""
        if self._tracking or now - self._track_at < TRACK_EVERY or now - self._track_try < 1.0:
            return
        self._track_try = now
        with self.lock:
            latest = self.lines[-1] if self.lines else None
            run = []
            for rec in reversed(self.lines):
                if rec["tx"] or not rec.get("known") or rec["rf_center"] != latest["rf_center"] \
                        or rec.get("mode") != latest.get("mode"):
                    break
                run.append(rec["data"])
                if len(run) >= TRACK_LINES:
                    break
        if len(run) < TRACK_LINES:
            return
        self._track_at = now
        self._tracking = True
        threading.Thread(target=self._track_check, args=(run, latest["rf_center"], now, latest.get("mode")),
                         daemon=True, name="waterfall-check").start()

    def _track_check(self, run, rf_center, now, mode=None):
        try:
            steady = np.median(np.stack(run), axis=0)     # speech, music and fading drop out; carriers stay
            found = reference_carriers(steady, rf_center, self.bin_hz)
            agreed = settle(found)
            with self.lock:
                self.state["track"] = {"when": time.strftime("%H:%M", time.localtime(now)), "carriers": len(found),
                                       "off_hz": None if agreed is None else int(round(agreed[0])),
                                       "stations": 0 if agreed is None else agreed[1], "mode": mode}
            if agreed is None:
                self._track_prev = None
                return
            off = agreed[0]
            prev, self._track_prev = self._track_prev, (now, off, mode)
            if prev is None or prev[2] != mode or now - prev[0] > 3 * TRACK_EVERY or abs(off - prev[1]) > TRACK_AGREE:
                return   # wait for a second check that agrees (in the same mode)
            move = -(off + prev[1]) / 2
            when = time.strftime("%a %b %d, %H:%M", time.localtime(now))
            if abs(move) < 10:   # on frequency: this mode's own correction is confirmed
                if mode and mode not in self.auto_settled:
                    with self._keep_lock:
                        here = self._correction(mode)
                        self.auto_modes = dict(self.auto_modes, **{mode: here})
                        self.auto_settled = self.auto_settled | {mode}
                        self.auto_default = here
                    self._keep(**self._auto_found(mode, None))
                    self.say("Waterfall: on frequency on %s (%+d Hz on top of offset_hz)" % (mode, round(here)))
                return
            # In a mode confirmed before, it's drift (the SDR's crystal), the same in every mode: all of them move.
            # In a mode checked for the first time, it's that mode's own: only it moves (in bigger steps).
            drift = mode is None or mode in self.auto_settled
            most = TRACK_STEP if drift else TRACK_FIRST
            move = max(-most, min(most, move))
            with self._keep_lock:
                before = self._correction(mode)
                wanted = before + move
                new = max(-TRACK_LIMIT, min(TRACK_LIMIT, wanted))
                self.auto_note = None
                if abs(new - wanted) > 0.5:
                    self.auto_note = ("that's as far as it moves it by itself (%.1f kHz): "
                                      "offset_hz in config.ini needs setting" % (TRACK_LIMIT / 1000))
                if abs(new - before) < 0.5:
                    return
                if drift:
                    step = new - before
                    self.auto_modes = {m: max(-TRACK_LIMIT, min(TRACK_LIMIT, v + step)) for m, v in self.auto_modes.items()}
                    if mode:
                        self.auto_modes[mode] = new
                    self.auto_default = max(-TRACK_LIMIT, min(TRACK_LIMIT, self.auto_default + step))
                else:
                    self.auto_modes = dict(self.auto_modes, **{mode: new})
            self._track_prev = None      # the next move needs two new checks, with the picture where it is now
            self._keep(**self._auto_found(mode, when))
            self.say("Waterfall: %d broadcast station%s %d Hz off%s: moved the picture %+d Hz (now %+d Hz on top of "
                     "offset_hz%s)" % (agreed[1], "s were" if agreed[1] != 1 else " was", round(abs(off)),
                                       " on %s" % mode if mode else "", round(new - before), round(new),
                                       "" if drift else ", for %s" % mode))
        except Exception as exc:   # a check that goes wrong mustn't stop the waterfall
            self.say("Waterfall: the check against broadcast stations failed (%s)" % exc)
        finally:
            self._tracking = False

    def _auto_found(self, mode, when):
        """What's kept in waterfall.json about the corrections (auto_hz too, as versions before 0.15.1 kept it)."""
        found = {"auto_hz": int(round(self._correction(mode))), "auto_base": self.offset_hz,
                 "auto_modes": {m: int(round(v)) for m, v in self.auto_modes.items()},
                 "auto_settled": sorted(self.auto_settled), "auto_default": int(round(self.auto_default))}
        if when:
            found["auto_when"] = when
        return found

    def _check_carrier(self, run):
        """After a transmission: where its carrier line showed, compared with where the picture puts it."""
        if len(run) < LINES:   # under a second: too short to trust
            return
        centre, mode = run[0][1], run[0][2]
        if any(c != centre for _, c, _ in run):
            return
        mode = str(mode).upper() if mode else None
        offset = self._offset(mode)
        stack = np.stack([d for d, _, _ in run]).astype(np.float32)
        steady = np.percentile(stack, 10, axis=0)   # a carrier is there all the time; speech comes and goes
        # Where the carrier should show: the picture's middle is SHIFT from the radio's frequency.
        expect = FFT // 2 + int(round((SHIFT if self.mirrored else -SHIFT) / self.bin_hz)) \
            - int(round(offset / self.bin_hz))
        reach = int(SSB_EDGE_HZ / self.bin_hz)
        lo, hi = max(expect - reach, 40), min(expect + reach, FFT - 40)
        local = steady[lo - 40:hi + 40]
        best, best_rise = None, 0.0
        for i in range(40, len(local) - 40):
            rise = float(local[i] - np.median(local[i - 40:i + 40]))
            if local[i] >= local[i - 1] and local[i] >= local[i + 1] and rise > best_rise:
                best, best_rise = i, rise
        if best is None or best_rise < 12:
            return
        bin_index = lo - 40 + best
        found_hz = int(round((bin_index - expect) * self.bin_hz))   # where it showed, from the dial
        needs = dict(self.learned.get("carrier_needs") or {})
        key = mode if mode in ("USB", "LSB") else "other"
        needs[key] = (needs.get(key, []) + [offset - found_hz])[-5:]   # the offset that puts it on the dial
        self._keep(carrier_needs=needs)
        self.say("Waterfall: the carrier of that %s transmission showed %+d Hz from the radio's frequency" % (key, found_hz))

    def _load(self):
        try:
            return json.loads((self.data_dir / "waterfall.json").read_text())
        except (OSError, ValueError):
            return {}

    def _keep(self, **found):
        """Adds findings to waterfall.json (from either thread, one at a time)."""
        with self._keep_lock:
            learned = dict(self.learned)
            learned.update(found)
            self.learned = learned       # replaced whole, so status() never sees it half changed
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                tmp = self.data_dir / "waterfall.json.tmp"
                tmp.write_text(json.dumps(learned, indent=1))
                os.replace(tmp, self.data_dir / "waterfall.json")
            except OSError:
                pass
