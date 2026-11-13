"""Sound from the waterfall's SDR, for listening from another computer ("station listen").

The SDR on the radio's first IF hears everything the radio hears, and the radio's own signal while it
transmits, so it can play the station being worked (both sides of the conversation), or any other signal on
the waterfall, without touching the radio. It works the way a receiver does, for each listener:
- The carrier of the frequency they listen on is moved to 0 Hz (mixing), then the samples are filtered down to
  12,000 a second in three steps, keeping every 10th, then every 5th, then every 3rd sample (each step's
  low-pass filter worked out only for the samples it keeps). The band stays flat 5 kHz either side, and
  anything that would fold into it is at least 80 dB down. (Until 0.15.1 one big FFT did this for everyone;
  picking out just the one channel takes about a third of the work.)
- The receive filter for USB, LSB, CW or AM, applied with small FFTs (480 points, 25 Hz apart, half
  overlapping: overlap-save filtering), then the sideband (the real part) or, for AM, the envelope.
- An automatic gain control: an even loudness, the noise kept well below the speech, and separate settings
  while the radio transmits, so a transmission doesn't leave the reply after it too quiet to hear.
The sound goes out as 16-bit samples, 12,000 a second (192 kbit/s), in frames: one byte for the kind
(A = sound, J = news about it, as JSON), three for the length, then the bytes.

Where a frequency is in the SDR's samples comes from the waterfall, so the sound is placed exactly as the
picture is (orientation, offset_hz and the corrections it finds by itself).

It only runs while someone listens, and it only listens: nothing here sends anything to the radio.
"""
import collections
import json
import secrets
import struct
import threading
import time

try:
    import numpy as np
except ImportError:   # the waterfall needs python3-numpy too; without it there's nothing to listen to
    np = None

IN_RATE = 1_800_000          # the SDR's samples a second (the waterfall's RATE)
OUT_RATE = 12_000            # sound samples a second
M = 480                      # the receive filter's FFTs, at 12,000 samples a second: 25 Hz apart
OUT_HOP = M // 2             # 240 sound samples from each (they half overlap)
BIN_HZ = OUT_RATE / M        # 25.0
DELAY = M // 4               # the filters' delay (120 samples, 10 ms), so their response fits in the half kept
FLAT_HZ = 5000               # the steps down to 12,000 samples a second keep this much either side flat
REACH_HZ = 780_000           # how far from the SDR's centre a channel can be (its own filter rolls off past that)
MAX_LISTENERS = 3
BFO_HZ = 700                 # CW: the dial is the signal's own frequency, heard as a 700 Hz note (as on the radio)
SUB = 24                     # the gain control works in steps of 2 ms
TARGET = 0.30                # speech peaks at about -10 dB of full scale
NOISE_OUT = 0.02             # the noise between words, at most about -34 dB of full scale (RMS)
HANG_S = 0.4                 # how long the gain holds after a loud moment before it rises again
RISE_DB_S = 10.0             # how fast it rises after that
WAITING = 20                 # tenths of a second of samples that can wait to be worked on (behind that, dropped)
QUEUE = 40                   # frames that can wait for a slow connection (about 3 seconds); the oldest go first
NEWS_EVERY = 0.5             # seconds between news frames

# The receive filters: (low, high, edge) in Hz from the carrier; the edges are raised-cosine, this wide.
# DATA is USB a little wider, for a decoding program (WSJT-X looks for FT8 signals from 200 to 3000 Hz or so).
FILTERS = {"USB": (200, 2900, 200), "LSB": (-2900, -200, 200), "CW": (450, 950, 250), "CWR": (-950, -450, 250),
           "AM": (-4500, 4500, 600), "DATA": (100, 3100, 200)}
KINDS = ("USB", "LSB", "AM", "CW", "CWR", "DATA")
STEADY_LEVEL = 31.6 / 32768  # a decoding program's noise level: about 30 dB on WSJT-X's meter (RMS of 32 counts)


class Full(Exception):
    """As many people are listening as can."""


def kind_for(mode):
    """What to play for one of the radio's modes (Hamlib's names); None for a mode that isn't played (FM)."""
    m = (mode or "").upper()
    if m in ("USB", "PKTUSB", "RTTYR", "ECSSUSB"):
        return "USB"
    if m in ("LSB", "PKTLSB", "RTTY", "ECSSLSB"):
        return "LSB"
    if m in ("CW", "CWR", "AM"):
        return m
    if m in ("SAM", "SAL", "SAH", "AMS", "PKTAM", "DSB"):
        return "AM"
    return None


