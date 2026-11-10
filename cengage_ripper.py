"""CENGAGE_RIPPER - drive Chrome over DevTools to capture an ebook into a PDF.

Personal, offline-reading use only. Requires your own valid Cengage login.

Flow:
  1. Attach to (or launch) a Chrome on a debugging port, reusing the persistent
     profile so you log in once and never again.
  2. Open the reader URL you paste (or just use the tab that is already open).
  3. Auto-turn pages: probe the content frame -> extract its document -> click
     next (JS, then a real mouse click, then keyboard) -> wait until the page
     identity really changes -> repeat until the book ends.
  4. Render each page with headless Chrome, merge into one PDF, and write real
     PDF bookmarks harvested from each page's ``<title>`` / ``data-cgi`` / heading.

Why CDP and not Selenium: chromedriver on this machine answers every command
with ``unhandled request`` (see cdp.py), while the DevTools protocol itself is
healthy. Talking CDP directly also means the browser is ours to keep: it stays
open after the script finishes, so the next run needs no login.

Because the reader shell (nav, cookie banner, translate plugin, blur filter) lives
outside the content document, extracting it drops all the chrome automatically.

Capture is incremental and resumable: after every page the metadata index is
flushed to ``pages/index.json``, so a crash / Ctrl-C never loses captured pages.

If the reader's DOM ever changes, run ``python diagnose.py`` - it reports every
frame on the page and where (if anywhere) the webreader document actually lives.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time

import cdp
import pagenum
import pdfmap
import toc as toc_mod
from config import load_config
from pdf_builder import (
    apply_page_zoom,
    bleed_cover,
    build_book_doc,
    build_book_html,
    cover_aspect,
    cover_image,
    html_to_pdf,
    measure_page_heights,
    merge_pdfs,
    pdf_page_count,
    write_page_html,
)
from reader_extract import (
    FRAME_URL_HINTS,
    PROBE_JS,
    WEBREADER_FRAME_ID,
    clean_webreader,
    frame_inventory,
    inline_assets,
    locate_content_frame,
    pagebreak_count,
    probe_frame,
)

# Re-exported for diagnose.py and the tests.
resolve_chrome = cdp.resolve_chrome

INDEX_FILE = "index.json"
TOC_FILE = "toc.json"

# Shell state in ONE round-trip: printed page label + next-button state.
SHELL_STATE_JS = r"""
var n = document.getElementById('pageNumber');
var b = document.getElementById('next-page-button');
var out = {
  page_label: n ? String(n.value || n.textContent || '').trim() : '',
  next_present: !!b,
  next_disabled: false,
  next_title: ''
};
if (b) {
  var d = b.getAttribute('disabled');
  var ad = b.getAttribute('aria-disabled');
  var cls = String(b.className || '');
  out.next_disabled = (d !== null && d !== 'false') || ad === 'true'
                      || /\bdisabled\b/.test(cls);
  var t = b.querySelector('.page-title');
  out.next_title = t ? String(t.textContent || '').trim() : '';
}
return out;
"""


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #
def _atomic_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def index_path(cfg: dict) -> str:
    return os.path.join(cfg["pages_dir"], INDEX_FILE)


def load_index(cfg: dict) -> list[dict]:
    path = index_path(cfg)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_index(cfg: dict, metas: list[dict]) -> None:
    os.makedirs(cfg["pages_dir"], exist_ok=True)
    _atomic_json(index_path(cfg), metas)


# --------------------------------------------------------------------------- #
# Browser lifecycle
# --------------------------------------------------------------------------- #
def open_browser(cfg: dict):
    """Attach to the running capture Chrome, or launch one we keep alive."""
    return cdp.launch_or_attach(cfg)


def close_browser(browser, cfg: dict) -> None:
    """Detach by default: the browser outlives the script (and the login)."""
    if cfg.get("close_browser"):
        browser.close(force=True)
        print("  browser closed (--close-browser)")
        return
    browser.close(force=False)
    port = browser.port
    print(f"  browser left running on port {port} - next run reuses it "
          f"(no login needed)")


def browser_alive(browser) -> bool:
    try:
        browser.url()
        return True
    except Exception:
        return False


def focus_reader_tab(browser, force: bool = False) -> str:
    """Point the session at the best page target and report its URL.

    The reader is driven through ONE attached page target. If the login flow
    (or a redirect) opened a second tab, or Chrome replaced the startup
    placeholder tab, we must move with it - otherwise every command talks to a
    document the user is not looking at.
    """
    try:
        current = browser.url()
    except Exception:
        current = ""
    try:
        pages = browser.pages()          # best candidate first
    except Exception:
        return current or "?"
    if not pages:
        return current or "?"

    best = pages[0]
    better = cdp.page_score(best.get("url", "")) > cdp.page_score(current)
    if (force or better) and best["targetId"] != browser.target_id:
        try:
            browser.switch_to(best["targetId"])
            moved = browser.url()
            print(f"  switched to: {moved[:110]}")
            return moved
        except Exception:
            pass
    return current or "?"


# --------------------------------------------------------------------------- #
# Page state
# --------------------------------------------------------------------------- #
class ContentFrame:
    """Remembers which frame holds the page content.

    Re-locating from scratch on every poll would walk the frame tree and probe
    each document several times a second for the whole book, so the winning
    frame id is cached and only re-discovered when it goes stale - which is what
    happens when the reader swaps the frame on a page turn.
    """

    def __init__(self) -> None:
        self.frame_id: str | None = None
        self.how: str | None = None

    def reset(self) -> None:
        self.frame_id = None
        self.how = None

    def enter(self, browser, cfg: dict, timeout: float = 0.0):
        """Return ``(frame_id, how)``; ``how`` is None when not found."""
        if self.frame_id is not None:
            info = probe_frame(browser, self.frame_id)
            if info is not None:
                return self.frame_id, self.how
            self.reset()  # frame swapped / navigated away -> rediscover
        frame_id, how, _info = locate_content_frame(browser, timeout=timeout)
        if how:
            self.frame_id, self.how = frame_id, how
        return self.frame_id, how


def read_state(browser, cfg: dict, content: ContentFrame | None = None,
               timeout: float = 0.0) -> dict:
    """One round-trip snapshot: printed page label, next-button state, page identity."""
    state = {
        "page_label": "",
        "next_present": False,
        "next_enabled": False,
        "next_title": "",
        "title": "",
        "cgi": "",
        "frame_url": "",
        "page_size": None,
        "how": None,
    }

    try:
        shell = browser.eval_main(SHELL_STATE_JS) or {}
    except Exception:
        shell = {}
    state["page_label"] = str(shell.get("page_label") or "").strip()
    state["next_present"] = bool(shell.get("next_present"))
    state["next_enabled"] = bool(shell.get("next_present")) and \
        not bool(shell.get("next_disabled"))
    state["next_title"] = str(shell.get("next_title") or "").strip()

    if content is not None:
        frame_id, how = content.enter(browser, cfg, timeout=timeout)
        state["how"] = how
        if how:
            info = probe_frame(browser, frame_id) or {}
            state["title"] = str(info.get("title") or "").strip()
            state["cgi"] = str(info.get("cgi") or "").strip()
            state["frame_url"] = str(info.get("url") or "").strip()
            w = int(info.get("w") or 0)
            h = int(info.get("h") or 0)
            if 0 < w <= 5000 and 0 < h <= 30000:
                state["page_size"] = [w, h]

    return state


def page_key(url: str) -> str:
    """Stable identity of the content document inside ``url``.

    Cengage serves one XHTML file per "page", so ``…/content/copyright.html``
    is the page. The path *after* ``/content/`` is the part that survives
    project-version changes; falling back to the last path segment keeps this
    working for readers that serve pages from a flat directory.
    """
    u = (url or "").split("#")[0].split("?")[0].strip()
    if not u:
        return ""
    if "/content/" in u:
        return u.split("/content/", 1)[1]
    return u.rsplit("/", 1)[-1]


def signature(state: dict) -> tuple:
    """Stable per-page identity, most reliable source first.

    ``data-cgi`` looks like an id but is **not** per page: in a real Cengage
    title every page (cover, titlepage, copyright, …) carries the same value,
    which made the pager believe nothing ever changed. The content document's
    own URL is genuinely per page, so it wins; ``data-cgi`` is kept as a
    fallback for readers that do mint a per-page id.
    """
    key = page_key(state.get("frame_url") or "")
    if key:
        return ("url", key)
    if state.get("cgi"):
        return ("cgi", state["cgi"])
    return ("title", f"{state.get('title', '')}|{state.get('page_label', '')}")


def wait_for_content(browser, content: ContentFrame, cfg: dict,
                     timeout: float | None = None) -> bool:
    """Wait until the content document is reachable. True when found.

    Keeps looking for the whole window because the reader lives in its OWN
    window: it may appear late, and it will not be the tab we navigated.
    """
    limit = cfg["frame_timeout"] if timeout is None else timeout
    deadline = time.time() + max(limit, 0.1)
    refocus_at = 0.0
    while True:
        left = deadline - time.time()
        _frame_id, how = content.enter(
            browser, cfg, timeout=min(cfg["frame_timeout"], max(1.0, left)))
        if how:
            print(f"  content located: {how}")
            return True
        if time.time() >= deadline:
            return False
        # The reader window may have just opened / finished loading.
        if time.time() >= refocus_at:
            refocus_at = time.time() + 3.0
            focus_reader_tab(browser, force=True)
            content.reset()
            time.sleep(0.3)


def goto_next_page(browser, cfg: dict, content: ContentFrame | None = None) -> str:
    """Advance one page. Returns how it succeeded ('' when it could not)."""
    # 1. JS click, then real mouse click at the element's centre
    try:
        if browser.click("#next-page-button"):
            return "click"
    except Exception:
        pass

    # 2. keyboard, browser-wide (the reader listens for ArrowRight)
    try:
        if browser.press("ArrowRight"):
            return "key"
    except Exception:
        pass

    # 3. synthetic key event inside the content frame
    if content is not None and content.frame_id:
        try:
            browser.eval(content.frame_id, r"""
