"""Build a readable PDF / HTML book from extracted webreader pages.

Two strategies:
  * Faithful (default): render each cleaned page with headless Chrome, then merge
    the per-page PDFs. Each page keeps its own exact styles.
  * Fast (optional): ``build_book_html`` concatenates all pages into one HTML
    document with ``.book-page { break-after: page }`` and prints once.
"""
from __future__ import annotations

import base64
import copy
import os
import re
import subprocess
from pathlib import Path

from bs4 import BeautifulSoup

from reader_extract import PB_CLASS, mark_pagebreaks

try:
    import pikepdf
except ImportError:  # pragma: no cover
    pikepdf = None

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None

PAGE_BREAK_CSS = """
@page { size: auto; margin: 10mm; }
.book-page { break-after: page; }
.book-page:last-child { break-after: auto; }
.book-toc { break-after: page; font-family: sans-serif; }
.book-toc h1 { font-size: 1.4em; margin: 0 0 .8em; }
.book-toc ul { list-style: none; padding: 0; margin: 0; }
.book-toc li { margin: .18em 0; }
.book-toc li.lvl2 { padding-left: 1.4em; font-size: .95em; }
.book-toc li.lvl3 { padding-left: 2.8em; font-size: .9em; }
.book-toc a { text-decoration: none; color: #111; }
"""

# Break markers injected by reader_extract.clean_webreader. In the flow they are
# zero-height blocks that start the next printed page, so one PDF page == one
# real textbook page and the printed numbers line up with the PDF page numbers.
PB_CSS = """
.ripper-pb { break-before: page; page-break-before: always;
  height: 0 !important; margin: 0 !important; padding: 0 !important;
  border: 0 !important; }
.ripper-pb:first-child { break-before: auto; page-break-before: auto; }
.book-page.ripper-newpage { break-before: page; page-break-before: always; }
"""

# Elements we are willing to split in two when a pagebreak marker sits inside
# them (a paragraph that continues on the next printed page).
SPLIT_HOSTS = {
    "p", "li", "td", "th", "dd", "dt", "blockquote", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6", "pre", "div", "section", "article",
    "aside", "figure",
}

# One element per textbook page, produced by :func:`group_pages`. The page break
# moves from the marker to the wrapper, which is what lets a page carry its own
# zoom (see ``measure_page_heights``) and therefore stay on exactly one sheet.
_ANCHOR_RE = re.compile(r"G\d{4}")

PAGE_WRAP_CSS = """
.ripper-page { break-before: page; page-break-before: always; position: relative; }
.ripper-page:first-of-type { break-before: auto; page-break-before: auto; }
.ripper-page .book-page { break-after: auto; page-break-after: auto; }
/* A loose insert (the R appendices) carries no page number, so it has to start
   a sheet of its own: letting it begin in the middle of the sheet that closes
   page 33 made that sheet unnumbered, and the book jumped 32 -> 34. */
.ripper-page.ripper-insert { break-before: page; page-break-before: always; }
/* An invisible marker at the top of every wrapper. Chrome writes it into the
   PDF text layer, so after printing we can read which sheet each wrapper
   became instead of hunting for its prose: on the 734-page edition, text
   search failed for 703 of 1035 wrappers, which left only 412 page anchors and
   collapsed the footer numbering into "352 (cont.)" for hundreds of sheets. */
.ripper-anchor, .book-page span.ripper-anchor {
                 display: inline !important; font-size: 1px !important;
                 line-height: 1px !important; letter-spacing: 0 !important;
                 color: transparent !important; }
"""

# Continuous flow: sections follow each other like pages of a book instead of
# each starting a new sheet (which produced a white tail after every section).
STREAM_CSS = """
.book-page { break-after: auto; }
.book-page h1, .book-page h2, .book-page h3, .book-page h4 {
  break-after: avoid-page; break-inside: avoid;
}
.book-page p { orphans: 2; widows: 2; margin: 0 0 .62em; }
.book-page p:first-child { margin-top: 0; }
.book-page .keytermentry, .book-page .keyterm { display: inline; }
.book-page figure { break-inside: avoid; }
/* A table is allowed to run onto the next sheet: forbidding it made a table
   that is 70% of a page jump to a fresh sheet and leave the rest of the
   previous one blank, which is how a 3-page section became 6 sheets. */
.book-page table { break-inside: auto; }
.book-page thead { display: table-header-group; }
.book-page tr { break-inside: avoid; }
/* The reader styles its tables for a scrolling screen: 12px cell padding and
   a 2em line box make a 12-row table fill a whole sheet, so the text paid for
   the screen's airiness in extra pages. A printed table is tighter. */
.book-page table { font-size: .9em; line-height: 1.25; }
.book-page td, .book-page th { padding: 3px 6px !important; }
.book-page td p, .book-page th p { margin: 0 !important; }
.book-page { line-height: 1.45; }
"""
TOC_HTML_HEAD = '<nav class="book-toc"><h1>{title}</h1><ul>'


# --------------------------------------------------------------------------- #
# Paper size taken from the cover
# --------------------------------------------------------------------------- #
# The book's page shape is not a guess: the cover scan *is* one page, so its
# pixel ratio is the paper's ratio. Using it keeps the cover edge-to-edge and
# gives every other page the proportions of the printed book instead of the
# 1:2 strip that a bare 752x1500 produces.
def image_size(data: bytes):
    """(width, height) of a PNG or JPEG byte string, or ``None``."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] == b"\xff\xd8":                       # JPEG: walk to the SOFn
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC7, 0xC9, 0xCB, 0xCD):
                height = int.from_bytes(data[i + 5:i + 7], "big")
                width = int.from_bytes(data[i + 7:i + 9], "big")
                return (width or None), (height or None)
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def cover_aspect(webreader_html: str) -> float | None:
    """height / width of the cover image on this page (``None`` if no cover)."""
    data = cover_image(webreader_html)
    if not data:
        return None
    size = image_size(data)
    if not size or not size[0]:
        return None
    return size[1] / size[0]


def cover_image(webreader_html: str) -> bytes | None:
    """The decoded bytes of this page's cover scan, if it has one."""
    soup = BeautifulSoup(webreader_html, "lxml")
    img = (soup.find("img", class_="coverimage")
           or soup.find("img", attrs={"epub:type": "cover"})
           or soup.find("img", attrs={"role": "doc-cover"}))
    if img is None:
        return None
    src = img.get("src") or ""
    if not src.startswith("data:"):
        return None
    try:
        return base64.b64decode(src.partition(",")[2], validate=False)
    except Exception:
        return None


