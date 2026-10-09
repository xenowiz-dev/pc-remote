"""Phone remote: serves a touch trackpad/keyboard page and injects the input with SendInput.

    python server.py            # listens on 127.0.0.1:8777
    tailscale serve --bg --https=8777 http://127.0.0.1:8777   # one-time: expose to the tailnet

Then open https://<this-pc>.<tailnet>.ts.net:8777 on the phone.
"""
import argparse
import ctypes
import json
import logging
import socket
import subprocess
import time
from ctypes import wintypes as wt
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

HERE = Path(__file__).resolve().parent
log = logging.getLogger("remote")

# --------------------------------------------------------------------------- Win32 input

user32 = ctypes.WinDLL("user32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT
user32.MapVirtualKeyW.argtypes = (wt.UINT, wt.UINT)
user32.MapVirtualKeyW.restype = wt.UINT
user32.VkKeyScanW.argtypes = (wt.WCHAR,)
user32.VkKeyScanW.restype = ctypes.c_short

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0001, 0x0800, 0x1000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4

BUTTONS = {"l": (0x0002, 0x0004), "r": (0x0008, 0x0010), "m": (0x0020, 0x0040)}
MODS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
KEYS = {
    "back": 0x08, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "space": 0x20,
    "pgup": 0x21, "pgdn": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "prtsc": 0x2C, "ins": 0x2D, "del": 0x2E, "menu": 0x5D, "caps": 0x14,
    "mute": 0xAD, "voldown": 0xAE, "volup": 0xAF,
    "next": 0xB0, "prev": 0xB1, "play": 0xB3,
    **MODS,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}
# Keys that need KEYEVENTF_EXTENDEDKEY so apps don't read them as their numpad twins.
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E,
            0x5B, 0x5D, *range(0xA6, 0xB8)}


def mouse(flags, dx=0, dy=0, data=0):
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0))


def key(vk, up=False):
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, 0))


def unicode_key(unit, up=False):
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, unit, flags, 0, 0))


def send(events):
    if not events:
        return
    arr = (INPUT * len(events))(*events)
    n = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if n != len(events):
        # Happens on the lock screen / UAC secure desktop. Elevated windows fail silently (UIPI).
        log.warning("SendInput inserted %d/%d events (error %d)", n, len(events), ctypes.get_last_error())


def resolve(name):
    """Key name or single character -> (vk, implied modifiers), or None if the layout can't type it."""
    if name in KEYS:
        return KEYS[name], set()
    if name == "\n":
        return KEYS["enter"], set()
    if name == "\t":
        return KEYS["tab"], set()
    if len(name) == 1:
        r = user32.VkKeyScanW(name)
        if r != -1:
            shift = (r >> 8) & 0xFF
            implied = {m for bit, m in ((1, "shift"), (2, "ctrl"), (4, "alt")) if shift & bit}
            return r & 0xFF, implied
    return None


def type_unicode(text):
    events = []
    for ch in text:
        if ch in "\n\t":
            vk = KEYS["enter" if ch == "\n" else "tab"]
            events += [key(vk), key(vk, up=True)]
            continue
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            events += [unicode_key(unit), unicode_key(unit, up=True)]
    return events