var e = new KeyboardEvent('keydown', {key: 'ArrowRight', code: 'ArrowRight',
  keyCode: 39, which: 39, bubbles: true});
(document.activeElement || document.body).dispatchEvent(e);
return true;
""")
            return "key-frame"
        except Exception:
            pass
    return ""


def goto_start(browser, cfg: dict, content: ContentFrame | None = None) -> bool:
    """Put the reader on the book's first page before capturing.

    A capture starts at whatever page the reader happens to show, which is why
    the first PDF page used to be a page from the middle of chapter 1. Two
    routes, cheapest first: rewrite the reader URL to the cover, then use the
    reader's own contents panel, then walk back.
    """
    current = browser.url() or ""
    match = re.search(r"/content-[^/?#]*", current)
    if match and "cover" not in match.group(0):
        target = current[:match.start()] + "/content-cover" + current[match.end():]
        print("  going back to the cover (reader URL) ...")
        try:
            browser.navigate(target)
            time.sleep(cfg["page_wait"])
            if content is not None:
                content.reset()
                return wait_for_content(browser, content, cfg)
            return True
        except Exception as exc:
            print(f"  (could not navigate to the cover: {str(exc)[:100]})")

    for selector in ('#toc-tree li[data-id="content-cover"] .p-tree-node-content',
                     '#toc-tree li[data-id="content-cover"]',
                     '#toc-tree li[role="treeitem"] .p-tree-node-content',
                     '#toc-tree li[role="treeitem"]'):
        try:
            clicked = browser.click(selector)
        except Exception:
            clicked = False
        if not clicked:
            continue
        time.sleep(cfg["page_wait"] * 1.5)
        url = browser.url() or ""
        if "-cover" in url:
            print("  jumped to the cover through the contents panel")
            if content is not None:
                content.reset()
            return True
        print(f"  contents panel click did not move ({url[:70]})")
        break

    print("  walking back to the first page (this can take a while)...")
    for _ in range(600):
        try:
            state = browser.eval_main(
                "var e=document.getElementById('previous-page-button');"
                "if(!e) return 'gone';"
                "return (e.getAttribute('aria-disabled')==='true'"
                " || /disabled/.test(e.className||'')) ? 'off' : 'on';")
        except Exception:
            state = "on"
        if state in ("off", "gone"):
            break
        try:
            if not browser.click("#previous-page-button"):
                break
        except Exception:
            break
        time.sleep(max(cfg["page_wait"] * 0.5, 0.2))
    if content is not None:
        content.reset()
    return True


def wait_change(browser, cfg: dict, content: ContentFrame, prev_sig: tuple,
                timeout: float | None = None):
    """Block until the page identity changes. Returns the new state, or None."""
    deadline = time.time() + (timeout or cfg["advance_timeout"])
    while time.time() < deadline:
        try:
            state = read_state(browser, cfg, content)
        except Exception:
            time.sleep(cfg["poll_interval"])
            continue
        if signature(state) != prev_sig:
            return state
        time.sleep(cfg["poll_interval"])
    return None


# --------------------------------------------------------------------------- #
# Capture loop (automatic page turning)
# --------------------------------------------------------------------------- #
_CHAPTER_RE = re.compile(r"chapter\s*(\d+)", re.I)


def hits_boundary(state: dict, needle: str) -> bool:
    """Is this reader page the one ``--until`` asked us to stop *before*?

    Matched against the document title and the reader URL, because neither alone
    is enough: front-matter pages often have no title, and the reader's
    ``<title>`` drops the chapter prefix (``Chapter 1. Data and Statistics``
    comes through as just ``Data and Statistics``). So ``--until 'Chapter 1'``
    also matches the URL slug segment ``bd_ch_01_opener`` - same chapter, and
    the slug is what the reader keys its pages by.
    """
    text = (needle or "").strip().lower()
    if not text:
        return False
    haystack = [str(state.get("title") or "").lower(),
                str(state.get("frame_url") or "").lower()]
    if any(text in h for h in haystack):
        return True
    match = _CHAPTER_RE.search(text)
    if match:
        slug = re.compile(rf"(?:^|[^a-z0-9])ch[_\-]?0*{int(match.group(1))}(?!\d)")
        if any(slug.search(h) for h in haystack):
            return True
    return False


def capture(browser, cfg: dict) -> list[dict]:
    pdir = cfg["pages_dir"]
    os.makedirs(pdir, exist_ok=True)
    debug_dir = cfg.get("debug_dir")
    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)

    if cfg.get("refresh"):
        metas = []
        print("  refreshing: every page will be extracted again (in place)")
    elif cfg.get("resume"):
        metas = load_index(cfg)
        if metas:
            print(f"  resuming: {len(metas)} page(s) already captured "
                  f"-> re-walking and skipping them")
    else:
        metas = []
        rotate_old_capture(cfg)

    seen = {tuple(m.get("sig") or []): m for m in metas if m.get("sig")}
    content = ContentFrame()
    base_url = browser.url()
    last_sig = None
    skipped = 0

    while True:
        try:
            state = read_state(browser, cfg, content)
            if not state["how"]:
                print("\n  lost the content document - rediscovering...")
                if not wait_for_content(browser, content, cfg):
                    dump_diagnostics(browser, cfg)
                    print("\n  could not find the content document -> stopping")
                    break
                state = read_state(browser, cfg, content)
        except cdp.CdpError as exc:
            print(f"  browser error while reading page: {exc}")
            dump_diagnostics(browser, cfg)
            break

        sig = signature(state)
        if last_sig is not None and sig == last_sig:
            print("  page did not advance -> end of book (or reader is stuck)")
            break
        last_sig = sig

        if hits_boundary(state, cfg.get("until") or ""):
            print(f"\n  reached {cfg['until']!r} "
                  f"({state.get('title') or state.get('frame_url')}) "
                  f"-> stopping before it")
            break

        if sig in seen:
            skipped += 1
            print(f"\r  skipping already-captured page {skipped}", end="", flush=True)
        else:
            frame_id, how = content.enter(browser, cfg, timeout=cfg["frame_timeout"])
            if not how:
                dump_diagnostics(browser, cfg)
                print("\n  could not re-open the content document -> stopping")
                break

            if cfg.get("inline_assets"):
                stats = inline_assets(browser, frame_id)
                if stats.get("failed"):
                    print(f"\r  WARN: {stats['failed']}/{stats.get('imgs', 0)} "
                          f"image(s) could not be inlined "
                          f"(e.g. {stats.get('failedSrc', ['?'])[0][:60]})")

            raw = browser.html(frame_id)

            cleaned = clean_webreader(raw)
            meta = toc_mod.page_meta(cleaned, page_number=state.get("page_label"))
            index = len(metas) + 1
            meta.update({
                "page": index,
                "file": f"page_{index:04d}.html",
                "sig": list(sig),
                "page_size": state.get("page_size"),
                "frame": how,
                "next_title": state.get("next_title") or "",
                "url": base_url,
                # Real pagination: the reader marks where each printed page
                # starts, so the count tells us how many textbook pages this
                # section holds (the print step uses the markers themselves).
                "breaks": pagebreak_count(cleaned),
            })
            write_page_html(cleaned, os.path.join(pdir, meta["file"]), base_url=base_url)
            if debug_dir:
                with open(os.path.join(debug_dir, meta["file"]), "w",
                          encoding="utf-8") as f:
                    f.write(raw)
            metas.append(meta)
            save_index(cfg, metas)  # flush after every page -> crash-safe
            seen[sig] = meta
            label = f"  (p.{state['page_label']})" if state.get("page_label") else ""
            print(f"\r  [{index}] {meta['title'][:64]!r}{label}".ljust(90),
                  flush=True)

        if cfg["max_pages"] and len(metas) >= cfg["max_pages"]:
            print(f"\n  reached max_pages={cfg['max_pages']} -> stopping")
            break
        if not state["next_present"]:
            print("\n  no next-page control -> end of book")
            break
        if not state["next_enabled"]:
            print("\n  next-page control disabled -> end of book")
            break

        turned = goto_next_page(browser, cfg, content)
        if not turned:
            print("\n  could not turn the page -> stopping")
            break
        if cfg["page_wait"]:
            time.sleep(cfg["page_wait"])
        if wait_change(browser, cfg, content, sig) is None:
            print(f"\n  page never changed within {cfg['advance_timeout']}s "
                  f"(turned via {turned}) -> stopping")
            break

    print()
    return metas


def rotate_old_capture(cfg: dict) -> None:
    """Move a previous capture aside instead of deleting it (nothing is lost)."""
    pdir = cfg["pages_dir"]
    if not os.path.isdir(pdir):
        return
    leftovers = [n for n in os.listdir(pdir) if n.startswith("page_")]
    if not leftovers:
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{pdir}.bak-{stamp}"
    os.makedirs(backup, exist_ok=True)
    for name in leftovers:
        shutil.move(os.path.join(pdir, name), os.path.join(backup, name))
    print(f"  previous capture moved to {backup}")


# --------------------------------------------------------------------------- #
# Diagnostics (what to send me when a run fails)
# --------------------------------------------------------------------------- #
def dump_diagnostics(browser, cfg: dict) -> str:
    """Write everything needed to fix a broken run into ``diagnose/``."""
    out_dir = cfg.get("diagnose_dir") or "diagnose"
    os.makedirs(out_dir, exist_ok=True)
    report: dict = {"url": None, "title": None, "frames": [], "targets": []}

    try:
        report["url"] = browser.url()
        report["title"] = browser.title()
    except Exception as exc:
        report["error"] = str(exc)[:300]

    try:
        report["targets"] = [
            {"type": "page", "url": (t.get("url") or "")[:200],
             "title": (t.get("title") or "")[:100],
             "score": cdp.page_score(t.get("url", "")),
             "driven": t.get("targetId") == browser.target_id}
            for t in browser.pages()]
    except Exception:
        pass

    try:
        report["frames"] = frame_inventory(browser)
    except Exception as exc:
        report["frame_error"] = str(exc)[:200]

    try:
        with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    try:
        main = browser.main_frame_id()
        with open(os.path.join(out_dir, "page_source.html"), "w",
                  encoding="utf-8") as f:
            f.write(browser.html(main))
    except Exception:
        pass

    browser.screenshot(os.path.join(out_dir, "screen.png"))

    print(f"\n  diagnostics written to {os.path.abspath(out_dir)}/")
    print(f"    url      : {report['url']}")
    print(f"    title    : {report['title']}")
    if report.get("targets"):
        print(f"    tabs     : {len(report['targets'])} page(s) - the reader "
              f"usually has its own window")
        for t in report["targets"]:
            print(f"      {'*' if t.get('driven') else ' '} score={t.get('score')} "
                  f"{(t.get('url') or '')[:90]}")
    if report["frames"]:
        print(f"    frames   : {len(report['frames'])} found")
        for row in report["frames"]:
            mark = "OK " if row.get("webreader") else "   "
            print(f"      {mark}[{row['index']}] depth={row.get('depth')} "
                  f"{(row.get('body_class') or '')[:28]!r} "
                  f"text={row.get('text_len', 0)} hint={(row.get('hint') or '')[:80]}")
    else:
        print("    frames   : none found")
    if report.get("error"):
        print(f"    error    : {report['error']}")
    return out_dir


# --------------------------------------------------------------------------- #
# Assembly (PDF + bookmarks)
# --------------------------------------------------------------------------- #
def out_path(cfg: dict, key: str = "output_pdf") -> str:
    """Absolute path of an output file (paths are relative to the cwd)."""
    p = cfg.get(key) or ""
    return p if not p else os.path.abspath(p)


def book_spec(cfg: dict, aspect: float | None = None):
    """(w, h, pad_x, pad_top, pad_bottom) for the continuous "book" layout.

    ``aspect`` (height / width) comes from the cover scan: the cover *is* one
    page, so its pixel ratio is the paper's. That keeps the cover edge-to-edge
    and gives the text pages the proportions of the printed book instead of the
    1:2 strip a bare 752x1500 produces.
    """
    bp = list(cfg.get("book_page") or [752, 963, 16, 12, 26])
    while len(bp) < 5:
        bp.append((16, 12, 26)[len(bp) - 2] if len(bp) > 2 else 16)
    width, height = int(bp[0]), int(bp[1])
    if aspect and str(cfg.get("paper_ratio") or "cover").lower() == "cover":
        height = int(round(width * float(aspect)))
    return (width, height, int(bp[2]), int(bp[3]), int(bp[4]))


def paper_spec(cfg: dict, meta: dict | None = None):
    """(page_size, book) for pdf_builder, from ``--paper``."""
    mode = str(cfg.get("paper") or "book").lower()
    if mode == "a4":
        return None, None                 # plain A4 from print_css
    if mode == "book":
        return None, book_spec(cfg)
    return (meta or {}).get("page_size"), None   # "auto": measured frame


def describe_size(size) -> str:
    if not size:
        return "?"
    w, h = int(size[0]), int(size[1])
    return f"{w}x{h}px ({w * 0.75:.0f}x{h * 0.75:.0f}pt)"


def assemble(cfg: dict, metas: list[dict]) -> None:
    pdir = cfg["pages_dir"]
    chrome = resolve_chrome(cfg)
    title = cfg.get("title") or "eTextbook"
    pdf_out = out_path(cfg)

    if not metas:
        print("  nothing captured -> no output")
        return
    os.makedirs(os.path.dirname(pdf_out) or ".", exist_ok=True)

    def page_html(meta: dict) -> str:
        with open(os.path.join(pdir, meta["file"]), encoding="utf-8") as f:
            return f.read()

    entries: list[dict] = []

    if cfg.get("split_print"):
        entries = _print_per_page(cfg, metas, page_html, chrome, pdf_out, title)
    else:
        entries = _print_stream(cfg, metas, page_html, chrome, pdf_out, title)

    if cfg.get("output_html"):
        html_toc = entries or toc_mod.build_toc(
            metas, per_page=bool(cfg.get("toc_per_page")),
            max_level=int(cfg.get("toc_max_level", 3)))
        html_out = out_path(cfg, "output_html")
        os.makedirs(os.path.dirname(html_out) or ".", exist_ok=True)
        with open(html_out, "w", encoding="utf-8") as f:
            f.write(build_book_html([page_html(m) for m in metas],
                                    title=title, toc_entries=html_toc,
                                    stream=not cfg.get("split_print")))
        print(f"  wrote {html_out}")

    _print_result(cfg, pdf_out, len(metas))


def _write_toc(cfg: dict, metas: list[dict], pdf_out: str, title: str) -> list[dict]:
    """Build the outline from meta['pdf_page'] and write it into the PDF."""
    if not cfg.get("toc"):
        return []
    entries = toc_mod.build_toc(
        metas,
        per_page=bool(cfg.get("toc_per_page")),
        max_level=int(cfg.get("toc_max_level", 3)),
    )
    _atomic_json(os.path.join(cfg["pages_dir"], TOC_FILE), entries)
    if not entries:
        print("  no titles found -> no bookmarks")
        return []
    try:
        top = toc_mod.apply_outline(pdf_out, entries, book_title=title)
        print(f"  bookmarks: {len(entries)} entries ({top} top-level)")
    except Exception as exc:
        print(f"  WARN: could not write bookmarks ({exc}); "
              f"toc.json still saved for --rebuild")
    return entries


def _section_starts(metas: list[dict]) -> list[bool]:
    """Which reader pages must begin a new sheet.

    Front matter (cover, title page, copyright, preface...) hands over no usable
    pagebreak markers, so its section boundaries are the only page boundaries we
    can trust. Body text does have markers, so it keeps flowing continuously -
    that is what stops a two-paragraph section from becoming a half-empty page.
    """
    out = []
    for i, meta in enumerate(metas):
        kind = str(meta.get("epub_type") or "").lower()
        out.append(i > 0 and not kind.startswith("body"))
    return out


def _page_id(meta: dict, index: int) -> str:
    """Slug used for in-document section anchors (taken from the page's URL)."""
    sig = meta.get("sig") or []
    if len(sig) == 2 and sig[0] == "url":
        slug = str(sig[1]).rsplit("/", 1)[-1]
        if slug.endswith(".html"):
            slug = slug[:-5]
        if slug:
            return slug
    return f"p{index}"


def _page_anchors(metas: list[dict], pages: list[int]) -> list[tuple[int, str]]:
    """(PDF page, printed page label) for every section the reader labelled.

    ``page_label`` is what the reader's page box showed while capturing that
    section - the real book page (``i``, ``xxv``, ``1``, ``6``…). Only some
    sections carry one: the box is empty when the page did not change.
    """
    out: list[tuple[int, str]] = []
    for i, meta in enumerate(metas):
        label = str(meta.get("page_label") or "").strip()
        if not label or i >= len(pages) or not pages[i]:
            continue
        out.append((int(pages[i]), label))
    return out


def _spilled(breaks: list[dict], located: list[int | None]) -> list[int]:
    """Breaks whose textbook page needed more than one PDF page."""
    out: list[int] = []
    prev = prev_num = None
    for k, (brk, page) in enumerate(zip(breaks, located)):
        if page is None:
            continue
        num = brk.get("num")
        if prev is not None and num is not None and prev_num is not None:
            step = page - prev
            expected = max(num - prev_num, 1)
            if step > expected:
                out.append(k)
        prev = page
        if num is not None:
            prev_num = num
    return out


def _label_number(brk: dict) -> int | None:
    """Printed arabic page number of a marker, even one the reader parked.

    A parked marker is not allowed to *print* its page (its position is a
    guess), but the page itself exists in the book: its number still has to be
    accounted for, otherwise the numbering of the sheets around it looks like a
    hole and the planner pads the book with blank pages.
    """
    num = brk.get("num")
    if num is not None:
        return num
    label = (brk.get("label") or "").strip()
    return int(label) if label.isdigit() else None


_ANCHOR_TEXT = re.compile(r"\bG\d{4}\b")


def _page_text(el, limit: int = 160) -> str:
    """First readable words of a page wrapper (used to find it in the PDF).

    The wrapper opens with the invisible ``G####`` marker, which must not leak
    into a snippet: the PDF text search would then look for "G0042 The first…".
    """
    text = " ".join(el.get_text(" ", strip=True).split())
    return _ANCHOR_TEXT.sub("", text).strip()[:limit]


def _group_starts(pdf_out: str, groups: list[dict]) -> list[int | None]:
    """1-based PDF page where each page wrapper starts, read from the PDF.

    Every wrapper begins a sheet, so knowing where they land is the cheapest
    possible check on the fitting: a wrapper that owns two sheets needs a
    smaller zoom.

    The invisible ``G####`` marker at the top of each wrapper makes this exact.
    Searching for the wrapper's opening prose is only a fallback for PDFs
    printed before the marker existed: on the 734-page edition that search
    failed for 703 of 1035 wrappers, which is what collapsed the footer
    numbering into "352 (cont.)" for hundreds of sheets.
    """
    if not groups:
        return []
    try:
        anchors = pdfmap.locate_anchors(pdf_out)
    except Exception:
        anchors = {}
    if anchors:
        found = [anchors.get(i) for i in range(len(groups))]
        if sum(1 for s in found if s) >= max(1, int(len(groups) * 0.8)):
            return found
    snippets = [_page_text(g["el"], 120) for g in groups]
    try:
        return pdfmap.locate_snippets(pdf_out, snippets)
    except Exception:
        return []


def _repair_starts(starts: list[int | None], total: int) -> list[int | None]:
    """Give a wrapper that could not be found in the text layer its own sheet.

    A wrapper whose opening words never reach the PDF text (a page that is all
    artwork, or one whose first line the print pushed onto the next sheet) has
    no located start, and the numbering used to drop it - which deleted its page
    number from the whole book (page 26 of this edition). If the sheets between
    its neighbours leave room, the wrapper owns the first of them.
    """
    if not starts:
        return starts
    out = list(starts)
    for i, start in enumerate(out):
        if start:
            continue
        prev = next((out[j] for j in range(i - 1, -1, -1) if out[j]), 0)
        nxt = next((out[j] for j in range(i + 1, len(out)) if out[j]), total + 1)
        if nxt - prev > 1:
            out[i] = prev + 1
    return out


def _print_stream(cfg: dict, metas: list[dict], page_html, chrome: str,
                  pdf_out: str, title: str) -> list[dict]:
    """One continuous document -> one Chrome print -> bookmarks read back.

    The reader tells us where every textbook page begins (``epub:type=
    "pagebreak"``), so the print inserts a real break there: one PDF page holds
    exactly one textbook page. That is what lets ``pagenum`` bind the printed
    numbers to the PDF's own page numbers instead of drifting away from them.
    """
    ids = [_page_id(m, i) for i, m in enumerate(metas, start=1)]
    breaks: list[dict] = []
    link_stats: dict = {}
    book_html = os.path.join(cfg["pages_dir"], "book.html")
    doc = build_book_doc([page_html(m) for m in metas], title=title,
                         stream=True, page_ids=ids, breaks=breaks,
                         link_stats=link_stats,
                         new_pages=_section_starts(metas))
    soup, groups = doc["soup"], doc["groups"]
    with open(book_html, "w", encoding="utf-8") as f:
        f.write(str(soup))
    # A "phantom" marker is one the reader could not place: a whole run of them
    # gets parked in one block (front matter: data-after="from i to xxi"), so the
    # page exists in the book but no content does. Printing a sheet for those
    # would leave the book full of blank pages.
    real = [b for b in breaks
            if not b.get("phantom") and b.get("num") is not None]
    # Everything the reader named but could not place still exists in the book:
    # 6 and 7 open no sheet of their own (their marker was parked at the end of
    # a section), yet the sheets between page 5 and page 8 belong to them.
    phantoms = [_label_number(b) for b in breaks if b.get("num") is None] + doc["skip"]
    numbered = len([b for b in real if b.get("num") is not None])
    parked = (f", {len(phantoms)} parked marker(s) with no content"
              if phantoms else "")
    print(f"  printing one continuous document ({len(metas)} reader pages, "
          f"{len(real)} page breaks / {numbered} numbered{parked})...")
    if groups:
        print(f"  pages: {sum(1 for g in groups if g['fit'])} cut by a marker "
              f"(one sheet each), {sum(1 for g in groups if not g['fit'])} "
              f"flowing (the reader parked those markers)")
    if link_stats.get("internal"):
        extra = (f", {link_stats['dropped']} web-only link(s) unlinked"
                 if link_stats.get("dropped") else "")
        print(f"  links: {link_stats['internal']} in-book anchor(s) -> "
              f"internal jumps{extra}")

    aspect = cover_aspect(page_html(metas[0])) if metas else None
    if aspect and str(cfg.get("paper_ratio") or "cover").lower() == "cover":
        print(f"  paper: following the shape of the cover page ({aspect:.3f} h/w)")
    w, h, pad_x, pad_top, pad_bottom = book_spec(cfg, aspect)
    if not real:
        # Nothing to bind: the markers are missing (an old capture). Do not turn
        # the book into tall pages for no reason.
        h = min(h, 1180)
    # Page numbers come first: a textbook page that spills onto a second sheet
    # makes the footer lie. Shrinking the text is the honest fix - growing the
    # sheet just made every page a narrow ribbon and moved the problem.
    book_mode = str(cfg.get("paper") or "book").lower() == "book"
    usable = max(h - pad_top - pad_bottom, 200)

    # Fit every marker page to one sheet. Measured, not guessed: each textbook
    # page wrapper is laid out at the printed column width and its height read
    # back, then that page alone gets the zoom that makes it exactly one sheet
    # tall. Small type on one page beats a page number that lies.
    zoom: dict[int, float] = {}
    base = 1.0
    if groups and book_mode:
        try:
            heights = measure_page_heights(
                book_html, chrome, width=w - 2 * pad_x,
                book=(w, h, pad_x, pad_top, pad_bottom))
        except Exception as exc:
            print(f"  WARN: could not measure the pages ({exc}); "
                  f"printing every page at 100%")
            heights = []
        if heights:
            floor = float(cfg.get("fit_floor") or 0.72)
            need: dict[int, float] = {}
            for i, g in enumerate(groups):
                if not g["fit"] or i >= len(heights) or heights[i] <= 0:
                    continue
                # ``zoom`` reflows as well as scales: the column gets wider in
                # layout units by 1/z while the type shrinks by z, so the height
                # falls with z squared. Hence the square root - a linear ratio
                # would shrink every page far more than it needs.
                need[i] = (usable * 0.985 / heights[i]) ** 0.5
            if need:
                # One size for the whole book - a reader should not see the type
                # change from page to page. The size is the one that lets the
                # bulk of the pages fit (the 15th percentile of "what this page
                # would need"), floored at a legible minimum. The handful of
                # pages that still run over print as ``N (cont.)``, which is the
                # honest trade: uniform type, a few continuation sheets.
                sample = sorted(need.values())
                pick = sample[max(0, min(len(sample) - 1,
                                         int(len(sample) * 0.15) - 1))]
                base = max(floor, min(1.0, pick))
                zoom = {i: base for i in range(len(groups))}
                apply_page_zoom(groups, zoom)
                fitted = sum(1 for v in need.values() if v >= base * 0.995)
                print(f"  fit: one size for the whole book ({base:.0%}); "
                      f"{fitted}/{len(need)} page(s) fit a single sheet at it"
                      + (", the rest print as (cont.)"
                         if fitted < len(need) else ""))

    located: list[int | None] = []
    total = 0
    scale = 1.0
    with open(book_html, "w", encoding="utf-8") as f:
        f.write(str(soup))
    html_to_pdf(book_html, pdf_out, chrome,
                book=(w, h, pad_x, pad_top, pad_bottom), scale=scale)
    total = pdf_page_count(pdf_out)
    cfg["_book_size"] = (w, h)
    cfg["_text_scale"] = scale

    # Where every page wrapper landed, and which sheets are the unnumbered
    # inserts (the R appendices). Read from the print, because that is the only
    # place the truth about sheets lives.
    starts = _group_starts(pdf_out, groups) if groups else []
    starts = _repair_starts(starts, total)
    page_entries = [i for i, g in enumerate(groups)
                    if (g["pb"] or "").isdigit() and starts and starts[i]]
    plan_breaks = [{"num": int(groups[i]["pb"]), "label": groups[i]["pb"],
                    "style": "D",
                    "snippet": _page_text(groups[i]["el"])} for i in page_entries]
    plan_pages = [starts[i] for i in page_entries]
    insert_sheets: set[int] = set()
    for i, g in enumerate(groups):
        if not g["insert"] or not starts or not starts[i]:
            continue
        # The insert ends where the next *numbered* page begins. Walking to the
        # next wrapper instead stopped early: a flowing wrapper's snippet is
        # short, it was located inside the appendix, and the rest of the
        # appendix then wore a page number (sheet 64 was stamped "24").
        end = next((starts[j] for j in range(i + 1, len(starts))
                    if starts[j] and (groups[j]["pb"] or "").isdigit()),
                   total + 1)
        insert_sheets.update(range(starts[i], end))

    if aspect and cfg.get("cover_bleed", True):
        if bleed_cover(pdf_out, cover_image(page_html(metas[0]))):
            print("  cover: painted over the whole first page (no white frame)")

    info: dict = {}
    if plan_breaks:
        info = pagenum.plan(plan_breaks, plan_pages, total, skip=phantoms)
        for msg in info["warnings"][:5]:
            print(f"  note: {msg}")
        if info["blanks"]:
            pagenum.insert_blanks(pdf_out, info["blanks"])
            total = pdf_page_count(pdf_out)

    # Section starts are located once and drive both the bookmarks and the page
    # numbering: the reader prints the real book page next to every section in
    # its own page box, and each capture records it as ``page_label``. That is
    # the only honest anchor - unlike the pagebreak markers, which the reader
    # parks in blocks and often cannot place.
    pages: list[int]
    misses: list[int] = []
    # The wrapper map is the first source: every printed page knows which reader
    # section it was cut from (``id="page-N"``), and the wrapper's sheet is
    # already known from the page numbering. Heading search alone put "Content",
    # "Glossary" and the two appendices on the wrong sheet - a heading like
    # "Content" matches running text, and one miss drags every later bookmark
    # with it, because the search only looks forward.
    by_reader: dict[int, int] = {}
    on_page: set[int] = set()
    if groups and starts:
        for i, g in enumerate(groups):
            if not starts[i]:
                continue
            for art in g["el"].find_all(attrs={"data-src": True}):
                n = int(art["data-src"]) - 1
                # A reader section is often cut in two by a pagebreak: the tail
                # of the previous book page, then the page the section opens.
                # The bookmark belongs to the page, not to the tail, so the
                # first wrapper that starts a numbered page wins.
                if n not in by_reader or (g["pb"] and n not in on_page):
                    by_reader[n] = starts[i]
                    if g["pb"]:
                        on_page.add(n)
    try:
        typed = pdfmap.locate_section_pages(pdf_out, metas, misses=misses)
        how = "located by heading in the PDF text layer"
    except Exception as exc:                     # PyMuPDF missing / unlucky PDF
        typed = pdfmap.estimate_section_pages(metas, total)
        how = f"spread by text volume ({exc})"
    pages = []
    for n in range(len(metas)):
        page = by_reader.get(n) or (typed[n] if n < len(typed) else None)
        if page is None:
            page = pages[-1] if pages else 1
            misses.append(n + 1)
        pages.append(int(page))
    if by_reader:
        how = "read from the page wrappers (same map as the page numbers)"
        misses = [n + 1 for n in range(len(metas)) if n not in by_reader]
        # The reader prints the book's own page number beside some headings.
        # Where it does, that number names the sheet exactly - the wrapper map
        # cannot, because a section with parked markers shares its wrapper with
        # its neighbours. The heading search below then has the last word, so a
        # snap that overshoots is corrected rather than trusted.
        sheet_of: dict[int, int] = {}
        for entry in info.get("map") or []:
            if entry.get("book_page") is not None and not info.get("blanks"):
                sheet_of.setdefault(int(entry["book_page"]),
                                    int(entry["pdf_page"]))
        snapped = 0
        for n, meta in enumerate(metas):
            label = str(meta.get("page_label") or "").strip()
            if not label.isdigit() or n == 0:
                continue
            sheet = sheet_of.get(int(label))
            if sheet and sheet != pages[n]:
                pages[n] = sheet
                snapped += 1
        if snapped:
            print(f"  bookmark targets: {snapped} section(s) taken from the "
                  f"book's own page number")
        # A wrapper's sheet is where the section's *text* starts, which is not
        # always where its heading is (the tail of the previous book page can sit
        # above it) and two sections can share one wrapper. Each heading is
        # looked for again inside its own window - see pdfmap.refine_sections.
        try:
            pages = pdfmap.refine_sections(pdf_out, metas, pages, total)
        except Exception:
            pass
    if pages:
        # The first captured section always begins the PDF, so its bookmark is
        # page 1 by definition. Without this the cover points at page 2: its
        # title is artwork, so the heading search matches the title page instead.
        pages[0] = 1

    # The reader's page box is the ground truth even when the capture has no
    # pagebreak markers at all: an older capture of this very book carries 27
    # labels and zero markers, and numbering it 1..199 (PDF order) is what the
    # user rightly called "not the book's own page divisions".
    anchors = (_page_anchors(metas, pages) if pages and not plan_breaks else [])
    labels = pagenum.plan_labels(anchors, total) if anchors else {}
    if labels.get("labels"):
        info["labels"] = labels["labels"]
        info["segments"] = labels["segments"]
        info["anchors"] = labels["anchors"]
        print(f"  page numbers follow the book's own labels "
              f"({len(labels['anchors'])} anchor(s): "
              f"{', '.join(t for _, t in labels['anchors'][:4])}"
              f"{'…' if len(labels['anchors']) > 4 else ''})")

    if plan_breaks or labels.get("labels"):
        if cfg.get("page_numbers"):
            # A printed book never numbers its cover; here it is artwork, so a
            # grey numeral would be both invisible and wrong. The loose inserts
            # wear no number either - the book does not give them one.
            quiet = set(insert_sheets)
            if aspect:
                quiet.add(1)
            stamped = pagenum.stamp_numbers(pdf_out, info, skip=quiet)
            labelled = pagenum.write_page_labels(pdf_out, info)
            print(f"  page numbers: {stamped} page(s) stamped in the footer, "
                  f"labels {'written' if labelled else 'not written'}")
        print(f"  binding: {pagenum.summary(info)}")
        cfg["_page_binding"] = pagenum.summary(info)
    elif not plan_breaks:
        if cfg.get("page_numbers"):
            # No markers anywhere: the file still needs *some* numbering, so
            # every sheet gets its PDF page number. Said plainly, because these
            # are not textbook pages.
            n = pagenum.stamp_numbers(pdf_out, {"sheet_only": True})
            print(f"  page numbers: {n} sheet number(s) stamped - PDF page "
                  f"numbers, not textbook pages (re-capture with --refresh for "
                  f"the real ones)")
            cfg["_page_binding"] = "PDF page numbers only (no pagebreak markers)"
        print("  note: no pagebreak markers in the capture -> PDF page numbers "
              "are the only numbering (re-capture with --refresh to get them)")

    pdfmap.apply_pages(metas, pages)
    print(f"  bookmark targets: {len(pages)} sections, {how}")

    if misses:
        shown = ", ".join(str(n) for n in misses[:8])
        print(f"  WARN: {len(misses)} section(s) had no findable heading "
              f"(reader page {shown}{'...' if len(misses) > 8 else ''}); "
              f"their bookmark sits on the previous section's page")

    backward = [m["page"] for i, m in enumerate(metas)
                if i and m["pdf_page"] < metas[i - 1]["pdf_page"]]
    if backward:
        print(f"  WARN: {len(backward)} section(s) mapped backwards "
              f"(first at reader page {backward[0]}); "
              f"try --split-print for exact per-section pages")
    return _write_toc(cfg, metas, pdf_out, title)


def _print_per_page(cfg: dict, metas: list[dict], page_html, chrome: str,
                    pdf_out: str, title: str) -> list[dict]:
    """Legacy: each reader page printed alone (exact pages, white tails)."""
    pdfs: list[str] = []
    for meta in metas:
        hp = os.path.join(cfg["pages_dir"], meta["file"])
        pp = hp[:-5] + ".pdf"
        size, book = paper_spec(cfg, meta)
        html_to_pdf(hp, pp, chrome, page_size=size, book=book)
        pdfs.append(pp)
        print(f"\r  printing {len(pdfs)}/{len(metas)}", end="", flush=True)
    print()

    counts = merge_pdfs(pdfs, pdf_out)
    start = 1
    for meta, n in zip(metas, counts):
        meta["pdf_page"] = start
        start += n
    return _write_toc(cfg, metas, pdf_out, title)


def _print_result(cfg: dict, pdf_out: str, source_pages: int) -> None:
    """Make the deliverable impossible to lose: absolute path, size, page count."""
    try:
        pages = pdf_page_count(pdf_out)
        mb = os.path.getsize(pdf_out) / (1024 * 1024)
        line = (f"{pages} PDF pages from {source_pages} reader pages, {mb:.1f} MB")
    except Exception:
        line = f"{source_pages} reader pages"
    mode = str(cfg.get("paper") or "book").lower()
    if mode == "a4":
        line += " | A4"
    else:
        size = cfg.get("_book_size") or (cfg.get("book_page") or [752, 1180])[:2]
        line += f" | page {describe_size(size)}"
    bar = "  " + "-" * 68
    print(f"\n{bar}\n  PDF  {pdf_out}\n       {line}")
    if cfg.get("_page_binding"):
        print(f"       {cfg['_page_binding']}")
    print(bar)
    if cfg.get("open_pdf"):
        try:
            os.startfile(pdf_out)          # Windows
        except AttributeError:             # pragma: no cover - posix
            os.system(f'open "{pdf_out}"')
        except Exception as exc:
            print(f"  (could not open the PDF: {exc})")


def rebuild(cfg: dict) -> None:
    """Re-print / re-bookmark from pages already on disk - no browser needed."""
    metas = load_index(cfg)
    if not metas:
        metas = [
            {"page": i, "file": name}
            for i, name in enumerate(
                sorted(n for n in os.listdir(cfg["pages_dir"])
                       if n.startswith("page_") and n.endswith(".html")), start=1)
        ] if os.path.isdir(cfg["pages_dir"]) else []

    for i, meta in enumerate(metas, start=1):
        meta.setdefault("page", i)
        meta.setdefault("file", f"page_{i:04d}.html")
        # Re-harvest metadata so improved extraction applies to old captures.
        path = os.path.join(cfg["pages_dir"], meta["file"])
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                fresh = toc_mod.page_meta(f.read(), page_number=meta.get("page_label"))
            meta.update(fresh)

    save_index(cfg, metas)
    assemble(cfg, metas)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def prompt_url(browser, cfg: dict) -> str:
    """URL to open. Empty string means 'use the reader window already open'."""
    try:
        hit = browser.reader_page()
        if hit is not None:
            browser.switch_to(hit["targetId"])
            print(f"  reader window already open: {browser.url()[:110]}")
            return ""
    except Exception:
        pass
    try:
        current = browser.url()
    except Exception:
        current = ""
    looks_like_reader = any(tok in current for tok in ("/reader/", "content-",
                                                      "cenreader"))
    last = cfg.get("last_url") or ""

    if looks_like_reader:
        print(f"  using the open tab: {current[:110]}")
        return ""
    hint = f" [ENTER = {last[:70]}]" if last else " [ENTER = keep current tab]"
    url = input(f"Paste the ebook reader URL{hint}: ").strip() or last
    return url


def main() -> None:
    cfg = load_config()

    if cfg.get("rebuild"):
        rebuild(cfg)
        return

    try:
        browser = open_browser(cfg)
    except Exception as exc:
        print(f"  could not start/attach Chrome: {str(exc)[:400]}")
        return

    content = ContentFrame()
    try:
        print(f"  active tab: {focus_reader_tab(browser)[:110]}")
        url = prompt_url(browser, cfg)
        if url:
            print(f"  opening: {url[:110]}")
            browser.navigate(url)
            cfg["last_url"] = url
            _save_last_url(url)
            print(f"  landed on: {focus_reader_tab(browser)[:110]}")

        if cfg.get("auto_start"):
            print(f"  waiting up to {cfg['login_timeout']}s for the reader "
                  f"(log in in the browser if prompted)...")
            if not wait_for_content(browser, content, cfg, cfg["login_timeout"]):
                dump_diagnostics(browser, cfg)
                return
        else:
            input("Log in if prompted and wait until the reader shows a page, "
                  "then press ENTER here: ")
            if not browser_alive(browser):
                print("  the browser window is gone - reconnecting...")
                browser.close(force=False)
                browser = open_browser(cfg)
            focus_reader_tab(browser)
            if not wait_for_content(browser, content, cfg):
                dump_diagnostics(browser, cfg)
                print("\n  If the book is visible in the browser but we still "
                      "found no content, the reader changed its DOM.\n"
                      "  Send the diagnose/ folder (summary.json + screen.png).")
                return
        time.sleep(cfg["page_wait"])

        if cfg.get("from_start"):
            goto_start(browser, cfg, content)
            time.sleep(cfg["page_wait"])
            if not wait_for_content(browser, content, cfg):
                dump_diagnostics(browser, cfg)
                print("  could not re-find the content after going to the cover")
                return
            first = read_state(browser, cfg, content)
            print(f"  starting at: {first.get('title') or '(untitled)'}"
                  + (f"  (p.{first['page_label']})" if first.get("page_label") else ""))

        metas = capture(browser, cfg)
        if metas:
            assemble(cfg, metas)
        else:
            print("No pages captured.")
    except KeyboardInterrupt:
        print("\n  interrupted - captured pages are kept in "
              f"{cfg['pages_dir']}/ (re-run to resume)")
    except cdp.CdpError as exc:
        print(f"\n  browser connection problem: {str(exc)[:300]}")
        print("  the browser was left open - re-run to resume, or run "
              "python diagnose.py")
    finally:
        close_browser(browser, cfg)


def _save_last_url(url: str) -> None:
    """Remember the reader URL so the next run can be a bare ENTER."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_url")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(url)
    except Exception:
        pass


if __name__ == "__main__":
    main()
