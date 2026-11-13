"""What WSJT-X hears, for the station display's map, from WSJT-X's own network messages.

WSJT-X sends a UDP message to 127.0.0.1:2237 (its default "UDP Server", under Settings > Reporting) for every
decode, and whenever its state changes (the dial frequency, the mode...). The format is described in WSJT-X's
Network/NetworkMessage.hpp: Qt's QDataStream, big-endian: the magic number 0xadbccbda, a schema number, the
message type, the sending program's id, then the message's fields. WSJT-X sends these without waiting for an
answer, so nothing here ever writes back: it only listens (WSJT-X's "Accept UDP requests" can stay off).

From each decode it keeps the station that sent the message (the caller in a CQ, the second call in the others),
where that station is (the grid square in its message if there is one, otherwise its country, or for a Canadian
ham the town, from the app's own look-ups), how far and in which direction, how strong, and when: the stations of
the last 15 minutes, for the map and the health report. Decodes WSJT-X flags as low-confidence, played back from a
file, or replayed rather than new are left out.

It also keeps what's needed to say why nothing is heard (quiet_hint): when WSJT-X last sent anything (it sends a
heartbeat every 15 seconds, and its status, with the dial frequency, twice in every decoding period), and which
frequency and mode it's on. One message it can't make sense of is counted and skipped: listening never stops.
"""
import re
import socket
import struct
import threading
import time

from . import geo, logbook

MAGIC = 0xADBCCBDA
PORT = 2237                    # WSJT-X's default UDP server port
WINDOW = 15 * 60               # seconds a station stays on the map after it was last heard
MOST = 400                     # stations kept at most (a busy band has a hundred or so in 15 minutes)
HEARTBEAT, STATUS, DECODE, CLEAR, CLOSE = 0, 1, 2, 3, 6
SILENT_AFTER = 60              # seconds without any message from an open WSJT-X before it's taken as not sending

# WSJT-X 3.0.2's own frequencies for FT8 and FT4 (models/FrequencyList.cpp, the ones for every IARU region), the
# dial frequencies its band buttons tune to; the others (DXpedition, intercontinental) count as FT8 frequencies too.
FT8_DIALS = {"160m": 1840000, "80m": 3573000, "60m": 5357000, "40m": 7074000, "30m": 10136000, "20m": 14074000,
             "17m": 18100000, "15m": 21074000, "12m": 24915000, "10m": 28074000, "6m": 50313000, "2m": 144174000}
FT8_OTHERS = (3567000, 7056000, 10131000, 14090000, 18095000, 21091000, 24911000, 28091000, 50323000)
FT4_DIALS = {"80m": 3575000, "60m": 5357000, "40m": 7047500, "30m": 10140000, "20m": 14080000, "17m": 18104000,
             "15m": 21140000, "12m": 24919000, "10m": 28180000, "6m": 50318000, "2m": 144170000}
NEAR_HZ = 1000                 # a dial this close to one of them still hears nearly all its stations
BUSY_ENOUGH = 10               # stations hearing the area on a band before it's named as the busiest

_GRID = re.compile(r"^[A-R]{2}[0-9]{2}$")
_REPORT = re.compile(r"^R?[+-]\d{2}$")
_WORD = re.compile(r"^[A-Z0-9/]{3,12}$")
_NOT_CALLS = {"RR73", "RRR", "73", "CQ", "QRZ", "DE", "TNX", "TU"}


class Short(ValueError):
    """The message ended before a field it should have."""


class Reader:
    """QDataStream's big-endian encoding, for the field types WSJT-X's messages use."""

    def __init__(self, data):
        self.data, self.at = data, 0

    def _take(self, fmt):
        size = struct.calcsize(fmt)
        if self.at + size > len(self.data):
            raise Short(fmt)
        value = struct.unpack_from(fmt, self.data, self.at)[0]
        self.at += size
        return value

    def u8(self):
        return self._take(">B")

    def boolean(self):
        return self._take(">B") != 0

    def u32(self):
        return self._take(">I")

    def i32(self):
        return self._take(">i")

    def u64(self):
        return self._take(">Q")

    def f64(self):
        return self._take(">d")

    def utf8(self):
        """A QByteArray: a 32-bit length then that many bytes; 0xffffffff is a null one (None)."""
        n = self.u32()
        if n == 0xFFFFFFFF:
            return None
        if self.at + n > len(self.data):
            raise Short("utf8")
        text = self.data[self.at:self.at + n].decode("utf-8", "replace")
        self.at += n
        return text


