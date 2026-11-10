"""TOC / PDF outline generation from captured Cengage webreader pages.

Every webreader page carries enough signal to build a real bookmark tree:

  * ``<title>``                   -> the page title the reader itself displays
  * ``<body data-cgi="...">``     -> a BOOK-level id (same on every page - not a page key)
  * ``<body epub:type="...">``    -> semantic role (frontmatter / bodymatter / ...)
  * first ``<h1>..<h6>``          -> section heading, whose tag number is the level

Pipeline: ``page_meta`` (harvest one page) -> ``build_toc`` (page sequence ->
flat outline) -> ``normalize_levels`` -> ``nest`` (tree) -> ``apply_outline``
(pikepdf writes the real PDF bookmarks).

The ``pdf_page`` of an entry comes from the caller: continuous layout reads it
back out of the finished PDF (``pdfmap.py``), per-page layout counts pages.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_WS = re.compile(r"\s+")
# "1.2.3 Something", "Chapter 4: X", "Unit 7 - Y", "12 Exercises"
_NUMBERED = re.compile(
    r"^\s*(?:(?:chapter|unit|part|module|section)\s*)?(\d+)((?:\.\d+)*)(?=[\s.:\-–—)]|$)",
    re.I)


def _tidy(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip()


def numbering_level(title: str) -> int:
    """Infer a heading depth from textbook numbering ("1.2.3" -> 3).

    Headings alone are unreliable in this reader: a sub-section frequently
    arrives as ``<h1>``, which would flatten the whole outline. The printed
    number is a much better level hint and costs nothing.
    """
    m = _NUMBERED.match(_tidy(title))
    if not m:
        return 0
    major, tail = m.group(1), m.group(2)
    if len(major) == 4 and not tail:
        return 0  # a year such as "2024 Cengage", not a section number
    depth = len([part for part in tail.split(".") if part])
    return min(1 + depth, 3)


def page_meta(webreader_html: str, page_number: int | None = None) -> dict:
    """Harvest title / heading / level / epub:type / data-cgi from one page.

    ``page_number`` is the *printed* page label from the reader chrome
    (``#pageNumber``), kept for reference and for human-readable reports.
    """
    soup = BeautifulSoup(webreader_html, "lxml")
    body = soup.find("body")

    cgi = ""
    epub_type = ""
    if body is not None:
        cgi = (body.get("data-cgi") or "").strip()
        epub_type = (body.get("epub:type") or "").strip()

    title_tag = soup.find("title")
    title = _tidy(title_tag.get_text()) if title_tag else ""

    heading, level = "", 0
    for tag in soup.find_all(HEADING_TAGS):
        txt = _tidy(tag.get_text(" ", strip=True))
        if txt:
            heading, level = txt, int(tag.name[1])
            break

    if not title:
        title = heading

    return {
        "title": title,
        "heading": heading,
        "level": max(level or 1, numbering_level(title)),
        "epub_type": epub_type,
        "cgi": cgi,
        "page_label": page_number,
    }


def build_toc(metas: list[dict], per_page: bool = False,
              max_level: int = 3) -> list[dict]:
    """Turn an ordered page-metadata list into a flat outline.

    By default consecutive pages sharing a title are collapsed into a single
    bookmark pointing at the first of them (a chapter spread over 6 pages is
    still one TOC entry, not six). ``per_page=True`` keeps every page instead.
    """
    entries: list[dict] = []
    prev_title: str | None = None

    for meta in metas:
        title = _tidy(meta.get("title") or meta.get("heading") or "")
        if not title:
            title = f"Page {meta.get('page', len(entries) + 1)}"

        if not per_page and title == prev_title:
            prev_title = title
            continue
        prev_title = title

        level = int(meta.get("level") or 1)
        level = max(1, min(level, max_level))

        entries.append({
            "title": title,
            "level": level,
            "page": int(meta.get("page", len(entries) + 1)),
            "pdf_page": int(meta.get("pdf_page") or meta.get("page", 1)),
            "cgi": meta.get("cgi") or "",
            "epub_type": meta.get("epub_type") or "",
            "page_label": meta.get("page_label"),
        })

    return normalize_levels(entries)


def normalize_levels(entries: list[dict]) -> list[dict]:
    """Force a valid outline shape: first entry level 1, no level jumps > 1."""
    out: list[dict] = []
    prev = 0
    for entry in entries:
        level = max(1, int(entry.get("level") or 1))
        if prev == 0:
            level = 1
        elif level > prev + 1:
            level = prev + 1
        item = dict(entry)
        item["level"] = level
        out.append(item)
        prev = level
    return out


def nest(entries: list[dict]) -> list[dict]:
    """Fold a flat, level-annotated outline into a children tree."""
    root: list[dict] = []
    stack: list[tuple[int, list[dict]]] = [(0, root)]

    for entry in entries:
        node = {
            "title": entry["title"],
            "level": entry["level"],
            "pdf_page": entry.get("pdf_page", entry.get("page", 1)),
            "children": [],
        }
        while len(stack) > 1 and stack[-1][0] >= node["level"]:
            stack.pop()
        stack[-1][1].append(node)
        stack.append((node["level"], node["children"]))

    return root


def _to_outline_item(node: dict, n_pages: int, pikepdf):
    index = int(node.get("pdf_page", 1)) - 1
    index = max(0, min(index, max(n_pages - 1, 0)))
    item = pikepdf.OutlineItem(node["title"], index)
    for child in node.get("children", []):
        item.children.append(_to_outline_item(child, n_pages, pikepdf))
    return item


def apply_outline(pdf_path: str, entries: list[dict],
                  book_title: str | None = None) -> int:
    """Write bookmarks into ``pdf_path`` in place. Returns bookmark count."""
    import pikepdf

    if not entries:
        return 0

    tree = nest(entries)
    written = 0

    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        n_pages = len(pdf.pages)
        with pdf.open_outline() as outline:
            for node in tree:
                outline.root.append(_to_outline_item(node, n_pages, pikepdf))
                written += 1

        # Open the bookmark panel by default; set document title too.
        try:
            pdf.Root.PageMode = pikepdf.Name("/UseOutlines")
        except Exception:
            pass
        if book_title:
            with pdf.open_metadata() as meta:
                meta["dc:title"] = book_title
            if "/Title" not in pdf.docinfo:
                pdf.docinfo["/Title"] = book_title

        pdf.save(pdf_path)

    return written
