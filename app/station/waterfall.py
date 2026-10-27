"""Talks to the waterfall (Gqrx) through its remote control on 127.0.0.1.

An RTL-SDR (at VE6LDM, a NooElec inside the FT-950) listens at the radio's first IF
(69.450 MHz on the FT-950; if_hz in config.ini). Gqrx's "LNB LO" setting only changes
the numbers on its scale, so setting it to (radio frequency - IF) makes the scale read
the radio's own frequency while the SDR stays where it is.

Gqrx 2.17 takes one remote-control connection at a time, and one command per
message, so every call here opens a connection, sends one line, reads the reply
and closes. Nothing here waits on Gqrx for long, and if Gqrx isn't running it
quietly gives up and tries again later.

Run as a script for the start-up helper and the health report:
    python3 waterfall.py send "U DSP 1"     prints Gqrx's reply (exit 1 if it isn't there)
"""
import socket
import sys
import threading
import time

PORT = 7356
IF_HZ = 69_450_000


def send(command, port=PORT, host="127.0.0.1", timeout=1.0):
    """Sends one remote-control line to Gqrx. Returns its reply, or None if Gqrx isn't there."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall((command.strip() + "\n").encode("ascii"))
            reply = b""
            while not reply.endswith(b"\n") and len(reply) < 4096:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply += chunk
            return reply.decode("ascii", "replace").strip()
    except (OSError, UnicodeError):
        return None


class Scale:
    """Keeps Gqrx's frequency scale on the radio's frequency.

    note() is called with each radio reading (about once a second while the display is
    open). A background thread sends the new value when the dial moves, and again every
    30 seconds so a Gqrx that has just restarted catches up.
    """

    RESEND = 30

    def __init__(self, port=PORT, if_hz=IF_HZ, enabled=True, sender=send):
        self.port, self.if_hz, self.enabled, self._send = port, if_hz, enabled, sender
        self._want = None
        self._sent = None
        self._sent_at = 0.0
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.connected = False

    def start(self):
        if self.enabled:
            threading.Thread(target=self._run, daemon=True, name="waterfall-scale").start()
        return self

    def note(self, freq_hz):
        if not self.enabled or not freq_hz:
            return
        want = int(freq_hz) - self.if_hz
        with self._lock:
            changed = want != self._want
            self._want = want
        if changed:
            self._wake.set()

    def step(self, now=None):
        """Sends the LNB LO if it's needed. Returns True, False (Gqrx not there) or None (nothing to do)."""
        now = time.time() if now is None else now
        with self._lock:
            want = self._want
        if want is None:
            return None
        if want == self._sent and now - self._sent_at < self.RESEND:
            return None
        reply = self._send("LNB_LO %d" % want, port=self.port)
        self.connected = reply is not None
        if reply is not None and reply.startswith("RPRT 0"):
            self._sent, self._sent_at = want, now
            return True
        self._sent = None
        return False

    def _run(self):
        while True:
            self._wake.wait(self.RESEND)
            self._wake.clear()
            try:
                if self.step() is False:
                    time.sleep(5)  # Gqrx isn't running (or said no); don't keep knocking
            except Exception:
                time.sleep(5)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "send":
        port = PORT
        if len(sys.argv) >= 4:
            port = int(sys.argv[3])
        answer = send(sys.argv[2], port=port)
        if answer is None:
            sys.exit(1)
        print(answer)
    else:
        print(__doc__)
        sys.exit(2)