def bleed_cover(pdf_path: str, image_bytes: bytes | None) -> bool:
    """Paint the cover scan over the whole first page.

    A page margin lives in ``@page``, so CSS cannot bleed into it; and an image
    that is exactly one page tall is pushed onto the next sheet by the
    paginator. Painting it in PDF space is exact: the scan fills the page the
    way the printed cover fills the board.
    """
    if not image_bytes or fitz is None:
        return False
    try:
        with fitz.open(pdf_path) as doc:
            page = doc[0]
            rect = page.rect
            size = image_size(image_bytes)
            # The paper was shaped from this very scan, so stretching it to the
            # page costs nothing and leaves no white hairline. If the two
            # shapes disagree (a hand-set ``paper_ratio``), keep the scan's own
            # proportion instead of squashing the artwork.
            same_shape = bool(size and size[0]) and abs(
                (size[1] / size[0]) / (rect.height / rect.width) - 1) <= 0.03
            page.insert_image(rect, stream=image_bytes, overlay=True,
                              keep_proportion=not same_shape)
            doc.save(pdf_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        return True
    except Exception as exc:                      # pragma: no cover - defensive
        print(f"  WARN: could not bleed the cover ({exc})")
        return False


def _wrap_page(webreader_html: str, index: int, page_id: str | None = None):
    """Return (head_blocks, wrapped_body_div) for one page."""
    return _wrap_soup(BeautifulSoup(webreader_html, "lxml"), index, page_id)


def _wrap_soup(soup, index: int, page_id: str | None = None,
               new_page: bool = False):
    """Same as :func:`_wrap_page` for a soup we already parsed and prepared."""
    head_parts = []
    for st in soup.find_all("style"):
        if st.string:
            head_parts.append(str(st))
    for link in soup.find_all("link", rel="stylesheet"):
        href = link.get("href")
        if href:
            head_parts.append(f'<link rel="stylesheet" href="{href}">')
    body = soup.find("body") or soup
    is_cover = (body.get("epub:type") == "cover"
                or soup.find("img", class_="coverimage") is not None)
    if is_cover:
        # A cover is one piece of artwork and nothing else. The reader wraps it
        # in scaffolding it needs and a printed book does not: an ``h1.hideme``
        # reading "Book cover" (a landmark for screen readers) and
        # ``p.mediacredit`` ("Martin Puddy/Stone/Getty Images"). Printing them
        # pushed the credit under the image, overflowed the sheet and split the
        # cover over three pages. Keep the image, drop the rest.
        art = soup.find("img", class_="coverimage") or soup.find("img")
        inner = str(art) if art is not None else ""
    else:
        inner = body.decode_contents()
    classes = " ".join(body.get("class") or []) + " book-page"
    if new_page:
        classes += " ripper-newpage"
    if is_cover:
        classes += " ripper-cover"
    ident = f' id="page-{index}" data-src="{index}"' if not page_id else (
        f' id="page-{index}" data-src="{index}" data-sec="sec-{page_id}"')
    extra = f'<span id="sec-{page_id}"></span>' if page_id else ""
    div = f'<div class="{classes.strip()}"{ident}>{extra}{inner}</div>'
    return head_parts, div


def build_toc_html(entries: list[dict], title: str = "Contents") -> str:
    """Render outline entries as a linked HTML table of contents."""
    if not entries:
        return ""
    items = []
    for entry in entries:
        level = min(max(int(entry.get("level", 1)), 1), 3)
        label = (entry.get("title") or "").replace("&", "&amp;").replace("<", "&lt;")
        items.append(
            f'<li class="lvl{level}"><a href="#page-{entry["page"]}">{label}</a></li>')
    head = TOC_HTML_HEAD.format(title=title)
    return head + "\n".join(items) + "</ul></nav>"


def _index_of(nodes, target) -> int:
    """Position of ``target`` in ``nodes`` by identity (bs4 equality is deep)."""
    for i, node in enumerate(nodes):
        if node is target:
            return i
    return -1


def _snippet_after(node, limit: int = 110) -> str:
    """First slice of text that follows a node - our handle for finding it again."""
    parts: list[str] = []
    total = 0
    for text in node.find_all_next(string=True):
        piece = " ".join(str(text).split())
        if not piece:
            continue
        parts.append(piece)
        total += len(piece)
        if total >= limit:
            break
    return " ".join(parts)[:limit]


def _is_marker(node) -> bool:
    """Is ``node`` one of our pagebreak markers (and not just styled like one)?"""
    return (getattr(node, "name", None) == "span"
            and PB_CLASS in (node.get("class") or []))


_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _roman_to_int(text: str) -> int | None:
    """``xxvii`` -> 27. ``None`` for anything that is not a plain roman number.

    Front matter is numbered in roman numerals, so the reader's markers carry
    ``data-after="xxvii"``. Without this the front matter looks unnumbered and
    gets no footer at all.
    """
    s = (text or "").strip().lower()
    if not s or len(s) > 12 or any(ch not in _ROMAN for ch in s):
        return None
    total = 0
    for i, ch in enumerate(s):
        value = _ROMAN[ch]
        nxt = _ROMAN[s[i + 1]] if i + 1 < len(s) else 0
        total += -value if value < nxt else value
    return total if total > 0 else None


def _page_number(raw: str) -> tuple[int | None, str]:
    """(numeric value, style) for a marker's printed number; style is ``D``/``r``.

    Roman numerals get ``None`` on purpose. This book's front matter hands its
    markers over partly parked (24 markers for 3 pages of text, the first saying
    ``from i to xxi``), so numbering what is left produces a chain with holes -
    which then inserts fake blank pages and stamps ``xxii`` on the fifth sheet.
    A front-matter page number we cannot trust is worse than none; the label is
    still recorded for the index.
    """
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw), "D"
    if _roman_to_int(raw) is not None:
        return None, "r"
    return None, ""


# --------------------------------------------------------------------------- #
# Putting the publisher's paragraphs back together
# --------------------------------------------------------------------------- #
# This book's HTML is produced by a conversion tool that turns every inline
# element into a block: a glossary term becomes
#
#     <p>…the scale of measurement is considered a </p>
#     <div class="keytermentry"><button>nominal scale</button></div>
#     . For example, referring to …
#
# and long paragraphs get cut at those points. Rendered as-is that means a line
# ends mid-sentence ("…is considered a"), the rest starts on a new line, and the
# loose text after the block is laid out at a different width than the paragraph
# — exactly the ragged right edge and the uneven leading in the screenshots.
# ``refragment`` walks each container once and rebuilds real paragraphs.
BLOCK_TAGS = {
    "p", "div", "section", "article", "aside", "header", "footer", "nav",
    "main", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt",
    "dd", "table", "thead", "tbody", "tr", "td", "th", "figure", "figcaption",
    "blockquote", "pre", "hr", "form", "fieldset", "details", "summary",
    "center", "address", "caption", "colgroup", "video", "audio", "canvas",
}
# Containers whose loose text is a paragraph the conversion tool left unwrapped.
# A ``<p>`` is not one of them - wrapping text inside a paragraph would nest one.
CONTAINERS = {"body", "section", "article", "aside", "div", "li", "dd", "main",
              "header", "footer", "nav"}
