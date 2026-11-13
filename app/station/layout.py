"""The computer's own screen kept tidy: the waterfall, and beside it whatever else opens.

The station display fills the big screen and the waterfall fills the computer's own. Anything else that opens
there (WSJT-X, the web browser for a favourite, a look-up or Listen to yourself, any other program) would cover the
waterfall, and a window that opens over the station display would cover that. So this keeps a side area on the
right of the computer's own screen: a window that opens goes there, and the waterfall narrows to the left to make
room, still whole and still clickable. When nothing is left beside it (closed or minimized), the waterfall takes
the whole screen again.

  - Each window is placed when it first opens. One moved or resized by hand stays where it's put from then on.
  - WSJT-X's Wide Graph goes along the bottom of the side area and its main window fills the rest above it (all
    of it while the Wide Graph is closed). The side area is made wide enough for each window there to show whole
    (WSJT-X's Wide Graph can't be narrower than about 845 px), and the windows put there follow when it changes.
  - A program's own dialogs (Save as, WSJT-X's Log QSO, settings...) open over their program as usual, and
    splash screens, menus, notifications and anything full-screen are left alone.
  - A window that opens over the station display goes to the side area too.
  - The waterfall's FT8 button (ft8()) brings WSJT-X to the front or puts it away (minimized: it keeps
    listening, and the station display's map keeps filling in).

It's an ordinary desktop program (X11), run by station-windows.service, using python3-xlib. It doesn't replace the
window manager: it asks it, with the requests every window manager understands (EWMH and ICCCM), as a taskbar
would. The work area (the screen less Cinnamon's panel) comes from the window manager itself: it's where the
waterfall's window goes when it's maximized. What it's doing is written to data/windows.json for the health report.
"""
import configparser
import json
import os
import re
import select
import sys
import time
from collections import namedtuple
from pathlib import Path

try:
    from Xlib import X, Xatom
    from Xlib import display as xdisplay
    from Xlib import error as xerror
    from Xlib.protocol import event as xevent
except ImportError:          # python3-xlib isn't installed (the station installer adds it, with the waterfall)
    X = None

Rect = namedtuple("Rect", "x y w h")

SIDE_WIDTH = 800             # the side area's width, frames included ([layout] side_width in config.ini)
WATERFALL_LEAST = 640        # the waterfall keeps at least this much of the screen's width
GRAPH_HEIGHT = 300           # WSJT-X's Wide Graph at the bottom of the side area (its own size, 245, with a title
                             # bar and a little more), or taller if it can't be made that small
WIDEN_AFTER = 0.6            # seconds with nothing beside the waterfall before it widens again (so one window
                             # closing as another opens doesn't make it jump)
CLOSE_ENOUGH = 4             # pixels: a window this close to where it was put is still where it was put
CHECK_EVERY = 5.0            # seconds between looks round, besides the window manager's own news
SETTLE = 0.4                 # seconds after a window is put somewhere before checking where it really went
FRAME_SLACK = 40             # pixels: a difference up to this is the frame miscounted, and put right; more is a move
MIN_GAP = 0.1                # seconds: at most ten looks round a second (a window being dragged sends many moves)
STARTUP_GRACE = 120          # seconds after starting during which a window waits for the waterfall to be there
LEAVE_TYPES = {"DESKTOP", "DOCK", "TOOLBAR", "MENU", "UTILITY", "SPLASH", "DIALOG", "DROPDOWN_MENU", "POPUP_MENU",
               "TOOLTIP", "NOTIFICATION", "COMBO", "DND"}
BESIDE = ("wsjtx-main", "wsjtx-graph", "wsjtx", "window")      # the kinds of window that go in the side area


def say(text):
    sys.stdout.write("windows: %s\n" % text)
    sys.stdout.flush()


# ---------- what goes where (no X needed) ----------

def center(r):
    return r.x + r.w // 2, r.y + r.h // 2


def inside(point, r):
    return r.x <= point[0] < r.x + r.w and r.y <= point[1] < r.y + r.h


def monitor_of(rect, monitors):
    """The monitor the middle of rect is on, or None."""
    point = center(rect)
    return next((m for m in monitors if inside(point, m["rect"])), None)


def near(a, b, slack=CLOSE_ENOUGH):
    return a is not None and b is not None and all(abs(p - q) <= slack for p, q in zip(a, b))


def overlap(a, b):
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    return max(0, w) * max(0, h)


def split(work, side_width):
    """The waterfall's place and the side area's, on the work area (the side area on the right)."""
    side = max(1, min(side_width, work.w - WATERFALL_LEAST))
    return Rect(work.x, work.y, work.w - side, work.h), Rect(work.x + work.w - side, work.y, side, work.h)


