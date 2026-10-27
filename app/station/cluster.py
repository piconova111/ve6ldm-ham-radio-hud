"""Live DX spots from a DX cluster node (a plain telnet connection).

It logs in as a listener (the station's callsign with -2 added by default, so it never clashes
with another program using the plain callsign) and only reads. It never posts spots or
announcements. It reconnects by itself if the connection drops.

There's a short list of nodes: the one in the settings first, then backups. A node
that says nothing at all after connecting (on Oct 26, 2026 VE7CC accepted connections
and then stayed silent), or that stops sending spots for a while, is left for the next
one. On a backup, the first node is tried again every hour.
"""
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from .logbook import band_for

UTC = timezone.utc
# DX de W3LPL:     14205.0  JA1VQB       CQ CQ                          1549Z FN20
LIVE = re.compile(r"^DX de (\S+?):?\s+(\d+(?:\.\d+)?)\s+(\S+)\s+(.*?)\s*(\d{4})Z(?:\s+\S+)?\s*$", re.I)
#   14205.0  JA1VQB       2-Oct-2026 1549Z  CQ CQ                  <W3LPL>
SHDX = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(\S+)\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\s+(\d{4})Z\s*(.*?)\s*<(\S+)>\s*$")
TELNET_NOISE = re.compile(rb"\xff[\xfb-\xfe].|\xff[\xf0-\xfa]", re.S)
CALL = re.compile(r"^[A-Z0-9/]{3,15}$")
# Commands sent after logging in. CC Cluster understands them all; other nodes
# just reply "unknown command", which is harmless.
AFTER_LOGIN = ("set/noft8", "set/noft4", "set/nobeacon", "sh/dx 60")
# Telnet clusters that answered from the station on Oct 26, 2026 (NC7J and K1TTT run AR-Cluster,
# W3LPL DX-Spider, VE7CC CC Cluster); the app uses the one in the settings first.
BACKUPS = (("dxc.ve7cc.net", 23), ("dxc.nc7j.com", 7373), ("k1ttt.net", 7373), ("w3lpl.net", 7373))


def _minutes(seconds):
    m = max(1, round(seconds / 60))
    return "%d minute%s" % (m, "" if m == 1 else "s")


def parse_nodes(text):
    """'dxc.nc7j.com:7373, k1ttt.net:7373' -> [("dxc.nc7j.com", 7373), ("k1ttt.net", 7373)]"""
    nodes = []
    for part in str(text or "").replace(";", ",").split(","):
        host, _, port = part.strip().partition(":")
        if host and re.match(r"^[A-Za-z0-9.-]+$", host):
            try:
                nodes.append((host.lower(), int(port or 23)))
            except ValueError:
                continue
    return nodes


def say(message):
    sys.stdout.write("%s\n" % message)   # one write, so lines from different threads don't run together
    sys.stdout.flush()