# Wrappers that only *look* like blocks. ``keytermentry``/``keyterm`` wrap a
# glossary word that sits inside the sentence, and ``footnote-wrapper`` wraps a
# single ``*`` - the conversion tool turned each into a ``div``, which closes the
# paragraph around it ("…as appraised by the Fitch Group" / [ * ] / "; the credit
# ratings range…"). A ``div.mathexpr`` is *not* one of these: that is a display
# equation and belongs on its own line.
INLINE_BLOCK_CLASSES = ("keytermentry", "keyterm", "glossary", "gl-entry",
                        "footnote")
# A paragraph that ends like this is finished; the next block is a new one.
_PARA_END = re.compile(r"[.!?:;”\"’)\]}]$")


def _inline_wrappers(soup) -> int:
    """Turn block-shaped inline wrappers (a glossary term, a button) inline."""
    changed = 0
    for el in soup.find_all(["div", "button"]):
        classes = " ".join(el.get("class") or []).lower()
        if el.name == "button" or any(c in classes for c in INLINE_BLOCK_CLASSES):
            el.name = "span"
            changed += 1
    return changed


def _joinable(prev_text: str, next_text: str) -> bool:
    """Is ``next_text`` the same sentence as ``prev_text``, split by markup?"""
    prev_text = prev_text.rstrip()
    next_text = next_text.lstrip()
    if not prev_text or not next_text:
        return False
    if next_text[0].islower() or next_text[0] in ".,;:)]}—–":
        return True
    return not _PARA_END.search(prev_text)


def _new_paragraph(soup, before):
    """A fresh ``<p>`` inserted right before ``before``."""
    p = soup.new_tag("p")
    before.insert_before(p)
    return p


def _merge_children(soup, host, stats: dict) -> None:
    """Rebuild the direct children of ``host`` into whole paragraphs."""
    buffer = None                     # the <p> that inline content flows into
    can_wrap = host is soup or host.name in CONTAINERS
    for node in list(host.contents):
        name = getattr(node, "name", None)

        if name is None:                                  # text node
            if not str(node).strip():
                node.extract()
                continue
            if not can_wrap:
                continue
            if buffer is None:
                buffer = _new_paragraph(soup, node)
            else:
                stats["joined"] += 1
            buffer.append(node.extract())
            continue

        if name in ("script", "style", "link", "meta"):
            continue

        if _is_marker(node):                              # page break: barrier
            buffer = None
            continue

        if name not in BLOCK_TAGS:                        # inline element
            if buffer is None:
                if not can_wrap:
                    continue
                buffer = _new_paragraph(soup, node)
            buffer.append(node.extract())
            continue

        # A block element.
        if name == "p" and buffer is not None and _joinable(
                buffer.get_text(" ", strip=True), node.get_text(" ", strip=True)):
            for child in list(node.contents):
                buffer.append(child.extract())
            node.decompose()
            stats["joined"] += 1
            continue

        buffer = node if name == "p" else None


def refragment(soup) -> dict:
    """Repair the block-split paragraphs the publisher's conversion left behind.

    Returns a small count dict for the console (how many wrappers were made
    inline and how many paragraphs absorbed a stray run).
    """
    stats = {"inline": _inline_wrappers(soup), "joined": 0}
    for host in [soup] + soup.find_all(True):
        if host.name in ("script", "style", "head"):
            continue
        _merge_children(soup, host, stats)
    return stats


def reveal_details(soup) -> int:
    """Open every ``<details>`` so the exercise answers print.

    The reader hides each answer behind ``<details class="toggleAnswer"><summary>
    SHOW ANSWER`` - closed until the student clicks it. A closed ``<details>``
    prints nothing but its summary line, so a page that held nothing *but*
    answers came out blank; the fitter then saw a wrapper with no text and no
    figure, dropped it, and the book silently lost a page number (page 26 of
    this edition). Opening the element costs a taller page and keeps the text.

    Returns how many were opened, for the console.
    """
    opened = 0
    for el in soup.find_all("details"):
        if not el.has_attr("open"):
            el["open"] = ""
            opened += 1
    return opened


_ANSWER_SUMMARY = re.compile(r"^\s*(show|view|see)\s+answer\s*$", re.I)


def format_answers(soup) -> int:
    """Turn "SHOW ANSWER" toggles into a printed ``Answer: ...`` line.

    The webreader hides each exercise answer behind
    ``<details class="toggleAnswer"><summary class="quiz-answer-summary-button">
    SHOW ANSWER</summary>``. On paper the button is meaningless - there is
    nothing to click - and it costs a full line per exercise plus the answer's
    own paragraph break. A textbook prints the answer right under the question,
    indented, with a bold ``Answer:`` lead-in, so the exercise reads:

        a. How many elements are in this data set?
            Answer: The 10 elements are the 10 tablet computers.

    The summary is dropped, the answer body becomes inline content so it stays
    on the same line as the lead-in, and the block is indented instead.
    """
    count = 0
    for det in soup.find_all("details"):
        summary = det.find("summary")
        if summary is not None and not _ANSWER_SUMMARY.match(
                summary.get_text(" ", strip=True)):
            continue                     # an ordinary collapsible box
        box = soup.new_tag("div")
        box["class"] = ["ripper-answer"]
        if det.find(["table", "ul", "ol"]):
            # Some answers are a small table ("Variable | Measurement scale")
            # or a list. Flattening those onto one line turns them into
            # "VariableMeasurementScaleCost ($)Ratio..." - the structure *is*
            # the answer, so they keep their blocks and the lead-in sits above.
            box["class"] = ["ripper-answer", "ripper-answer-block"]
        lead = soup.new_tag("span")
        lead["class"] = "ripper-answer-lead"
        lead.string = "Answer: "
        box.append(lead)
        for child in list(det.contents):
            if getattr(child, "name", None) == "summary":
                continue
            box.append(child)            # bs4 moves the node out of <details>
        det.replace_with(box)
        count += 1
    return count


def _has_content_after(mark) -> bool:
    """Does real content follow this marker before the next marker?

    The reader cannot always place a pagebreak where the page really starts: it
    parks a whole run of them in one ``div.block-of-pagenumbers`` (front matter
    says ``data-after="from i to xxi"``; a full-page table does the same for the
    pages it spans). Those markers name pages that simply are not in the HTML,
    so giving each one a break would print a stack of empty sheets.
    """
    for node in mark.next_elements:
        if getattr(node, "name", None) is None:          # NavigableString
            if str(node).strip():
                return True
            continue
        if node.name in ("script", "style"):
            continue
        if _is_marker(node):
            return False                                  # ran into the next one
        if node.name in ("img", "figure", "table", "svg", "picture", "hr"):
            return True
        if node.get_text(strip=True):
            return True
        # A wrapper with no text of its own (e.g. the block-of-pagenumbers
        # container): keep walking into it instead of counting it as content.
    return False


