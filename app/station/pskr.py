"""Who can hear you, from PSK Reporter's live feed.

PSK Reporter (pskreporter.info) collects reports from stations that decode digital
signals such as FT8. Its live feed (mqtt.pskreporter.info, run by Tom M0LTE with
PSK Reporter's permission) sends each report as it happens; nothing is polled.

The station listens for two things:
  * signals sent from the nine grid squares around the station (for VE6LDM, DO32 and
    its neighbours, about 400 by 330 km). Where those are being heard shows which bands
    are open from here and to where, even while the station is on voice;
  * reports of the station's own signals, for when it runs FT8.

The feed is MQTT. This is a minimal MQTT 3.1.1 client (subscribe only, QoS 0), so
the app still needs nothing beyond Python itself.
"""
import json
import os
import random
import socket
import ssl
import struct
import sys
import threading
import time
from pathlib import Path

from . import geo

HOST = "mqtt.pskreporter.info"
PORT_TLS, PORT_PLAIN = 1884, 1883
WINDOW = 15 * 60          # band openings: the last 15 minutes
KEEP_OWN = 24 * 3600      # reports of the station's own signals: the last day
BAND_ORDER = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m", "2m", "70cm"]
MAX_POINTS = 400          # receivers per band sent to the display


def say(message):
    sys.stdout.write("%s\n" % message)   # one write, so lines from different threads don't run together
    sys.stdout.flush()