def wsjtx_places(side, graph_least=0, graph=True):
    """WSJT-X's two windows in the side area: the Wide Graph along the bottom (GRAPH_HEIGHT, or as tall as it has to
    be, but no more than half), the main window filling the rest above it, or all of it when the graph is closed.
    Either can open first (WSJT-X shows its Wide Graph before its main window), and nothing's left between them."""
    if not graph:
        return side, None
    h = min(max(GRAPH_HEIGHT, graph_least), side.h // 2)
    return Rect(side.x, side.y, side.w, side.h - h), Rect(side.x, side.y + side.h - h, side.w, h)


def kind_of(info, profiles):
    """What a window is, from what's known of it: "display" (the station display), "waterfall", "wsjtx-main",
    "wsjtx-graph", "wsjtx" (another of WSJT-X's), "window" (anything else that goes beside the waterfall), or None
    (left alone: dialogs, splash screens, menus, the desktop, full-screen windows...).
      info: title, wm_class (instance, class), comm (its program's process name), args (its command line),
            types (_NET_WM_WINDOW_TYPE without the prefix), transient (a dialog of another window), states
      profiles: the Chromium profiles (folders) of the station display and the waterfall
    """
    if info.get("override"):
        return None
    title, args = info.get("title") or "", info.get("args") or ()
    types, states = set(info.get("types") or ()), set(info.get("states") or ())
    chromium = "chrom" in " ".join(info.get("wm_class") or ()).lower()
    for kind, start in (("display", "Station display"), ("waterfall", "Waterfall")):
        profile = profiles.get(kind)
        if (profile and "--user-data-dir=" + profile in args) or (chromium and title.startswith(start)):
            return kind
    if info.get("comm") == "wsjtx":
        if "SPLASH" in types:
            return None
        if "Wide Graph" in title:
            return "wsjtx-graph"
        if types & LEAVE_TYPES or info.get("transient"):
            return None                     # Log QSO, Settings and the like: dialogs
        return "wsjtx-main" if re.search(r" v\d", title) and " - " not in title else "wsjtx"
    if types & LEAVE_TYPES or info.get("transient") or states & {"FULLSCREEN", "SKIP_TASKBAR"}:
        return None
    return "window"


def side_width_for(setting, least_widths=()):
    """The side area's width: as set, but never narrower than a window there can be made (its least width, frame
    included: WSJT-X's Wide Graph can't go below about 845 px)."""
    return max([setting] + [w for w in least_widths if w])


def ft8_choice(wsjtx, others):
    """The FT8 button, with WSJT-X open: "front" or "away".
      wsjtx: its windows [{kind, hidden, rect, level}] (level: its place in the stacking order, the top highest)
      others: the other windows beside the waterfall, the same way
    Away when its main window shows and nothing covers it; else it's brought to the front."""
    main = next((w for w in wsjtx if w["kind"] == "wsjtx-main"), None) or (wsjtx[0] if wsjtx else None)
    if main is None or main["hidden"]:
        return "front"
    for o in others:
        if not o["hidden"] and o["level"] > main["level"] and overlap(o["rect"], main["rect"]) > 0.3 * main["rect"].w * main["rect"].h:
            return "front"
    return "away"


# ---------- the X side ----------

class Desk:
    """The desktop, as the window manager tells it (EWMH/ICCCM), and the requests made of it."""

    def __init__(self, dpy, home=None):
        self.d = dpy
        self.root = dpy.screen().root
        self.home = home or os.path.expanduser("~")
        self.d.set_error_handler(lambda *args: None)   # a window that went away meanwhile isn't a fault
        self._atoms = {}
        self._static = {}            # window id -> what doesn't change (its program, class, type)
        self.randr = dpy.has_extension("RANDR")

    def atom(self, name):
        if name not in self._atoms:
            self._atoms[name] = self.d.intern_atom(name)
        return self._atoms[name]

    def name_of(self, atom):
        try:
            return self.d.get_atom_name(atom)
        except Exception:
            return ""

    def window(self, wid):
        return self.d.create_resource_object("window", wid)

    def _prop(self, w, name, kind=None):
        try:
            p = w.get_full_property(self.atom(name), kind if kind is not None else X.AnyPropertyType)
        except Exception:
            return None
        return None if p is None else p.value

    def ids(self, name):
        v = self._prop(self.root, name)
        return [int(x) for x in v] if v is not None else []

    def clients(self):
        return self.ids("_NET_CLIENT_LIST")

    def levels(self):
        """Each window's place in the stacking order, the top highest, from the X server itself (the root window's
        children come bottom first, and a window manager's frames are among them): exact whatever the window
        manager, where _NET_CLIENT_LIST_STACKING isn't always (one test window manager lists it top first)."""
        try:
            frames = [int(c.id) for c in self.root.query_tree().children]
        except Exception:
            return {w: i for i, w in enumerate(self.ids("_NET_CLIENT_LIST_STACKING"))}
        order = {f: i for i, f in enumerate(frames)}
        out = {}
        for wid in self.clients():
            w, top = wid, None
            try:
                for _ in range(8):                   # client -> its frame (the window just below the root)
                    if w in order:
                        top = w
                        break
                    w = int(self.window(w).query_tree().parent.id)
            except Exception:
                pass
            out[wid] = order.get(top, -1)
        return out

    # -- what's known of a window --
    def static(self, wid):
        if wid in self._static:
            return self._static[wid]
        w = self.window(wid)
        info = {}
        try:
            cls = w.get_wm_class()
        except Exception:
            cls = None
        info["wm_class"] = tuple(cls) if cls else ()
        pid = self._prop(w, "_NET_WM_PID")
        info["pid"] = int(pid[0]) if pid is not None and len(pid) else None
        info["comm"], info["args"] = "", ()
        if info["pid"]:
            try:
                info["comm"] = Path("/proc/%d/comm" % info["pid"]).read_text().strip()
                info["args"] = tuple(Path("/proc/%d/cmdline" % info["pid"]).read_bytes().decode("utf-8", "replace").split("\0"))
            except OSError:
                pass
        types = self._prop(w, "_NET_WM_WINDOW_TYPE")
        info["types"] = tuple(self.name_of(t).replace("_NET_WM_WINDOW_TYPE_", "") for t in (types if types is not None else ()))
        tf = self._prop(w, "WM_TRANSIENT_FOR")
        info["transient"] = bool(tf is not None and len(tf) and int(tf[0]))
        try:
            info["override"] = bool(w.get_attributes().override_redirect)
        except Exception:
            info["override"] = False
        self._static[wid] = info
        return info

    def title(self, wid):
        w = self.window(wid)
        v = self._prop(w, "_NET_WM_NAME")
        if isinstance(v, bytes) and v:
            return v.decode("utf-8", "replace")
        try:
            name = w.get_wm_name()
            return name.decode("latin-1") if isinstance(name, bytes) else (name or "")
        except Exception:
            return ""

    def states(self, wid):
        v = self._prop(self.window(wid), "_NET_WM_STATE")
        return {self.name_of(a).replace("_NET_WM_STATE_", "") for a in (v if v is not None else ())}

    def iconic(self, wid):
        v = self._prop(self.window(wid), "WM_STATE")
        return v is not None and len(v) and int(v[0]) == 3

    def hidden(self, wid, states=None):
        return "HIDDEN" in (states if states is not None else self.states(wid)) or self.iconic(wid)

    def on_this_desktop(self, wid):
        """On the workspace in view (or on all of them)."""
        cur = self._prop(self.root, "_NET_CURRENT_DESKTOP")
        mine = self._prop(self.window(wid), "_NET_WM_DESKTOP")
        if cur is None or mine is None or not len(cur) or not len(mine):
            return True
        return int(mine[0]) in (int(cur[0]), 0xFFFFFFFF)

    def extents(self, wid):
        v = self._prop(self.window(wid), "_NET_FRAME_EXTENTS")
        return tuple(int(x) for x in v) if v is not None and len(v) == 4 else (0, 0, 0, 0)    # left, right, top, bottom

    def least(self, wid):
        """The least size the window can be made, frame included (WM_NORMAL_HINTS' minimum, which Qt sets from the
        window's layout), or (0, 0)."""
        try:
            h = self.window(wid).get_wm_normal_hints()
        except Exception:
            h = None
        if h is None or not (h.flags & 16):          # PMinSize
            return 0, 0
        left, right, top, bottom = self.extents(wid)
        return h.min_width + left + right, h.min_height + top + bottom

    def frame(self, wid):
        """Where the window is on the screen, its frame (title bar and edges) included."""
        w = self.window(wid)
        try:
            g = w.get_geometry()
            at = self.root.translate_coords(w, 0, 0)
        except Exception:
            return None
        left, right, top, bottom = self.extents(wid)
        return Rect(at.x - left, at.y - top, g.width + left + right, g.height + top + bottom)

    def info(self, wid):
        info = dict(self.static(wid))
        info["title"] = self.title(wid)
        info["states"] = self.states(wid)
        return info

    def watch(self, wid):
        try:
            self.window(wid).change_attributes(event_mask=X.PropertyChangeMask | X.StructureNotifyMask)
        except Exception:
            pass

    def forget(self, wid):
        self._static.pop(wid, None)

    # -- the screens --
    def monitors(self):
        """[{name, rect, primary}], from RandR (1.5's monitors, else the outputs in use), else the whole screen."""
        out = []
        if self.randr:
            try:
                v = self.d.xrandr_query_version()
                if (v.major_version, v.minor_version) >= (1, 5):
                    for m in self.root.xrandr_get_monitors().monitors:
                        out.append({"name": self.name_of(m.name), "primary": bool(m.primary),
                                    "rect": Rect(m.x, m.y, m.width_in_pixels, m.height_in_pixels)})
                if not out:
                    res = self.root.xrandr_get_screen_resources()
                    for o in res.outputs:
                        oi = self.d.xrandr_get_output_info(o, res.config_timestamp)
                        if oi.connection == 0 and oi.crtc:
                            c = self.d.xrandr_get_crtc_info(oi.crtc, res.config_timestamp)
                            name = oi.name.decode() if isinstance(oi.name, bytes) else oi.name
                            out.append({"name": name, "primary": False, "rect": Rect(c.x, c.y, c.width, c.height)})
            except Exception:
                out = []
        if not out:
            g = self.root.get_geometry()
            out = [{"name": "screen", "primary": True, "rect": Rect(0, 0, g.width, g.height)}]
        return out

    def workarea(self, monitor):
        """The window manager's work area (_NET_WORKAREA, desktop 0) on one monitor, or the monitor."""
        v = self._prop(self.root, "_NET_WORKAREA")
        m = monitor["rect"]
        if v is None or len(v) < 4:
            return m
        x, y, w, h = (int(a) for a in v[:4])
        x0, y0 = max(x, m.x), max(y, m.y)
        x1, y1 = min(x + w, m.x + m.w), min(y + h, m.y + m.h)
        return Rect(x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else m

    # -- requests to the window manager --
    def _send(self, wid, name, data):
        ev = xevent.ClientMessage(window=self.window(wid), client_type=self.atom(name),
                                  data=(32, (list(data) + [0] * 5)[:5]))
        self.root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)

    def set_states(self, wid, add, *names):
        names = list(names) + [None]
        for a, b in zip(names[::2], names[1::2]):
            self._send(wid, "_NET_WM_STATE", [1 if add else 0, self.atom("_NET_WM_STATE_" + a),
                                              self.atom("_NET_WM_STATE_" + b) if b else 0, 2])

    def move(self, wid, rect):
        """Puts the window's frame at rect: un-maximized first, then _NET_MOVERESIZE_WINDOW with north-west
        gravity (x and y are the frame's top left; the width and height its inside, so less its frame)."""
        left, right, top, bottom = self.extents(wid)
        self.set_states(wid, False, "MAXIMIZED_VERT", "MAXIMIZED_HORZ")
        flags = 1 | (0xF << 8) | (2 << 12)       # north-west gravity; x, y, width and height given; from a pager
        self._send(wid, "_NET_MOVERESIZE_WINDOW", [flags, rect.x, rect.y, max(1, rect.w - left - right),
                                                   max(1, rect.h - top - bottom)])

    def maximize(self, wid):
        self.set_states(wid, True, "MAXIMIZED_VERT", "MAXIMIZED_HORZ")

    def minimize(self, wid):
        self._send(wid, "WM_CHANGE_STATE", [3])           # ICCCM: IconicState

    def activate(self, wid):
        self._send(wid, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])   # from a pager: unminimized and raised

    def flush(self):
        self.d.flush()