def is_grid(word):
    return bool(_GRID.match(word)) and word != "RR73"


def is_call(word):
    """Could this word of a message be a callsign? (The country look-up has the last word.)"""
    return (bool(_WORD.match(word)) and word not in _NOT_CALLS and not is_grid(word) and not _REPORT.match(word)
            and any(c.isdigit() for c in word) and any(c.isalpha() for c in word))


def sender(text):
    """(callsign, grid square or None, called CQ) for the station that sent an FT8 or FT4 message, or None when
    the message doesn't say (free text, say). "CQ K5SUB EM21" and "CQ DX K5SUB EM21" are K5SUB calling from EM21;
    "BG7IDX N2PPP FN30" is N2PPP answering from FN30; "W7MKE 4U1UN +00" is 4U1UN sending a report. A call shown in
    <angle brackets> is one WSJT-X recognised from its hash; <...> is one it didn't."""
    words = (text or "").strip().upper().split()
    if not words:
        return None
    if words[0] in ("CQ", "QRZ"):
        rest = [w.strip("<>") for w in words[1:]]
        for i, word in enumerate(rest[:2]):        # at most one word (DX, NA, POTA, 290...) before the call
            if is_call(word):
                grid = rest[i + 1] if i + 1 < len(rest) and is_grid(rest[i + 1]) else None
                return word, grid, True
        return None
    if len(words) < 2:
        return None
    call = words[1].strip("<>")
    if not is_call(call):
        return None
    grid = words[2] if len(words) > 2 and is_grid(words[2]) else None
    return call, grid, False


