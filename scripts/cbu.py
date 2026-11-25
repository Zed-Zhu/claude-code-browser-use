#!/usr/bin/env python3
"""cbu - browser use for coding agents over raw Chrome DevTools Protocol (CDP).

Drives the user's own, already-running Chromium browser (Chrome / Edge / Brave /
Chromium) instead of launching a fresh automation browser. Pure Python standard
library (3.8+): no Playwright, no Selenium, no browser download, no pip install.

A small background daemon keeps ONE CDP connection open, so the browser's
"Allow remote debugging?" prompt is answered once per session, not per command.

Run `cbu.py doctor` first. Every command prints JSON.
"""
import argparse
import base64
import http.server
import itertools
import json
import os
import platform
import plistlib
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("CBU_HOME") or (Path.home() / ".cache" / "claude-browser-use"))
STATE = HOME / "daemon.json"
ENDPOINT = HOME / "endpoint.json"
ERRFILE = HOME / "daemon.error"
OWNED = HOME / "owned.json"
LOG = HOME / "daemon.log"
IDLE_SECONDS = int(os.environ.get("CBU_IDLE", "1800"))
APPROVE_TIMEOUT = int(os.environ.get("CBU_APPROVE_TIMEOUT", "180"))
MIN_TOGGLE_MAJOR = 144  # chrome://inspect/#remote-debugging toggle for the default profile


class CBUError(Exception):
    pass


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def die(msg, code=1, **extra):
    emit(dict({"ok": False, "error": msg}, **extra))
    sys.exit(code)


# --------------------------------------------------------------------------- browsers