def _hoist_parked_run(soup) -> None:
    """Move one marker of a parked run *after* its container.

    The reader parks a run of page markers in ``div.block-of-pagenumbers`` - a
    block that holds nothing but empty anchors and sits between two pages'
    content. A page break *inside* that block does nothing (there is no text in
    it to push), which is why the text following it stayed on the same sheet.
    Moving the *last* marker behind the container puts the break exactly where
    the next printed page with content starts (the earlier ones name pages whose
    content the reader never sent); they stay put.
    """
    for box in soup.find_all(class_="block-of-pagenumbers"):
        marks = box.find_all("span", class_=PB_CLASS)
        # Ask about the text after the *last* marker, not after the container:
        # ``next_elements`` from the container walks its own children first, so
        # it used to trip over the second marker and answer "nothing follows" -
        # which left a real page break buried in a zero-height block, and the
        # text after it simply stayed on the same sheet.
        if not marks or not _has_content_after(marks[-1]):
            continue
        last = marks[-1]
        last["data-parked"] = "1"
        last.extract()
        box.insert_after(last)


def paginate(soup, tail_content: bool = False) -> list[dict]:
    """Turn inline pagebreak markers into block-level page breaks.

    Returns one dict per break, in document order: ``{"num": 58, "snippet":
    "In the left margin, the row labels..."}``. ``num`` is the printed page
    number the reader attached to the marker; ``snippet`` is the text that
    starts the new page, which later lets us find the page again in the PDF.

    A marker sitting mid-paragraph splits that paragraph, so the printed page
    starts exactly where the textbook page starts.

    ``tail_content`` says the document continues after this soup, in the next
    reader section. The reader very often puts a pagebreak at the *end* of a
    section, and the text it announces opens the next one; without this flag
    those markers look like "a page with no content", get dropped, and their
    pages silently merge into the previous sheet.
    """
    breaks: list[dict] = []
    mark_pagebreaks(soup)                 # tolerate raw (not yet cleaned) input
    refragment(soup)                      # rebuild the paragraphs it broke apart
    format_answers(soup)                  # "SHOW ANSWER" -> a printed Answer line
    reveal_details(soup)                  # any other collapsible box stays open
    _hoist_parked_run(soup)
    marks = soup.find_all("span", class_=PB_CLASS)
    if not marks:
        return breaks

    for mark in marks:
        raw = (mark.get("data-pb") or "").strip()
        num, style = _page_number(raw)
        anchor_id = mark.get("id")
        # A run parked in ``div.block-of-pagenumbers`` names pages the reader
        # never placed (front matter says "from i to xxi" and then leaves 24
        # markers for 3 pages of text). Such a marker may still need a break for
        # the layout, but it must not claim to be that printed page.
        parked_in_block = mark.find_parent(class_="block-of-pagenumbers")
        if mark.get("data-parked") or parked_in_block:
            num = None
        if parked_in_block:
            # Only the one marker ``_hoist_parked_run`` lifted out of the block
            # owns a break; its companions are a *list* of pages the reader
            # could not place. Breaking on each of them shredded the text into
            # 24 half-empty sheets per section and doubled the PDF. Keep the
            # anchors so in-book links still resolve, and nothing else.
            anchor = soup.new_tag("span")
            if anchor_id:
                anchor["id"] = anchor_id
            mark.replace_with(anchor)
            breaks.append({"num": None, "label": raw, "style": style,
                           "snippet": "", "phantom": True})
            continue

        if not _has_content_after(mark) and not tail_content:
            # A page that exists in the book but not in this document: keep the
            # anchor (in-book links still resolve) but print no sheet for it.
            anchor = soup.new_tag("span")
            if anchor_id:
                anchor["id"] = anchor_id
            mark.replace_with(anchor)
            breaks.append({"num": num, "label": raw, "style": style,
                           "snippet": "", "phantom": True})
            continue

        # Walk up to the nearest block; split each inline wrapper on the way.
        path = [mark]
        host = mark.parent
        while host is not None and host.name not in SPLIT_HOSTS and host.name != "body":
            path.append(host)
            host = host.parent
        if host is None:                      # detached document: give up safely
            mark.extract()
            continue

        node = mark
        for el in path[1:]:
            kids = list(el.children)
            i = _index_of(kids, node)
            if i < 0:
                node = el
                continue
            rest = kids[i + 1:]
            if rest:
                clone = soup.new_tag(el.name)
                for key, val in el.attrs.items():
                    if key != "id":
                        clone[key] = val
                for child in rest:
                    clone.append(child.extract())
                el.insert_after(clone)
                node = clone
            else:
                node = el

        div = soup.new_tag("div")
        div["class"] = [PB_CLASS]
        if num is not None:
            div["data-pb"] = str(num)
        if anchor_id:
            div["id"] = anchor_id
        if node is host:
            host.insert_after(div)            # marker sat at the very end
        else:
            node.insert_before(div)
        mark.extract()
        breaks.append({"num": num, "label": raw, "style": style,
                       "snippet": _snippet_after(div), "phantom": False})
    return breaks


# --------------------------------------------------------------------------- #
# Links: an <a> that leaves the book becomes an in-document jump.
# --------------------------------------------------------------------------- #
READER_HOST_HINTS = ("cengage.com", "cengagebrain.com")
_SECTION_RE = re.compile(r"/content-([A-Za-z0-9_\-]+)")


def _content_id_from_href(href: str) -> str:
    match = _SECTION_RE.search(href or "")
    return match.group(1) if match else ""


def rewrite_links(soup, known_ids: set[str], section_ids: set[str]) -> dict:
    """Point every in-book anchor at a document anchor instead of the website.

    Cengage already ships semantic links (``data-page-id="content-bd_ch_02_sect_03"``
    plus a ``#cgi`` fragment for figures/tables). Both map onto ids that exist in
    the assembled document, so Chrome writes them into the PDF as internal
    GoTo actions - the link works offline and never opens the web reader.
    """
    stats = {"internal": 0, "external": 0, "dropped": 0}
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        anchor.attrs.pop("target", None)
        if href.startswith("#"):
            stats["internal"] += 1
            continue
        frag = href.split("#", 1)[1] if "#" in href else ""
        page_id = (anchor.get("data-page-id") or "").replace("content-", "") \
            or _content_id_from_href(href)
        is_reader = any(h in href for h in READER_HOST_HINTS)

        if frag and frag in known_ids:                    # figure/table/page id
            anchor["href"] = f"#{frag}"
            stats["internal"] += 1
            continue
        if page_id and page_id in section_ids:            # another section
            anchor["href"] = f"#sec-{page_id}"
            stats["internal"] += 1
            continue
        if is_reader:                                     # menu/search/help link
            del anchor["href"]
            anchor["class"] = list(anchor.get("class") or []) + ["ripper-deadlink"]
            stats["dropped"] += 1
            continue
        stats["external"] += 1
    return stats


