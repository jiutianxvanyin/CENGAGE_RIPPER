"""Direct Chrome DevTools Protocol driver - no chromedriver, no Selenium.

Why this exists
---------------
Selenium 4.x talks to Chrome through ``chromedriver``. On this machine that
bridge is broken: a freshly created driver answers *every* command with
``WebDriverException: unhandled request`` (chromedriver 154.0.8037.92 vs a
custom Chrome 154.0.8037.100), so page loads and frame lookups can never work.
The DevTools websocket itself is perfectly healthy, so we speak CDP directly.

What CDP gives us that the driver could not
-------------------------------------------
* Real frame enumeration (``Page.getFrameTree``) including cross-origin and
  shadow-DOM hosted frames - no "switch into an element and hope".
* A scriptable handle on the browser process: we can leave it running, reuse it
  next run (login once), and never have a session silently die.
* Per-frame JS evaluation with an isolated world, so a probe can read the
  textbook document without touching the reader's own globals.

Public surface (deliberately small, and easy to fake in tests):

    frame = Frame(id, url, name, parent, depth)
    b = launch_or_attach(cfg)      # CdpBrowser
    b.url() / b.title() / b.navigate(url) / b.wait_ready()
    b.frames()                     # list[Frame], document order, main first
    b.eval(frame_id, js)           # js = function BODY (use ``return``)
    b.html(frame_id)               # outerHTML of that frame's document
    b.click(selector)              # JS click, mouse fallback
    b.press("ArrowRight")          # real key event
    b.screenshot(path)
    b.close(force=False)
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import websocket  # websocket-client


class CdpError(RuntimeError):
    """Any protocol / transport failure, normalised."""


# Chrome replaces page targets all the time (startup placeholder tab, session
# restore, a redirect that opens a new tab, the user closing a tab). When that
# happens every command on the old session answers with one of these, and the
# only correct recovery is "attach again to whatever page is alive now".
_SESSION_GONE = (
    "session with given id not found",
    "no session with given id",
    "session closed",
    "target closed",
    "no target with given id found",
    "inspected target navigated or closed",
    "target is closing",
)

READER_HINTS = ("/reader/", "content-", "cenreader")

# page_score at or above this means "this target is almost certainly the reader"
READER_MIN_SCORE = 8


def is_session_gone(message: str) -> bool:
    text = (message or "").lower()
    return any(needle in text for needle in _SESSION_GONE)


def page_score(url: str) -> int:
    """Rank a page target: the reader we want first, blank/error pages last."""
    u = (url or "").strip().lower()
    if u.startswith(("http://", "https://")):
        score = 2
    elif u in ("", "about:blank"):
        score = 0
    else:                     # chrome://newtab, devtools://, data:, ...
        score = 1
    if any(h in u for h in READER_HINTS):
        score += 6
    if "ebooks.cengage.com" in u:
        score += 3
    if any(bad in u for bad in ("login", "signin", "auth")):
        score -= 3
    return score


# --------------------------------------------------------------------------- #
# Chrome discovery + process control
# --------------------------------------------------------------------------- #
def resolve_chrome(cfg: dict) -> str:
    if cfg.get("chrome_path"):
        return cfg["chrome_path"]
    for name in ("chrome", "google-chrome", "chromium"):
        import shutil
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Users\%s\AppData\Local\Google\Chrome\Application\chrome.exe"
        % os.environ.get("USERNAME", ""),
    ):
        if os.path.exists(candidate):
            return candidate
    return "chrome"


def free_port(start: int = 9222) -> int:
    for port in range(start, start + 60):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise CdpError("no free TCP port in range")


def http_json(port: int, path: str = "/json/version", timeout: float = 1.5) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise CdpError(f"{path} on port {port}: {exc}") from exc


def devtools_alive(port: int, timeout: float = 0.8) -> bool:
    try:
        info = http_json(port, "/json/version", timeout=timeout)
        return bool(info.get("webSocketDebuggerUrl"))
    except CdpError:
        return False


def spawn_chrome(chrome: str, profile: str, port: int, headless: bool = False,
                 extra_args: tuple[str, ...] = ()) -> subprocess.Popen:
    """Start a detached Chrome the caller owns (survives our process exiting)."""
    os.makedirs(profile, exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        # DevTools rejects websocket handshakes that carry an Origin header
        # unless the origin is allowed; our client sends none, but this keeps
        # manual/other clients working too.
        "--remote-allow-origins=*",
        f"--user-data-dir={os.path.abspath(profile)}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        # The reader is opened with window.open(); never let Chrome swallow it.
        "--disable-popup-blocking",
        "--no-sandbox",
        "--disable-gpu",
        "--window-size=1400,1000",
        "about:blank",
    ]
    if headless:
        args.insert(1, "--headless=new")
    args.extend(extra_args)
    flags = 0
    if os.name == "nt":  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        flags = 0x00000008 | 0x00000200
    return subprocess.Popen(args, creationflags=flags, close_fds=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def chrome_pids_on(profile: str) -> list[int]:
    """PIDs of chrome.exe processes started with this profile (Windows only)."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=20)
        rows = json.loads(out.stdout or "[]")
    except Exception:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    key = os.path.abspath(profile).lower()
    return [r["ProcessId"] for r in rows
            if key in (r.get("CommandLine") or "").lower()]


