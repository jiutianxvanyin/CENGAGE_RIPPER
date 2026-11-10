"""Bind the textbook's printed page numbers to the PDF's own page numbers.

The reader ships the pagination inside the content: one
``<span epub:type="pagebreak" data-after="58">`` sits exactly where page 58
begins. ``pdf_builder.paginate`` turns each of those into a real page break, so
one PDF page holds exactly one textbook page - that is what makes "book page N"
and "PDF page N" the same thing instead of two drifting sequences.

What is left for this module:

* find where each break actually landed in the printed PDF (via the snippet of
  text that follows it, read back from the PDF's own text layer),
* repair the rare holes - a page whose marker was swallowed produces a gap, and
  a gap would shift every following page - by inserting blank pages so the
  numbers stay in sync,
* stamp the printed page number in the bottom margin and write ``/PageLabels``
  so viewers that honour page labels (Acrobat, Preview) show the book's numbers
  in their page field too.
"""
from __future__ import annotations

import re

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - optional
    fitz = None

try:
    import pikepdf
except ImportError:  # pragma: no cover - optional
    pikepdf = None

import pdfmap

_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip()


def plan(breaks: list[dict], located: list[int | None], total_pages: int,
         skip: list[int | None] = ()) -> dict:
    """Decide the book-page <-> PDF-page mapping and the blanks it needs.

    ``breaks``   one dict per page break, in document order: ``{"num": 58,
                 "snippet": "..."}`` (``num`` may be ``None``).
    ``located``  1-based PDF page of each break (``None`` when not found).
    ``skip``     printed numbers of pages that exist in the book but hold no
                 content in the capture (the reader parks their markers in one
                 block, e.g. ``data-after="from i to xxi"``). They occupy no
                 sheet, so they must not be mistaken for missing pages - that
                 would insert blanks that shift every following page.

    Every break starts a PDF page, so the textbook page that follows it owns the
    pages from there up to the next break. A perfect book prints one page per
    page; a textbook page that spilled (a full-page table) owns two or more and
    is flagged, because that is where the two numberings stop matching 1:1.
    """
    entries: list[dict] = []                  # one per PDF page that starts a page
    merged: list[int] = []
    warnings: list[str] = []
    prev_page = 0

    for k, (brk, page) in enumerate(zip(breaks, located)):
        num = brk.get("num")
        if page is None:
            page = prev_page + 1 if prev_page else None
            if page is None:
                warnings.append("the first page break could not be located by "
                                "its text; numbering may be off")
                continue
            warnings.append(f"page break {num if num is not None else k + 1} was "
                            f"not found in the PDF; assuming page {page}")
        if page == prev_page:                 # two markers, one page (table page)
            # The reader cannot put a marker *inside* a table, so it parks the
            # markers of a table's pages next to each other. The page that
            # follows therefore starts at the first of them.
            if num is not None:
                merged.append(num)
            continue
        entries.append({"pdf_page": page, "book_page": num,
                        "style": brk.get("style") or ("" if num is None else "D"),
                        "label": brk.get("label") or ("" if num is None else str(num))})
        prev_page = page

    blanks: list[tuple[int, int]] = []
    inserts = 0
    absent = list(merged) + [n for n in skip if n is not None]
    labels: dict[int, tuple[str, bool]] = {}   # sheet -> (printed label, (cont.))
    for i, entry in enumerate(entries):
        nxt = entries[i + 1] if i + 1 < len(entries) else None
        entry["pdf_span"] = (nxt["pdf_page"] - entry["pdf_page"]) if nxt else (
            total_pages - entry["pdf_page"] + 1)
        if i == 0:
            continue
        prev = entries[i - 1]
        if entry["book_page"] is None or prev["book_page"] is None:
            continue
        num_gap = entry["book_page"] - prev["book_page"]
        pdf_gap = entry["pdf_page"] - prev["pdf_page"]
        hidden = [n for n in absent if prev["book_page"] < n < entry["book_page"]]
        if pdf_gap == num_gap and pdf_gap > 1:
            # One sheet per page, several pages between two located anchors.
            # Their markers were parked somewhere the reader could not cut the
            # text at, so they never became anchors of their own - but the
            # sheets are there, in order, and the numbering simply counts on.
            # (Page 8 opens sheet 21 and page 11 opens sheet 24; sheets 22 and
            # 23 are pages 9 and 10, not a two-sheet spill of page 8.)
            for k in range(1, pdf_gap):
                labels[prev["pdf_page"] + k] = (str(prev["book_page"] + k), False)
            continue
        if num_gap > pdf_gap > 0 and not hidden:
            need = num_gap - pdf_gap
            if 0 < need <= 4:
                blanks.append((entry["pdf_page"] - 1 + inserts, need))
                inserts += need
                warnings.append(
                    f"textbook pages {prev['book_page'] + 1}-"
                    f"{prev['book_page'] + pdf_gap} are not in the capture; "
                    f"padded with {need} blank page(s) to keep the numbers in place")
            elif need > 4:
                warnings.append(
                    f"textbook pages {prev['book_page'] + 1}-"
                    f"{entry['book_page'] - 1} are missing ({need} pages) - not "
                    f"padded, that gap looks like a numbering change, not a hole")
        elif pdf_gap > num_gap > 0:
            if hidden:
                # The reader parked these markers, so we cannot cut the text at
                # them - but the pages are in the capture, in order, and the
                # sheets between the two anchors are exactly where they are.
                # Give each spare sheet the next parked number instead of
                # repeating the previous one (which reads as a printing error).
                spare = pdf_gap - 1
                for k in range(spare):
                    filler = hidden[k] if k < len(hidden) else hidden[-1]
                    held = k >= len(hidden)
                    labels[prev["pdf_page"] + k + 1] = (str(filler), held)
            else:
                warnings.append(
                    f"textbook page {entry['book_page']} spilled over {pdf_gap} "
                    f"PDF pages (long table/figure) - numbering drifts by "
                    f"{pdf_gap - num_gap}")

    for entry in entries:
        entry["pdf_page"] += _inserts_before(blanks, entry["pdf_page"])
    for entry in entries:
        if entry["pdf_span"] > 1:
            entry["spilled"] = True

    numbered = [e for e in entries if e["book_page"] is not None]
    front_pages = (numbered[0]["pdf_page"] - 1) if numbered else max(total_pages, 1)
    perfect = bool(numbered) and all(e["pdf_span"] == 1 for e in entries) and not merged
    return {
        "blanks": blanks,
        "inserts": inserts,
        "map": entries,
        "merged": merged,
        "filler": labels,
        "front_pages": max(front_pages, 0),
        "body_start_number": numbered[0]["book_page"] if numbered else None,
        "total_pages": total_pages + inserts,
        "perfect": perfect,
        "warnings": warnings,
    }


