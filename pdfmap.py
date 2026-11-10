"""Map reader pages to PDF pages in a continuously printed book.

Printing each reader page as its own PDF keeps page numbers trivial, but a
reflowable Cengage title has *short* sections: half a page of text followed by
a forced page break, i.e. a document full of white tails that looks like a
slide deck rather than a book.

So the book is printed as ONE continuous document and the bookmark targets are
recovered from the finished PDF: each reader page starts with its heading, and
we look for that heading in the PDF's own (real, searchable) text layer in
reading order. No drift, because we never compute offsets - we read them.
"""
from __future__ import annotations

import re
import unicodedata

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - optional, a fallback covers it
    fitz = None

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Casefold + collapse whitespace: PDF text extraction inserts newlines."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", text or "")).strip().lower()


def _compact(text: str) -> str:
    """Whitespace-free form, for text whose words lost their spaces (kerning)."""
    return _WS.sub("", unicodedata.normalize("NFKC", text or "")).lower()


def needles(meta: dict, max_len: int = 48) -> list[str]:
    """Strings that should appear on the section's first PDF page."""
    out: list[str] = []
    for key in ("title", "heading"):
        n = _norm(str(meta.get(key) or ""))[:max_len].strip()
        if len(n) >= 4 and n not in out:
            out.append(n)
    return out


# Fallback for text whose punctuation the PDF layer drops: a page that opens
# with "Statistics in Practice Bloomberg Businessweek * New York" comes back
# without the asterisk, and a needle carrying it never matches.
_ALNUM = re.compile(r"[^0-9a-z]+")


def _plain(text: str) -> str:
    return _ALNUM.sub("", _norm(text))


def _page_lines(pdf_path: str) -> list[list[str]]:
    """Normalised text lines per page (headings are lines, not substrings)."""
    out: list[list[str]] = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            lines = []
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    txt = _norm("".join(sp.get("text", "")
                                        for sp in line.get("spans", [])))
                    if txt:
                        lines.append(txt)
            out.append(lines)
    return out


def _line_hit(needle: str, lines: list[str]) -> bool:
    """Does one of ``lines`` look like this heading rather than a mention?

    A heading owns its line (``1.1 Applications in Business``), whereas a
    passing reference sits inside a full-width sentence. Requiring the line to
    be barely longer than the needle rejects the latter, which is what made
    bookmarks land a page early.
    """
    for line in lines:
        if line == needle:
            return True
        if needle in line and len(line) <= len(needle) + 24:
            return True
    return False


def _search(needle: str, lines: list[list[str]], blob: list[str],
            compact: list[str], start: int) -> int | None:
    for i in range(start, len(lines)):
        if _line_hit(needle, lines[i]):
            return i + 1
    # Loose fallback: the heading lost its own line (wrapped, or glued to text).
    cn = needle.replace(" ", "")
    for i in range(start, len(blob)):
        if needle in blob[i] or (cn and cn in compact[i]):
            return i + 1
    return None


def locate_section_pages(pdf_path: str, metas: list[dict],
                         misses: list[int] | None = None) -> list[int]:
    """1-based PDF page where each reader page begins.

    Searches forward only (sections appear in reading order), so a title that is
    also mentioned in running text cannot drag a bookmark *backwards*.

    A section whose heading is nowhere in the text layer (an image-only title,
    for instance) is parked on the previous section's page; its index is
    appended to ``misses`` so the caller can warn about it.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is required to map bookmarks; "
                           "pip install pymupdf")
    lines = _page_lines(pdf_path)
    blob = [_norm(" ".join(ls)) for ls in lines]
    compact = [_compact(b) for b in blob]
    pages: list[int] = []
    prev = 0
    for n, meta in enumerate(metas):
        start = prev if prev else 0
        hit = None
        for needle in needles(meta):
            hit = _search(needle, lines, blob, compact, start)
            if hit:
                break
        if hit is None:
            if misses is not None:
                misses.append(n + 1)
            hit = pages[-1] if pages else 1     # same page as the previous one
        pages.append(hit)
        prev = hit - 1
    return pages


def locate_snippets(pdf_path: str, snippets: list[str],
                    start_page: int = 1) -> list[int | None]:
    """1-based PDF page of each text snippet, searching forward only.

    Used for the pagebreak markers: the text right after a break sits at the top
    of a textbook page, so finding it tells us which PDF page that page became.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is required to locate page breaks; "
                           "pip install pymupdf")
    lines = _page_lines(pdf_path)
    blob = [_norm(" ".join(ls)) for ls in lines]
    compact = [_compact(b) for b in blob]
    plain = [_plain(b) for b in blob]
    out: list[int | None] = []
    cursor = max(start_page - 1, 0)
    for raw in snippets:
        text = _norm(raw or "")
        # The very first words can end up on the previous page (a heading and
        # the first sentence of a paragraph are often split), so try a couple of
        # windows into the snippet instead of only its beginning.
        hit = None
        # Shorter and shorter openings: a page begins with "Figure 1.5 Histogram
        # of Per Capita GDP for 60 Nations", and a 90-character needle that runs
        # on into a reader-only "Details" button never matches it.
        for size in (90, 64, 44, 28):
            for start in (0, 12, 30):
                needle = text[start:start + size]
                if len(needle) < 14:
                    continue
                for i in range(cursor, len(blob)):
                    if needle in blob[i]:
                        hit = i + 1
                        break
                if hit is None:
                    cn = needle.replace(" ", "")
                    for i in range(cursor, len(compact)):
                        if cn and cn in compact[i]:
                            hit = i + 1
                            break
                if hit is None:
                    pn = _plain(needle)
                    if len(pn) >= 12:
                        for i in range(cursor, len(plain)):
                            if pn in plain[i]:
                                hit = i + 1
                                break
                if hit:
                    break
            if hit:
                break
        out.append(hit)
        if hit:
            cursor = hit - 1                 # same page may hold the next break
    return out