# ---------- the keeper ----------

class Keeper:
    def __init__(self, desk, side_width=SIDE_WIDTH, state_path=None, profiles=None, clock=time.monotonic):
        self.desk = desk
        self.side_setting = side_width
        self.state_path = Path(state_path) if state_path else None
        self.profiles = profiles or {}
        self.clock = clock
        self.known = {}              # window id -> {"kind", "placed": Rect we put it at, "pending": not placed yet}
        self.work = None             # the work area of the waterfall's screen, learned from its maximized window
        self.work_from = None        # ...on which monitor (its rect), so it's learned again if that changes
        self.mode = None             # "wide" or "narrow", as last asked of the waterfall
        self.narrow_at = None        # the waterfall's narrow place, as last asked
        self.wide_at = None          # when to widen it, once nothing has been beside it for WIDEN_AFTER
        self.measuring = False       # it was maximized to learn the work area
        self.wake_at = None          # when to look again (a window to check, once it has settled)
        self.started = clock()
        self._told = set()
        self.carry_side = None       # the side area's width last time (for taking on the windows it placed)
        self.monitors = desk.monitors()
        self.state = {}
        self._written = None
        self._load()

    def _load(self):
        """The work area learned last time, if the screens are the same (so a restart doesn't make the waterfall
        jump to learn it again)."""
        try:
            old = json.loads(self.state_path.read_text())
            if old.get("monitors") == self._monitors_text() and old.get("work"):
                self.work = Rect(*old["work"])
                self.work_from = Rect(*old["work_from"]) if old.get("work_from") else None
                self.carry_side = old.get("side_width")
        except Exception:
            pass

    def _monitors_text(self):
        return ", ".join("%s %dx%d+%d+%d" % (m["name"], m["rect"].w, m["rect"].h, m["rect"].x, m["rect"].y)
                         for m in self.monitors)

    # -- one look round --
    def sync(self, first=False):
        desk = self.desk
        now = self.clock()
        clients = desk.clients()
        for gone in [w for w in self.known if w not in clients]:
            del self.known[gone]
            desk.forget(gone)
        infos = {}
        for wid in clients:
            info = desk.info(wid)
            kind = kind_of(info, self.profiles)
            if wid not in self.known:
                desk.watch(wid)
                # what it is when it first opens decides whether it's placed: a dialog, a splash screen or a
                # full-screen window isn't, even if it changes later (a video leaving full screen stays put)
                self.known[wid] = {"kind": kind, "placed": None, "pending": not first and kind in BESIDE, "first": now}
            self.known[wid]["kind"] = kind
            infos[wid] = info
        waterfall = next((w for w in clients if self.known[w]["kind"] == "waterfall"), None)
        display = next((w for w in clients if self.known[w]["kind"] == "display"), None)
        wf_rect = desk.frame(waterfall) if waterfall else None
        work_mon = monitor_of(wf_rect, self.monitors) if wf_rect else None
        disp_rect = desk.frame(display) if display else None
        disp_mon = monitor_of(disp_rect, self.monitors) if disp_rect else None
        if work_mon is not None and disp_mon is not None and work_mon is disp_mon:
            work_mon = None              # one screen for both: nothing to keep apart
        if work_mon is None and disp_mon is not None:
            # no waterfall: the screen that isn't the station display's still gets what opens over it
            work_mon = next((m for m in self.monitors if m is not disp_mon), None)
        # the work area: where the waterfall's window goes when it's maximized
        if waterfall and work_mon is not None:
            st = infos[waterfall]["states"]
            if {"MAXIMIZED_VERT", "MAXIMIZED_HORZ"} <= st and wf_rect is not None:
                if self.work != wf_rect:
                    self.work, self.work_from = wf_rect, work_mon["rect"]
                self.measuring = False
            if self.work is not None and self.work_from != work_mon["rect"]:
                self.work = None         # the screens changed: learn it again
        work = self.work or (desk.workarea(work_mon) if work_mon is not None and not waterfall else None)
        # what's beside the waterfall (on its screen), and the side area: as wide as set, or as the least any of
        # them can be made, so all of each shows (whether showing just now or put away, so it doesn't change width
        # each time WSJT-X is put away and brought back)
        beside = [w for w in clients if self.known[w]["kind"] in BESIDE]
        frames = {w: desk.frame(w) for w in beside}
        here = [w for w in beside if frames[w] is not None and work_mon is not None and monitor_of(frames[w], [work_mon])]
        side_w = side_width_for(self.side_setting, [desk.least(w)[0] for w in here if self.known[w].get("mine") or self.known[w]["pending"]])
        if first and here and self.carry_side:
            # windows placed before a restart: the side area as it was, to see which are still in their places (those
            # are taken on again below, and count from then on as any others do)
            side_w = max(side_w, self.carry_side)
        self.carry_side = None
        narrow, side = split(work, side_w) if work is not None else (None, None)
        targets = self._plan(side, clients) if side is not None else {}
        if first and side is not None:
            # windows still where the last run put them are taken on again, so they go on following the side area
            for w in here:
                self.known[w]["mine"] = True            # for now, so the plan counts WSJT-X's Wide Graph in
            targets = self._plan(side, clients)
            for w in here:
                t, f = targets.get(w), frames[w]
                if t is not None and f is not None and abs(f.x - t.x) <= CLOSE_ENOUGH and abs(f.y - t.y) <= CLOSE_ENOUGH \
                        and f.w >= t.w - CLOSE_ENOUGH and f.h >= t.h - CLOSE_ENOUGH:
                    self.known[w].update(mine=True, settled=True, base=f, placed=t, moved=now)
                else:
                    self.known[w]["mine"] = False
            targets = self._plan(side, clients)
        # place what has just opened
        for wid in clients:
            k = self.known[wid]
            if not k["pending"] or k["kind"] not in BESIDE:
                continue
            states = infos[wid]["states"]
            if desk.hidden(wid, states):
                continue                     # placed when it first shows
            if "FULLSCREEN" in states:
                k["pending"] = False
                continue
            rect = frames.get(wid)
            if rect is None:
                continue
            if waterfall and work_mon is not None:
                if side is None:             # the work area isn't known yet: learned first, then this is placed
                    self._measure(waterfall)
                    continue
                target = targets[wid]
            elif work_mon is not None and disp_mon is not None and monitor_of(rect, [disp_mon]):
                m = work_mon["rect"]         # no waterfall to make room: just off the station display
                target = Rect(m.x + max(0, (m.w - rect.w) // 2), m.y + max(0, (m.h - rect.h) // 2), min(rect.w, m.w), min(rect.h, m.h))
            else:
                # no waterfall yet: just after the computer starts, it's on its way (so what opens meanwhile still
                # goes beside it once it's there); later, the waterfall is off, and the window is left where it is
                if now - self.started > STARTUP_GRACE:
                    k["pending"] = False
                continue
            self._move(wid, target, now)
            k["pending"], k["mine"] = False, bool(waterfall)
            say("%s: put beside the waterfall" % (infos[wid]["title"] or "a window") if waterfall
                else "%s: moved off the station display" % (infos[wid]["title"] or "a window"))
        # the windows put there: settled where the window manager put them, and following the side area when it
        # changes (wider, or the Wide Graph coming or going), unless they've been moved or resized by hand since
        for wid in beside:
            if self.known[wid].get("mine") and not self.known[wid]["pending"]:
                self._follow(wid, targets.get(wid), infos[wid]["states"], now)
        # the waterfall: narrow while anything shows beside it
        showing = [w for w in beside if not desk.hidden(w, infos[w]["states"]) and desk.on_this_desktop(w)
                   and (lambda r: r is not None and work_mon is not None and monitor_of(r, [work_mon]))(desk.frame(w))]
        if waterfall and work_mon is not None:
            want = "narrow" if showing else "wide"
            self._waterfall(waterfall, want, narrow, infos[waterfall]["states"], wf_rect, now)
        else:
            self.mode = None
        self.state = {
            "monitors": self._monitors_text(), "work": list(self.work) if self.work else None,
            "work_from": list(self.work_from) if self.work_from else None, "side_width": side_w,
            "waterfall": (self.mode or "?") if waterfall else "none",
            "beside": [infos[w]["title"] or "?" for w in showing], "display": bool(display)}
        self._write()
        desk.flush()
        wake = [t for t in (self.wide_at, self.wake_at) if t is not None]
        self.wake_at = None
        return min(wake) if wake else None

    def _plan(self, side, clients):
        """Where each window beside the waterfall goes now: WSJT-X's Wide Graph along the bottom and its main window
        above it (wsjtx_places), anything else in the whole side area."""
        kinds = {w: self.known[w]["kind"] for w in clients}
        graph = next((w for w in clients if kinds[w] == "wsjtx-graph"
                      and (self.known[w].get("mine") or self.known[w]["pending"])), None)
        main_place, graph_place = wsjtx_places(side, self.desk.least(graph)[1] if graph else 0, graph is not None)
        out = {}
        for w in clients:
            out[w] = main_place if kinds[w] == "wsjtx-main" else graph_place if w == graph else side
        return out

    def _move(self, wid, target, now):
        self.desk.move(wid, target)
        k = self.known.setdefault(wid, {"kind": None, "pending": False, "first": now})
        k.update(placed=target, moved=now, settled=False, base=None)
        self._wake(now + SETTLE)

    def _follow(self, wid, target, states, now):
        """A window put beside the waterfall. A moment after it was put somewhere: where it really went is noted
        (a window that can't be made that small is left at its least size); if the window manager put it a little
        off (window managers count frames differently), it's put again, allowing for that, once. After that, as
        long as it's where it settled, it follows when its place changes; moved or resized by hand (or maximized by
        its program), it's left alone from then on."""
        k = self.known[wid]
        if "HIDDEN" in states or self.desk.iconic(wid):
            return                           # minimized: it follows when it shows again
        actual = self.desk.frame(wid)
        if actual is None:
            return
        if not k.get("settled"):
            if now - k["moved"] < SETTLE:
                self._wake(k["moved"] + SETTLE)
                return
            t = k["placed"]
            if states & {"MAXIMIZED_VERT", "MAXIMIZED_HORZ", "FULLSCREEN"}:
                k["mine"] = False            # its program maximized it meanwhile: that stands
                return
            dx, dy = actual.x - t.x, actual.y - t.y
            if abs(dx) > FRAME_SLACK or abs(dy) > FRAME_SLACK:
                k["mine"] = False            # it went somewhere else altogether: moved since
                return
            if (dx or dy) and not k.get("corrected"):
                k["corrected"] = True        # counted from a different corner: put again, allowing for that
                fixed = Rect(t.x - dx, t.y - dy, t.w, t.h)
                self.desk.move(wid, fixed)
                k["moved"] = now
                self._wake(now + SETTLE)
                say("a window came out %+d,%+d px from its place: put again, allowing for that" % (dx, dy))
                return
            least_w, least_h = self.desk.least(wid)
            def fits(got, asked, least):     # as asked, or bigger because it can't be made that small
                return abs(got - asked) <= CLOSE_ENOUGH or asked < got <= max(asked, least) + CLOSE_ENOUGH
            if not (fits(actual.w, t.w, least_w) and fits(actual.h, t.h, least_h)):
                k["mine"] = False            # resized since, and not by the window manager: by hand
                return
            k["settled"], k["base"] = True, actual
            return
        if not near(actual, k["base"]):
            k["mine"] = False                # moved or resized by hand: it stays where it's put
            return
        if target is not None and target != k["placed"]:
            self._move(wid, target, now)     # its place changed: it follows

    def _wake(self, at):
        self.wake_at = at if self.wake_at is None else min(self.wake_at, at)

    def _measure(self, waterfall):
        """Maximizes the waterfall, to learn the work area from where the window manager puts it."""
        if not self.measuring:
            self.desk.maximize(waterfall)
            self.measuring = True

    def _waterfall(self, wid, want, narrow, states, rect, now):
        maximized = bool(states & {"MAXIMIZED_VERT", "MAXIMIZED_HORZ"})
        if want == "narrow":
            self.wide_at = None
            if narrow is None:               # the work area isn't known yet: maximized first, to learn it
                self._measure(wid)
                return
            if self.mode != "narrow" or maximized or self.narrow_at != narrow:
                self._move(wid, narrow, now)
                if self.mode != "narrow":
                    say("the waterfall makes room (%d px wide)" % narrow.w)
                self.mode, self.narrow_at = "narrow", narrow
        else:
            if self.mode == "wide" and maximized:
                self.wide_at = None
                return
            if self.mode == "narrow" and self.wide_at is None:
                self.wide_at = now + WIDEN_AFTER   # in a moment, if nothing opens meanwhile
                return
            if self.wide_at is not None and now < self.wide_at:
                return
            self.desk.maximize(wid)
            if self.mode == "narrow":
                say("the waterfall has the whole screen again")
            self.mode, self.wide_at, self.narrow_at = "wide", None, None

    def _write(self):
        if self.state_path is None:
            return
        text = json.dumps(self.state, sort_keys=True)
        if text == self._written and time.time() - getattr(self, "_written_t", 0) < 60:
            return
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(dict(self.state, t=round(time.time()))))
            tmp.replace(self.state_path)
            self._written, self._written_t = text, time.time()
        except OSError:
            pass

    # -- the loop --
    def run(self):
        desk = self.desk
        desk.root.change_attributes(event_mask=X.PropertyChangeMask | X.SubstructureNotifyMask)
        screen_news = None
        if desk.randr:
            try:
                from Xlib.ext import randr
                desk.root.xrandr_select_input(randr.RRScreenChangeNotifyMask)
                screen_news = desk.d.extension_event.ScreenChangeNotify
            except Exception:
                screen_news = None
        desk.flush()
        wake = self.sync(first=True)
        say("keeping the computer's own screen tidy (side area %d px)" % self.side_setting)
        last = self.clock()
        fd = desk.d.fileno()
        # the properties whose changes matter (a program typing away changes others, such as _NET_WM_USER_TIME,
        # many times a second: those don't call for a look round)
        watched = {desk.atom(a) for a in ("_NET_CLIENT_LIST", "_NET_WORKAREA", "_NET_CURRENT_DESKTOP", "_NET_WM_STATE",
                                          "WM_STATE", "_NET_WM_DESKTOP", "_NET_FRAME_EXTENTS", "_NET_WM_NAME", "WM_NAME")}
        pending = False
        while True:
            soon = [t for t in (wake, last + MIN_GAP if pending else None) if t is not None]
            timeout = max(0.01, min(CHECK_EVERY, min(soon) - self.clock())) if soon else CHECK_EVERY
            if not desk.d.pending_events():
                # (news that came in during the last look round is already read, and waiting: select wouldn't see it)
                select.select([fd], [], [], timeout)
            while desk.d.pending_events():
                ev = desk.d.next_event()
                if screen_news is not None and ev.type == screen_news:
                    self.monitors = desk.monitors()
                    pending = True
                elif ev.type == X.PropertyNotify:
                    pending = pending or ev.atom in watched
                elif ev.type in (X.ConfigureNotify, X.MapNotify, X.UnmapNotify, X.DestroyNotify, X.CreateNotify, X.ReparentNotify):
                    pending = True
            now = self.clock()
            if (pending and now - last >= MIN_GAP) or (wake is not None and now >= wake) or now - last >= CHECK_EVERY:
                pending = False
                try:
                    wake = self.sync()
                except xerror.ConnectionClosedError:
                    raise
                except Exception as exc:     # one odd look round mustn't end the keeping
                    wake = None
                    kind = type(exc).__name__
                    if kind not in self._told:
                        self._told.add(kind)
                        say("a look round went wrong (%s: %s); carrying on" % (kind, str(exc)[:200]))
                last = self.clock()


# ---------- the FT8 button ----------

def ft8(desk, profiles=None):
    """WSJT-X open: brings it to the front, or puts it away if it's in front already. Returns "front", "away", or
    "open" (no WSJT-X window to be found: the app opens it)."""
    profiles = profiles or {}
    stack = desk.levels()
    wsjtx, others = [], []
    for wid in desk.clients():
        info = desk.info(wid)
        kind = kind_of(info, profiles)
        if kind not in BESIDE:
            continue
        rect = desk.frame(wid)
        if rect is None:
            continue
        row = {"id": wid, "kind": kind, "hidden": desk.hidden(wid, info["states"]), "rect": rect, "level": stack.get(wid, -1)}
        (wsjtx if kind.startswith("wsjtx") else others).append(row)
    if not wsjtx:
        return "open"
    choice = ft8_choice(wsjtx, others)
    order = sorted(wsjtx, key=lambda r: r["kind"] == "wsjtx-main")     # the main window last, so it ends on top
    for r in order:
        if choice == "away":
            desk.minimize(r["id"])
        else:
            desk.activate(r["id"])
    desk.flush()
    return choice


# ---------- running it ----------

def settings(station_dir):
    cp = configparser.ConfigParser(inline_comment_prefixes=(" #", " ;"), interpolation=None)
    cp.read(Path(station_dir) / "config.ini", encoding="utf-8")
    keep = cp.get("layout", "keep", fallback="yes").strip().lower() not in ("no", "off", "false", "0")
    try:
        width = int(cp.get("layout", "side_width", fallback=str(SIDE_WIDTH)).strip())
    except ValueError:
        width = SIDE_WIDTH
    return keep, max(400, min(width, 1600))


def profiles_for(home):
    return {"display": os.environ.get("STATION_DISPLAY_PROFILE") or os.path.join(home, ".config/station-display"),
            "waterfall": os.environ.get("STATION_WF_PROFILE") or os.path.join(home, ".config/station-waterfall")}


def connect(wait=0):
    """The desktop's X display (DISPLAY, else :0), waiting up to wait seconds for it."""
    os.environ.setdefault("DISPLAY", ":0")
    if not os.environ.get("XAUTHORITY"):
        os.environ["XAUTHORITY"] = os.path.expanduser("~/.Xauthority")
    end = time.time() + wait
    while True:
        try:
            return xdisplay.Display()
        except Exception as exc:
            if time.time() >= end:
                raise SystemExit("can't reach the desktop (%s): %s" % (os.environ["DISPLAY"], exc))
            time.sleep(5)


def main(args):
    what = args[0] if args else "keep"
    home = os.path.expanduser("~")
    station = os.environ.get("STATION_DIR") or os.path.join(home, "station")
    if X is None:
        if what == "keep":
            say("python3-xlib isn't installed, so the windows aren't kept tidy (the station installer adds it)")
            return 3
        print(json.dumps({"action": None, "error": "python3-xlib isn't installed"}))
        return 3
    if what == "ft8":
        desk = Desk(connect(), home)
        print(json.dumps({"action": ft8(desk, profiles_for(home))}))
        return 0
    if what == "show":
        desk = Desk(connect(), home)
        for m in desk.monitors():
            print("monitor %s %s%s" % (m["name"], tuple(m["rect"]), " (primary)" if m["primary"] else ""))
        for wid in desk.clients():
            info = desk.info(wid)
            print("0x%08x %-11s %-28s %s %s" % (wid, kind_of(info, profiles_for(home)), (info["title"] or "")[:28],
                                               tuple(desk.frame(wid) or ()), ",".join(sorted(info["states"]))))
        return 0
    if what != "keep":
        print(__doc__)
        return 1
    keep, width = settings(station)
    if not keep:
        say("switched off in config.ini (keep = no under [layout])")
        return 3
    # wait for the desktop and its window manager
    dpy = connect(wait=600)
    desk = Desk(dpy, home)
    for _ in range(120):
        if desk._prop(desk.root, "_NET_SUPPORTING_WM_CHECK") is not None:
            break
        time.sleep(1)
    data = Path(station) / "data"
    data.mkdir(parents=True, exist_ok=True)
    try:
        Keeper(desk, width, data / "windows.json", profiles_for(home)).run()
    except xerror.ConnectionClosedError as exc:
        say("lost the desktop (%s); starting again in a few seconds" % exc)
        return 1
    return 0