def _classes(tag) -> list[str]:
    return list(tag.get("class") or [])


def group_pages(soup) -> list[dict]:
    """Re-parent the flowing text so that one textbook page is one element.

    The reader marks every textbook page start with ``<span class="pagenumber
    rs_skip" data-after="46" epub:type="pagebreak">``; after cleaning that is a
    ``div.ripper-pb`` sitting *between* two blocks of text. Printing used to put
    the break there, which cut the page correctly but left the content between
    two markers as loose siblings: it could not be measured, so it could not be
    made to fit a sheet, and every textbook page spilled onto a second one.

    Here the content between marker N and marker N+1 is moved into a single
    ``div.ripper-page`` (``data-pb="N"``). Section wrappers are re-created
    around it, so the reader's own classes survive, and a page that spans two
    reader sections still ends up in one element.

    A marker the reader parked in ``div.block-of-pagenumbers`` (it could not
    place it) opens no page: the run it belongs to flows, because cutting there
    would be a guess. Those groups are reported with ``fit=False`` - no zoom is
    applied to them, since there is no single page to fit.

    Returns one dict per group, in document order:
    ``{"el", "pb", "id", "fit", "parked"}``.
    """
    body = soup.find("body") or soup
    roots = [ch for ch in body.find_all(True, recursive=False)
             if "book-toc" not in _classes(ch)]

    items: list[tuple] = []     # ("start"|"anchor"|"break"|"leaf"|"insert", …)

    def walk(node, path):
        for ch in node.find_all(True, recursive=False):
            if PB_CLASS in _classes(ch):
                items.append(("break", ch, path))
            elif ch.find(class_=PB_CLASS) is not None:
                walk(ch, path + [ch])
            else:
                items.append(("leaf", ch, path))

    insert_at = [i for i, ch in enumerate(roots) if _is_insert(ch)]
    first_insert = min(insert_at) if insert_at else None
    last_insert = max(insert_at) if insert_at else None

    for i, ch in enumerate(roots):
        cls = _classes(ch)
        if i == first_insert:
            # The insert is its own block: it can start on the sheet the
            # previous page left half empty, or a new one when there is no room.
            items.append(("start", ch, []))
        elif last_insert is not None and i == last_insert + 1:
            # First section after the inserts. Whatever the inserts carried ends
            # here and the numbering resumes at Chapter 2 (page 34) - but the
            # chapter's own opening (title, learning objectives, introduction)
            # is the *head* of that page, printed above the marker the reader
            # put further down the same section. Handing it forward to the
            # marker's page keeps the title and the page number on one sheet;
            # starting a page here instead printed the opener as page 33.
            kind = "carry" if ch.find(class_=PB_CLASS) is not None else "start"
            items.append((kind, ch, []))
        elif first_insert is not None and first_insert <= i <= last_insert:
            pass                              # the insert: whole section, no cut
        elif "ripper-newpage" in cls or "ripper-cover" in cls:
            items.append(("start", ch, []))
        if first_insert is not None and first_insert <= i <= last_insert:
            items.append(("insert", ch, []))
        elif ch.find(class_=PB_CLASS) is None:
            items.append(("leaf", ch, []))
        else:
            walk(ch, [ch])

    groups: list[dict] = []
    cur = _new_group()
    groups.append(cur)
    for kind, tag, path in items:
        if kind == "start":
            if cur["nodes"]:
                cur = _new_group()
                cur["started"] = True
                groups.append(cur)
            continue
        if kind == "carry":
            # Open a holder for the head of the next page; the page's own marker
            # adopts it (see "break" below), so it never becomes a group of its
            # own - a group without a page number would have claimed a sheet and
            # pushed every following number one page off.
            if cur["nodes"]:
                cur = _new_group()
                cur["started"] = True
                groups.append(cur)
            cur["carry"] = True
            continue
        if kind == "insert":
            cur["insert"] = True
            cur["nodes"].append((tag, path))
            continue
        if kind == "break":
            pb = (tag.get("data-pb") or "").strip()
            if not pb:
                # The reader says "somewhere around here there were pages I
                # cannot point at". Keep flowing; do not invent a boundary.
                cur["parked"] = True
                cur["fit"] = False
                continue
            # The marker sits where page 46 *starts*: the block it opens is
            # page 46. (Checked against the book's own first page: marker "1"
            # precedes Chapter 1's opening page, and that page prints 1.)
            cur["fit"] = cur["started"] and not cur["parked"] and not cur["insert"]
            head = cur["nodes"] if cur.get("carry") else None
            cur = _new_group(pb, tag.get("id"))
            if head:
                # Document order: the chapter opening precedes the page body.
                cur["nodes"] = list(head) + cur["nodes"]
                groups.pop()                  # the holder is now empty
            cur["started"] = True
            groups.append(cur)
            continue
        cur["nodes"].append((tag, path))

    for ch in roots:
        ch.extract()

    out: list[dict] = []
    for g in groups:
        if not g["nodes"]:
            continue
        div = soup.new_tag("div")
        cls = ["ripper-page"]
        if not g["fit"]:
            cls.append("ripper-flow")
        if g["insert"]:
            cls.append("ripper-insert")
        div["class"] = cls                   # a list: bs4 splits multi-class only then
        if g["pb"]:
            div["data-pb"] = g["pb"]
        if g["id"]:
            div["id"] = g["id"]
        stack: list[tuple] = []               # [(source element, empty clone)]
        for tag, path in g["nodes"]:
            keep = 0
            while (keep < len(stack) and keep < len(path)
                   and stack[keep][0] is path[keep]):
                keep += 1
            del stack[keep:]
            for src in path[keep:]:
                clone = soup.new_tag(src.name)
                clone.attrs = {k: v for k, v in src.attrs.items()
                               if k != "id"}      # no duplicate anchors
                (stack[-1][1] if stack else div).append(clone)
                stack.append((src, clone))
            (stack[-1][1] if stack else div).append(copy.deepcopy(tag))
        # Invisible index written into the sheet by the print step, so the caller
        # can map wrapper -> PDF page without searching for prose. It must sit
        # *inside* the page block: ``.book-page.ripper-newpage`` forces a break
        # before itself, so a marker placed in front of it would get a sheet of
        # its own (measured: one 5-character page per wrapper).
        anchor = soup.new_tag("span")
        anchor["class"] = "ripper-anchor"
        anchor.string = f"G{len(out):04d}"
        (div.find(class_="book-page") or div).insert(0, anchor)
        body.append(div)
        g["el"] = div
        out.append(g)
    return out


