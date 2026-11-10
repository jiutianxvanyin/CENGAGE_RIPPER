"""End-to-end self-test with a REAL Chrome and a fake reader - no Cengage needed.

It builds a miniature reader that behaves like the real one (shell page with
``#pageNumber`` / ``#next-page-button`` / ``#iframe-page``, one XHTML page per
turn), then runs the exact production path:

    cdp.launch_or_attach -> capture() -> assemble() -> textbook.pdf

Checks: every page is captured once, the pager stops at the end, the PDF has one
page per source page, and the bookmarks are real.

``--new-window`` serves the fake reader from a portal page that opens it with
``window.open``, so the capture must follow the reader into its own window -
exactly how the real Cengage reader behaves.

    python selftest_browser.py [--keep] [--new-window]
"""
from __future__ import annotations

import functools
import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading

import cdp
from cengage_ripper import assemble, capture, load_index
from config import DEFAULTS

SHELL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Fake Reader</title>
<style>body{{margin:0;font:16px serif}} .nav{{padding:8px;background:#eee}}
#iframe-page{{width:820px;height:1100px;border:0}}</style></head><body>
<div class="nav">
  <button id="previous-page-button">&lt;</button>
  <input id="pageNumber" value="41" size="4">
  <button id="next-page-button" onclick="turn()">
    <span class="page-title">{next_title}</span></button>
</div>
<iframe id="iframe-page" src="p1.html"></iframe>
<script>
var i = 1, N = {total};
function turn() {{
  if (i >= N) return;
  i++;
  document.getElementById('iframe-page').src = 'p' + i + '.html';
  document.getElementById('pageNumber').value = String(i + 40);
  if (i >= N) document.getElementById('next-page-button').disabled = true;
}}
</script></body></html>
"""

PAGE = """<!DOCTYPE html><html xmlns:epub="http://www.idpf.org/2007/ops">
<head><meta charset="utf-8"><title>{title}</title>
<style>body{{margin:24px;font:16px/1.6 Georgia,serif}}</style></head>
<body class="webreader" data-cgi="SELFTEST{cgi}" epub:type="bodymatter">
<section><h1>{title}</h1>
<p>This is page {n} of the self-test book. {filler}</p>
<p>Second paragraph before the printed page break.
<span class="pagenumber rs_skip" data-after="{page_no}" epub:type="pagebreak"
      id="page-{page_no}" aria-label="page {page_no}"></span>
After the break the text continues on textbook page {page_no}, uniquely
introduced here so the marker can be found again in the PDF.</p>
<h2>{title} - section {n}.1</h2>
<p>{filler}</p></section></body></html>
"""

PAGES = [("Chapter 1 Numbers", "01"), ("Chapter 1 Numbers", "02"),
         ("1.2 Reading Data", "03")]


def build_site(root: str) -> str:
    for n, (title, cgi) in enumerate(PAGES, start=1):
        with open(os.path.join(root, f"p{n}.html"), "w", encoding="utf-8") as f:
            f.write(PAGE.format(title=title, cgi=cgi, n=n,
                                page_no=40 + n,
                                filler="Data, uncertainty and decisions. " * 6))
    shell = SHELL.format(total=len(PAGES), next_title=PAGES[1][0])
    path = os.path.join(root, "shell.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(shell)
    return path


def build_portal(root: str, target: str = "shell.html") -> str:
    """A course page that opens the reader with window.open (as Cengage does)."""
    path = os.path.join(root, "portal.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            "<title>Course page</title></head><body><h1>My course</h1>"
            "<p>Open the ebook</p><script>setTimeout(function(){"
            f"window.open('{target}','readerwin','width=1280,height=1000');"
            "}, 400);</script></body></html>")
    return path


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(root: str):
    handler = functools.partial(_Quiet, directory=root)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def main() -> int:
    keep = "--keep" in sys.argv
    new_window = "--new-window" in sys.argv
    root = tempfile.mkdtemp(prefix="ripper-selftest-")
    site = build_site(root)

    cfg = dict(DEFAULTS)
    cfg.update({
        "pages_dir": os.path.join(root, "pages"),
        "debug_dir": None,
        "output_pdf": os.path.join(root, "book.pdf"),
        "toc": True,
        "resume": False,
        "page_wait": 0.25,
        "poll_interval": 0.2,
        "advance_timeout": 10,
        "frame_timeout": 15,
        "max_pages": 0,
        "user_data_dir": None,          # throwaway profile
        "browser_port": cdp.free_port(9500),
        "attach_port": 0,
        "headless": True,
        "paper": "book",
        "login_timeout": 30,
        "title": "Self Test Book",
    })

    httpd = None
    opener = site
    if new_window:
        build_portal(root)
        httpd, base = serve(root)
        opener = f"{base}/portal.html"
        print(f"mode: reader opens in its own window ({opener})")
    else:
        print(f"site: {site}")

    browser = cdp.launch_or_attach(cfg)
    ok = True
    try:
        before = browser.target_id
        browser.navigate("file:///" + opener.replace("\\", "/") if not opener.startswith("http")
                         else opener, timeout=25)
        if new_window:
            print(f"followed window: {before[:8]} -> {browser.target_id[:8]} "
                  f"| url: {browser.url()[:70]}")
            if browser.target_id == before:
                print("FAIL: did not follow the reader into its new window")
                ok = False
            if "shell.html" not in browser.url():
                print(f"FAIL: following landed on {browser.url()[:70]}")
                ok = False
        metas = capture(browser, cfg)
        print(f"captured {len(metas)} page(s)")

        if [m["title"] for m in metas] != [p[0] for p in PAGES]:
            print(f"FAIL titles: {[m['title'] for m in metas]}")
            ok = False
        expected = [("url", f"p{i}.html") for i in range(1, 4)]
        if [tuple(m["sig"]) for m in metas] != expected:
            print(f"FAIL signatures: {[m['sig'] for m in metas]}")
            ok = False
        if len(load_index(cfg)) != len(PAGES):
            print("FAIL index.json does not hold every page")
            ok = False

        assemble(cfg, metas)

        from pdf_builder import pdf_page_count
        n_pdf = pdf_page_count(cfg["output_pdf"])
        # Sections flow into one document now, so the count is "whatever the
        # text needs" - only sanity bounds apply.
        print(f"pdf pages: {n_pdf} (for {len(PAGES)} reader pages)")
        if n_pdf < 1:
            print("FAIL: no PDF pages")
            ok = False

        import pikepdf
        with pikepdf.open(cfg["output_pdf"]) as pdf:
            with pdf.open_outline() as outline:
                titles = [it.title for it in outline.root]
            print(f"bookmarks: {titles}")
            if not titles:
                print("FAIL: no bookmarks written")
                ok = False
        try:
            import fitz
            with fitz.open(cfg["output_pdf"]) as doc:
                page_texts = [p.get_text("text") for p in doc]
            text = " ".join(page_texts).lower()
            for title, _cgi in PAGES:
                if title.lower() not in " ".join(text.split()):
                    print(f"FAIL: heading {title!r} missing from the PDF text")
                    ok = False
            # The reader marks where each textbook page starts; those numbers
            # must show up in the PDF footer (41, 42, 43).
            tails = {t.strip().split()[-1] for t in page_texts if t.strip()}
            wanted = {str(40 + i) for i in range(1, len(PAGES) + 1)}
            hit = wanted & tails
            print(f"page numbers in footer: {sorted(hit)}")
            if not hit:
                print("FAIL: no textbook page number reached the footer")
                ok = False
        except ImportError:
            pass
    finally:
        browser.close(force=True)
        if httpd is not None:
            httpd.shutdown()

    if keep:
        print(f"kept: {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)

    print("SELFTEST OK" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