# --------------------------------------------------------------------------- #
# Protocol plumbing
# --------------------------------------------------------------------------- #
@dataclass
class Frame:
    id: str
    url: str = ""
    name: str = ""
    parent: str | None = None
    depth: int = 0

    def hint(self) -> str:
        return " ".join(x for x in (self.name, self.url) if x)


@dataclass
class CdpBrowser:
    port: int
    ws_url: str
    own_process: subprocess.Popen | None = None
    session_id: str = ""
    target_id: str = ""
    _ws: Any = None
    _seq: int = 0
    _contexts: dict[str, int] = field(default_factory=dict)
    _events: list[dict] = field(default_factory=list)

    # -- transport ---------------------------------------------------------- #
    def connect(self) -> "CdpBrowser":
        self._ws = websocket.create_connection(
            self.ws_url, timeout=20, suppress_origin=True)
        self._seq = 0
        self._attach(self._pick_target())
        return self

    def _targets(self) -> list[dict]:
        """All targets, most recently used first where we can know that."""
        try:
            rows = http_json(self.port, "/json/list", timeout=3)
            out = [{"targetId": r.get("id", ""), "type": r.get("type", ""),
                    "url": r.get("url", ""), "title": r.get("title", "")}
                   for r in rows if r.get("id")]
            if out:
                return out
        except CdpError:
            pass
        info = self.send("Target.getTargets", session_id="", retry=False)
        return [{"targetId": t.get("targetId", ""),
                 "type": t.get("type", ""),
                 "url": t.get("url", ""),
                 "title": t.get("title", "")}
                for t in info.get("targetInfos", [])]

    def pages(self) -> list[dict]:
        """Live page targets, best candidate first."""
        pages = [t for t in self._targets() if t.get("type") == "page"]
        pages.sort(key=lambda t: -page_score(t.get("url", "")))
        return pages

    def _pick_target(self) -> str:
        pages = self.pages()
        if pages:
            return pages[0]["targetId"]
        return self.send("Target.createTarget", {"url": "about:blank"},
                         session_id="", retry=False)["targetId"]

    def _attach(self, target_id: str) -> None:
        self.session_id = self.send(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
            session_id="", retry=False)["sessionId"]
        self.target_id = target_id
        self._contexts.clear()
        for method in ("Page.enable", "Runtime.enable"):
            try:
                self.send(method, retry=False)
            except CdpError:
                pass

    def reattach(self) -> str:
        """Attach to a live page after Chrome dropped the one we were driving.

        Keeps the same tab when it still exists (normal case after a redirect),
        otherwise moves to the best remaining page - which is usually the reader
        tab the login flow opened - and only creates a tab as a last resort.
        """
        if self._ws is None:
            raise CdpError("not connected")
        pages = self.pages()
        live = {t["targetId"] for t in pages}
        if self.target_id and self.target_id in live:
            choice = self.target_id
        elif pages:
            choice = pages[0]["targetId"]
        else:
            choice = ""
        for candidate in ([choice] if choice else []) + \
                         [t["targetId"] for t in pages if t["targetId"] != choice]:
            try:
                self._attach(candidate)
                return candidate
            except CdpError:
                continue
        fresh = self._pick_target()          # no usable tab left -> make one
        self._attach(fresh)
        return fresh

    def switch_to(self, target_id: str) -> None:
        """Drive another existing tab (used when login popped a new one)."""
        if target_id and target_id != self.target_id:
            self._attach(target_id)

    def reader_page(self) -> dict | None:
        """The live page target that looks most like the ebook reader."""
        for t in self.pages():
            if page_score(t.get("url", "")) >= READER_MIN_SCORE:
                return t
        return None

    def follow_new_window(self, before_ids=(), timeout: float = 20.0,
                          grace: float = 1.5) -> str:
        """Move to the window/tab the reader opened, and keep driving it.

        Cengage opens the reader in its own window, so the tab we navigated
        never holds the book. This waits for that new target to appear (or for
        the tab we are on to finish loading into the reader) and attaches to
        whichever is the real reader. Returns the URL now driven, '' if none.
        """
        before = set(before_ids)
        start = time.time()

        def fresh() -> list[tuple[int, dict]]:
            rows = [(page_score(t.get("url", "")), t) for t in self.pages()
                    if t["targetId"] not in before
                    and t["targetId"] != self.target_id]
            rows.sort(key=lambda r: -r[0])
            return rows

        while True:
            elapsed = time.time() - start
            candidates = fresh()
            for score, target in candidates:
                # A brand-new window starts on about:blank for a moment; give
                # it a beat before committing to it.
                if score <= 0 and elapsed < grace:
                    continue
                try:
                    self.switch_to(target["targetId"])
                    return self.url()
                except CdpError:
                    continue      # target died between listing and attach
            try:
                current = self.url()
            except CdpError:
                current = ""
            if elapsed >= grace:
                # The tab we are already on may itself have become the reader.
                if page_score(current) >= READER_MIN_SCORE:
                    return current
                # Nothing new appeared and this tab has finished loading: the
                # reader is not coming, so do not stall the caller.
                if not candidates and self._settled():
                    return current
            if elapsed >= timeout:
                return current
            time.sleep(0.25)

    def _settled(self) -> bool:
        try:
            return str(self.eval_main("return document.readyState;")
                       or "") == "complete"
        except CdpError:
            return False

    def send(self, method: str, params: dict | None = None,
             session_id: str | None = None, timeout: float = 30.0,
             retry: bool = True) -> dict:
        if self._ws is None:
            raise CdpError("not connected")
        self._seq += 1
        msg_id = self._seq
        payload: dict[str, Any] = {"id": msg_id, "method": method,
                                   "params": params or {}}
        sid = session_id if session_id is not None else self.session_id
        if sid:
            payload["sessionId"] = sid
        try:
            self._ws.send(json.dumps(payload))
        except Exception as exc:
            raise CdpError(f"send {method}: {exc}") from exc

        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CdpError(f"{method}: timed out after {timeout}s")
            self._ws.settimeout(min(remaining, 5.0))
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:
                raise CdpError(f"{method}: connection lost ({exc})") from exc
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if data.get("id") == msg_id:
                if "error" in data:
                    err = data["error"]
                    message = str(err.get("message", err))
                    # The tab we were attached to is gone: pick up a live one
                    # and replay the command instead of failing the whole run.
                    if retry and is_session_gone(message):
                        try:
                            self.reattach()
                        except CdpError as exc:
                            raise CdpError(
                                f"{method}: {message} "
                                f"(re-attach also failed: {exc})") from exc
                        return self.send(method, params, None, timeout,
                                         retry=False)
                    raise CdpError(f"{method}: {message}")
                return data.get("result", {})
            if "method" in data:
                self._events.append(data)
                del self._events[:-200]

    def close(self, force: bool = False) -> None:
        """Detach. The browser keeps running unless ``force`` and we own it."""
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        if force:
            try:
                ws = websocket.create_connection(self.ws_url, timeout=5,
                                                 suppress_origin=True)
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                ws.close()
            except Exception:
                pass
            time.sleep(0.5)

    # -- page basics -------------------------------------------------------- #
    def url(self) -> str:
        return str(self.eval_main("return document.location.href;") or "")

    def title(self) -> str:
        return str(self.eval_main("return document.title;") or "")

    def navigate(self, url: str, timeout: float = 60.0,
                 follow: bool = True) -> None:
        """Open ``url``, then follow the reader window it opens.

        Two things regularly invalidate the tab we were attached to:
          * the site answers by opening the reader in a NEW window, and
          * Chrome replaces the startup placeholder tab.
        Both surface as ``Session with given id not found``. ``send`` already
        re-attaches on that, and here we additionally switch to whichever
        target actually became the reader.
        """
        self._contexts.clear()
        before = {t["targetId"] for t in self.pages()}
        failure = ""
        try:
            res = self.send("Page.navigate", {"url": url})
            # ``errorText`` is a per-navigation failure (blocked, DNS, ...).
            # ERR_ABORTED is normal when the site redirects or opens a window.
            err = str(res.get("errorText") or "")
            if err and err != "net::ERR_ABORTED":
                failure = err
        except CdpError as exc:
            failure = str(exc)

        if follow:
            landed = self.follow_new_window(before, timeout=20.0)
            if not landed and failure:
                raise CdpError(f"navigate to {url}: {failure}")
        elif failure:
            raise CdpError(f"navigate to {url}: {failure}")
        self.wait_ready(timeout)

    def wait_ready(self, timeout: float = 60.0) -> str:
        """Poll until the main document is interactive/complete."""
        deadline = time.time() + timeout
        state = ""
        while time.time() < deadline:
            try:
                state = str(self.eval_main("return document.readyState;") or "")
                if state in ("interactive", "complete"):
                    return state
            except CdpError:
                pass
            time.sleep(0.25)
        return state

    def screenshot(self, path: str) -> bool:
        try:
            res = self.send("Page.captureScreenshot", {"format": "png"})
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "wb") as f:
                f.write(base64.b64decode(res["data"]))
            return True
        except Exception:
            return False

    # -- frames ------------------------------------------------------------- #
    def frames(self) -> list[Frame]:
        tree = self.send("Page.getFrameTree")["frameTree"]
        out: list[Frame] = []

        def walk(node: dict, parent: str | None, depth: int) -> None:
            f = node.get("frame", {})
            out.append(Frame(id=f.get("id", ""), url=f.get("url", ""),
                             name=f.get("name", "") or "", parent=parent,
                             depth=depth))
            for child in node.get("childFrames", []) or []:
                walk(child, f.get("id"), depth + 1)

        walk(tree, None, 0)
        return out

    def main_frame_id(self) -> str:
        for f in self.frames():
            if f.parent is None:
                return f.id
        raise CdpError("no main frame")

    # -- evaluation --------------------------------------------------------- #
    def _context(self, frame_id: str, refresh: bool = False) -> int:
        if not refresh and frame_id in self._contexts:
            return self._contexts[frame_id]
        res = self.send("Page.createIsolatedWorld",
                        {"frameId": frame_id, "worldName": "wbprobe"})
        ctx = int(res["executionContextId"])
        self._contexts[frame_id] = ctx
        return ctx

    def eval(self, frame_id: str, js: str, timeout: float = 20.0,
             is_async: bool = False) -> Any:
        """Run ``js`` as a function body inside ``frame_id``'s document.

        ``is_async=True`` wraps it in an async function, so the body may use
        ``await`` (used for inlining resources inside the page).
        """
        if is_async:
            expression = "(async function(){\n" + js + "\n})()"
        else:
            expression = "(function(){\n" + js + "\n})()"
        last: Exception | None = None
        for attempt in (0, 1):
            try:
                ctx = self._context(frame_id, refresh=attempt > 0)
                res = self.send("Runtime.evaluate", {
                    "expression": expression,
                    "contextId": ctx,
                    "returnByValue": True,
                    "awaitPromise": True,
                }, timeout=timeout)
                if "exceptionDetails" in res:
                    raise CdpError(str(res["exceptionDetails"].get("text")
                                       or res["exceptionDetails"])[:200])
                return res.get("result", {}).get("value")
            except CdpError as exc:
                last = exc
                # Stale context after a navigation/frame swap: rebuild once.
                self._contexts.pop(frame_id, None)
                time.sleep(0.05)
        raise CdpError(f"eval in frame {frame_id[:8]}: {last}")

    def eval_main(self, js: str, timeout: float = 20.0) -> Any:
        return self.eval(self.main_frame_id(), js, timeout=timeout)

    def html(self, frame_id: str) -> str:
        return str(self.eval(frame_id,
                             "return document.documentElement ? "
                             "document.documentElement.outerHTML : '';") or "")

    # -- interaction -------------------------------------------------------- #
    def click(self, selector: str, frame_id: str | None = None) -> bool:
        """Click by selector: JS click first, then a real mouse event."""
        fid = frame_id or self.main_frame_id()
        js_selector = json.dumps(selector)
        try:
            hit = self.eval(fid, f"""
var el = document.querySelector({js_selector});
if (!el) return false;
el.scrollIntoView({{block: 'center', inline: 'center'}});
el.click();
return true;
""")
            if hit:
                return True
        except CdpError:
            pass
        # Real mouse fallback (only meaningful in the main frame: coordinates
        # inside a child frame are relative to that frame's viewport).
        if fid != self.main_frame_id():
            return False
        try:
            rect = self.eval(fid, f"""
var el = document.querySelector({js_selector});
if (!el) return null;
el.scrollIntoView({{block: 'center'}});
var r = el.getBoundingClientRect();
return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};
""")
        except CdpError:
            return False
        if not rect:
            return False
        x, y = float(rect["x"]), float(rect["y"])
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": kind, "x": x, "y": y, "button": "left",
                      "clickCount": 1}
            self.send("Input.dispatchMouseEvent", params)
        return True

    KEYS = {
        "ArrowRight": (39, "ArrowRight"),
        "ArrowLeft": (37, "ArrowLeft"),
        "PageDown": (34, "PageDown"),
        "PageUp": (33, "PageUp"),
        "Escape": (27, "Escape"),
    }

    def press(self, key: str = "ArrowRight", frame_id: str | None = None) -> bool:
        """Send a key press. Real input first, synthetic event in-frame after."""
        code, name = self.KEYS.get(key, (0, key))
        try:
            for kind in ("keyDown", "keyUp"):
                self.send("Input.dispatchKeyEvent", {
                    "type": kind, "key": name, "code": name,
                    "windowsVirtualKeyCode": code,
                    "nativeVirtualKeyCode": code,
                })
            return True
        except CdpError:
            pass
        if frame_id:
            try:
                self.eval(frame_id, f"""
var e = new KeyboardEvent('keydown', {{key: '{name}', code: '{name}',
  keyCode: {code}, which: {code}, bubbles: true}});
(document.activeElement || document.body).dispatchEvent(e);
return true;
""")
                return True
            except CdpError:
                return False
        return False