class Listener:
    def __init__(self, place, home=None, say=print, host="127.0.0.1", port=PORT, window=WINDOW, clock=time.time):
        self.place = place             # callsign -> {"country", "region", "lat", "lon", ...} (the server's place_of)
        self.home = home               # (lat, lon) of the station, for distances and beam headings, or None
        self.say = say
        self.host, self.port, self.window = host, port, window
        self.clock = clock
        self._lock = threading.Lock()
        self.stations = {}             # callsign -> what's known about it (see _heard)
        self.dial = {}                 # WSJT-X's id -> (dial frequency in Hz, mode), from its status messages
        self.dial_at = {}              # WSJT-X's id -> when its last status message came
        self.listening = False
        self.problem = None
        self.decodes = 0               # decodes taken in since the app started
        self.last = None               # when the last one came
        self.messages = 0              # messages from WSJT-X of any kind (heartbeats, status, decodes...)
        self.heard_at = None           # when the last one came
        self.bad = 0                   # messages that couldn't be read (cut short)
        self.errors = 0                # messages that went wrong some other way (skipped; see handle)
        self._told = set()             # the kinds of error already in the app's messages

    def start(self):
        threading.Thread(target=self._serve, daemon=True, name="wsjtx-udp").start()
        return self

    def _serve(self):
        while True:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind((self.host, self.port))
            except OSError as exc:
                problem = "another program is using WSJT-X's reporting port %d (%s)" % (self.port, exc.strerror or exc)
                if problem != self.problem:
                    self.say("FT8: " + problem + ", so the map can't show what WSJT-X hears; trying again every minute")
                self.problem, self.listening = problem, False
                time.sleep(60)
                continue
            self.listening, self.problem = True, None
            self.say("FT8: listening for WSJT-X's decodes on 127.0.0.1:%d, for the map" % self.port)
            try:
                while True:
                    try:
                        data = sock.recv(65536)
                    except OSError:
                        break
                    self.handle(data)
            except Exception as exc:          # handle() keeps its own errors; this is only in case
                self._tell(exc)
                time.sleep(5)
            finally:
                self.listening = False
                sock.close()

    def _tell(self, exc):
        """Puts an unexpected error in the app's messages, once for each kind, and counts it."""
        self.errors += 1
        kind = type(exc).__name__
        if kind not in self._told:
            self._told.add(kind)
            self.say("FT8: couldn't make sense of a message from WSJT-X (%s: %s); skipped it, and still listening"
                     % (kind, str(exc)[:160]))

    def handle(self, data):
        """One datagram from WSJT-X (anything else is ignored). Never raises: a message that can't be read is
        counted and skipped, so one odd message can't stop the listening."""
        try:
            r = Reader(data)
            if r.u32() != MAGIC:
                return
            with self._lock:
                self.messages += 1
                self.heard_at = self.clock()
            r.u32()                        # schema: 2 or 3, the same encoding for every field read here
            kind = r.u32()
            sender_id = r.utf8() or ""
            if kind == STATUS:
                dial, mode = r.u64(), r.utf8()
                with self._lock:
                    self.dial[sender_id] = (dial, mode or "")
                    self.dial_at[sender_id] = self.clock()
            elif kind == DECODE:
                new, _ms, snr, dt, df = r.boolean(), r.u32(), r.i32(), r.f64(), r.u32()
                _mode_char, text, low, off_air = r.utf8(), r.utf8(), r.boolean(), r.boolean()
                if new and not low and not off_air:
                    self._heard(sender_id, snr, dt, df, text)
        except Short:
            self.bad += 1
        except Exception as exc:
            self._tell(exc)

    def _heard(self, sender_id, snr, dt, df, text):
        found = sender(text)
        if not found:
            return
        call, grid, cq = found
        place = self.place(call) or {}
        if not place.get("country"):
            return                          # not a callsign the country file knows: probably free text
        now = self.clock()
        with self._lock:
            dial, mode = self.dial.get(sender_id, (None, ""))
            old = self.stations.get(call)
            if not grid and old and old.get("grid") and now - old["t"] <= self.window:
                grid = old["grid"]          # it gave its grid square a little earlier: keep that place
            if grid:
                lat, lon = geo.grid_to_latlon(grid)
            else:
                lat, lon = place.get("lat"), place.get("lon")
            if lat is None or lon is None:
                return
            freq = dial + df if dial else None
            km = bearing = None
            if self.home and self.home[0] is not None:
                km = round(geo.distance_km(self.home[0], self.home[1], lat, lon))
                bearing = round(geo.bearing(self.home[0], self.home[1], lat, lon))
            self.stations[call] = {
                "call": call, "grid": grid, "lat": lat, "lon": lon, "km": km, "bearing": bearing,
                "country": place.get("country", ""), "region": place.get("region", "") if not grid else "",
                "snr": snr, "dt": round(dt, 1), "freq_hz": freq, "band": logbook.band_for(freq), "mode": mode or "FT8",
                "cq": cq, "t": round(now)}
            self.decodes += 1
            self.last = round(now)
            if len(self.stations) > MOST:      # the longest unheard go first
                for gone in sorted(self.stations, key=lambda c: self.stations[c]["t"])[:len(self.stations) - MOST]:
                    del self.stations[gone]

    def status(self):
        """The stations heard in the last window, the most recent first, whether this is listening, and what's
        known of WSJT-X itself: how many messages it has sent, when the last came, and its dial frequency and mode
        (from the WSJT-X that reported last, if there's more than one)."""
        now = self.clock()
        with self._lock:
            for call in [c for c, s in self.stations.items() if now - s["t"] > self.window]:
                del self.stations[call]
            rows = sorted(self.stations.values(), key=lambda s: -s["t"])
            latest = max(self.dial_at, key=self.dial_at.get) if self.dial_at else None
            dial, mode = self.dial.get(latest, (None, "")) if latest is not None else (None, "")
            status_at = self.dial_at.get(latest) if latest is not None else None
            messages, heard_at = self.messages, self.heard_at
        return {"listening": self.listening, "problem": self.problem, "window_min": self.window // 60,
                "decodes": self.decodes, "last": self.last, "stations": rows,
                "messages": messages, "last_message": round(heard_at) if heard_at else None,
                "dial_hz": dial or None, "dial_mode": mode or None, "status_at": round(status_at) if status_at else None,
                "bad": self.bad, "errors": self.errors}


