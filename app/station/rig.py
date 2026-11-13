"""Talks to Hamlib's rigctld, the radio link.

It reads the frequency, mode, S-meter and whether the radio is transmitting
(rigctld's lower-case "t", which only reads), and can change frequency when asked.
While the radio transmits it also reads the SWR and the output power (read-only meter readings).
For working split it reads, and when asked sets, where the radio transmits (VFO B): that only
sets the radio up for when the operator transmits.
It never sends a transmit (PTT) command: that would be the capital "T", which this file never uses.

Uses rigctld's extended replies ("+f" etc.), which always end with "RPRT <code>".
"""
import re
import socket

FT950 = 1028   # Hamlib's model number for the Yaesu FT-950
# The FT-950's power-output meter (CAT command RM5; reply RM5nnn;), raw reading -> watts.
# The same table Hamlib uses for this radio (rigs/yaesu/ft950.h, FT950_RFPOWER_METER_CAL).
FT950_POWER_CAL = [(0, 0.0), (10, 0.8), (50, 8.0), (100, 26.0), (150, 54.0), (200, 92.0), (250, 140.0)]
RM5 = re.compile(r"RM5(\d{3});")


def _interpolate(table, raw):
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if raw <= x1:
            return y0 + (y1 - y0) * (raw - x0) / (x1 - x0)
    return table[-1][1]


