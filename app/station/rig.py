"""Talks to Hamlib's rigctld, the radio link.

It reads the frequency, mode, S-meter and whether the radio is transmitting
(rigctld's lower-case "t", which only reads), and can change frequency when asked.
While the radio transmits it also reads the SWR and the output power (read-only meter readings).
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

    def status(self):
        """{'ok': True, 'freq_hz': 14205000, 'mode': 'USB', 's_db': -12.0, 'tx': False} or {'ok': False, 'reason': ...}.

        tx is True while the radio transmits, and None if the radio can't say.
        reason is "link" when the radio link program (rigctld) isn't running, "quiet" when it runs
        but the radio doesn't answer (usually switched off).
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
                code, lines = self._ask(sock, reader, "l STRENGTH")
                s_db = _last_number(lines) if code == 0 else None
                code, lines = self._ask(sock, reader, "t")  # read-only: is it transmitting?
                ptt = _value(lines, "PTT") if code == 0 else None
                tx = None if ptt is None else ptt.strip() not in ("0", "")
                return {"ok": True, "freq_hz": int(float(freq)), "mode": mode, "s_db": s_db, "tx": tx}
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