def _new_group(pb: str = "", ident: str | None = None) -> dict:
    return {"pb": pb, "id": ident, "nodes": [], "fit": False, "started": False,
            "parked": False, "insert": False, "carry": False, "el": None}


_INSERT_HEAD = re.compile(
    r"^appendix\s+(getting\s+started\s+with\s+r|basic\s+data\s+manipulation\s+with\s+r)\b",
    re.IGNORECASE)


def _is_insert(root) -> bool:
    """Is this reader section a loose insert the printed book does not number?

    The two R appendices are bound into the ebook without page numbers: the book
    numbers up to 33, drops them in, and resumes at 34 on Chapter 2. Printing a
    page number on them would invent a page that does not exist.
    """
    head = root.find(["h1", "h2"])
    text = " ".join((head.get_text(" ", strip=True) if head
                     else root.get_text(" ", strip=True)).split())
    return bool(_INSERT_HEAD.match(text))


def zoom_style(value: float) -> str:
    """``zoom`` that shrinks the *type* but keeps the column full width.

    A bare ``zoom: 0.8`` on a block already lays the element out 1/z wider and
    then paints it back down, so the column still fills the page while holding
    more characters per line - which is exactly what makes the page shorter.
    What broke that was a ``max-width`` shipped by the reader: it clamped the
    widened box back to its nominal width, the zoom then painted that narrower
    box, and every page ended up as a ribbon down the middle of the sheet with
    ~20% white on each side. Releasing the clamp restores the geometry.
    """
    value = max(0.30, min(1.0, float(value)))
    if abs(value - 1.0) < 5e-3:
        return ""
    return f"zoom:{value:.4f};max-width:none"


def apply_page_zoom(groups: list[dict], zoom: dict[int, float]) -> None:
    """Give each filtered page its own ``zoom``; the rest print at 100%."""
    for i, g in enumerate(groups):
        el = g.get("el")
        if el is None:
            continue
        value = zoom.get(i)
        if value is None or abs(value - 1.0) < 5e-3:
            if el.has_attr("style") and not value:
                del el["style"]
            continue
        style = zoom_style(value)
        if style:
            el["style"] = style


def measure_page_heights(html_path: str, chrome: str, width: int,
                         book=None, port: int = 9333,
                         timeout: float = 90.0) -> list[float]:
    """Height in CSS pixels of every ``.ripper-page`` at 100% zoom.

    Measured in a throwaway headless Chrome whose body is pinned to the printed
    column width, so the numbers match what the paginator will see: font sizes
    are absolute, images are capped by ``max-width: 100%`` and the only thing
    that changes between screen and print is the page box around the column.

    The printed stylesheet is injected here as well - without it the page is
    measured with the reader's *screen* typography (16px text on a 32px line
    box) and every height comes back roughly twice the real one.
    """
    import tempfile
    import time

    import cdp

    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    probe = (
        "<style data-ripper='measure'>"
        + (print_css(book=book) if book else "")
        + f"body {{ width: {int(width)}px !important; }}"
        ".ripper-page { break-before: auto !important;"
        " page-break-before: auto !important; }"
        ".book-page { break-after: auto !important;"
        " page-break-after: auto !important; }"
        "</style>"
    )
    html = html.replace("</head>", probe + "</head>", 1)

    folder = tempfile.mkdtemp(prefix="ripper-measure-")
    page_path = os.path.join(folder, "measure.html")
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(html)

    browser = cdp.launch_or_attach({
        "chrome_path": chrome, "user_data_dir": None, "headless": True,
        "browser_port": port, "attach_port": 0, "launch_timeout": timeout,
    }, log=lambda *_: None)
    try:
        browser.navigate(Path(page_path).as_uri())
        deadline = time.time() + timeout
        heights: list[float] = []
        while time.time() < deadline:
            heights = browser.eval_main("""
                const out = [];
                const els = document.querySelectorAll('.ripper-page');
                for (let i = 0; i < els.length; i++) {
                  const el = els[i];
                  const saved = el.style.zoom;
                  el.style.zoom = '1';
                  const r = el.getBoundingClientRect();
                  out.push(r.height);
                  el.style.zoom = saved;
                }
                return out;
            """) or []
            if heights:
                break
            time.sleep(0.4)
        return [float(x) for x in heights]
    finally:
        try:
            browser.close(force=True)
        except Exception:
            pass


def build_book_doc(pages, base_url=None, title: str = "eTextbook",
                   toc_entries=None, stream: bool = False,
                   page_ids=None, breaks: list | None = None,
                   link_stats: dict | None = None,
                   new_pages=None):
    """Combine cleaned webreader pages into one document.

    ``stream=False`` keeps the "1 reader page = 1 sheet" layout. ``stream=True``
    lets the text flow like a real book. Either way, pagebreak markers captured
    from the reader become real page breaks, so one PDF page holds exactly one
    textbook page.

    When ``breaks`` is given, the printed page numbers of those breaks are
    appended to it in document order (this is what binds book page -> PDF page).

    Returns ``(soup, groups)``: the document, plus the per-textbook-page wrappers
    from :func:`group_pages` (empty when ``stream`` is off). The soup is kept
    rather than serialised because printing re-writes it once per fitting pass.
    """
    soups = [BeautifulSoup(p, "lxml") for p in pages]
    ids = page_ids or [None] * len(soups)
    starts = list(new_pages or []) + [False] * (len(soups) - len(new_pages or []))
    known_ids = set()
    for s in soups:
        known_ids.update(el.get("id") for el in s.find_all(True) if el.get("id"))
    section_ids = {i for i in ids if i}
    known_ids.update(f"sec-{i}" for i in section_ids)
    known_ids.update(f"page-{n}" for n in range(1, len(soups) + 1))

    heads: list[str] = []
    articles: list[str] = []
    for i, (soup, pid) in enumerate(zip(soups, ids), start=1):
        found = paginate(soup, tail_content=(i < len(soups)))
        if breaks is not None:
            breaks.extend(found)
        stats = rewrite_links(soup, known_ids, section_ids)
        if link_stats is not None:
            for key, val in stats.items():
                link_stats[key] = link_stats.get(key, 0) + val
        h, art = _wrap_soup(soup, i, pid, new_page=bool(starts[i - 1]))
        heads.extend(h)
        articles.append(art)
    head = "\n".join(heads)
    body = "\n".join(articles)
    toc_html = build_toc_html(toc_entries or [], title=title)
    css = PAGE_BREAK_CSS + PB_CSS + (PAGE_WRAP_CSS if stream else "") + (
        STREAM_CSS if stream else "")
    soup = BeautifulSoup(
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title>{head}<style>{css}</style>"
        f"</head><body>{toc_html}{body}</body></html>", "lxml")
    groups = group_pages(soup) if stream else []
    skip: list = []
    if groups:
        numbered = [b for b in (breaks or [])
                    if b.get("num") is not None and not b.get("phantom")]
        pages = [g for g in groups if g["pb"]]
        if len(numbered) == len(pages):
            # The page wrapper knows the text the sheet really starts with; a
            # marker that closed its section had no readable text after it in
            # that soup, and its snippet would have been empty.
            dead = []
            for g, brk in zip(pages, numbered):
                text = _page_snippet(g["el"])
                if not text and not g["el"].find(["img", "svg", "table", "figure"]):
                    dead.append((g, brk))
                    continue
                if text:
                    brk["snippet"] = text
            for g, brk in dead:
                skip.append(brk.get("num"))
                (breaks or []).remove(brk)
                g["el"].extract()
                groups.remove(g)
    return {"soup": soup, "groups": groups, "skip": skip}


