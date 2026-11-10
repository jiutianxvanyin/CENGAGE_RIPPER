"""Cengage webreader page extraction (CDP based).

The textbook content is a self-contained XHTML document
(``<html xmlns:epub>`` -> ``<body class="webreader">``) with selectable text.
It lives inside a frame - in the saved reference pages an
``<iframe id="iframe-page">``, live it varies by product line.

Instead of "switch into an element and hope", we ask the DevTools protocol for
the real frame tree and probe each frame's document, which also covers
cross-origin and shadow-DOM hosted frames:

  1. frames whose URL/name looks like the reader stream (``cenreader``,
     ``reader/stream``, ``iframe-page`` ...);
  2. any other sub-frame;
  3. the main document itself (some builds render the chapter inline).

Every candidate is validated by probing for a real webreader document, so a
wrong guess never fabricates content.

Entry points:
  * ``locate_content_frame(browser)``        - where the page content lives.
  * ``extract_page_html(browser)``           - live extraction via CDP.
  * ``extract_page_html_from_source(html)``  - static / saved file (read ``srcdoc``).
"""
from __future__ import annotations

import html as _html
import re
import time

from bs4 import BeautifulSoup

# The reader injects one ``<span epub:type="pagebreak" data-after="58">`` at the
# exact spot where textbook page 58 begins. Those spans are the only source of
# real pagination in a flow-reflowing EPUB, so cleanup must not eat them (they
# carry ``rs_skip``, which used to be stripped as noise). We rename them to our
# own class instead: the number stays, the visual stays zero-height.
PB_CLASS = "ripper-pb"

# Fast-path handle for the page-content frame (verified in both reference files).
WEBREADER_FRAME_ID = "iframe-page"

# Substrings that hint a frame is the reader's content stream.
FRAME_URL_HINTS = ("cenreader", "reader/stream", "/stream/", "webreader",
                   "content-", "iframe-page")

# Substring tokens that mark reader chrome / accessibility noise to strip.
NOISE_CLASS_TOKENS = (
    "screen-reader-only",
    "sf-hidden",
    "rs_skip",
    "schwepsy-skip",
    "selection-caret",
)

# IDs that should never appear in extracted content (defensive; mostly live outside).
NOISE_IDS = (
    "onetrust-consent-sdk",
    "blurLayer",
    "goog-gt-tt",
    "immersive-translate-popup",
    "readspeaker_button1",
)

# Probe run inside a frame/document to decide "is this the page content?".
PROBE_JS = r"""
var b = document.body, d = document.documentElement;
return {
  /* The document URL is the only reliable per-page identity on Cengage:
     data-cgi is a book-level id (identical on every page of the same title). */
  url: location.href || '',
  title: document.title || '',
  cls: (b && b.className) ? String(b.className) : '',
  cgi: (b && b.getAttribute) ? (b.getAttribute('data-cgi') || '') : '',
  epub: (b && b.getAttribute) ? (b.getAttribute('epub:type') || '') : '',
  len: b ? ((b.innerText || '').length) : 0,
  w: Math.max((d && d.scrollWidth) || 0, (b && b.scrollWidth) || 0),
  h: Math.max((d && d.scrollHeight) || 0, (b && b.scrollHeight) || 0)
};
"""

DOC_HTML_JS = ("return document.documentElement ? "
               "document.documentElement.outerHTML : '';")

# Inline every image as a data: URI, using the page's own session.
#
# Textbook pages reference figures relatively (``../graphics/x.jpg``), so once
# the HTML is printed outside the browser there are no cookies and the images
# 404. Fetching them from *inside* the frame keeps the login and makes the page
# self-contained - no screenshot, no OCR, real text plus real figures.
INLINE_ASSETS_JS = r"""
var imgs = [].slice.call(document.querySelectorAll('img'));
var inlined = 0, failed = 0, failedSrc = [];
function toDataURL(blob) {
  return new Promise(function (res, rej) {
    var f = new FileReader();
    f.onload = function () { res(f.result); };
    f.onerror = rej;
    f.readAsDataURL(blob);
  });
}
await Promise.all(imgs.map(function (img) {
  var src = img.currentSrc || img.getAttribute('src') || '';
  if (!src || src.indexOf('data:') === 0) return null;
  return fetch(src, {credentials: 'include'})
    .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
    .then(toDataURL)
    .then(function (dataUrl) { img.setAttribute('src', dataUrl); inlined++; })
    .catch(function () { failed++; if (failedSrc.length < 5) failedSrc.push(src); });
}));
return {imgs: imgs.length, inlined: inlined, failed: failed,
        failedSrc: failedSrc};
"""


