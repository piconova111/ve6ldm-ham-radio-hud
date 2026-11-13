"""A receive-only radio link for WSJT-X.

WSJT-X follows the FT-950 (frequency, mode) through Hamlib's "NET rigctl", but it doesn't reach rigctld
(port 4532) directly: it connects here (127.0.0.1, port 4534 unless [ft8] rig_port says otherwise), and
this passes its requests on to rigctld and rigctld's answers back, with two changes that keep the radio
receiving:

  - Only plain reads and tuning are passed on: frequency, mode, VFO, split, levels and the like, each in
    exactly the form Hamlib's NET rigctl client sends it, one request per line (so a line can't carry a
    second request). Everything else is refused with "RPRT -11" (Hamlib's "feature not available"):
    transmitting (PTT), the tuner's tune (which transmits a carrier), sending CW or a voice memory, raw
    radio commands, turning VOX or break-in on, switching the radio off, and anything not on the list.
  - rigctld's description of the radio ("\\dump_state") comes back saying it has no PTT (ptt_type=0x0), so
    WSJT-X doesn't try to transmit through the radio link at all.

Why both: WSJT-X set to PTT method VOX tells Hamlib "no PTT", but Hamlib's NET rigctl client replaces that
with the PTT type rigctld reports when it connects (seen with Hamlib 4.7.1, the version inside WSJT-X 3.0.2,
against rigctld 4.5.5), so WSJT-X on its own would key the radio by CAT whenever it transmits. With the
description changed, VOX means no PTT again; and if someone sets WSJT-X to PTT method CAT, its request is
refused here and WSJT-X says so.

The station app's own link to rigctld doesn't come through here.
"""
import re
import socket
import threading
import time

REFUSED = b"RPRT -11\n"       # Hamlib's -RIG_ENAVAIL, "feature not available"
MAX_LINE = 1024

_NAME = r"[A-Za-z][A-Za-z0-9_]*"
_NUM = r"[-+]?[0-9]+(?:\.[0-9]*)?(?:[eE][-+]?[0-9]+)?"
_INT = r"[-+]?[0-9]+"
_OPS = r"(?:TOGGLE|CPY|XCHG|FROM_VFO|TO_VFO|MCL|UP|DOWN|BAND_UP|BAND_DOWN|LEFT|RIGHT)"   # never TUNE
_PASS = [
    # Each with exactly the arguments rigctld reads for it (none of these take a VFO: rigctld runs without
    # --vfo), so rigctld never reads past the end of the line for more, and nothing is left over on it.
    r"[fmvstijzrnocdx_qQ]",              # reads: frequency, mode, VFO, split, PTT (read only), split freq, RIT,
                                         # XIT, repeater shift, step, offset, tones, split mode, info;
                                         # q closes the connection
    r"y " + _INT,                        # which antenna (a read; rigctld wants the antenna asked about)
    r"\\(?:chk_vfo|dump_state|get_powerstat|get_dcd|get_ctcss_sql|get_dcs_sql|get_lock_mode|get_trn"
    r"|get_vfo_list|get_modes|get_rig_info|recv_dtmf)",
    r"\\get_vfo_info " + _NAME,
    r"\\get_mode_bandwidths " + _NAME,
    r"\\power2mW " + _NUM + " " + _NUM + " " + _NAME,
    r"\\mW2power " + _NUM + " " + _NUM + " " + _NAME,
    r"[lup] " + _NAME,                   # read a level, function or setting
    r"F " + _NUM,                        # tune
    r"I " + _NUM,                        # where it would transmit when split
    r"M " + _NAME + " " + _INT,          # mode and filter width
    r"X " + _NAME + " " + _INT,          # split mode
    r"K " + _NUM + " " + _NAME + " " + _INT,
    r"V " + _NAME,                       # VFO
    r"S [01] " + _NAME,                  # split on/off
    r"J " + _INT,                        # RIT
    r"Z " + _INT,                        # XIT
    r"G " + _OPS,                        # VFO operations (swap, copy, up/down), not the tuner's tune
]
_ALLOWED = re.compile(r"(?:%s)\r?" % "|".join(_PASS))
# What a refused request was, for the messages (transmit-type requests are named as such).
_TRANSMIT = re.compile(rb"^\s*[+;|,]?\s*(?:T|\\set_ptt|b|\\send_morse|\\send_voice_mem|\\send_dtmf|w|W|\\send_cmd"
                       rb"|\\send_cmd_rx|\\send_raw|G\s+TUNE|\\vfo_op\s+TUNE|U\s+(?:VOX|SBKIN|FBKIN)"
                       rb"|\\set_func\s+(?:VOX|SBKIN|FBKIN))(?:\s|$|[0-9])")


def allowed(line):
    """Is this request (one line, without its newline) one that's passed on to rigctld?"""
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        return False
    return _ALLOWED.fullmatch(text) is not None