def _page_snippet(el, limit: int = 160) -> str:
    """First readable words of a page element (what the footer search needs).

    The invisible ``G####`` marker lives at the top of the page block, so it
    must be stripped: otherwise every page would look non-empty and the
    "this wrapper has no content, drop it" rule could never fire.
    """
    text = " ".join(el.get_text(" ", strip=True).split())
    return _ANCHOR_RE.sub("", text).strip()[:limit]


def build_book_html(pages, base_url=None, title: str = "eTextbook",
                    toc_entries=None, stream: bool = False,
                    page_ids=None, breaks: list | None = None,
                    link_stats: dict | None = None,
                    new_pages=None) -> str:
    """Combine cleaned webreader pages into one HTML document."""
    doc = build_book_doc(pages, base_url=base_url, title=title,
                         toc_entries=toc_entries, stream=stream,
                         page_ids=page_ids, breaks=breaks,
                         link_stats=link_stats, new_pages=new_pages)
    return str(doc["soup"])



def write_page_html(page_html: str, path: str, base_url: str | None = None) -> None:
    """Write one cleaned webreader page to disk so Chrome can print it.
    Injects a <base> so relative resource URLs resolve against the live reader URL."""
    soup = BeautifulSoup(page_html, "lxml")
    if base_url:
        base = soup.new_tag("base", href=base_url)
        if soup.head is not None:
            soup.head.insert(0, base)
        else:
            soup.insert(0, base)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(soup))


def print_css(page_size=None, book=None, scale: float = 1.0) -> str:
    """CSS that decides what one PDF page looks like.

    Three modes:

    * ``book=(w, h)`` - the default. The reader lays the text out in a narrow
      column (720px on a real Cengage title) inside a much wider chrome frame;
      printing the frame is what produced the huge white margins. Here the PDF
      page is the column plus a small gutter, so the text keeps its exact line
      breaks and the *content* defines the page, not the browser window.
    * ``page_size=(w, h)`` - legacy "auto": print at the measured reader frame
      size (1 reader page -> 1 PDF page, margins included).
    * neither - plain A4 portrait.
    """
    if book:
        w, h = int(book[0]), int(book[1])
        pad_x = int(book[2]) if len(book) > 2 else 16
        pad_top = int(book[3]) if len(book) > 3 else 12
        pad_bottom = int(book[4]) if len(book) > 4 else 12
        try:
            zoom = float(scale)
        except (TypeError, ValueError):
            zoom = 1.0
        zoom = min(1.0, max(0.4, zoom))
        # ``zoom`` scales the *layout*, so the column stays 720px wide while the
        # text inside it shrinks - a textbook page then fits one sheet instead
        # of spilling onto a second. Page numbers come first, legibility second.
        # The width compensation in ``zoom_style`` is what stops that shrink from
        # also narrowing the column (see there).
        zoom_css = (f".book-page {{ {zoom_style(zoom)}; }}"
                    if abs(zoom - 1.0) > 1e-4 else "")
        return (
            # Margins go on @page, not on body: body padding exists only once,
            # so continuation pages had their text glued to the top edge.
            f"@page {{ size: {w}px {h}px;"
            f" margin: {pad_top}px {pad_x}px {pad_bottom}px; }}"
            "html, body { height: auto !important; min-height: 0 !important;"
            " overflow: visible !important; background: #fff !important; }"
            # The reader paints a grey "page" behind the text; a book page is
            # white. Tables and callouts keep their own fills.
            ".book-page { background: transparent !important; }"
            + zoom_css +
            # The cover is one page of artwork. It is laid out inside the normal
            # content box (so it cannot overflow onto a second sheet) and then
            # painted over the whole page in PDF space by ``bleed_cover`` - CSS
            # cannot bleed into @page margins, and negative margins make the
            # image one page tall, which the paginator duly pushes to page 2.
            f".book-page.ripper-cover {{ height: {h - pad_top - pad_bottom}px;"
            " overflow: hidden; margin: 0 !important; padding: 0 !important; }"
            ".book-page.ripper-cover figure, .book-page.ripper-cover img {"
            " margin: 0 !important; padding: 0 !important; max-width: none !important; }"
            ".book-page.ripper-cover img, .book-page.ripper-cover .coverimage {"
            " display: block; width: 100% !important; height: auto !important; }"
            # The reader's own gutter (100px each side) is not part of the book.
            "body { margin: 0 !important; padding: 0 !important; }"
            "img, svg, table { max-width: 100% !important; }"
            # The reader is built for a scrolling screen: a 2.0 line box and a
            # ~28px gutter inside the content column. Printed, that costs a
            # third of every sheet (26 lines instead of 43) and pushes each
            # textbook page onto two sheets. A printed book is tighter, so the
            # rules below win with !important - the reader ships its own
            # stylesheet from a CDN and specificity alone is not enough.
            ".book-page { padding: 0 !important; }"
            ".book-page, .book-page section, .book-page article,"
            ".book-page .content {"
            " margin-left: 0 !important; margin-right: 0 !important;"
            " padding-left: 0 !important; padding-right: 0 !important;"
            " max-width: none !important; }"
            ".book-page, .book-page p, .book-page li, .book-page div,"
            ".book-page span, .book-page dd, .book-page dt,"
            ".book-page h1, .book-page h2, .book-page h3, .book-page h4,"
            ".book-page h5, .book-page h6 { line-height: 1.42 !important; }"
            ".book-page p { margin: 0 0 .5em !important; }"
            ".book-page li { margin: 0 !important; }"
            ".book-page ul, .book-page ol {"
            " margin: .25em 0 .5em !important; padding-left: 1.5em !important; }"
            # Tables keep their own tight leading (see STREAM_CSS).
            ".book-page table, .book-page td, .book-page th,"
            ".book-page td *, .book-page th * { line-height: 1.25 !important; }"
            # The reader ships figures at their screen size, and the tall ones
            # are taller than a sheet (a 463x940 chart is 1.4 sheets on its own),
            # so a page with two of them spilled onto four. A printed figure
            # never fills a whole page: cap it and it keeps its proportions.
            f".book-page img.figureimage {{ max-height: {int((h - pad_top - pad_bottom) * 0.62)}px !important;"
            " height: auto !important; }"
            # The appendix screenshots (the JMP and RStudio windows) are shipped
            # even larger than the charts, and they carry no ``figureimage``
            # class - capped they stay on their page, uncapped they pushed a
            # one-page appendix onto three sheets.
            f".book-page:not(.ripper-cover) img {{ max-height: {int((h - pad_top - pad_bottom) * 0.55)}px !important;"
            " height: auto !important; }"
            # Exercise answers live in <details class="toggleAnswer">. The
            # ``open`` attribute is set while cleaning, but the reader's own
            # stylesheet also hides the body of the element - so state it twice.
            ".book-page details { display: block !important; }"
            ".book-page details > *:not(summary) { display: block !important; }"
            ".book-page summary.quiz-answer-summary-button {"
            " font-weight: 700 !important; list-style: none !important; }"
            # The printed form of an answer: indented under its question, the
            # lead-in bold, and the text flowing after it on the same line -
            # a paper answer has no button to press and no reason to start a
            # new paragraph.
            ".book-page .ripper-answer { display: block !important;"
            " margin: .05em 0 .5em 1.7em !important; padding: 0 !important;"
            " text-indent: 0 !important;"
            # An answer is never split across sheets: a page that ended with a
            # bare "Answer:" above its table read as a mistake.
            " break-inside: avoid !important; page-break-inside: avoid !important; }"
            # Everything inside flattens onto that one line, including the
            # ``.correct`` wrapper's own <p> - which is why the selector is not
            # limited to direct children. Answers that carry a table or a list
            # opt out: their structure is the answer.
            ".book-page .ripper-answer:not(.ripper-answer-block) * {"
            " display: inline !important;"
            " margin: 0 !important; padding: 0 !important;"
            " background: none !important; border: 0 !important; }"
            # Two paragraphs flattened onto one line lose the space that used
            # to separate them ("...CPU ManufacturerQuantitative variables").
            ".book-page .ripper-answer:not(.ripper-answer-block) p + p::before {"
            " content: '\\00a0'; }"
            ".book-page .ripper-answer-block .ripper-answer-lead {"
            " display: block !important; margin-bottom: .15em !important; }"
            ".book-page .ripper-answer-block table { margin: .2em 0 !important; }"
            ".book-page .ripper-answer svg, .book-page .ripper-answer img {"
            " display: none !important; }"
            # The reader marks the answer with a tick and its own "Answer:"
            # label through ::before; the lead-in above already says it.
            ".book-page .ripper-answer .correct::before,"
            ".book-page .ripper-answer .correct::after {"
            " content: none !important; }"
            ".book-page .ripper-answer-lead { font-weight: 700 !important;"
            " font-style: normal !important; }"
            # Controls the reader draws over the text: the blue "+" that opens
            # a figure enlarged, the media buttons. They are screen furniture,
            # not part of the book.
            ".book-page .zoom-image-indicator, .book-page .figure-zoom,"
            ".book-page .media-button, .book-page button.zoom {"
            " display: none !important; }"
            # "Table 1.6" and its caption are two inline boxes with nothing
            # between them, so the printed title ran together as
            # "Table 1.6Product Information".
            ".book-page header .label { margin-right: .4em !important; }"
            # Long sections flow onto as many PDF pages as they need.
            "section, .chapter { break-inside: auto; }"
        )
    if page_size:
        w, h = int(page_size[0]), int(page_size[1])
        if 0 < w <= 5000 and 0 < h <= 30000:
            return (
                f"@page {{ size: {w}px {h}px; margin: 0; }}"
                "html, body { height: auto !important; min-height: 0 !important;"
                " overflow: visible !important; }"
            )
    return "@page { size: A4; margin: 10mm; }"