def _value(lines, key):
    for line in lines:
        if line.lower().startswith(key.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return None


def _last_number(lines):
    for line in reversed(lines):
        token = line.split()[-1] if line.split() else ""
        try:
            return float(token)
        except ValueError:
            continue
    return None


class Rig:
    def __init__(self, host="127.0.0.1", port=4532, timeout=1.0, model=None, max_watts=100):
        self.host, self.port, self.timeout = host, port, timeout
        self.model, self.max_watts = model, max_watts
        self.raw_power_fails = 0   # the raw meter read is given up after a few failures

    @staticmethod
    def _ask(sock, reader, command):
        sock.sendall(("+" + command + "\n").encode("ascii"))
        lines = []
        while True:
            raw = reader.readline()
            if not raw:
                raise ConnectionError("rigctld closed the connection")
            line = raw.decode("ascii", "replace").strip()
            if line.startswith("RPRT"):
                parts = line.split()
                return (int(parts[1]) if len(parts) > 1 else -1), lines
            if line:
                lines.append(line)

    def status(self, split=False):
        """{'ok': True, 'freq_hz': 14205000, 'mode': 'USB', 's_db': -12.0, 'tx': False} or {'ok': False, 'reason': ...}.

        tx is True while the radio transmits, and None if the radio can't say.
        reason is "link" when the radio link program (rigctld) isn't running, "quiet" when it runs
        but the radio doesn't answer (usually switched off).
        With split=True it also reads the radio's split: 'split' is {'on': True, 'tx_hz': 14245000},
        {'on': False}, or None if the radio can't say.
        """
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                reader = sock.makefile("rb")
                code, lines = self._ask(sock, reader, "f")
                freq = _value(lines, "Frequency") if code == 0 else None
                if freq is None:
                    return {"ok": False}
                code, lines = self._ask(sock, reader, "m")
                mode = _value(lines, "Mode") if code == 0 else None
                width = _value(lines, "Passband") if code == 0 else None   # the receive filter's width, Hz
                code, lines = self._ask(sock, reader, "l STRENGTH")
                s_db = _last_number(lines) if code == 0 else None
                code, lines = self._ask(sock, reader, "t")  # read-only: is it transmitting?
                ptt = _value(lines, "PTT") if code == 0 else None
                tx = None if ptt is None else ptt.strip() not in ("0", "")
                out = {"ok": True, "freq_hz": int(float(freq)), "mode": mode, "s_db": s_db, "tx": tx,
                       "width_hz": int(float(width)) if width and width.strip().lstrip("-").replace(".", "", 1).isdigit() else None}
                if split:
                    out["split"] = self._split(sock, reader)
                return out
        except ConnectionRefusedError:
            return {"ok": False, "reason": "link"}
        except (OSError, ValueError):
            return {"ok": False, "reason": "quiet"}

    def meters(self):
        """While transmitting: {'swr': 1.3, 'power_w': 87.0, 'power_set_w': 100.0}; any can be None.

        Read on a connection of its own, so a slow meter can never make the radio look disconnected.
        """
        out = {"swr": None, "power_w": None, "power_set_w": None}
        try:
            with socket.create_connection((self.host, self.port), timeout=1.5) as sock:
                reader = sock.makefile("rb")
                code, lines = self._ask(sock, reader, "l SWR")
                if code == 0:
                    out["swr"] = _last_number(lines)
                if self.model == FT950 and self.raw_power_fails < 3:
                    # Hamlib 4.5 has no power-meter level for the FT-950, so ask the radio directly:
                    # "RM5;" only reads the meter. The reply is read up to its ";".
                    code, lines = self._ask(sock, reader, "W RM5; ;")
                    hit = RM5.search(" ".join(lines))
                    if code == 0 and hit:
                        out["power_w"] = round(_interpolate(FT950_POWER_CAL, int(hit.group(1))), 1)
                        self.raw_power_fails = 0
                    else:
                        self.raw_power_fails += 1
                elif self.model not in (None, 0, 1, FT950) and self.raw_power_fails < 3:
                    # Other radios: Hamlib's own power-meter levels, where it has them.
                    code, lines = self._ask(sock, reader, "l RFPOWER_METER_WATTS")
                    watts = _last_number(lines) if code == 0 else None
                    if watts is None:
                        code, lines = self._ask(sock, reader, "l RFPOWER_METER")   # 0 to 1 of full power
                        level = _last_number(lines) if code == 0 else None
                        watts = None if level is None else level * self.max_watts
                    if watts is not None:
                        out["power_w"] = round(watts, 1)
                        self.raw_power_fails = 0
                    else:
                        self.raw_power_fails += 1
                code, lines = self._ask(sock, reader, "l RFPOWER")   # the power knob's setting, 0 to 1
                level = _last_number(lines) if code == 0 else None
                if level is not None:
                    out["power_set_w"] = round(level * self.max_watts)
        except (OSError, ValueError):
            if self.model not in (None, 0, 1):
                self.raw_power_fails += 1
        return out

    def _split(self, sock, reader):
        """The radio's split, read only (rigctld's lower-case "s" and "i")."""
        try:
            code, lines = self._ask(sock, reader, "s")
            on = _value(lines, "Split") if code == 0 else None
            if on is None:
                return None
            if on.strip() in ("0", ""):
                return {"on": False}
            code, lines = self._ask(sock, reader, "i")
            tx = _value(lines, "TX Frequency") if code == 0 else None
            return {"on": True, "tx_hz": int(float(tx)) if tx else None}
        except ValueError:
            return None

    def set_split(self, tx_hz, mode=None, rx_hz=None):
        """Sets the radio up to work split: it listens where it's tuned (VFO A) and transmits on tx_hz
        (VFO B). With rx_hz it's tuned there first (and mode, USB or LSB, is set). This only sets
        where the radio transmits; it never transmits. Returns the split the radio reports afterwards
        ({'on': True, 'tx_hz': ...}), or None if it didn't take it."""
        tx_hz = int(tx_hz)
        if not 100_000 <= tx_hz <= 470_000_000:
            raise ValueError("That frequency is out of range.")
        if mode not in (None, "USB", "LSB"):
            raise ValueError("Only USB or LSB can be set.")
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                reader = sock.makefile("rb")
                code, lines = self._ask(sock, reader, "v")
                on_b = code == 0 and (_value(lines, "VFO") or "").upper() == "VFOB"
                if rx_hz is not None:
                    if on_b:   # listen on VFO A, so VFO B is free to transmit on
                        self._ask(sock, reader, "V VFOA")
                    code, _ = self._ask(sock, reader, "F %d" % int(rx_hz))
                    if code != 0:
                        return None
                    if mode:
                        self._ask(sock, reader, "M %s 0" % mode)
                elif on_b:
                    raise ValueError("the radio is listening on VFO B: switch it to VFO A to set split from here")
                code, lines = self._ask(sock, reader, "s")
                was_on = code == 0 and (_value(lines, "Split") or "0").strip() not in ("0", "")
                copied = False
                if not was_on:   # VFO B starts as a copy of VFO A, so it has the same mode
                    code, _ = self._ask(sock, reader, "G CPY")
                    copied = code == 0
                code, _ = self._ask(sock, reader, "S 1 VFOB")   # before "I": Hamlib sets the transmit VFO's frequency
                if code != 0:
                    return None
                code, _ = self._ask(sock, reader, "I %d" % tx_hz)
                if code != 0:   # don't leave split on with VFO B wherever it happened to be
                    if not was_on:
                        self._ask(sock, reader, "S 0 VFOA")
                    return None
                if not was_on and not copied and mode:
                    self._ask(sock, reader, "X %s 0" % mode)   # radios that can't copy A to B: best effort
                    self._ask(sock, reader, "S 1 VFOB")         # (some drop split while setting VFO B's mode)
                return self._split(sock, reader)
        except OSError:
            return None

    def clear_split(self):
        """Split off: the radio transmits where it listens again. Returns True if the radio took it."""
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                reader = sock.makefile("rb")
                code, _ = self._ask(sock, reader, "S 0 VFOA")
                return code == 0
        except OSError:
            return False

    def tune(self, freq_hz, mode=None):
        """Changes frequency (and, for voice spots, the sideband). Returns True if the radio accepted it."""
        freq_hz = int(freq_hz)
        if not 100_000 <= freq_hz <= 470_000_000:
            raise ValueError("That frequency is out of range.")
        if mode not in (None, "USB", "LSB"):
            raise ValueError("Only USB or LSB can be set.")
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                reader = sock.makefile("rb")
                code, _ = self._ask(sock, reader, "F %d" % freq_hz)
                if code == 0 and mode:
                    self._ask(sock, reader, "M %s 0" % mode)  # best effort; the frequency is what matters
                return code == 0
        except OSError:
            return False