def is_transmit(line):
    """Would this refused request have made the radio transmit (or set it up to)?"""
    return _TRANSMIT.match(line) is not None


class CatGuard:
    def __init__(self, port=4534, rig_port=4532, say=print, host="127.0.0.1", rig_host="127.0.0.1"):
        self.port, self.rig_port, self.host, self.rig_host = port, rig_port, host, rig_host
        self.say = say
        self.lock = threading.Lock()
        self.connected = 0             # programs connected now (WSJT-X)
        self.since = None              # when the first of them connected
        self.passed = 0                # requests passed on to rigctld
        self.refused = 0               # requests refused
        self.transmit_refused = 0      # ...of which would have transmitted
        self.last_refused = None       # (text, time)
        self.problem = None
        self.listening = False

    def status(self):
        with self.lock:
            last = self.last_refused
            return {"port": self.port, "listening": self.listening, "connected": self.connected,
                    "since": round(self.since) if self.since else None, "passed": self.passed,
                    "refused": self.refused, "transmit_refused": self.transmit_refused,
                    "last_refused": last[0] if last else None,
                    "last_refused_at": round(last[1]) if last else None, "problem": self.problem}

    def start(self):
        threading.Thread(target=self._serve, daemon=True, name="catguard").start()
        return self

    # ---------- listening ----------

    def _serve(self):
        while True:
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((self.host, self.port))
                srv.listen(4)
                break
            except OSError as exc:
                srv.close()
                text = "WSJT-X's radio link can't use port %d (%s); trying again every 30 s" % (self.port, exc.strerror or exc)
                if text != self.problem:
                    self.say("FT8: " + text)
                self.problem = text
                time.sleep(30)
        self.problem = None
        self.listening = True
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                time.sleep(1)
                continue
            threading.Thread(target=self._client, args=(conn,), daemon=True, name="catguard-client").start()

    # ---------- one program (WSJT-X) ----------

    def _client(self, conn):
        try:
            rig = socket.create_connection((self.rig_host, self.rig_port), timeout=5)
            rig.settimeout(None)
        except OSError as exc:
            text = "WSJT-X asked for the radio, but the radio link (rigctld, port %d) isn't answering (%s)" % (
                self.rig_port, exc.strerror or exc)
            if text != self.problem:
                self.say("FT8: " + text)
            self.problem = text
            conn.close()
            return
        self.problem = None
        send_lock = threading.Lock()   # both directions write to the program's socket
        with self.lock:
            self.connected += 1
            if self.connected == 1:
                self.since = time.time()
        if self.connected == 1:
            self.say("FT8: WSJT-X connected to the radio through the station's receive-only link")
        told = set()
        answers = threading.Thread(target=self._answers, args=(rig, conn, send_lock), daemon=True,
                                   name="catguard-answers")
        answers.start()
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if allowed(line):
                        rig.sendall(line + b"\n")
                        with self.lock:
                            self.passed += 1
                    else:
                        self._refuse(conn, send_lock, line, told)
                if len(buf) > MAX_LINE:   # no newline in sight: not Hamlib talking
                    self._refuse(conn, send_lock, buf[:60], told)
                    break
        except OSError:
            pass
        finally:
            for s in (rig, conn):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
            with self.lock:
                self.connected -= 1
                if self.connected == 0:
                    self.since = None
            if self.connected == 0:
                self.say("FT8: WSJT-X disconnected from the radio link")

    def _refuse(self, conn, send_lock, line, told):
        text = line.decode("ascii", "replace").strip()[:60]
        transmit = is_transmit(line)
        with self.lock:
            self.refused += 1
            if transmit:
                self.transmit_refused += 1
            self.last_refused = (text, time.time())
        name = text.split(" ")[0] if text else "(blank)"
        if name not in told:   # once per kind of request per connection: WSJT-X may repeat it
            told.add(name)
            if transmit:
                self.say("FT8: kept WSJT-X from making the radio transmit (it asked: %s)" % text)
            else:
                self.say("FT8: refused a radio request from WSJT-X that isn't a plain read or tuning (%s)" % text)
        with send_lock:
            conn.sendall(REFUSED)

    @staticmethod
    def _answers(rig, conn, send_lock):
        """rigctld's answers back to the program, with the radio described as having no PTT."""
        buf = b""
        try:
            while True:
                data = rig.recv(4096)
                if not data:
                    break
                buf += data
                out = []
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.startswith(b"ptt_type="):
                        line = b"ptt_type=0x0"
                    out.append(line + b"\n")
                # hold back only what could still turn into a ptt_type line
                if buf and not (b"ptt_type=".startswith(buf) or buf.startswith(b"ptt_type=")):
                    out.append(buf)
                    buf = b""
                if out:
                    with send_lock:
                        conn.sendall(b"".join(out))
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