def inject_print_css(html_str: str, page_size=None, book=None,
                     scale: float = 1.0) -> str:
    soup = BeautifulSoup(html_str, "lxml")
    style = soup.new_tag("style")
    style["data-ripper"] = "print"
    style.string = print_css(page_size, book=book, scale=scale)
    if soup.head is not None:
        soup.head.append(style)
    else:
        soup.insert(0, style)
    return str(soup)


def html_to_pdf(html_path: str, pdf_path: str, chrome: str = "chrome",
                page_size=None, book=None, scale: float = 1.0) -> None:
    """Render an HTML file to PDF with headless Chrome (real, searchable text).

    Uses ``Path.as_uri()`` rather than ``file://{abs_path}`` because the naive
    form produces ``file://E:\\...`` on Windows, which Chrome reads as UNC host
    "E:" and fails with ERR_FILE_NOT_FOUND while still exiting 0.
    """
    src = Path(html_path).resolve()
    dst = Path(pdf_path).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    target = src
    temp = None
    if page_size is not None or book is not None:
        # Print from a copy carrying the measured @page size; the captured page
        # file itself stays pristine so --rebuild can re-print at a new size.
        with open(src, encoding="utf-8") as f:
            patched = inject_print_css(f.read(), page_size, book=book,
                                       scale=scale)
        temp = dst.with_suffix(".print.html")
        with open(temp, "w", encoding="utf-8") as f:
            f.write(patched)
        target = temp

    try:
        cmd = [
            chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
            "--no-pdf-header-footer",          # no date/URL banner on every page
            f"--print-to-pdf={dst}",
            target.as_uri(),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    finally:
        if temp is not None and temp.exists():
            temp.unlink()

    if res.returncode != 0:
        raise RuntimeError(
            f"Chrome failed ({res.returncode}): {res.stderr[-500:]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(
            f"Chrome produced no PDF for {target.as_uri()}: {res.stderr[-500:]}")


def pdf_page_count(path: str) -> int:
    """How many PDF pages one rendered page produced (usually 1, sometimes 2)."""
    if pikepdf is None:  # pragma: no cover
        return 1
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def merge_pdfs(pdf_paths: list[str], out_path: str) -> list[int]:
    """Concatenate page-ordered PDFs into one file.

    Returns each source's page count so callers can map a source page to its
    final PDF page index (needed for accurate bookmarks).
    """
    if pikepdf is None:  # pragma: no cover
        raise RuntimeError("pikepdf is required for merge_pdfs")
    counts: list[int] = []
    with pikepdf.new() as out:
        for p in pdf_paths:
            with pikepdf.open(p) as src:
                counts.append(len(src.pages))
                out.pages.extend(src.pages)
        out.save(out_path)
    return counts