def _mac_version(app):
    try:
        with open(Path(app) / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except Exception:
        return None


def _cli_version(exe):
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        m = re.search(r"(\d+\.\d+[\d.]*)", r.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def major(version):
    m = re.match(r"(\d+)", version or "")
    return int(m.group(1)) if m else None


def browser_candidates():
    """Known Chromium browsers, installed or not, with their default user-data dir."""
    system, home, out = platform.system(), Path.home(), []
    if system == "Darwin":
        AS = home / "Library" / "Application Support"
        table = [
            ("Google Chrome", "Google Chrome", "Google/Chrome", "chrome"),
            ("Google Chrome Beta", "Google Chrome Beta", "Google/Chrome Beta", "chrome"),
            ("Google Chrome Canary", "Google Chrome Canary", "Google/Chrome Canary", "chrome"),
            ("Microsoft Edge", "Microsoft Edge", "Microsoft Edge", "edge"),
            ("Brave Browser", "Brave Browser", "BraveSoftware/Brave-Browser", "brave"),
            ("Chromium", "Chromium", "Chromium", "chrome"),
        ]
        for name, exe, data, scheme in table:
            app = next((r / (name + ".app") for r in (Path("/Applications"), home / "Applications")
                        if (r / (name + ".app")).exists()), None)
            out.append(dict(name=name, app=str(app) if app else None,
                            exe=str(app / "Contents" / "MacOS" / exe) if app else None,
                            data=str(AS / data), scheme=scheme,
                            version=_mac_version(app) if app else None))
    elif system == "Linux":
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        table = [
            ("Google Chrome", ["google-chrome", "google-chrome-stable"], "google-chrome", "chrome"),
            ("Google Chrome Beta", ["google-chrome-beta"], "google-chrome-beta", "chrome"),
            ("Microsoft Edge", ["microsoft-edge", "microsoft-edge-stable"], "microsoft-edge", "edge"),
            ("Brave Browser", ["brave-browser", "brave"], "BraveSoftware/Brave-Browser", "brave"),
            ("Chromium", ["chromium", "chromium-browser"], "chromium", "chrome"),
        ]
        for name, exes, data, scheme in table:
            exe = next((shutil.which(e) for e in exes if shutil.which(e)), None)
            out.append(dict(name=name, app=None, exe=exe, data=str(cfg / data), scheme=scheme,
                            version=_cli_version(exe)))
    elif system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roots = [Path(os.environ[k]) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
                 if os.environ.get(k)]
        table = [
            ("Google Chrome", r"Google\Chrome\Application\chrome.exe", r"Google\Chrome\User Data", "chrome"),
            ("Microsoft Edge", r"Microsoft\Edge\Application\msedge.exe", r"Microsoft\Edge\User Data", "edge"),
            ("Brave Browser", r"BraveSoftware\Brave-Browser\Application\brave.exe",
             r"BraveSoftware\Brave-Browser\User Data", "brave"),
            ("Chromium", r"Chromium\Application\chrome.exe", r"Chromium\User Data", "chrome"),
        ]
        for name, rel, data, scheme in table:
            exe = next((str(r / rel) for r in roots if (r / rel).exists()), None)
            out.append(dict(name=name, app=None, exe=exe, data=str(local / data), scheme=scheme, version=None))
    for b in out:
        b["installed"] = bool(b["exe"] and Path(b["exe"]).exists())
    return out


def isolated_profile():
    return HOME / "profile"


def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def read_port_file(data_dir):
    """ws:// URL from <user-data-dir>/DevToolsActivePort, or None if absent/stale.
    Never guess 9222: Electron apps (VS Code, Obsidian, ...) often sit there."""
    try:
        parts = (Path(data_dir) / "DevToolsActivePort").read_text().split()
        port, path = int(parts[0]), parts[1]
    except (OSError, ValueError, IndexError):
        return None
    return f"ws://127.0.0.1:{port}{path}" if port_open(port) else None


def choose_browser(name=None):
    bs = [b for b in browser_candidates() if b["installed"]]
    if name:
        bs = [b for b in bs if b["name"].lower() == name.lower()] or \
             [b for b in bs if name.lower() in b["name"].lower()]
    return bs[0] if bs else None


def open_url_in(b, url):
    if platform.system() == "Darwin" and b.get("app"):
        subprocess.run(["open", "-a", b["app"], url], check=False)
    else:
        kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "posix":
            kw["start_new_session"] = True
        subprocess.Popen([b["exe"], url], **kw)


def save_endpoint(ep):
    HOME.mkdir(parents=True, exist_ok=True)
    ENDPOINT.write_text(json.dumps(ep, ensure_ascii=False))


def pick_endpoint(pref=None):
    if os.environ.get("CBU_WS"):
        return {"browser": "custom", "mode": "custom", "ws": os.environ["CBU_WS"]}
    cands = []
    try:
        cands.append(json.loads(ENDPOINT.read_text()))
    except Exception:
        pass
    cands += [{"browser": b["name"], "data": b["data"], "mode": "user"} for b in browser_candidates()]
    cands.append({"browser": "isolated", "data": str(isolated_profile()), "mode": "isolated"})
    for c in cands:
        if pref and pref.lower() not in c["browser"].lower():
            continue
        ws = read_port_file(c["data"])
        if ws:
            return dict(c, ws=ws)
    return None


# --------------------------------------------------------------------------- websocket + CDP

class WebSocket:
    """Minimal RFC 6455 client: text frames, fragmentation, ping/pong. Enough for CDP."""

    def __init__(self, url, timeout):
        m = re.match(r"ws://([^/:]+):(\d+)(/.*)", url)
        if not m:
            raise CBUError(f"bad ws url {url}")
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)  # the handshake blocks until the user clicks Allow
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CBUError("browser closed the connection during handshake (prompt denied?)")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise CBUError("handshake rejected: " + head.decode(errors="replace").splitlines()[0])
        self.buf = bytearray(rest)
        self.sock.settimeout(None)
        self.wlock = threading.Lock()

    def _frame(self, opcode, data):
        n = len(data)
        hdr = bytearray([0x80 | opcode])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        if n:
            k = (mask * (n // 4 + 1))[:n]
            data = (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")
        with self.wlock:
            self.sock.sendall(bytes(hdr) + mask + data)

    def send(self, text):
        self._frame(0x1, text.encode())

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(1 << 16, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("browser closed the connection")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv(self):
        msg = bytearray()
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
            if op == 0x8:
                raise ConnectionError("browser sent close frame")
            if op == 0x9:
                self._frame(0xA, payload)
                continue
            if op == 0xA:
                continue
            msg += payload
            if b1 & 0x80:
                return msg.decode("utf-8", errors="replace")

    def close(self):
        try:
            self._frame(0x8, b"")
            self.sock.close()
        except Exception:
            pass


class CDP:
    def __init__(self, ws_url, connect_timeout):
        self.ws = WebSocket(ws_url, connect_timeout)
        self.ids = itertools.count(1)
        self.pending = {}
        self.listeners = []
        self.closed = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        try:
            while True:
                m = json.loads(self.ws.recv())
                if "id" in m:
                    slot = self.pending.pop(m["id"], None)
                    if slot:
                        slot[1] = m
                        slot[0].set()
                else:
                    for fn in list(self.listeners):
                        try:
                            fn(m)
                        except Exception as e:
                            eprint("listener error", e)
        except Exception as e:
            self.closed = repr(e)
            for slot in list(self.pending.values()):
                slot[0].set()

    def call(self, method, params=None, session=None, timeout=60):
        if self.closed:
            raise CBUError(f"browser connection closed: {self.closed}")
        i = next(self.ids)
        slot = [threading.Event(), None]
        self.pending[i] = slot
        msg = {"id": i, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        if not slot[0].wait(timeout):
            self.pending.pop(i, None)
            raise CBUError(f"{method} timed out after {timeout}s")
        r = slot[1]
        if r is None:
            raise CBUError(f"browser connection closed: {self.closed}")
        if "error" in r:
            err = r["error"]
            raise CBUError(f"{method}: {err.get('message')} {err.get('data', '')}".strip())
        return r.get("result", {})


# --------------------------------------------------------------------------- daemon

class Daemon:
    def __init__(self, cdp, ep):
        self.cdp, self.ep = cdp, ep
        self.sessions = {}        # targetId -> sessionId
        self.owned = []           # targetIds opened by cbu
        self.current = None
        self.watches = {}         # sessionId -> [regex]
        self.inflight = {}        # (sessionId, requestId) -> meta
        self.req_method = {}
        self.captures = {}        # sessionId -> [capture]
        self.last = time.time()
        self.lock = threading.Lock()
        cdp.listeners.append(self.on_event)
        cdp.call("Target.setDiscoverTargets", {"discover": True})
        try:  # remember tabs cbu opened before a daemon restart
            saved = json.loads(OWNED.read_text())
            alive = {t["targetId"] for t in self.pages()}
            self.owned = [t for t in saved.get("owned", []) if t in alive]
            self.current = saved.get("current") if saved.get("current") in self.owned else \
                (self.owned[-1] if self.owned else None)
        except Exception:
            pass

    def save_owned(self):
        try:
            OWNED.write_text(json.dumps({"owned": self.owned, "current": self.current}))
        except OSError:
            pass

    def on_event(self, m):
        method, sid, p = m.get("method"), m.get("sessionId"), m.get("params", {})
        if method == "Target.targetDestroyed":
            t = p.get("targetId")
            with self.lock:
                self.sessions.pop(t, None)
                if t in self.owned:
                    self.owned.remove(t)
                if self.current == t:
                    self.current = self.owned[-1] if self.owned else None
                self.save_owned()
        elif method == "Target.detachedFromTarget":
            with self.lock:
                for t, s in list(self.sessions.items()):
                    if s == p.get("sessionId"):
                        self.sessions.pop(t)
        elif method == "Network.requestWillBeSent" and sid in self.watches:
            self.req_method[(sid, p["requestId"])] = p["request"]["method"]
        elif method == "Network.responseReceived" and sid in self.watches:
            url = p["response"]["url"]
            key = (sid, p["requestId"])
            if self.req_method.get(key) == "OPTIONS":
                return
            if any(rx.search(url) for rx in self.watches[sid]):
                self.inflight[key] = {"url": url, "status": p["response"].get("status"),
                                      "mimeType": p["response"].get("mimeType"),
                                      "method": self.req_method.get(key)}
        elif method == "Network.loadingFinished":
            key = (sid, p.get("requestId"))
            if key in self.inflight:
                threading.Thread(target=self._grab, args=(key,), daemon=True).start()

    def _grab(self, key):
        meta = self.inflight.pop(key)
        sid, rid = key
        try:
            r = self.cdp.call("Network.getResponseBody", {"requestId": rid}, sid, timeout=30)
            body = r.get("body", "")
            if r.get("base64Encoded"):
                meta["base64"] = True
            else:
                try:
                    body = json.loads(body)
                    meta["json"] = True
                except ValueError:
                    pass
            meta["body"] = body
        except Exception as e:
            meta["error"] = str(e)
        meta["ts"] = time.time()
        with self.lock:
            self.captures.setdefault(sid, []).append(meta)

    # --- helpers
    def pages(self):
        return [t for t in self.cdp.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]

    def resolve(self, target, allow_user=False):
        if not target:
            if not self.current:
                raise CBUError("no current tab: run `cbu.py open <url>` first (or pass --tab)")
            target = self.current
        ids = [t["targetId"] for t in self.pages()]
        hits = [t for t in ids if t == target] or [t for t in ids if t.lower().startswith(target.lower())]
        if len(hits) != 1:
            raise CBUError(f"tab {target!r} not found (or ambiguous); see `cbu.py tabs`")
        t = hits[0]
        if t not in self.owned and not allow_user:
            raise CBUError("that tab was not opened by cbu. Only touch the user's own tabs when they "
                           "explicitly ask; then pass --allow-user-tab")
        return t

    def session(self, target):
        with self.lock:
            if target in self.sessions:
                return self.sessions[target]
        sid = self.cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        for dom in ("Page.enable", "Runtime.enable"):
            self.cdp.call(dom, {}, sid)
        with self.lock:
            self.sessions[target] = sid
        return sid

    # --- API (each returns a JSON-able dict)
    def api_ping(self, q):
        return {"ok": True, "browser": self.ep.get("browser"), "mode": self.ep.get("mode")}

    def api_call(self, q):
        sid = None
        if not q.get("browserLevel"):
            sid = self.session(self.resolve(q.get("target"), q.get("allowUser")))
        return {"result": self.cdp.call(q["method"], q.get("params"), sid, q.get("timeout", 60))}

    def api_open(self, q):
        t = self.cdp.call("Target.createTarget", {"url": q.get("url") or "about:blank",
                                                  "background": not q.get("foreground"),
                                                  "newWindow": bool(q.get("newWindow"))})["targetId"]
        with self.lock:
            self.owned.append(t)
            self.current = t
            self.save_owned()
        return {"targetId": t, "sessionId": self.session(t)}

    def api_use(self, q):
        t = self.resolve(q.get("target"), q.get("allowUser"))
        with self.lock:
            self.current = t
            self.save_owned()
        return {"targetId": t}

    def api_tabs(self, q):
        return {"tabs": [{"id": t["targetId"], "title": t.get("title"), "url": t.get("url"),
                          "openedByCbu": t["targetId"] in self.owned,
                          "current": t["targetId"] == self.current} for t in self.pages()]}

    def api_close(self, q):
        targets = list(self.owned) if q.get("allOwned") else [self.resolve(q.get("target"), q.get("allowUser"))]
        for t in targets:
            try:
                self.cdp.call("Target.closeTarget", {"targetId": t})
            except CBUError:
                pass
        return {"closed": targets}

    def api_watch(self, q):
        sid = self.session(self.resolve(q.get("target"), q.get("allowUser")))
        if sid not in self.watches:
            self.cdp.call("Network.enable", {"maxTotalBufferSize": 200_000_000,
                                             "maxResourceBufferSize": 50_000_000}, sid)
            self.watches[sid] = []
        self.watches[sid].append(re.compile(q["pattern"]))
        return {"watching": [rx.pattern for rx in self.watches[sid]]}

    def api_captures(self, q):
        sid = self.session(self.resolve(q.get("target"), q.get("allowUser")))
        with self.lock:
            items = list(self.captures.get(sid, []))
            if q.get("clear"):
                self.captures[sid] = []
        return {"pending": sum(1 for k in self.inflight if k[0] == sid), "captures": items}

    def api_stop(self, q):
        closed = []
        if not q.get("keepTabs"):
            closed = self.api_close({"allOwned": True})["closed"]
        quit_ = False
        if q.get("quitBrowser"):
            if self.ep.get("mode") != "isolated":
                raise CBUError("--quit-browser only applies to an isolated instance; never quit the user's browser")
            threading.Timer(0.3, lambda: self.cdp.call("Browser.close", timeout=5)).start()
            quit_ = True
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        return {"stopped": True, "closedTabs": closed, "quitIsolatedBrowser": quit_}


def run_daemon(ep_json):
    ep = json.loads(ep_json)
    try:
        cdp = CDP(ep["ws"], connect_timeout=APPROVE_TIMEOUT)
        d = Daemon(cdp, ep)
    except Exception as e:
        ERRFILE.write_text(f"could not connect to {ep.get('browser')}: {e}")
        return
    token = secrets.token_urlsafe(24)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            # custom header + token: a web page cannot forge this without a CORS preflight we never answer
            if not secrets.compare_digest(self.headers.get("X-CBU-Token", ""), token):
                self.send_error(403)
                return
            try:
                q = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                fn = getattr(d, "api_" + self.path.strip("/"), None)
                if not fn:
                    raise CBUError("unknown endpoint " + self.path)
                d.last = time.time()
                res = fn(q)
            except Exception as e:
                res = {"error": str(e)}
            body = json.dumps(res, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    d.server = srv

    def watchdog():
        while True:
            time.sleep(5)
            if cdp.closed or time.time() - d.last > IDLE_SECONDS:
                srv.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "port": srv.server_address[1], "token": token,
                               "browser": ep.get("browser"), "mode": ep.get("mode"),
                               "started": time.time()}))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)
    try:
        srv.serve_forever()
    finally:
        try:
            STATE.unlink()
        except OSError:
            pass
        cdp.ws.close()


def _post(st, path, payload, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route localhost via a proxy
    req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-CBU-Token": st["token"]})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def daemon_info():
    try:
        st = json.loads(STATE.read_text())
        return st if _post(st, "ping", {}, 3).get("ok") else None
    except Exception:
        return None


def ensure_daemon(pref=None):
    st = daemon_info()
    if st:
        return st
    ep = pick_endpoint(pref)
    if not ep:
        die("no browser with remote debugging enabled", 3,
            next="run `cbu.py doctor` and follow its `next` field")
    HOME.mkdir(parents=True, exist_ok=True)
    for f in (STATE, ERRFILE):
        try:
            f.unlink()
        except OSError:
            pass
    kw = {"stdin": subprocess.DEVNULL, "stdout": open(LOG, "ab"), "stderr": subprocess.STDOUT}
    if os.name == "posix":
        kw["start_new_session"] = True
    else:
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_daemon", json.dumps(ep)], **kw)
    if ep["mode"] == "user":
        eprint(f"[cbu] connecting to {ep['browser']} - if it asks to allow remote debugging, click Allow.")
    deadline = time.time() + APPROVE_TIMEOUT + 5
    while time.time() < deadline:
        if ERRFILE.exists():
            die(ERRFILE.read_text(), 4, next="the prompt was probably denied or timed out; retry, "
                                             "or run `cbu.py doctor`")
        st = daemon_info()
        if st:
            return st
        time.sleep(0.4)
    die("timed out waiting for the browser connection", 4, log=str(LOG))


# --------------------------------------------------------------------------- client API

SNAP_JS = r"""
(limit) => {
  const sel = 'a[href],button,input,textarea,select,summary,[role=button],[role=link],[role=tab],' +
    '[role=menuitem],[role=checkbox],[role=option],[contenteditable=""],[contenteditable=true],[onclick]';
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  document.querySelectorAll('[data-cbu-ref]').forEach(e => e.removeAttribute('data-cbu-ref'));
  const seen = new Set(), list = [];
  document.querySelectorAll(sel).forEach(e => { if (!seen.has(e)) { seen.add(e); list.push(e); } });
  // cursor:pointer elements catch div/span "buttons" (e.g. 'show more replies')
  for (const e of document.querySelectorAll('body *')) {
    if (seen.has(e)) continue;
    const c = getComputedStyle(e).cursor;
    if (c === 'pointer' && (!e.parentElement || getComputedStyle(e.parentElement).cursor !== 'pointer')) {
      seen.add(e); list.push(e);
    }
  }
  const out = [];
  for (const e of list) {
    if (!vis(e)) continue;
    const r = e.getBoundingClientRect();
    const t = (e.innerText || e.value || e.getAttribute('aria-label') || e.getAttribute('placeholder') ||
               e.title || e.alt || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    if (!t && !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName)) continue;
    const ref = out.length + 1;
    e.setAttribute('data-cbu-ref', ref);
    const o = {ref, tag: e.tagName.toLowerCase(), text: t, inView: r.bottom > 0 && r.top < innerHeight};
    if (e.getAttribute('role')) o.role = e.getAttribute('role');
    if (e.type && e.tagName !== 'BUTTON') o.type = e.type;
    if (e.href) o.href = String(e.href).slice(0, 160);
    out.push(o);
    if (out.length >= limit) break;
  }
  return {url: location.href, title: document.title, scrollY: Math.round(scrollY),
          pageHeight: document.documentElement.scrollHeight, elements: out};
}
"""

FIND_JS = r"""
(ref, selector, text) => {
  let el = null;
  if (ref != null) el = document.querySelector(`[data-cbu-ref="${ref}"]`);
  else if (selector) el = document.querySelector(selector);
  else if (text) {
    let best = null, bestLen = 1e9;
    for (const e of document.querySelectorAll('body *')) {
      const t = (e.innerText || e.value || '').trim();
      if (!t || !t.includes(text)) continue;
      const r = e.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      if (t.length < bestLen) { best = e; bestLen = t.length; }
    }
    el = best;
  }
  if (!el) return null;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  window.__cbuEl = el;
  return {x, y, tag: el.tagName.toLowerCase(), text: (el.innerText || el.value || '').trim().slice(0, 60),
          covered: !(hit && (hit === el || el.contains(hit)))};
}
"""


class Browser:
    """Python API, also exposed as `b` inside `cbu.py run script.py`."""

    def __init__(self, browser=None, allow_user_tab=False):
        self.st = ensure_daemon(browser)
        self.allow_user = allow_user_tab

    def _q(self, path, http_timeout=90, **payload):
        payload.setdefault("allowUser", self.allow_user)
        r = _post(self.st, path, payload, http_timeout)
        if "error" in r:
            raise CBUError(r["error"])
        return r

    def cdp(self, method, params=None, tab=None, browser_level=False, timeout=60):
        return self._q("call", http_timeout=timeout + 10, method=method, params=params or {}, target=tab,
                       browserLevel=browser_level, timeout=timeout)["result"]

    # tabs
    def tabs(self):
        return self._q("tabs")["tabs"]

    def open(self, url, foreground=False, new_window=True, wait=True):
        """Default: a separate background window. Background *tabs* are hidden, so Chrome stops
        rendering them: real input events stall and lazy-loading (IntersectionObserver) pauses."""
        # create blank first, then navigate: about:blank reports readyState=complete immediately,
        # so waiting on a target created with the URL can return before the real page loads
        t = self._q("open", url="about:blank", foreground=foreground, newWindow=new_window)["targetId"]
        if url and url != "about:blank":
            self.goto(url, t, wait=wait)
        return t

    def use(self, tab):
        return self._q("use", target=tab)["targetId"]

    def close(self, tab=None, all_owned=False):
        return self._q("close", target=tab, allOwned=all_owned)["closed"]

    def goto(self, url, tab=None, wait=True):
        r = self.cdp("Page.navigate", {"url": url}, tab)
        if r.get("errorText"):
            raise CBUError("navigation failed: " + r["errorText"])
        if wait:
            self.wait_load(tab)

    def wait_load(self, tab=None, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.eval("document.readyState", tab) == "complete":
                    return True
            except CBUError:
                pass
            time.sleep(0.3)
        return False

    # page
    def eval(self, js, tab=None, timeout=60):
        r = self.cdp("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True, "replMode": True,
                                          "userGesture": True}, tab, timeout=timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise CBUError("JS error: " + (d.get("exception", {}).get("description") or d.get("text", "")))
        return r.get("result", {}).get("value")

    def call_js(self, fn, *args, tab=None):
        return self.eval(f"({fn})({','.join(json.dumps(a) for a in args)})", tab)

    def text(self, selector=None, tab=None, max_chars=20000):
        t = self.call_js("(s) => { const e = s ? document.querySelector(s) : document.body;"
                         " return e ? e.innerText : null; }", selector, tab=tab)
        if t is None:
            raise CBUError(f"selector not found: {selector}")
        return t[:max_chars]

    def snap(self, tab=None, limit=150):
        return self.call_js(SNAP_JS, limit, tab=tab)

    def _find(self, ref, selector, text, tab):
        hit = self.call_js(FIND_JS, ref, selector, text, tab=tab)
        if not hit:
            raise CBUError("element not found (refs expire when the page changes: run snap again)")
        return hit

    def _input(self, method, params, tab):
        """Real input event. Returns False if the page isn't rendering (hidden tab) so callers fall back."""
        try:
            self.cdp(method, params, tab, timeout=8)
            return True
        except CBUError as e:
            if "timed out" in str(e):
                return False
            raise

    def click(self, ref=None, selector=None, text=None, tab=None):
        hit = self._find(ref, selector, text, tab)
        if hit["covered"]:
            self.eval("window.__cbuEl.click()", tab)
            hit["via"] = "js (element covered at its center)"
            return hit
        for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
            if not self._input("Input.dispatchMouseEvent", {"type": typ, "x": hit["x"], "y": hit["y"],
                                                            "button": "left", "clickCount": 1}, tab):
                self.eval("window.__cbuEl.click()", tab)
                hit["via"] = "js (page not rendering: hidden tab? open in a window instead)"
                return hit
        hit["via"] = "mouse"
        return hit

    def type(self, text, ref=None, selector=None, tab=None, clear=False, enter=False):
        hit = None
        if ref is not None or selector:
            hit = self._find(ref, selector, None, tab)
            self.eval("window.__cbuEl.focus()", tab)
            if clear:
                self.eval("(e => { if ('value' in e) { e.value=''; e.dispatchEvent(new Event('input',"
                          "{bubbles:true})); } else e.textContent=''; })(window.__cbuEl)", tab)
        self.cdp("Input.insertText", {"text": text}, tab)
        if enter:
            self.press("Enter", tab)
        return hit or {"typed": len(text)}

    def press(self, key, tab=None):
        codes = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, "ArrowDown": 40, "ArrowUp": 38,
                 "ArrowLeft": 37, "ArrowRight": 39, "PageDown": 34, "PageUp": 33, "End": 35, "Home": 36}
        base = {"key": key, "code": key, "windowsVirtualKeyCode": codes.get(key, 0)}
        down = dict(base, type="keyDown")
        if key == "Enter":
            down["text"] = "\r"
        self.cdp("Input.dispatchKeyEvent", down, tab)
        self.cdp("Input.dispatchKeyEvent", dict(base, type="keyUp"), tab)

    def scroll(self, selector=None, by=None, bottom=False, tab=None, js=False):
        """Default: a real mouse-wheel event over the element (or viewport). Many sites only
        load more content on genuine wheel/scroll input, not on a scrollTop assignment."""
        info = self.call_js("""(s) => {
            const e = s ? document.querySelector(s) : (document.scrollingElement || document.body);
            if (!e) return null;
            const r = s ? e.getBoundingClientRect() : {left: 0, top: 0, width: innerWidth, height: innerHeight};
            return {x: r.left + r.width / 2, y: r.top + Math.min(r.height, innerHeight) / 2,
                    h: Math.min(r.height || innerHeight, innerHeight), sh: e.scrollHeight}; }""",
                            selector, tab=tab)
        if info is None:
            raise CBUError(f"selector not found: {selector}")
        delta = info["sh"] if bottom else (by if by is not None else int(info["h"] * 0.8))
        via = "wheel"
        if js or not self._input("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": info["x"],
                                                               "y": info["y"], "deltaX": 0, "deltaY": delta}, tab):
            via = "js" if js else "js (page not rendering: hidden tab? open in a window instead)"
            self.call_js("""(s, d) => { const e = s ? document.querySelector(s) :
                (document.scrollingElement || document.body); e.scrollTop += d;
                e.dispatchEvent(new Event('scroll', {bubbles: true})); }""", selector, delta, tab=tab)
        time.sleep(0.3)
        pos = self.call_js("""(s) => { const e = s ? document.querySelector(s) :
            (document.scrollingElement || document.body);
            return {scrollTop: Math.round(e.scrollTop), scrollHeight: e.scrollHeight}; }""", selector, tab=tab)
        pos["via"] = via
        return pos

    def wait_for(self, selector=None, text=None, tab=None, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            ok = self.call_js("(s, t) => s ? !!document.querySelector(s) : document.body.innerText.includes(t)",
                              selector, text, tab=tab)
            if ok:
                return True
            time.sleep(0.4)
        raise CBUError(f"timed out waiting for {selector or text!r}")

    def shot(self, path=None, full=False, tab=None):
        params = {"format": "png"}
        if full:
            m = self.cdp("Page.getLayoutMetrics", {}, tab)
            size = m.get("cssContentSize") or m["contentSize"]
            params.update(captureBeyondViewport=True,
                          clip={"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1})
        data = self.cdp("Page.captureScreenshot", params, tab, timeout=60)["data"]
        path = Path(path or Path(tempfile.gettempdir()) / f"cbu-shot-{int(time.time() * 1000)}.png")
        path.write_bytes(base64.b64decode(data))
        return str(path)

    # network
    def watch(self, pattern, tab=None):
        return self._q("watch", target=tab, pattern=pattern)["watching"]

    def captured(self, tab=None, clear=False):
        return self._q("captures", target=tab, clear=clear)


# --------------------------------------------------------------------------- commands

def cmd_doctor(a):
    bs = browser_candidates()
    rows = []
    for b in bs:
        if not b["installed"]:
            continue
        rows.append({"browser": b["name"], "version": b["version"],
                     "remoteDebugging": "on" if read_port_file(b["data"]) else "off"})
    iso = read_port_file(isolated_profile())
    st = daemon_info()
    res = {"platform": platform.system(), "browsers": rows,
           "isolatedInstance": "running" if iso else "not running",
           "daemon": ({"connectedTo": st["browser"], "mode": st["mode"]} if st else None)}
    on = [r for r in rows if r["remoteDebugging"] == "on"]
    if st:
        res["status"], res["next"] = "ready", "connected; go ahead (`open`, `snap`, ...)"
    elif on or iso:
        res["status"] = "ready"
        res["next"] = (f"remote debugging is on in {on[0]['browser']}; the first command connects and the "
                       f"browser shows a one-time Allow prompt - tell the user to click Allow"
                       if on else "isolated instance is running; commands will use it")
    elif rows:
        b = rows[0]
        mj = major(b["version"])
        if mj is not None and mj < MIN_TOGGLE_MAJOR and b["browser"].startswith("Google Chrome"):
            res["status"] = "browser_too_old"
            res["next"] = (f"{b['browser']} {b['version']} has no in-browser debugging toggle. Either ask the "
                           f"user to update Chrome (Settings > About), or run `cbu.py launch` for an isolated "
                           f"profile (no existing logins; the user can sign in there once, it persists)")
        else:
            res["status"] = "debugging_off"
            res["next"] = (f"run `cbu.py setup --browser \"{b['browser']}\"`: it opens "
                           f"chrome://inspect/#remote-debugging; ask the user to tick 'Allow remote debugging "
                           f"for this browser instance'. If the page has no such toggle, use `cbu.py launch`")
    else:
        res["status"] = "no_browser"
        res["next"] = ("no Chromium browser found. Ask the user to install Google Chrome "
                       "(macOS: `brew install --cask google-chrome`; Windows: `winget install Google.Chrome`; "
                       "Linux: distro package or https://www.google.com/chrome/), then rerun doctor. "
                       "Without installing system-wide: `python3 -m pip install playwright && python3 -m "
                       "playwright install chromium`, then `cbu.py launch --browser-path <chromium binary>`")
    emit(res)


def cmd_setup(a):
    b = choose_browser(a.browser)
    if not b:
        die("no installed Chromium browser found", 2, next="run `cbu.py doctor`")
    if read_port_file(b["data"]):
        save_endpoint({"browser": b["name"], "data": b["data"], "mode": "user"})
        emit({"ok": True, "status": "ready", "browser": b["name"]})
        return
    url = f"{b['scheme']}://inspect/#remote-debugging"
    open_url_in(b, url)
    eprint(f"[cbu] opened {url} in {b['name']}.\n"
           f"[cbu] ask the user to tick 'Allow remote debugging for this browser instance'. "
           f"Waiting up to {a.timeout}s ...")
    end = time.time() + a.timeout
    while time.time() < end:
        if read_port_file(b["data"]):
            save_endpoint({"browser": b["name"], "data": b["data"], "mode": "user"})
            emit({"ok": True, "status": "ready", "browser": b["name"],
                  "note": "first connection shows an Allow prompt in the browser"})
            return
        time.sleep(1)
    die("remote debugging was not enabled in time", 2,
        next="re-run setup once the user is ready, or use `cbu.py launch` (isolated profile)")


def cmd_launch(a):
    exe = a.browser_path or (choose_browser(a.browser) or {}).get("exe")
    if not exe:
        die("no browser binary found", 2, next="install Chrome, or pass --browser-path (see `cbu.py doctor`)")
    prof = Path(a.profile).expanduser() if a.profile else isolated_profile()
    ws = read_port_file(prof)
    if not ws:
        prof.mkdir(parents=True, exist_ok=True)
        try:
            (prof / "DevToolsActivePort").unlink()
        except OSError:
            pass
        args = [exe, "--remote-debugging-port=0", f"--user-data-dir={prof}", "--no-first-run",
                "--no-default-browser-check"]
        if a.headless:
            args.append("--headless=new")
        kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "posix":
            kw["start_new_session"] = True
        subprocess.Popen(args + ["about:blank"], **kw)
        end = time.time() + 30
        while time.time() < end and not ws:
            time.sleep(0.5)
            ws = read_port_file(prof)
        if not ws:
            die("browser started but no DevToolsActivePort appeared", 2)
    save_endpoint({"browser": "isolated", "data": str(prof), "mode": "isolated"})
    emit({"ok": True, "status": "ready", "mode": "isolated", "profile": str(prof),
          "note": "separate profile: none of the user's normal logins. Sign-ins made here persist."})


def _b(a):
    return Browser(a.browser, getattr(a, "allow_user_tab", False))


def main():
    # global options are accepted both before and after the subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--browser", default=argparse.SUPPRESS,
                        help="prefer this browser (name substring, e.g. chrome, edge, isolated)")
    common.add_argument("--allow-user-tab", action="store_true", default=argparse.SUPPRESS,
                        help="allow acting on tabs the user opened (only when they explicitly ask)")
    p = argparse.ArgumentParser(prog="cbu.py", description=__doc__, parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    _add_parser = sp.add_parser
    sp.add_parser = lambda name, **kw: _add_parser(name, parents=[common], **kw)

    def add(name, help_):
        s = sp.add_parser(name, help=help_)
        s.add_argument("--tab", help="tab id or id prefix (default: current cbu tab)")
        return s

    sp.add_parser("doctor", help="detect browsers and debugging state; prints the next step")
    s = sp.add_parser("setup", help="open the browser's remote-debugging toggle page and wait for the user")
    s.add_argument("--timeout", type=int, default=180)
    s = sp.add_parser("launch", help="start a separate debuggable instance with its own profile")
    s.add_argument("--browser-path")
    s.add_argument("--profile")
    s.add_argument("--headless", action="store_true")
    sp.add_parser("tabs", help="list tabs")
    s = sp.add_parser("open", help="open URL in a new background window (becomes current)")
    s.add_argument("url")
    s.add_argument("--foreground", action="store_true", help="bring it to the front")
    s.add_argument("--background-tab", action="store_true",
                   help="hidden tab in the user's current window instead (not rendered: no real input/lazy-load)")
    s = add("use", "make a tab current")
    s = add("goto", "navigate the current tab")
    s.add_argument("url")
    s = add("eval", "evaluate JS (await-able) and print the value")
    s.add_argument("js", nargs="?")
    s.add_argument("--file")
    s = add("text", "page (or element) innerText")
    s.add_argument("--selector")
    s.add_argument("--max", type=int, default=20000)
    s = add("snap", "list interactive elements with refs for click/type")
    s.add_argument("--limit", type=int, default=150)
    s = add("click", "click by --ref / --selector / --text")
    s.add_argument("--ref", type=int)
    s.add_argument("--selector")
    s.add_argument("--text")
    s = add("type", "type text (into --ref/--selector if given, else the focused element)")
    s.add_argument("text")
    s.add_argument("--ref", type=int)
    s.add_argument("--selector")
    s.add_argument("--clear", action="store_true")
    s.add_argument("--enter", action="store_true")
    s = add("press", "press a key (Enter, Escape, Tab, ArrowDown, PageDown, ...)")
    s.add_argument("key")
    s = add("scroll", "scroll the page or a scroll container")
    s.add_argument("--selector")
    s.add_argument("--by", type=int)
    s.add_argument("--bottom", action="store_true")
    s.add_argument("--js", action="store_true", help="set scrollTop via JS instead of a real wheel event")
    s = add("wait", "wait for --selector or --text to appear")
    s.add_argument("--selector")
    s.add_argument("--text")
    s.add_argument("--timeout", type=int, default=20)
    s = add("shot", "screenshot to PNG (prints the path)")
    s.add_argument("--out")
    s.add_argument("--full", action="store_true")
    s = add("watch", "capture response bodies whose URL matches a regex")
    s.add_argument("pattern")
    s = add("captured", "dump captured responses")
    s.add_argument("--out")
    s.add_argument("--clear", action="store_true")
    s = add("raw", "send any CDP method (tab session; --browser-level for browser target)")
    s.add_argument("method")
    s.add_argument("params", nargs="?", default="{}")
    s.add_argument("--browser-level", action="store_true")
    s = add("close", "close a cbu tab (or --all)")
    s.add_argument("--all", action="store_true")
    s = sp.add_parser("run", help="run a Python script with `b` (Browser) and `args` predefined")
    s.add_argument("script")
    s.add_argument("args", nargs=argparse.REMAINDER)
    s = sp.add_parser("stop", help="close cbu tabs and disconnect from the browser")
    s.add_argument("--keep-tabs", action="store_true")
    s.add_argument("--quit-browser", action="store_true", help="also quit the isolated instance (isolated mode only)")
    sp.add_parser("_daemon").add_argument("ep")

    a = p.parse_args()
    a.browser = getattr(a, "browser", None)
    a.allow_user_tab = getattr(a, "allow_user_tab", False)
    try:
        if a.cmd == "_daemon":
            return run_daemon(a.ep)
        if a.cmd in ("doctor", "setup", "launch"):
            return {"doctor": cmd_doctor, "setup": cmd_setup, "launch": cmd_launch}[a.cmd](a)
        if a.cmd == "stop":
            st = daemon_info()
            emit(_post(st, "stop", {"keepTabs": a.keep_tabs, "quitBrowser": a.quit_browser}, 30) if st else {"stopped": False,
                                                                               "note": "not running"})
            return
        b = _b(a)
        tab = getattr(a, "tab", None)
        if a.cmd == "tabs":
            emit({"tabs": b.tabs()})
        elif a.cmd == "open":
            t = b.open(a.url, a.foreground, not a.background_tab)
            emit({"tab": t, "title": b.eval("document.title", t), "url": b.eval("location.href", t)})
        elif a.cmd == "use":
            emit({"current": b.use(tab)})
        elif a.cmd == "goto":
            b.goto(a.url, tab)
            emit({"ok": True, "title": b.eval("document.title", tab)})
        elif a.cmd == "eval":
            js = Path(a.file).read_text() if a.file else a.js
            emit({"value": b.eval(js, tab)})
        elif a.cmd == "text":
            print(b.text(a.selector, tab, a.max))
        elif a.cmd == "snap":
            emit(b.snap(tab, a.limit))
        elif a.cmd == "click":
            emit(b.click(a.ref, a.selector, a.text, tab))
        elif a.cmd == "type":
            emit(b.type(a.text, a.ref, a.selector, tab, a.clear, a.enter))
        elif a.cmd == "press":
            b.press(a.key, tab)
            emit({"ok": True})
        elif a.cmd == "scroll":
            emit(b.scroll(a.selector, a.by, a.bottom, tab, a.js))
        elif a.cmd == "wait":
            emit({"ok": b.wait_for(a.selector, a.text, tab, a.timeout)})
        elif a.cmd == "shot":
            emit({"path": b.shot(a.out, a.full, tab)})
        elif a.cmd == "watch":
            emit({"watching": b.watch(a.pattern, tab)})
        elif a.cmd == "captured":
            r = b.captured(tab, a.clear)
            if a.out:
                Path(a.out).write_text(json.dumps(r, ensure_ascii=False))
                emit({"saved": a.out, "count": len(r["captures"]), "pending": r["pending"]})
            else:
                emit(r)
        elif a.cmd == "raw":
            emit(b.cdp(a.method, json.loads(a.params), tab, a.browser_level))
        elif a.cmd == "close":
            emit({"closed": b.close(tab, a.all)})
        elif a.cmd == "run":
            src = Path(a.script).read_text()
            g = {"__name__": "__main__", "__file__": a.script, "b": b, "args": a.args,
                 "json": json, "time": time, "CBUError": CBUError}
            exec(compile(src, a.script, "exec"), g)
    except CBUError as e:
        die(str(e))


if __name__ == "__main__":
    main()