class Cluster:
    PROMPT_WAIT = int(os.environ.get("STATION_CLUSTER_PROMPT", "20"))   # seconds for a node to say anything
    NUDGE = int(os.environ.get("STATION_CLUSTER_NUDGE", "120"))         # seconds of quiet before a nudge
    SPOT_QUIET = int(os.environ.get("STATION_CLUSTER_QUIET", "1200"))   # no spots this long: try another node
    GO_HOME = int(os.environ.get("STATION_CLUSTER_HOME", "3600"))       # on a backup this long: try the first again

    def __init__(self, host, port, login, on_spot=None, keep_minutes=90, backups=BACKUPS):
        first = (host.lower(), int(port))
        self.nodes = [first] + [n for n in dict.fromkeys(backups) if n != first]
        self.host, self.port, self.login = first[0], first[1], login   # the node in use now
        self.on_spot = on_spot
        self.keep = timedelta(minutes=keep_minutes)
        self.status = "starting"
        self._spots = {}
        self._spotted = False
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="cluster").start()

    # ---------- connection ----------
    def _run(self):
        i, failures, delay = 0, 0, 15
        while True:
            self.host, self.port = self.nodes[i]
            began = time.time()
            self._spotted = False
            outcome = None
            try:
                self.status = "connecting"
                with socket.create_connection((self.host, self.port), timeout=30) as sock:
                    outcome = self._session(sock, first=(i == 0))
            except Exception as exc:
                say("DX cluster %s:%s: %s" % (self.host, self.port, exc))
            self.status = "reconnecting"
            if outcome == "home":                 # an hour on a backup: is the first node back?
                say("DX cluster: trying %s:%s again" % self.nodes[0])
                i, failures = 0, 0
                time.sleep(2)
                continue
            if self._spotted and time.time() - began > 300:
                failures, delay = 0, 15           # it had been working: the same node, soon
                time.sleep(delay)
                continue
            failures += 1
            if failures < len(self.nodes):        # this one isn't working: the next one on the list
                i = (i + 1) % len(self.nodes)
                say("DX cluster: trying %s:%s" % self.nodes[i])
                time.sleep(3)
                continue
            # every node failed in a row: wait (longer each round), then start again from the first
            i, failures = 0, 0
            time.sleep(delay)
            delay = min(delay * 2, 600)

    def _session(self, sock, first):
        """Reads spots until the connection fails, or returns "home" when it's time to try the first node again."""
        sock.settimeout(self.PROMPT_WAIT)
        buf = b""
        logged_in = False        # our call has been sent
        answered = False         # and the node has said something since
        quiet = 0
        started = last_spot = time.time()
        while True:
            now = time.time()
            if answered and now - last_spot > self.SPOT_QUIET:
                raise ConnectionError("no spots for %s" % _minutes(self.SPOT_QUIET))
            if answered and not first and now - started > self.GO_HOME:
                return "home"
            try:
                data = sock.recv(4096)
            except socket.timeout:
                if not logged_in:     # no login prompt yet: send the call anyway, once
                    self._log_in(sock)
                    logged_in = True
                    continue
                if not answered:      # not a word, even after the call: this node isn't working
                    raise ConnectionError("the node isn't answering")
                quiet += 1
                if quiet >= 5:
                    raise ConnectionError("no data for %s" % _minutes(5 * self.NUDGE))
                sock.sendall(b"\r\n")  # a nudge; nodes answer with a prompt
                continue
            if not data:
                raise ConnectionError("the cluster closed the connection")
            quiet = 0
            buf = TELNET_NOISE.sub(b"", buf + data)
            if not logged_in:
                tail = buf.rsplit(b"\n", 1)[-1].decode("latin-1").strip().lower()
                if tail.endswith(":") and ("call" in tail or "login" in tail):
                    self._log_in(sock)
                    logged_in = True
                    buf = b""
                continue
            if not answered:
                answered = True
                last_spot = time.time()
                sock.settimeout(self.NUDGE)
                self.status = "connected"
                say("DX cluster: connected to %s:%s as %s" % (self.host, self.port, self.login))
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("latin-1").strip()
                if LIVE.match(text) or SHDX.match(text):
                    last_spot = time.time()
                    self._spotted = True
                self.parse_line(text)
            if len(buf) > 8192:
                buf = b""

    def _log_in(self, sock):
        sock.sendall(self.login.encode("ascii") + b"\r\n")
        for command in AFTER_LOGIN:
            sock.sendall(command.encode("ascii") + b"\r\n")
        self.status = "logging in"

    # ---------- spots ----------
    def parse_line(self, line):
        m = LIVE.match(line)
        if m:
            spotter, freq, call, comment, hhmm = m.groups()
            now = datetime.now(UTC)
            when = now.replace(hour=int(hhmm[:2]) % 24, minute=int(hhmm[2:]) % 60, second=0, microsecond=0)
            if when > now + timedelta(minutes=5):
                when -= timedelta(days=1)
        else:
            m = SHDX.match(line)
            if not m:
                return None
            freq, call, day, hhmm, comment, spotter = m.groups()
            try:
                when = datetime.strptime("%s %s" % (day, hhmm), "%d-%b-%Y %H%M").replace(tzinfo=UTC)
            except ValueError:
                return None
        return self.add(call, float(freq), spotter, comment, when)

    def add(self, call, freq_khz, spotter, comment, when):
        call = call.strip().upper()
        band = band_for(int(freq_khz * 1000))
        if not band or not CALL.match(call):
            return None
        spot = {"call": call, "freq_khz": round(freq_khz, 1), "band": band,
                "spotter": spotter.strip().upper().rstrip(":").split("-")[0],
                "comment": comment.strip()[:60], "time": when}
        with self._lock:
            old = self._spots.get((call, band))
            if old and old["time"] > when:
                return None  # we already have a newer spot of this station on this band
            self._spots[(call, band)] = spot
            cutoff = datetime.now(UTC) - self.keep
            for key in [k for k, s in self._spots.items() if s["time"] < cutoff]:
                del self._spots[key]
        if self.on_spot:
            self.on_spot(spot)
        return spot

    def spots(self, minutes=30):
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        with self._lock:
            items = [dict(s) for s in self._spots.values() if s["time"] >= cutoff]
        return sorted(items, key=lambda s: s["time"], reverse=True)