def _inserts_before(blanks: list[tuple[int, int]], pdf_page: int) -> int:
    """How many blank pages were pushed in front of this 1-based PDF page."""
    return sum(count for index, count in blanks if index + 1 <= pdf_page)



def locate_breaks(pdf_path: str, breaks: list[dict]) -> list[int | None]:
    """1-based PDF page of each break, found through the text that follows it."""
    snippets = [_clean(b.get("snippet") or "") for b in breaks]
    return pdfmap.locate_snippets(pdf_path, snippets)


# --------------------------------------------------------------------------- #
# PDF surgery
# --------------------------------------------------------------------------- #
def insert_blanks(pdf_path: str, blanks: list[tuple[int, int]]) -> int:
    """Insert empty pages (same paper size) before the given 0-based indices."""
    if not blanks or pikepdf is None:
        return 0
    added = 0
    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        box = pdf.pages[0].obj.get("/MediaBox")
        for index, count in blanks:
            index = min(max(index, 0), len(pdf.pages))
            for _ in range(count):
                blank = pikepdf.Page(pdf.make_indirect(pikepdf.Dictionary(
                    Type=pikepdf.Name("/Page"),
                    MediaBox=box,
                    Resources=pikepdf.Dictionary(),
                    Contents=pdf.make_stream(b""),
                )))
                pdf.pages.insert(index, blank)
                index += 1
                added += 1
        pdf.save(pdf_path)
    return added


def segment_of(planinfo: dict, pdf_page: int) -> tuple[int | None, bool]:
    """(textbook page on this PDF page, is it a continuation of that page?)"""
    best = None
    for entry in planinfo.get("map") or []:
        if entry["pdf_page"] <= pdf_page:
            best = entry
        else:
            break
    if best is None:
        return None, False
    same = best.get("pdf_span", 1) > 1
    return best["book_page"], same and pdf_page > best["pdf_page"]