def estimate_section_pages(metas: list[dict], total_pages: int) -> list[int]:
    """Fallback without PyMuPDF: spread sections over the book by text size."""
    if not metas:
        return []
    weights = [max(len(str(m.get("title") or "")) + 200, 1) for m in metas]
    total = sum(weights)
    pages, acc = [], 0.0
    for w in weights:
        pages.append(min(int(acc / total * max(total_pages - 1, 0)) + 1,
                         max(total_pages, 1)))
        acc += w
    return pages


def apply_pages(metas: list[dict], pages: list[int]) -> None:
    for meta, page in zip(metas, pages):
        meta["pdf_page"] = int(page)


def refine_sections(pdf_path: str, metas: list[dict], rough: list[int],
                    total: int) -> list[int]:
    """Pull each bookmark onto the sheet that really carries its heading.

    ``rough`` is where the page wrappers say each reader section starts - the
    same map the page numbers come from, so it is already trustworthy. But a
    reader section can open in the middle of a sheet (the tail of the previous
    book page above its heading), and two sections can share one wrapper (the
    unnumbered appendices). So each heading is looked for again, in its own
    window: from the previous section's sheet to the next one's. A heading owns
    its line (``_line_hit``), which is what keeps the word "Content" from
    matching running text, and the window keeps one miss from dragging every
    later bookmark with it.
    """
    if fitz is None or not metas or not rough:
        return list(rough)
    lines = _page_lines(pdf_path)
    blob = [_norm(" ".join(ls)) for ls in lines]
    compact = [_compact(b) for b in blob]
    out: list[int] = []
    for i, meta in enumerate(metas):
        base = rough[i] if i < len(rough) else (out[-1] if out else 1)
        lo = out[i - 1] if i else 1
        hi = rough[i + 1] if i + 1 < len(rough) else total
        if hi < lo:
            hi = lo
        hit = None
        for needle in needles(meta):
            got = _search(needle, lines, blob, compact, max(lo - 1, 0))
            if got and got <= hi:
                hit = got
                break
        out.append(int(hit or base))
    return out


_ANCHOR_RE = re.compile(r"G(\d{4})")


def locate_anchors(pdf_path: str) -> dict[int, int]:
    """Wrapper index -> 1-based PDF page, read from the invisible markers.

    ``group_pages`` writes ``<span class="ripper-anchor">G0042</span>`` at the
    top of every wrapper (1px, transparent). Chrome keeps that text in the PDF
    text layer, so this map is exact - unlike searching for the wrapper's
    opening prose, which failed for two thirds of the wrappers of a 734-page
    book: formulas are images, "Content" / "Glossary" repeat on every chapter,
    and a paragraph's first words often land on the previous sheet.
    """
    if fitz is None:
        return {}
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return {}
    out: dict[int, int] = {}
    try:
        for page_no, page in enumerate(doc, 1):
            text = "".join((page.get_text("text") or "").split())
            for match in _ANCHOR_RE.finditer(text):
                out.setdefault(int(match.group(1)), page_no)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return out