# --------------------------------------------------------------------------- #
# Launch / attach
# --------------------------------------------------------------------------- #
def close_browser_on_port(port: int, timeout: float = 8.0) -> bool:
    """Ask the Chrome behind ``port`` to close (used when its mode no longer fits)."""
    try:
        info = http_json(port, "/json/version")
        ws = websocket.create_connection(info["webSocketDebuggerUrl"], timeout=5,
                                         suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        ws.close()
    except Exception:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not devtools_alive(port):
            return True
        time.sleep(0.2)
    return False


def _marker_path(profile: str) -> str:
    return os.path.join(os.path.abspath(profile), ".ripper-launch.json")


def _read_marker(profile: str) -> dict | None:
    try:
        with open(_marker_path(profile), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_marker(profile: str, port: int, headless: bool) -> None:
    try:
        with open(_marker_path(profile), "w", encoding="utf-8") as f:
            json.dump({"port": port, "headless": bool(headless),
                       "launched_by": "cengage_ripper",
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)
    except Exception:
        pass


def launch_or_attach(cfg: dict, log=print) -> CdpBrowser:
    """Return a connected browser.

    Three modes, picked by config:
      * ``attach_port``  > 0 : connect to that port only (never launch);
      * ``browser_port`` > 0 : reuse the Chrome on that port, else launch one
                               with the persistent profile (default);
      * ``user_data_dir`` is None : launch with a throwaway profile.
    """
    attach_only = int(cfg.get("attach_port") or 0)
    port = attach_only or int(cfg.get("browser_port") or 9222)
    profile = cfg.get("user_data_dir") or "chrome-profile"

    if devtools_alive(port):
        mode = _read_marker(profile)
        if mode and bool(mode.get("headless")) != bool(cfg.get("headless")):
            log(f"  the Chrome on port {port} was running "
                f"{'headless' if mode.get('headless') else 'headed'}; "
                f"restarting it to match this run")
            close_browser_on_port(port)
        else:
            info = http_json(port, "/json/version")
            log(f"  reusing Chrome on port {port} ({info.get('Browser')})")
            return CdpBrowser(port=port,
                              ws_url=info["webSocketDebuggerUrl"]).connect()

    if attach_only:
        raise CdpError(
            f"nothing is listening on port {attach_only}. Start Chrome with "
            f"start_chrome.bat {attach_only}, or drop --attach to let the tool "
            f"launch its own.")

    chrome = resolve_chrome(cfg)
    if "user_data_dir" in cfg and cfg["user_data_dir"] is None:
        import tempfile
        profile = tempfile.mkdtemp(prefix="ripper-profile-")  # throwaway
    log(f"  launching Chrome on port {port} (profile: {profile})")
    proc = spawn_chrome(chrome, profile, port,
                        headless=bool(cfg.get("headless")))

    deadline = time.time() + float(cfg.get("launch_timeout", 45))
    exited_at = None
    while time.time() < deadline:
        if devtools_alive(port):
            info = http_json(port, "/json/version")
            log(f"  chrome ready ({info.get('Browser')})")
            _write_marker(profile, port, bool(cfg.get("headless")))
            browser = CdpBrowser(port=port, ws_url=info["webSocketDebuggerUrl"],
                                 own_process=proc)
            return browser.connect()
        if proc.poll() is not None:
            exited_at = exited_at or time.time()
            if time.time() - exited_at > 4:
                break
        time.sleep(0.25)

    pids = chrome_pids_on(profile)
    raise CdpError(
        f"Chrome did not open a DevTools port on {port} "
        f"(launcher rc={proc.poll()}). "
        + (f"Another Chrome is already using the profile {profile} (pids {pids}); "
           f"close it, or run with --attach {port}." if pids else
           "Check that chrome.exe runs and no policy blocks remote debugging."))


if __name__ == "__main__":  # tiny smoke test
    cfg = {"chrome_path": None, "user_data_dir": None, "headless": True,
           "browser_port": 0, "attach_port": 0}
    b = launch_or_attach(cfg)
    try:
        print("url:", b.url(), "| title:", b.title())
        print("frames:", [(f.id[:8], f.url[:40]) for f in b.frames()])
    finally:
        b.close(force=True)
    sys.exit(0)