# --------------------------------------------------------------------------- #
# Locating the content
# --------------------------------------------------------------------------- #
def looks_like_webreader(info: dict | None) -> bool:
    if not info:
        return False
    if "webreader" in (info.get("cls") or ""):
        return True
    if info.get("cgi"):
        return True
    return int(info.get("len") or 0) > 400


def probe_frame(browser, frame_id: str) -> dict | None:
    """Describe one frame's document; None when it cannot be read."""
    try:
        info = browser.eval(frame_id, PROBE_JS)
    except Exception:
        return None
    return info if isinstance(info, dict) else None


def probe_current_context(browser) -> dict | None:
    """Probe the main document (kept for diagnostics / backwards compat)."""
    try:
        return probe_frame(browser, browser.main_frame_id())
    except Exception:
        return None


def _hint_matches(frame) -> bool:
    hint = (frame.hint() or "").lower()
    return any(token in hint for token in FRAME_URL_HINTS)


def _rank(frame):
    """Order candidates: reader-looking first, sub-frames before main, deeper first."""
    return (0 if _hint_matches(frame) else 1,
            0 if frame.parent else 1,
            -int(frame.depth or 0))


def _how(frame) -> str:
    tail = (frame.url or "").split("?")[0][-42:]
    if frame.parent is None:
        return "main-document"
    if _hint_matches(frame):
        return f"hint:{tail}"
    return f"frame:{frame.id[:8]} {tail}"


def _locate_once(browser):
    """One attempt. Returns (frame_id|None, how|None, probe|None)."""
    try:
        frames = browser.frames()
    except Exception:
        return None, None, None
    for frame in sorted(frames, key=_rank):
        info = probe_frame(browser, frame.id)
        if looks_like_webreader(info):
            return frame.id, _how(frame), info
    return None, None, None


def locate_content_frame(browser, timeout: float = 20.0, poll: float = 0.4):
    """Find where the page content lives.

    Returns ``(frame_id_or_None, how, probe)``; ``how`` is a short strategy label
    (``None`` when nothing was found).
    """
    deadline = time.time() + max(timeout, 0)
    while True:
        frame_id, how, info = _locate_once(browser)
        if how:
            return frame_id, how, info
        if time.time() >= deadline:
            return None, None, None
        time.sleep(poll)


def frame_inventory(browser) -> list[dict]:
    """Debug helper: every frame on the page with its identity and a probe."""
    rows: list[dict] = []
    try:
        frames = browser.frames()
    except Exception as exc:
        return [{"index": 0, "hint": "", "error": str(exc)[:160]}]
    for i, frame in enumerate(frames):
        row = {"index": i, "hint": frame.hint()[:200],
               "depth": frame.depth, "url": (frame.url or "")[:160]}
        info = probe_frame(browser, frame.id) or {}
        row.update({
            "title": (info.get("title") or "")[:80],
            "body_class": (info.get("cls") or "")[:80],
            "cgi": (info.get("cgi") or "")[:40],
            "epub_type": (info.get("epub") or "")[:40],
            "text_len": info.get("len") or 0,
            "webreader": looks_like_webreader(info),
        })
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def extract_page_html(browser, timeout: float = 15.0) -> str:
    """Live extraction: locate the content frame, then return its document HTML."""
    frame_id, how, _info = locate_content_frame(browser, timeout=timeout)
    if how is None:
        raise ValueError(
            "No webreader content found (no frame with body.webreader or "
            "data-cgi, and the main document is not a reader document). "
            "Run diagnose.py and send the report it writes.")
    doc = browser.html(frame_id)
    if not doc or "webreader" not in doc:
        raise ValueError(f"Content frame ({how}) produced no webreader HTML")
    return doc


def inline_assets(browser, frame_id: str) -> dict:
    """Turn the page's images into data: URIs so the saved HTML is portable.

    Returns ``{"imgs": n, "inlined": n, "failed": n, "failedSrc": [...]}``.
    Never raises: a page whose images cannot be fetched is still worth keeping.
    """
    try:
        stats = browser.eval(frame_id, INLINE_ASSETS_JS, timeout=90,
                             is_async=True)
    except Exception as exc:
        return {"imgs": 0, "inlined": 0, "failed": 0,
                "failedSrc": [f"{type(exc).__name__}: {exc}"[:120]]}
    return stats if isinstance(stats, dict) else {
        "imgs": 0, "inlined": 0, "failed": 0, "failedSrc": []}