def neighbours(grid):
    """The 4-character grid square and the eight around it, e.g. DO32 -> DO21 ... DO43."""
    grid = (grid or "").upper()[:4]
    if len(grid) < 4 or not geo.valid_grid(grid):
        return []
    lon_i = (ord(grid[0]) - 65) * 10 + int(grid[2])      # 0..179, 2 degrees each
    lat_i = (ord(grid[1]) - 65) * 10 + int(grid[3])      # 0..179, 1 degree each
    out = []
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            x, y = (lon_i + dlon) % 180, lat_i + dlat
            if 0 <= y < 180:
                out.append("%s%s%d%d" % (chr(65 + x // 10), chr(65 + y // 10), x % 10, y % 10))
    return out


def base_call(call):
    """VE6ABC/P -> VE6ABC, VE6/K1ABC -> K1ABC (the longest part with a digit and a letter)."""
    parts = [p for p in (call or "").upper().split("/") if p]
    good = [p for p in parts if any(c.isdigit() for c in p) and any(c.isalpha() for c in p)]
    return max(good, key=len) if good else (parts[0] if parts else "")


# ---------- a minimal MQTT client ----------
def _string(text):
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _length(n):
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


class MQTTError(Exception):
    pass


class Subscriber:
    """Connects, subscribes, and hands every message to on_message(topic, payload). Reconnects by itself."""

    def __init__(self, host, topics, on_message, port=None, tls=True, keepalive=60, client_id=None):
        self.host, self.topics, self.on_message = host, topics, on_message
        self.port, self.tls, self.keepalive = port, tls, keepalive
        self.client_id = client_id or "ham-radio-hud-%06x" % random.randrange(16 ** 6)
        self.connected = False
        self.last_error = ""
        self._stop = False

    def _recv_exact(self, sock, n):
        data = bytearray()
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise MQTTError("the feed closed the connection")
            data += chunk
        return bytes(data)

    def _read_packet(self, sock):
        first = self._recv_exact(sock, 1)[0]
        size, mult = 0, 1
        for _ in range(4):
            byte = self._recv_exact(sock, 1)[0]
            size += (byte & 0x7F) * mult
            mult *= 128
            if not byte & 0x80:
                break
        else:
            raise MQTTError("bad packet length")
        if size > 256 * 1024:
            raise MQTTError("packet too big")
        return first, self._recv_exact(sock, size) if size else b""

    def _open(self, port, tls):
        raw = socket.create_connection((self.host, port), timeout=20)
        if not tls:
            return raw
        context = ssl.create_default_context()
        return context.wrap_socket(raw, server_hostname=self.host)

    def _session(self, port, tls):
        sock = self._open(port, tls)
        try:
            variable = _string("MQTT") + bytes([4, 0x02]) + struct.pack(">H", self.keepalive)   # clean session
            payload = _string(self.client_id)
            sock.sendall(bytes([0x10]) + _length(len(variable) + len(payload)) + variable + payload)
            kind, data = self._read_packet(sock)
            if kind >> 4 != 2 or len(data) < 2 or data[1] != 0:
                raise MQTTError("the feed refused the connection (%r)" % data)
            body = struct.pack(">H", 1) + b"".join(_string(t) + b"\x00" for t in self.topics)
            sock.sendall(bytes([0x82]) + _length(len(body)) + body)
            self.connected, self.last_error = True, ""
            say("PSK Reporter feed connected (%s:%d), listening for %d topics" % (self.host, port, len(self.topics)))
            sock.settimeout(5)
            last_in = last_out = time.time()
            while not self._stop:
                now = time.time()
                if now - last_out > self.keepalive / 2:
                    sock.sendall(b"\xc0\x00")      # PINGREQ
                    last_out = now
                if now - last_in > self.keepalive * 2:
                    raise MQTTError("the feed went quiet")
                try:
                    kind, data = self._read_packet(sock)
                except socket.timeout:
                    continue
                last_in = time.time()
                if kind >> 4 == 3:                 # PUBLISH
                    qos = (kind >> 1) & 3
                    size = struct.unpack(">H", data[:2])[0]
                    topic = data[2:2 + size].decode("utf-8", "replace")
                    start = 2 + size + (2 if qos else 0)
                    if qos == 1:                   # not expected (we ask for QoS 0), but acknowledge it
                        sock.sendall(b"\x40\x02" + data[2 + size:4 + size])
                    try:
                        self.on_message(topic, data[start:])
                    except Exception as exc:       # one bad report mustn't stop the feed
                        self.last_error = "bad report: %s" % exc
        finally:
            self.connected = False
            try:
                sock.close()
            except OSError:
                pass

    def run(self):
        """Keeps a connection up: TLS on 1884 first, plain 1883 if that doesn't connect."""
        choices = [(self.port, self.tls)] if self.port else [(PORT_TLS, True), (PORT_PLAIN, False)]
        attempt, wait = 0, 5
        while not self._stop:
            port, tls = choices[attempt % len(choices)]
            started = time.time()
            try:
                self._session(port, tls)
            except Exception as exc:
                self.last_error = str(exc)
                if time.time() - started > 120:
                    wait = 5                       # it had been working: reconnect quickly
                say("PSK Reporter feed: %s (trying again in %d s)" % (exc, wait))
            attempt += 1
            time.sleep(wait)
            wait = min(wait * 2, 300)

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="pskr").start()


# ---------- what the reports say ----------
class Openings:
    """Band openings from the station's area, and who decoded the station itself."""

    def __init__(self, data_dir, callsign, grid, lat, lon, lookup, host=HOST, port=None, tls=True):
        self.callsign = (callsign or "").upper()
        self.lat, self.lon = lat, lon
        self.lookup = lookup                     # callsign -> {"name", "continent", ...} or None
        self.squares = neighbours(grid)
        self.own_path = Path(data_dir) / "psk-heard.json"
        topics = ["pskr/filter/v2/+/+/+/+/%s/+/+/+" % sq for sq in self.squares]
        if self.callsign:
            topics.append("pskr/filter/v2/+/+/%s/#" % self.callsign)
        self.feed = Subscriber(host, topics, self._message, port=port, tls=tls) if topics else None
        self._lock = threading.Lock()
        self.area = {}                           # (band, receiver) -> report
        self.own = {}                            # (band, receiver) -> report
        self.seen = []                           # recent sequence numbers (overlapping topics send copies)
        self.messages = 0
        self.last_message = None
        self._saved = 0.0
        try:
            for r in json.loads(self.own_path.read_text()):
                self.own[(r["band"], r["call"])] = r
        except Exception:
            pass

    def start(self):
        if self.feed:
            self.feed.start()

    def _message(self, topic, payload):
        spot = json.loads(payload)
        seq = spot.get("sq")
        now = time.time()
        with self._lock:
            if seq is not None:
                if seq in self.seen:
                    return
                self.seen.append(seq)
                if len(self.seen) > 500:
                    del self.seen[:250]
            self.messages += 1
            self.last_message = now
        band = str(spot.get("b") or "").lower()
        receiver = str(spot.get("rc") or "").upper()
        locator = str(spot.get("rl") or "")[:6]
        if not band or not receiver or not geo.valid_grid(locator[:4]):
            return
        if len(locator) == 6 and not geo.valid_grid(locator):
            locator = locator[:4]
        lat, lon = geo.grid_to_latlon(locator)
        when = spot.get("t") or now
        try:
            when = float(when)
        except (TypeError, ValueError):
            when = now
        when = min(when, now)
        place = self.lookup(receiver) or {}
        report = {
            "call": receiver, "band": band, "mode": str(spot.get("md") or ""), "snr": spot.get("rp"),
            "time": round(when), "lat": round(lat, 2), "lon": round(lon, 2),
            "km": round(geo.distance_km(self.lat, self.lon, lat, lon)),
            "bearing": round(geo.bearing(self.lat, self.lon, lat, lon)),
            "country": place.get("name") or "", "continent": place.get("continent") or "",
            "sender": str(spot.get("sc") or "").upper(), "freq_hz": spot.get("f"),
        }
        own = base_call(report["sender"]) == self.callsign
        with self._lock:
            if own:
                self.own[(band, receiver)] = report
            if not own or str(spot.get("sl") or "")[:4].upper() in self.squares:
                self.area[(band, receiver)] = report
            if len(self.area) > 20000:           # very busy: drop the oldest
                for key in sorted(self.area, key=lambda k: self.area[k]["time"])[:5000]:
                    del self.area[key]
        if own and now - self._saved > 60:
            self._save_own()

    def _save_own(self):
        self._saved = time.time()
        with self._lock:
            rows = [r for r in self.own.values() if time.time() - r["time"] < KEEP_OWN]
        try:
            self.own_path.write_text(json.dumps(rows))
        except OSError:
            pass

    def summary(self):
        now = time.time()
        with self._lock:
            for key in [k for k, r in self.area.items() if now - r["time"] > WINDOW]:
                del self.area[key]
            for key in [k for k, r in self.own.items() if now - r["time"] > KEEP_OWN]:
                del self.own[key]
            area = list(self.area.values())
            own = sorted(self.own.values(), key=lambda r: -r["time"])
            messages, last = self.messages, self.last_message
        bands = []
        for band in sorted({r["band"] for r in area}, key=lambda b: BAND_ORDER.index(b) if b in BAND_ORDER else 99):
            rows = [r for r in area if r["band"] == band]
            continents = {}
            for r in rows:
                if r["continent"]:
                    continents[r["continent"]] = continents.get(r["continent"], 0) + 1
            far = max(rows, key=lambda r: r["km"])
            bands.append({
                "band": band, "receivers": len(rows), "senders": len({r["sender"] for r in rows}),
                "continents": sorted(continents.items(), key=lambda kv: -kv[1]),
                "farthest": {"call": far["call"], "km": far["km"], "country": far["country"]},
                "points": [[r["lat"], r["lon"], r["km"], r["call"]]
                           for r in sorted(rows, key=lambda r: -r["km"])[:MAX_POINTS]],
            })
        return {
            "on": self.feed is not None, "connected": bool(self.feed and self.feed.connected),
            "error": self.feed.last_error if self.feed else "", "window_min": WINDOW // 60,
            "squares": self.squares, "messages": messages,
            "quiet_s": None if last is None else round(now - last),
            "bands": bands, "own": own[:200],
        }