def mhz(hz):
    """14074000 -> "14.074", 7047500 -> "7.0475", 14250000 -> "14.250"."""
    text = "%.4f" % (hz / 1e6)
    return text[:-1] if text.endswith("0") else text


def _button(band):
    return band[:-1] if band.endswith("m") else band     # WSJT-X's band buttons: "20" for 20 m, "70" for 70 cm


def quiet_hint(status, wsjtx_open, open_for=None, udp=None, busiest=None, now=None):
    """Why WSJT-X has heard nobody, as best it can be told, for the health report: the words that follow "none
    heard in the last 15 min".
      status     the listener's status()
      wsjtx_open whether WSJT-X is running; open_for: for how many seconds (None: not known)
      udp        where WSJT-X's settings send its reports, (host, port), or None if not known
      busiest    the band the most stations near here are being heard on just now (PSK Reporter), among the
                 radio's: {"band": "10m", "receivers": 233, "here": n}, here being the count on WSJT-X's band
    While monitoring, WSJT-X sends its status (with the dial frequency) at every decoding pass, and a heartbeat
    every 15 seconds whatever it's doing; so a heartbeat without status for a minute means it isn't decoding."""
    if not wsjtx_open:
        return " (WSJT-X isn't open)"
    now = time.time() if now is None else now
    settled = open_for is None or open_for > SILENT_AFTER
    quiet = None if status.get("last_message") is None else now - status["last_message"]
    if (quiet is None or quiet > SILENT_AFTER) and settled:
        host, port = udp or ("127.0.0.1", PORT)
        host, port = host or "127.0.0.1", str(port or PORT)
        if host not in ("127.0.0.1", "localhost") or port != str(PORT):
            return (": WSJT-X sends its reports to %s, port %s, and the app listens on 127.0.0.1, port %d"
                    " (WSJT-X, File, Settings, Reporting, UDP Server)" % (host, port, PORT))
        return (": WSJT-X hasn't sent the app anything %s (its reports should come to 127.0.0.1, port %d: WSJT-X,"
                " File, Settings, Reporting, UDP Server)" % ("since it opened" if quiet is None
                                                             else "for %d min" % max(1, round(quiet / 60)), PORT))
    stale = status.get("status_at") is None or now - status["status_at"] > SILENT_AFTER
    if stale and settled and quiet is not None and quiet <= SILENT_AFTER:
        return ": WSJT-X isn't decoding (its Monitor button should be green)"
    dial, mode = status.get("dial_hz"), (status.get("dial_mode") or "").upper()
    if not dial:
        return " (WSJT-X hasn't said which frequency it's on yet)"
    band = logbook.band_for(dial)
    if mode not in ("FT8", "FT4"):
        return ": WSJT-X is on %s in %s" % (mhz(dial), mode or "an unknown mode")
    table = FT8_DIALS if mode == "FT8" else FT4_DIALS
    others = FT8_OTHERS if mode == "FT8" else ()
    near = [f for f in list(table.values()) + list(others) if abs(dial - f) <= NEAR_HZ]
    tip = ""
    if busiest and busiest.get("band") != band and busiest.get("band") in table \
            and busiest.get("receivers", 0) >= BUSY_ENOUGH and busiest["receivers"] >= 2 * (busiest.get("here") or 0):
        tip = "; the busiest band from your area now is %s, with %d stations hearing it (WSJT-X's %s button)" % (
            busiest["band"].replace("m", " m"), busiest["receivers"], _button(busiest["band"]))
    if near:
        return " on %s: WSJT-X is on %s, an %s frequency, so the band seems quiet for now%s" % (
            (band or "?").replace("m", " m"), mhz(dial), mode, tip)
    if band in table:
        return ": WSJT-X is on %s, not an %s frequency (%s's is %s, on WSJT-X's %s button)%s" % (
            mhz(dial), mode, band.replace("m", " m"), mhz(table[band]), _button(band), tip)
    return ": WSJT-X is on %s, not an %s frequency (WSJT-X's band buttons tune to one)%s" % (mhz(dial), mode, tip)