def extract_page_html_from_source(page_html: str) -> str:
    """Given a full reader HTML document (live snapshot or saved file),
    return the webreader document as an ``<html>...</html>`` string.

    Raises ValueError if neither a usable ``#iframe-page`` nor an inlined
    ``webreader`` body can be found.
    """
    soup = BeautifulSoup(page_html, "lxml")
    frame = soup.find(id=WEBREADER_FRAME_ID)
    if frame is not None:
        srcdoc = frame.get("srcdoc") or frame.get("data-srcdoc")
        if srcdoc:
            return _html.unescape(srcdoc)
        inner = frame.decode_contents()
        if inner.strip():
            return "<html>" + inner + "</html>"
    # Fallback: content may already be inlined as a webreader body.
    wr_body = soup.find("body", class_="webreader")
    if wr_body is not None:
        return "<html>" + str(wr_body) + "</html>"
    if soup.find(class_="webreader") is not None:
        return page_html
    raise ValueError("Could not locate #iframe-page or a webreader body in the source")


def _pagebreak_number(el) -> str:
    """Printed page number carried by a reader pagebreak marker."""
    for attr in ("data-after", "data-page", "data-pagenum"):
        val = el.get(attr)
        if val and str(val).strip():
            return str(val).strip()
    label = el.get("aria-label") or el.get("title") or ""
    m = re.search(r"(\d+)", str(label))
    if m:
        return m.group(1)
    el_id = el.get("id") or ""
    m = re.search(r"(\d+)$", el_id)
    return m.group(1) if m else ""


def mark_pagebreaks(soup: BeautifulSoup) -> list:
    """Rename reader pagebreak markers to ``span.ripper-pb[data-pb]``.

    Returns the markers in document order. The original ``id="page-58"`` is kept
    because in-book cross references use it as a link target.
    """
    markers = []
    for el in soup.find_all(True):
        is_break = (el.get("epub:type") == "pagebreak"
                    or (el.name == "span"
                        and "pagenumber" in " ".join(el.get("class") or [])))
        if not is_break or PB_CLASS in (el.get("class") or []):
            continue
        num = _pagebreak_number(el)
        mark = soup.new_tag("span")
        mark["class"] = [PB_CLASS]
        if num:
            mark["data-pb"] = num
        if el.get("id"):
            mark["id"] = el["id"]
        el.replace_with(mark)
        markers.append(mark)
    return markers


def pagebreak_numbers(webreader_html: str) -> list[int]:
    """Printed page numbers of every pagebreak marker in a document, in order."""
    soup = BeautifulSoup(webreader_html, "lxml")
    out: list[int] = []
    for el in soup.find_all(True):
        marks = (el.get("epub:type") == "pagebreak"
                 or PB_CLASS in (el.get("class") or [])
                 or (el.name == "span"
                     and "pagenumber" in " ".join(el.get("class") or [])))
        if not marks:
            continue
        num = _pagebreak_number(el)
        if num.isdigit():
            out.append(int(num))
    return out


def clean_webreader(webreader_html: str) -> str:
    """Strip reader chrome and accessibility noise from a webreader document
    while preserving ``data-cgi`` anchors, ``epub:type`` semantic structure and
    the reader's pagebreak markers (see ``PB_CLASS``).
    """
    soup = BeautifulSoup(webreader_html, "lxml")

    for node in soup.find_all("script"):
        node.extract()

    mark_pagebreaks(soup)          # before the noise filter, so rs_skip is harmless

    # Collect targets first, then extract, to avoid mutating during iteration.
    to_remove = []
    for el in soup.find_all(True):
        cls = el.get("class")
        if cls:
            joined = " ".join(cls)
            if any(tok in joined for tok in NOISE_CLASS_TOKENS):
                to_remove.append(el)
                continue
        el_id = el.get("id")
        if el_id and el_id in NOISE_IDS:
            to_remove.append(el)
    for el in to_remove:
        el.extract()

    return str(soup)


def page_text(webreader_html: str) -> str:
    """Plain-text extract of a (cleaned) webreader document - handy for TOC / search."""
    soup = BeautifulSoup(webreader_html, "lxml")
    return soup.get_text(" ", strip=True)


def pagebreak_count(webreader_html: str) -> int:
    """How many pagebreak markers the document carries, roman numerals included.

    ``pagebreak_numbers`` only reports the decimal ones (that is what the page
    binding needs); the index wants the raw count, because a front-matter page
    is still paginated even though it is numbered ``xxvii`` rather than 27.
    """
    soup = BeautifulSoup(webreader_html, "lxml")
    total = 0
    for el in soup.find_all(True):
        if (el.get("epub:type") == "pagebreak"
                or PB_CLASS in (el.get("class") or [])
                or (el.name == "span"
                    and "pagenumber" in " ".join(el.get("class") or []))):
            total += 1
    return total