def usual_kind(freq_hz):
    """The sideband hams use for voice there: LSB below 10 MHz, USB above (and on 60 m)."""
    return "USB" if freq_hz >= 10_000_000 or 5_250_000 <= freq_hz <= 5_450_000 else "LSB"


def frame(kind, payload):
    """One frame of the stream: A (sound) or J (news), its length in three bytes, then the bytes."""
    return struct.pack(">I", (ord(kind) << 24) | len(payload)) + payload


def news_frame(obj):
    return frame("J", json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def lowpass(count, cutoff, rate, beta=8.0):
    """A low-pass filter's taps (windowed sinc, Kaiser window: about 80 dB down past the transition), gain 1."""
    n = np.arange(count) - (count - 1) / 2
    h = np.sinc(2.0 * cutoff / rate * n) * np.kaiser(count, beta)
    return h / h.sum()


def steps():
    """The three steps from 1.8 million samples a second down to 12,000: (taps, keep every nth).
    - /10: three 10-sample averages in a row (28 taps). Their zeros sit on every multiple of 180 kHz, where
      what would fold onto the channel comes from: at 5 kHz from those, 94 dB down.
    - /5 (180 to 36 kHz): flat to 5 kHz, 80 dB down from 31 kHz (where what would fold in after it starts).
    - /3 (36 to 12 kHz): flat to 5 kHz, 80 dB down from 7 kHz."""
    box = np.ones(10)
    return [(np.convolve(np.convolve(box, box), box) / 1000.0, 10),
            (lowpass(35, 18_000, 180_000), 5),
            (lowpass(90, 6_000, 36_000), 3)]


class Decimator:
    """One low-pass filter that keeps every nth sample, worked out only for the samples kept."""

    def __init__(self, taps, keep):
        self.h = np.ascontiguousarray(np.asarray(taps, dtype=np.float64)[::-1]).astype(np.complex64)
        self.keep = keep
        self.held = np.zeros(len(taps) - 1, dtype=np.complex64)   # the samples the next output reaches back to

    def run(self, x):
        buf = np.concatenate((self.held, np.asarray(x, dtype=np.complex64)))
        size = len(self.h)
        count = max(0, (len(buf) - size) // self.keep + 1)
        if count == 0:
            self.held = buf
            return np.zeros(0, dtype=np.complex64)
        step = buf.strides[0]
        rows = np.lib.stride_tricks.as_strided(buf, shape=(count, size), strides=(self.keep * step, step))
        self.held = buf[count * self.keep:]
        return rows @ self.h


class Channel:
    """One listener's channel: the carrier moved to 0 Hz, then down to 12,000 samples a second."""

    def __init__(self):
        self.stages = [Decimator(taps, keep) for taps, keep in steps()]
        self.freq = None
        self.turn = None                 # exp(-j 2 pi f n / rate) for a chunk of samples at this frequency
        self.phase = 0.0                 # where the mixing has got to, carried from one chunk to the next
        self.tail = np.zeros(OUT_HOP, dtype=np.complex64)   # for the receive filter's overlap

    def run(self, x, carrier):
        n = len(x)
        if carrier != self.freq or self.turn is None or len(self.turn) != n:
            self.freq = carrier
            self.turn = np.exp(-2j * np.pi * carrier * np.arange(n) / IN_RATE).astype(np.complex64)
        z = x * (self.turn * np.complex64(np.exp(-1j * self.phase)))
        self.phase = float((self.phase + 2 * np.pi * carrier * n / IN_RATE) % (2 * np.pi))
        for stage in self.stages:
            z = stage.run(z)
        return z


def response(kind):
    """The receive filter for a kind of sound, on the 480 points of each small FFT, in the FFT's order
    (0 Hz first), with its delay. Smooth (raised-cosine) edges keep its response short in time, so the
    overlap-save filtering stays clean (about 60 dB)."""
    lo, hi, edge = FILTERS[kind]
    j = np.arange(M) - M // 2
    f = j * BIN_HZ
    h = np.zeros(M)
    h[(f >= lo + edge / 2) & (f <= hi - edge / 2)] = 1.0
    rise = (f > lo - edge / 2) & (f < lo + edge / 2)
    h[rise] = 0.5 - 0.5 * np.cos(np.pi * (f[rise] - (lo - edge / 2)) / edge)
    fall = (f > hi - edge / 2) & (f < hi + edge / 2)
    h[fall] = 0.5 + 0.5 * np.cos(np.pi * (f[fall] - (hi - edge / 2)) / edge)
    return np.fft.ifftshift(h * np.exp(-2j * np.pi * j * DELAY / M))


class Agc:
    """Keeps the loudness even: the gain drops at once for a loud moment (judged 2 ms at a time, before it's
    played, so nothing overshoots), holds for a moment, then rises slowly. It never rises past the point
    where the noise would be louder than about -34 dB of full scale. The noise is measured beside the receive
    filter, where there's only noise (see noise_rms), so a steady signal (a carrier, an AM station) isn't
    taken for noise."""

    def __init__(self):
        self.gain = None
        self.hold = 0.0
        self.noise = collections.deque(maxlen=30)   # the noise level of each tenth of a second, 3 seconds

    def run(self, x, noise):
        """x: the sound before the gain; noise: what the noise alone would be in it (RMS), or None."""
        n = len(x) // SUB * SUB
        if n == 0:
            return x * (self.gain or 0.0)
        if noise is not None and noise > 0:
            self.noise.append(noise)
        blocks = x[:n].reshape(-1, SUB)
        peak = np.abs(blocks).max(axis=1)
        if self.noise:
            floor = float(np.percentile(np.fromiter(self.noise, dtype=np.float64), 20))
        else:
            floor = float(np.percentile(np.sqrt((blocks * blocks).mean(axis=1)), 20))
        most = NOISE_OUT / max(floor, 1e-7)
        want = np.minimum(most, TARGET / np.maximum(peak, 1e-9))
        step = SUB / OUT_RATE
        up = 10 ** (RISE_DB_S * step / 20)
        g = self.gain if self.gain is not None else float(want.min())
        start = g
        gains = np.empty(len(want))
        for i, w in enumerate(want.tolist()):
            if w < g:
                g, self.hold = w, HANG_S
            elif self.hold > 0:
                self.hold -= step
            else:
                g = min(w, g * up)
            gains[i] = g
        prev = np.concatenate(([start], gains[:-1]))
        first = np.where(gains < prev, gains, prev)          # straight down for a loud moment, else a ramp up
        ramp = first[:, None] + (gains - first)[:, None] * (np.arange(1, SUB + 1) / SUB)[None, :]
        self.gain = g
        out = (blocks * ramp).reshape(-1)
        if n < len(x):
            out = np.concatenate((out, x[n:] * g))
        return np.clip(out, -1.0, 1.0)


class Steady:
    """A steady gain for a decoding program (WSJT-X): the noise held at about 30 dB on its level meter, set from
    the median noise of the last five seconds (so a moment's burst, such as the filters starting, doesn't throw
    it) and moving towards that smoothly (in about a second), so a strong signal doesn't push down the weak
    ones FT8 decodes alongside it, the way a receiver's gain control would."""

    def __init__(self):
        self.gain = None
        self.noise = collections.deque(maxlen=50)

    def run(self, x, noise):
        if noise is not None and noise > 0:
            self.noise.append(noise)
        if len(self.noise) >= 3:
            want = STEADY_LEVEL / float(np.median(np.fromiter(self.noise, dtype=np.float64)))
            self.gain = want if self.gain is None else self.gain * (want / self.gain) ** 0.1
        return np.clip(x * (self.gain or 0.0), -1.0, 1.0)


class Listener:
    """One person listening (or, with a sink, a program on this computer: the FT8 feed): what to, and the
    state of their sound."""

    def __init__(self, follow=True, freq_hz=None, kind=None, fine_hz=0, sink=None):
        self.id = secrets.token_hex(8)
        self.since = time.time()
        self.follow, self.freq_hz, self.kind, self.fine_hz = True, None, "USB", 0
        self.set(follow, freq_hz, kind, fine_hz)
        # A program here rather than a person elsewhere: its sound goes to sink(bytes), always as DATA (USB for a
        # decoder, whatever the radio's mode), at a steady level, and silent while the radio transmits.
        self.sink = sink
        self.steady = Steady() if sink else None
        self.out = collections.deque(maxlen=QUEUE)
        self.cond = threading.Condition()
        self.channel = Channel()
        self.dc = None                   # AM: the carrier's level, taken off the envelope
        self.agc = {"rx": Agc(), "tx": Agc()}
        self.was_tx = False
        self.rx_gains = collections.deque(maxlen=30)   # (time, receive gain) for the last 3 seconds
        self.rx_back = None              # the receive gain to go back to when a transmission ends
        self.last_kind = None
        self.info = {}
        self.news_at = 0.0
        self.level = collections.deque(maxlen=5)       # the signal's strength (dB of full scale), last half second
        self.snr = collections.deque(maxlen=5)         # how far the sound stood above the noise (dB), last half second

    def set(self, follow, freq_hz, kind, fine_hz):
        follow = bool(follow)
        if not follow:
            freq_hz = int(freq_hz)
            if not 100_000 <= freq_hz <= 470_000_000:
                raise ValueError("That frequency is out of range.")
        kind = str(kind or "").upper() or None
        if kind is not None and kind not in KINDS:
            raise ValueError("Listen with USB, LSB, AM or CW.")
        fine_hz = int(fine_hz or 0)
        if not -3000 <= fine_hz <= 3000:
            raise ValueError("The fine tuning goes up to 3 kHz either way.")
        self.follow = follow
        self.freq_hz = None if follow else freq_hz
        self.kind = kind or (usual_kind(freq_hz) if not follow else "USB")
        self.fine_hz = fine_hz

    def put(self, data):
        with self.cond:
            self.out.append(data)
            self.cond.notify()

    def take(self, timeout):
        """The frames waiting to be sent (an empty list after `timeout` seconds with none)."""
        with self.cond:
            if not self.out:
                self.cond.wait(timeout)
            items = list(self.out)
            self.out.clear()
        return items


class Listening:
    """Everyone listening, and the work of making their sound, in a thread of its own that only works while
    someone listens. The waterfall hands it each tenth of a second of the SDR's samples with where the radio
    was at the time (feed)."""

    def __init__(self, say=print, rate=IN_RATE):
        if rate != IN_RATE:
            raise ValueError("listening needs the SDR at %d samples a second" % IN_RATE)
        self.say = say
        self.listeners = {}
        self.lock = threading.Lock()
        self.waiting = collections.deque()
        self.wake = threading.Event()
        self.thread = None
        self.work = collections.deque(maxlen=50)   # seconds it took to work on each tenth of a second
        self.dropped = 0
        self.fed = 0.0                   # when the waterfall last handed over samples
        self.filters = {}

    # ---------- for the server ----------

    def count(self):
        """People listening from other computers (not the FT8 feed, which is a program on this one)."""
        return sum(1 for lst in list(self.listeners.values()) if lst.sink is None)

    def wanted(self):
        return bool(self.listeners)

    def add(self, follow=True, freq_hz=None, kind=None, fine_hz=0):
        if np is None:
            raise ValueError("Listening needs python3-numpy: run the station installer again.")
        lst = Listener(follow, freq_hz, kind, fine_hz)   # checks what it was asked for first
        with self.lock:
            if self.count() >= MAX_LISTENERS:
                raise Full("%d people are listening already, which is as many as it takes." % MAX_LISTENERS)
            self.listeners[lst.id] = lst
            self._working()
        self.say("Listening: someone started listening from another computer (%d listening now)" % self.count())
        return lst

    def add_local(self, sink):
        """A program on this computer (the FT8 feed): the radio's frequency as DATA, its sound to sink(bytes)."""
        if np is None:
            raise ValueError("Listening needs python3-numpy: run the station installer again.")
        lst = Listener(follow=True, kind="DATA", sink=sink)
        with self.lock:
            self.listeners[lst.id] = lst
            self._working()
        return lst

    def _working(self):
        """Starts the thread that makes the sound, if it isn't running (called with the lock held)."""
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True, name="listen")
            self.thread.start()

    def get(self, lid):
        return self.listeners.get(str(lid or ""))

    def tune(self, lid, follow, freq_hz=None, kind=None, fine_hz=0):
        lst = self.get(lid)
        if lst is None:
            return None
        with self.lock:
            lst.set(follow, freq_hz, kind, fine_hz)
        lst.news_at = 0.0                # say what it's hearing now with the next sound
        return lst

    def remove(self, lst):
        with self.lock:
            gone = self.listeners.pop(lst.id, None) is not None
            if not self.listeners:
                self.waiting.clear()
        if gone and lst.sink is None:
            self.say("Listening: someone stopped listening (%d listening now)" % self.count())

    def status(self):
        now = time.time()
        with self.lock:
            people = [p for p in self.listeners.values() if p.sink is None]
        work = list(self.work)
        return {"listeners": [{"follow": p.follow, "freq_hz": p.info.get("heard_hz") or p.freq_hz,
                               "kind": p.info.get("kind") or p.kind, "since": round(p.since), "for_s": round(now - p.since)}
                              for p in people],
                "load": round(sum(work) / (0.1 * len(work)), 3) if work else None,
                "dropped": self.dropped, "fed_s_ago": round(now - self.fed, 1) if self.fed else None}

    # ---------- from the waterfall ----------

    def feed(self, raw, stamp):
        """A tenth of a second of the SDR's samples (bytes: I, Q, I, Q ... from 0 to 255), and where the radio was."""
        if not self.listeners:
            return
        with self.lock:
            if len(self.waiting) >= WAITING:      # two seconds behind: this computer isn't keeping up
                self.waiting.popleft()
                self.dropped += 1
            self.waiting.append((raw, stamp))
            self.fed = time.time()
        self.wake.set()

    # ---------- the work ----------

    def _run(self):
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            while True:
                with self.lock:
                    item = self.waiting.popleft() if self.waiting and self.listeners else None
                if item is None:
                    break
                started = time.perf_counter()
                try:
                    self._chunk(*item)
                except Exception as exc:   # one bad moment mustn't stop the sound for good
                    self.say("Listening: a moment of sound failed (%s)" % exc)
                self.work.append(time.perf_counter() - started)

    def _filter(self, kind):
        """The receive filter for a kind of sound, the points beside it where there's only noise, and what the
        noise in each of those points comes to in the sound (RMS), for the gain control."""
        got = self.filters.get(kind)
        if got is None:
            h = response(kind)
            f = np.fft.fftfreq(M, 1.0 / OUT_RATE)
            # The noise is measured with an FFT of its own, windowed (Hann), so strong stations inside the filter
            # don't leak into the points beside it; and not within 100 Hz of the filter, where a window's own
            # spread would still reach. Only where the band is flat (5 kHz either side).
            inside = np.abs(h) >= 1e-3
            near = inside.copy()
            for k in range(1, 5):
                near |= np.roll(inside, k) | np.roll(inside, -k)
            beside = ~near & (np.abs(f) <= FLAT_HZ)
            window = np.hanning(M)
            # White noise of power s in each sample puts s * sum(w^2) in each windowed point on average (the
            # median is ln 2 of that), and gives sound of power s * sum|h|^2 / M after the filter (the inverse FFT
            # divides by M), half of it in the real part that's heard. For AM the envelope's changes are about
            # the same.
            scale = float(np.sum(np.abs(h) ** 2)) / M / 2 / float(np.sum(window ** 2)) / np.log(2)
            got = self.filters[kind] = (h, beside, scale, window)
        return got

    def _chunk(self, raw, stamp):
        v = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - np.float32(127.5)
        iq = np.empty(len(v) // 2, dtype=np.complex64)
        iq.real = v[0::2]
        iq.imag = -v[1::2] if stamp.get("mirrored") else v[1::2]   # turned round the way the picture is
        with self.lock:
            people = list(self.listeners.values())
        for lst in people:
            self._listener(lst, iq, stamp)

    def _where(self, lst, st):
        """(where the carrier is from the SDR's centre in Hz, the kind of sound, the frequency heard, a note)."""
        mode = st.get("mode")
        if lst.follow:
            heard = st.get("tx_hz") if st.get("tx") and st.get("tx_hz") else st.get("dial")
            kind = "DATA" if lst.sink else kind_for(mode) if mode else lst.last_kind   # a decoder always wants USB
            if kind is None and mode:
                return None, None, heard, "The radio is on %s, which isn't played here (USB, LSB, CW and AM are)." % mode
            kind = kind or (usual_kind(heard) if heard else "USB")
            lst.last_kind = kind
            base = st.get("dial_bb")
        else:
            heard, kind = lst.freq_hz, lst.kind
            if st.get("rf_center") is None:
                return None, kind, heard, "Waiting for the radio's frequency, to know where that is in the band."
            base = lst.freq_hz - st["rf_center"]
        if base is None:
            return None, kind, heard, "Waiting for the waterfall."
        bfo = -BFO_HZ if kind == "CW" else BFO_HZ if kind == "CWR" else 0
        carrier = base + bfo + lst.fine_hz
        if abs(carrier) > REACH_HZ:
            return None, kind, heard, ("That's outside what the waterfall's receiver hears now: it hears from about "
                                       "500 kHz below the radio's frequency to 1 MHz above.")
        return carrier, kind, heard, None

    def _listener(self, lst, iq, st):
        with self.lock:
            carrier, kind, heard, note = self._where(lst, st)
            fine = lst.fine_hz
        tx = bool(st.get("tx"))
        noise = None
        if carrier is None:
            sound = np.zeros(len(iq) * OUT_RATE // IN_RATE)
            level = None
        else:
            ch = lst.channel
            buf = np.concatenate((ch.tail, ch.run(iq, carrier)))   # the channel at 12,000 samples a second
            h, beside, scale, window = self._filter(kind)
            count = max(0, (len(buf) - OUT_HOP) // OUT_HOP)
            y = np.empty(count * OUT_HOP, dtype=np.complex128)
            beside_power = []
            for i in range(count):
                block = buf[i * OUT_HOP:i * OUT_HOP + M]
                points = np.fft.fft(block)
                y[i * OUT_HOP:(i + 1) * OUT_HOP] = np.fft.ifft(points * h)[M - OUT_HOP:]   # the rest has wrapped round
                if beside.any():
                    looked = np.fft.fft(block * window)[beside]                             # the noise beside it
                    beside_power.append(float(np.median(looked.real ** 2 + looked.imag ** 2)))
            ch.tail = buf[count * OUT_HOP:]
            if beside_power:
                noise = float(np.sqrt(np.mean(beside_power) * scale))
            power = float(np.mean(y.real ** 2 + y.imag ** 2)) if len(y) else 0.0
            level = 10 * np.log10(power / (127.5 ** 2) + 1e-20)
            if kind == "AM":
                env = np.abs(y)
                dc = float(env.mean()) if lst.dc is None else 0.9 * lst.dc + 0.1 * float(env.mean())
                start = dc if lst.dc is None else lst.dc
                sound = env - np.linspace(start, dc, len(env))
                lst.dc = dc
            else:
                sound = y.real
        # The gain: its own for the radio's transmissions; when one starts, the receive gain from just before
        # it (the radio's reading of it lags a little) is kept, for when it ends.
        now = st.get("t") or time.time()
        if tx and not lst.was_tx:
            earlier = [g for t, g in lst.rx_gains if t <= now - 1.0]
            lst.rx_back = earlier[-1] if earlier else (lst.rx_gains[0][1] if lst.rx_gains else None)
        elif not tx and lst.was_tx and lst.rx_back is not None:
            lst.agc["rx"].gain, lst.agc["rx"].hold = lst.rx_back, 0.2
            lst.rx_back = None
        lst.was_tx = tx
        sound = np.asarray(sound, dtype=np.float64)
        if noise:   # how far the sound stands above the noise, like a receiver's signal meter
            lst.snr.append(10 * np.log10(max(float(np.mean(sound * sound)), 1e-20) / noise ** 2))
        if lst.sink is not None:
            # The FT8 feed: a steady level, and silence while the radio transmits (its own signal would only
            # swamp the decoder, and the steady level is kept for the stations after it).
            out = np.zeros(len(sound)) if tx else lst.steady.run(sound, noise)
        else:
            out = lst.agc["tx" if tx else "rx"].run(sound, noise)
            if not tx and lst.agc["rx"].gain is not None:
                lst.rx_gains.append((now, lst.agc["rx"].gain))
        pcm = (out * 32767.0).astype("<i2").tobytes()
        if level is not None:
            lst.level.append(level)
        if carrier is None:
            lst.snr.clear()
        info = {"heard_hz": int(heard) if heard else None, "kind": kind, "follow": lst.follow, "fine_hz": fine,
                "tx": tx, "tx_hz": st.get("tx_hz"), "dial": st.get("dial"), "mode": st.get("mode"),
                "radio_ok": bool(st.get("known")), "note": note,
                "level_db": round(max(lst.level), 1) if lst.level else None,
                "snr_db": round(max(0.0, max(lst.snr)), 1) if lst.snr else None}
        changed = {k: v for k, v in info.items() if k not in ("level_db", "snr_db")} != \
            {k: v for k, v in lst.info.items() if k not in ("level_db", "snr_db")}
        lst.info = info
        if lst.sink is not None:
            lst.sink(pcm)
            return
        lst.put(frame("A", pcm))
        if changed or time.time() - lst.news_at >= NEWS_EVERY:
            lst.news_at = time.time()
            lst.put(news_frame(dict(info, listeners=self.count())))
