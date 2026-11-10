"""Diagnose the live Cengage reader DOM - what to run when capture fails.

It attaches to (or launches) the capture Chrome, lets you log in by hand, then
reports:

  * every frame the DevTools protocol sees (depth, url, body class, ``data-cgi``,
    text size) and whether it holds the textbook document
  * whether the main document itself holds the book text
  * which reader controls exist (``#next-page-button``, ``#pageNumber``, ...)
  * a screenshot + full page source, saved under ``diagnose/``

Nothing is captured or converted; this is read-only reconnaissance.

Usage:
    python diagnose.py [URL]
"""
from __future__ import annotations

import json
import sys
import time

from cengage_ripper import (  # noqa: E402
    close_browser,
    dump_diagnostics,
    focus_reader_tab,
    open_browser,
    resolve_chrome,
)
from config import load_config
from reader_extract import (  # noqa: E402
    frame_inventory,
    locate_content_frame,
    probe_current_context,
)

CUSTOM_TAGS_JS = r"""
var out = [], seen = new Set();
function scan(root, depth) {
  if (!root || depth > 10) return;
  var els = root.querySelectorAll ? root.querySelectorAll('*') : [];
  for (var i = 0; i < els.length; i++) {
    var t = els[i].tagName.toLowerCase();
    if (t.indexOf('-') >= 0 && !seen.has(t)) { seen.add(t); out.push(t); }
    if (els[i].shadowRoot) scan(els[i].shadowRoot, depth + 1);
  }
}
scan(document, 0);
return out;
"""

CONTROLS_JS = r"""
function has(sel) { try { return !!document.querySelector(sel); } catch (e) { return false; } }
return {
  next: has('#next-page-button'),
  prev: has('#previous-page-button'),
  page_input: has('#pageNumber'),
  next_title: has('#next-page-button .page-title'),
  next_disabled: has('#next-page-button[disabled]'),
  nav_menu: has('#navigation-menu'),
  iframe_page: has('#iframe-page'),
  iframes_total: document.querySelectorAll('iframe').length
};
"""


def main() -> int:
    cfg = load_config()
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    url = positional[0] if positional else input(
        "Paste the ebook reader URL (or press ENTER to use the open tab): ").strip()

    browser = open_browser(cfg)
    try:
        print(f"chrome     : {resolve_chrome(cfg)}")
        print(f"mode       : port {browser.port}"
              + (f" (attach only)" if int(cfg.get("attach_port") or 0) else ""))
        print(f"profile    : {cfg.get('user_data_dir') or '(throwaway)'}")
        if url:
            print(f"opening    : {url}")
            browser.navigate(url)
        else:
            print(f"tab        : {browser.url()[:110]}")
        input("Log in if prompted and open a page of the book, "
              "then press ENTER here: ")

        print("scanning (up to 25s)...")
        print(f"active tab : {focus_reader_tab(browser)[:110]}")
        frame_id, how, info = locate_content_frame(browser, timeout=25.0)

        frames = frame_inventory(browser)
        try:
            custom = browser.eval_main(CUSTOM_TAGS_JS) or []
            controls = browser.eval_main(CONTROLS_JS) or {}
            outer = probe_current_context(browser) or {}
            title = browser.title()
            current = browser.url()
        except Exception as exc:
            print(f"  (some probes failed: {exc})")
            custom, controls, outer = [], {}, {}
            title, current = "", url

        print("\n===== REPORT (copy everything below) =====")
        print(f"url        : {current}")
        print(f"title      : {title}")
        print(f"located    : {how or 'NOT FOUND'}")
        if info:
            print(f"  body     : class={info.get('cls', '')!r} "
                  f"epub={info.get('epub', '')!r} cgi={info.get('cgi', '')[:24]!r}")
            print(f"  size     : {info.get('w')}x{info.get('h')}px  "
                  f"text={info.get('len')} chars")
        print(f"frames     : {len(frames)} (DevTools frame tree)")
        for row in frames:
            mark = "OK " if row.get("webreader") else "-- "
            print(f"  {mark}[{row['index']}] depth={row.get('depth')} "
                  f"{row.get('hint', '')[:110]}")
            print(f"       body_class={row.get('body_class', '')[:60]!r} "
                  f"text={row.get('text_len', 0)} cgi={row.get('cgi', '')[:16]!r}"
                  + (f" error={row['error']}" if row.get("error") else ""))
        print(f"outer doc  : body_class={outer.get('cls', '')[:60]!r} "
              f"text={outer.get('len')} chars")
        print(f"custom tags: {', '.join(custom[:15]) if custom else '(none)'}")
        print(f"controls   : {json.dumps(controls, ensure_ascii=False)}")

        out_dir = dump_diagnostics(browser, cfg)
        print(f"saved      : {out_dir}/summary.json, page_source.html, screen.png")
        print("===== END REPORT =====")

        if how:
            print("\nGood news: content was located by the tool, so capture should "
                  "work. If the run still failed, re-run capture now.")
        else:
            print("\nNo webreader content found. Most likely causes:\n"
                  "  1. the book page is not open yet (only the reader shell is);\n"
                  "  2. the readable page is in a frame that is still loading;\n"
                  "  3. the reader needs a click (e.g. 'Read'/'Open') first.\n"
                  "Send the REPORT block plus diagnose/screen.png.")

        # Keep the browser open a moment so the screenshot matches what you see.
        time.sleep(2)
        return 0 if how else 2
    finally:
        close_browser(browser, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