def book_page_of_pdf(planinfo: dict, pdf_page: int) -> int | None:
    """Which textbook page a 1-based PDF page holds (``None`` = front matter)."""
    return segment_of(planinfo, pdf_page)[0]


_ROMAN = (("m", 1000), ("cm", 900), ("d", 500), ("cd", 400), ("c", 100),
          ("xc", 90), ("l", 50), ("xl", 40), ("x", 10), ("ix", 9), ("v", 5),
          ("iv", 4), ("i", 1))


def roman(number: int) -> str:
    """``1`` -> ``"i"``; front matter is counted in roman numerals."""
    number = int(number)
    if number <= 0:
        return ""
    out = []
    for glyph, value in _ROMAN:
        while number >= value:
            out.append(glyph)
            number -= value
    return "".join(out)


def sheet_label(planinfo: dict, pdf_page: int) -> str:
    """Roman sheet number for a front-matter page (``i``, ``ii``, ``iii`` …).

    This is the *sheet* order inside this PDF, not the roman page the book
    prints: the reader hands a book's whole front matter over as one parked
    block of markers (``data-after="from i to xxi"``), so the true mapping is
    not recoverable. A sheet number is still better than a blank footer -
    without it a 16-page front matter has no visible numbering at all - and
    roman numerals say "front matter" the way the book does.
    """
    front = int(planinfo.get("front_pages") or 0)
    if 1 <= pdf_page <= front:
        return roman(pdf_page)
    return ""


_ROMAN_VALUE = dict(i=1, v=5, x=10, l=50, c=100, d=500, m=1000)


def roman_value(text: str) -> int | None:
    """``"xxvii"`` -> 27; ``None`` when it is not a roman numeral."""
    text = (text or "").strip().lower()
    if not text or any(ch not in _ROMAN_VALUE for ch in text):
        return None
    total = 0
    for i, ch in enumerate(text):
        value = _ROMAN_VALUE[ch]
        nxt = _ROMAN_VALUE[text[i + 1]] if i + 1 < len(text) else 0
        total += -value if value < nxt else value
    return total or None


def label_value(text: str) -> tuple[int | None, str]:
    """A printed page label -> (number, style ``"D"``/``"r"``)."""
    text = (text or "").strip().lower()
    if text.isdigit():
        return int(text), "D"
    value = roman_value(text)
    if value:
        return value, "r"
    return None, ""


def plan_labels(anchors, total_pages: int) -> dict:
    """Number every sheet from the page labels the reader showed.

    The reader prints the real book page in its page box (``i``, ``xxv``,
    ``xxvii``, ``1``, ``6``…), and every captured section records it. Those are
    facts about the book, unlike the pagebreak markers, which the reader parks
    in blocks and often cannot place. So each located section start becomes an
    anchor: the sheets between two anchors count up from the earlier one, and
    the anchor itself overrules the count - drift cannot accumulate.

    Returns ``{"labels": {page: (text, style, value)}, "segments": [(index,
    style, start)], "anchors": [(page, text)]}``.
    """
    points: list[tuple[int, int, str, str]] = []
    seen = set()
    for page, text in anchors or []:
        value, style = label_value(text)
        if value is None or page in seen:
            continue
        seen.add(page)
        points.append((int(page), value, style, (text or "").strip()))
    points.sort()
    if not points:
        return {"labels": {}, "segments": [], "anchors": []}

    labels: dict[int, tuple[str, str, int]] = {}
    segments: list[tuple[int, str, int]] = []

    first_page, first_value, first_style, _ = points[0]
    if first_page > 1:
        # Count backwards from the first anchor so the front matter is numbered
        # too; stop at 1 - there is no page 0 to invent.
        back = []
        for page in range(first_page - 1, 0, -1):
            value = first_value - (first_page - page)
            if value < 1:
                break
            back.append((page, value))
        if back:
            start = back[-1]
            segments.append((start[0] - 1, first_style, start[1]))
        for page, value in back:
            labels[page] = (_print_number(value, first_style), first_style, value)

    for idx, (page, value, style, text) in enumerate(points):
        end = points[idx + 1][0] if idx + 1 < len(points) else total_pages + 1
        # Never count past the next anchor: this section may occupy more sheets
        # than it has pages (or fewer), and the anchor is the truth.
        cap = points[idx + 1][1] - 1 if idx + 1 < len(points) else None
        segments.append((page - 1, style, value))
        for step, sheet in enumerate(range(page, end)):
            number = value + step
            if cap is not None and cap >= value and number > cap:
                number = cap
            labels[sheet] = (_print_number(number, style), style, number)

    return {"labels": labels,
            "segments": segments,
            "anchors": [(p[0], p[3]) for p in points]}