class Session:
    """One phone connection. Tracks what it holds down so a dropped connection can't leave keys stuck."""

    def __init__(self):
        self.buttons = set()
        self.held = set()  # locked modifiers, physically held down until released

    def press(self, name, mods=(), count=1):
        target = resolve(name)
        if target is None:  # e.g. an emoji with Ctrl latched: just type it
            send(type_unicode(name * count))
            return
        vk, implied = target
        wrap = [MODS[m] for m in MODS if (m in mods or m in implied) and m not in self.held]
        downs = [key(m) for m in wrap]
        ups = [key(m, up=True) for m in reversed(wrap)]
        send(downs + [key(vk), key(vk, up=True)] * max(1, min(count, 1000)) + ups)

    def handle(self, m):
        t = m.get("t")
        if t == "m":
            send([mouse(MOUSEEVENTF_MOVE, int(m["x"]), int(m["y"]))])
        elif t == "s":
            events = []
            if m.get("y"):
                events.append(mouse(MOUSEEVENTF_WHEEL, data=int(m["y"])))
            if m.get("x"):
                events.append(mouse(MOUSEEVENTF_HWHEEL, data=int(m["x"])))
            send(events)
        elif t == "c":
            down, up = BUTTONS[m.get("b", "l")]
            wrap = [MODS[x] for x in m.get("mods", ()) if x in MODS and x not in self.held]
            send([key(v) for v in wrap] + [mouse(down), mouse(up)]
                 + [key(v, up=True) for v in reversed(wrap)])
        elif t == "bd":
            b = m.get("b", "l")
            if b not in self.buttons:
                self.buttons.add(b)
                send([mouse(BUTTONS[b][0])])
        elif t == "bu":
            b = m.get("b", "l")
            if b in self.buttons:
                self.buttons.discard(b)
                send([mouse(BUTTONS[b][1])])
        elif t == "k":
            self.press(m["k"], m.get("mods", ()), int(m.get("n", 1)))
        elif t == "txt":
            if self.held:  # Ctrl/Alt/Win latched: send real keys so shortcuts register
                for ch in m["s"]:
                    self.press(ch)
            else:
                send(type_unicode(m["s"]))
        elif t == "md":
            mod = m["k"]
            if mod in MODS and mod not in self.held:
                self.held.add(mod)
                send([key(MODS[mod])])
        elif t == "mu":
            mod = m["k"]
            if mod in self.held:
                self.held.discard(mod)
                send([key(MODS[mod], up=True)])
        elif t == "ping":
            return {"t": "pong", "id": m.get("id")}

    def release(self):
        send([mouse(BUTTONS[b][1]) for b in self.buttons]
             + [key(MODS[mod], up=True) for mod in self.held])
        self.buttons.clear()
        self.held.clear()


# --------------------------------------------------------------------------- web


class HostGuard:
    """Only answer to names this machine actually has, and only accept WebSockets opened by our own page.

    Stops other websites (via DNS rebinding or a cross-site WebSocket) from driving the mouse/keyboard.
    """

    def __init__(self, extra):
        self.extra = {h.lower() for h in extra}
        self.allowed = set()
        self.refreshed = 0.0
        self.refresh()

    def refresh(self):
        self.refreshed = time.monotonic()
        names = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower(), *self.extra}
        try:
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, timeout=5,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            me = json.loads(out.stdout.decode("utf-8-sig"))["Self"]
            dns = me.get("DNSName", "").rstrip(".").lower()
            names |= {dns, dns.split(".")[0], me.get("HostName", "").lower(), *me.get("TailscaleIPs", [])}
        except Exception as e:
            log.warning("could not read tailscale status (%s); allowing local names only", e)
        self.allowed = names - {""}

    def ok(self, hostname):
        hostname = (hostname or "").lower()
        if hostname not in self.allowed and time.monotonic() - self.refreshed > 30:
            self.refresh()
        return hostname in self.allowed


def make_app(guard):
    @web.middleware
    async def check_host(request, handler):
        if not guard.ok(urlsplit("//" + request.host).hostname):
            log.warning("rejected Host %r from %s", request.host, request.remote)
            raise web.HTTPForbidden(text="host not allowed")
        return await handler(request)

    async def index(request):
        return web.FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})

    async def ws_handler(request):
        origin = request.headers.get("Origin")
        if not origin or not guard.ok(urlsplit(origin).hostname):
            log.warning("rejected WebSocket from origin %r", origin)
            raise web.HTTPForbidden(text="origin not allowed")
        ws = web.WebSocketResponse(heartbeat=5, max_msg_size=64 * 1024)
        await ws.prepare(request)
        who = request.headers.get("Tailscale-User-Login") or request.remote
        log.info("phone connected (%s)", who)
        session = Session()
        await ws.send_json({"t": "hello", "host": socket.gethostname()})
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    reply = session.handle(json.loads(msg.data))
                except Exception:
                    log.exception("bad message %.200r", msg.data)
                    continue
                if reply:
                    await ws.send_json(reply)
        finally:
            session.release()
            log.info("phone disconnected (%s)", who)
        return ws

    app = web.Application(middlewares=[check_host])
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--allow-host", action="append", default=[], help="extra Host name to accept")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    guard = HostGuard(args.allow_host)
    log.info("accepting hosts: %s", ", ".join(sorted(guard.allowed)))
    web.run_app(make_app(guard), host=args.bind, port=args.port, print=lambda s: log.info(s.splitlines()[0]))


if __name__ == "__main__":
    main()
