"""Optional online look-ups from HamQTH.com (free account). Standard library only.

Protocol: https://www.hamqth.com/developers.php
  log in:  xml.php?u=USER&p=PASS              -> <session><session_id>...
  search:  xml.php?id=SESSION&callsign=X&prg=  -> <search>...fields...</search>
A session lasts an hour; this renews it after 50 minutes or when HamQTH says it expired.
"""
import os
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import USER_AGENT

URL = os.environ.get("HAMQTH_URL", "https://www.hamqth.com/xml.php")
PROGRAM = "HamRadioHUD"


class HamQTHError(Exception):
    pass


def _strip_namespaces(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


class HamQTH:
    def __init__(self, username, password, timeout=6):
        self.username = username
        self.password = password
        self.timeout = timeout
        self._session = None
        self._session_time = 0.0
        self._lock = threading.Lock()

    def _fetch(self, params):
        request = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params),
                                         headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return _strip_namespaces(ET.fromstring(response.read()))

    def _session_id(self, renew=False):
        with self._lock:
            if renew or not self._session or time.time() - self._session_time > 50 * 60:
                root = self._fetch({"u": self.username, "p": self.password})
                session_id = (root.findtext("session/session_id") or "").strip()
                if not session_id:
                    raise HamQTHError(root.findtext("session/error") or "HamQTH login failed")
                self._session, self._session_time = session_id, time.time()
            return self._session

    def lookup(self, call):
        """HamQTH's fields for a call; {} if HamQTH has no record. Raises if HamQTH can't be reached."""
        for attempt in (1, 2):
            root = self._fetch({"id": self._session_id(renew=attempt == 2), "callsign": call, "prg": PROGRAM})
            search = root.find("search")
            if search is not None:
                return {child.tag: (child.text or "").strip() for child in search}
            error = (root.findtext("session/error") or "").strip()
            if "not found" in error.lower():
                return {}
            if attempt == 1 and "session" in error.lower():
                continue  # session expired: log in again and retry once
            raise HamQTHError(error or "unexpected reply from HamQTH")
        return {}