def _print_number(value: int, style: str) -> str:
    return roman(value) if style == "r" else str(value)


def label_of_pdf(planinfo: dict, pdf_page: int) -> tuple[str, bool]:
    """Printed label on this PDF page (``"58"``, ``"xxvii"`` or ``""``).

    Separate from :func:`segment_of` because the printed form is not always a
    number: front matter counts in roman numerals, and stamping ``27`` where the
    book prints ``xxvii`` would be worse than stamping nothing.
    """
    number, continued = segment_of(planinfo, pdf_page)
    filler = (planinfo.get("filler") or {}).get(pdf_page)
    if filler:
        # A parked page that owns this spare sheet inside the previous anchor's
        # span. Its number is the book's, its position is a best fit.
        return filler[0], filler[1]
    if number is None:
        return "", False
    for entry in reversed(planinfo.get("map") or []):
        if entry["pdf_page"] <= pdf_page and entry.get("book_page") is not None:
            return (entry.get("label") or str(number)), continued
    return str(number), continued

def stamp_numbers(pdf_path: str, planinfo: dict, font_size: float | None = None,
                  colour: tuple | None = None, skip=()) -> int:
    """Write a page number into the footer band of every page.

    Body pages get the textbook page number (``58``, repeats of a spilled page
    get ``58 (cont.)``); front matter gets its roman sheet number (``i``), because
    a PDF whose first 16 pages carry no number at all looks broken. ``skip``
    lists 1-based pages to leave alone - the cover, which is artwork and is never
    numbered in a printed book.

    The size follows the sheet: these pages are printed 7.8in x 15.6in, so a page
    number pinned at 8.5pt renders as a speck once the viewer fits the page to a
    screen. ``height/100`` (≈11.3pt here) matches the 12pt body text, which is
    how a printed book does it.
    """
    skip = set(skip or ())
    if fitz is None:
        return 0
    sheets_only = bool(planinfo.get("sheet_only"))
    stamped = 0
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            if i in skip:
                continue
            if sheets_only:
                label, continued = str(i), False
            else:
                # The reader's own page labels win: they are the printed book.
                # When they exist they are also the *only* source - a sheet the
                # book does not number (title page, copyright) stays blank
                # instead of getting an invented roman numeral.
                known = planinfo.get("labels") or {}
                entry = known.get(i)
                if entry:
                    label, continued = entry[0], False
                elif known:
                    continue
                else:
                    label, continued = label_of_pdf(planinfo, i)
                    if not label:
                        label, continued = sheet_label(planinfo, i), False
            if not label:
                continue
            rect = page.rect
            size = font_size or round(min(14.0, max(9.0, rect.height * 0.0125)), 1)
            ink = colour or (0.12, 0.12, 0.12)
            font = "hebo"
            text = f"{label} (cont.)" if continued else label
            roman_page = not continued and not label.isdigit()
            width = fitz.get_text_length(text, fontname=font, fontsize=size)
            centre = rect.width / 2
            baseline = rect.height - size * 0.9
            page.insert_text(fitz.Point(centre - width / 2, baseline), text,
                             fontname=font, fontsize=size, color=ink)
            if roman_page:
                # A lone roman "i" is a hairline and disappears once the viewer
                # fits a 15.6in sheet to a screen; the rules frame it the way a
                # printed front matter does. Drawn, not typed - a dash character
                # is missing from the built-in font and would print as a dot.
                rule = size * 0.85
                gap = size * 0.5
                y = baseline - size * 0.32
                for x0, x1 in ((centre - width / 2 - gap, centre - width / 2 - gap - rule),
                               (centre + width / 2 + gap, centre + width / 2 + gap + rule)):
                    page.draw_line(fitz.Point(x0, y), fitz.Point(x1, y),
                                   color=ink, width=size * 0.055)
            stamped += 1
        doc.save(pdf_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    return stamped


def write_page_labels(pdf_path: str, planinfo: dict) -> bool:
    """Make the viewer's page box show the same number as the footer.

    When the numbering came from the reader's own labels (``plan_labels``) every
    segment is known exactly, so the labels are written as-is - one run per
    anchor, which is also how a jump (page 6 -> page 8) gets recorded.
    The marker-based fallback only writes when one PDF page is one textbook
    page; otherwise the label would start pointing at the wrong page.
    """
    if pikepdf is None:
        return False
    segments = planinfo.get("segments") or []
    if segments:
        nums = pikepdf.Array()
        for index, style, start in segments:
            entry = pikepdf.Dictionary(S=pikepdf.Name("/" + style))
            if int(start) > 1:
                entry.St = int(start)
            nums.append(int(index))
            nums.append(entry)
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            pdf.Root.PageLabels = pdf.make_indirect(pikepdf.Dictionary(Nums=nums))
            pdf.save(pdf_path)
        return True
    front = int(planinfo.get("front_pages") or 0)
    start = planinfo.get("body_start_number")
    if not start:
        # Front matter only (the user asked for everything before Chapter 1):
        # one roman run describes the whole file.
        if front <= 0:
            return False
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            pdf.Root.PageLabels = pdf.make_indirect(pikepdf.Dictionary(
                Nums=pikepdf.Array([0, pikepdf.Dictionary(S=pikepdf.Name("/r"))])))
            pdf.save(pdf_path)
        return True
    if not planinfo.get("perfect"):
        return False
    styles = {e.get("style") for e in (planinfo.get("map") or [])
              if e.get("book_page") is not None}
    if len(styles) > 1:
        # Front matter in roman + body in decimal needs two label runs; a single
        # /PageLabels segment would renumber one of them wrongly.
        return False
    nums = pikepdf.Array()
    if front > 0:
        nums.append(0)
        nums.append(pikepdf.Dictionary(S=pikepdf.Name("/r")))
    nums.append(front)
    nums.append(pikepdf.Dictionary(S=pikepdf.Name("/D"), St=int(start)))
    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        pdf.Root.PageLabels = pdf.make_indirect(pikepdf.Dictionary(Nums=nums))
        pdf.save(pdf_path)
    return True


def summary(planinfo: dict) -> str:
    """One line for the console: how well the numbers line up."""
    anchors = planinfo.get("anchors") or []
    if anchors:
        shown = ", ".join(f"p.{text} = PDF p.{page}" for page, text in anchors[:4])
        more = f" (+{len(anchors) - 4} more)" if len(anchors) > 4 else ""
        style = (planinfo.get("labels") or {})
        printed = len(style)
        return (f"numbered from the book's own page labels "
                f"({len(anchors)} anchor(s): {shown}{more}); {printed} sheet(s) "
                f"numbered inside those limits")
    entries = [e for e in (planinfo.get("map") or []) if e.get("book_page") is not None]
    if not entries:
        if planinfo.get("map"):
            return ("front matter only - the book's own roman numbers are not "
                    "recoverable (the reader parks them in one block), so each "
                    "sheet is stamped with its roman sheet number")
        return "no page markers found - page numbers could not be bound"
    first, last = entries[0], entries[-1]
    span = last["pdf_page"] + last.get("pdf_span", 1) - first["pdf_page"]
    offset = first["pdf_page"] - first["book_page"]
    shown = first.get("label") or str(first["book_page"])
    text = (f"book p.{shown} = PDF p.{first['pdf_page']} "
            f"(offset {offset:+d}, {planinfo['front_pages']} front-matter page(s)); "
            f"{len(entries)} numbered pages over {span} PDF page(s)")
    if planinfo.get("perfect"):
        text += " - every textbook page gets exactly one sheet"
    else:
        spills = [e["book_page"] for e in entries if e.get("pdf_span", 1) > 1]
        if spills:
            text += f" - {len(spills)} page(s) needed more than one sheet"
        if planinfo.get("merged"):
            text += f" - {len(planinfo['merged'])} marker(s) shared a page"
    if planinfo.get("inserts"):
        text += f"; {planinfo['inserts']} blank page(s) inserted"
    return text
