"""Tests for frame location + the automatic page-turning loop.

No browser and no Cengage account: ``FakeBrowser`` implements the small CDP
surface the tool uses (frames / eval / html / click / press), so the risky logic
is verified deterministically:

  * where the content lives (hinted frame / unlabelled frame / main document),
  * page advance detection, end-of-book, stuck reader,
  * resume-skip, max_pages, and "never lose a page" index flushing,
  * the browser-lifecycle rules (never close what we did not launch).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cdp  # noqa: E402
from cdp import Frame  # noqa: E402
from cengage_ripper import (  # noqa: E402
    ContentFrame,
    capture,
    close_browser,
    goto_next_page,
    hits_boundary,
    read_state,
    rotate_old_capture,
    save_index,
    signature,
)
from config import DEFAULTS  # noqa: E402
from reader_extract import (  # noqa: E402
    extract_page_html,
    inline_assets,
    locate_content_frame,
)


def _page_doc(title, cgi):
    return (
        '<html xmlns:epub="http://www.idpf.org/2007/ops"><head>'
        f"<title>{title}</title></head>"
        f'<body class="webreader" data-cgi="{cgi}" epub:type="bodymatter">'
        f"<section><h1>{title}</h1><p>Body of {title}</p></section></body></html>"
    )


PAGES = [
    {"title": "Chapter 1", "cgi": "CGI0001", "label": "1", "path": "p01"},
    {"title": "Chapter 1", "cgi": "CGI0002", "label": "2", "path": "p02"},
    {"title": "1.1 Reading Data", "cgi": "CGI0003", "label": "3", "path": "p03"},
    {"title": "Chapter 2", "cgi": "CGI0004", "label": "4", "path": "p04"},
]

# Real Cengage books stamp ONE data-cgi on every page of the title (cover,
# titlepage, copyright, ...), so page identity must not depend on it.
SAME_CGI_PAGES = [
    {"title": "Book cover", "cgi": "BOOKLEVEL", "label": "", "path": "cover"},
    {"title": "Title Page", "cgi": "BOOKLEVEL", "label": "i", "path": "titlepage"},
    {"title": "Copyright Statement", "cgi": "BOOKLEVEL", "label": "ii",
     "path": "copyright"},
]

READER_URL = "https://ebooks.cengage.com/reader/abc/content-cover"


class FakeBrowser:
    """Walks a fixed list of pages, one per next-click.

    ``where`` mirrors the shapes the real reader has used:
        "frame"  - content in a frame whose name/url looks like the reader
        "scan"   - content in a plain, unlabelled frame
        "inline" - content directly in the main document
        "gone"   - nowhere (locator must fail)
    """

    MAIN = "MAIN0001"
    CHILD = "CHILD001"

    def __init__(self, pages, start=0, stuck=False, where="frame"):
        self.pages = pages
        self.index = start
        self.stuck = stuck
        self.where = where
        self.clicks = 0
        self.closed = []
        self.port = 9222
        self.target_id = "T1"

    # --- CDP surface ------------------------------------------------------- #
    def url(self):
        return READER_URL

    def title(self):
        return "Reader"

    def main_frame_id(self):
        return self.MAIN

    def frames(self):
        main = Frame(id=self.MAIN, url=READER_URL, name="", parent=None, depth=0)
        if self.where == "inline" or self.where == "gone":
            return [main]
        if self.where == "frame":
            child = Frame(id=self.CHILD, url=READER_URL, name="iframe-page",
                          parent=self.MAIN, depth=1)
        else:  # unlabelled frame: only reachable by enumerating frames
            child = Frame(id=self.CHILD, url="https://reader.example/frame/9",
                          name="", parent=self.MAIN, depth=1)
        return [main, child]

    def _holds_content(self, frame_id):
        if self.where == "frame" or self.where == "scan":
            return frame_id == self.CHILD
        if self.where == "inline":
            return frame_id == self.MAIN
        return False

    @property
    def page(self):
        return self.pages[self.index]

    @property
    def page_url(self):
        """Per-page document URL, the way the real reader serves them."""
        return (f"https://ebooks.cengage.com/projectVersions/pv/index/"
                f"html5/43/content/content/{self.page.get('path', 'cover')}.html")

    def eval(self, frame_id, js, timeout=20.0, is_async=False):
        if "data:image" in js or "FileReader" in js:  # INLINE_ASSETS_JS
            return {"imgs": 1, "inlined": 1, "failed": 0, "failedSrc": []}
        if "outerHTML" in js:
            if self._holds_content(frame_id):
                return self._doc()
            return "<html><body class='reader-shell'>shell only</body></html>"
        if "epub" in js:  # reader_extract.PROBE_JS
            if self._holds_content(frame_id):
                return {"title": self.page["title"], "cls": "webreader",
                        "cgi": self.page.get("cgi", "BOOKLEVEL"),
                        "epub": "bodymatter", "url": self.page_url,
                        "len": 900, "w": 900, "h": 1600}
            return {"title": "Reader", "cls": "reader-shell", "cgi": "",
                    "epub": "", "url": READER_URL, "len": 40, "w": 1400, "h": 900}
        if "next-page-button" in js:  # cengage_ripper.SHELL_STATE_JS
            last = self.index >= len(self.pages) - 1
            return {
                "page_label": self.page["label"],
                "next_present": True,
                "next_disabled": last,
                "next_title": "" if last else self.pages[self.index + 1]["title"],
            }
        return {}

    def eval_main(self, js, timeout=20.0):
        return self.eval(self.MAIN, js, timeout=timeout)

    def html(self, frame_id):
        return self._doc() if self._holds_content(frame_id) else \
            "<html><body class='reader-shell'>shell only</body></html>"

    def _doc(self):
        return _page_doc(self.page["title"], self.page["cgi"])

    # --- interaction ------------------------------------------------------- #
    def click(self, selector, frame_id=None):
        if selector != "#next-page-button":
            return False
        if self.index >= len(self.pages) - 1:
            return False  # disabled button: click does nothing
        self.clicks += 1
        if not self.stuck:
            self.index += 1
        return True

    def press(self, key="ArrowRight", frame_id=None):
        return False

    def send(self, method, params=None, session_id=None, timeout=30.0):
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "url": self.url(),
                                     "title": self.title(), "targetId": "T1"}]}
        return {}

    def screenshot(self, path):
        return True

    def close(self, force=False):
        self.closed.append(bool(force))
        return None


@pytest.fixture
def cfg(tmp_path):
    conf = dict(DEFAULTS)
    conf.update({
        "pages_dir": str(tmp_path / "pages"),
        "debug_dir": None,
        "page_wait": 0,
        "poll_interval": 0.01,
        "advance_timeout": 0.3,
        "frame_timeout": 1,
        "max_pages": 0,
        "resume": False,
        "toc": True,
        "diagnose_dir": str(tmp_path / "diagnose"),
        "close_browser": False,
    })
    return conf


# --------------------------------------------------------------------------- #
# Locating the content frame
# --------------------------------------------------------------------------- #
def test_locate_prefers_reader_looking_frame():
    frame_id, how, info = locate_content_frame(FakeBrowser(PAGES), timeout=0)
    assert frame_id == FakeBrowser.CHILD
    assert how.startswith("hint:")
    assert info["cgi"] == "CGI0001"


def test_locate_finds_unlabelled_frame():
    _frame_id, how, _info = locate_content_frame(
        FakeBrowser(PAGES, where="scan"), timeout=0)
    assert how.startswith("frame:")


def test_locate_falls_back_to_main_document():
    frame_id, how, _info = locate_content_frame(
        FakeBrowser(PAGES, where="inline"), timeout=0)
    assert how == "main-document"
    assert frame_id == FakeBrowser.MAIN


def test_locate_returns_nothing_when_content_is_absent():
    frame_id, how, info = locate_content_frame(
        FakeBrowser(PAGES, where="gone"), timeout=0.05)
    assert (frame_id, how, info) == (None, None, None)


def test_extract_raises_actionable_error_when_content_missing():
    with pytest.raises(ValueError) as exc:
        extract_page_html(FakeBrowser(PAGES, where="gone"), timeout=0.05)
    assert "diagnose.py" in str(exc.value)


@pytest.mark.parametrize("where", ["frame", "scan", "inline"])
def test_extract_returns_real_webreader_html(where):
    html = extract_page_html(FakeBrowser(PAGES, where=where), timeout=0.2)
    assert 'class="webreader"' in html
    assert "Body of Chapter 1" in html


# --------------------------------------------------------------------------- #
# Page state + turning
# --------------------------------------------------------------------------- #
def test_read_state_reads_label_and_next_button(cfg):
    state = read_state(FakeBrowser(PAGES), cfg, ContentFrame())
    assert state["page_label"] == "1"
    assert state["next_present"] is True
    assert state["next_enabled"] is True
    assert state["next_title"] == "Chapter 1"
    assert state["cgi"] == "CGI0001"
    assert state["page_size"] == [900, 1600]


def test_read_state_marks_last_page_disabled(cfg):
    state = read_state(FakeBrowser(PAGES, start=3), cfg)
    assert state["next_enabled"] is False


def test_goto_next_page_reports_strategy(cfg):
    assert goto_next_page(FakeBrowser(PAGES), cfg) == "click"
    assert goto_next_page(FakeBrowser(PAGES, start=3), cfg) == ""


def test_signature_uses_the_page_url_because_cgi_is_book_level():
    # Two different pages of the same book carry the same data-cgi.
    a = signature({"cgi": "BOOK", "title": "Cover", "page_label": "",
                   "frame_url": "https://x/pv/html5/43/content/content/cover.html"})
    b = signature({"cgi": "BOOK", "title": "Copyright", "page_label": "",
                   "frame_url": "https://x/pv/html5/43/content/content/"
                                "copyright.html?code=abc#frag"})
    assert a != b
    assert a == ("url", "content/cover.html")
    assert b == ("url", "content/copyright.html")


def test_signature_falls_back_to_cgi_then_title():
    assert signature({"cgi": "X1", "title": "t", "page_label": "1"}) == ("cgi", "X1")
    assert signature({"cgi": "", "title": "t", "page_label": "7"}) == ("title", "t|7")


def test_capture_advances_even_when_every_page_shares_one_cgi(cfg):
    """Regression: the real reader stopped after page 1 because all pages had
    the same data-cgi, so the pager never saw the identity change."""
    metas = capture(FakeBrowser(SAME_CGI_PAGES), cfg)
    assert [m["title"] for m in metas] == [p["title"] for p in SAME_CGI_PAGES]
    assert len({tuple(m["sig"]) for m in metas}) == len(SAME_CGI_PAGES)


# --------------------------------------------------------------------------- #
# Capture loop
# --------------------------------------------------------------------------- #
def test_capture_walks_every_page(cfg):
    metas = capture(FakeBrowser(PAGES), cfg)
    assert [m["title"] for m in metas] == [p["title"] for p in PAGES]
    assert [m["sig"][0] for m in metas] == ["url"] * len(PAGES)
    assert [m["sig"][1] for m in metas] == [f"content/p0{i}.html" for i in range(1, 5)]
    assert os.path.exists(os.path.join(cfg["pages_dir"], "page_0004.html"))


def test_capture_flushes_index_after_each_page(cfg):
    capture(FakeBrowser(PAGES), cfg)
    with open(os.path.join(cfg["pages_dir"], "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    assert len(index) == len(PAGES)
    assert index[0]["file"] == "page_0001.html"


def test_capture_stops_at_end_of_book(cfg):
    browsed = FakeBrowser(PAGES)
    metas = capture(browsed, cfg)
    assert len(metas) == len(PAGES)
    assert browsed.clicks == len(PAGES) - 1


def test_capture_stops_when_the_reader_is_stuck(cfg):
    metas = capture(FakeBrowser(PAGES, stuck=True), cfg)
    assert len(metas) == 1


def test_capture_respects_max_pages(cfg):
    cfg["max_pages"] = 2
    metas = capture(FakeBrowser(PAGES), cfg)
    assert len(metas) == 2


def test_hits_boundary_reads_the_title_and_the_reader_url():
    assert hits_boundary({"title": "Data and Statistics"}, "data and statistics")
    assert not hits_boundary({"title": "Acknowledgments xxvii"}, "chapter 1")
    assert hits_boundary({"frame_url": "https://…/content-bd_ch_01_opener"},
                         "bd_ch_01")
    assert not hits_boundary({"title": "Preface"}, ""), "empty needle stops nothing"


def test_hits_boundary_finds_a_chapter_through_the_url_slug():
    """"Chapter 1" must match ``bd_ch_01_opener``: the reader's <title> drops
    the chapter prefix, so title matching alone would never fire."""
    opener = {"title": "Data and Statistics",
              "frame_url": "https://…/html5/43/content/content/bd_ch_01_opener.html"}
    later = {"title": "Other Sampling Methods",
             "frame_url": "https://…/html5/43/content/content/bd_ch_07_sect_08.html"}
    assert hits_boundary(opener, "Chapter 1")
    assert not hits_boundary(later, "Chapter 1"), "ch_07 is not chapter 1"
    assert hits_boundary(later, "chapter 7"), "0-padded slug"
    assert not hits_boundary(opener, "Chapter 10"), "10 must not match ch_01"


def test_capture_stops_before_the_until_page(cfg):
    """--until is exclusive: the named page itself is not captured."""
    cfg["until"] = "1.1 Reading Data"
    metas = capture(FakeBrowser(PAGES), cfg)
    assert [m["title"] for m in metas] == ["Chapter 1", "Chapter 1"]
    assert not os.path.exists(os.path.join(cfg["pages_dir"], "page_0003.html"))


def test_capture_resumes_and_skips_captured_pages(cfg):
    first = capture(FakeBrowser(PAGES[:2]), cfg)
    assert len(first) == 2

    cfg["resume"] = True
    second = capture(FakeBrowser(PAGES), cfg)
    sigs = [tuple(m["sig"]) for m in second]
    assert len(sigs) == len(PAGES)
    assert len(set(sigs)) == len(PAGES), "no page may be captured twice"


def test_save_index_is_atomic(cfg):
    save_index(cfg, [{"page": 1, "title": "A"}])
    path = os.path.join(cfg["pages_dir"], "index.json")
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == [{"page": 1, "title": "A"}]
    assert not os.path.exists(path + ".tmp")


def test_rotate_old_capture_moves_instead_of_deleting(cfg):
    os.makedirs(cfg["pages_dir"], exist_ok=True)
    open(os.path.join(cfg["pages_dir"], "page_0001.html"), "w").close()
    rotate_old_capture(cfg)
    assert not os.path.exists(os.path.join(cfg["pages_dir"], "page_0001.html"))
    backups = [n for n in os.listdir(os.path.dirname(cfg["pages_dir"]))
               if n.startswith("pages.bak-")]
    assert backups, "previous capture must be moved aside, not deleted"


# --------------------------------------------------------------------------- #
# Image inlining (figures must survive the trip out of the browser)
# --------------------------------------------------------------------------- #
def test_inline_assets_reports_counts():
    stats = inline_assets(FakeBrowser(PAGES), FakeBrowser.CHILD)
    assert stats == {"imgs": 1, "inlined": 1, "failed": 0, "failedSrc": []}


def test_inline_assets_never_raises():
    class Broken(FakeBrowser):
        def eval(self, frame_id, js, timeout=20.0, is_async=False):
            if "FileReader" in js:
                raise cdp.CdpError("context destroyed")
            return super().eval(frame_id, js, timeout=timeout, is_async=is_async)

    stats = inline_assets(Broken(PAGES), Broken.CHILD)
    assert stats["imgs"] == 0
    assert "context destroyed" in stats["failedSrc"][0]


# --------------------------------------------------------------------------- #
# Browser lifecycle: the bug that closed the user's browser
# --------------------------------------------------------------------------- #
def test_close_browser_keeps_it_open_by_default(cfg):
    browser = FakeBrowser(PAGES)
    close_browser(browser, cfg)
    assert browser.closed == [False], "detach only - never quit"


def test_close_browser_honours_close_flag(cfg):
    cfg["close_browser"] = True
    browser = FakeBrowser(PAGES)
    close_browser(browser, cfg)
    assert browser.closed == [True]


@pytest.mark.parametrize("argv,expected", [
    (["--port", "9333"], ("browser_port", 9333)),
    (["--attach"], ("attach_port", 9222)),
    (["--attach", "9444"], ("attach_port", 9444)),
])
def test_cli_wires_port_flags(argv, expected):
    import config as config_mod
    old = sys.argv
    sys.argv = ["cengage_ripper.py"] + argv
    try:
        cfg = config_mod.load_config()
    finally:
        sys.argv = old
    key, value = expected
    assert cfg[key] == value


def test_attach_only_mode_refuses_to_launch():
    cfg = dict(DEFAULTS)
    cfg.update({"attach_port": cdp.free_port(9560), "user_data_dir": None})
    with pytest.raises(cdp.CdpError) as exc:
        cdp.launch_or_attach(cfg, log=lambda *_: None)
    assert "nothing is listening" in str(exc.value)


def test_spawn_chrome_uses_a_separate_profile_and_port(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        class P:
            pid = 1
            def poll(self):
                return 0
        return P()

    monkeypatch.setattr(cdp.subprocess, "Popen", fake_popen)
    cdp.spawn_chrome("chrome.exe", str(tmp_path / "prof"), 9333, headless=True)

    args = captured["args"]
    assert "--remote-debugging-port=9333" in args
    assert f"--user-data-dir={tmp_path / 'prof'}" in args
    assert "--headless=new" in args
    assert "--remote-allow-origins=*" in args


# --------------------------------------------------------------------------- #
# Session recovery: the reader opens its OWN window
# --------------------------------------------------------------------------- #
READER_URL_FULL = "https://ebooks.cengage.com/reader/abc/content-cover?code=x"


class FakeWs:
    """Duck-typed websocket answering CDP commands from a handler."""

    def __init__(self, handler):
        self.handler = handler
        self.sent = []
        self.outbox = []

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        reply = dict(self.handler(msg) or {"result": {}})
        reply["id"] = msg["id"]
        self.outbox.append(json.dumps(reply))

    def settimeout(self, timeout):
        pass

    def recv(self):
        if not self.outbox:
            raise AssertionError("no queued CDP response")
        return self.outbox.pop(0)

    def close(self):
        pass


def _offline(monkeypatch):
    """Make the HTTP target list unavailable so only CDP answers."""
    def boom(*a, **kw):
        raise cdp.CdpError("offline")
    monkeypatch.setattr(cdp, "http_json", boom)


def _browser(handler):
    b = cdp.CdpBrowser(port=9222, ws_url="ws://fake")
    b._ws = FakeWs(handler)
    b.session_id = "S-T1"
    b.target_id = "T1"
    return b


def _page(target_id, url, title="t"):
    return {"targetId": target_id, "type": "page", "url": url, "title": title}


def _targets_reply(targets):
    return {"result": {"targetInfos": list(targets)}}


def _eval_reply(value="complete"):
    return {"result": {"result": {"value": value}}}


def test_send_replays_command_after_the_tab_is_replaced(monkeypatch):
    _offline(monkeypatch)
    state = {"targets": [_page("T1", READER_URL_FULL)], "attach": [],
             "navigate": 0}

    def handler(msg):
        method = msg["method"]
        if method == "Target.getTargets":
            return _targets_reply(state["targets"])
        if method == "Target.attachToTarget":
            state["attach"].append(msg["params"]["targetId"])
            return {"result": {"sessionId": "S-" + msg["params"]["targetId"]}}
        if method == "Page.navigate":
            state["navigate"] += 1
            if state["navigate"] == 1:
                # exactly what the user hit: Chrome dropped our target
                return {"error": {"message": "Session with given id not found"}}
            return {"result": {}}
        return {"result": {}}

    browser = _browser(handler)
    browser.send("Page.navigate", {"url": READER_URL_FULL})

    assert state["navigate"] == 2, "the command must be replayed, not dropped"
    assert state["attach"] == ["T1"], "must re-attach before replaying"


def test_navigate_follows_the_window_the_reader_opens(monkeypatch):
    _offline(monkeypatch)
    state = {"targets": [_page("T1", "https://ebooks.cengage.com/", "portal")],
             "attach": []}

    def handler(msg):
        method, params = msg["method"], msg.get("params", {})
        if method == "Target.getTargets":
            return _targets_reply(state["targets"])
        if method == "Target.attachToTarget":
            state["attach"].append(params["targetId"])
            return {"result": {"sessionId": "S-" + params["targetId"]}}
        if method == "Page.navigate":
            # the reader answers by opening a NEW window
            state["targets"].append(_page("T2", READER_URL_FULL, "Reader"))
            return {"result": {"errorText": "net::ERR_ABORTED"}}
        if method == "Page.getFrameTree":
            return {"result": {"frameTree": {"frame": {"id": "F1", "url": "u"}}}}
        if method == "Page.createIsolatedWorld":
            return {"result": {"executionContextId": 3}}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            return _eval_reply("complete" if "readyState" in expr else READER_URL_FULL)
        return {"result": {}}

    browser = _browser(handler)
    browser.navigate(READER_URL_FULL, timeout=5)

    assert browser.target_id == "T2", "must drive the reader window, not the opener"
    assert "T2" in state["attach"]


def test_navigate_keeps_the_same_tab_when_no_new_window_opens(monkeypatch):
    _offline(monkeypatch)
    state = {"targets": [_page("T1", READER_URL_FULL)], "attach": []}

    def handler(msg):
        method, params = msg["method"], msg.get("params", {})
        if method == "Target.getTargets":
            return _targets_reply(state["targets"])
        if method == "Target.attachToTarget":
            state["attach"].append(params["targetId"])
            return {"result": {"sessionId": "S-" + params["targetId"]}}
        if method == "Page.getFrameTree":
            return {"result": {"frameTree": {"frame": {"id": "F1", "url": "u"}}}}
        if method == "Page.createIsolatedWorld":
            return {"result": {"executionContextId": 3}}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            return _eval_reply("complete" if "readyState" in expr else READER_URL_FULL)
        return {"result": {}}

    browser = _browser(handler)
    browser.navigate(READER_URL_FULL, timeout=5)

    assert browser.target_id == "T1"
    assert state["attach"] == [], "no pointless re-attach"


def test_page_score_ranks_the_reader_first():
    reader = cdp.page_score(READER_URL_FULL)
    login = cdp.page_score("https://www.cengage.com/login")
    blank = cdp.page_score("about:blank")
    assert reader > blank > login, "reader first, login pages last"
    assert reader >= cdp.READER_MIN_SCORE


def test_is_session_gone_matches_chromes_wording():
    assert cdp.is_session_gone("Session with given id not found.")
    assert cdp.is_session_gone("No target with given id found")
    assert not cdp.is_session_gone("ReferenceError: x is not defined")
